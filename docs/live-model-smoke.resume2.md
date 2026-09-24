# 真实模型端到端验收

结果：**blocked**。SmokeLimit: runtime_blocked:[{"stage": 6, "reason": "input_budget_exceeded"}]

- 模型：gpt-5.6-sol；reasoning：medium。
- 本工程累计真实 API 请求：22（本轮恢复/运行发出 3）；ModelCall：22。
- 输入 tokens：445135；输出 tokens：44114；推理 tokens：6150；缓存输入：101066。
- 已归档估算费用：USD 2.6429854；用量或结果未知请求：0。
- 活跃验收时间：39.35 秒；本次限制 USD 4.20 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_192458_cd6081f2`；数据库：`branch_agent_local`（保留现场）。
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`；Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

本轮恢复Task：["e88c5f84-3fc4-4c91-b6cf-fc9022dddbf7"]。恢复仅调用正常control_task继续入口，复用原确认消息与固定配置；未重建项目或重写模型调用状态。时间在SDK安全边界检查，已发HTTP允许在其180秒timeout内完成，以免测试时钟人为制造未知结果。

详见 [验收回执](../artifacts/live-smoke.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。


本轮完成两个真实批次的完整覆盖，原批次恢复复用保存结果，未重复调用模型。Step6父汇总仍超预算，定位为证据递归把已固定方案v2之外的旧v1正文重新装入：输入33595/32000。修复仅将这种自动追溯旧版保留为可检索的固定历史引用；必需当前材料和覆盖门槛不变。修后静态预检28252/32000，保留记录见 `artifacts/live-smoke.step6-aggregation-preflight.json`。
