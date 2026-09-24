# Nexo 分支 Agent

使用 OpenAI Agents SDK 的本地互动剧本创作应用：通过对话提交原作、讨论改编、确认阶段产物，最终查看、复制和下载 Nexo `Project` JSON。

## 本地启动

需要 Python 3.10+ 和 PostgreSQL 16。已有 `agent` 环境可以作为 Python 来源；依赖安装在项目 `.venv` 中。

```bash
cd Nex_Branch_Agent
conda activate agent
python -m venv .venv       # 仅首次创建；当前交付已安装依赖
.venv/bin/python -m pip install -e '.[test]'
createdb branch_agent_local   # 仅首次创建
./scripts/start.sh
```

打开 <http://127.0.0.1:8767>。首次启动在 `.data/local-login.txt` 生成本地登录信息，文件权限为600；账号身份、签名密钥与项目历史会跨重启保留。该登录适配器供本地开发使用，正式平台认证按后端接口替换。

服务端读取项目根目录 `.env`。API key 优先使用项目文件中的 `OPENAI_API_KEY`，避免终端遗留变量覆盖；没有项目 key 时使用环境变量。数据库、端口等其他配置仍是环境变量优先。不要覆盖现有 `.env`；按 `.env.example` 补齐：

```dotenv
OPENAI_API_KEY=你的有效API密钥
DATABASE_URL=postgresql:///branch_agent_local
```

可选 `BRANCH_DATA_DIR` 指定持久目录，`BRANCH_PORT` 更换监听端口。密钥只存在于服务端，不进入页面配置或模型上下文。认证失败会明确暂停，不能靠重复重试修复密钥。

## 使用

1. 创建项目及会话，粘贴原文或导入 UTF-8／UTF-16 LE TXT、Markdown、DOCX。解析后的原文保存为固定版本。
2. 在对话中提出改编任务。程序按既定流程准备材料并调用专业 Agent；需要选择或确认时在对话中回答。
3. 可以追问历史、修改已有产物或要求调整顺序。运行中提交消息时选择 Steer 或 Queue。
4. 在“配置”中调整 Agent、阶段 instructions、输入材料、模型与 output_type；保存草稿后发布，后续 Run 使用新配置。
5. 在“运行数据”中按项目查看每次 Agent Run 的实际输入、工具调用、模型输出、保存的产物和错误原因。
6. 完成章节确认与审核后，最终结果卡片提供同一版本 JSON 的查看、复制和下载。

Step1 必须同时读取全文；原作导入上限、模型输入预算与输出上限以“全局默认”中的当前配置为准。测试版暂停费用门禁，仍记录模型用量；达到其他运行上限时保存进度等待继续。

真实短篇《一盏灯》曾完成 Step1–11、返修、独立审核和最终 JSON 下载验收。验收脚本模拟用户选择与确认，浏览器只读核对和下载；联调产物保存在本地，不随源码发布。证据及边界见[真实模型记录](docs/live-model-smoke.md)。

## 后台与恢复

默认 API 服务包含后台调度器，关闭浏览器不会取消任务。可分开启动：

```bash
BRANCH_EMBEDDED_WORKER=0 ./scripts/start.sh
./scripts/worker.sh
```

两个进程须使用同一数据库、持久目录与密钥环境。服务恢复读取检查点、控制意图与租约；外部模型调用结果未知时保留待核对状态，不盲目重发。页面提供停止、继续任务及继续队列入口。

## 测试与验收

```bash
.venv/bin/python -m pytest -q
node --test tests/frontend-utils.test.mjs
node --test tests/runtime-view.test.mjs
.venv/bin/python docs/context/validate.py
.venv/bin/python docs/harness-data/validate.py
.venv/bin/python docs/output-schemas/v2/validate.py
```

数据库测试使用独立临时schema；不会将合成产物作为生产模型结果。SDK适配测试使用真实Agents SDK与本地模拟HTTP供应商；真实API联调单独记录，不用模拟成功替代真实成功。

详细范围见 [开发文档](docs/分支Agent开发文档.md)、[实施计划](docs/implementation-plan.md)；实际验证与当前边界见 [交付状态](docs/implementation-status.md)。

## 服务端部署准备

仓库提供 `Dockerfile` 和 `compose.yaml`。在目标环境配置 `OPENAI_API_KEY`、`BRANCH_DB_PASSWORD`、`BRANCH_LOCAL_PASSWORD` 后使用 `docker compose up -d --build`。数据库与文件分别使用持久卷；现有测试实例通过 AWS Systems Manager 部署更新。正式平台认证和备份策略仍需单独接入。

模型价格与容量初值依据官方模型页：[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[GPT-5 Mini](https://developers.openai.com/api/docs/models/gpt-5-mini)、[GPT-5 Nano](https://developers.openai.com/api/docs/models/gpt-5-nano)。更新价格表时生成新的配置版本。Nexo编辑器嵌入、工程自动写入/发布及完整DSL求解不在首版范围；当前仅对受支持表达式执行保守的不可达节点检查。
