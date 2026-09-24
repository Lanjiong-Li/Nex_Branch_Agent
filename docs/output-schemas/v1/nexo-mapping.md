# 章节创作载荷到 Nexo 编辑包的映射

对应 [chapter_graph.schema.json](chapter_graph.schema.json) 的 `payload`。模型返回的通用信封与 Harness 保存外壳不进入最终编辑包。这里定义映射与校验要求，转换器尚未实现。

## 执行语义与覆盖

- 根 `node_changes[]` 提交带 operation=create/update 的节点创作数据；`node_removal_proposals[]` 以及各子项 removal_proposals 明确表示删除提案。全部只用于基于冻结输入构造**新的草稿产物版本**，不删除任何旧版本、不直接写 Nexo、不直接删除持久对象。
- node_changes 是按 ID 的显式增改集合，**不是替换整章节点数组**；未提及节点、事件、分支、选项、互动、媒体一律保留。所有移除都必须在对应 removal_proposals 明示并通过依赖校验。
- update 的已列标量是明确替换值，例如 title/boundary、narrative body/synopsis/scene_ref；scene_ref=null 是明确解除场次引用。需要仅改某个正文时先读当前创作投影，并保留其其他明确标量。种类创建后固定，不能 update 改 kind。
- Harness 的任务范围清单记录计划应覆盖的章节、事件、互动设计意图、既有节点/互动 ID、本批已处理范围、暂缺内容；对分块返回做去重与覆盖检查。最终全章验收作用于“旧快照＋所有已接纳变更”的组装结果，不以一次返回对象数当成完成。模型返回空 changes 不代表整章完成。
- 串行/并发提交前检查 pinned artifact version 和 shared registry version，过期结果按既有运行一致性规则保留为旧依赖草稿，不能覆盖新成果。

## 逻辑身份规则

- *_ref 是 Harness 维护的稳定逻辑 ID，和实际 Nexo ID 的映射由程序保管。现有对象只能复用输入目录里的引用，禁止凭名称重新生成 ID；同名不是同对象。
- 新对象使用预留命名空间，如 `new:node:<slug>`、`new:event:<slug>`、`new:interaction:<slug>`、`new:choice:<slug>`、`new:branch:<slug>`、`new:edge:<slug>`、`new:asset:<slug>`、`new:scene:<slug>`、`new:variable:<slug>`、`new:chapter_edge:<slug>`。slug 在任务内唯一；程序一次分配稳定 ID，并按 run/子任务/逻辑 ref 幂等记录，重试不能复制对象。不要以模型编造的实际 project_id/UUID 当已存在实体。
- 所有引用必须在受信任目录、同批新提案或已接纳前批提案中解析。关键对象的项目归属和是否可写由 Harness 注入与校验，不依赖模型声称。
- shared_entity_proposals 仅新增角色/地点、共享场次、变量；不能用现有 ID 覆盖既有定义。全局 key 或同一拟建实体冲突必须统一解决再接纳，不能让两个章节各建一份。

## 映射表

| 创作载荷 | Nexo 编辑包目标 | 由程序补齐／保留 |
|---|---|---|
| chapter_ref/title/summary | content.chapters[] | project_id、id 映射、审计、sort_order、总览坐标；其余章节不动 |
| node_changes[].kind/title/boundary | content.nodes[] | id/project_id/chapter_id/position/collapsed/sort_order/revision/审计；已有 kind 固定 |
| narrative synopsis/body/scene_ref | nodes[].narrative / scene_id | 解析场次、验证它为项目共享场次 |
| event_writes / removal_proposals | nodes[].events[] | 节点／项目归属、稳定 id、审计；未提及事件保留 |
| interaction_writes kind=choice_group | timeline.elements[].choice_group | element 和 choice ID、config_version、display_config/style_config；choice_writes 按 ID upsert |
| interaction_writes kind=qte | timeline.elements[].qte | gesture.kind → qte.kind；taps 或 direction → parameters；styles/动画/热区走保留或受控默认 |
| schedule_action + schedule | 元素 track_id/start_ticks/end_ticks/active_* | 统一 120000 ticks/s；轨道类型必须 interaction，代码解析已有轨道或分配合法轨道 |
| branch_writes | nodes[].branches[] | id 映射；sort_order/DSL 合法性检查；不把目标填进 branch |
| script_dsl/dsl_version | nodes[].variable | version=1，受信任 DSL 解析/类型检查 |
| game_binding_ref | nodes[].game.url | 只从已有可信游戏绑定解析 URL；模型不能发明 URL，null 可作为编辑草稿 |
| edge_writes | nodes[].edges[] | edge_ref→id，source_ref→source_id，target_ref→target_node_id/target_chapter_id；另一目标字段 null |
| shared_entity_proposals | content.assets/scenes/variables | 新增提案统一审核、去重、ID 分配；默认 styles/voices=[]；不覆盖现有对象 |
| chapter_relation_proposals | content.chapter_edges[] | 原 entry/transition/exit 语义、稳定 ID、project_id 与审计 |
| resource_binding_proposals | 已有 media.resource_key 的显式替换提案 | 只能引用受信任目录中已有 resource_key，检查类型/裁剪/时长兼容，禁止臆造 URL 或媒体事实 |

## 时间轴与媒体保护

- 此投影只创作 choice_group/QTE，不生成 video/audio/image/subtitle 制作清单、资源 URL、媒体事实或虚假成片时长。无资源的纯剧本 Graph 可以是合法编辑草稿。
- `schedule_action=preserve` 仅允许已有互动，schedule=null；原排期、样式、热区、动画全部保留。`set` 要求 schedule 对象并通过区间校验；`unschedule` 要求 schedule 五项全 null，此动作在新草稿中清空该互动排期及依赖排期的热区/active 值，不影响其他媒体。create 必须 set 或 unschedule。
- 构建新互动时，未排期为 track/start/end/active_* 全 null，hit_regions=[]；显示配置从受控默认模板构造，不随意捏造时间。已排期时不允许通过不兼容旧热区/动画校验；需要明确修复或退回未排期草稿。
- 不能将局部创作投影视为完整 timeline 替换。既有 media、subtitle、tracks、canvas/fps、style_config、animations、favorites 与所有未改字段从冻结快照保留。新增 narrative 的默认 timeline 由程序提供；不能对更新节点把未出现在模型输出里的资源清空。
- 已存作品上的媒体绑定替换是额外显式提案；保持原媒体事实与剪辑区间只有在新资源兼容时才成立，不兼容则拒绝该提案，不静默截断/清空。

## 必需业务校验（Schema 无法代替）

1. 唯一 ID、同项目/同章归属、身份映射、引用存在；结构中没有 dangling source/target；类型不可变；变量 key 稳定且初始 JSON 类型一致。
2. 五类 node 的稳定出口齐全且唯一；source_kind/source_ref/result_key 合法；continue 仅 choice/QTE 且 target_kind/ref=null；target_kind node/chapter 不可冲突；QTE 即使禁 failure 仍保留两出口。
3. narrative/game 才可作章开始/结束；每章最多一个开始；终止 completed 与 chapter exit 配对。章结束有效节点目标仅同章 jump。
4. 普通出口仅同章；jump 跨章必须匹配 chapter_edges 的**直接** transition，不按传递可达性放行；chapter target 解析至该章唯一开始节点；entry/transition/exit 的 null 组合和作品入口唯一性检查。
5. 条件分支顺序唯一、首命中语义、default 出口；选项条件与 DSL version 成对；DSL 只读稳定 variable key、条件结果布尔、修改类型正确，不允许任意程序代码。
6. 移除节点/选项/QTE/分支前处理引用与相关出口，不能留下悬空引用，也不能移除该节点类型仍要求的必需出口。
7. 互动 schedule 三项全值/全空；active 成对且落在显示区间；end>start；同一节点所有互动跨轨道不重叠；保持已有媒体真实性，resource_key 必须存在；不得猜测拍摄时长。
8. 依赖已确认方案及章节设计；有正文、有预期互动/路线覆盖、无未解决必需项后才标“剧情 Graph 完成”；编辑 Schema 合法和完整剧情验收不等于发布/播放通过。

最终程序聚合成 format_version=4、scope=project 的完整 EditorPackage。Nexo 无 scope=chapter；也不把创作载荷封套、来源记录、审批状态、run/config/schema 版本写入 additionalProperties=false 的最终包。它们保存在 Harness 的独立产物封套／索引中。最终版本映射可配置，但 Nexo 固定字段/枚举/关系不可被普通 output_type 编辑绕过。

## 资料缺口

提供的 README.md:175 引用 `../dsl-and-routes.md`，该文件在 `/Users/llj/Nexquence/分支Agent` 不存在，Nexquence 下文件检索也未发现。因此以上只承诺四文件明确的 DSL v1 保存格式与基础表达能力；完整词法/语法/保留字/复杂度上限待接入真实规范与校验器。提供目录为复制件，其多处源码相对链接也失效，本次没有将这些链接所指仓库当作已核对源码。
