# ACP Studio · 本地测试电脑

当前版本 0.2：已支持按 ACP 方法配置响应、请求前后通知、独立通知发送及全方法录制。内置 18 份真实 Desktop 调试文档解析出的样例。详细范围与限制见 [METHODS.md](METHODS.md)。

独立运行的 ACP Mock / 回放工具，供 AgnesCN 的 Debug 入口直连。真实使用 iroh 1.1.0 / QUIC，沿用 `goose-acp/1` ALPN 和 ACP JSON-RPC，不访问 BFF、Relay 或模型服务。

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

## Mock、JSON 和历史

- 内置 Mock：每次 prompt 输出固定 Markdown 回复，不运行模型或真实工具。
- JSON 回放：导入 JSON 或调试文档会立即应用到识别出的 ACP 方法；也可选择方法、编辑脚本后点击「应用到该方法」。规则按方法分别保存。
- 历史回放：每个请求自动保存 `record-*.json`；点击历史记录载入，再应用。有状态方法保留原始 `capturedFrames`，默认回放使用当前会话状态生成的 `$result` / `$notifications`。
- 调试文档：可导入带说明文字、request/response 标签的 JSON 文件；响应会转换为脚本，混用 ID 等问题显示在导入报告中。
- 主动通知：从通知样例载入，选择在线连接和会话，点击「发送编辑器通知」。`history_replay` 仅随历史分页请求发送。
- 所有双向 ACP 消息写入 `traffic-*.jsonl`，界面保留最近 250 条。
- 支持原速、0.5×、2×、4×及即时回放；切换设置不改变已开始的 prompt。
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

`offset` 为相对当前请求的秒数，不递减，最大 3600 秒；请求脚本必须有唯一的 RPC 响应，也可使用 `error` 代替 `result`。除 prompt 外支持响应之后继续发送通知。独立通知脚本使用 `kind: "notification"`，不包含 RPC 响应。导入上限 8 MB、10000 帧。

历史记录来自 App 与 **Studio** 的交互，不是现有真实 Desktop 的流量录制。用户文本会保存在本机日志和录制中；不会写入配对口令。

## 当前范围

已实现 initialize、session/new/list/load、历史分页、配置变更、prompt 入队/执行/查询/取消、已读状态，以及样例模型、能力、项目列表。会话、配置、消息历史、请求状态保存在数据目录的 `agent-state.json`；运行中请求在重启后标为 interrupted，不自动重新执行。

配置切换只影响 Studio 的模拟会话，模型和工具不会实际运行。当前不实现附件读写、真实项目管理和审批反向请求，不支持完整双向审批分支、断点调试或转发真实 Desktop。未知方法默认返回 method-not-found；可以添加该方法的静态响应脚本。

控制台只在本机提供；API 使用本次启动的控制台密钥校验请求。向其他设备提供 iroh 监听前，请确认局域网可信并妥善保存配对 JSON。

## 与 BFF 测试电脑模式的差异

可复用：ACP 方法处理、会话 Mock、JSON 脚本、ID 映射、时间轴、记录与控制台。

需要另行实现的 BFF 接入：持久电脑身份、账号设备注册、上线/心跳、Relay 配置与准入、`mobile_access_id` 的 Hello 校验、授权到期/撤销/接管处理。本版只接受带 `studio_version` 的独立配对握手，不能仅更换一个 URL 就成为 BFF 电脑。

直接模式主要验证 App 的 ACP 业务和渲染；BFF 模式还验证完整设备发现与安全授权、Relay 路径和租约恢复。
