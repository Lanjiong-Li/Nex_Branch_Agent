# 分支 Agent 本地版实施计划

> **For agentic workers:** Use superpowers:subagent-driven-development to implement and review the assigned modules. 用户已确认交付范围并授权实施；不再增加计划审批节点。

**Goal:** 在本机通过真实 Agents SDK 完成线性原作到互动剧本 Project JSON 的可追溯改编。

**Architecture:** FastAPI 提供同源页面、账号适配及契约接口；独立可恢复调度器运行 Agents SDK；PostgreSQL 与持久文件保存真实记录。网页用原生 ES modules，减少本地环境和部署依赖。

**Tech Stack:** Python 3.10+、openai-agents 0.22.2、FastAPI、psycopg3、PostgreSQL 16、HTML/CSS/JavaScript。

**Spec:** [主开发文档](分支Agent开发文档.md)、[存储契约](harness-data/storage-and-api.md)、[运行契约](runtime/运行状态机.md)、[上下文执行](context/上下文执行方案.md)、[输出契约](output-schemas/v2/README.md)。

## Global Constraints

- 1 个协调 Agent、专用的 Step1 双分支 Agent 与 Step2 知识资产 Agent；结构化阶段输出采用活动的 16 种 Schema。
- 默认 gpt-5.6-sol / medium；Step1 原文200000、输入256000、输出32000、安全余量2000 tokens；其他阶段输入32000、输出8000。
- Step1 全文同时输入，不分批、不摘要替代、不截断；压缩保留原始归档。
- 人工确认基于实际用户消息、固定版本及明确范围；Step11通过后自动交付同一候选。
- schema_contract 与 unreachable_nodes 为两项脚本产物检查；可选校验 Agent 默认关闭，仅在 instructions 非空时读取最终 Graph；不扩展 Harness 效果评测。
- 原始记录不可变，草稿与生效版本分开；所有公开读取都按账号及项目授权。
- 本地测试身份与持久化先交付；真实平台认证和服务器部署随后接入。
- Task 初始累计执行上限3600秒、费用20 USD；显式继续建议追加相同额度，可在配置页调整。未知价格不当零费用。

## Review Focus

1. 重复提交与并发确认不重复推进；数据库幂等和CAS测试。
2. 用户中途停止、服务崩溃后不丢控制意图；runtime恢复及过期租约测试。
3. 原文内伪指令或XSS作为数据；模板边界与浏览器渲染测试。
4. 旧版确认及Schema变更不批准新内容；固定版本和发布兼容性测试。
5. 审核的候选与下载字节一致；组装/审核/交付测试。

## 模块任务

### 1. 持久化与记录

文件：branch_agent/storage.py、records.py、migrations/001_initial.sql、tests/test_storage.py。
接口：Store(dsn,blob_dir).transaction/put/get/list/update/scan/list_projects；内部projection_get/put、advisory_lock；records.new_record/scope/usage/budget。
- [x] 为隔离、CAS、不可变历史和事务回滚建立PostgreSQL集成测试。
- [x] 实现22类记录表、Schema验证、固定版本引用、Blob及内部调度投影。
- [x] 执行 `python -m pytest tests/test_storage.py` 并复核跨项目读写。

### 2. 模型、上下文与配置

文件：branch_agent/schemas.py、configuration.py、context.py、model_service.py、prompts.py；tests/test_models.py。
接口：SchemaCatalog.validate；ConfigService.resolve返回ConfigVersion；ModelService.run(stage,task,run,session,config,materials,message,control)返回严格结构化结果。
- [x] 验证Schema绑定、配置继承、预算和全文保留的失败案例。
- [x] 实现15种output_type适配、动态提示词、三个只读工具、持久Session与调用审计。
- [x] 实现配置草稿、校验、发布、固定运行快照及材料字段绑定。
- [x] 运行真实SDK与模拟HTTP供应商的适配测试。
- [x] 真实模型最小联调成功；修复终端遗留key覆盖项目.env的配置问题。

### 3. 流程、恢复与Graph

文件：branch_agent/engine.py、workflow.py、graph.py、graph_contract.py；tests/test_runtime.py、test_graph.py。
接口：Engine.start/stop/submit_message/import_source/status/control_task/resume_queue；SchemaCatalog与Store按上述接口接入。
- [x] 为确认等待、重复消息、停止与故障续跑编写行为测试。
- [x] 实现Step1–11、逐章循环、依赖变更、Steer/Queue和持久调度。
- [x] 实现Graph候选组装、两项校验、固定候选审核和同版本交付。
- [x] 为 Step10／Step11 提供固定版本的 Nexo DSL 创作契约，验证旧配置兼容与已保存运行的不可变材料恢复。
- [x] 运行恢复和Graph测试，核对生产路径不调用fixture模型。

### 4. 页面与API

文件：branch_agent/app.py、auth.py、static/*；tests/test_api.py。
接口：/api/branch-agent/v1，按存储接口契约；本地身份采用签名会话与CSRF令牌。
- [x] 验证未认证、跨项目读取及重复提交被正确处理。
- [x] 实现对话、配置编辑、15种Schema字段树、22类记录导航与图表。
- [x] 浏览器实测原作上传、配置发布、历史与数据查看；API／工作流测试覆盖确认及固定JSON交付。
- [x] 真实 API／Engine 驱动原作到最终产物，完成 Step10 返修、Step11 独立审核及同版本交付；Chrome 核对真实会话并下载固定 Graph v11，下载字节 SHA-256 与保存版本一致。

### 5. 集成与交付

文件：pyproject.toml、scripts/start.sh、scripts/worker.sh、compose.yaml、.env.example、README.md、docs/implementation-status.md。
- [x] 本地安装启动、数据库迁移、连接现有环境但不输出密钥。
- [x] 运行离线契约校验、数据库/SDK/API测试、浏览器操作。
- [x] 短剧本真实模型测试：独立隔离环境完成《一盏灯》Step1–11；补足余额后复用原项目、确认和运行历史，审核通过后交付最终 Graph，证据见交付记录。用户确认由验收脚本明确模拟，不代表真实人工审核。
- [x] 对照规格审查，修复阻断问题；在交付状态文档区分当前边界与完整设计。
- [x] 提供启动入口、测试项目、Graph示例JSON与部署说明；测试项目与示例不冒充真实模型产物。

## 接口交叉检查

| 模块 | 依赖 | 处理 |
|---|---|---|
| 存储→运行/API | 严格记录与内部投影 | 业务记录不增加任意字段；投影单独持久化 |
| 模型→运行 | 结构化结果和异常 | ModelService不签发业务确认，Engine执行控制 |
| 运行→模型 | 固定Run/Session/配置/材料 | 每次调用保存真实输入和用量 |
| API→页面 | 同源REST/SSE | 错误统一error.code/message，修改带幂等键和CSRF |
| Graph→审核 | 固定候选版本 | 先组装候选再审核，交付不重生成 |

Ruling: 现有目录不是git仓库；实现直接放在该项目新增branch_agent包，不修改SillyTavern或学习脚本，不自动提交已有文档和密钥。
