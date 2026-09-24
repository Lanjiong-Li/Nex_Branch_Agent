# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": null, "reason": "input_budget_exceeded"}, {"stage": null, "reason": "summary_failed"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 本工程累计真实 API 请求：23（本轮恢复/运行发出 1）；ModelCall：23。
- 输入 tokens：470061；输出 tokens：49271；推理 tokens：6533；缓存输入：101066。
- 已归档估算费用：USD 2.8707524；用量或结果未知请求：0。
- 活跃验收时间：68.54 秒；本次限制 USD 4.20 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_192458_cd6081f2`；数据库：`branch_agent_local`（保留现场）。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`；Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

本轮恢复Task：["e88c5f84-3fc4-4c91-b6cf-fc9022dddbf7"]。恢复仅调用正常control_task继续入口，复用原确认消息与固定配置；未重建项目或重写模型调用状态。时间在SDK安全边界检查，已发HTTP允许在其180秒timeout内完成，以免测试时钟人为制造未知结果。

详见 [验收回执](../artifacts/live-smoke.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。


Step6 已生成真实完整候选并展示；程序尚未接受模拟确认。协调器触发摘要，首窗口15条原归档加完整材料/信封后33463/32000，在HTTP发出前停止，原Session generation5保留。摘要窗口原固定6000tokens预留不适配实际公共材料大小，等待按实际输入计算窗口后正常恢复。
