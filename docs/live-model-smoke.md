# 真实模型端到端验收

结果：**成功**。原创短篇《一盏灯》已完成 Step1–11：真实模型生成、隔离测试用户逐项确认、Graph 返修、正式复审及同一固定版本交付。最后一次正常调度后，顶层工作流与全部业务阶段均已完成。

最终文件：[live-smoke.nexo.json](../artifacts/live-smoke.nexo.json)。正式审核：[live-smoke.review.json](../artifacts/live-smoke.review.json)。固定证据：[live-smoke.delivery-evidence.json](../artifacts/live-smoke.delivery-evidence.json)。完整调用及会话记录：[live-smoke.receipts.json](../artifacts/live-smoke.receipts.json)。

## 固定交付与确认

| 记录 | 固定身份 |
| --- | --- |
| 交付事件 | `8e46a266-5104-41e2-8515-6580a220b51f`，2026-09-21 10:45:13 +08:00 |
| Nexo Graph | `5205d440-35dc-4fdc-b60b-e1ac1629d827`，版本 **11** |
| Graph 实体版本记录 | `134b9454-dc4b-4fee-a3fe-7e1c7a03bcf9` |
| 正式审核 | `360061e0-991c-479b-a2df-e3715e1913d6`，版本 **2**，结论 **pass** |
| 同版本程序检查 | `0e77cc90-afe9-4da8-8963-542975a828df` |
| 返修章节候选 | `80c0f3f0-bff6-4f55-a794-aade91860602`，版本 **2** |
| 章节确认 | `6a05ba8a-3b81-49e7-b049-838493eff233` |
| 确认源消息 | `d5ad21e6-0733-4b49-9e81-8a47ec995f57` |

Graph 为 1 章、10 个节点、10 条章内边，包含一个关键选择和两个安全结局。首次审核提出的两个 condition 缺少默认阻断分支的问题，已由真实 writer 按正常返修流程处理；原 9 个节点内容保留，增加一个阻断节点及两条 default 边。返修章节通过测试用户固定版本确认后，再次正式审核。审核报告覆盖 `chapter-01-one-lamp`，`unchecked_scope=[]`、`findings=[]`；审核结束无需额外人工确认。

导出 JSON 解析后与固定 Graph v11 完全相同；canonical SHA-256 为 `379c2389ad60f9a5e5fd23149efe4a2415405afb422691f75652b1454be88399`，与 `ArtifactVersion.content_sha256` 一致。现有两项程序检查 `schema_contract`、`chapter_edge_endpoints` 均通过。只读浏览器下载验收获得 11,139 bytes 的 JSON，下载内容与固定版本及本地导出一致。

## 调用、用量与费用

全程使用 `gpt-5.6-sol`，`reasoning_effort=medium`，通过真实 `ModelService`、OpenAI Agents SDK 与供应商 API 执行。所有创作及审核内容来自真实模型响应；程序只负责装配、持久化、验证和状态推进。

| 范围 | 物理 API 请求 | 归档估算费用（USD） |
| --- | ---: | ---: |
| 本次持续使用的项目/schema | 102 | 18.8119380 |
| 补足余额后的本次继续验收 | 22 | 5.1240268 |
| 前两次独立尝试 | 8 | 0.5585212 |
| 全部尝试合计 | 110 | **19.3704592** |

本 schema 的 102 次请求中，100 次成功，2 次为已明确的供应商余额拒绝；结果或用量未知为 **0**，没有待定或在途调用。早前拒绝的安全错误字段记录为 `credit_balance_exhausted` / `insufficient_quota`；补款后恢复成功，历史失败记录未改写。前两次独立尝试与所有恢复回执仍保留。

| Token 统计 | 当前 schema | 全部尝试 |
| --- | ---: | ---: |
| 输入 | 3,122,869 | 3,178,840 |
| 输出 | 248,823 | 266,453 |
| 其中缓存输入 | 386,645 | 399,743 |
| 其中推理输出 | 36,306 | 38,345 |

费用按调用时固定价格配置和真实供应商 usage 估算，包含已归档 cache-write 用量；不是供应商最终账单。以上统计包含开发排障、格式修复、上下文摘要与恢复，不能视为单个正常短篇的稳定生产成本。当前 schema 硬上限为 USD 19.40，加前两次尝试后仍低于本次 USD 20 的总验收预算。

最后一份真实复审响应已在 Run `9bb95334-e795-4b46-932a-40ec9ab351ce` 成功返回并保存，随后测试预算在响应后边界按“下一次调用”预留 USD 0.68，触发暂停。正常继续创建 recovery Run `bad1df90-440a-4cf7-b108-6a9771f5aaee`，复用原 ready/pass，再次执行证据、依赖与交付门禁后生成交付事件，**新增 API 0 次、费用 0**。随后再执行一次正常调度，顶层工作流转为 succeeded，同样没有调用模型。最终回执的 0.36 秒、0 次请求仅表示最后调度收尾，不能解释为整次续跑耗时或调用量。

## 实际配置与验收边界

后段使用已发布的项目配置覆盖：`coordinator`、`step8`、`step9`、`step10`、`step11`、`aux.summary` 的输入硬预算为 **64,000 tokens**，历史控制目标为 **12,000 tokens**。通过正常配置草稿、校验、发布入口生效，仅影响后来创建的任务；既有固定配置未改写。全局与 Step1 默认值保留。不能将本次结果描述为“全程使用 32k 默认配置”。发布记录见 [live-smoke.config-publication.json](../artifacts/live-smoke.config-publication.json)。

故事为本次验收原创《一盏灯》：单章、玩家阿澄、一次关键选择、两种安全结局。所有用户确认明确标记为自动化验收模拟用户，只在隔离测试项目生效，**不代表生产项目实际人类审核批准**。确认经正常消息与确认协议产生，未直接改数据库确认状态、注入模型产物或跳过审核。

这是一个小短篇样本，验证了真实生成、确认、返修、复审、恢复和固定版本交付的完整链路。未据此宣称长篇、并发、多账号部署或所有故障场景均已验收。程序质量检查当前只覆盖 Schema 与章内边端点存在性；真实 Nexo 编辑器导入、发布、播放器行为，尤其默认阻断节点的具体播放行为，不属于本次已完成验收。

## 最终现场

- 数据库：`branch_agent_local`；隔离 schema：`live_smoke_20260920_192458_cd6081f2`。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`。
- Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。
- 顶层工作流：`3a0a45fe-6209-43b5-8537-1443a4ed8c9a`，**succeeded**。
- 正式审核任务：`735ac13f-c9a4-466c-91af-44850dcfb1b3`，**succeeded**。
- 55 个任务记录为 succeeded；保留 5 个早前摘要失败的历史子任务。没有运行中、排队、等待用户、暂停中的任务或未处理用户待办。
- 保留 1 条早前 `intent_ambiguous` 队列记录；其目标已被后续明确模拟用户消息正式确认。保留原记录不代表当前业务仍待确认。

最终交付无需继续执行脚本。该项目位于上述独立 schema，主服务默认数据库路径不会自动显示此隔离项目。只读浏览器曾在最后一次调度前看到顶层任务 running；最终状态以 `resume21` 的正常收尾记录为准。原始归档、SDK 工作历史、固定产物、失败记录和恢复现场均保留。

`artifacts/live-smoke.resume19.receipts.json` 保存真实复审返回后的预算暂停；`resume20` 保存零调用复核交付；`resume21` 保存最后正常调度完成状态。`live-smoke.receipts.json` 与 `resume21` 对应最终现场。之前的 `attempt1`、`attempt2`、`attempt3` 及其余 `resume*` 回执未覆盖。
