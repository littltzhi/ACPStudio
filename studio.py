import argparse
import copy
import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent import EXT, METHODS, MockAgent, RPCError
from recordings import STATEFUL, desktop_prompt, document_scenario, extract_document, load_catalog, scenario, substitute


class Studio:
    def __init__(self, root, data, process, ui_port, catalog=None):
        self.root, self.data, self.process, self.ui_port = root, data, process, ui_port
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.pairing = None
        self.peers = set()
        self.events = deque(maxlen=250)
        self.speed = 1
        self.rules, self.active = {}, {}
        self.notification_rules = {}
        self.workers = set()
        self.last_error = ""
        self.stopping = threading.Event()
        self.agent = MockAgent(data, catalog or [])
        settings = data / "settings.json"
        if settings.exists():
            saved = json.loads(settings.read_text())
            self.speed = saved["speed"]
            self.rules = {method: scenario(rule) for method, rule in saved["rules"].items()}
            self.notification_rules = {name: scenario(rule) for name, rule in saved.get("notifications", {}).items()}

    def send(self, connection, message):
        with self.write_lock:
            if self.process.poll() is not None:
                raise RuntimeError("iroh 进程已退出")
            self.process.stdin.write(json.dumps({"connection": connection, "message": message}) + "\n")
            self.process.stdin.flush()
        self.log_event(connection, "out", message)

    def log_event(self, connection, direction, message):
        event = {"time": time.time(), "connection": connection, "direction": direction, "message": message}
        with self.lock:
            self.events.append(event)

    def default_rule(self, method):
        if method == "session/prompt":
            path = self.root / "examples/desktop-prompt.json"
            return scenario((path if self.agent.catalog and self.agent.definition["models"] and path.exists()
                             else self.root / "examples/reply.json").read_text())
        if method in METHODS and method not in STATEFUL and method != "_agnes/unstable/remote/session/capabilities/list":
            result, _ = self.agent.handle(method, {"sessionId": "$sessionId"})
            return {"format": "acp-studio/v1", "method": method, "frames": [
                {"offset": 0, "message": {"jsonrpc": "2.0", "id": "$rpcId", "result": result}}]}
        return {"format": "acp-studio/v1", "method": method, "frames": [
            {"offset": 0, "message": "$notifications"},
            {"offset": 0, "message": {"jsonrpc": "2.0", "id": "$rpcId", "result": "$result"}}]}

    def snapshot(self):
        with self.lock:
            return {"apiVersion": 2, "pairing": self.pairing, "peers": len(self.peers), "connections": sorted(self.peers),
                    "speed": self.speed, "events": list(self.events), "error": self.last_error,
                    "methods": sorted(set(METHODS) | self.rules.keys()), "overrides": list(self.rules),
                    "sessions": [{"sessionId": identity, "title": item["record"].get("title", identity)}
                                 for identity, item in self.agent.sessions.items()],
                    "sources": [{"name": item["source"], "warnings": item["warnings"]} for item in self.agent.catalog]}

    def reader(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
                connection = event.get("connection")
                with self.lock:
                    if event["kind"] == "ready":
                        self.pairing = event["pairing"]
                        path = self.data / "pairing.json"
                        path.write_text(json.dumps(self.pairing, indent=2))
                        path.chmod(0o600)
                    elif event["kind"] == "connected":
                        self.peers.add(connection)
                    elif event["kind"] == "closed":
                        self.peers.discard(connection)
                        for turn in self.active.values():
                            if turn["connection"] == connection:
                                turn["cancel"].set()
                    elif event["kind"] == "message":
                        self.log_event(connection, "in", event["message"])
                        self.handle(connection, event["message"])
            except Exception as error:
                self.last_error = str(error)
        if not self.stopping.is_set():
            self.last_error = "iroh 进程已停止"

    def variables(self, request, result, notifications, entry=None):
        params = request.get("params") or {}
        session_id = params.get("sessionId") or (result.get("sessionId") if isinstance(result, dict) else None)
        session = self.agent.sessions.get(session_id, {})
        return {"$rpcId": request.get("id"), "$params": params, "$result": result, "$notifications": notifications,
                "$config": {option["id"]: option["currentValue"] for option in session.get("configOptions", [])},
                "$sessionId": session_id, "$cwd": session.get("record", {}).get("cwd", params.get("cwd", "/studio")),
                "$requestId": (entry or {}).get("requestId", params.get("requestId", str(uuid.uuid4()))),
                "$messageId": (entry or {}).get("messageId", params.get("messageId", str(uuid.uuid4()))),
                "$agentMessageId": str(uuid.uuid4()), "$runId": (entry or {}).get("runId", "run_" + str(uuid.uuid4())),
                "$pageId": params.get("pageId"), "$loadId": str(uuid.uuid4()), "$waitId": str(uuid.uuid4())}

    def spawn(self, target, *arguments):
        def run():
            try:
                target(*arguments)
            finally:
                with self.lock:
                    self.workers.discard(threading.current_thread())
        worker = threading.Thread(target=run, daemon=True)
        self.workers.add(worker)
        worker.start()

    def handle(self, connection, request):
        with self.lock:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                self.send(connection, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                return
            if "id" not in request or "method" not in request:
                return
            method, params = request["method"], request.get("params") or {}
            turn = None
            try:
                if not isinstance(params, dict):
                    raise RPCError("params 必须是对象")
                rule = copy.deepcopy(self.rules.get(method))
                speed = self.speed
                scripted_error = rule and any("error" in frame["message"] for frame in rule["frames"]
                                              if isinstance(frame.get("message"), dict))
                if scripted_error and method != "session/prompt":
                    result, notifications = {}, []
                elif method == "session/prompt":
                    entry, notifications = self.agent.start_prompt(params)
                    turn = {"connection": connection, "cancel": threading.Event(), "entry": entry}
                    self.active[(entry["sessionId"], entry["requestId"])] = turn
                    result = {"stopReason": "end_turn"}
                elif method == EXT + "session/prompt/cancel":
                    session_id = params.get("sessionId")
                    self.agent.session(session_id)
                    entry = self.agent.prompts[session_id].get(params.get("requestId"))
                    if entry is None:
                        raise RPCError("找不到该请求")
                    active = self.active.get((session_id, entry["requestId"]))
                    if active:
                        active["cancel"].set()
                        entry.update(status="cancelled", stopReason="cancelled", updatedAt=int(time.time()))
                    elif entry["status"] == "queued":
                        self.agent.finish_prompt(entry, {"result": {"stopReason": "cancelled"}}, True)
                    result, notifications = {"entry": self.agent.public_entry(entry)}, []
                elif method not in METHODS and rule:
                    result, notifications = {}, []
                else:
                    result, notifications = self.agent.handle(method, params)
                self.agent.save()
                variables = self.variables(request, result, notifications, turn["entry"] if turn else None)
                self.spawn(self.play, connection, request, rule or self.default_rule(method), variables, speed, turn)
            except (RPCError, ValueError, KeyError, TypeError) as error:
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {
                    "code": error.code if isinstance(error, RPCError) else -32602, "message": str(error)}}
                self.send(connection, response)

    def play(self, connection, request, rule, variables, speed, turn=None):
        started, replied = time.monotonic(), False
        session_id = variables.get("$sessionId")
        history_sequences = []
        def bind_identities(value):
            if isinstance(value, dict):
                for item in value.values():
                    bind_identities(item)
            elif isinstance(value, list):
                for item in value:
                    bind_identities(item)
            elif isinstance(value, str) and value.startswith("$identity:"):
                with self.lock:
                    saved = self.agent.sessions[session_id].setdefault("notificationIdentities", {}) if rule.get("kind") == "notification" and session_id in self.agent.sessions else variables
                    variables[value] = saved.setdefault(value, str(uuid.uuid4()))
        bind_identities(rule["frames"])
        def emit(message, ingest=True):
            nonlocal replied
            with self.lock:
                if turn and "id" in message:
                    queue = self.agent.finish_prompt(turn["entry"], message, turn["cancel"].is_set())
                    emit(queue, False)
                if ingest and request["method"] not in ("session/load", EXT + "session/history/load") and session_id in self.agent.sessions and "method" in message:
                    self.agent.ingest(message, session_id)
                self.send(connection, message)
                if "id" in message:
                    replied = True

        try:
            if turn:
                for message in variables["$notifications"]:
                    emit(message, False)
            for frame in rule["frames"]:
                delay = max(0, frame["offset"] / speed - (time.monotonic() - started)) if speed else 0
                if (turn["cancel"] if turn else self.stopping).wait(delay) or self.stopping.is_set():
                    break
                messages = ([] if turn else variables["$notifications"]) if frame["message"] == "$notifications" else [substitute(frame["message"], variables)]
                for message in messages:
                    if message.get("method") and message.get("params", {}).get("sessionId") not in (None, session_id):
                        raise RPCError("脚本通知 sessionId 与当前请求不符；请使用 $sessionId")
                    if request["method"] == "session/new" and "result" in message:
                        if not isinstance(message["result"], dict) or message["result"].get("sessionId") != session_id:
                            raise RPCError("session/new 的 result.sessionId 必须使用 $sessionId，以关联当前模拟会话")
                    if request["method"] == EXT + "session/history/load" and "result" in message:
                        result = message["result"]
                        if not isinstance(result, dict) or result.get("pageId") != variables["$pageId"]:
                            raise RPCError("历史响应的 pageId 必须使用 $pageId")
                        if type(result.get("updateCount")) is not int or result["updateCount"] != len(history_sequences):
                            raise RPCError("历史响应的 updateCount 必须等于本脚本已发送的 history_replay 通知数")
                        if history_sequences != list(range(len(history_sequences))):
                            raise RPCError("历史通知的 sequence 必须从 0 开始连续递增")
                    if request["method"] == EXT + "session/history/load" and "method" in message:
                        update = message.get("params", {}).get("update", {})
                        if update.get("sessionUpdate") == "history_replay":
                            if update.get("pageId") != variables["$pageId"]:
                                raise RPCError("历史通知必须使用本次 $pageId")
                            history_sequences.append(update.get("sequence"))
                    if turn and message.get("method"):
                        update = message.get("params", {}).get("update", {})
                        if update.get("sessionUpdate") == "prompt_queue_update":
                            continue
                        if update.get("sessionUpdate") == "user_message_chunk" and update.get("messageId") == variables["$messageId"]:
                            continue
                    emit(message)
            if turn and not replied:
                emit({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "cancelled"}})
        except Exception as error:
            self.last_error = str(error)
            if not replied:
                try:
                    emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": str(error)}})
                except Exception:
                    pass
        finally:
            with self.lock:
                if turn:
                    self.active.pop((turn["entry"]["sessionId"], turn["entry"]["requestId"]), None)
                self.agent.save()

    def configure(self, data):
        rule = scenario(data["scenario"])
        if rule.get("kind") == "notification":
            raise ValueError("独立通知请使用发送通知按钮，或将帧加入请求脚本")
        if data["speed"] not in (0, .5, 1, 2, 4):
            raise ValueError("发送速度无效")
        with self.lock:
            self.rules[rule["method"]] = rule
            self.speed = data["speed"]
            self.save_settings()

    def save_settings(self):
        path = self.data / "settings.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"rules": self.rules, "notifications": self.notification_rules,
                                         "speed": self.speed}, ensure_ascii=False))
        temporary.replace(path)

    def samples(self):
        with self.lock:
            samples = {rule["name"]: rule for rule in self.agent.samples}
            samples.update(self.notification_rules)
            return list(samples.values())

    def save_sample(self, data):
        rule = scenario(data["scenario"])
        if rule.get("kind") != "notification" or not isinstance(rule.get("name"), str) or not rule["name"].strip():
            raise ValueError("通知样例需要 kind: notification 和非空 name；同名保存覆盖，修改 name 可另存")
        with self.lock:
            self.notification_rules[rule["name"]] = rule
            self.save_settings()

    def notify(self, data):
        rule = scenario(data["scenario"])
        if rule.get("kind") != "notification" or data["connection"] not in self.peers:
            raise ValueError("请选择通知脚本和在线连接")
        session_id = data["sessionId"]
        with self.lock:
            self.agent.session(session_id)
            request = {"method": rule["method"], "params": {"sessionId": session_id}}
            variables = self.variables(request, {}, [])
            for frame in rule["frames"]:
                message = substitute(frame["message"], variables)
                if not isinstance(message, dict) or message.get("params", {}).get("update", {}).get("sessionUpdate") == "history_replay":
                    raise ValueError("history_replay 必须由 history/load 触发，以匹配 App 正在收集的 pageId")
            self.spawn(self.play, data["connection"], request, rule, variables, self.speed)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def response(self, value, status=200, content_type="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        app = self.server.studio
        url = urlsplit(self.path)
        path = url.path
        try:
            if self.headers.get("Host") not in (f"127.0.0.1:{app.ui_port}", f"localhost:{app.ui_port}"):
                self.response({"error": "本机控制台不接受此 Host"}, 403)
                return
            if path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Studio-Key", ""), app.token):
                self.response({"error": "无效的控制台密钥"}, 403)
                return
            data = None
            if self.command == "POST":
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError("请求最大 8 MB")
                data = json.loads(self.rfile.read(size))
            if self.command == "GET" and path in ("/", "/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[1:]
                content = (app.root / "web" / name).read_bytes().replace(b"__KEY__", app.token.encode())
                self.response(content, content_type={"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name])
            elif self.command == "GET" and path == "/api/state":
                self.response(app.snapshot())
            elif self.command == "GET" and path == "/api/agent-data":
                with app.lock:
                    self.response(app.agent.definition)
            elif self.command == "POST" and path == "/api/agent-data":
                with app.lock:
                    app.agent.configure(data)
                    app.agent.save()
                    self.response(app.agent.definition)
            elif self.command == "GET" and path == "/api/session-data":
                identity = parse_qs(url.query).get("sessionId", [None])[0]
                with app.lock:
                    self.response(app.agent.session_data(identity))
            elif self.command == "POST" and path == "/api/session-data":
                with app.lock:
                    result = app.agent.configure_session(data)
                    app.agent.save()
                    self.response(result)
            elif self.command == "GET" and path == "/api/rule":
                method = parse_qs(url.query)["method"][0]
                self.response(app.rules.get(method) or app.default_rule(method))
            elif self.command == "GET" and path == "/api/samples":
                self.response(app.samples())
            elif self.command == "POST" and path == "/api/samples":
                app.save_sample(data)
                self.response({"ok": True})
            elif self.command == "POST" and path == "/api/settings":
                app.configure(data)
                self.response({"ok": True})
            elif self.command == "POST" and path == "/api/reset":
                with app.lock:
                    app.rules.pop(data["method"], None)
                    app.save_settings()
                self.response(app.default_rule(data["method"]))
            elif self.command == "POST" and path == "/api/import":
                try:
                    value = json.loads(data["text"])
                except (ValueError, TypeError):
                    rule = document_scenario(extract_document(data["text"], data.get("name", "import")))
                else:
                    if isinstance(value, dict) and value.get("format") == "acp-studio/v1":
                        rule = scenario(value)
                    elif isinstance(value, dict) and value.get("jsonrpc") == "2.0":
                        rule = document_scenario(extract_document(data["text"], data.get("name", "import")))
                    else:
                        rule = scenario({"format": "acp-studio/v1", "method": data["method"], "frames": [
                            {"offset": 0, "message": {"jsonrpc": "2.0", "id": "$rpcId", "result": value}}]})
                self.response(rule)
            elif self.command == "POST" and path == "/api/notify":
                app.notify(data)
                self.response({"ok": True})
            else:
                self.response({"error": "未找到"}, 404)
        except Exception as error:
            self.response({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1:43187")
    parser.add_argument("--advertise-address")
    parser.add_argument("--ui-port", type=int, default=9091)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "Library/Application Support/ACPStudio")
    parser.add_argument("--transport", type=Path)
    parser.add_argument("--fixtures-dir", type=Path)
    parser.add_argument("--export-catalog", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    catalog_path = root / "examples/desktop-catalog.json"
    catalog = load_catalog(args.fixtures_dir) if args.fixtures_dir else json.loads(catalog_path.read_text()) if catalog_path.exists() else []
    if args.export_catalog:
        args.export_catalog.write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
        args.export_catalog.with_name("desktop-prompt.json").write_text(json.dumps(desktop_prompt(catalog), ensure_ascii=False, indent=2))
        return
    bundled = root / "bin/acp-studio-transport"
    binary = args.transport or (bundled if bundled.is_file() else root / "native/target/debug/acp-studio-transport")
    if not binary.is_file():
        parser.error("请构建原生传输或传入 --transport")
    data = args.data_dir.expanduser()
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                               env=os.environ | {"ACP_STUDIO_BIND": args.bind,
                               "ACP_STUDIO_ADDRESS": args.advertise_address or args.bind,
                               "ACP_STUDIO_TOKEN": secrets.token_urlsafe(32)})
    app = Studio(root, data, process, args.ui_port, catalog)
    server = ThreadingHTTPServer(("127.0.0.1", args.ui_port), Handler)
    server.studio = app
    threading.Thread(target=app.reader, daemon=True).start()
    print(f"ACP Studio: http://127.0.0.1:{args.ui_port} · {len(METHODS)} 个方法 · {len(catalog)} 份 Desktop 样例", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping.set()
        with app.lock:
            for turn in app.active.values():
                turn["cancel"].set()
            workers = list(app.workers)
        for worker in workers:
            worker.join(timeout=5)
        process.terminate()
        process.wait(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
