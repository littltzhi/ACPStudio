import argparse
import copy
import json
import math
import os
import secrets
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def substitute(value, variables):
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    return variables.get(value, value) if isinstance(value, str) else value


def scenario(text):
    value = json.loads(text) if isinstance(text, str) else text
    if not isinstance(value, dict) or value.get("format") != "acp-studio/v1" or value.get("method") != "session/prompt":
        raise ValueError("需要 acp-studio/v1 格式的 session/prompt 回放文件。")
    previous = 0
    frames = value.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= 10000:
        raise ValueError("frames 需要包含 1–10000 个事件。")
    replies = 0
    for frame in frames:
        offset = frame.get("offset")
        if type(offset) not in (int, float) or not math.isfinite(offset) or not previous <= offset <= 3600:
            raise ValueError("offset 必须递增，且介于 0–3600 秒。")
        previous = offset
        message = frame.get("message", {})
        if message.get("jsonrpc") != "2.0":
            raise ValueError("每个 message 都需要 jsonrpc: 2.0。")
        if "id" in message:
            if message["id"] != "$rpcId" or ("result" in message) == ("error" in message):
                raise ValueError("响应需要 id: $rpcId，并包含 result 或 error。")
            replies += 1
        elif not isinstance(message.get("method"), str) or "params" not in message:
            raise ValueError("通知需要 method 和 params。")
    if replies != 1 or frames[-1]["message"].get("id") != "$rpcId":
        raise ValueError("最后一帧必须是唯一的 $rpcId 响应。")
    return value


class Studio:
    def __init__(self, root, data, process, ui_port):
        self.root, self.data, self.process, self.ui_port = root, data, process, ui_port
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.pairing = None
        self.peers = set()
        self.events = deque(maxlen=250)
        self.mode, self.speed = "mock", 1
        self.sessions, self.entries, self.active = {}, {}, {}
        self.rule = scenario((root / "examples/reply.json").read_text())
        self.journal = (data / f"traffic-{time.time_ns()}.jsonl").open("a", encoding="utf-8")
        self.last_error = ""
        self.stopping = threading.Event()

    def record(self, connection, direction, message):
        event = {"time": time.time(), "connection": connection, "direction": direction, "message": message}
        with self.lock:
            self.events.append(event)
            self.journal.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.journal.flush()

    def send(self, connection, message):
        with self.write_lock:
            if self.process.poll() is not None:
                raise RuntimeError("iroh 进程已退出。")
            self.process.stdin.write(json.dumps({"connection": connection, "message": message}) + "\n")
            self.process.stdin.flush()
        self.record(connection, "out", message)

    def reply(self, connection, request, result=None, error=None):
        if "id" in request:
            self.send(connection, {"jsonrpc": "2.0", "id": request["id"],
                                   **({"error": error} if error else {"result": result})})

    def snapshot(self):
        with self.lock:
            records = sorted(self.data.glob("record-*.json"), reverse=True)[:100]
            return {
                "pairing": self.pairing, "peers": len(self.peers), "mode": self.mode, "speed": self.speed,
                "events": list(self.events), "error": self.last_error,
                "records": [{"id": path.stem, "name": path.stem} for path in records],
            }

    def reader(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
                connection = event.get("connection")
                if event["kind"] == "ready":
                    self.pairing = event["pairing"]
                    path = self.data / "pairing.json"
                    path.write_text(json.dumps(self.pairing, indent=2))
                    path.chmod(0o600)
                elif event["kind"] == "connected":
                    self.peers.add(connection)
                elif event["kind"] == "closed":
                    self.peers.discard(connection)
                    for (peer, _), turn in list(self.active.items()):
                        if peer == connection:
                            turn["cancel"].set()
                elif event["kind"] == "message":
                    self.record(connection, "in", event["message"])
                    self.handle(connection, event["message"])
            except Exception as error:
                self.last_error = str(error)
        self.last_error = "iroh 进程已停止。"

    def handle(self, connection, request):
        method, params = request.get("method"), request.get("params") or {}
        session_id = params.get("sessionId")
        now = int(time.time())
        if method == "initialize":
            result = {"protocolVersion": 1, "agentInfo": {"name": "acp-studio", "version": "0.1.0"},
                      "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}},
                                            "_meta": {"agnes": {"remotePromptQueue": True}}}}
        elif method == "session/new":
            session_id = str(uuid.uuid4())
            self.sessions[session_id] = {"sessionId": session_id, "cwd": params.get("cwd") or "/studio",
                                         "title": "Studio 本地会话", "_meta": {"isTemporary": True}, "history": []}
            result = {"sessionId": session_id, "configOptions": []}
        elif method == "session/list":
            result = {"sessions": [{key: value for key, value in item.items() if key != "history"}
                                    for item in self.sessions.values()]}
        elif method == "session/load" and session_id in self.sessions:
            for notification in self.sessions[session_id]["history"]:
                self.send(connection, notification)
            result = {"configOptions": [], "_meta": {"workingDir": self.sessions[session_id]["cwd"]}}
        elif method == "_agnes/unstable/session/info" and session_id in self.sessions:
            result = {"session": {key: value for key, value in self.sessions[session_id].items() if key != "history"}}
        elif method == "_agnes/unstable/session/prompt/enqueue" and session_id in self.sessions:
            if (connection, session_id) in self.active:
                self.reply(connection, request, error={"code": -32602, "message": "Studio 会话正在运行"})
                return
            entry = {key: params[key] for key in ("sessionId", "requestId", "messageId", "createdAt")}
            entry.update(status="queued", updatedAt=now)
            self.entries[(connection, session_id)] = entry
            result = {"entry": entry}
        elif method == "session/prompt" and session_id in self.sessions:
            key = (connection, session_id)
            if key in self.active or key not in self.entries:
                self.reply(connection, request, error={"code": -32602, "message": "需要先登记 prompt"})
                return
            turn = {"cancel": threading.Event()}
            self.active[key] = turn
            threading.Thread(target=self.prompt, args=(connection, request, turn), daemon=True).start()
            return
        elif method == "_agnes/unstable/session/prompt/list":
            result = {"entries": [entry for (peer, session), entry in self.entries.items()
                                   if peer == connection and session == session_id
                                   and (not params.get("requestIds") or entry["requestId"] in params["requestIds"])]}
        elif method == "_agnes/unstable/session/prompt/cancel":
            key = (connection, session_id)
            entry = self.entries.get(key)
            if not entry or entry["requestId"] != params.get("requestId"):
                self.reply(connection, request, error={"code": -32602, "message": "找不到该请求"})
                return
            if key in self.active:
                self.active[key]["cancel"].set()
            entry.update(status="cancelled", stopReason="cancelled", updatedAt=now)
            result = {"entry": entry}
        elif method == "_agnes/unstable/remote/projects/list":
            result = {"projects": [], "temporaryWorkingDir": "/studio"}
        elif method in ("_agnes/unstable/session/capabilities/list", "_agnes/unstable/remote/capabilities/list"):
            result = {"plugins": [], "connectors": [], "skills": []}
        elif method == "_agnes/unstable/remote/models/list":
            result = {"models": []}
        elif method == "_agnes/unstable/remote/session/modes/get":
            result = {"modes": {"currentModeId": "default",
                               "availableModes": [{"id": "default", "name": "Studio"}]}}
        elif method == "_agnes/unstable/remote/approvals/list":
            result = {"approvals": []}
        elif method == "_agnes/unstable/remote/artifacts/list":
            result = {"files": []}
        elif method == "_agnes/unstable/session/message_feedback/list":
            result = {"sessionId": session_id, "feedback": []}
        elif method in ("_agnes/unstable/session/read_state", "_agnes/unstable/session/mark_read"):
            result = {"sessionId": session_id, "readVersion": 0, "activityVersion": 0}
        elif method == "_agnes/unstable/sidebar/pins/get":
            result = {"revision": 0, "pinnedSessionIds": [], "pinnedWorkspacePaths": []}
        else:
            self.reply(connection, request, error={"code": -32601, "message": f"Studio 暂不支持 {method}"})
            return
        self.reply(connection, request, result)

    def prompt(self, connection, request, turn):
        params = request["params"]
        session_id = params["sessionId"]
        entry = self.entries[(connection, session_id)]
        variables = {"$rpcId": request["id"], "$sessionId": session_id, "$requestId": entry["requestId"],
                     "$messageId": entry["messageId"], "$agentMessageId": str(uuid.uuid4())}
        entry.update(status="running", runId=entry["requestId"], updatedAt=int(time.time()))
        user = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {"sessionUpdate": "user_message_chunk",
            "messageId": entry["messageId"], "content": {"type": "text", "text": "".join(
                part.get("text", "") for part in params.get("prompt", []) if part.get("type") == "text")}}}}
        self.sessions[session_id]["history"].append(user)
        with self.lock:
            rule, speed = copy.deepcopy(self.rule), self.speed
            mode = self.mode
        if mode == "mock":
            rule = scenario((self.root / "examples/reply.json").read_text())
        started = time.monotonic()
        recorded = []
        replied = False
        failed = False
        try:
            for frame in rule["frames"]:
                wait = max(0, frame["offset"] / speed - (time.monotonic() - started)) if speed else 0
                if turn["cancel"].wait(wait) or self.stopping.is_set():
                    break
                message = substitute(frame["message"], variables)
                self.send(connection, message)
                recorded.append({"offset": time.monotonic() - started, "message": frame["message"]})
                replied = message.get("id") == request["id"] if "id" in message else replied
                failed = "error" in message if "id" in message else failed
                if message.get("method") == "session/update":
                    self.sessions[session_id]["history"].append(message)
            if turn["cancel"].is_set() and not replied:
                reply = {"jsonrpc": "2.0", "id": "$rpcId", "result": {"stopReason": "cancelled"}}
                self.send(connection, substitute(reply, variables))
                recorded.append({"offset": time.monotonic() - started, "message": reply})
            entry.update(status="cancelled" if turn["cancel"].is_set() else "failed" if failed else "completed",
                         stopReason="cancelled" if turn["cancel"].is_set() else "end_turn", updatedAt=int(time.time()))
            self.send(connection, {"jsonrpc": "2.0", "method": "_agnes/unstable/session/update", "params": {
                "sessionId": session_id, "update": {"sessionUpdate": "prompt_queue_update", "entry": entry}}})
        except Exception as error:
            self.last_error = str(error)
        finally:
            document = {"format": "acp-studio/v1", "method": "session/prompt", "frames": recorded}
            if recorded:
                (self.data / f"record-{time.time_ns()}.json").write_text(
                    json.dumps(document, ensure_ascii=False, indent=2))
            self.active.pop((connection, session_id), None)


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
        path = urlsplit(self.path).path
        try:
            if self.headers.get("Host") not in (f"127.0.0.1:{app.ui_port}", f"localhost:{app.ui_port}"):
                self.response({"error": "本机控制台不接受此 Host"}, 403)
                return
            if path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Studio-Key", ""), app.token):
                self.response({"error": "无效的控制台密钥"}, 403)
                return
            if self.command == "GET" and path in ("/", "/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[1:]
                data = (app.root / "web" / name).read_bytes().replace(b"__KEY__", app.token.encode())
                self.response(data, content_type={"index.html": "text/html", "app.js": "text/javascript",
                                                   "style.css": "text/css"}[name])
            elif self.command == "GET" and path == "/api/state":
                self.response(app.snapshot())
            elif self.command == "GET" and path.startswith("/api/records/"):
                name = path.split("/")[-1]
                if not name.startswith("record-") or not name[7:].isdigit():
                    raise ValueError("录制 ID 无效")
                self.response(json.loads((app.data / f"{name}.json").read_text()))
            elif self.command == "POST" and path == "/api/settings":
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError("JSON 最大 8 MB")
                data = json.loads(self.rfile.read(size))
                rule = scenario(data["scenario"])
                if data["mode"] not in ("mock", "replay") or data["speed"] not in (0, .5, 1, 2, 4):
                    raise ValueError("模式或速度无效")
                with app.lock:
                    app.rule, app.mode, app.speed = rule, data["mode"], data["speed"]
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
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    bundled = root / "bin/acp-studio-transport"
    binary = args.transport or (bundled if bundled.is_file() else root / "native/target/debug/acp-studio-transport")
    if not binary.is_file():
        parser.error("请先运行 cargo build --locked --manifest-path native/Cargo.toml，或传入 --transport")
    data = args.data_dir.expanduser()
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                               env=os.environ | {"ACP_STUDIO_BIND": args.bind,
                               "ACP_STUDIO_ADDRESS": args.advertise_address or args.bind,
                               "ACP_STUDIO_TOKEN": secrets.token_urlsafe(32)})
    app = Studio(root, data, process, args.ui_port)
    server = ThreadingHTTPServer(("127.0.0.1", args.ui_port), Handler)
    server.studio = app
    threading.Thread(target=app.reader, daemon=True).start()
    print(f"ACP Studio: http://127.0.0.1:{args.ui_port}  · iroh {args.bind}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping.set()
        for turn in list(app.active.values()):
            turn["cancel"].set()
        process.terminate()
        process.wait(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
