# Harness 数据契约

| 项目 | 定义 |
| --- | --- |
| 契约版本 | `1.0.0` |
| 记录结构 | [records.schema.json](records.schema.json) |
| 记录目录 | [record-catalog.json](record-catalog.json) |
| 存储与接口 | [storage-and-api.md](storage-and-api.md) |
| 运行调度 | [运行状态机与调度契约](../runtime/运行状态机.md) |
| 同页数据可视化 | [inspector.md](inspector.md) |
| 示例 | [records.example.json](examples/records.example.json) |

这里定义 Harness 自身持久化的数据。结构化阶段内容继续使用 [output-schemas/v2](../output-schemas/v2/README.md)；最终 Nexo Graph 仍是直接 `Project` JSON。内部记录与 Step1 的纯文本全局分析不注册为 `Agent.output_type`，不进入 Nexo Graph 根对象。当前共有 17 个活动模型输出 Schema。

## 1. 公共字段与引用

所有记录按 JSON Schema Draft 2020-12 验证，必须启用 `uuid`、`date-time` 格式检查。记录字段均须出现；允许空值的字段显式使用 `null`，封闭对象中的未声明字段拒绝保存。`Content.value`、配置 values、事件 payload 和恢复 state 等开放载荷还须通过各自绑定的业务 Schema、配置规则或处理器验证，不能仅凭外壳通过就直接执行。内部 Schema 随服务版本发布和迁移，不作为模型输出类型开放任意在线改结构。

| 字段 | 类型与含义 |
| --- | --- |
| `record_type` | 记录类型，取记录目录中的固定值 |
| `schema_version` | 当前为 `1.0.0`，内部存储契约版本 |
| `id` | 服务端分配的 UUID，本条记录的身份 |
| `project_id` | Harness 项目 UUID；仅全局配置允许为空；Project 自身 `project_id=id` |
| `created_at` | UTC RFC3339 时间，使用 `Z` 后缀 |
| `updated_at / row_version` | 仅可变记录具有；用于更新时间与并发更新检查，`row_version` 从 1 递增 |

平台 `account_id` 为平台返回的不透明字符串。Nexo 的项目、章节、节点 ID 使用其原有字符串格式，与 Harness UUID 分开；不能重新分配已有 Nexo 身份。

统一沿用 `EvidenceRef={record_id,version,item_id,json_pointer}`：

- 产物引用：`record_id=Artifact.id`，`version` 为 `ArtifactVersion.version` 的十进制字符串。
- 决策引用：`record_id=Decision.decision_id`，`version` 为决策版本的十进制字符串。`Decision.id` 是具体版本记录的 UUID。
- 消息、检查点、调用快照等不可变记录：`record_id=id`，`version=null`。配置内容通过固定 `ConfigVersion.id` 引用，不能把发布状态变化当成内容变更。
- 条目引用通过稳定 `item_id` 定位；`json_pointer` 是指定版本中的字段位置，不能单独代替条目身份。
- UI 获取可变记录时返回当前 `row_version`；需要还原过去的状态时读取对应 RuntimeEvent／Checkpoint，不以当前记录冒充历史状态。

普通详情路由使用物理记录 UUID；业务 `EvidenceRef` 先经过统一 resolver，返回确切记录 UUID 和版本。两种 ID 不混用，也不得自动把固定历史引用替换为最新版本。

## 2. 记录清单

每一行对应 Schema 中同名 `$defs`；下表只解释职责，准确字段和枚举以 JSON Schema 为准。

| 定义 | 保存内容 | 可视化入口 |
| --- | --- | --- |
| `Project` | 账号归属、项目名称、状态与可选 Nexo 项目关联 | 项目概览 |
| `Conversation` | 用户可见会话、标题与消息序号 | 会话列表 |
| `HistoryRecord` | 用户及内部交互、实际模型输入／输出、工具调用／结果与错误 | 对话及执行记录 |
| `WorkSession` | 工作范围、历史代次、摘要引用与执行租约 | Session 视图 |
| `SessionItem` | 某一代工作 Session 的 SDK item 与原始记录关联 | 工作历史列表 |
| `Artifact` | 产物身份、类型、范围和当前版本指针 | 产物列表 |
| `ArtifactVersion` | 不可变内容、Schema、来源、依赖、生成运行与配置 | 版本预览与对比 |
| `ArtifactState` | 指定版本的确认范围、依赖有效性与质量检查状态 | 产物状态卡 |
| `Decision` | 关键创作决定、来源、状态、理由及版本关系 | 决策时间线 |
| `Confirmation` | 真实确认／撤回、目标版本、条目范围和来源消息 | 确认依据 |
| `Task` | 业务目标、父子任务、累计预算与暂停状态 | 任务树 |
| `Run` | 一次受控执行段、处理者、Session、输入与固定配置；runner 段调用 Runner，recovery 段仅执行程序恢复 | 执行时间线 |
| `QueuedRequest` | Steer／Queue 原消息、目标和实际采用结果 | 消息处理队列 |
| `Checkpoint` | 应用恢复位置、输入版本、已完成／未明操作与计数 | 暂停与恢复记录 |
| `ConfigVersion` | 配置草稿／发布版本或运行所用完整配置快照 | 配置版本对比 |
| `ContextSnapshot` | 每次调用实际发送的 instructions、input items、tools 与选材记录 | 上下文查看器 |
| `ModelCall` | 每次真实请求尝试、耗时、结果、错误与用量 | 模型调用明细 |
| `ToolCall` | 工具名称、参数、结果、重试与错误 | 工具执行明细 |
| `Dependency` | 消费者与上游固定版本、使用范围、当前有效性 | 依赖关系图 |
| `Blob` | 原作／附件／大 JSON 的内容地址、大小与哈希 | 文件与大内容详情 |
| `RuntimeEvent` | 已提交的状态变化、检查记录及进度事件 | 状态时间线 |
| `IdempotencyRecord` | 同一请求的处理结果和去重依据 | 关联操作详情 |

工作摘要作为 `artifact_kind=work_summary` 的纯文本 ArtifactVersion 保存，不绑定 Agent `output_type`；覆盖消息与来源由 `source_refs` 和 Session 记录维护。原作保存为 `artifact_kind=source_text`，正文版本中的 Blob 为权威原文。各阶段产物使用其 `schema_id` 作为 `artifact_kind`。

Step1 不生成 `batch_manifest`；根据阈值在全文和滑动窗口之间选择。全局事件 Agent 与主要人物事件 Agent 并行读取同一固定原作版本，各有独立 Run、Session、窗口游标与输入输出记录。全局 Agent 保存带原文索引的 `source_global_events` 和纯文本 `source_global_analysis`；人物 Agent 只保存无逐事件索引的 `source_character_events`。下游阶段要求三份有效产物及一致的原作来源；一条视图失败不抹去另一条已验证的进度。超预算或无法确认安全区间时不推进对应游标，详见[上下文执行方案](../context/上下文执行方案.md)。

其他阶段的分批计划使用 `artifact_kind=batch_manifest`、`output_schema=null`，内容按[上下文执行方案](../context/上下文执行方案.md)的 `context.batch_manifest.v1` 内部处理器校验；它是程序生成的版本化计划，不是新增模型输出类型。覆盖进度由批次子 Task 和 `context.batch_completed` 事件重建。身份索引、版本比较、依赖评估与确认映射的事件载荷及跨记录约束见[版本与确认映射](../context/版本与确认映射.md)，不增加核心记录类型。

## 3. 内容、版本与状态

`Content` 是三选一的联合类型：小段文本用 `{storage:"inline_text",text}`；结构化内容用 `{storage:"inline_json",value}`；大内容和附件用 `{storage:"blob",blob_id}`。文本按 UTF-8 保存，原作锚点仍使用主文档规定的 UTF-16 偏移；二者不混用。

内容哈希对实际保存的字节计算 SHA-256。JSON 保存时由服务端统一使用 UTF-8、对象键排序、无多余空白的确定性序列化；保存后不可重新排版再冒充原哈希。下载可返回保存的原字节，页面预览可格式化展示。Blob 元数据中的哈希与实际字节相符后才能标记 ready。ready 后内容字节、哈希及大小不可替换；修改内容新建 Blob。物理迁移可更新受控存储位置，但必须验证字节完全一致。

不可变内容与可变状态分开：

- HistoryRecord、ArtifactVersion、Decision 版本、Confirmation、Checkpoint、ContextSnapshot、RuntimeEvent 只追加；修改或撤回使用新版本／新记录。
- ArtifactState 保存状态投影；每次状态变化与对应事件在同一事务中提交。原始确认始终可追溯。
- ConfigVersion 的已保存 `values` 不原地改写；编辑另存版本。发布状态可以更新并留下事件，`run_snapshot` 创建后整体不可变。
- Run、Task、调用状态及队列等可变记录通过 `row_version` 并发检查修改，不能从 HTML JSON 面板任意 PATCH。
- SessionItem 是可重建工作数据。裁剪或压缩切换 `WorkSession.generation`；先保存归档、摘要和调用快照，再清理旧代工作 items。删除工作 items 不删除原始历史。Step1 的工作历史即使重建，下一次模型调用仍必须从固定原作版本加载本次要求的全文或窗口，不能沿用摘要代替。

`Artifact.current_effective_version` 仅指整个产物范围已生效的版本。部分确认保存在 Confirmation／ArtifactState.effective_selections 中，不能把未确认部分标为有效。已确认、依赖有效、质量通过是三个独立状态；任一个不能替代其余条件。

`Confirmation.basis=unchanged_scope` 仅用于保留已确认且未改变的范围，必须携带原确认 ID 和程序验证的范围映射记录；新增内容或语义变化不能继承确认。`action=revoke` 保存要撤回的确认 ID 和用户来源，不删除原确认。

## 4. 写入时的跨记录约束

JSON Schema 检查字段形状；下列数据库与服务约束同样属于内部数据写入契约，不增加首版 Graph 质量算法：

1. 所有项目内关联必须指向同项目已存在记录。账号、项目权限、服务端身份及全局配置读取权限在查询和写入前验证。
2. 版本按逻辑产物／决策连续递增，固定父版本和依赖版本；不存在的引用不得保存为成功结果。循环关联可在同一事务中用延迟约束创建，提交前必须完整。
3. ArtifactState 的 artifact_id／version 必须与 artifact_version_id 对应；Artifact 的版本指针必须属于自身。
4. Confirmation 必须来自本项目真实用户消息，目标版本和条目存在。用户沉默、工具读取和模型候选确认均不能签发确认。范围映射须逐条验证身份、语义和依赖。
5. Run 的 Session、Task、配置快照和所有输入引用一致；SessionItem.sequence 在 session_id＋generation 内唯一。当前 Session 与 Run 的租约及 fencing_token 在写入时再次检查，过期执行者不得覆盖新状态。
6. 同一逻辑请求重试复用 operation_id／幂等键。模型调用按真实 attempt 单独登记；未知状态先核实，不能把同一次已完成调用重复记账。ModelCall 只能属于 runner Run。ToolCall.model_call_id 必须指向同项目、同任务内实际发起该工具调用的 ModelCall；正常执行同 Run，跨 Run 仅允许[已验证的恢复链](storage-and-api.md)承接。ToolCall.run_id 记录实际执行段，保留原 ModelCall 身份，并检查当前执行段的租约、原工具请求、参数、固定输入与 operation_id。
7. `null` 用量或耗时表示未知。缓存输入 token 属于输入 token，推理 token 属于输出 token；逐次调用优先使用可核验账单，否则使用有定价版本的估算并展示估算标记，再去重汇总。父任务不能再次相加子任务汇总值，也不能在混合账单／估算时遗漏任一调用。存在无法确定或估算的费用、或无法完整计量的耗时，且相应限制启用时，先核对或暂停；不能以已知小计或零放行新调用。
8. 数据记录不保存 API 密钥、认证头或密码。上下文／工具内容按现有项目权限可见；平台凭证来自独立服务端秘密配置。UI 不展示服务端 storage_key、租约身份或幂等内部请求摘要等运维字段，可展示不含秘密的处理结果。
9. 原始模型输入和输出归档保持 SDK 协议关联；内部隐藏思维链不是数据采集或可视化要求。可用的响应文本、公开推理摘要和工具执行结果按实际返回值归档。

RuntimeEvent 的图检查结果绑定固定 `artifact_ref`，记录 `schema_contract` 和 `unreachable_nodes` 的状态与具体原因。Schema 问题使用 JSON Pointer；可达性问题定位章节、节点／边及原因。可选校验 Agent 不接收或签发这些脚本检查结果。

确认范围映射使用 `event_name=confirmation.scope_mapped`；payload 含 `from_subject`、`to_subject`、`entries` 和 `dependency_assessment_refs`。每条 entry 含原 Selection、新 Selection、`unchanged=true` 及核实依据引用；无法证明不变的条目不写入继承确认范围。对任意改写前后内容，仅哈希相等或路径相同不足以推断语义确认可继承。

`Budget` 中的 null 表示尚未配置，显式禁用的限制列于 `disabled_limits`。运行前必须解析所有启用项的具体限额，不能把缺少配置误解为无限或免费。预算调整作为有用户依据的事件保存，原始调用和检查点快照不改写。

`Checkpoint.cursor.handler_version=runtime.v1` 时，`cursor.state` 必须另外通过 [checkpoint-state.schema.json](../runtime/checkpoint-state.schema.json) 校验，再核对当前控制事件、额度账本和引用关系。外壳结构合法不代表可以恢复。Task／Run 的状态转移、关闭、轮次继承和队列规则以[运行契约](../runtime/运行状态机.md)为准；Run 已结束而 Task 等待确认是有效组合，无活动 Run 时 Task.current_run_id 为 null。

## 5. 结构验证与开发验收

[records.example.json](examples/records.example.json) 提供每种记录至少一个结构合法的样例，帮助开发和页面组件联调；样例为合成数据，不表示真实改编运行。样例的预算和配置内容仅供展示，不作为生产默认值或可直接执行的完整配置包。

运行 `python docs/harness-data/validate.py` 验证记录与恢复状态 Schema、示例、固定引用及若干非法输入；需要 `jsonschema`。该脚本不调用模型、不连接数据库、不实现生产写入校验器或完整恢复链验证器。

结构验证包括：所有记录通过 Schema；非法字段／类型、无版本的确认、无来源的工作 item 被拒绝；统一 EvidenceRef 与既有 output_type 一致。跨记录验收包括：确认能跳回准确用户消息和产物版本；同请求重复提交返回原结果；项目间关联被拒绝；工作历史压缩后仍可查看原始交互；用量未知时不显示为零。

内部数据 Schema 及其示例验证不等于 Harness 效果评测。正式数据库迁移、服务实现和页面组件按这些契约开发。
