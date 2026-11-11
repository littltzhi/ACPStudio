const $ = selector => document.querySelector(selector);
const key = $('meta[name="studio-key"]').content;
let pairing = null;
let eventsSignature = "";
let initialized = false;
let backendReady = false;
let samples = [];
let editorMode = "script";
let editorRule;
const template = {
  format: "acp-studio/v1", method: "session/prompt",
  frames: [
    {offset: .3, message: {jsonrpc: "2.0", method: "session/update", params: {
      sessionId: "$sessionId", update: {sessionUpdate: "agent_message_chunk", messageId: "$agentMessageId",
        content: {type: "text", text: "## 本地模拟响应\n\n"}}}}},
    {offset: 1.2, message: {jsonrpc: "2.0", method: "session/update", params: {
      sessionId: "$sessionId", update: {sessionUpdate: "agent_message_chunk", messageId: "$agentMessageId",
        content: {type: "text", text: "通过 iroh / QUIC 发送，无需 BFF。"}}}}},
    {offset: 1.5, message: {jsonrpc: "2.0", id: "$rpcId", result: {stopReason: "end_turn"}}}
  ]
};
loadRule(template);
async function api(path, body) {
  if (body && !backendReady) throw Error("Studio 后端尚未就绪或版本过旧，请重启 Studio 并刷新页面");
  const response = await fetch(path, {method: body ? "POST" : "GET",
    headers: {"X-Studio-Key": key, "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) throw Error(data.error);
  return data;
}
function note(text) { $("#notice").textContent = text; }
function isSimple(rule) {
  const frame = rule.frames?.[0];
  return rule.kind !== "notification" && rule.frames?.length === 1 && frame.offset === 0 &&
    frame.message?.id === "$rpcId" && "result" in frame.message && !("error" in frame.message);
}
function readRule() {
  const value = JSON.parse($("#editor").value);
  if (editorMode === "script") return value;
  const rule = structuredClone(editorRule);
  rule.frames[0].message.result = value;
  return rule;
}
function loadRule(rule, mode = isSimple(rule) ? "result" : "script") {
  if (mode === "result" && !isSimple(rule)) throw Error("含通知、延迟或错误的配置请使用完整脚本");
  editorRule = rule; editorMode = mode;
  $("#editor-format").value = mode;
  $("#editor").value = JSON.stringify(mode === "result" ? rule.frames[0].message.result : rule, null, 2);
  $("#script-method").textContent = rule.method;
  if (rule.kind !== "notification") {
    if (![...$("#method").options].some(item => item.value === rule.method)) {
      $("#method").add(new Option(rule.method, rule.method));
    }
    $("#method").value = rule.method;
  }
}
function options(selector, items) {
  const select = $(selector), current = select.value;
  if (JSON.stringify([...select.options].map(item => [item.value, item.text])) === JSON.stringify(items)) return;
  select.replaceChildren(...items.map(([value, text]) => new Option(text, value)));
  if (items.some(([value]) => value === current)) select.value = current;
}
async function act(work) { try { await work(); } catch (error) { note(error.message); } }
async function editData(work) {
  try { await work(); } catch (error) { $("#data-notice").textContent = error.message; }
}
async function loadSessionData() {
  const value = await api(`/api/session-data?sessionId=${encodeURIComponent($("#data-session").value)}`);
  $("#session-data").value = JSON.stringify(value, null, 2);
}
$("#reload-agent-data").onclick = () => editData(async () => {
  $("#agent-data").value = JSON.stringify(await api("/api/agent-data"), null, 2);
  $("#data-notice").textContent = "已读取 Agent 当前数据";
});
$("#save-agent-data").onclick = () => editData(async () => {
  const value = await api("/api/agent-data", JSON.parse($("#agent-data").value));
  $("#agent-data").value = JSON.stringify(value, null, 2);
  $("#data-notice").textContent = "Agent 数据已保存并同步到会话；可读取会话当前值，在 App 重新查询验证";
});
$("#data-session").onchange = () => editData(loadSessionData);
$("#reload-session-data").onclick = () => editData(async () => {
  await loadSessionData();
  $("#data-notice").textContent = "已读取会话当前值，包括 App 最近设置的值";
});
$("#save-session-data").onclick = () => editData(async () => {
  const value = await api("/api/session-data", JSON.parse($("#session-data").value));
  $("#session-data").value = JSON.stringify(value, null, 2);
  if (![...$("#data-session").options].some(item => item.value === value.sessionId)) {
    $("#data-session").add(new Option(`${value.title} · ${value.sessionId}`, value.sessionId));
  }
  $("#data-session").value = value.sessionId;
  $("#data-notice").textContent = `已保存会话 ${value.sessionId}；session/list、load、info 和配置查询使用同一份数据`;
});
async function refreshSamples(name) {
  samples = await api("/api/samples");
  options("#sample", [["", "选择通知样例"], ...samples.map(sample => [sample.name, sample.name])]);
  if (name) $("#sample").value = name;
}
function selectedSample() {
  const sample = samples.find(item => item.name === $("#sample").value);
  if (!sample) throw Error("请先选择通知样例");
  return structuredClone(sample);
}
$("#editor-format").onchange = () => act(async () => {
  const mode = $("#editor-format").value;
  $("#editor-format").value = editorMode;
  loadRule(readRule(), mode);
});
$("#method").onchange = () => act(async () => {
  loadRule(await api(`/api/rule?method=${encodeURIComponent($("#method").value)}`));
  note("已载入该方法规则；编辑后点击应用");
});
$("#reset").onclick = () => act(async () => {
  const rule = await api("/api/reset", {method: $("#method").value});
  loadRule(rule); note("已恢复该方法默认行为");
});
$("#load-sample").onclick = () => act(async () => {
  loadRule(selectedSample()); note("可编辑后保存通知样例；独立发送请选择目标连接和会话");
});
$("#save-sample").onclick = () => act(async () => {
  const rule = readRule();
  await api("/api/samples", {scenario: rule});
  await refreshSamples(rule.name);
  note(`已保存通知样例 ${rule.name}；同名覆盖，修改 name 可另存`);
});
$("#insert-sample").onclick = () => act(async () => {
  const rule = readRule(), sample = selectedSample();
  if (rule.kind === "notification") throw Error("请先选择并载入请求方法，再插入通知样例");
  const responseIndex = rule.frames.findIndex(frame => frame.message?.id === "$rpcId");
  if (responseIndex < 0) throw Error("请求脚本需要包含 id: $rpcId 的响应");
  const offset = rule.frames[responseIndex].offset;
  const duration = sample.frames[sample.frames.length - 1].offset;
  for (const frame of rule.frames.slice(responseIndex)) frame.offset += duration;
  rule.frames.splice(responseIndex, 0, ...sample.frames.map(frame => ({...frame, offset: offset + frame.offset})));
  loadRule(rule, "script");
  note(`已将通知插入 ${rule.method} 的响应前；检查关联字段与时序后应用`);
});
$("#notify").onclick = () => act(async () => {
  await api("/api/notify", {connection: $("#connection").value, sessionId: $("#session").value,
    scenario: readRule()});
  note("通知已安排发送");
});
$("#copy").onclick = () => act(async () => {
  if (!pairing) throw Error("iroh 端点尚未就绪");
  await navigator.clipboard.writeText(JSON.stringify(pairing, null, 2)); note("配对 JSON 已复制");
});
$("#apply").onclick = () => act(async () => {
  const rule = readRule();
  await api("/api/settings", {speed: Number($("#speed").value), scenario: rule});
  loadRule(rule);
  note(`已保存 ${rule.method}；请在 App 重新触发该请求，已开始的请求不受影响`);
});
$("#import").onclick = () => $("#file").click();
$("#file").onchange = () => act(async () => {
  const file = $("#file").files[0]; $("#file").value = ""; if (!file) return;
  if (file.size > 8 * 1024 * 1024) throw Error("文件最大 8 MB");
  const rule = await api("/api/import", {name: file.name, text: await file.text(), method: $("#method").value});
  loadRule(rule);
  if (rule.kind === "notification") {
    note("已载入通知脚本；请选择在线连接和会话后点击“发送编辑器通知”");
  } else {
    await api("/api/settings", {speed: Number($("#speed").value), scenario: rule});
    note((rule.warnings || []).join(" ") || `已导入并应用到 ${rule.method}`);
  }
});
$("#export").onclick = () => act(async () => {
  const rule = readRule();
  const text = JSON.stringify(rule, null, 2);
  const url = URL.createObjectURL(new Blob([text], {type: "application/json"}));
  const link = document.createElement("a"); link.href = url;
  link.download = `${rule.method.replaceAll("/", "-")}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
async function poll() {
  try {
    const state = await api("/api/state"); pairing = state.pairing;
    backendReady = state.apiVersion === 2;
    if (!backendReady) throw Error("Studio 后端版本过旧，请重启 Studio 并刷新页面");
    $("#pairing").textContent = pairing ? JSON.stringify(pairing, null, 2) : "等待 iroh 端点…";
    $("#status").textContent = pairing ? `iroh 已就绪 · ${state.peers} 个连接` : "iroh 尚未就绪";
    options("#method", state.methods.map(method => [method, `${state.overrides.includes(method) ? "● " : ""}${method}`]));
    options("#connection", state.connections.map(connection => [connection, connection]));
    options("#session", state.sessions.map(session => [session.sessionId, `${session.title} · ${session.sessionId}`]));
    options("#data-session", [["", "新建模拟会话"], ...state.sessions.map(session => [session.sessionId, `${session.title} · ${session.sessionId}`])]);
    $("#data-overrides").textContent = state.overrides.length ?
      `以下方法已配置自定义脚本，可能覆盖自动返回值：${state.overrides.join("、")}。需要完全使用模拟数据时，选择对应方法并恢复默认。` :
      "当前接口使用默认 Mock。下方接口脚本用于特殊响应、时序和故障测试。";
    if (!initialized) {
      $("#speed").value = state.speed;
      $("#method").value = "session/prompt";
      loadRule(await api("/api/rule?method=session%2Fprompt"));
      await refreshSamples();
      $("#agent-data").value = JSON.stringify(await api("/api/agent-data"), null, 2);
      await loadSessionData();
      $("#data-notice").textContent = "已读取模拟数据；修改后保存，对后续请求生效";
      $("#sources").textContent = state.sources.map(source => `${source.name}\n${source.warnings.join("\n")}`).join("\n\n");
      initialized = true;
    }
    if (state.error) note(state.error);
    const eventKey = JSON.stringify(state.events);
    if (eventKey !== eventsSignature) {
      eventsSignature = eventKey;
      $("#count").textContent = state.events.length;
      $("#events").replaceChildren(...state.events.slice().reverse().map(event => {
        const row = document.createElement("div"); row.className = `event ${event.direction}`;
        const title = document.createElement("b");
        title.textContent = `${event.direction === "in" ? "APP → STUDIO" : "STUDIO → APP"} · ${event.message.method || "response"} · #${event.connection}`;
        const pre = document.createElement("pre"); pre.textContent = JSON.stringify(event.message, null, 2);
        row.append(title, pre); return row;
      }));
    }
  } catch (error) {
    backendReady = false;
    $("#status").textContent = "Studio 未就绪";
    note(error.message);
  }
  setTimeout(poll, 1200);
}
poll();
