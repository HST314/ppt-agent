# 当前架构

## 边界

- `main_front.py`：HTTP 输入校验与静态前端，不承担业务判断。
- `agent_core/workflow.py`：阶段 capability、澄清、文档/样品生成、修订、确认和失效规则。
- `storage/project_store.py`：SQLite WAL 事务、显式检查点 DAG、内容寻址 artifact、PromptCall 审计、分支创建和分支头切换。
- `storage/sqlite_schema.py`、`storage/persistence.py`、`storage/prompt_audit.py`：数据库 Schema、耐久原子写入与独立 PromptCall 审计仓储。
- `model_router/client.py`：阶段模型绑定、工具循环与输出边界。
- `runtime/read_tool.py`：限定在 `skills_root` 内的只读标准 Skill 文本/前端源码访问。
- `runtime/package_tool.py`：单次生成使用的路径受限 HTML-PPT 草稿包；支持写入/读取草稿、复制 Skill 静态资源和对草稿文本做精确替换，不执行 Skill 脚本。

前端只渲染服务端返回的 `capabilities` 和状态，不自行推断可执行动作。写请求携带当前 `checkpoint_id`；过期页面返回 `409`。

工作区采用“顶部创作进度卡 + 下方当前阶段主渲染区”。服务沿 `parent_checkpoint_id` 投影当前分支的不可变任务快照；点击阶段卡可只读回看，并从该快照回到阶段输入边界创建重跑分支。分支切换只允许移动到已登记的分支头，不改写历史；后台 Job 运行时禁止创建或切换分支。Job 注册表与分支操作通过 SQLite 写事务在多个 worker 之间串行化。设置更新不接受密钥值，服务在同一配置锁内校验并原子更新 `runtime.yaml` 与 `model_config.yaml`，后续模型调用使用热重载后的配置。默认运行绑定使用 Ark 的 `deepseek-v4-flash-ga-260731` 文本推理模型，密钥从 `ARK_API_KEY` 读取。

## 产物规则

叙事结构与逐页大纲正文不设业务 Schema，外围信封记录 revision、hash、父 revision、创建者、确认状态以及模型、运行配置和工具读取来源。逐页大纲用唯一的连续页号建立样品选择边界。PPT 样品修订保存不可变 HTML-PPT 包：入口固定为包内 `index.html`，每个清单页通过 `source_slide_number` 映射回大纲页，CSS、JavaScript、SVG、图片和字体等资源按路径与 SHA-256 建立清单。生成边界同时核对选择范围连续性、清单页数与 `index.html` 中静态 `data-slide-id`。修改意见生成新修订并保持原页段映射；选择历史版本只移动 `current_sample_revision_hash` 指针并形成新检查点，不复制内容或创建修订。后续修改以当前指针所指修订为父级，因此可自然形成版本分叉；“从此版本创建分支”仍是显式、独立的工程分支操作。叙事结构的编辑会把既有逐页大纲和样品标记为 stale，逐页大纲的编辑会把既有样品标记为 stale。

## 持久化模型

- `project_state` 保存当前投影；`checkpoints` 保存不可变状态并用 `parent_checkpoint_id` 形成 DAG；`branches` 保存分支头。
- `document_revisions`、`sample_revisions` 保存可查询的修订记录；`sample_packages`、`sample_package_files` 保存整包入口、页数、文件路径与哈希。`sample_pages` 和 `artifacts` 保留旧版页面兼容能力。
- `events` 与状态变更同事务提交。包文件先用临时文件、`fsync` 和原子替换写入；提交失败最多留下可安全清理的未引用内容哈希文件，不会产生指向缺失产物的已提交状态。
- `prompt_calls` 和 `prompt_call_events` 形成独立审计链，记录脱敏后的 messages、模板/运行/模型/Skill 哈希、模型参数、工具调用、开始/完成/失败/冲突和 output ref；只有已提交产物可进入 completed，CAS 冲突终态不携带 output ref。最新样品修复链从该审计记录投影为有界的尝试摘要，JSONL 只作为完整导出格式。
- `.jobs/jobs.db` 持久化 Job 和状态事件。SQLite `BEGIN IMMEDIATE` 同时承担跨线程、跨 worker 的写入协调；后续多机部署可将表边界迁移到 PostgreSQL，并将 artifact 目录替换为对象存储。

## 安全

- HTTP 请求体上限 512 KiB，Pydantic 请求模型拒绝未知字段。
- Markdown 前端渲染先进行 HTML 编码，仅支持少量结构语法。
- HTML-PPT 只接受受限相对路径、允许的静态文件类型和有界文件/整包大小；入口必须是 `index.html`。预览通过无 `allow-same-origin` 的 `sandbox` iframe 加载，响应 CSP 也强制 `sandbox allow-scripts`，因此直接打开预览 URL 仍是唯一源沙箱。包内脚本与资源可以加载，但 CSP 禁止联网、表单提交、对象和嵌套框架。
- `read` 拒绝绝对路径、`..`、越界、符号链接逃逸、未知扩展名和超预算读取；草稿写入与 Skill 静态资源复制使用相同的路径约束，Skill 中的脚本只可读或复制、不会被 Agent 执行。
- 模型密钥只通过配置指定的环境变量读取，不进入项目快照、设置接口或日志。
