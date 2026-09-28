# 分支 Agent 首版界面实现记录

界面文件位于 `branch_agent/static/`，由 FastAPI 同源提供。原生 ES Modules、HTML、CSS，无前端构建步骤及外部运行时依赖。

**当前界面包含对话内结构化待处理卡片，最新验证见[结构化操作交付记录](structured-actions.md)。** 阶段确认、修改、逐题回答、暂停恢复和单条队列处理均有直接按钮或输入框。已有真实最终交付下载和错误展示验证继续保留：API／Engine 驱动真实模型完成创作、返修和审核，浏览器只读打开最终 Graph v11 并下载 11,139 字节文件；解析内容与固定产物一致，实际 SHA-256 与 `ArtifactVersion.content_sha256` 一致。下面各历史批次保留当时范围和结果。

## 已实现

- 浅底墨绿工作区：项目与会话侧栏、主对话、阶段产物条、默认收起的配置／运行数据区；支持窄屏布局和键盘发送。
- 本地账号登录、创建项目与会话、持久历史恢复。写请求使用 Cookie、CSRF token 与幂等键；不确定网络失败重试相同内容复用原幂等键。
- TXT／Markdown／DOCX 或粘贴全文导入，展示实际 token、准入上限及有效输入预算。导入保留原文，切分仍由工作流决定。
- 运行中明确选择 Steer／Queue；停止、原配置继续、显式追加耗时／费用、新配置重跑，以及逐条恢复／取消队列。待办卡片直接执行用户选择，进度仅来自服务端 Task／Run／SSE 记录；不再提供清除整个会话暂停的通用按钮。
- 卡片展示固定版本、依赖和新旧配置；刷新保留输入，过期选择阻止提交，费用接受需主动勾选。产物标签优先显示依赖失效／待复核状态，队列数只统计当前会话尚未处理的请求。
- 错误显示中文原因和原错误码，并可跳转到准确的 Task／最新 Run；父任务的 `child_blocked` 展开当前阻断子任务原因。输出截断、内容策略、供应商失败、HTTP 分类和结果不确定分别展示，历史失败不冒充当前问题；控制按钮目标保持不变。
- 已保存阶段版本查看；最终交付按服务端 `latest_delivery` 固定版本提供查看、内容复制和 Project JSON 下载。
- 全部 22 种 Harness 记录的筛选、分页、精确 ID 定位、中文结构化详情、可折叠 JSON、字段／内容搜索与复制。
- 固定 EvidenceRef 解析、原始来源跳转、外置内容读取；产物版本选择、字段差异、逐行文本差异和按稳定 ID 对齐的数组差异。
- 直接关系图及等价关系表、任务事件时间线、实际模型上下文与用量记录。模型调用列表按调用 ID 去重，可按任务／Run／Agent／阶段汇总已加载记录；明确标记分页范围、估算和未知，不冒充全项目完整统计。费用显示采用十进制定点字符串相加。
- 通用模型、上下文、分批、重试、预算、instructions 表单及完整 JSON；全部 16 个活动结构化输出 Schema 可用字段树与 JSON 编辑。Step1 全局事件 Agent 直接使用 `source_global_events`，逐事件分析保存在事件内；Step2 输出单一结构化 `source_knowledge_asset`，工作摘要使用普通文本。配置先保存草稿后发布，支持项目／Agent／阶段／辅助任务作用范围、查看继承依据及恢复继承。
- 配置保存仅提交相对已解析基线的修改差异，并保留同范围既有覆盖，防止把协调阶段默认值错误发布成项目级覆盖。Step1 可单独读取阶段配置，并展示真实模型窗口与输出能力。
- 原作、模型内容、JSON、代码均作为文本渲染；公开副本对凭据及隐藏推理载荷做额外遮蔽。数据详情没有任意改库或伪造确认入口。

## 验证

1. `node --check branch_agent/static/app.js` 和 `node --check branch_agent/static/ui-utils.mjs`：通过。
2. `node --test tests/frontend-utils.test.mjs`：最新 **18 项通过**，覆盖 HTML 文本转义／敏感字段、未知预算、配置能力约束、固定 ID 差异、Schema JSON Pointer、逐行差异、继承差异保存、精确费用汇总、主任务控制目标及真实错误来源选择。
3. `tests/frontend-smoke.mjs`：真实 Chrome + 明确 API fixture，通过消息模式、CSRF／幂等头、XSS 文本化、草稿发布、记录遮蔽、HTTP 200 失败 ModelCall 正常展示及非 2xx 错误拒绝、1440px／390px 布局检查；未调用模型。
4. `tests/frontend-live.mjs`：对 `http://127.0.0.1:8767` 的真实本地 API 通过登录、项目／会话创建、全文导入、15 Schema、差异配置发布、Step1 继承预算仍为输入 256000／输出 32000、记录 JSON 搜索、关系图与窄屏检查。此脚本要求 embedded worker 关闭，私下读取 `.data/local-login.txt`，不输出登录凭据；不提交生成消息、不调用模型。
5. `tests/frontend-errors.mjs`：本机 Chrome fixture 验证当前子任务输出截断、精确 Run 跳转、恢复目标不变、真正结果不确定、内容策略限制、明确供应商失败、历史失败排除及敏感诊断遮蔽；无业务写入或模型调用。
6. `tests/frontend-error-live.mjs`：在明确收到部署与记录更正完成信号后，对主服务执行登录和只读核验，拦截其他所有写请求。实际错误中文、固定 Run、聊天更正、原文及 Step1 产物保留均通过。

浏览器测试依赖环境中的 Playwright 与 Chrome；不是产品运行依赖。可用 `PLAYWRIGHT_MODULE` 指定已有 Playwright 路径，或通过 `NODE_PATH` 指向环境自带包。真实接口测试可通过 `FRONTEND_BASE_URL` 指定地址。

截图：`tests/frontend-live-desktop.png`、`tests/frontend-live-mobile.png` 为真实接口联调界面；`tests/frontend-desktop.png`、`tests/frontend-mobile.png` 为标明测试项目的 fixture 界面。

## 验证边界

本记录中的真实创作由 API／Engine 驱动，浏览器只进行现有数据展示与最终下载验收；不宣称浏览器驱动了完整 Step1–11 创作。平台身份及生产部署仍需单独接入。界面的服务端预算和权限检查不能由客户端校验替代。用量列表默认仅汇总已加载记录，未增加独立全项目分析仪表盘。

## 后续 API 与身份边界复核

修改范围仅为 `branch_agent/auth.py`、`branch_agent/app.py`、`tests/test_api.py` 和本报告。

- `WorkflowBlocked` 统一映射为可读的 HTTP 409，并返回真实 reason／details；JSON 对象及重要字段类型、查询参数错误、未预期异常均返回既有 `error` 包装。
- 本地身份校验拒绝非文本密码、畸形签名内容及跨源写入。凭据文件先以 0600 写入临时文件再原子发布，防止并发初始化读到半份签名密钥。
- 导入文件和 JSON 粘贴正文执行相同 10 MB 字节上限。DOCX 按正文 XML 顺序读取段落、表格和嵌套表格，保留前后顺序；损坏文件返回明确解析错误。图片／嵌入对象／脚注尾注等当前无法完整读取的正文不会被静默丢弃，会要求补充纯文本。限制异常巨大的 DOCX 解压内容。
- 导入幂等摘要同时包含原始附件字节哈希与解析正文哈希；重复提交同一附件返回原结果，不增加附件及原作导入。
- 记录列表分批跨越全部数据；历史和 SSE 直接按数据库 sequence 查询，不再只取前 10000 条。SSE 续接读取 `Last-Event-ID`；历史保持 tail／after_sequence 及前后 cursor 兼容。项目 limit 截断及负数／超大游标得到一致处理。
- 关系查询使用 `record_links`／`config_links` 的真实投影，返回物理固定版本目标和原始 `target_ref`；正文中的 UUID 字符串不再产生伪关系。
- Blob JSON 通过公开投影返回，明确标记凭据或不可公开推理载荷已遮蔽；原始证据不改写。普通文本强制 `text/plain`、二进制文件强制下载，避免在同源执行上传内容。

验证使用真实 PostgreSQL、独立测试 schema 与 IdleEngine，不发送模型请求。原有及新增 API 用例共 11 项均获得通过结果：其中含 10007 条历史和 10007 条事件的分页／SSE 续接回归，DOCX 顺序与嵌套表格、重复导入、同等文件／JSON 限制、身份与错误封装、固定关系防误匹配及公开 Blob 投影。最近补充的大小写凭据遮蔽与不完整 DOCX 拒绝用例单独复跑通过。`app.py`、`auth.py` 编译检查通过。

## 历史非 Runtime 回归批次（2026-09-21，后续修复前）

本历史批次从头运行当时代码，包含 Schema 校验缓存、SDK 边界、Session 持有权、配置快照及工具读取修复。只记录完整批次结果，不合并中断批次；按分工未执行 `tests/test_runtime.py`。本节不是后续修复后的最终全量测试结论。

- `.venv/bin/python -m pytest -q --ignore=tests/test_runtime.py`：**92 项通过**，耗时 **45.78 秒**。唯一警告来自 Starlette 测试客户端引用已弃用的 `anyio.abc.BlockingPortal`，无失败或错误。
- 当时随后运行 `node --test tests/frontend-utils.test.mjs`：**9 项通过**，无失败；后续主任务控制修复新增回归，最新为上文的 **12 项通过**。
- 随后运行 Chrome fixture `tests/frontend-smoke.mjs`：**通过**。覆盖消息模式、CSRF／幂等、文本转义、注册模型选择与对应推理档、草稿保存发布、记录遮蔽、桌面及窄屏溢出检查。

浏览器使用明确的本地 API fixture；本批未运行真实模型脚本，也未新增业务项目。检索与产物读取回归包含实际 JSON token 上限、同会话邻接、固定引用、异常游标、大状态及部分确认范围分页回读。

## 真实模型数据的浏览器只读复核（2026-09-21）

本次数据来自 API／Engine 驱动的真实模型验收，浏览器只负责只读展示核验，未通过浏览器驱动完整创作。验收使用现有独立 schema `live_smoke_20260920_192458_cd6081f2` 和项目 `dcfc7a66-1c6c-498c-bb4b-17ac0047900e`，没有新增业务项目或改写其记录。

临时服务仅监听 `127.0.0.1:8768`，嵌入工作进程关闭；数据库连接强制只读，并拒绝业务写入路径。临时 LocalAuth 仅在内存适配器映射到现有验收账号，不改变生产认证或项目所有权。浏览器另加写请求拦截；登录凭据不输出、不存入报告。

03:36（Asia/Shanghai）的真实 Chrome 核验通过：15 条可见历史、8 份已保存产物、14 条实际模型调用均可读取。打开了 Step5 游戏事件视图的固定 v1，以及具有真实 provider response ID、token 和费用记录的成功模型调用。无浏览器脚本错误、HTTP 错误或业务写请求尝试。该时点尚无最终交付，未虚构下载验收结果。

发现并修复任务状态栏的实际遮蔽问题：同一会话内优先选择 `running/stopping`，其次 `paused/failed/stopped`，再选 `waiting_user`，最后保留 `queued` 状态；同级取最新状态记录。父任务等待确认不再遮住需要恢复的协调任务。现场已显示 `input_budget_exceeded`，继续按钮明确指向 `0a30b5f2-1ab7-4004-a28b-acd39fe31a8c`；未点击或提交恢复。新增纯函数回归后共 **10 项通过**，Chrome fixture 再次通过。

证据脚本及结果：`tests/frontend-real-model-readonly.mjs`、`tests/frontend-real-model-results.json`。截图：`tests/frontend-real-model-resume-target.png`、`tests/frontend-real-model-conversation.png`、`tests/frontend-real-model-artifact.png`、`tests/frontend-real-model-paused-task.png`、`tests/frontend-real-model-call.png`。真实交付出现后，脚本将从页面打开其固定版本并通过 Chrome 下载，再比较下载文件 SHA-256 与已保存的 `ArtifactVersion.content_sha256`。

03:53 的补充复核处理了内部辅助任务的控制边界：有 `parent_task_id` 且 `intent=summarize` 的摘要任务不作为页面主停止／继续目标；父任务已 `succeeded` 的历史 `failed` 子任务也不再遮蔽当前确认等待。完整 Task 记录仍保留在数据检查器中，真正业务暂停仍优先显示。新增两条回归先失败后通过，当前单测 **12 项通过**，Chrome fixture 通过。实际 Chrome 从 22 条可见历史、12 份产物、23 条模型调用中正确展示暂停父任务 `1cee5ff5-720f-453e-9595-a6d445c5c6cc` 及 `input_budget_exceeded`，未把失败摘要 `aa37d5aa-1026-4c4d-bf0a-33c6f19880af` 作为恢复目标；未提交恢复，无脚本错误、HTTP 错误或业务写入尝试。最新截图与 JSON 结果已更新。

## 历史停点：只读验收与关闭临时服务（2026-09-21 05:03）

最终 Chrome 快照读取 **53 条可见历史、21 个产物记录、80 条模型调用**（78 条 `succeeded`、2 条 `failed`，无剩余分页）。末两条调用记录为 `RateLimitError`／HTTP 429；运行端另外诊断为 `credit_balance_exhausted`／`insufficient_quota` 后停止重试。浏览器显示 Step10 暂停及精确恢复目标 `0e509c0b-bd1e-4e87-8171-e007230670ab`，未提交恢复。Graph 产物 `5205d440-35dc-4fdc-b60b-e1ac1629d827` 已有过程草稿 v3，但没有有效版本或最终交付事件，不能宣称完整 Graph 验收成功。**最终固定版本下载与 SHA-256 对照尚未执行。**

最后失败调用详情暴露并修复了错误信封与记录数据混淆：HTTP 200 返回的 ModelCall 本身可合法包含 `error` 字段，现按记录读取；非 2xx 响应始终报错，即使响应带 `record_type`。既有 Chrome fixture 增加这两种明确行为验证并通过，随后真实失败调用 `5c893c60-40c2-4678-a4e0-323cca7420de` 的 JSON 与错误字段成功展示。最终浏览器检查无脚本错误、接口 HTTP 错误或业务写入尝试；模型调用记录中的供应商 HTTP 429 作为真实历史数据展示。

最后结果仍保存于 `tests/frontend-real-model-results.json`；当前 API 摘要为 `tests/frontend-real-model-current.json`。`tests/frontend-real-model-call.png` 显示真实失败调用，`tests/frontend-real-model-resume-target.png` 显示暂停任务，其他同名前缀截图均已更新。只读验收脚本保留，余额补充且正常流程产生真实 `latest_delivery` 后，可用新的隔离只读适配器继续页面下载及哈希核验。

临时 `127.0.0.1:8768` 服务已正常退出，端口关闭，独立临时认证目录已删除。主服务 `127.0.0.1:8767` 未停止，收尾时 health 为 `ok`、worker 为 `true`。本次浏览器验收没有写入业务数据、发送恢复请求或触发任何模型调用。

## 最终交付下载验收通过（2026-09-21 10:46）

余额补充后，API／Engine 在原隔离项目中继续真实运行，并完成 Step10 候选、正式模拟确认、Step11 审核、返修与复审。只有实际 `latest_delivery` 出现后才执行本次浏览器下载，未把先前中间草稿当作最终交付。验收仍使用仅本机的独立 8768 适配器：工作进程关闭、数据库事务只读、业务写入路径拒绝、独立临时认证；未修改真实项目身份或触发模型调用。

真实 Chrome 点击“查看最终交付”后打开 Graph **v11**，再点击“下载最终 Project JSON”保存文件。下载对应逻辑产物 `5205d440-35dc-4fdc-b60b-e1ac1629d827`，固定版本记录 `134b9454-dc4b-4fee-a3fe-7e1c7a03bcf9`，文件名 `nexo-5205d440-35dc-4fdc-b60b-e1ac1629d827-v11.json`。

- 下载文件 **11,139 字节**，解析后的 JSON 与该固定产物 API 内容完全一致，也与程序导出 `artifacts/live-smoke.nexo.json` 的解析内容一致。
- 下载实际 SHA-256 与存储的 `ArtifactVersion.content_sha256` 均为 `379c2389ad60f9a5e5fd23149efe4a2415405afb422691f75652b1454be88399`。
- 无浏览器脚本错误、HTTP 错误或业务写入尝试。页面读取 64 条可见历史、25 个产物记录；调用列表仅检查已加载 100 条，`next_cursor=100`，不将其冒充全项目调用总量。

已保存下载文件 `tests/frontend-real-model-delivery.json`、截图 `tests/frontend-real-model-delivery.png` 及最终结构化结果 `tests/frontend-real-model-results.json`。脚本 `tests/frontend-real-model-readonly.mjs` 保留，可重复核验。最新截图展示真实交付按钮、固定 v11 记录及下载入口；页面主控制状态直接来自当时 API，没有为验收更改 Task 状态。

验收后 8768 服务正常退出，已确认端口关闭和本次私有临时认证目录删除。主服务 8767 未停止，收尾 health 仍为 `ok`、worker 为 `true`。此次最终下载核验通过，UI 剩余验收项已完成。

## 错误展示部署后的真实只读核验（2026-09-21 11:31）

在收到“已部署／已矫正”信号后，使用独立 Chrome 会话登录主服务 8767，私下读取现有本地凭据；未输出凭据、修改用户页面输入、配置或继续任务。项目 `926aed68-d15f-5576-8990-1a1d9488fc5d` 的顶部实际显示“模型输出达到本次上限，结果未完整生成。”及 `output_limit_exceeded`。

“错误详情”准确打开 Run `1d216750-dc46-474c-b3e1-bb7f70ff1755`，对应暂停 Task `c3d345fa-de71-4e5b-9d47-d34c5dbe0ba4`；显示 `terminal_status=incomplete` 和 `max_output_tokens=8000`。聊天中的错误原因更正记录 `3c34e6fc-8c7c-4b61-b196-d31388f2cd47` 可见，说明旧调用真实用量未保存、费用仍待核实，任务未自动重跑。原文 `source_text` v1 和 Step1 `source_views` v1 的固定内容及哈希仍可读取。

核验期间 Task／Run 的状态与 row_version 未改变，浏览器脚本错误、HTTP 错误、业务写入尝试均为 0。截图 `tests/frontend-error-live.png`、结构化结果 `tests/frontend-error-live-results.json` 及只读脚本已保留。本次未重启服务，也未调用模型或提交任何业务写入。
