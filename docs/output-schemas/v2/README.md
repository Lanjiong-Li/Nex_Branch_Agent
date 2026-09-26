# 分支 Agent 输出 Schema v2

最终 `nexo_graph` 的根对象直接是 **Project**，章节内嵌 `nodes/edges`；供 Nexo 剧情树与剧本视图共同使用。它不包含模型结果信封、媒体时间轴或完整编辑包。契约从 `nexo-studio` 的 `feature/v2.1.0`、commit `006296f` 提取，来源与裁剪范围见 [source-manifest.json](source-manifest.json)。

先读 [完整 Graph JSON 示例](examples/nexo_graph.example.json)，再看 [最终 output_type Schema](nexo_graph.schema.json)。示例含两章、选择、QTE、条件、变量修改、分支汇流和跨章 jump；可用来理解 JSON 形状。样例身份与时间为离线 fixture，不是实际项目、资产或用户确认记录。

## 绑定与版本

[registry.json](registry.json) 是当前类型和绑定清单。默认 Step10 每次输出 [chapter_graph](chapter_graph.schema.json) 的完整单章快照，程序按冻结基线组装最终 Project；整剧生成时也可直接把 `nexo_graph` 绑定为 Agent 的输出类型。两个模式的最终产物只有同一个 Project 合同。

JSON 文件是 `output_type` 的定义源。[SchemaCatalog／JSONOutput](../../../branch_agent/schemas.py) 负责加载和校验，通过 `AgentOutputSchemaBase` 接入 SDK；不能把原始 JSON 字典当成 SDK 类型直接传入。[Graph 组装器](../../../branch_agent/graph.py) 将章节快照合并为最终 Project。

| 使用位置 | Schema | 版本 |
| --- | --- | --- |
| 协调／历史回答 | [coordinator_response](coordinator_response.schema.json) | 1.1.0 |
| Step1 · 全局事件 Agent | [source_global_events](source_global_events.schema.json) | 1.0.0 |
| Step1 · 主要人物事件 Agent | [source_character_events](source_character_events.schema.json) | 1.0.0 |
| Step2 | 两份普通文本分析：`source_global_analysis`、`source_character_analysis`（均不绑定 `output_type`） | — |
| Step3 | [adaptation_strategy](adaptation_strategy.schema.json) | 2.1.0 |
| Step4 | [adaptation_plan](adaptation_plan.schema.json) | 2.1.0 |
| Step5 | [game_event_view](game_event_view.schema.json)（叙事功能暂为 null） | 2.3.0 |
| Step6 | [game_event_narrative_patch](game_event_narrative_patch.schema.json)（只输出事件 ID 与叙事功能，由 Harness 合并） | 1.0.0 |
| Step7 | [ending_routes](ending_routes.schema.json) | 3.0.0 |
| Step8 | [player_profiles](player_profiles.schema.json) | 1.1.0 |
| Step9 | [chapter_design](chapter_design.schema.json) | 2.1.0 |
| Step10 逐章 | [chapter_graph](chapter_graph.schema.json) | 2.0.0 |
| Step11（仅当校验 instructions 非空） | [review_report](review_report.schema.json) | 2.1.0 |
| 局部辅助任务 | [subtask_result](subtask_result.schema.json) | 1.0.0 |
| 整剧输出／最终产物 | [nexo_graph](nexo_graph.schema.json) | 2.0.0 |

共 15 个注册 Schema，其中旧 `source_views` 仅供历史配置和产物读取；新 Step1 分别保存两份结构化产物。Step2 的全局事件分析、主要人物事件分析与工作摘要均为普通文本产物，不注册为 Agent `output_type`；Step6 的模型输出仅是叙事功能补丁，Harness 将其合并到原有 `game_event_view`，保存新版本并更新方案的 `game_events` 引用。Step7 根据这个固定版本的事件视图生成独立 `ending_routes`；来源记录与版本由 Harness 保存，确认后更新方案的 `ending_routes` 引用。旧 `event_function_map` 仅供历史产物解读。旧 Nexo 编辑包快照留在 v1，不在活动注册表中。

## 返回结构与消费边界

- 除 `nexo_graph` 外的注册结构化类型使用 `result_kind/payload/questions/evidence_refs/notes` 信封；Step2 一次返回带固定分隔标记的可读文本，Harness 拆分保存为两份独立产物。任何模型状态或文本均不是实际用户确认。
- `nexo_graph` **不使用信封**：`id/name/description/prompt/revision/updatedAt/chapters/chapterEdges/variables/scenes` 就是完整根结构。来源、运行、Schema、确认和产物版本存在 Harness 外壳。整剧模式只有在所需材料与身份目录完整后启用，缺项经前置阶段／协调处理，不在 Project 中塞入问题或伪造字段值。
- 字段是编辑器的 camelCase，节点为 `story`；`body` 保存完整文字，互动以 `scriptInline.afterLine/order` 定位；`Variable.value` 为类型对应的字符串。上游原作 UTF-16 锚点与语义类型初值仍保持其本来定义。
- 本 profile 保留 TypeScript 必需字段，并收紧适用类型。选入可选字段显式必填但使用原字段类型；没有采用“可选即 null”。可选制作/复制/版本缓存字段省略，已存工程中的对应原值必须在消费合并时保留。Project JSON 不是现有 API 可直接接收的请求体。
- 程序质量检查固定启用 JSON Schema 与不可达节点检查；后者从章节入口遍历连线，并对受支持的条件与变量脚本做保守静态执行。校验 Agent 默认关闭，配置非空 instructions 后只读取最终 Graph。逐章快照中未提及旧对象不等于获准删除，详见 [nexo-mapping.md](nexo-mapping.md)。
- DSL 字符串的具体语法见 [Nexo 创作契约](nexo-authoring-contract.json)。Step10／Step11 自动获得其固定正文与哈希，作为与 Schema 配套的必需程序材料；不依赖模型搜索历史猜测赋值或条件语法。
- 人物／地点设定保留在改编方案与项目实体目录中，`Scene.characterIds/locationIds` 只引用真实解析的实体；根 Project 不新增 `characters/assets/resources` 等另一种结构。

## HTML 与 instructions 配置

全部 13 个活动结构化输出类型（包括直接 Project）进入 HTML 配置页的字段树／JSON Schema 编辑器。Step2 的两份普通文本分析和工作摘要不出现在该编辑器中。instructions 同样可编辑、预览。发布时保护既有消费者依赖字段及阶段绑定；不兼容改动需要新增消费端适配，不能仅修改 Schema 就宣称支持。`chapter_graph` 与 `nexo_graph` 内复制的领域定义必须一致且同步发布。

规范见 [开发文档第 6 部分](../../分支Agent开发文档.md#6-输出类型与数据契约)，实际支持边界见 [交付状态](../../implementation-status.md)。当前与历史版本不可静默混用，运行固定配置与 Schema 版本；任意旧产物结构的自动迁移尚未实现。

## 审查和验证

以下是已有 Schema／样例的离线核对资料，检查范围不等于首版运行时启用清单；首版清单见 [开发文档 8.2](../../分支Agent开发文档.md#82-首版程序校验)。整个 Harness 的系统评测暂不纳入范围。

- [逐类型审查](schema-review.md)
- [映射、字段所有权与业务约束](nexo-mapping.md)
- [离线检查脚本](validate.py) · [结果](validation-report.json)
- [改编方案示例](examples/adaptation_plan.example.json) · [章节设计示例](examples/chapter_design.example.json)
- [章节输出示例](examples/chapter_graph.example.json) · [最终 Project 示例](examples/nexo_graph.example.json)

后两个示例彼此一致；上游两个示例是独立 fixture，不是与最终 Graph 完全逐字对应的一次连续运行。离线检查不代表真实模型调用、浏览器渲染、后端写入、完整 DSL 编译或发布播放已通过。
