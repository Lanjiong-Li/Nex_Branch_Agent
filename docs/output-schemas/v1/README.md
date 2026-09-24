# 分支 Agent 输出 Schema v1

本目录是首版输出契约与配置设计文件，尚未实现 Harness 加载器、模型适配层、Nexo 转换器或 HTML 编辑页面。

`registry.json` 登记所有类型、版本、文件哈希与使用位置。业务模型 Schema 版本为 `1.0.0`；最终 Nexo 契约快照版本为 `format4-20260914`。这些版本与产物内容版本分别管理。

| 文件 | 用途 |
| --- | --- |
| [registry.json](registry.json) | 类型注册与 Agent／阶段／辅助任务绑定 |
| [coordinator_response.schema.json](coordinator_response.schema.json) | 对话协调、历史回答及任务请求 |
| [source_views.schema.json](source_views.schema.json) | Step1 原作双视图 |
| [source_analysis.schema.json](source_analysis.schema.json) | Step2 原作分析与候选保留项 |
| [adaptation_strategy.schema.json](adaptation_strategy.schema.json) | Step3 玩家身份与改编策略 |
| [adaptation_plan.schema.json](adaptation_plan.schema.json) | Step4 改编方案 |
| [game_event_view.schema.json](game_event_view.schema.json) | Step5 游戏事件 |
| [event_function_map.schema.json](event_function_map.schema.json) | Step6 事件功能／玩家意图 |
| [ending_routes.schema.json](ending_routes.schema.json) | Step7 结局与路线 |
| [player_profiles.schema.json](player_profiles.schema.json) | Step8 玩家画像 |
| [chapter_design.schema.json](chapter_design.schema.json) | Step9 完整正文与章节互动设计 |
| [chapter_graph.schema.json](chapter_graph.schema.json) | Step10 章节创作／变更载荷 |
| [review_report.schema.json](review_report.schema.json) | Step11 审核报告 |
| [work_summary.schema.json](work_summary.schema.json) | 工作摘要 |
| [subtask_result.schema.json](subtask_result.schema.json) | 局部辅助任务默认结果 |
| [nexo-editor-package.format4.schema.json](nexo-editor-package.format4.schema.json) | 用户提供的 Nexo 编辑包 Schema 原样快照，用于程序组装后校验 |
| [nexo-mapping.md](nexo-mapping.md) | Step10 创作载荷与编辑包的字段映射及业务规则 |

每个模型输出 Schema 都是自包含的对象，使用统一的 `result_kind / payload / questions / evidence_refs / notes` 信封。正文对象在 `payload`；根不允许任意字段。不同文件的 `$defs` 作用域独立，修改一份定义不自动修改其他类型；跨类型公共语义变化须在配置发布时一并检查消费者。

`ready` 要有非空 payload 且无阻塞问题；`needs_input` 要有具体待澄清事项。结果信封中的取值仍需业务校验，不能签发完成、确认、生效或审核通过状态。系统保存外壳与实际确认记录由程序维护，不属于模型可签发字段。

Schema 通过只代表结构满足定义。引用是否存在、UTF-16 定位、当前产物内 ID、变量类型与 DSL、Graph 拓扑、正文覆盖、人工确认及版本一致性另外校验。Nexo 格式合法不代表已能导入、发布或播放。

所有类型均应出现在未来 HTML 配置页，支持字段树／JSON 双视图、结构修改、样例验证、消费者影响检查、版本发布与回退。最终 Nexo 目标同样可配置，但修改本地 Schema 不会改变实际 Nexo 接口能力；需要选择受支持的合同与映射。完整设计见 [开发文档第 6 部分](../../分支Agent开发文档.md#6-输出-schema数据契约与-nexo-映射)。

示例文件：

- [互动改编方案](examples/adaptation_plan.example.json)
- [章节设计](examples/chapter_design.example.json)
- [章节创作载荷](examples/chapter_graph.example.json)

这些示例只用于结构和映射说明，各自使用虚构的 `demo:` 或 `fixture:` 记录，并非一次真实运行的连续产物。实际运行须由 Harness 提供真实固定版本与身份目录；样例中的 `ready` 不代表已经获得用户确认，不可当作真实导入或播放验收证据。

本轮离线验证记录见 [validation-report.json](validation-report.json)。本地 SDK 的严格 Schema 处理通过不能替代所选模型 API 的支持验证。未来发布检查应验证模型能力以及实际消费者；本轮不调用外部 API。
