# 真实模型端到端验收

结果：**blocked**。workflow_scope_exhausted_before_graph: coordinator returned stage=1 for the full-workflow request; root stages=[1] completed without Step2 or Graph delivery

- 模型：gpt-5.6-sol；reasoning：medium。
- 真实 API 请求：3；ModelCall：3。
- 输入 tokens：20179；输出 tokens：2688；推理 tokens：514；缓存输入：6511。
- 已归档估算费用：USD 0.1110364；用量或结果未知请求：0。
- 活跃验收时间：50.02 秒；限制 USD 5 / 600 秒。
- 隔离 PostgreSQL schema：`live_smoke_20260920_191331_e24ef49d`；数据库：`branch_agent_local`（保留现场）。
- Project：`32e42d89-a43d-4ca0-8947-7db51b03324a`；Conversation：`39d9fb90-d5b9-47ab-b701-9952d9eeeeb1`。

故事为本次验收原创《一盏灯》，一章、玩家阿澄、一个关键选择、两个安全结局。所有创作与审核产物必须来自真实ModelService / Agents SDK调用；脚本只导入原作、提交请求和模拟用户确认，不直接修改确认状态或注入模型产物。

所有确认消息明确标记为“自动化验收模拟用户”，**不代表实际人类审核批准**。这是单个短篇样本，不能证明长篇、并发或所有故障恢复均可用。费用为当前配置费率估算，不是供应商账单；发送前按保守输入估计与输出硬上限预留预算，未知结果会停止后续请求。

详见 [验收回执](../artifacts/live-smoke.attempt1.receipts.json)，包括真实调用、状态、模拟会话与预算预留。未交付最终Graph，未生成成功导出文件。

真实阻断定位：协调 Agent 的回复文字识别了“首次整剧互动改编”，但结构化 task_requests 中 stage=1。运行器据此保存 stages=[1]，完成该唯一阶段后正确地结束了这个范围；整剧授权没有被正确映射为全流程范围。需要修正协调指令/契约对 stage=null 与单阶段的含义，再重新验收。

费用局限：供应商此次返回 cache_write_tokens；当前调用费用估算未单独应用缓存写入倍率，因此此金额不是最终账单。发送前预算预留使用双倍输入估计，涵盖了这个额外倍率。
