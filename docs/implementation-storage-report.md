# PostgreSQL 存储实现与验证

实现文件：`branch_agent/storage.py`、`branch_agent/records.py`、`migrations/001_records.sql`、`migrations/002_relationships.sql`。

## 已实现

- 22 类独立记录表、记录索引、逻辑身份、关联投影、运行投影和版本／幂等业务键索引。JSON 为权威记录，投影字段由数据库生成或触发器同步。
- 严格内部 Schema 校验、固定版本引用解析、项目复合外键与类型检查、连续内容版本、正确的父版本、产物／配置／实际输入哈希校验。
- 每线程数据库连接、嵌套事务保存点、外层提交前关联校验、事务级 advisory lock、CAS 乐观锁。普通更新只允许各类记录的可变字段。
- 原始历史等只追加；数据库触发器同时阻止直接 SQL 覆写。仅允许删除可重建 SessionItem，不删除对应原始历史。
- Blob 独占创建、文件 fsync、内容哈希与长度校验、ready 后禁止覆写；后续读取再次校验文件完整性。正文不能引用尚未 ready 的 Blob。
- `get/list` 始终按项目读取；`project_id=None` 仅对应全局配置。项目列表使用可信账号过滤，`scan`、运行投影与锁为内部接口，不直接暴露为页面的跨项目接口。
- 记录构造器只初始化公共字段、明确空值与初始状态，不伪造来源、内容或关联身份；未知用量保持 null。预算 helper 默认 3600 秒、20 USD。

`new_record(record_kind, project_id, **values)` 使用 `record_kind` 参数名，允许 HistoryRecord 正常传入 `kind="model_output"`。ContextSnapshot 哈希对象为三个实际 Content 信封：`instructions`、`input_items`、`tool_definitions`，使用 UTF-8、键排序且无多余空白的 JSON。

## 验证

使用真实本机 PostgreSQL 数据库 `branch_agent_local`；存储测试每例创建独立 schema，结束只删除自身 schema。测试数据由各测试明确构造，不将文档合成记录用作运行数据。

- `.venv/bin/python -m pytest tests/test_storage.py -q`：19 项通过。
- `.venv/bin/python -m pytest -q`：本次完整运行 34 项通过，26.45 秒。

存储测试覆盖多项目隔离、数据库直接 SQL 跨项目拒绝、并发 CAS 仅一个写入者成功、嵌套／外层事务回滚、历史不可变、Session 工作副本删除、Blob 不可覆写及损坏检测、全局配置范围、配置与输入哈希、旧基线派生新版本、数据库 UTC 时钟和事务运行投影。

## 服务边界

存储接口由已认证的服务调用；账号认证、用户确认意图、实际依赖语义、任务状态机、每次 worker 的租约授权以及业务 payload 专用处理器由对应服务执行。存储不会从合法 Schema 推断这些业务事实。尚无验证恢复链适配器时，跨 Run 的 ToolCall 写入明确拒绝；不会复制或伪造原始 ModelCall 以绕过检查。

文件写入成功而数据库事务回滚可能留下无引用 Blob 文件；不在运行路径自动删除文件，避免误删仍被外层事务使用或已有归档引用的内容。生产部署需配置持久 Blob 目录与数据库／文件联合备份。
