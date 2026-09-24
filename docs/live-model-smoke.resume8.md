# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": null, "reason": "user_stop"}, {"stage": null, "reason": "summary_failed"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 本工程累计真实 API 请求：42（本轮恢复/运行发出 5）；ModelCall：42。
- 输入 tokens：961174；输出 tokens：95901；推理 tokens：13080；缓存输入：149026。
- 已归档估算费用：USD 6.0382444；用量或结果未知请求：0。
- 活跃验收时间：167.17 秒；本次限制 USD 7.40 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_192458_cd6081f2`；数据库：`branch_agent_local`（保留现场）。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`；Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

本轮恢复Task：["657eae5f-101e-4360-be98-f3e01b646328"]。恢复仅调用正常control_task继续入口，复用原确认消息与固定配置；未重建项目或重写模型调用状态。时间在SDK安全边界检查，已发HTTP允许在其180秒timeout内完成，以免测试时钟人为制造未知结果。

详见 [验收回执](../artifacts/live-smoke.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。

本轮新发 5 次真实 API：第四页摘要修复 1 次、协调回复 1 次、第二个协调任务的摘要页 3 次。前三页原摘要由正式 `session.summary_page_reused` 事件证明复用，未重新计费。末页修复后 Session generation 7→8。协调回复将继续目标指向顶层工作流，未将已提交的问题答复传给等待批次；发现此问题后，使用正常 stop 控制停止第二个协调任务，并让已发 HTTP 完成归档。第二条答复和三个新摘要结果均保留，未知结果为 0。后续需要修复父任务继续的问答路由再正常恢复；本轮没有再次确认任何产物。
