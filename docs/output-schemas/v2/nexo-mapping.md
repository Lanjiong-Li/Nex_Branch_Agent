# 章节产物到编辑器 Project

本文件定义 [chapter_graph](chapter_graph.schema.json) 到 [nexo_graph](nexo_graph.schema.json) 的组装及消费要求。字段来源固定为 [source-manifest.json](source-manifest.json) 的源码版本。[组装器](../../../branch_agent/graph.py) 与 [SDK 输出适配器](../../../branch_agent/schemas.py) 已实现；本地应用交付 Project JSON，Nexo 平台自动写入及通用旧结构迁移不在首版范围。

## 最终合同

`nexo_graph` 根就是 `Project`，采用现有字段和类型的规范化 profile，嵌套 `chapters[].nodes/edges`，共享 `scenes/variables`。当前 Schema 可直接作为最终输出类型的定义源，不另套 `payload`。内部审核证据和确认记录存于外部产物封套，不能污染根对象。

| 内容 | 编辑器位置 | 字段来源与约束 |
| --- | --- | --- |
| 名称／概要 | Project.name/description，Chapter.title/summary | 已确认方案及章节正文 |
| 剧情 | kind=story，title/synopsis/body/sceneId | body 是完整自由文本；sceneId 指项目全局场次，空字符串表示无关联 |
| 选择 | choiceGroups[].choices[] | 组／选项独立稳定 ID；action、targetNodeId 表达结果；没有选项失败/超时 |
| QTE | qtes[] | 成功、失败分别 jump/continue；failureEnabled 控制失败边是否有效；没有轨道、时间、热区、手势制作参数 |
| 剧本插入点 | choiceGroups/qtes 的 scriptInline | version=1；afterLine 为 body.split("\n") 的 0 基行索引，order 为同位置次序；不得把 UTF-16 偏移或毫秒直接填入 |
| 条件 | kind=condition，branches[] | conditionDsl 权威；mode='and'/conditions=[] 仅为当前接口兼容形状；数组顺序优先，default 出口独立 |
| 变量修改 | kind=variable，scriptDsl | 以全局 key 写 DSL；operations=[] 不维护第二份可编辑状态修改定义 |
| 状态定义 | Project.variables[] | id/key/name/description/type/value；type=number/string/boolean，但 value 都是字符串；数值合法有限、布尔仅 true/false、文本不额外 JSON 加引号 |
| 场次 | Project.scenes[] | 非章节私有；字符/地点 ID 须解析到可信实体目录；兼容显示字符串由程序与 ID 名称目录同步 |
| 游戏 | kind=game，gameUrl | 从可信游戏目录解析；此模型仅 next 出口，缺失 URL 不能当作已配置可执行游戏 |
| 节点跳转 | kind=jump，jumpTarget | kind=node 只指同章或下一章的非开始剧情节点；kind=chapter 指下一章入口；不画普通出边，不推断变量恢复 |
| 章内可视连线 | Chapter.edges[] | id/source/sourcePort/target；普通节点 next，条件 branch.id/default，选项 choice.id，QTE qte.id 或 qte.id+:failure |
| 章节链 | Project.chapterEdges[] | 当前源码自动按章节数组生成 begin→首章→…→末章→end；使用 automaticChapterEdges 的 auto: ID |

## DSL 创作契约

[nexo-authoring-contract.json](nexo-authoring-contract.json) 提供变量修改与条件表达式的精确语法、类型限制及外层 JSON 转义示例。它与 JSON Schema 配合使用，不能只凭 `scriptDsl`／`conditionDsl` 的 string 类型推测语法。

变量修改直接引用全局 key，例如 `selected_route = "route_a";`、`battery -= 1;`；条件是无分号的布尔表达式，例如 `battery >= 1`。一次性资源先检查后扣减，变量类型及路线值来自已确认设计。契约中的示例不是项目变量定义，不得据此新增剧情或改写已有类型。

Step10 模型只接收已确认章节设计、对应原作正文及 instructions，输出受 `chapter_graph` output_type 约束。Graph 基线与写入上下文由 Harness 内部固定、校验和组装，不进入 Step10 模型材料。Step11 审核仍可读取程序固定的创作契约及其哈希；这些内部记录不进入最终 Project 根对象。

## 字段所有权

1. 模型创作正文、标题、梗概、互动意图及 DSL；已有内容变化仍受本次修改范围约束。
2. 已有对象 ID 从固定输入目录复用，新对象使用程序预留 ID 或任务局部 ID，程序幂等解析所有引用；项目身份、对象归属、`revision/updatedAt` 来自程序。它们出现于模型 JSON 并不产生任何签发或覆盖权限。
3. `x/y/collapsed` 是领域必需字段。新对象由程序布局或传入默认值；既有对象保留原值，除非任务授权布局变更。模型输出的坐标不作为程序计算的权威来源。
4. `prompt` 保留已有 Project.prompt 或程序提供的初始值；它不允许模型改写 Harness instructions。
5. 完整 Project 的模型输出需复制程序提供的固定字段并通过控制字段比对；默认逐章流程由程序组装这些字段。任何修改都形成新草稿、记录固定输入版本和配置，不直接写外部 Nexo。
6. 当前 profile 不含可选的 node.revision/completionPort/copySourceId/copyIds/subtitles/events 和 Project.chapterCanvas。读取已有工程时，先投影创作字段；消费新结果时按 ID 合并回原领域文档并保留未选字段。尤其字幕不能被当成空数组覆盖。不可把精简 JSON 直接整份替换已有编辑器状态后再保存。

经过窄 Schema 校验的是可交付 Graph profile；恢复额外字段后的完整编辑器对象按完整领域模型及服务接入规则验证，不再要求通过这个 `additionalProperties=false` 的窄 Schema。保留字段来自固定原始快照或旁存记录，不进入模型创作。

整剧 `nexo_graph` 模式本身没有 removed_*。Harness 必须在外壳记录相对完整 Project 基线的章节、节点、边、互动、场次、变量新增/修改/删除差异，核对真实授权与修改范围后才接受新版本；缺席仍不等于删除授权。这与单章显式移除清单具有相同保护力度。

## Step10 完整章节快照

阶段结果仍使用普通信封，payload 固定为：

```text
chapter: Chapter                  完整章节内容与顺序
shared_scenes: Scene[]             已引用共享场次的新建/变更提案
shared_variables: Variable[]       已引用共享变量的新建/变更提案
removed_node_ids: string[]         相对冻结章节版本明确移除的节点
removed_edge_ids: string[]         相对冻结章节版本明确移除的边
```

组装必须遵守：

- 一次 ready 对应该章完整快照，保留未修改节点、互动、连线及顺序。长章可先经局部子任务和草稿分批完成，但未覆盖全章时不能把局部数组当作可替换的 ready 章节；程序依据覆盖清单合成全章后再验收。
- `old_node_ids - new_node_ids` 必须精确等于 removed_node_ids，边亦同；清单不包含新建或仍保留的 ID。列表只是删除提案，还要核对实际用户授权及本次修改范围、受影响引用和已确认内容。节点内互动/分支删除通过同样的前后差异及授权检查，不能静默省略。
- 已有节点类型固定；更换类型需另建对象并显式处理旧对象与引用。不得用同一 ID 改 kind。
- 单章合并只替换目标章节的已批准创作字段；不删除其他章节、不改变其顺序。新建章节位置由冻结任务清单给出；没有位置则不能根据模型返回先后决定顺序。
- shared_scenes/shared_variables 按稳定 ID 协调提案，不是共享集合全量替换；省略不删除。已有定义相同则复用，改变则需核验授权、类型和所有引用影响，新定义去重并分配身份；冲突不能最后写入者获胜。
- 新建角色／地点由上游设定及独立项目实体目录承接；场次引用经目录解析后写入 characterIds/locationIds。Graph 本身不承担人物、地点完整档案或媒体资产包。
- 单章可暂时引用已登记但尚未生成的下一章身份，此时只完成本章检查；最终 Project 必须能解析全部目标及目标章入口，缺少章节时不能认定整剧完成。
- 内容版本、章节基线、共享目录及 Schema/映射版本在执行时固定；过期结果保存为旧依赖草稿，不覆盖现行版本。组装新 Graph 后执行最终验证；创作变更按开发文档的阶段规则确认。Step11 审核通过且交付条件满足后自动交付，不增加审核报告或整剧确认。

## 路由唯一含义与一致性

当前编辑器对 choice/QTE 同时保存目标字段和可视边，两份必须一致；发现冲突直接报错修复，不能任选一份覆盖。最终所有对象保留稳定 ID。

- 普通同章边必须引用存在的有效源端口和同章目标；一个端口至多一条边，不允许直接自己连自己或连到章节开始节点。
- choice/QTE 的 jump 结果在完成稿中有合法同章 targetNodeId，并有相同目标的可视边；continue 的目标为空字符串且无可视边。continue 只继续当前节点，不自动生成 next。
- QTE 禁用失败没有失败边，可以保留已解析的失败草稿目标；之后启用时重新检查。新无失败互动采用程序默认 continue/空目标。
- 含任意 choiceGroups/qtes 的 story 关闭 next，即使所有结果是 continue；不能为退出节点偷偷创建这个出口。设计如果需要离开，必须已有可表达的互动 jump 或明确调整分段/互动设计并保持原意。
- 条件节点分支数组决定优先级，各 branch.id 和 default 是独立出口；变量与游戏节点使用 next。不能在通用 Edge 中加入 condition/effects。
- chapterStart/chapterEnd 互斥；完整章节必须有且仅有一个开始节点，编辑中可无；仅 story/game 可用边界。章结束的有效出边只能指向同章 jump；无后继的终止要符合已确认结局设计，不能把所有 chapterEnd 都解释为整剧结局。
- jump 节点通过 jumpTarget 保存目标，没有可视出边；kind=node 只允许非开始的 story 节点且不能指自己，范围为同章或下一章；kind=chapter 进入下一章入口。当前 Inspector 允许选择同章开始剧情节点，但 store.change 会清空任何开始节点目标；本 profile 按两者可稳定保存的交集约束，源实现统一前不承诺这种回跳。跨章只能到章节数组直接下一章，不承诺任意章节 DAG、跳章、回前章。章内合法多节点循环可以存在，但不会自动恢复变量快照。
- 变量效果和解锁条件需要显式 variable/condition 节点。需要“仅修改变量后继续同一节点”或动态隐藏选项而无法在当前领域形状中等价表达时，登记能力冲突，不能新增不存在的效果字段或悄悄改变体验。

## 正文覆盖与程序验收

原作 UTF-16 锚点、Step9 有序正文段和 Step10 的 body 分工不同。段落可以拆分或合并为 story 节点，程序保存来源覆盖映射；已确认正文的有效事实和要求必须保留，新增分支的实际完整文字必须写入 body。剧本阅读顺序采用章节和 nodes 数组，执行路径采用路由，不按图遍历来替换编排顺序。

产物质量脚本验收执行 Schema 与不可达节点检查；后者从章节入口遍历连线，并对受支持的条件和变量脚本做保守静态执行，无法安全解析的 DSL 按可能可达处理。可选校验 Agent 默认关闭，启用时只读取最终 Graph，且 `graph_checks=[]`。版本、授权和确认规则继续由 Harness 执行。

`validate.py` 只实现合同与离线样例的一部分检查，覆盖明细见 validation-report；不是正式权限/确认/DSL/内容审核实现。

## 旧产物与后续服务接入

v1 原文件不改写。旧 chapter_graph 创作投影不能只改 schema_version 后当作 Chapter；旧媒体排期与资源绑定不会进入当前 Graph。迁移必须解析身份、正文、互动、DSL 和出口，生成符合领域形状的全章，再按当前章节链、scriptInline、控制字段与确认范围复核，无法映射的内容保留为历史材料并报告；本轮没有迁移已有真实业务产物。

将 Project 保存到现有 Nexo 服务属于后续适配工作：story↔narrative、嵌套章节↔平铺 nodes、可视边/跳转目标↔完整路由、choiceGroups/qtes↔未排期 timeline elements。原媒体、样式、字幕、时间轴及审计从冻结服务端数据保留，不能拿精简 Graph 当完整 PUT 请求。该适配和发布播放不属于当前最终产物的 output_type。
