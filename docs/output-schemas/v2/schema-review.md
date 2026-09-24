# 全部输出 Schema 审查

审查日期：2026-09-22。目标为 `nexo-studio feature/v2.1.0 / 006296f` 的编辑器领域 `Project / Chapter / StoryNode`。当前注册表共 13 个活动类型；活动绑定见 [registry.json](registry.json)。本文件记录结构影响和校验结论，开发文档只描述当前设计。

## 逐类型结论

| 类型 | 结论／版本 | 依据与改动 |
| --- | --- | --- |
| coordinator_response | 保留 1.0.0 | 回复、任务与确认候选是调度提案，未依赖旧 Graph 字段；保留原字节 |
| source_views | 更新 1.3.0 | Step1 使用全本上下文分别生成全局事件与人物事件；两类事件独立编号并各自直接锚定固定原作的 UTF-16 区间，完成时核验全本覆盖 |
| adaptation_strategy | 更新 2.1.0 | 将单一原作分析引用拆为全局事件分析与主要人物事件分析引用；描述明确当前交互能力，不能提前承诺通用边条件或自动重试恢复 |
| adaptation_plan | 更新 2.1.0 | 将单一原作分析引用拆为全局事件分析与主要人物事件分析引用；人物/地点设定保留为叙事身份目录，不混入 Project 媒体资产；明确跨章约束 |
| game_event_view | 更新 2.3.0 | Step5 依据原作双视图、两份分析与固定全文重新设计游戏事件，事件与覆盖项直接锚定原文，不复用 Step1 事件身份；Step6 只补充 `events[].narrative_function` 并保留原文索引；确认后由 Harness 将最新版本写回方案 |
| event_function_map | 移出活动注册表 | 旧独立映射仅保留为历史产物；Step6 模型只生成叙事功能补丁，Harness 合并为新版 `game_event_view`，下游统一读取该版本 |
| ending_routes | 更新 3.0.0 | Step7 只接收方案引用的固定版本含叙事功能事件视图；产物仅描述候选结局、路线和进入条件，以 `game_event_ids` 关联业务事件；来源记录和版本由 Harness 保存，确认后写回方案 |
| player_profiles | 更新 1.1.0 | 动机/偏好/假设结构不变；固定引用改为含叙事功能的 `game_event_view` |
| chapter_design | 更新 2.1.0 | `game_event_refs` 指定本章游戏事件；Harness 合并这些事件的固定原文锚点并写入 `chapter_source_anchors`，供 Step10 精确提取；保留完整正文段与 Graph 映射约束 |
| chapter_graph | 重写 2.0.0 | payload 变为完整 Chapter、共享场次/变量提案与显式删除 ID；直接复用最终 Graph 的领域定义，去除 timeline/schedule/resource/API 节点写入协议 |
| review_report | 更新 2.1.0 | 供可选校验 Agent 返回语义发现；Agent 只接收最终 Graph，来源与标准引用由 Harness 绑定，graph_checks 固定为空，metrics 先返回 [] |
| subtask_result | 保留 1.0.0 | 发现、建议、限制、来源和产物引用仍有效；保留原字节 |
| nexo_graph | 新增 2.0.0 | 最终根直接为 Project；规范化严格 profile，保留 camelCase、story、body、scriptInline、DSL 和图关系，可作为整剧 output_type 定义源 |

Step2 的 `source_analysis` 已退出活动 Schema 与阶段绑定，改为 `source_global_analysis` 与 `source_character_analysis` 两份普通文本产物；历史结构化版本仍可读取。`nexo_editor_package` 的历史快照保留，但退出所有活动绑定。

## 新增语义与消费者

`GraphCompatibility`：`status=compatible/requires_lowering/blocked/unresolved`、`lowering_notes[]`、`conflicts[]`。conflicts 用已有设计 ID 定位原始需求、原因和候选解决方法；不编造未来 Graph ID。模型兼容判断不能替代程序检查或用户确认。仅当确实需要用户选择时询问，确定性限制由程序处理。

`GraphCheck`：`check_id/check_kind/status/targets/summary/evidence_refs`；状态为 pass/fail/unverified/not_applicable。`GraphTarget`：`object_kind/object_id/chapter_id/node_id/json_pointer`；路径相对被审固定版本的 Project，必须与真实 ID/归属一致。引用固定版本在报告的 reviewed_artifact_refs 与 evidence_refs 中关联。一次最终 Graph 审核固定一个 Project 快照；对多个版本分别出报告，不能在同一报告混用路径而不区分版本。

GraphCheck 保留既有枚举用于历史兼容，但当前可选校验 Agent 的 `graph_checks` 固定为空。Harness 独立执行 `schema_contract` 和 `unreachable_nodes`，后者检查入口可达性、断裂连线及可证明恒假的条件路径；模型不能签发或覆盖脚本检查结果。

Step9 原作来源、正文段和 flow_links 保留原有独立语义；上游初始值仍为 number/string/boolean，只有最终 Variable.value 转字符串。边上条件和互动效果须显式映射功能节点；如果不能等价实现，登记冲突而非悄悄改变互动体验。Choice 没有失败/超时结果，QTE 成功/失败分别配置，game 当前只有 next 出口。

## 提取边界与一致性修订

- 最终 Schema 包含完整 Project 的必需字段，显式填写选入的可选字段；按五种节点收窄。源接口允许的可选字段被省略不代表与其不兼容，详见 [source-manifest.json](source-manifest.json)。
- 新 Graph 中不会生成媒体时间轴、资源绑定、播放参数、字幕或节点制作事件；制作字段位于固定基线/旁存记录，获准变更叠加回完整编辑器对象时保留。这个扩充对象与经过封闭 Schema 校验的精简 profile 要分开验证。
- Project.id/revision/updatedAt、布局和既有身份是程序控制字段。整剧模式的删除差异在 Harness 外壳核验，逐章模式还需 removed_* 精确匹配差异；两者都不能靠省略删除旧对象。
- current UI 章节图与保存逻辑均用 automaticChapterEdges 顺序链，跨章仅直接下一章。node 类型 jump 的 node 目标限非开始 story，chapter 目标进入下一章入口。Inspector 与 store 对同章开始节点的处理不一致，profile 按稳定交集限制，并记录为消费端升级时复核项。
- 选项/QTE 的目标和可视边双写需一致；continue 不画边且不能暗示 next。含互动的 story 无 next，jump 无普通输出边。章开始无普通入线，边界互斥，章结束出边仅同章 jump。
- 未确认的媒体能力、导入、服务端编译/执行和后端真值不由此 profile 证明；当前只钉住所读前端分支与其领域行为。

## 验证结果

以下为已有离线合同／样例检查记录，见 [validation-report.json](validation-report.json)，执行入口 [validate.py](validate.py)。这些检查不定义首版运行时的质量检查清单；2.1.0 的审核字段结构保持不变，仅同步启用范围描述与注册哈希。

- 13 个活动 Schema 元验证及本地 Agents SDK strict 处理通过；strict 处理没有静默修改结构。
- 160 个本地 `$ref` 可解析，注册文件哈希和绑定一致；chapter_graph 与 nexo_graph 的领域定义完全一致。
- 对 18 个源接口 profile 检查字段归属及必需项，类型另经人工源码审查；未运行 TypeScript 编译。
- 4 个示例结构验证通过；章节/最终 Graph 示例相互一致，原作/方案示例是独立 fixture。
- 样例图的身份、引用、端口、互动位置、顺序章节、变量、可达性和章节删除差异通过；17 个故意错误样例被拒绝，禁用失败而保留合法草稿目标的正例通过。
- 7 个未受影响类型与 v1 保持原字节；v1 全部文件仍符合其既有注册哈希。

以上离线检查不验证真实模型 API、浏览器渲染、完整 DSL 语法/执行、条件可满足性、实体真实存在性、权限/确认、运行迁移或外部服务接入。Harness 与 HTML 的实现及单独验证结果见 [交付状态](../../implementation-status.md)。
