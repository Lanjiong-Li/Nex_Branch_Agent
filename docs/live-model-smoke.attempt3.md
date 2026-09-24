# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": null, "reason": "input_budget_exceeded"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 真实 API 请求：14；ModelCall：14。
- 输入 tokens：253638；输出 tokens：35341；推理 tokens：4451；缓存输入：12793。
- 已归档估算费用：USD 1.9161202；用量或结果未知请求：0。
- 活跃验收时间：402.38 秒；本次限制 USD 4.20 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_192458_cd6081f2`；数据库：`branch_agent_local`（保留现场）。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`；Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

详见 [验收回执](../artifacts/live-smoke.attempt3.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。

真实阻断：Step5已经真实生成，确认协调任务在成功摘要后仍超过32k输入上限。原因是旧版/其他Session摘要自动混装、当前摘要在history和materials重复，以及完整历史Task记录膨胀。修复后只读预算预检为23408/32000 tokens，正式Decision约束保持完整。所有14次调用结果与用量可核对，将用同一Task的正常继续入口恢复，不重新生成已完成阶段。
