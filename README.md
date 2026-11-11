# ACP Studio · 本地测试电脑

当前版本 0.4：统一编辑模拟 Agent 数据与会话当前值，根据请求参数自动构造响应和通知；保留按 ACP 方法编辑脚本。内置 18 份真实 Desktop 调试文档解析出的样例。详细范围与限制见 [METHODS.md](METHODS.md)。

独立运行的 ACP 接口调试工具，模拟桌面 Agent，供 AgnesCN 的 Debug 入口直连。真实使用 iroh 1.1.0 / QUIC，沿用 `goose-acp/1` ALPN 和 ACP JSON-RPC，不访问真实 Desktop、BFF、Relay 或模型服务。

## 启动

本机交付目录包含 macOS Apple Silicon 传输程序：

```sh
python3 studio.py
```

- 控制台：`http://127.0.0.1:9091`
- iroh：`127.0.0.1:43187`，UDP
- 数据：`~/Library/Application Support/ACPStudio/`

从源码构建传输程序：

```sh
cargo build --locked --manifest-path native/Cargo.toml
python3 studio.py
```

需要 Python 3.10+、Rust 1.94+。本次使用 Rust 1.96.1。控制台没有 npm/pip 依赖。

## App 连接

1. 使用 Debug 构建启动 AgnesCN。登录页和个人设置里的 Debug 都有 `ACP Studio` 按钮。
2. 控制台点击「复制配对 JSON」。
3. App 的「ACP Studio · 本地直连」页面粘贴 JSON，点击连接。
4. 显示「ACP 已就绪」后，点击「打开远程聊天」。

此入口创建独立的 RemoteServices、临时移动端身份和内存中的执行上下文，不使用正常 BFF 的设备列表、challenge/connect、租约续期、账号密钥或设备选择记录。新会话推荐也使用空的本地 Repository。App 自身启动时其他云功能可能仍发送请求，不属于 Studio 链路。

客户端验证 Studio 的端点公钥；Studio 验证本次启动的随机配对口令。配对文件位于独立数据目录，权限为 0600。公钥和口令每次启动更换；重启 Studio 后需要重新复制配对 JSON。

关闭调试页面会停止独立服务；进入后台断开，回前台重连并初始化 ACP，不自动重发 prompt。断线后也可点击重新连接。Release 构建不编译这些 Swift 调试入口及服务。

## 真机

手机与 Mac 位于同一可信局域网：

```sh
python3 studio.py --bind 0.0.0.0:43187 --advertise-address 192.168.1.23:43187
```

将示例 IP 改成 Mac 的局域网 IPv4，允许防火墙的 UDP 43187 和 App 的本地网络访问。复制此时控制台生成的 JSON。控制台始终只监听 Mac 本机；无需设置 HTTP 代理或安装 CA。

这是局域网直连，不提供跨公网 NAT 穿透或 Relay 回退。局域网隔离、VPN、防火墙可能阻止 UDP。

## 模拟 Agent 数据

控制台顶部的「模拟 Agent 数据」是日常调试入口。开发者配置业务值，无需为每个接口重复编写 JSON-RPC 响应：

- **Agent 可选值与默认值**：维护 `models`、`defaultModel`、`projects`、`temporaryWorkingDir`、`capabilities` 和 `configOptions`。provider/model 配置项由模型列表生成；mode 等其他配置使用 `type: "select"`、`options` 和 `defaultValue`。
- **会话与用户当前值**：维护 `sessionId`、`title`、`cwd` 和 `values`。空 ID 保存时自动生成，新 ID 新增，同 ID 更新已有会话；原有消息历史和请求队列保留。
- 保存 Agent 数据后，可选项同步到已有会话，已有合法选择保留；被移除的选择回退到有效的默认值。`defaultModel` 和配置 `defaultValue` 用于新会话的初始选择。
- App 请求 `session/set_config_option` 时，Studio 使用 `sessionId/configId/value` 校验并更新该会话，自动发送 `config_option_update` 通知和包含同一配置列表的 response。切换 provider 会重建模型选项，不兼容的旧 model 回退到该 provider 的有效模型。
- `session/list`、`session/load`、`session/info`、模式查询读取同一份会话数据；模型、项目和能力查询读取同一份 Agent 数据。`session/new` 使用默认值，接受请求中的 cwd 和权限模式；列表过滤、历史分页等继续按请求参数执行。

例如会话当前值可以是：

```json
{
  "sessionId": "demo-session",
  "title": "配置测试",
  "cwd": "/demo/project",
  "values": {
    "provider": "agnes",
    "model": "auto",
    "mode": "plan"
  }
}
```

这些配置值必须存在于上方 Agent 数据中。保存后 App 就可以通过列表获取该会话，再加载或修改它，不再只是返回一个内部不存在的会话 ID。项目关联通过 `cwd` 与项目 `path` 对应；临时会话也可以使用其他工作目录，不会实际访问该路径。

App 改过设置后，点击「读取会话当前值」查看；编辑器不会在后台轮询时覆盖未保存内容。控制台保存数据对后续请求生效，不主动向在线 App 广播更新，需要重新加载或查询。当前只模拟 select 配置与模型/provider、mode 的关联，不执行真实模型、工具或所有 Desktop 业务规则。

已配置的接口脚本仍然优先，界面会列出这些方法；需要完全使用统一数据时，恢复对应方法默认，或使用 `$result` / `$notifications`。未知方法仍需要显式脚本，不会从任意请求字段猜测协议。

## 按接口编辑 JSON

- 内置 Mock：每次 prompt 输出固定 Markdown 回复，不运行模型或真实工具。
- 选择方法，编辑 JSON 后点击「应用到该方法」。已配置方法直接使用对应脚本，其他方法继续使用默认 Mock，无需切换模式。「恢复该方法默认」移除该接口的自定义配置。
- 简单接口默认只显示响应结果，例如 `approvals/list` 的 `{"approvals":[]}`。Studio 自动补齐 `jsonrpc` 和当前请求的 `id`。
- 通知、延迟和错误使用「完整脚本」。有状态接口默认保留 `$result` / `$notifications`，按当前模拟状态生成响应。
- 导入完整脚本会立即应用到脚本指定的方法；只含响应结果的 JSON 应用到当前选择的方法。导出接口 JSON 会保存完整脚本（含方法名），可用本地编辑器修改后重新导入。
- 调试文档：可导入带说明文字、request/response 标签的 JSON 文件；响应会转换为脚本，混用 ID 等问题显示在导入报告中。
- 请求通知：选择方法和通知样例，点击「插入到当前方法」，通知帧插在响应前；检查关联字段、时间和顺序后应用。插入的是副本，修改样例不会自动修改已配置的方法。
- 编辑通知样例：点击「载入通知单独编辑」，修改脚本后点击「保存通知样例」。同名 `name` 覆盖，修改 `name` 可另存；可编辑每帧的 `message.method` 与 `params`。自定义样例随设置持久保存。
- 主动通知：载入通知，选择在线连接和会话，点击「发送编辑器通知」，用于不依附请求的局部状态推送。`history_replay` 仅随历史分页请求发送。
- 双向 ACP 消息面板保留最近 250 条，便于核对请求和响应；不再自动写入录制或流量文件。
- 支持 1×、0.5×、2×、4×及即时发送；速度随「应用到该方法」保存，对后续请求和主动通知生效，不改变已开始的脚本。
- App 的停止按钮调用定向取消；Studio 终止等待并返回 cancelled。不同连接的请求分别关联。

脚本示例见 `examples/reply.json`。格式：

```json
{
  "format": "acp-studio/v1",
  "method": "session/prompt",
  "frames": [
    {
      "offset": 0.3,
      "message": {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
          "sessionId": "$sessionId",
          "update": {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "$agentMessageId",
            "content": { "type": "text", "text": "来自本地 JSON" }
          }
        }
      }
    },
    {
      "offset": 1,
      "message": {
        "jsonrpc": "2.0",
        "id": "$rpcId",
        "result": { "stopReason": "end_turn" }
      }
    }
  ]
}
```

占位符仅在整个 JSON 值等于占位符时替换：

| 占位符 | 本次请求对应值 |
| --- | --- |
| `$rpcId` | JSON-RPC 请求 ID，保留原类型 |
| `$sessionId` | 当前会话 ID |
| `$requestId` | App 登记的业务请求 ID |
| `$messageId` | App 的用户消息 ID |
| `$agentMessageId` | 本轮生成的助手消息 ID，所有回复分片复用 |
| `$params.xxx` | 当前请求参数中的字段 |
| `$config.xxx` | 当前会话配置值，例如 `$config.model`、`$config.mode` |

`offset` 为脚本开始后的秒数，不递减，最大 3600 秒；请求脚本必须有唯一的 RPC 响应，也可使用 `error` 代替 `result`。除 prompt 外支持响应之后继续发送通知。独立通知脚本使用 `kind: "notification"`，不包含 RPC 响应。导入上限 8 MB、10000 帧。

接口配置保存在数据目录的 `settings.json`，统一 Agent 数据、模拟会话及消息历史保存在 `agent-state.json`。升级时没有统一数据的旧状态会从样例初始化，已有合法会话选择保留。旧 `mode` 字段不再使用；旧录制与流量文件不再展示或读取，磁盘文件保留。

## 默认数据来源与编辑生效

默认 Mock 是模拟数据，不能当作完整协议规范：

- `examples/desktop-catalog.json`：从调试文档提取的响应字段、配置选项、初始会话、历史片段与通知素材；用于首次初始化统一 Agent 数据。启动时传入 `--fixtures-dir` 可改用指定目录，已有持久化的 Agent 数据不会被覆盖。
- `agent.py`：编写的默认响应，以及根据统一 Agent 数据、当前模拟会话、队列、已读状态和历史页计算的结果。部分额外响应字段仍使用文档模板。
- `examples/desktop-prompt.json`：有文档样例时的默认 prompt 场景，由通知片段组装；无文档样例时使用 `examples/reply.json`。

选择方法后，编辑 `result` 或 `frames`，点击「应用到该方法」，再在 App 触发同一方法的新请求；应用不会重发已经结束或执行中的请求。完整脚本也可以修改外层 `method` 并应用，以新增自定义方法。方法名称修改后会保存到新方法，原方法的配置保留，可选择原方法后恢复默认。

`"$result"` 与 `"$notifications"` 是动态 Mock 占位符，不是实际业务对象。需要固定返回内容时，将它们替换为完整的结果对象或通知帧。历史分页的完整可编辑场景见 `examples/history.json`，prompt 场景见 `examples/reply.json`。

`session/new` 的 `result.sessionId` 必须使用 `$sessionId`，以保持 App 和本地模拟会话一致。历史分页的通知和响应使用 `$pageId`，`sequence` 从 0 连续递增，响应的 `updateCount` 等于该脚本实际发送的历史通知数。Studio 校验这些关联字段，自定义历史结果不再被内置结果覆盖；不一致时返回明确错误。

修改 Python 代码后必须重启 Studio，刷新网页不能更新已运行的 Python 后端。控制台会检查后端版本，不兼容时显示重启提示并阻止保存。重启后配对信息更换，需要 App 重新配对。修改样例库、通知来源或 `--fixtures-dir` 文件同样需要重启；界面保存的方法和通知配置不需要重启。

## 当前范围

已实现 initialize、session/new/list/load、历史分页、配置变更、prompt 入队/执行/查询/取消、已读状态，以及样例模型、能力、项目列表。会话、配置、消息历史、请求状态保存在数据目录的 `agent-state.json`；运行中请求在重启后标为 interrupted，不自动重新执行。

配置切换只影响 Studio 的模拟会话，模型和工具不会实际运行。当前不实现附件读写、真实项目管理和审批反向请求，不支持完整双向审批分支、断点调试或转发真实 Desktop。未知方法默认返回 method-not-found；可以添加该方法的静态响应脚本。

控制台只在本机提供；API 使用本次启动的控制台密钥校验请求。向其他设备提供 iroh 监听前，请确认局域网可信并妥善保存配对 JSON。

## 与 BFF 测试电脑模式的差异

可复用：ACP 方法处理、会话 Mock、JSON 脚本、ID 映射、消息时序与控制台。

需要另行实现的 BFF 接入：持久电脑身份、账号设备注册、上线/心跳、Relay 配置与准入、`mobile_access_id` 的 Hello 校验、授权到期/撤销/接管处理。本版只接受带 `studio_version` 的独立配对握手，不能仅更换一个 URL 就成为 BFF 电脑。

直接模式主要验证 App 的 ACP 业务和渲染；BFF 模式还验证完整设备发现与安全授权、Relay 路径和租约恢复。
