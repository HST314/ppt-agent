# PPT Agent

PPT Agent 是一个带持久化确认门的演示文稿创作工作台。当前覆盖：

`任务卡 → 澄清问题 → 叙事结构 → 逐页大纲 → PPT 样品`

叙事结构和逐页大纲以版本化 Markdown 保存；PPT 样品是版本化、自包含的 16:9 HTML 页面，净化后按内容哈希去重保存。样品区提供大画框隔离预览、分页切换、自然语言修改和确认，并在意见区后方提供修订历史：历史版本可只读预览、恢复为新修订或作为新分支起点。工作台支持浏览器草稿恢复、修订确认、上游失效传播、从阶段快照创建重跑分支，以及按阶段配置模型。Agent 仅能通过标准 `read` 工具读取 `skills/` 内的 Markdown / 文本文件。

## 本地启动

支持 Python 3.10+。editable 安装要求 `pip>=23.1`；先升级 pip，避免旧版 pip 无法识别构建后端的 editable hook。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip>=23.1"
python -m pip install -e ".[dev]"
python main.py
```

如果创建出的虚拟环境没有 pip，请先安装当前 Python 对应的 `venv`/`ensurepip` 系统包，再重新创建 `.venv`；不要继续使用系统自带的旧 pip 做 editable 安装。

打开 `http://127.0.0.1:8000`。默认配置已对齐 Image Agent 的真实文本推理路由：供应商 `ark`、模型 `deepseek-v4-flash-ga-260731`、服务地址 `https://ark.cn-beijing.volces.com/api/v3`。启动前需提供 `ARK_API_KEY`：

```bash
export ARK_API_KEY=你的密钥
python main.py
```

自动化测试使用显式注入的 `mock` 配置，不会调用真实付费模型。

## 接入真实模型

可以直接在工作台“设置”页修改四个生成阶段的 `provider`、`model`、`base_url`、备用模型、调用参数，以及 `runtime.yaml` 中的 HTML 样品页数、澄清/工具/读取预算。样品页数范围为 1–6，默认 2；真实样品模型默认配置 16,384 输出令牌，并为单页 HTML 提供 7,000 字符预算。JSON 不完整或 HTML 安全净化失败时最多自动修复 2 次。保存时服务会先完整校验，再原子写回配置文件并热重载。

模型密钥不在页面中录入或返回，仍由 `model_config.yaml` 的 `api_key_env` 指定环境变量。也可以通过环境变量指定独立配置文件：

```bash
export PPT_AGENT_MODEL_CONFIG=/absolute/path/model_config.yaml
export PPT_AGENT_RUNTIME_POLICY=/absolute/path/runtime.yaml
export ARK_API_KEY=...
python main.py
```

设置页不会读取、保存或展示密钥值。若配置文件位于受保护目录，需确保启动服务的账号对该文件有写权限。

## 数据与恢复

项目数据默认保存在 `frontend/data/projects/`。每个工程包含一个启用 WAL 的 `project.db`，在单个事务中更新工程状态、显式父检查点、分支头、修订、事件和 artifact 元数据；PromptCall 使用同库的独立审计状态。净化后的 HTML 位于 `artifacts/html/<sha256>.html`，相同内容自动复用。后台 Job 存放在 `.jobs/jobs.db`，多 worker 通过 SQLite 写锁协调提交与分支操作。文件格式的既有工程会在首次读取时通过跨进程初始化锁自动导入，源文件仍保留。

PromptCall 审计记录保存脱敏后的输入消息、模板/配置/Skill 哈希、模型参数、工具调用、终态和产物引用。产物提交成功后才写入 `completed`；并发 CAS 落败会写入不带产物引用的 `conflicted`。可通过 `GET /api/projects/<project_id>/audit/prompt-calls` 查询，或从 `GET /api/projects/<project_id>/audit/prompt-calls.jsonl` 导出 JSONL。

创作进度卡中已有快照的阶段可点击回看，并可从该阶段的输入边界重跑创建新分支；已有分支可在分支页面查看和切换。旧版本中已确认逐页大纲的工程可直接进入 PPT 样品阶段。也可通过 `PPT_AGENT_PROJECTS_ROOT` 指定受管数据目录。

## 测试

```bash
python -m pytest
```

设计与 API 说明见 [docs/architecture.md](docs/architecture.md)。
