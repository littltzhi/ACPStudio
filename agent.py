import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from recordings import notification_scenarios


EXT = "_agnes/unstable/"
REMOTE = EXT + "remote/"
CONTENT = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"}
METHODS = ["initialize", "session/new", "session/list", "session/load", "session/prompt", "session/set_config_option",
           EXT + "session/info", EXT + "session/history/load", EXT + "session/read_state", EXT + "session/mark_read",
           EXT + "session/prompt/enqueue", EXT + "session/prompt/list", EXT + "session/prompt/cancel",
           REMOTE + "projects/list", REMOTE + "models/list", REMOTE + "capabilities/list",
           REMOTE + "session/capabilities/list", REMOTE + "session/modes/get", REMOTE + "approvals/list",
           REMOTE + "artifacts/list", EXT + "session/message_feedback/list", EXT + "sidebar/pins/get"]


class RPCError(Exception):
    def __init__(self, message, code=-32602):
        super().__init__(message)
        self.code = code


def notification(session_id, update, method="session/update"):
    return {"jsonrpc": "2.0", "method": method, "params": {"sessionId": session_id, "update": update}}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def message_identity(update):
    content = update.get("content", {})
    return (update.get("messageId") or update.get("_meta", {}).get("agnes", {}).get("messageId")
            or (content.get("_meta", {}).get("agnes", {}).get("messageId") if isinstance(content, dict) else None))


class MockAgent:
    def __init__(self, data, catalog):
        self.path = data / "agent-state.json"
        self.catalog = catalog
        self.samples = notification_scenarios(catalog)
        self.templates = {}
        for document in catalog:
            if document["requests"] and document["responses"]:
                self.templates[document["requests"][0]["method"]] = document["responses"][-1].get("result", {})
        self.definition = self.initial_definition()
        self.sessions = {}
        self.prompts = {}
        if self.path.exists():
            state = json.loads(self.path.read_text())
            self.definition = state.get("definition", self.definition)
            self.sessions, self.prompts = state["sessions"], state["prompts"]
            for entries in self.prompts.values():
                for entry in entries.values():
                    if entry["status"] == "running":
                        entry.update(status="interrupted", error="Studio 已重启；不会自动重发")
        else:
            self.seed()
        self.configure(self.definition)
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"definition": self.definition, "sessions": self.sessions,
                                         "prompts": self.prompts}, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def template(self, method, fallback):
        return copy.deepcopy(self.templates.get(method, fallback))

    def default_configuration(self):
        return self.configuration(self.definition)

    def initial_definition(self):
        models = self.template(REMOTE + "models/list", {"models": [], "defaultModel": None})
        projects = self.template(REMOTE + "projects/list", {"projects": [], "temporaryWorkingDir": "/studio"})
        configuration = self.template("session/new", {"configOptions": [
            {"id": "mode", "name": "Mode", "type": "select", "currentValue": "default",
             "options": [{"value": "default", "name": "Studio"}]}]})
        options = []
        for option in configuration.get("configOptions", []):
            if option["id"] not in ("provider", "model"):
                option["defaultValue"] = option.pop("currentValue")
                options.append(option)
        return {"configOptions": options, "models": models["models"], "defaultModel": models.get("defaultModel"),
                "projects": projects["projects"], "temporaryWorkingDir": projects["temporaryWorkingDir"],
                "capabilities": self.template(REMOTE + "capabilities/list", {"plugins": [], "connectors": [], "skills": []})}

    @staticmethod
    def configuration(definition, values=None):
        values = values or {}
        options = []
        models = definition["models"]
        if models:
            default = definition["defaultModel"]
            providers = list(dict.fromkeys(model["providerId"] for model in models))
            provider = values.get("provider", default["providerId"])
            if provider not in providers:
                provider = default["providerId"]
            available = [model for model in models if model["providerId"] == provider]
            model_id = values.get("model", default["modelId"])
            if model_id not in [model["modelId"] for model in available]:
                model_id = next((model["modelId"] for model in available if model["modelId"] == default["modelId"]),
                                available[0]["modelId"])
            options = [
                {"id": "provider", "name": "Provider", "type": "select", "currentValue": provider,
                 "options": [{"value": provider_id, "name": provider_id} for provider_id in providers]},
                {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": model_id,
                 "options": [{"value": model["modelId"], "name": model["name"]} for model in available]}]
        for template in definition["configOptions"]:
            option = copy.deepcopy(template)
            default = option.pop("defaultValue")
            selected = values.get(option["id"], default)
            option["currentValue"] = selected if selected in [item["value"] for item in option["options"]] else default
            options.append(option)
        mode = next((option for option in options if option["id"] == "mode"), None)
        modes = {"currentModeId": mode["currentValue"] if mode else "default",
                 "availableModes": [{"id": item["value"], **{key: value for key, value in item.items() if key != "value"}}
                                    for item in mode["options"]] if mode else [{"id": "default", "name": "Studio"}]}
        return {"configOptions": options, "modes": modes}

    @staticmethod
    def validate_definition(definition):
        if not isinstance(definition, dict):
            raise RPCError("Agent 数据必须是对象")
        for key in ("models", "projects", "configOptions"):
            if not isinstance(definition.get(key), list):
                raise RPCError(f"{key} 必须是数组")
        if not isinstance(definition.get("temporaryWorkingDir"), str) or not definition["temporaryWorkingDir"]:
            raise RPCError("temporaryWorkingDir 不能为空")
        capabilities = definition.get("capabilities")
        if not isinstance(capabilities, dict) or any(not isinstance(capabilities.get(key), list)
                                                   for key in ("plugins", "connectors", "skills")):
            raise RPCError("capabilities 需要 plugins、connectors、skills 数组")
        identities = set()
        for model in definition["models"]:
            if not isinstance(model, dict) or any(not isinstance(model.get(key), str) or not model[key]
                                                 for key in ("providerId", "modelId", "name")):
                raise RPCError("模型需要非空 providerId、modelId 和 name")
            identity = (model["providerId"], model["modelId"])
            if identity in identities:
                raise RPCError("同一 providerId 下 modelId 不能重复")
            identities.add(identity)
        default = definition.get("defaultModel")
        if identities and (not isinstance(default, dict) or (default.get("providerId"), default.get("modelId")) not in identities):
            raise RPCError("defaultModel 必须引用 models 中的模型")
        if not identities and default is not None:
            raise RPCError("models 为空时 defaultModel 必须为 null")
        project_ids = set()
        for project in definition["projects"]:
            if not isinstance(project, dict) or any(not isinstance(project.get(key), str) or not project[key]
                                                   for key in ("id", "path", "name")):
                raise RPCError("项目需要非空 id、path 和 name")
            if project["id"] in project_ids:
                raise RPCError("项目 id 不能重复")
            project_ids.add(project["id"])
        option_ids = {"provider", "model"}
        for option in definition["configOptions"]:
            if not isinstance(option, dict) or any(not isinstance(option.get(key), str) or not option[key]
                                                  for key in ("id", "name")):
                raise RPCError("配置项需要非空 id 和 name")
            if option["id"] in option_ids:
                raise RPCError("配置 id 不能重复；provider/model 由模型列表自动生成")
            option_ids.add(option["id"])
            choices = option.get("options")
            if option.get("type") != "select" or not isinstance(choices, list) or not choices:
                raise RPCError("配置项目前支持 type: select，options 不能为空")
            if any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key]
                                                    for key in ("value", "name")) for item in choices):
                raise RPCError("每个配置选项需要非空 value 和 name")
            choice_values = [item["value"] for item in choices]
            if len(choice_values) != len(set(choice_values)) or option.get("defaultValue") not in choice_values:
                raise RPCError("配置选项 value 不能重复，defaultValue 必须是其中一个值")

    def configure(self, definition):
        self.validate_definition(definition)
        configurations = {identity: self.configuration(definition, {
            option["id"]: option["currentValue"] for option in session["configOptions"]})
            for identity, session in self.sessions.items()}
        self.definition = copy.deepcopy(definition)
        for identity, configuration in configurations.items():
            self.sessions[identity].update(configuration)

    def session_data(self, session_id=None):
        if not session_id:
            return {"sessionId": "", "title": "Studio 本地会话", "cwd": self.definition["temporaryWorkingDir"],
                    "values": {option["id"]: option["currentValue"] for option in self.default_configuration()["configOptions"]}}
        session = self.session(session_id)
        return {"sessionId": session_id, "title": session["record"].get("title", ""),
                "cwd": session["record"]["cwd"],
                "values": {option["id"]: option["currentValue"] for option in session["configOptions"]}}

    def configure_session(self, data):
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ("sessionId", "title", "cwd")):
            raise RPCError("会话数据需要 sessionId、title、cwd 字符串")
        if not data["cwd"] or not isinstance(data.get("values"), dict):
            raise RPCError("cwd 不能为空，values 必须是配置值对象")
        identity = data["sessionId"] or "studio-" + str(uuid.uuid4())
        values = self.session_data(identity)["values"] if identity in self.sessions else {}
        values.update(data["values"])
        configuration = self.configuration(self.definition, values)
        actual = {option["id"]: option["currentValue"] for option in configuration["configOptions"]}
        if any(key not in actual or actual[key] != value for key, value in data["values"].items()):
            raise RPCError("values 包含未知配置或不支持的值；model 必须属于所选 provider")
        if identity not in self.sessions:
            self.add_session({"sessionId": identity, "cwd": data["cwd"], "title": data["title"]})
        self.sessions[identity]["record"].update(cwd=data["cwd"], title=data["title"], updatedAt=utc_now())
        self.sessions[identity].update(configuration)
        return self.session_data(identity)

    def add_session(self, record):
        session_id = record["sessionId"]
        configuration = self.default_configuration()
        self.sessions[session_id] = {"record": copy.deepcopy(record), "history": [],
                                     "configOptions": configuration.get("configOptions", []),
                                     "modes": configuration.get("modes", {}), "activityVersion": 0, "readVersion": 0}
        self.prompts[session_id] = {}
        return self.sessions[session_id]

    def seed(self):
        for record in self.templates.get("session/list", {}).get("sessions", []):
            self.add_session(record)
        info = self.templates.get(EXT + "session/info", {}).get("session")
        if info and info["sessionId"] not in self.sessions:
            self.add_session(info)
        for document in self.catalog:
            if not any(item["method"] == EXT + "session/history/load" for item in document["requests"]):
                continue
            notifications = sorted(document["notifications"], key=lambda item: item["params"]["update"].get("sequence", 0))
            for message in notifications:
                params = message["params"]
                update = params["update"]
                if update.get("sessionUpdate") != "history_replay":
                    continue
                session_id = params["sessionId"]
                if session_id not in self.sessions:
                    self.add_session({"sessionId": session_id, "cwd": "/studio/recorded", "title": "Desktop 历史片段样例",
                                      "_meta": {"isTemporary": True}})
                self.sessions[session_id]["history"].append(notification(session_id, update["update"]))

    def session(self, session_id):
        if not isinstance(session_id, str) or session_id not in self.sessions:
            raise RPCError("会话不存在，请先 session/new 或 session/list")
        return self.sessions[session_id]

    def read_state(self, session_id):
        session = self.session(session_id)
        content = [item["params"]["update"] for item in session["history"]
                   if item["params"]["update"].get("sessionUpdate") in CONTENT]
        last = content[-1] if content else None
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if last else None
        return {"activityVersion": session["activityVersion"], "readVersion": session["readVersion"],
                "hasUnreadActivity": session["activityVersion"] > session["readVersion"],
                "messageId": message_identity(last) if last else None, "contentFingerprint": fingerprint}

    def record(self, session_id):
        session = self.session(session_id)
        record = copy.deepcopy(session["record"])
        record.setdefault("_meta", {})["messageCount"] = len(self.history_groups(session_id))
        record["_meta"].setdefault("agnes", {})["readState"] = self.read_state(session_id)
        active = next((entry["runId"] for entry in self.prompts[session_id].values() if entry["status"] == "running"), None)
        record["_meta"]["agnes"]["activeRunId"] = active
        return record

    def ingest(self, message, expected_session):
        params = message.get("params", {})
        if params.get("sessionId") != expected_session:
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        session = self.session(expected_session)
        if kind in CONTENT or kind in ("message_usage", "message_model"):
            session["history"].append(copy.deepcopy(message))
            if kind in CONTENT:
                session["activityVersion"] += 1
                session["record"]["updatedAt"] = utc_now()
        elif kind == "config_option_update":
            session["configOptions"] = copy.deepcopy(update.get("configOptions", []))
        elif kind == "session_info_update":
            for key in ("title", "updatedAt"):
                if key in update:
                    session["record"][key] = update[key]

    def history_groups(self, session_id):
        groups, indices = [], {}
        for message in self.session(session_id)["history"]:
            update = message["params"]["update"]
            identity = message_identity(update) or update.get("toolCallId") or f"event-{len(groups)}"
            if identity not in indices:
                indices[identity] = len(groups)
                groups.append([])
            groups[indices[identity]].append(message)
        return groups

    def history_page(self, session_id, limit, cursor=None, page_id=None):
        groups = self.history_groups(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RPCError("limit 必须介于 1–1000")
        end = len(groups)
        if cursor is not None:
            parts = cursor.rsplit(":", 1) if isinstance(cursor, str) else []
            if len(parts) != 2 or parts[0] != f"studio:{session_id}" or not parts[1].isdigit():
                raise RPCError("无效 Studio 历史游标")
            end = int(parts[1])
            if end > len(groups):
                raise RPCError("历史游标已失效")
        start = max(0, end - limit)
        selected = [item for group in groups[start:end] for item in group]
        updates, usages, models = [], [], []
        for item in selected:
            update = item["params"]["update"]
            kind = update.get("sessionUpdate")
            if kind == "message_usage":
                usages.append({key: value for key, value in update.items() if key != "sessionUpdate"})
            elif kind == "message_model":
                models.append({key: value for key, value in update.items() if key != "sessionUpdate"})
            elif page_id:
                updates.append(notification(session_id, {"sessionUpdate": "history_replay", "pageId": page_id,
                                                          "sequence": len(updates), "update": update}, EXT + "session/update"))
            else:
                updates.append(item)
        result = {"nextCursor": f"studio:{session_id}:{start}" if start else None, "hasMore": start > 0,
                  "returnedMessages": end - start, "totalVisibleMessages": len(groups)}
        if page_id:
            result.update(pageId=page_id, updateCount=len(updates), messageUsages=usages, messageModels=models,
                          promptRequests=[self.public_entry(entry) for entry in self.prompts[session_id].values()])
        return result, copy.deepcopy(updates)

    def activation(self, session_id, limit=None):
        session = self.session(session_id)
        result = self.template("session/load", {})
        result.update(configOptions=copy.deepcopy(session["configOptions"]), modes=copy.deepcopy(session["modes"]))
        metadata = result.setdefault("_meta", {})
        metadata.update(workingDir=session["record"]["cwd"], backgroundExtensionRetryPending=False,
                        readOnly=False, occupied=False, extensionResults=[])
        metadata.pop("remoteSessionView", None)
        metadata.setdefault("agnes", {})["readState"] = self.read_state(session_id)
        page, updates = self.history_page(session_id, 10 if limit is None else limit)
        metadata["historyPage"] = page
        return result, updates

    def handle(self, method, params):
        session_id = params.get("sessionId")
        if method == "initialize":
            if params.get("protocolVersion", 1) != 1:
                raise RPCError("仅支持 ACP protocolVersion 1")
            return {"protocolVersion": 1, "agentInfo": {"name": "acp-studio", "version": "0.4.0"}, "authMethods": [],
                    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}},
                                          "_meta": {"agnes": {"remotePromptQueue": True}}}}, []
        if method == "session/new":
            session_id = "studio-" + str(uuid.uuid4())
            metadata = params.get("_meta", {})
            session = self.add_session({"sessionId": session_id, "cwd": params.get("cwd") or self.definition["temporaryWorkingDir"],
                                        "title": "Studio 本地会话", "updatedAt": utc_now(), "_meta": metadata})
            mode = metadata.get("permissionMode")
            if mode:
                self.set_config(session_id, "mode", mode)
            result, updates = self.activation(session_id)
            result["sessionId"] = session_id
            updates.extend([notification(session_id, {"sessionUpdate": "config_option_update", "configOptions": session["configOptions"]}),
                            notification(session_id, {"sessionUpdate": "usage_update", "used": 0, "size": 512000})])
            return result, updates
        if method == "session/list":
            metadata = params.get("_meta", {})
            query = metadata.get("query", "").casefold()
            records = [self.record(identity) for identity in self.sessions]
            records = [record for record in records if (not params.get("cwd") or record["cwd"] == params["cwd"])
                       and query in record.get("title", "").casefold()]
            records.sort(key=lambda record: record.get("updatedAt", ""), reverse=True)
            page = metadata.get("agnes", {})
            limit = page.get("pageSize", 20)
            cursor = params.get("cursor")
            if type(limit) is not int or not 1 <= limit <= 1000 or (cursor and not str(cursor).isdigit()):
                raise RPCError("无效列表分页参数")
            start = int(cursor or 0)
            return {"sessions": records[start:start + limit], "nextCursor": str(start + limit) if start + limit < len(records) else None,
                    "_meta": {"agnes": {"pinRevision": 0, "pinnedWorkspacePaths": []}}}, []
        if method == "session/load":
            return self.activation(session_id, params.get("_meta", {}).get("messageLimit"))
        if method == EXT + "session/info":
            return {"session": self.record(session_id)}, []
        if method == EXT + "session/history/load":
            if not isinstance(params.get("pageId"), str) or not params["pageId"]:
                raise RPCError("缺少 pageId")
            return self.history_page(session_id, params.get("limit", 10), params.get("cursor"), params["pageId"])
        if method == EXT + "session/read_state":
            return self.read_state(session_id), []
        if method == EXT + "session/mark_read":
            state = self.read_state(session_id)
            if (params.get("messageId"), params.get("contentFingerprint"), params.get("throughVersion")) == (
                    state["messageId"], state["contentFingerprint"], state["activityVersion"]):
                self.session(session_id)["readVersion"] = state["activityVersion"]
            return self.read_state(session_id), []
        if method == "session/set_config_option":
            self.set_config(session_id, params.get("configId"), params.get("value"))
            options = copy.deepcopy(self.session(session_id)["configOptions"])
            return {"configOptions": options}, [notification(session_id, {"sessionUpdate": "config_option_update", "configOptions": options})]
        if method == EXT + "session/prompt/enqueue":
            return self.enqueue(params)
        if method == EXT + "session/prompt/list":
            self.session(session_id)
            entries = self.prompts[session_id].values()
            return {"entries": [self.public_entry(entry) for entry in entries
                                if not params.get("requestIds") or entry["requestId"] in params["requestIds"]]}, []
        if method == REMOTE + "session/modes/get":
            return {"modes": copy.deepcopy(self.session(session_id)["modes"] if session_id else self.default_configuration()["modes"])}, []
        if method == REMOTE + "session/capabilities/list":
            self.session(session_id)
            return {"extensions": [], "skills": copy.deepcopy(self.definition["capabilities"]["skills"])}, []
        if method == REMOTE + "projects/list":
            return copy.deepcopy({key: self.definition[key] for key in ("projects", "temporaryWorkingDir")}), []
        if method == REMOTE + "models/list":
            return {**copy.deepcopy({key: self.definition[key] for key in ("models", "defaultModel")}),
                    "builtinError": None, "customError": None}, []
        if method == REMOTE + "capabilities/list":
            return copy.deepcopy(self.definition["capabilities"]), []
        fallbacks = {REMOTE + "approvals/list": {"approvals": []}, REMOTE + "artifacts/list": {"files": []},
                     EXT + "session/message_feedback/list": {"sessionId": session_id, "feedback": []},
                     EXT + "sidebar/pins/get": {"revision": 0, "legacyImported": False, "pinnedSessionIds": [], "pinnedWorkspacePaths": []}}
        if method in fallbacks:
            return self.template(method, fallbacks[method]), []
        raise RPCError(f"Studio 暂不支持 {method}", -32601)

    def set_config(self, session_id, config_id, value):
        session = self.session(session_id)
        option = next((item for item in session["configOptions"] if item["id"] == config_id), None)
        if option is None or value not in [item.get("value") for item in option.get("options", [])]:
            raise RPCError("未知配置或不支持的选项")
        values = {item["id"]: item["currentValue"] for item in session["configOptions"]}
        values[config_id] = value
        session.update(self.configuration(self.definition, values))

    @staticmethod
    def public_entry(entry):
        return {key: copy.deepcopy(value) for key, value in entry.items() if key not in ("prompt", "retry")}

    def queue_notification(self, entry):
        return notification(entry["sessionId"], {"sessionUpdate": "prompt_queue_update", "entry": self.public_entry(entry)}, EXT + "session/update")

    def enqueue(self, params):
        session_id = params.get("sessionId")
        self.session(session_id)
        for key in ("requestId", "messageId"):
            if not isinstance(params.get(key), str) or not params[key]:
                raise RPCError(f"缺少 {key}")
        if not isinstance(params.get("prompt"), list) or not params["prompt"] or type(params.get("createdAt")) is not int:
            raise RPCError("需要 prompt 和整数 createdAt")
        request_id = params["requestId"]
        existing = self.prompts[session_id].get(request_id)
        if existing:
            if existing["messageId"] != params["messageId"] or existing["prompt"] != params["prompt"]:
                raise RPCError("requestId 已绑定不同内容")
            if params.get("retry") and existing["status"] in ("failed", "cancelled", "interrupted"):
                existing.update(status="queued", updatedAt=int(time.time()))
                for key in ("runId", "stopReason", "error", "startedAtMs", "finishedAtMs", "elapsedMs"):
                    existing.pop(key, None)
            return {"entry": self.public_entry(existing)}, []
        entry = {key: copy.deepcopy(params[key]) for key in ("sessionId", "requestId", "messageId", "createdAt", "prompt")}
        entry.update(status="queued", updatedAt=int(time.time()), submittedAtMs=int(time.time() * 1000))
        self.prompts[session_id][request_id] = entry
        return {"entry": self.public_entry(entry)}, [self.queue_notification(entry)]

    def start_prompt(self, params):
        session_id = params.get("sessionId")
        self.session(session_id)
        content = params.get("prompt")
        entries = self.prompts[session_id]
        request_ids = {item.get("_meta", {}).get("agnes", {}).get("requestId") for item in content or [] if isinstance(item, dict)} - {None}
        entry = next((item for item in entries.values() if item["requestId"] in request_ids), None)
        if entry is None:
            entry = next((item for item in entries.values() if item["status"] == "queued" and item["prompt"] == content), None)
        if not entry or entry["status"] != "queued" or entry["prompt"] != content:
            raise RPCError("需要先登记相同 prompt；已完成请求不会重复执行")
        if any(item["status"] == "running" for item in entries.values()):
            raise RPCError("该会话已有运行中的 prompt")
        entry.update(status="running", runId="run_" + str(uuid.uuid4()), updatedAt=int(time.time()), startedAtMs=int(time.time() * 1000))
        user = notification(session_id, {"sessionUpdate": "user_message_chunk", "messageId": entry["messageId"],
                                       "content": {"type": "text", "text": "".join(item.get("text", "") for item in content)},
                                       "_meta": {"agnes": {"requestId": entry["requestId"], "created": entry["createdAt"], "messageId": entry["messageId"]}}})
        if not any(message_identity(item["params"]["update"]) == entry["messageId"] for item in self.session(session_id)["history"]):
            self.ingest(user, session_id)
        return entry, [self.queue_notification(entry), user]

    def finish_prompt(self, entry, response, cancelled=False):
        entry.update(status="cancelled" if cancelled else "failed" if "error" in response else "completed",
                     updatedAt=int(time.time()), finishedAtMs=int(time.time() * 1000))
        entry["elapsedMs"] = entry["finishedAtMs"] - entry.get("startedAtMs", entry["finishedAtMs"])
        if "error" in response:
            entry["error"] = str(response["error"].get("message", "Replay error"))
        else:
            entry["stopReason"] = response.get("result", {}).get("stopReason", "end_turn")
        return self.queue_notification(entry)
