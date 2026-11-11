# ACP 方法与真实记录

## 启动与样例来源

```sh
python3 studio.py
```

默认加载 `examples/desktop-catalog.json`，由以下目录的 18 个文件解析：

`/Users/tzz/Documents/工作/Kiwi/AgnesCode_ACP_Method`

原始文档保持不变。提取到 18 个 request、16 个 response、32 个 notification。文档含说明、分页片段及跨会话样例；它们不是完整逐帧录制。

重新生成：

```sh
python3 studio.py --fixtures-dir '/Users/tzz/Documents/工作/Kiwi/AgnesCode_ACP_Method' \
  --export-catalog examples/desktop-catalog.json
```

也可以启动时指定 `--fixtures-dir`。首次创建本地状态时从样例会话列表和历史片段初始化会话；已有状态不覆盖。可使用另一个 `--data-dir` 创建干净环境。

## 方法覆盖

以下 A 为 `_agnes/unstable/`，R 为 `_agnes/unstable/remote/`。

| 方法 | 行为 |
| --- | --- |
| initialize | ACP 1，声明实际支持的列表/加载/请求队列；不会照抄真实端的所有能力 |
| session/new | 创建持久化的本地模拟会话；样例配置、权限模式和初始化通知 |
| session/list | 当前模拟会话，支持目录/标题过滤及游标分页 |
| session/load | 配置与最新历史页；不会把加载历史再次写入历史 |
| A/session/info | 当前会话信息、运行状态和已读状态 |
| A/session/history/load | 当前会话历史，回传本次 pageId；sequence 连续，updateCount 对齐 |
| session/set_config_option | 校验样例选项，修改本地配置并发送 config_option_update；不调用真实模型 |
| A/session/read_state、mark_read | 根据模拟消息计算版本与指纹；匹配当前边界才确认已读 |
| A/session/prompt/enqueue | 按 sessionId/requestId 去重，校验重复内容，终态可显式 retry |
| session/prompt | 执行已登记的模拟请求；发送 running、用户消息、脚本事件、终态和响应 |
| A/session/prompt/list | 查询已持久化请求，不再以连接为作用域；重连后可查 |
| A/session/prompt/cancel | 定向取消等待或运行中的模拟请求 |
| R/projects/list、models/list、capabilities/list | 使用真实记录中的列表数据，仅作模拟展示 |
| R/session/modes/get | 读取当前模拟会话模式，未传会话则返回样例默认模式 |
| R/session/capabilities/list | 正确方法名；返回 extensions/skills，extensions 暂为空 |
| R/approvals/list、artifacts/list、A/session/message_feedback/list、A/sidebar/pins/get | 合法空响应；不代表审批、产物下载、反馈写入、置顶修改已实现 |

两类 `session/update` 通知由相应方法或控制台主动发送，不当成 request。

## 回放与导入

每个方法单独配置 `acp-studio/v1` 脚本。回放模式只覆盖已配置的方法，其他方法继续走状态化 Mock。可以编辑通知帧和响应时间，支持响应前后通知；prompt 响应仍是最后一帧。

支持变量：

- `$rpcId`：本次 JSON-RPC 请求 ID，保留字符串/数字类型。
- `$sessionId`、`$requestId`、`$messageId`、`$agentMessageId`、`$runId`。
- `$pageId`、`$loadId`、`$waitId`、`$cwd`。
- `$params.xxx`：读取当前请求参数。
- `$result`：当前方法按本地模拟状态计算的结果。
- `$notifications`：作为 frame.message，展开当前方法生成的通知列表。
- `$identity:类型:原ID`：导入器生成的关联 ID；同一脚本引用保持一致。独立通知在同一会话中保持对应关系。

变量只替换完整 JSON 值，不替换正文中的任意子串。所有样例中的命令、路径和模型名只作数据使用，不执行、不访问。

带说明的文档导入时保留 `capturedResponse` 与警告；有状态方法默认使用 `$result` 和 `$notifications`，不直接写回旧会话 ID、请求状态和游标。真实模型/项目/能力列表可以直接作为静态响应使用。

所有方法的交互写入 traffic 日志与 record 文件；有状态方法的实际消息保存在 `capturedFrames`。默认回放根据当前会话重新生成响应，防止旧 ID、游标和状态污染；可手动编辑脚本进行故障注入。历史分页始终以 Studio 的当前页边界为准，不能拿旧 pageId 或残缺序号替代。

## 通知与示例 prompt

控制台提供全部 32 个原始通知样例，可选择连接/会话后发送；必要的会话 ID 和消息关联会调整。`history_replay` 只随历史分页调用发送，因为 App 必须先登记 pageId。

`examples/desktop-prompt.json` 从真实记录选取完整的工具开始/结束对、助手文本、用量与模型事件，生成可用的展示场景。它使用人工 0.3 秒间隔；不同来源的用量/模型事件关联到本轮示例助手消息。这不是原始完整事务或真实模型执行结果。

不自动广播全部样例，因为 requiredExtensionWait、进度、队列、历史等通知需要正确的前置状态。可在编辑器按需要组合场景。

## 边界

这是一台模拟测试电脑，不是完整 Desktop Agent。无真实工具、模型调用、上传下载、反向审批、真实目录访问或转发代理。当前会话内只运行一个 prompt，不实现真实桌面完整调度器。样例选项变化不等于完成 Desktop 的配置关联规则。样例库可能含个人路径和对话内容，仅保存在本机。

App 沿用原有 request / response / notification 和 poll 逻辑，本轮无需修改 App 或真实 Desktop。

## 本轮验证

- 内存传输协议检查：方法处理、同 ID 去重、配置读回、分页 pageId/sequence/updateCount、已读确认、定向取消、重连查询、状态持久化与录制文件校验。
- 真实 iroh + iOS 模拟器：初始化、新建会话、请求登记、prompt、工具开始/结束、正文、用量/模型、已读确认；实际触发 17 类方法，无 RPC 错误。
- 控制台主动发送通知：未发起新 prompt，App 仍通过原有链路展示通知文本。
- 控制台方法切换和 HTTP API 访问校验通过。

尚未逐一做全部 UI 分支、真机连接或真实 Desktop 完整语义回归；没有新增测试文件。
