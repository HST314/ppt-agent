# 当前架构

## 边界

- `main_front.py`：HTTP 输入校验与静态前端，不承担业务判断。
- `agent_core/workflow.py`：阶段 capability、澄清、文档/样品生成、修订、确认和失效规则。
- `storage/project_store.py`：SQLite WAL 事务、显式检查点 DAG、内容寻址 artifact、PromptCall 审计、分支创建和分支头切换。
- `storage/sqlite_schema.py`、`storage/persistence.py`、`storage/prompt_audit.py`：数据库 Schema、耐久原子写入与独立 PromptCall 审计仓储。
- `model_router/client.py`：阶段模型绑定、工具循环与输出边界。
- `runtime/read_tool.py`：限定在 `skills_root` 内的只读 UTF-8 文件访问。

前端只渲染服务端返回的 `capabilities` 和状态，不自行推断可执行动作。写请求携带当前 `checkpoint_id`；过期页面返回 `409`。

工作区采用“顶部创作进度卡 + 下方当前阶段主渲染区”。服务沿 `parent_checkpoint_id` 投影当前分支的不可变任务快照；点击阶段卡可只读回看，并从该快照回到阶段输入边界创建重跑分支。分支切换只允许移动到已登记的分支头，不改写历史；后台 Job 运行时禁止创建或切换分支。Job 注册表与分支操作通过 SQLite 写事务在多个 worker 之间串行化。设置更新不接受密钥值，服务在同一配置锁内校验并原子更新 `runtime.yaml` 与 `model_config.yaml`，后续模型调用使用热重载后的配置。默认运行绑定使用 Ark 的 `deepseek-v4-flash-ga-260731` 文本推理模型，密钥从 `ARK_API_KEY` 读取。

## 产物规则

叙事结构与逐页大纲正文不设业务 Schema，外围信封记录 revision、hash、父 revision、创建者、确认状态以及模型、运行配置和工具读取来源。PPT 样品的修订与页面元数据存入 SQLite；页面 HTML 经过净化后按 SHA-256 保存到 artifact 目录，修订只引用 `artifact_id/sha256/size/sanitizer_version`。修改意见生成新修订；历史预览不移动分支头，“恢复为当前版本”复制内容并生成新修订与检查点，“从此版本创建分支”从该修订的来源检查点分叉。编辑永远创建新修订。叙事结构的编辑会把既有逐页大纲和样品标记为 stale，逐页大纲的编辑会把既有样品标记为 stale。

## 持久化模型

- `project_state` 保存当前投影；`checkpoints` 保存不可变状态并用 `parent_checkpoint_id` 形成 DAG；`branches` 保存分支头。
- `document_revisions`、`sample_revisions`、`sample_pages` 保存可查询的修订记录；`artifacts` 保存内容哈希、大小、净化器版本和受管相对路径。
- `events` 与状态变更同事务提交。HTML 文件先用临时文件、`fsync` 和原子替换写入；提交失败最多留下可安全清理的未引用内容哈希文件，不会产生指向缺失产物的已提交状态。
- `prompt_calls` 和 `prompt_call_events` 形成独立审计链，记录脱敏后的 messages、模板/运行/模型/Skill 哈希、模型参数、工具调用、开始/完成/失败/冲突和 output ref；只有已提交产物可进入 completed，CAS 冲突终态不携带 output ref，JSONL 只作为导出格式。
- `.jobs/jobs.db` 持久化 Job 和状态事件。SQLite `BEGIN IMMEDIATE` 同时承担跨线程、跨 worker 的写入协调；后续多机部署可将表边界迁移到 PostgreSQL，并将 artifact 目录替换为对象存储。

## 安全

- HTTP 请求体上限 512 KiB，Pydantic 请求模型拒绝未知字段。
- Markdown 前端渲染先进行 HTML 编码，仅支持少量结构语法。
- 样品 HTML 在服务端拒绝脚本、事件处理器、危险标签和外部资源；前端只写入无权限 `sandbox` iframe 的 `srcdoc`，并注入禁止脚本、联网、表单、对象和嵌套框架的 CSP。
- `read` 拒绝绝对路径、`..`、越界、符号链接逃逸、未知扩展名和超预算读取。
- 模型密钥只通过配置指定的环境变量读取，不进入项目快照、设置接口或日志。
