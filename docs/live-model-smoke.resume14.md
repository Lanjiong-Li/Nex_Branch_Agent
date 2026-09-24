# 真实模型端到端验收

结果：**未完成，供应商 API 余额耗尽**。等待至少 60 秒后的一次受控重试仍返回 HTTP 429，安全错误字段为 `code=credit_balance_exhausted`、`type=insufficient_quota`。已停止全部 API 请求；没有最终 Nexo Graph、Step11 审核或成功下载文件。

## 已完成与保留进度

- 原创短篇《一盏灯》，单章、玩家阿澄、一个关键选择、两个安全结局。
- Step1–9 已完成；需要确认的阶段均经真实 `submit_message`、协调模型与程序确认协议记录。确认者为**自动化验收模拟用户**，只对隔离测试项目有效，不能当作生产项目实际人类审批。
- Step9 完整章节正文固定引用：`30cd9751-cb9c-4df4-bcf0-32641fafc542` v1，已确认、依赖有效。
- Step10 首次模型响应已返回并执行两个只读工具，调用／结果配对完整；随后第二模型轮被完整输入预算门禁暂停。原始响应、不可变历史、SDK 工作项、固定产物、确认和检查点均已保存。
- 正常恢复前的摘要请求遭上述明确余额拒绝，未把失败调用或未审核产物算作成功。不存在未知调用结果。

## 实际调用与费用

| 项目 | 记录 |
| --- | --- |
| 模型／推理 | `gpt-5.6-sol` / `medium` |
| 当前隔离项目请求 | 80 次：78 次供应商成功响应、末 2 次明确 HTTP 429 拒绝 |
| 输入／输出 tokens | 2,137,452 / 197,341 |
| 缓存输入／推理 tokens | 205,638 / 26,991 |
| 当前项目累计估算费用 | USD 13.6879112 |
| 前两次独立尝试估算费用 | USD 0.5585212（8 次请求） |
| 所有尝试合计估算费用 | **USD 14.2464324** |
| 验收总上限／当前项目硬上限 | USD 20.00 / USD 19.40 |
| 当前项目剩余硬预算 | USD 5.7120888，发送前仍需满足单次保守预留 |

费用按固定计价记录和真实 API usage 计算，属于估算，不是供应商账单。供应商余额不足独立于 Harness 尚有的验收预算余量。所有原始回执保留；最后一次重试只有失败响应，没有新增 token 或费用。

## 配置与本轮验证边界

前段按原阶段配置运行。后段通过正常 `ConfigService.draft → validate_values → publish` 为**本项目**的 coordinator、Step8/9/10/11 和 aux.summary 发布 `context.input_token_cap=64000`、`context.history_token_cap=12000`，原项目其他字段与全局／Step1 默认不变。已启动 Run 的冻结快照不回写，新任务才采用新覆盖；不能宣称本次全流程均在默认 32k 下完成。具体前后配置引用见 [配置发布记录](../artifacts/live-smoke.config-publication.json)。

真实流程已经覆盖固定候选确认、批次问答、暂停／正常继续、保存结果复用、摘要分页恢复、错误引用有限修复和真实项目配置发布。最后修复的摘要投影只移除向摘要模型发送的 `reasoning.encrypted_content` 密文副本，保留实际公开 summary 与固定原档引用；原始响应和 SDK 工作项密文不变。此修复通过聚焦回归和独立审查；余额阻断使其恢复后的真实摘要尚未完成。完整回归由主任务报告为 163 项通过。

本样本尚不能证明最终 Graph 生成、审核、交付或下载成功，也不能证明长篇与并发质量。未用离线 mock 或手工 JSON 代替真实交付。

## 正常恢复

先确保所用 API 账号已经补充可用余额。**不要新建项目，不重跑 Step1–9，不直接改数据库状态。** 当前测试工程在独立 PostgreSQL schema，主应用 `8767` 的默认数据库空间看不到它；不要在主应用里寻找该隔离项目。

- 数据库：`branch_agent_local`
- schema：`live_smoke_20260920_192458_cd6081f2`
- Project：`dcfc7a66-1c6c-498c-bb4b-17ac0047900e`
- Conversation：`5beadfdb-262e-446e-8e79-cb91d7314e23`
- 暂停 Step10 Task：`0e509c0b-bd1e-4e87-8171-e007230670ab`
- 章节：`chapter-01-one-lamp`
- 工作 Session：`b7e76472-88c0-43c4-9f3b-5840d731fe28`

余额恢复后，在该项目目录执行以下正常恢复命令。脚本读取不可变回执定位既有工程，再通过 `control_task(..., 'continue')` 继续暂停任务；保留自动化测试身份、固定配置、真实确认与剩余验收费用上限：

```bash
cd /Users/llj/HKU/Agent_Harness_Develop
LIVE_SMOKE_RESUME_RECEIPTS=artifacts/live-smoke.resume14.receipts.json \
LIVE_SMOKE_MAX_COST=19.40 \
LIVE_SMOKE_MAX_SECONDS=600 \
.venv/bin/python scripts/live_smoke.py
```

发送前检查已知调用费用和保守预留；若仍有额度不足、未知结果或实际预算问题则停止。600 秒在调用边界检查，允许已发 HTTP 在既有 timeout 内归档。成功交付后才会创建 `artifacts/live-smoke.nexo.json`；当前没有该文件。

完整现场见 [当前验收回执](../artifacts/live-smoke.receipts.json) 和 [最后不可变恢复回执](../artifacts/live-smoke.resume14.receipts.json)。历史 attempt1/2 与 resume1–13 回执保留，供区分已修复集成问题和最后的外部余额阻断。
