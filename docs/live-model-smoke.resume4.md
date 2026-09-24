# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": 7, "reason": "cost_limit"}, {"stage": 7, "reason": "cost_limit"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 本工程累计真实 API 请求：30（本轮恢复/运行发出 7）；ModelCall：30。
- 输入 tokens：643555；输出 tokens：62285；推理 tokens：9512；缓存输入：149026。
- 已归档估算费用：USD 3.7778654；用量或结果未知请求：0。
- 活跃验收时间：204.45 秒；本次限制 USD 4.20 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_192458_cd6081f2`；数据库：`branch_agent_local`（保留现场）。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`；Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

本轮恢复Task：["1cee5ff5-720f-453e-9595-a6d445c5c6cc"]。恢复仅调用正常control_task继续入口，复用原确认消息与固定配置；未重建项目或重写模型调用状态。时间在SDK安全边界检查，已发HTTP允许在其180秒timeout内完成，以免测试时钟人为制造未知结果。

详见 [验收回执](../artifacts/live-smoke.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。


本轮真实验证了摘要自动分窗与完整归档覆盖，已完成Step6模拟确认，进入Step7批处理。在创建下一ModelCall之前因原4.2验收预留上限暂停，全部30次调用均有结果与用量归档。下一轮按明确追加验收预算采用当前schema 7.4美元硬上限；与其他独立attempt费用合计低于8美元，通过正常继续API追加业务预算，不修改已冻结配置。
