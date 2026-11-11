# ACP 方法与调试样例

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
| session/set_config_option | 根据 sessionId/configId/value 校验统一可选值，更新会话配置并发送 config_option_update；provider 切换联动模型选项，mode 同步模式查询 |
| A/session/read_state、mark_read | 根据模拟消息计算版本与指纹；匹配当前边界才确认已读 |
| A/session/prompt/enqueue | 按 sessionId/requestId 去重，校验重复内容，终态可显式 retry |
| session/prompt | 执行已登记的模拟请求；发送 running、用户消息、脚本事件、终态和响应 |
| A/session/prompt/list | 查询已持久化请求，不再以连接为作用域；重连后可查 |
| A/session/prompt/cancel | 定向取消等待或运行中的模拟请求 |
| R/projects/list、models/list、capabilities/list | 读取统一 Agent 数据中的项目、模型与能力；初次从调试文档初始化，可在控制台修改 |
| R/session/modes/get | 读取当前模拟会话模式，未传会话则返回样例默认模式 |
| R/session/capabilities/list | 正确方法名；返回 extensions/skills，extensions 暂为空 |
| R/approvals/list、artifacts/list、A/session/message_feedback/list、A/sidebar/pins/get | 合法空响应；不代表审批、产物下载、反馈写入、置顶修改已实现 |

两类 `session/update` 通知由相应方法或控制台主动发送，不当成 request。

## 响应配置与导入

日常配置使用「模拟 Agent 数据」入口：全局定义项目、模型、默认模型、能力及 select 配置选项，会话定义 ID、标题、cwd 和当前 values。新增会话会登记进统一状态，session/list/load/info、历史与配置查询可使用同一个 ID。数据保存在 agent-state.json，App 的 set_config_option 修改也会持久保存；读取会话当前值即可查看。逐方法脚本优先，适合特殊输出、时序或错误场景。

每个方法单独配置 `acp-studio/v1` 脚本，保存后直接生效，其他方法继续走状态化 Mock。简单静态接口默认只编辑 `result`；需要通知帧、响应时间或错误时使用完整脚本。支持响应前后通知；prompt 响应仍是最后一帧。导入纯结果 JSON 时使用当前选中的方法；导出的完整脚本包含方法名，方便修改后重新导入。

支持变量：

- `$rpcId`：本次 JSON-RPC 请求 ID，保留字符串/数字类型。
- `$sessionId`、`$requestId`、`$messageId`、`$agentMessageId`、`$runId`。
- `$pageId`、`$loadId`、`$waitId`、`$cwd`。
- `$params.xxx`：读取当前请求参数。
- `$config.xxx`：读取当前会话配置值，例如 `$config.model`、`$config.mode`。默认 prompt 的模型通知使用当前会话选择。
- `$result`：当前方法按本地模拟状态计算的结果。
- `$notifications`：作为 frame.message，展开当前方法生成的通知列表。
- `$identity:类型:原ID`：导入器生成的关联 ID；同一脚本引用保持一致。独立通知在同一会话中保持对应关系。

变量只替换完整 JSON 值，不替换正文中的任意子串。所有样例中的命令、路径和模型名只作数据使用，不执行、不访问。

带说明的文档导入时保留 `capturedResponse` 与警告；有状态方法默认使用 `$result` 和 `$notifications`，不直接写回旧会话 ID、请求状态和游标。真实模型/项目/能力列表可以直接作为静态响应使用。

所有方法的交互显示在内存中的消息面板，最多保留 250 条，不生成 traffic 或 record 文件。有状态方法默认根据当前会话生成响应，防止旧 ID、游标和状态污染；可手动编辑脚本进行故障注入。自定义历史分页可返回自己的消息和分页字段，通知/响应的 pageId 必须对应本次请求，sequence 连续，updateCount 与已发送的历史通知数一致。校验失败会返回错误，不再静默覆盖自定义结果。可直接导入 `examples/history.json` 进行编辑。

## 通知与示例 prompt

控制台提供从文档提取的 32 个通知样例。主流程是选择请求方法，再将通知样例插入该方法的响应前，组合 request → notifications → response。独立通知可选择连接/会话后发送；必要的会话 ID 和消息关联会调整。`history_replay` 只随历史分页调用发送，因为 App 必须先登记 pageId。

「载入通知单独编辑」可编辑通知方法、params 和样例 name，再用「保存通知样例」持久保存。同名覆盖内置或已有自定义样例，修改 name 可新增。配置保存在 settings.json 的 notifications 中；内置文档样例不被修改。

`examples/desktop-prompt.json` 从真实记录选取完整的工具开始/结束对、助手文本、用量与模型事件，生成可用的展示场景。它使用人工 0.3 秒间隔；不同来源的用量/模型事件关联到本轮示例助手消息。这不是原始完整事务或真实模型执行结果。

不自动广播全部样例，因为 requiredExtensionWait、进度、队列、历史等通知需要正确的前置状态。可在编辑器按需要组合场景。

## 边界

这是一台模拟测试电脑，不是完整 Desktop Agent。无真实工具、模型调用、上传下载、反向审批、真实目录访问或转发代理。当前会话内只运行一个 prompt，不实现真实桌面完整调度器。样例选项变化不等于完成 Desktop 的配置关联规则。样例库可能含个人路径和对话内容，仅保存在本机。

App 沿用原有 request / response / notification 和 poll 逻辑，本轮无需修改 App 或真实 Desktop。

## 既有链路验证（0.2）

- 内存传输协议检查：方法处理、同 ID 去重、配置读回、分页 pageId/sequence/updateCount、已读确认、定向取消、重连查询、状态持久化。
- 真实 iroh + iOS 模拟器：初始化、新建会话、请求登记、prompt、工具开始/结束、正文、用量/模型、已读确认；实际触发 17 类方法，无 RPC 错误。
- 控制台主动发送通知：未发起新 prompt，App 仍通过原有链路展示通知文本。
- 控制台方法切换和 HTTP API 访问校验通过。

尚未逐一做全部 UI 分支、真机连接或真实 Desktop 完整语义回归；没有新增测试文件。

## 收敛验证（0.3）

- 内存传输与 HTTP 处理检查：22 个接口默认配置、结果覆盖、请求 ID 替换、状态化 Mock、纯结果/完整脚本导入、错误响应、定时通知、prompt 和主动通知。
- 配置持久化、旧设置加载、恢复默认、录制 API 移除、无新增录制/流量文件、消息面板 250 条上限。
- 本地 iroh 启动与浏览器检查：简单结果编辑、应用后重新载入、完整脚本转换、复杂脚本保护；未重跑手机端端到端连接。

## 统一数据验证（0.4）

- 全局数据与会话保存、会话 ID/cwd 在 list/load/info 间一致、App 配置请求生成相同内容的 notification/response。
- provider/model 联动、mode 查询一致、非法选择不修改状态、新会话默认值、重启持久化及旧状态迁移。
- 修改可选项后保留合法当前值，删除的选项回退到有效值；现有自定义历史脚本保持可用。
