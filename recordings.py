import copy
import json
import math
from pathlib import Path


STATEFUL = {"initialize", "session/new", "session/list", "session/load", "session/set_config_option",
            "_agnes/unstable/session/info", "_agnes/unstable/session/history/load",
            "_agnes/unstable/session/read_state", "_agnes/unstable/session/mark_read",
            "_agnes/unstable/session/prompt/enqueue", "_agnes/unstable/session/prompt/list",
            "_agnes/unstable/session/prompt/cancel", "_agnes/unstable/remote/session/modes/get"}


def extract_document(text, name="import"):
    decoder = json.JSONDecoder()
    cursor, messages, fragments = 0, [], 0
    while (start := text.find("{", cursor)) >= 0:
        try:
            value, size = decoder.raw_decode(text[start:])
        except ValueError as error:
            raise ValueError(f"{name}: JSON 对象无效，位置 {start}: {error}") from error
        cursor = start + size
        if value.get("jsonrpc") == "2.0":
            messages.append(value)
        else:
            fragments += 1
    if not messages:
        raise ValueError(f"{name}: 未找到 JSON-RPC 消息")
    requests = [item for item in messages if "method" in item and "id" in item]
    responses = [item for item in messages if "method" not in item and "id" in item]
    notifications = [item for item in messages if "method" in item and "id" not in item]
    warnings = ["缺少逐帧采集时间；生成脚本采用人为间隔，不代表真实延迟。"]
    if fragments:
        warnings.append(f"保留说明来源；跳过 {fragments} 个非 JSON-RPC 元数据片段。")
    if {item['id'] for item in requests} != {item['id'] for item in responses}:
        warnings.append("请求与响应 ID 不完全配对；按方法制作样例，不当成原始完整事务。")
    session_ids = {item.get("params", {}).get("sessionId") for item in messages}
    session_ids.discard(None)
    if len(session_ids) > 1:
        warnings.append("混有多个会话；通知须按场景选择，不能推断为同一次调用。")
    return {"source": name, "requests": requests, "responses": responses,
            "notifications": notifications, "warnings": warnings}


def load_catalog(directory):
    return [extract_document(path.read_text(), path.name) for path in sorted(Path(directory).glob("*.json"))]


def scenario(value):
    value = json.loads(value) if isinstance(value, str) else copy.deepcopy(value)
    if not isinstance(value, dict) or value.get("format") != "acp-studio/v1":
        raise ValueError("需要 acp-studio/v1 格式")
    if not isinstance(value.get("method"), str) or not value["method"]:
        raise ValueError("需要 method")
    if value.get("kind", "request") not in ("request", "notification"):
        raise ValueError("kind 只能是 request 或 notification")
    frames = value.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= 10000:
        raise ValueError("frames 需要包含 1–10000 个事件")
    previous, replies = 0, 0
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("frame 必须是对象")
        offset = frame.get("offset")
        if type(offset) not in (int, float) or not math.isfinite(offset) or not previous <= offset <= 3600:
            raise ValueError("offset 必须非递减，且介于 0–3600 秒")
        previous = offset
        message = frame.get("message")
        if message == "$notifications":
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("每个 message 都需要 jsonrpc: 2.0")
        if "id" in message:
            if "method" in message or message["id"] != "$rpcId" or ("result" in message) == ("error" in message):
                raise ValueError("响应需要 id: $rpcId 和 result/error；暂不支持反向 request 模拟")
            replies += 1
        elif not isinstance(message.get("method"), str) or not isinstance(message.get("params"), dict):
            raise ValueError("通知需要 method 和对象 params")
    expected = 0 if value.get("kind") == "notification" else 1
    if replies != expected:
        raise ValueError(f"该脚本必须包含 {expected} 个 RPC 响应")
    if value["method"] == "session/prompt" and expected and "id" not in frames[-1]["message"]:
        raise ValueError("prompt 的最后一帧必须为响应")
    return value


def substitute(value, variables):
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    if value in variables:
        return copy.deepcopy(variables[value])
    if value.startswith(("$params.", "$config.")):
        variable, path = value.split(".", 1)
        result = variables.get(variable, {})
        for part in path.split("."):
            if not isinstance(result, dict) or part not in result:
                raise ValueError(f"变量缺少字段 {value}")
            result = result[part]
        return copy.deepcopy(result)
    return value


def normalize(value, session=True, identities=None):
    identities = {} if identities is None else identities
    if isinstance(value, list):
        return [normalize(item, session, identities) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "sessionId" and session:
            result[key] = "$sessionId"
        elif key in ("requestId", "runId", "activeRunId", "pageId", "loadId", "waitId") and item:
            result[key] = "$" + ("runId" if key == "activeRunId" else key)
        elif key in ("toolCallId", "messageId") and isinstance(item, str):
            token = identities.setdefault((key, item), f"$identity:{key}:{item}")
            result[key] = token
        elif key in ("workingDir", "cwd") and session:
            result[key] = "$cwd"
        else:
            result[key] = normalize(item, session, identities)
    if value.get("jsonrpc") == "2.0" and "id" in value:
        result["id"] = "$rpcId"
    return result


def document_scenario(document):
    requests = document["requests"]
    methods = {item["method"] for item in requests}
    if len(methods) != 1 or not document["responses"]:
        raise ValueError("请选择包含单一请求方法和响应的文档；独立通知请从通知样例中选择")
    method = next(iter(methods))
    identities = {}
    scoped = method not in ("initialize", "session/list") and "/remote/" not in method
    response = normalize(document["responses"][-1], scoped, identities)
    frames = [{"offset": 0, "message": response}]
    if method in STATEFUL:
        frames = [{"offset": 0, "message": "$notifications"},
                  {"offset": 0, "message": {"jsonrpc": "2.0", "id": "$rpcId", "result": "$result"}}]
    return scenario({"format": "acp-studio/v1", "method": method, "source": document["source"],
                     "capturedResponse": response,
                     "warnings": document["warnings"] + [
                         "有状态方法使用 $result/$notifications；capturedResponse 保留原响应供参考。"
                         if method in STATEFUL else "默认只导入响应；通知从样例中选择并合并到 frames。"],
                     "frames": frames})


def notification_scenarios(catalog):
    result = []
    for document in catalog:
        for index, message in enumerate(document["notifications"]):
            tag = message.get("params", {}).get("update", {}).get("sessionUpdate", "notification")
            result.append({"format": "acp-studio/v1", "kind": "notification", "method": message["method"],
                           "name": f"{document['source']} · {index + 1} · {tag}",
                           "frames": [{"offset": 0, "message": normalize(message)}]})
    return result


def desktop_prompt(catalog):
    document = next(item for item in catalog if item["source"] == "session:update.json")
    updates = document["notifications"]
    completed = {item["params"]["update"]["toolCallId"] for item in updates
                 if item["params"]["update"].get("sessionUpdate") == "tool_call_update"}
    chosen = []
    identities = {}
    for message in updates:
        update = message["params"]["update"]
        kind = update["sessionUpdate"]
        if kind in ("tool_call", "tool_call_update") and update.get("toolCallId") in completed:
            chosen.append(normalize(message, identities=identities))
        elif kind in ("agent_message_chunk", "message_usage", "message_model"):
            adjusted = normalize(message, identities=identities)
            if kind == "message_model":
                adjusted["params"]["update"]["modelId"] = "$config.model"
            def rewrite(value):
                if isinstance(value, dict):
                    return {key: "$agentMessageId" if key == "messageId" else rewrite(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [rewrite(item) for item in value]
                return value
            chosen.append(rewrite(adjusted))
    frames = [{"offset": round((index + 1) * .3, 2), "message": item} for index, item in enumerate(chosen)]
    response = next(item for item in catalog if item["source"] == "session:prompt.json")["responses"][0]
    response = copy.deepcopy(response)
    response["id"] = "$rpcId"
    frames.append({"offset": round((len(chosen) + 1) * .3, 2), "message": response})
    return scenario({"format": "acp-studio/v1", "method": "session/prompt",
                     "warnings": ["由真实通知片段组装的展示场景；人工 0.3 秒间隔，不是原始完整事务。",
                                  "仅选取存在结束事件的工具调用；模型/用量事件关联到本轮示例助手消息。"],
                     "frames": frames})
