# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": 2, "reason": "missing_material_field"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 真实 API 请求：5；ModelCall：5。
- 输入 tokens：35792；输出 tokens：14942；推理 tokens：1525；缓存输入：6587。
- 已归档估算费用：USD 0.4474848；用量或结果未知请求：0。
- 活跃验收时间：146.87 秒；本次限制 USD 4.80 / 550 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_191930_3bd89f5f`；数据库：`branch_agent_local`（保留现场）。
- Project：`f3193723-a12f-4868-b481-9fbcd2f26d79`；Conversation：`95e8e8c3-67bf-4d0d-a77d-a1e422b6fe44`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

详见 [验收回执](../artifacts/live-smoke.attempt2.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。

真实阻断定位：Step2 首次输出 ready 但 questions 非空，程序拒绝并发起第1次真实自动修复；修复结果已变为 ready/questions=[]。随后引用验证发现 runtime.task_frame 的 RuntimeEvent 引用了 `/goal`，但实际保存路径为 `/payload/content/goal`。模型输入中的字段路径使用了局部路径，审计selection则使用完整路径；两者不一致导致暂停。应统一模型可见引用路径，不能放宽引用验证。

首次尝试归档：[Attempt 1](live-model-smoke.attempt1.md)。两次合计：8个真实请求，约196.89秒活跃；当前归档估算费用约USD0.5585212（首次未单列缓存写入倍率）。本轮未生成待确认候选，因此尚无模拟确认消息；未生成最终Graph。
