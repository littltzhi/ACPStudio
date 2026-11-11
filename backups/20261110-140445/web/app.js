const $ = selector => document.querySelector(selector);
const key = $('meta[name="studio-key"]').content;
let pairing = null;
let recordsSignature = "";
let eventsSignature = "";
let initialized = false;
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
async function act(work) { try { await work(); } catch (error) { note(error.message); } }
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
  $("#editor").value = JSON.stringify(JSON.parse(await file.text()), null, 2);
  $("#mode").value = "replay"; note("已载入，点击应用后生效");
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
    if (!initialized) { $("#mode").value = state.mode; $("#speed").value = state.speed; initialized = true; }
    if (state.error) note(state.error);
    const recordKey = JSON.stringify(state.records);
    if (recordsSignature !== recordKey) {
      recordsSignature = recordKey;
      $("#records").replaceChildren(...state.records.map(record => {
        const button = document.createElement("button");
        button.textContent = record.name;
        button.onclick = () => act(async () => {
          const document = await api(`/api/records/${record.id}`);
          $("#editor").value = JSON.stringify(document, null, 2);
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
