# Harness 存储与接口契约

本文定义首版服务端的数据存储、写入约束及页面接口。记录的字段、类型、可空性和状态枚举以 [records.schema.json](records.schema.json) 为准；业务生效、运行控制与确认规则以 [分支 Agent 开发文档](../分支Agent开发文档.md) 为准。数据库和接口保存这些规则的执行结果，不另外引入人工审批步骤。

Task／Run 状态转移、控制事件优先级、队列 hold、服务恢复和跨 Run 轮次额度统一按 [运行状态机与调度契约](../runtime/运行状态机.md) 执行。`Checkpoint.cursor.handler_version=runtime.v1` 时，除完整 Checkpoint 记录校验外，还必须用 [checkpoint-state.schema.json](../runtime/checkpoint-state.schema.json) 二次校验 `cursor.state`；不能将任意 JSON 当作可恢复状态。

## 1. 存储边界与公共类型

结构化记录使用 PostgreSQL。完整记录保存为 `JSONB`；用于权限、外键、排序、唯一性和并发控制的字段同时投影为 SQL 列。大段原文、Graph、模型输入输出和导出文件经 `Blob` 引用保存到持久文件存储。

首版文件存储适配器使用服务端专用持久目录，部署时配置并挂载持久卷；禁止使用进程临时目录或浏览器缓存作为唯一副本。通过 `put / get / stat / delete_unreferenced` 四个存储操作隔离本地文件系统；以后替换对象存储不改变记录引用。数据库只保存不可变的存储键和校验信息；接口不返回实际文件路径或凭据。

| 公共类型 | 定义 |
| --- | --- |
| `Id` | Harness UUID 字符串；不得用 Nexo 节点／章节 ID 代替 |
| `AccountId` | 平台身份提供的不透明字符串；无需为 UUID |
| `Version` | 大于等于 1 的整数；映射既有 `EvidenceRef.version` 时使用十进制字符串 |
| `Timestamp` | 带时区的 RFC 3339 字符串，服务端存储统一使用 UTC |
| `Money` | `{amount:string,currency:string}`，严格使用 `$defs/Money`；金额为非负十进制字符串，运算使用 `Decimal`，SQL 使用 `NUMERIC`，禁止浮点金额或跨币种直接相加 |
| `PageCursor` | 不透明字符串；固定项目、过滤条件、排序和读取上界，客户端不能据其推断授权 |
| `Record<T>` | `records.schema.json` 中对应 `$defs/T` 的完整记录 |
| `RecordInput<T>` | `Record<T>` 去掉公共服务端字段及 4.1 指定字段后的严格对象，剩余字段的类型、必填项与可空性沿用记录 Schema |
| `Ref`、`Scope` | 分别为 `$defs/EvidenceRef`、`$defs/Scope`；引用产物／决策时 `record_id` 为逻辑 ID、`version` 为十进制版本字符串；普通不可变记录使用实际记录 ID、`version=null`。不得用“最新”代替历史固定版本 |

每个写入操作均使用可信 `AuthContext{account_id:AccountId, request_id:Id}`；后台 worker 还必须提供已授权的任务／运行身份。`project_id` 来自路由与已核验运行上下文，服务端检查账户对该项目的权限。请求正文中的 `account_id` 不作为授权来源。

当前测试版关闭费用门禁，`status.runtime_policy.cost_gates_enabled=false`；已知费用超额或已知终态缺费用不阻塞执行。历史预算与费用记录保留，恢复卡片不要求金额、未知费用接受或补录。下文费用限制与确认规则仅在费用门禁启用时执行。

计量缺失时使用 `null`，例如供应商未返回 token 数、暂未配置价格、调用结果未知；不得用 `0` 表示未知用量。费用逐个 `ModelCall` 采用已报告费用或有依据的估算，再按实际调用 ID 去重聚合；不能在父任务层二选一而漏算部分调用。已知部分与未知部分分别展示，不能把不完整费用标成完整总价。启用了限额而费用无法核验／估算或活动耗时无法完整计量时，先核对并暂停依赖该预算的新调用，不能仅凭已知小计放行。费用上限、耗时上限和追加额度使用已发布配置／明确继续记录，不在本存储契约中新增预算默认数值。

## 2. 数据库结构

### 2.1 共通列与投影

除下文专门列出的辅助表外，每张记录表具有：

| SQL 列 | 约束 |
| --- | --- |
| `id UUID` | 主键；对应记录 `id` |
| `project_id UUID` | 项目记录非空并引用 `projects.id`；`projects` 自身要求 `project_id = id`。仅全局 `ConfigVersion` 允许 `NULL`，其余记录不得借此绕过项目隔离 |
| `data JSONB` | 非空；必须是当前声明版本的完整记录，写入前通过对应 Schema 校验 |
| `created_at TIMESTAMPTZ` | 非空，服务端时间 |
| `updated_at TIMESTAMPTZ` | 仅可变记录具有，更新时由服务端设置 |
| `row_version BIGINT` | 仅可变记录具有，初值 1，每次状态更新递增 1 |

外键、过滤字段和状态列由服务端从 `data` 投影后同事务写入。对 ID、项目、版本、状态和外键投影增加数据库 `CHECK` 或写入触发器，拒绝投影与 `data` 不一致；不得由两套独立更新路径维护。Schema 校验属于服务端写入入口职责，数据库约束承担关系完整性与竞争保护。

项目记录表增加 `UNIQUE(project_id,id)`。项目内外键使用 `(project_id,关联id)` 引用目标 `(project_id,id)`，防止跨项目串联；外键默认 `ON DELETE RESTRICT`。全局配置只能经配置解析器引用：以 `config_version_id` 外键核验存在，再校验目标为同项目或明确的全局配置；不能放宽普通内容的跨项目外键。当前接口不提供硬删除历史功能，用户归档不删除记录。

内部辅助表 `record_index`：`id UUID PRIMARY KEY, project_id UUID NULL, record_type TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL`，并有 `UNIQUE(project_id,id,record_type)`、`UNIQUE(project_id,id)`；`project_id` 引用 `projects.id`。创建业务记录时在同一事务插入索引。多态引用解析为具体版本记录后，以实际目标 `id + record_type` 约束到该索引，应用同时验证原始逻辑 ID、版本和目标类型。全局配置的索引行允许空项目，只在配置关系中可引用。索引只登记元数据，不复制内容。

### 2.2 记录表与关系

下表的“关系”指需要独立投影并建立外键的引用；数组引用使用后述关联表，不能仅依赖 JSON 文本约定。

| 表 | 记录类型 | 关系与主要约束 |
| --- | --- | --- |
| `projects` | `Project` | 保存平台账号归属；账号字符串由认证服务校验，不建立到平台数据库的跨库外键 |
| `conversations` | `Conversation` | 属于项目；用户可见会话与内部工作 Session 独立 |
| `history_records` | `HistoryRecord` | 会话必须存在；工作 Session、任务、运行、调用引用按 Schema 可空；消息内容可引用 Blob；原始正文不可覆盖 |
| `work_sessions` | `WorkSession` | 所属会话／业务范围；`UNIQUE(project_id,session_key)`；关联当前摘要版本；同一 Session 只允许一个写入者 |
| `session_items` | `SessionItem` | 所属工作 Session、原始归档引用；裁剪只改变工作集，不删除归档 |
| `artifacts` | `Artifact` | 产物稳定身份和业务范围；不把当前有效内容直接覆盖存入本表 |
| `artifact_versions` | `ArtifactVersion` | 所属产物、生成运行、Schema／配置依据、Blob 或内联内容、基线版本；`UNIQUE(project_id,artifact_id,version)` |
| `artifact_states` | `ArtifactState` | `UNIQUE(project_id,artifact_version_id)`；对应具体产物版本的当前生效／依赖状态，核对 `artifact_id / version` 与目标版本一致；确认范围单独保存，不能只用一个布尔值代替 |
| `decisions` | `Decision` | `UNIQUE(project_id,decision_id,version)`；`decision_id` 是稳定逻辑身份，`id` 是本版记录；状态变化追加新版本，不原地改旧决定 |
| `confirmations` | `Confirmation` | 来源用户消息、目标产物版本／决定、范围及撤回／替代关系；保留历史记录 |
| `tasks` | `Task` | 入口消息、父任务、会话、目标范围和任务预算；父任务与子任务同项目 |
| `runs` | `Run` | 所属任务、工作 Session、固定配置、恢复检查点及 `execution_kind`；`runner` 至多执行一次 Runner 调用，`recovery` 仅执行程序恢复步骤；一次业务任务可有多个运行记录 |
| `queued_requests` | `QueuedRequest` | 原始消息、所属会话、目标／排队依据及被调度的任务；同一请求不能重复创建任务 |
| `checkpoints` | `Checkpoint` | 所属任务／运行、工作 Session、配置、产物和未完成调用引用；检查点不可覆盖修改 |
| `config_versions` | `ConfigVersion` | 配置范围及版本；保存模型、instructions、output_type 与上下文／执行配置的固定内容 |
| `context_snapshots` | `ContextSnapshot` | 所属任务／运行及配置，实际输入内容引用、加载／省略材料清单和 token 估算；模型调用通过 `context_snapshot_id` 引用此记录 |
| `model_calls` | `ModelCall` | 所属任务／运行、实际上下文快照、供应商响应引用、重试关系与可空计量 |
| `tool_calls` | `ToolCall` | 所属任务／实际执行运行、`model_call_id`、`operation_id`、参数与结果；发起模型调用必须属于同一项目和任务，正常执行同 Run，恢复时允许下文规定的跨 Run 关联；工具调用 ID 在实际执行运行内唯一 |
| `dependencies` | `Dependency` | 使用方与被依赖的固定记录／版本、字段范围及有效性；版本变更不改写历史依赖 |
| `blobs` | `Blob` | 项目、受控存储键、媒体类型、长度、内容 SHA-256、状态；内容写入后不可覆盖 |
| `runtime_events` | `RuntimeEvent` | 任务／运行／消息／记录引用、事件类别与事实摘要；只发布已提交事实 |
| `idempotency_records` | `IdempotencyRecord` | 账号、项目、操作名、幂等键、规范化请求摘要及完整结果引用；保存成功或可重放失败结果 |

关联数组统一投影到内部表 `record_links`：`project_id UUID, from_id UUID, relation TEXT, ordinal INTEGER, to_id UUID, to_type TEXT, target_version BIGINT NULL, selector JSONB NULL`；主键 `(project_id,from_id,relation,ordinal)`；来源及目标分别引用 `record_index`，目标类型随目标外键一并核验。`target_version` 仅供业务版本引用，不等同于可变记录的 `row_version`。产物 EvidenceRef 按 `(artifact_id,version)` 解析为 `ArtifactVersion.id`；决策按 `(decision_id,version)` 解析为 `Decision.id`，再写 `to_id` 外键。原始逻辑引用仍保存在记录 JSON 中，不能被解析结果改写。

`record_links` 是完整记录引用的事务投影，不是另一份可独立修改的业务事实。`dependencies` 承担业务依赖的语义与影响记录，普通“消息属于任务”等关联不冒充依赖。外键可设为 `DEFERRABLE INITIALLY DEFERRED`，支持同一事务内同时创建任务、运行、检查点和来源关系；提交前必须全部满足。

跨 Run 恢复工具时，`ToolCall.run_id` 指向实际执行该工具的新 Run，`model_call_id` 保留真实发起请求的原 ModelCall，不复制模型调用记录。写入必须证明二者同项目、同 Task，源 Run 已关闭，且新 Run 可通过持久 Checkpoint 恢复链追溯至源调用；同时核对原工具请求 ID、工具名、参数、读取的固定版本与 `operation_id`。发起模型所属 Run 与执行工具所属 Run 的不同是可审计恢复事实，不能免除当前新 Run 和 WorkSession 的租约／fencing 校验，也不能借此重发结果未明的工具尝试。

逻辑身份使用 `logical_objects(id UUID PRIMARY KEY, project_id UUID NOT NULL, object_type TEXT NOT NULL)`，`object_type` 为 `artifact` 或 `decision`，并有 `UNIQUE(project_id,id,object_type)`。Artifact 的逻辑 ID 与自身记录 ID 相同；Decision 的逻辑 ID 独立于各版本记录 ID。分配逻辑身份和首个版本时同事务登记，外键分别核对类型；引用解析器先定位逻辑对象，再按版本唯一约束找到实际版本记录，缺少具体版本时不自动取最新。

配置继承关系使用 `config_links(consumer_project_id UUID NULL,consumer_id UUID,config_version_id UUID,relation TEXT,ordinal INTEGER)`，主键 `(consumer_id,relation,ordinal)`，来源引用 `record_index.id`、目标引用 `config_versions.id`；约束触发器要求目标属于同项目或为合法全局配置，并要求全局配置只能引用全局上游。这是全局配置特许关系，普通记录仍走项目复合外键。

配置发布指针单独保存为 `config_scopes(scope_id UUID PRIMARY KEY,project_id UUID NULL,config_key TEXT,scope_kind TEXT,scope_key TEXT NULL,latest_version BIGINT,published_config_id UUID NULL,row_version BIGINT)`；全局范围仅允许 `project_id=NULL AND scope_kind='global'`，其他范围要求项目 ID 非空。唯一键使用 `COALESCE(project_id,'00000000-0000-0000-0000-000000000000'::uuid),config_key,scope_kind,COALESCE(scope_key,'')`，禁止真实业务 UUID 使用该保留值。`published_config_id` 引用相同范围的已发布配置；保存草稿和发布时锁定本行并递增 `row_version`。配置版本表按同一范围与 `version` 唯一，运行快照不参与发布指针竞争。

运行调度还使用状态机第 7 节定义的 `conversation_controls / task_runtime_controls / turn_allowances / turn_reservations / work_dispatches` 辅助投影；其主键、锁顺序及事件来源按该节执行。恢复配置、租约／心跳默认值和停止宽限按状态机第 8 节固定到运行快照，不在本文件重复定义另一套数值。

### 2.3 唯一约束与查询索引

| 对象 | 必要索引／约束 |
| --- | --- |
| 项目列表 | `projects(owner_account_id,updated_at DESC,id)`；首版按平台已有权限检查项目归属 |
| 会话列表 | `conversations(project_id,updated_at DESC,id)` |
| 历史与事件 | `HistoryRecord.sequence` 在每个会话内递增，`UNIQUE(project_id,conversation_id,sequence)` 并更新 `Conversation.last_message_seq`；只有 `RuntimeEvent.sequence` 按项目递增且 `UNIQUE(project_id,sequence)`，事件流按项目序号续读 |
| 工作历史 | `UNIQUE(project_id,session_id,generation,sequence)`；查询只包含当前工作集，归档查询不受该过滤影响 |
| 产物 | `(project_id,artifact_kind,stage)` 与章节范围 GIN 索引 定位；类型与范围可有多个候选，不将此索引误设为唯一而禁止候选方案 |
| 产物版本 | 唯一版本号；`(project_id,artifact_id,version DESC)`；生效状态独立关联到版本 |
| 确认／决定 | 决策 `(project_id,decision_id,version)` 唯一；确认按目标固定引用、范围、来源消息建立索引；重复确认由幂等键去重，不用正文字符串去重 |
| 任务／运行 | `(project_id,state,updated_at,id)`；任务父子关系索引；同一工作 Session 的运行写入由租约和串行锁保证 |
| 队列 | `(project_id,conversation_id,state,sequence)`；调度后绑定任务 ID 的唯一约束，避免同一队列项重复执行 |
| 配置 | 同一范围的版本号唯一；发布状态与当前引用使用乐观锁，不覆盖已固定给运行的配置 |
| 依赖 | 解析后的 producer 版本引用与 consumer 版本引用索引，用于反向影响查询 |
| 调用 | `(project_id,run_id,created_at,id)`；`ModelCall` 的 `(project_id,operation_id,attempt)` 唯一；`ToolCall` 的 `(project_id,operation_id,attempt)` 唯一，供应商工具 ID 与发起模型尝试关联；供应商响应 ID 在对应供应商与账户配置范围内检索，不假定全球唯一 |
| Blob | 存储键唯一；`(project_id,sha256,byte_size)` 用于完整性检查和项目内去重，禁止跨账号响应“已存在”泄露内容 |
| 幂等 | `UNIQUE(project_id,account_id,operation,key)` |
| 泛型引用 | `record_links(project_id,to_id,relation,from_id)` |

单调序号由内部 `record_sequences(project_id,stream_key,next_value BIGINT)` 行锁分配，`PRIMARY KEY(project_id,stream_key)`。`stream_key` 为 `history:<conversation_id>`、`runtime_events`、`session:<id>:<generation>` 或 `queue:<conversation_id>`；分配与记录写入同事务完成，允许出现序号空洞，不复用旧序号。

首版搜索保存可展示的 `search_text` 投影，覆盖消息文本、产物标题／摘要、决定、确认摘要及执行事件。使用字段过滤、PostgreSQL `simple` 全文索引和 `pg_trgm` GIN 子串索引；中文短关键词以子串匹配补充，空查询只做筛选分页。原始大内容通过 Blob／固定版本读取，不把全部 Graph 文本重复塞入每次搜索结果。

## 3. 写入一致性

### 3.1 不可变内容与可变状态

历史正文、产物版本内容、检查点、配置已发布内容和上下文快照一经持久化不可改写。修正生成新版本／新记录并引用旧记录；同一记录的允许状态更新仅作用于 Schema 明确的可变字段。服务器不提供任意表、任意 JSON Patch 的写入接口。

可变记录更新要求 `expected_row_version:integer`；SQL 使用 `WHERE id = :id AND row_version = :expected`，成功后递增，未命中返回 `version_conflict`。产物新版本还要检查 `expected_latest_version`；保存后继续作为草稿，不因版本号最大而替换有效结果。

### 3.2 必须同事务完成的操作

| 操作 | 事务内必须一起完成 |
| --- | --- |
| 接收用户消息 | 幂等占位、历史记录、用户选择的控制方式、任务／队列／待纳入记录及事件；竞争时保存消息并明确记录尚未应用，不误投其他运行 |
| 保存产物 | 锁定产物版本计数、分配版本、写内容引用／来源／依赖、初始化草稿状态、登记事件与幂等结果 |
| 确认生效 | 锁定目标版本及状态，核对来源消息与明确范围，写确认记录、更新对应范围生效状态与必要的依赖复核标识，再写事件 |
| 保存决定 | 写入决定／替代关系、来源和影响范围；若需确认，则与确认命令在同一外层事务提交，不能先把建议写成有效决定 |
| 更新摘要 | 保存纯文本摘要及程序确定的覆盖来源、保存内部 `work_summary` 产物版本、锁定 Session 并切换摘要引用／工作集、保存检查点与事件 |
| 调度队列 | 锁定队列项与目标 Session，创建任务／运行、记录固定输入与配置、绑定队列项和事件；必须保证一次调度只有一个执行者 |
| 保存检查点 | 已完成写入引用、待处理控制消息、未完成调用状态、累计计量／计数和恢复位置一起提交 |
| 扣减／归集计量 | 原始调用计量、所属任务累计与父任务聚合标识同事务写入，同一次调用只计一次；重算结果不能被再次累计 |

数据库事务不得跨越模型或外部工具网络请求。调用前先登记持久调用记录和执行身份，提交后发起请求，返回后另一个事务保存实际结果。连接中断不能直接推断外部操作失败；保留 `unknown` 或 Schema 对应的不确定状态，恢复时先核对。API 成功响应只在事务提交后返回。

### 3.3 租约、幂等与迟到结果

执行器通过 `Run`／`WorkSession` 中的 `lease_owner / lease_expires_at / fencing_token` 占有执行及 Session 写入资格，SQL 表投影这些字段。每次申请和接管同时锁定相应运行及 Session，心跳延长租约，接管时递增 fencing token；所有运行写入核对两者当前令牌，不能只靠进程内锁。过期执行者不得继续推进、确认或覆盖状态，迟到结果只能按原调用及原依赖保存历史／需复核草稿。

所有具有写入效果的客户端命令要求 `Idempotency-Key`，内部服务同样传入幂等键。相同项目、账号、操作和键：

- 请求摘要相同且已完成：返回原始结果，不再新建消息、版本或任务。
- 请求摘要相同且进行中：返回 `operation_in_progress` 与操作引用；客户端查询原操作，不换键重试。
- 请求摘要不同：返回 `idempotency_conflict`，不得覆盖原操作。

规范化请求摘要包含业务参数、目标、固定版本、文件内容 hash 和相关条件版本；不包含可变的 HTTP 追踪时间。幂等检查通过唯一约束和行锁完成，不能使用“先查询不存在，再无约束插入”。同一操作的网络重试复用原键；用户明确发起新的修改才使用新键。

创建项目是尚无 `project_id` 的唯一入口例外，使用内部辅助表 `project_creation_requests(account_id TEXT,key TEXT,request_sha256 TEXT,project_id UUID)`，主键 `(account_id,key)`，`project_id` 外键指向 `projects.id` 并允许事务末延迟检查。服务端在一个事务中占有账号级键、分配稳定项目 ID、创建 Project 和项目内 IdempotencyRecord，再写入请求映射；唯一键冲突时等待原事务结果。同账号同键同正文返回同一项目，不重复创建；同键不同正文返回 `idempotency_conflict`。该辅助表不属于普通业务记录，不放宽其他记录的 `project_id` 非空约束。

运行重试计数、修复计数、耗时与费用记录不因重启、续跑或新租约清零。模型服务未提供幂等或完成查询能力时，系统不承诺外部请求恰好一次；它保证已落库业务写入不重复，未明调用保持可见并按恢复规则处理。

### 3.4 文件提交

上传／生成文件先写入持久目录的暂存区域，完成后计算 SHA-256、字节数并校验媒体类型，再原子发布到不可变存储键；确认内容可读后，数据库事务写入 Blob 与使用方引用。失败时不提交可用 Blob 引用。Blob 为 `ready` 后，字节内容、hash 与长度不能替换；新内容产生新 Blob ID。物理存储迁移仅允许字节同一且 hash 验证通过，不能改变历史内容身份。对象存储适配器必须提供相同“内容已可读”的提交条件。

文件存储与 PostgreSQL 不具备跨系统事务：发布成功、数据库提交失败会留下无引用对象，后台仅删除已确认没有数据库引用、没有进行中操作持有且超过保留窗口的对象。删除程序不能据文件年龄清理正在使用或已归档的唯一副本。备份必须覆盖数据库、文件内容和存储键映射，并支持恢复后的 hash 核验。

## 4. 内部服务接口

### 4.1 公共请求与返回

所有内部命令共用 `project_id:Id`、可信 `AuthContext`、`idempotency_key:string`（1～128 字符）；以 worker 身份写入运行结果时还提供 `run_id:Id` 和当前租约令牌。下表仅列命令自身参数，未标 `?` 的参数必填，`?` 表示可省略；显式 `null` 是否允许由对应记录 Schema 决定。

成功统一为 `{status:"ok", operation_id:Id, data:object, event_ids:Id[]}`。失败统一为 `{status:"error", operation_id:Id|null, error:{code:string,message:string,retryable:boolean,details:object}}`；`details` 仅返回调用者有权读取的信息，校验失败使用字段路径与错误原因，不返回密钥或服务器路径。消息已保存但控制未生效时，失败详情必须包含 `message_id` 和真实处理状态。

记录输入先校验，再由服务端分配记录身份、版本、时间、序号、当前状态和 `row_version`。业务命令输入不是面向模型的 `output_type`，也不能直接信任模型填入的用户确认状态。

`RecordInput<T>` 首先移除所有公共服务端字段 `record_type / schema_version / id / project_id / created_at / updated_at / row_version`，再移除以下各类型字段；不允许提交被移除字段：

| T | 额外移除、由命令生成的字段 |
| --- | --- |
| `HistoryRecord` | `sequence / operation_id` |
| `ArtifactVersion` | `artifact_id / version / content_sha256 / dependency_ids` |
| `Decision` | `decision_id / version` |
| `Confirmation` | 无额外移除；来源与范围由程序核验，不仅依赖结构合法 |
| `Dependency` | `consumer_ref / state / assessment_ref`，由保存命令绑定新版本；创建为 `review_required`，校验后更新真实状态 |
| `QueuedRequest` | `conversation_id / source_message_id / sequence / state / adopted_run_id / adopted_context_snapshot_id / created_task_id / resolved_at / blocked_reason` |
| `ConfigVersion` | `version / state / sha256 / published_at / resolved_from_ids`；`run_snapshot` 只允许内部解析器创建 |

其中 `parent_version`、`producer_run_id`、`config_version_id` 等仍按记录 Schema 要求传入具体值或显式 `null`；外层参数和记录中同义目标不得冲突。内部写入完成后返回完整 `Record<T>`，包括分配结果。

`BudgetAllowance` 为下表定义的严格对象；所有字段均必填，不接受额外字段，且至少一个可空字段非空或 `retry_limits` 非空：

| 字段 | 类型与含义 |
| --- | --- |
| `max_turns` | `PositiveInteger\|null`，用户明确继续触顶任务时授予下一段执行的新轮次额度；保留旧额度、使用量及授权事件 |
| `max_active_seconds` | `PositiveInteger\|null`，原任务累计活动耗时的新总限额 |
| `max_cost` | `Money\|null`，原任务累计费用的新总限额，币种必须一致 |
| `retry_limits` | `{operation_id:Id,max_retries:NonNegativeInteger}[]`，指定原操作累计重试的新总限额；操作 ID 不重复且必须属于该任务允许控制的范围 |
| `max_repair_rounds` | `NonNegativeInteger\|null`，原任务累计修复的新总限额 |

`PositiveInteger` 为大于等于 1 的整数，`NonNegativeInteger` 为大于等于 0 的整数。空值／空列表表示本次不调整该项，不能表示关闭原上限。费用、耗时、重试和修复的新总限额必须高于对应已消耗值，并足够覆盖继续执行；`max_retries` 不含首次尝试，与原有重试计数口径一致。用户指定追加量时由程序换算成新总限额，事件记录原限额、追加量、新限额和真实来源；历史计数不清零。服务故障自动接管不接受这类用户额度替代，不得把新 Run 当成获得全额轮次；它继续使用原额度余额。

### 4.2 归档、产物与确认

| 接口 | 参数 | 成功 `data` | 核心拒绝条件 |
| --- | --- | --- | --- |
| `append_history` | `entry:RecordInput<HistoryRecord>`，`source_operation_id:Id\|null` | `{record:Record<HistoryRecord>}` | 关联记录不属于项目、调用身份不符、内容引用不可用；服务端分配序号，调用／结果记录必须关联真实源操作 |
| `save_artifact_version` | `artifact_id:Id`，`expected_latest_version:Version\|null`，`version:RecordInput<ArtifactVersion>`，`dependencies:RecordInput<Dependency>[]` | `{version:Record<ArtifactVersion>,state:Record<ArtifactState>}` | 内容 Schema 不通过、旧版本已变化、依赖引用不匹配；`null` 只允许该产物尚无版本 |
| `record_decision` | `decision_id:Id\|null`，`expected_latest_version:Version\|null`，`decision:RecordInput<Decision>` | `{decision:Record<Decision>}` | 首次创建两字段为 `null`；后续修改指定逻辑 ID 与旧版本，来源缺失、竞争、跨项目／循环替代或把建议直接声明已确认时拒绝 |
| `record_confirmation` | `confirmation:RecordInput<Confirmation>`，`expected_target_row_versions:{record_id:Id,row_version:integer}[]`，`expected_decision_latest_version:Version\|null` | `{confirmation:Record<Confirmation>,updated_states:Record<ArtifactState>[],decision_versions:Record<Decision>[]}` | 用户出处与明确对象／范围不符、目标版本不存在、指代含糊、依赖／状态竞争 |
| `commit_dependency_assessment` | `comparison_refs:Ref[]`，`assessments:DependencyAssessment[]`，`expected_dependency_row_versions:{record_id:Id,row_version:integer}[]`，`expected_artifact_state_row_versions:{record_id:Id,row_version:integer}[]` | `{event:Record<RuntimeEvent>,dependencies:Record<Dependency>[],artifact_states:Record<ArtifactState>[]}` | 范围比较或语义保护依据缺失、候选覆盖不全、跨项目关联、旧条件版本、未核验的模型结论直接要求 valid |
| `save_work_summary` | `work_session_id:Id`，`expected_session_row_version:integer`，`artifact_id:Id`，`expected_latest_version:Version\|null`，`summary:string`，`source_refs:Ref[]`，`covered_session_item_ids:Id[]` | `{summary_version:Record<ArtifactVersion>,session:Record<WorkSession>,checkpoint:Record<Checkpoint>}` | 来源未完整持久化、工具交互不成对、摘要为空、Session 仍有冲突写入者 |

`save_artifact_version` 不负责把草稿自动生效；生效必须走已定义的确认／阶段自动通过规则。`record_confirmation` 的范围以明确来源为准，不能用输入中的 `"all"` 扩大一句局部认可。目标记录状态使用条件版本检查，目标产物内容使用固定产物版本，两者不同。

`DependencyAssessment` 及比较、身份索引、映射事件的严格载荷按[版本与确认映射](../context/版本与确认映射.md)第 6 节。comparison_refs 和 assessments 非空，条件版本必须覆盖全部实际更新记录；命令复用公共鉴权、幂等和租约约束，同事务更新评估事件、Dependency、ArtifactState 与受影响任务控制。确认继承继续调用 record_confirmation，映射事件和状态在同一事务提交；不增加 Agent 工具或任意事件写入接口。

程序生成的 batch_manifest 通过 save_artifact_version 保存，output_schema 为 null 但必须通过[批次内容处理器](../context/上下文执行方案.md)校验；不能因没有模型 Schema 而跳过验证。context.batch_completed 由可信批次执行器在保存结果的同一事务提交，任务进度和覆盖以持久引用为准。

`append_history.source_operation_id` 写入 `HistoryRecord.operation_id`，表示被归档的真实模型／工具操作；模型或工具的调用、结果与错误必须提供相应 `ModelCall.operation_id` 或 `ToolCall.operation_id`，并核验同项目、同任务。`HistoryRecord.run_id` 关联被归档源调用所属的 Run：工具记录使用实际执行工具的 Run，模型记录保留实际发起模型请求的 Run。可信恢复流程可用当前恢复 Run 身份归档旧 Run 的真实返回，但须验证检查点恢复链及当前租约，不能改写源调用归属。普通对话没有源操作时为 `null`。本次归档命令的幂等操作 ID 只用于命令回执／幂等表，不能冒充被归档操作；同一真实操作的调用和结果共享源操作 ID，但各次归档写入具有独立幂等键。

`expected_target_row_versions` 仅列出会被修改的 Artifact／ArtifactState 等可变记录；Decision 为不可变版本记录，不能要求其具有 `row_version`。确认 Decision 时，`expected_decision_latest_version` 必须提供；服务端锁定对应 `logical_objects` 行，读取该逻辑决定的实际最大版本并与条件值比较，再校验 `confirmation.subject` 指向的具体候选内容版本及真实用户意图，最后在同事务分配新版本。所有 Decision 新增版本写入均须持有同一逻辑行锁。用户明确选择历史候选可作为新版本内容依据，但不能跳过最新版本竞争检查；目标为产物时本字段为 `null`。

目标产物身份由内部 `create_artifact(artifact_kind:string,scope:Scope)` 预先创建，返回 `{artifact:Record<Artifact>}`；该写入同样受公共鉴权与幂等规则约束，不能由模型直接调用。决定的确认状态变化由 `record_confirmation` 在同事务调用 `record_decision` 追加新决定版本，旧决定记录不修改；返回字段为 `decision_versions`，不是就地更新的旧记录。命令输入的 subject 定位用户实际讨论的候选版本，核对后生成状态改变而业务选择保持一致的新版本，返回的 Confirmation.subject 指向该生效版本；两版通过 supersedes_ref 与事件关联，不能顺便修改用户没有确认的选择。明确否定候选同样必须关联真实用户原话，模型不能把自身不推荐等同于用户拒绝。确认撤回时追加撤回记录，并保留其原 subject 以准确撤回旧确认。

`save_work_summary` 将纯文本摘要保存为内部 `kind=work_summary` 的产物版本，`output_schema=null`；覆盖范围由 `source_refs` 和 SessionItem 记录维护。只有当前 Session 的摘要与工作集引用切换；原始 `HistoryRecord`、旧摘要和旧检查点仍可读。

### 4.3 Graph 候选组装与交付

`assemble_nexo_graph` 先创建供 Step11 审核的固定候选，参数：

| 参数 | 类型与含义 |
| --- | --- |
| `target_artifact_id` | `Id`，最终 Graph 产物稳定身份 |
| `expected_latest_version` | `Version\|null`，用于保存新组装版本的并发控制 |
| `project_baseline_ref` | `Ref`，冻结项目基线，不在组装中追随最新版本 |
| `chapter_version_refs` | `Ref[]`，当前交付范围内各章节的固定 `chapter_graph` 版本；顺序来自有效章节规划 |
| `config_version_id` | `Id`，绑定 Nexo Graph Schema、映射和程序检查的固定配置 |

返回 `{version:Record<ArtifactVersion>,state:Record<ArtifactState>,blob:Record<Blob>,check_results:object[]}`。程序按既定章节规则合并，检查真实确认范围、依赖有效性、显式删除授权及两项首版程序检查；通过后保存候选版本与准确输入清单，暂不交付，不要求预先存在审核报告。候选可由 Step11 按 reviewed_artifact_refs 的固定引用读取；“未审核候选”保持真实状态，不冒充有效交付稿。未通过时记录具体错误并保留原有进度，不提交成功候选。

审核通过后由程序调用 `deliver_nexo_graph`：

| 参数 | 类型与含义 |
| --- | --- |
| `candidate_version_ref` | `Ref`，实际被审核的同一 nexo_graph 候选版本 |
| `review_version_ref` | `Ref`，固定审核报告，完整覆盖该候选及其所用章节版本且无阻断问题 |
| `expected_target_row_versions` | `{record_id:Id,row_version:positive_integer}[]`，覆盖本次将更新的 Artifact／ArtifactState 等可变记录 |

返回 `{version:Record<ArtifactVersion>,state:Record<ArtifactState>,blob:Record<Blob>,delivery_event:Record<RuntimeEvent>}`。命令复用公共鉴权、租约、幂等与事务约束；锁内重检候选 hash、审核覆盖、程序检查、依赖、真实确认及当前停止／预算控制，再登记生效和交付。version 与 blob 必须就是候选所用记录，不重新生成、合并或改写正文；失败返回实际阻塞，不标交付。候选或其依据发生变化时另存新候选并重新审核；不能沿用旧报告批准新内容。通过后自动交付，无额外全剧确认。

Blob 的实际下载内容为直接 `Project` 根对象，不含 Harness 信封、版本、审核或运行元数据。元数据留在记录接口。这两个接口均不调用 Nexo 服务端导入／发布 API，也不注册为 Agent 工具。

### 4.4 运行控制

| 接口 | 参数 | 成功 `data` | 竞争与边界 |
| --- | --- | --- | --- |
| `steer_run` | `run_id:Id`，`message_id:Id`，`expected_run_row_version:integer`，`scope:Scope` | `{run:Record<Run>,request:Record<QueuedRequest>,message_id:Id,application_status:"pending_boundary"}` | 保存 `mode=steer` 的待纳入记录；请求已保存不等于已纳入；在后续边界更新实际纳入的调用引用 |
| `enqueue_request` | `conversation_id:Id`，`message_id:Id`，`request:RecordInput<QueuedRequest>` | `{queued_request:Record<QueuedRequest>}` | Queue 接收时不修改当前运行输入／确认；实际调度后另存任务与新固定输入 |
| `interrupt_task` | `task_id:Id`，`expected_task_row_version:integer`，`source_message_id:Id\|null` | `{task:Record<Task>,affected_run_ids:Id[],interrupt_status:"requested"\|"stopped"\|"already_stopped"\|"already_finished"}` | 停止目标 Task 子树，先保存停止意图与队列 hold，再停止后续调度；等待用户且无活动 Run 时同样可停止 |
| `interrupt_run` | `run_id:Id`，`source_message_id:Id\|null`，`expected_run_row_version:integer` | `{run:Record<Run>,task:Record<Task>,affected_run_ids:Id[],interrupt_status:"requested"\|"stopped"\|"already_stopped"\|"already_finished"}` | 在锁内解析所属 Task 并复用 `interrupt_task`；Run 已结束不代表业务 Task 已完成，按实际 Task 状态处理 |
| `resume_task` | `task_id:Id`，`expected_task_row_version:integer`，`source_message_id:Id\|null`，`allowance:BudgetAllowance\|null` | `{task:Record<Task>,run:Record<Run>\|null}` | 显式用户继续操作；`allowance` 使用前述已解析新限额，未明确额度时不解除预算暂停；普通消息／配置发布不调用本命令 |
| `resume_queue` | `conversation_id:Id`，`expected_gate_row_version:integer`，`source_message_id:Id\|null` | `{conversation_id:Id,gate_row_version:integer,held:boolean}` | 用户明确继续排队消息时，条件更新会话 gate；不恢复停止／超限 Task，不清零用量，不跳过请求依赖 |

运行已结束时，Steer 返回 `target_not_running` 和已保存 `message_id`，由页面显示真实状态及后续处理入口，不自动改投其他任务。Queue 消息仍按原消息身份与固定用户意图保存；未来轮到它时才执行版本重检及业务确认解析，不能在接收阶段用排队表态改写当前任务。

停止的默认目标为页面所指的业务根 Task 及其子任务；用户明确指定章节／子任务时只作用于该子树。`interrupt_task` 以传入的明确目标 Task 为根，不擅自扩大到其父级。停止按钮可以没有对应对话消息，但必须保留真实账号操作事件，不能伪造用户文本；队列继续按钮遵守相同来源记录方式。“已请求停止”与“已停止”分开返回，远端请求未明时仍单独展示在途状态。

恢复命令默认续用旧配置快照；用户明确追加的轮次、重试、修复或预算额度保存为调整事件并固定到新检查点，历史用量不清零。Run 的等待／暂停／中断为本次执行段关闭状态，续跑创建新 Run；服务恢复继续使用旧执行链余额。若需要更换配置，按状态机记录明确的迁移依据并创建新运行，不能借恢复自动采用刚发布的配置。`resume_queue` 仅解除明确范围的队列 hold；普通消息、页面重连或发布配置不调用该命令。

`execution_kind=recovery` 的 Run 仍须取得 Run 与 WorkSession 的租约，设置 `max_turns=0`、`model_turns_used=0`，只核对已持久结果、完成获准的待执行工具／产物提交及恢复检查，不调用模型或 SDK Runner。若之后仍需模型，先保存检查点并关闭 recovery Run，再创建 `execution_kind=runner` 的 Run；后者 `max_turns>=1`，至多调用一次 Runner，并继承原执行链剩余轮次。余额为零时只能完成不需要新模型的程序工作或转为暂停，不能以 `max_turns=0` 调用 SDK，也不能创建新 runner Run 绕过限制。

## 5. HTML 页面接口

### 5.1 公共协议

基路径为 `/api/branch-agent/v1`。平台会话／令牌由服务端认证中间件解析；每个路由包括 SSE、上传、下载都执行项目权限校验。使用 cookie 认证时，对写入请求执行同源／CSRF 校验。REST 接口成功返回内部公共响应的 `data`；列表统一返回 `{items:object[],next_cursor:PageCursor|null,has_more:boolean}`。

列表 `limit` 为整数，默认 50、范围 1～100；`cursor` 可省略，禁止与不同过滤条件混用。过滤条件不接受任意 SQL、表名或 JSONPath。首版使用固定字段、JSON Pointer 和显式关联类型，不允许客户端指定服务端文件路径。

### 5.2 项目、会话与记录读取

| 方法与路径 | 参数／请求 | 返回与用途 |
| --- | --- | --- |
| `GET /projects` | `limit?,cursor?` | 当前账号有权访问的 `Project` 列表 |
| `POST /projects` | `{title:string}`，`Idempotency-Key` | 新项目记录；账号归属由认证注入 |
| `GET /projects/{project_id}` | 项目 ID | 项目概况、活动任务与待处理事项引用 |
| `GET /projects/{project_id}/conversations` | `limit?,cursor?` | `Conversation` 列表 |
| `POST /projects/{project_id}/conversations` | `{title:string}`，`Idempotency-Key` | 新会话记录 |
| `GET /projects/{project_id}/conversations/{conversation_id}/history` | `limit?,cursor?` | 用户可见历史及产物／运行卡片引用；内部执行记录通过数据区展开 |
| `GET /projects/{project_id}/records` | `record_type:string` 必填；`stage?:integer`、`chapter_id?:string`、`session_id?:Id`、`task_id?:Id`、`run_id?:Id`、`agent_key?:string`、`state?:string`、`created_from?:Timestamp`、`created_to?:Timestamp`、`limit?,cursor?` | 对应类型的分页记录；不适用的过滤字段返回 `invalid_request`，Agent 筛选通过实际关联运行解析 |
| `GET /projects/{project_id}/records/{record_id}` | `record_id:Id`，仅接受物理记录 UUID，不接受 `version` | 返回该记录自身；Artifact ID 对应产物索引元数据，不自动返回任何内容版本，不自动内联所有大内容 |
| `POST /projects/{project_id}/references/resolve` | `{ref:Ref}`；只读，不要求幂等键 | `{ref:Ref,resolved_record_id:Id,record:object}`；按逻辑产物／决策 ID 与固定版本解析，普通记录按物理 ID 解析；无匹配不回退最新版 |
| `GET /projects/{project_id}/records/{record_id}/relations` | `direction:"in"\|"out"`、`relation?:string`、`limit?,cursor?` | 来源／依赖／被依赖对象分页，用于关系可视化；该接口每页最多 50 个节点，只返回一层 |
| `GET /projects/{project_id}/artifacts/{artifact_id}/versions` | `limit?,cursor?` | 全部版本，区分最新草稿、有效范围及依赖状态 |
| `GET /projects/{project_id}/artifacts/{artifact_id}/diff` | `from_version:Version,to_version:Version` | `{from_ref,to_ref,changes:[{path:string,kind:"add"\|"remove"\|"replace",before:unknown,after:unknown}]}`；大值返回固定内容引用，不改变确认 |
| `GET /projects/{project_id}/search` | `q:string`，记录类型／阶段／章节／时间范围过滤及 `limit?,cursor?` | 片段、实际固定引用及匹配原因；沿用主文档搜索状态与默认包含历史状态规则 |

记录详情先展示规范化字段与来源；原始 JSON 通过相同授权接口读取。数据库租约、幂等记录可展示操作状态及关联 ID，隐藏内部 holder 标识、请求凭据和错误堆栈中的秘密。所有展示数据都从实际持久记录派生，不能让模型虚构当前进度。

页面从确认、历史、来源或依赖跳转时，先将完整 EvidenceRef 交给 `references/resolve`，再按返回的物理 ID 打开详情；直接浏览 Artifact 元数据时使用 `records/{artifact_id}`。配置引用通过固定 `ConfigVersion.id`、`version=null` 解析。所有解析先核验项目权限；仅对当前项目获准继承的全局配置提供读取特许，不对普通内容放宽项目隔离。

关系查询只返回当前项目内对象和该项目有权读取的全局配置上游。以共享全局配置为中心反向查询时，必须先按当前项目裁剪，再计算分页、计数和节点元数据；不能泄露其他项目的引用、ID、数量或存在性。全局配置详情由下述配置专用端点读取，普通 `records/{id}` 保持项目内记录语义。

### 5.3 消息与控制

`POST /projects/{project_id}/conversations/{conversation_id}/messages` 请求：

| 字段 | 类型与规则 |
| --- | --- |
| `text` | `string`；`text` 与 `attachment_ids` 不能同时为空 |
| `attachment_ids` | `Id[]`，引用已完成、同项目的 Blob，默认 `[]` |
| `mode` | `"start" \| "steer" \| "queue"`；存在活动运行时，用户必须选择 `steer` 或 `queue`，不得后台替用户选默认值 |
| `target_run_id` | `Id\|null`；Steer 必填；Queue 可指定正在运行的依据；Start 为 `null` |
| `expected_run_row_version` | `integer\|null`；Steer 必填，Queue／Start 为 `null` |

成功返回 `{message:Record<HistoryRecord>,disposition:"started"|"steer_pending"|"queued",task_id:Id|null,run_id:Id|null,queued_request_id:Id|null}`。请求使用 `Idempotency-Key`；原始消息先可靠保存，状态竞争导致未能投递时在同一接收事务内保存阻塞状态，返回保存的消息引用和未应用原因，用户后续选择处理方式复用同一消息，不能再复制一条历史。

已有保存消息重新选择处理方式使用 `POST /projects/{project_id}/messages/{message_id}/dispatch`，参数为上述 `mode / target_run_id / expected_run_row_version`，并使用新的命令幂等键；服务端拒绝对已执行或已纳入消息再次投递。

控制端点：

- `POST /projects/{project_id}/tasks/{task_id}/interrupt`：`{expected_task_row_version:integer}`，映射 `interrupt_task`；页面默认提交业务根 Task，明确指定子任务时提交该子任务 ID。
- `POST /projects/{project_id}/runs/{run_id}/interrupt`：`{expected_run_row_version:integer}`，映射 `interrupt_run`。
- `POST /projects/{project_id}/tasks/{task_id}/resume`：`{expected_task_row_version:integer,allowance:BudgetAllowance|null}`，映射显式继续操作；页面显示将采用的额度和原累计用量。
- `POST /projects/{project_id}/conversations/{conversation_id}/queue/resume`：`{expected_gate_row_version:integer}`，映射 `resume_queue`；显示解除队列暂停的实际回执，不表示被停止 Task 已恢复。
- `GET /projects/{project_id}/operations/{operation_id}`：返回幂等操作实际状态与原结果，供超时重连查询。

上述 POST 控制端点均要求 `Idempotency-Key`。待处理卡片采用 5.5 的结构化动作接口；服务端保存用户实际选择对应的用户消息和操作事件。用户在对话中明确提出相同控制时，内部命令关联已保存的真实消息 ID，不增加第二次确认。

确认绑定用户消息及固定版本写入 `record_confirmation`。每个需确认事项必须有直接按钮或填写框；按钮点击直接进入程序校验与执行，不再经过模型意图识别。明确自然语言表态仍可处理。数据可视化中的只读查看不触发确认。

### 5.4 文件、配置与事件

| 方法与路径 | 请求 | 返回／规则 |
| --- | --- | --- |
| `POST /projects/{project_id}/blobs` | `multipart/form-data`：`file`、`purpose:"source"\|"attachment"`；`Idempotency-Key` | `Blob` 元数据；媒体类型与上传大小按服务端配置校验。上传成功只表示保存完成，解析结果另作为产物记录 |
| `GET /projects/{project_id}/blobs/{blob_id}/content` | `download?:boolean`，支持 HTTP Range | 受控内容或附件下载；文本按记录编码读取，下载名经过清理；不返回存储绝对路径 |
| `GET /projects/{project_id}/config-versions` | `scope_kind?:string,scope_key?:string,config_key?:string`、`limit?,cursor?` | 配置版本、当前发布引用和 `scope_row_version`；包括获准读取的全局继承来源，供同页配置区读取 |
| `GET /projects/{project_id}/config-versions/{config_id}` | `config_id:Id` | 固定配置记录和适用的发布引用；允许当前项目配置及已获准继承的全局配置版本，其他目标返回不可见；不因此获得全局配置写权限 |
| `POST /projects/{project_id}/config-versions` | `{config:RecordInput<ConfigVersion>,expected_scope_row_version:integer\|null}`，`Idempotency-Key` | 返回 `{version:Record<ConfigVersion>,scope_row_version:integer}`；`null` 仅允许范围尚不存在。父内容通过 `config.parent_config_id` 指定；校验字段、Schema 和配置引用后保存新草稿，不影响活动运行 |
| `POST /projects/{project_id}/config-versions/{config_id}/publish` | `{expected_row_version:integer,expected_scope_row_version:integer}`，`Idempotency-Key` | 发布版本与变更影响；只对后续运行生效，活动配置快照保持固定 |
| `POST /projects/{project_id}/context-preview` | `{task_id:Id,config_version_id:Id\|null}` | 只读上下文组装预览：材料、来源版本、估算、缺项／省略原因；不保存正式模型调用，不触发生成／确认 |
| `GET /projects/{project_id}/events` | SSE；可选 `Last-Event-ID` 或 `cursor`，不能冲突 | 已提交的 `RuntimeEvent`；事件 ID 可恢复，数据含记录引用与序号，页面按 ID 去重 |

配置版本保存后不可任意改写其内容，后续编辑产生新的草稿版本；发布指针单独受乐观锁保护。配置差异、回退和恢复继承均保存新的明确操作与引用，不修改已运行任务依据。首版全局配置由服务端初始化并供继承读取；普通项目页面通过项目、Agent、阶段和辅助任务覆盖调整所有已声明参数，项目路由不能修改全局配置或跨项目配置。`run_snapshot` 由内部解析器创建，页面只读。

SSE 是数据库已提交事件的推送，不是任务进程生命周期控制器。页面断连不停止后台任务；重连先读取任务快照，再按事件游标补齐后续变更。游标早于可用推送保留范围时返回 `cursor_expired`，页面重新读取快照并取得新游标，不重新启动任务。流式文字片段可作为临时显示帧，必须以最终归档消息取代；临时帧不作为产物版本或业务事实。

### 5.5 结构化待处理卡片

| 方法与路径 | 请求 | 返回／规则 |
| --- | --- | --- |
| `GET /projects/{project_id}/conversations/{conversation_id}/actions` | 无 | `{cards:ActionCard[]}`；按实际任务、待回答事项、固定版本和队列状态派生，只读，无模型调用 |
| `POST /projects/{project_id}/conversations/{conversation_id}/actions` | `{card_id:string,action_id:string,expected_revision:string,values:object}`；`Idempotency-Key` | 已执行动作回执，包含状态、真实用户消息、操作事件，以及适用的任务／排队请求 ID；不把计划文本当作已执行事实 |

`ActionCard` 字段为 `id / revision / kind / title / description / details / targets / actions`。`details` 为 `{label,value}` 字符串数组，`targets` 为可展开的固定证据引用。每个动作包含 `id / label / description / fields / disabled_reason`；字段包含 `name / label / type / required`，可含 `options:[{value,label}]` 和 `default`。首版字段类型为 `text / textarea / number / select / checkbox`。`disabled_reason` 不为空时显示原因并禁止提交。

确认按钮只确认卡片绑定的版本与范围；提出修改时要求填写修改说明。问题支持建议选项及自行填写，每次只解决指定待办。费用、输出上限和队列等暂停原因显示独立操作，不能只显示错误码并等待用户猜测自然语言指令。原配置续跑与已发布配置重做必须区分；原配置续跑的附加额度留空明确按零处理，不暗中增加额度。新任务费用和活动时间预算以最新发布值预填，提交前可修改；旧费用未知的复选框不能预选。已知终态调用缺费用时，保留其未知事实；录入费用须有来源，独立重开须明确接受旧费用未知。真正未明的远端结果不允许通过新开按钮绕过。

服务端在事务与对应会话／材料锁内重新计算 `revision`，验证卡片、动作、字段和目标归属，再保存用户选择与执行结果。状态变化返回 `action_stale`，不自动将选择套用到新版本；相同幂等键与正文重放返回原回执，不重复写消息或启动任务。所有结构化选择保留用户来源，不能新增模型虚构的确认或用量。

## 6. 错误与验收

| 错误码 | HTTP | 含义与处理 |
| --- | --- | --- |
| `unauthenticated` | 401 | 重新登录；不取消已在后台执行的任务 |
| `forbidden` | 403 | 当前账号无操作权限；不能用客户端改项目 ID 绕过 |
| `not_found` | 404 | 授权范围内目标不存在；跨项目不可见目标也不泄露细节 |
| `invalid_request` | 400 | 参数类型、组合、游标或范围无效 |
| `schema_validation_failed` | 422 | 结构校验失败，返回字段路径和可操作原因 |
| `missing_required_input` | 422 | 必需固定来源缺失；不自动用草稿或别的版本替代 |
| `version_conflict` | 409 | 条件版本已变化；读取最新状态，再按原用户意图决定新操作 |
| `action_stale` | 409 | 待处理事项的任务、版本、依赖或配置已变化；刷新卡片后重新选择，不自动重放旧选择 |
| `action_unavailable` | 409 | 当前动作无法执行；展示实际阻塞原因，不静默清除其他暂停条件 |
| `dependency_changed` | 409 | 固定依赖已不满足生效规则；旧内容可归档，不能直接生效 |
| `ambiguous_target` | 409 | 对象或确认范围不唯一，返回允许查看的候选引用 |
| `target_not_running` | 409 | Steer 到达时运行已结束；保留消息并显示未应用状态 |
| `invalid_state` | 409 | 当前状态不支持此动作；不自动跳过等待／暂停 |
| `idempotency_conflict` | 409 | 相同键对应不同业务请求 |
| `operation_in_progress` | 202 | 原操作仍在处理；查询原操作，不能换键复制执行 |
| `lease_lost` | 409 | 后台执行者已失去写入资格；不继续推进 |
| `budget_exhausted` | 409 | 保存暂停与用量，等待明确继续／追加额度 |
| `cursor_expired` | 410 | 读取快照并重新建立分页／事件游标 |
| `payload_too_large` | 413 | 文件／请求超过已配置限额 |
| `read_error`、`write_error` | 503 | 持久服务失败；是否可重试由返回的 `retryable` 指定，写入结果不明先查幂等操作 |

模型工具仍使用主文档既有的 `ok / no_matches / not_found / no_effective_version / ambiguous_target / invalid_request / read_error` 操作状态；HTTP 错误与工具返回通过适配层转换，不能直接把 HTTP 200 等同于业务产物有效。

实现验收至少覆盖：相同消息重复提交只保存一次；并发确认命中明确版本；跨项目引用被拒绝；版本更新不覆盖旧内容；同一工作 Session 不并发写入；崩溃后检查点与已提交写入一致；未明调用和未知费用不会伪装成功／零费用；下载内容是独立有效的 Nexo `Project` JSON；页面断连重连不会重复执行任务。
