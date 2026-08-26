# PPT Agent

PPT Agent 是一个带持久化确认门的演示文稿创作工作台。当前覆盖：

`任务卡 → 澄清问题 → 叙事结构 → 逐页大纲 → PPT 样品`

叙事结构和逐页大纲以版本化 Markdown 保存；PPT 样品从大纲中选择连续页段，逐页记录 `source_slide_number`，并保存为不可变、内容寻址的 HTML-PPT 包。包至少包含 `index.html`，也可携带 CSS、JavaScript、SVG、图片与字体等本地资源。样品在无同源权限的沙箱中运行自身导航，整包预览、校验、导出和追溯。历史选择会直接移动“当前样品”指针，不复制内容；在所选版本上继续修改会保持页段映射并创建以它为父级的新修订。显式创建分支仍是独立操作。

工作台支持浏览器草稿恢复、修订确认、上游失效传播、从阶段快照创建重跑分支，以及按阶段配置模型。状态控制台持续汇总 Job、模型、Skill、校验、产物和错误事件，并提供密度条、筛选、搜索、顺序切换、展开和复制。Agent 可只读标准 Skill 中的文本、HTML、CSS、JavaScript 和 SVG；生成阶段只能在当前隔离草稿包内写入、复制或精确替换文本，且不会执行 Skill 脚本。

## 本地启动

支持 Python 3.10+。editable 安装要求 `pip>=23.1`；先升级 pip，避免旧版 pip 无法识别构建后端的 editable hook。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip>=23.1"
python -m pip install -e ".[dev]"
python main.py
```

Windows PowerShell 使用以下命令：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
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

Windows PowerShell 中使用 `$env:ARK_API_KEY = "你的密钥"` 设置同名环境变量。

自动化测试使用显式注入的 `mock` 配置，不会调用真实付费模型。

## 接入真实模型

可以直接在工作台“设置”页修改四个生成阶段的 `provider`、`model`、`base_url`、备用模型、调用参数，以及 `runtime.yaml` 中的样品页数、澄清/工具/读取预算。样品页数范围为 1–6，默认 2；工具轮次默认 20、可配置上限 100；真实样品模型默认配置 16,384 输出令牌。模型输出必须形成带 `index.html` 的完整包清单，声明合法且连续的大纲页映射，并让清单页与 HTML 静态页面标识一一对应；JSON 不完整、路径越界、文件超限或包校验失败时最多自动修复 2 次。保存时服务会先完整校验，再原子写回配置文件并热重载。

模型密钥不在页面中录入或返回，仍由 `model_config.yaml` 的 `api_key_env` 指定环境变量。也可以通过环境变量指定独立配置文件：

```bash
export PPT_AGENT_MODEL_CONFIG=/absolute/path/model_config.yaml
export PPT_AGENT_RUNTIME_POLICY=/absolute/path/runtime.yaml
export ARK_API_KEY=...
python main.py
```

设置页不会读取、保存或展示密钥值。若配置文件位于受保护目录，需确保启动服务的账号对该文件有写权限。

## 数据与恢复

项目数据默认保存在 `frontend/data/projects/`。每个工程包含一个启用 WAL 的 `project.db`，在单个事务中更新工程状态、显式父检查点、分支头、修订、事件和 artifact 元数据；PromptCall 使用同库的独立审计状态。HTML-PPT 文件按 SHA-256 写入 `artifacts/_objects/` 作为内部去重与完整性对象；每个已发布全稿修订还会在 `artifacts/full_decks/<revision>/` 留存一份可直接打开的完整项目，包含所有页面、资源、组装清单和 `project.json`。未通过校验的生成尝试保存在 `generated_html/prompt_*/` 受管排查目录，可能只含尚未与样品页合并的模型原始输出，不会进入预览或发布。旧版 `artifacts/html/`、`artifacts/packages/` 仍可读取。后台 Job 存放在 `.jobs/jobs.db`，多 worker 通过 SQLite 写锁协调提交与分支操作。文件格式的既有工程会在首次读取时通过跨进程初始化锁自动导入，源文件仍保留。

PromptCall 审计记录保存脱敏后的输入消息、模板/配置/Skill 哈希、模型参数、工具调用、终态和产物引用。产物提交成功后才写入 `completed`；并发 CAS 落败会写入不带产物引用的 `conflicted`。样品工作区会将最新自动修复链展示为“尝试 1/2/3”，并列出工具轮次、Skill 读取次数和未发布原因。可通过 `GET /api/projects/<project_id>/audit/prompt-calls` 查询完整审计，或从 `GET /api/projects/<project_id>/audit/prompt-calls.jsonl` 导出 JSONL。

创作进度卡中已有快照的阶段可点击回看，并可从该阶段的输入边界重跑创建新分支；已有分支可在分支页面查看和切换。旧版本中已确认逐页大纲的工程可直接进入 PPT 样品阶段。也可通过 `PPT_AGENT_PROJECTS_ROOT` 指定受管数据目录。

## 测试

```bash
python -m pytest
```

设计与 API 说明见 [docs/architecture.md](docs/architecture.md)。
