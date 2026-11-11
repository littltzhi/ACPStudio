const $ = selector => document.querySelector(selector);
const key = $('meta[name="studio-key"]').content;
let pairing = null;
let recordsSignature = "";
let eventsSignature = "";
let initialized = false;
let samples = [];
const template = {
  format: "acp-studio/v1", method: "session/prompt",
  frames: [
    {offset: .3, message: {jsonrpc: "2.0", method: "session/update", params: {
      sessionId: "$sessionId", update: {sessionUpdate: "agent_message_chunk", messageId: "$agentMessageId",
        content: {type: "text", text: "## 本地 JSON 回放\n\n"}}}}},
    {offset: 1.2, message: {jsonrpc: "2.0", method: "session/update", params: {
      sessionId: "$sessionId", update: {sessionUpdate: "agent_message_chunk", messageId: "$agentMessageId",
        content: {type: "text", text: "通过 iroh / QUIC 发送，无需 BFF。"}}}}},
    {offset: 1.5, message: {jsonrpc: "2.0", id: "$rpcId", result: {stopReason: "end_turn"}}}
  ]
};
$("#editor").value = JSON.stringify(template, null, 2);
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET",
    headers: {"X-Studio-Key": key, "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) throw Error(data.error);
  return data;
}
function note(text) { $("#notice").textContent = text; }
function loadRule(rule) {
  $("#editor").value = JSON.stringify(rule, null, 2);
  $("#script-method").textContent = rule.method;
  if ([...$("#method").options].some(item => item.value === rule.method)) $("#method").value = rule.method;
}
function options(selector, items) {
  const select = $(selector), current = select.value;
  if (JSON.stringify([...select.options].map(item => [item.value, item.text])) === JSON.stringify(items)) return;
  select.replaceChildren(...items.map(([value, text]) => new Option(text, value)));
  if (items.some(([value]) => value === current)) select.value = current;
}
async function act(work) { try { await work(); } catch (error) { note(error.message); } }
$("#method").onchange = () => act(async () => {
  loadRule(await api(`/api/rule?method=${encodeURIComponent($("#method").value)}`));
  note("已载入该方法规则；编辑后点击应用");
});
$("#reset").onclick = () => act(async () => {
  const rule = await api("/api/reset", {method: $("#method").value});
  loadRule(rule); note("已恢复该方法默认行为");
});
$("#load-sample").onclick = () => act(async () => {
  if ($("#sample").value === "") throw Error("请先选择通知样例");
  loadRule(samples[Number($("#sample").value)]); note("选择目标连接和会话后发送；也可复制帧加入请求脚本");
});
$("#notify").onclick = () => act(async () => {
  await api("/api/notify", {connection: $("#connection").value, sessionId: $("#session").value,
    scenario: JSON.parse($("#editor").value)});
  note("通知已安排发送");
});
$("#copy").onclick = () => act(async () => {
  if (!pairing) throw Error("iroh 端点尚未就绪");
  await navigator.clipboard.writeText(JSON.stringify(pairing, null, 2)); note("配对 JSON 已复制");
});
$("#apply").onclick = () => act(async () => {
  await api("/api/settings", {mode: $("#mode").value, speed: Number($("#speed").value),
    scenario: JSON.parse($("#editor").value)});
  note("已应用到之后的请求");
});
$("#import").onclick = () => $("#file").click();
$("#file").onchange = () => act(async () => {
  const file = $("#file").files[0]; $("#file").value = ""; if (!file) return;
  if (file.size > 8 * 1024 * 1024) throw Error("文件最大 8 MB");
  const rule = await api("/api/import", {name: file.name, text: await file.text()});
  loadRule(rule);
  if (rule.kind === "notification") {
    note("已载入通知脚本；请选择在线连接和会话后点击“发送编辑器通知”");
  } else {
    await api("/api/settings", {mode: "replay", speed: Number($("#speed").value), scenario: rule});
    note((rule.warnings || []).join(" ") || `已导入并应用到 ${rule.method}`);
  }
});
$("#export").onclick = () => act(async () => {
  const text = JSON.stringify(JSON.parse($("#editor").value), null, 2);
  const url = URL.createObjectURL(new Blob([text], {type: "application/json"}));
  const link = document.createElement("a"); link.href = url; link.download = "acp-reply.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
async function poll() {
  try {
    const state = await api("/api/state"); pairing = state.pairing;
    $("#pairing").textContent = pairing ? JSON.stringify(pairing, null, 2) : "等待 iroh 端点…";
    $("#status").textContent = pairing ? `iroh 已就绪 · ${state.peers} 个连接` : "iroh 尚未就绪";
    options("#method", state.methods.map(method => [method, `${state.overrides.includes(method) ? "● " : ""}${method}`]));
    options("#connection", state.connections.map(connection => [connection, connection]));
    options("#session", state.sessions.map(session => [session.sessionId, `${session.title} · ${session.sessionId}`]));
    if (!initialized) {
      $("#mode").value = state.mode; $("#speed").value = state.speed;
      $("#method").value = "session/prompt";
      loadRule(await api("/api/rule?method=session%2Fprompt"));
      samples = await api("/api/samples");
      options("#sample", [["", "选择真实通知"], ...samples.map((sample, index) => [String(index), sample.name])]);
      $("#sources").textContent = state.sources.map(source => `${source.name}\n${source.warnings.join("\n")}`).join("\n\n");
      initialized = true;
    }
    if (state.error) note(state.error);
    const recordKey = JSON.stringify(state.records);
    if (recordsSignature !== recordKey) {
      recordsSignature = recordKey;
      $("#records").replaceChildren(...state.records.map(record => {
        const button = document.createElement("button");
        button.textContent = record.name;
        button.onclick = () => act(async () => {
          const document = await api(`/api/records/${record.id}`);
          loadRule(document);
          $("#mode").value = "replay"; note("历史已载入，点击应用后生效");
        });
        return button;
      }));
    }
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
  } catch { $("#status").textContent = "Studio 未连接"; }
  setTimeout(poll, 1200);
}
poll();
