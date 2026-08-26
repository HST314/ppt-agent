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

## 分批全稿生成

首次全稿生成使用独立于正式修订的 `FullDeckGenerationSession`。确定性规划器 `balanced-3-4-v1` 按连续待生成区间固化 3–4 页批次；例如前两页为样品的 16 页全稿固定拆成 `4/4/3/3`。成功批次在同一事务中提交不可变 segment 包、部分 preview 包、页面就绪投影、指令应用记录和 `session_version`。只有全部批次成功后才从耐久包重新组装并创建一个正式 `pending_approval` 修订。

会话状态为 `queued → running → pause_requested → paused`，失败时进入 `failed`，全部批次成功后经 `finalizing` 进入 `completed`；`cancelled` 与 `stale` 为终态。暂停和取消均在当前批安全提交后生效，不强杀模型调用。失败重试只领取首个未成功批；部分预览失败保留已验证 segment，最终发布失败也不重新生成页面。下一批指令不可变，运行第 N 批时新增的指令从 N+1 批开始生效，并由批次记录实际应用的 directive ID。

模型只能通过只读参考工具访问服务端登记的已确认样品和最近两个成功 segment。参考路径、扩展名、来源 ID、文件大小和 UTF-8 内容均重新校验；用户指令和参考文本都按不可信数据处理，不能改变目标页、工具 allowlist 或发布校验。部分预览只从当前会话登记的 preview 包提供，继续复用正式预览的路径规范、媒体类型和 CSP。

### 上线开关与预算

`runtime.yaml` 中的 `full_deck_batched_generation_enabled` 控制新会话入口。仓库默认配置为 `true`；没有该字段的外部旧配置按安全默认值 `false` 加载。

- 开关开启：会话 API 和兼容 Job 入口均启动分批链路。
- 开关关闭：会话 API 拒绝新建会话，兼容 Job 入口使用既有单次全稿生成；已保存会话仍可读取和审计，正式全稿修订仍可预览、导出、修改、恢复和确认。
- 切换开关不会删除 session、batch、directive、segment、preview 或 artifact。正在运行的 worker 使用启动时取得的运行配置；应先等待或取消活动 Job，再修改部署配置并重启服务。

全稿生成的独立限制为：

- `full_deck_reference_max_read_chars_per_call`：单次参考文件读取字符数，默认 20,000；
- `full_deck_reference_max_read_chars_per_batch`：单批所有参考读取总字符数，默认 80,000，且不得小于单次预算；
- `full_deck_max_files`：草稿/包最大文件数，默认 384；
- `full_deck_max_file_bytes`：单文件最大字节数，默认 10,485,760（10MiB）；
- `full_deck_max_total_bytes`：整包最大字节数，默认 209,715,200（200MiB）。

这些字段属于部署边界，不在浏览器设置表单中编辑；公开 runtime context 只返回开关状态和数值，不返回凭据。运行配置哈希进入每个 PromptCall 和最终修订 provenance。

### v5 迁移与运行手册

v5 迁移是增量迁移：只新增生成会话、批次、页面投影、指令和不可变包表，并给 Job 数据库补充 `session_id`、`progress_json`。重复打开幂等，既有 checkpoint、正式修订哈希、artifact 元数据和文件内容不改写。没有破坏性降级迁移；回退应用版本前应先关闭分批开关并保留 v5 表，避免丢失可恢复证据。

上线步骤：

1. 备份受管项目目录和 `.jobs/jobs.db`，确认没有活动生成 Job。
2. 以开关关闭状态启动新版本，打开旧工程并验证正式全稿预览、ZIP 导出和修改入口。
3. 运行 Schema 初始化/启动检查，确认旧修订哈希与 artifact 文件不变。
4. 开启开关，使用固定 16 页夹具验证 `4/4/3/3`、部分预览、暂停/继续、指令、失败重试和最终单修订发布。
5. 导出审计并核对 revision → session → batch → PromptCall/directive → package → artifact SHA-256 关联。

回滚步骤：

1. 停止接收新生成请求；等待活动批到安全点，或通过取消接口登记协作式取消。
2. 将 `full_deck_batched_generation_enabled` 设为 `false` 并重启服务加载配置。
3. 验证已有正式修订的预览、导出和修改；保留所有 v5 表及内容寻址对象。
4. 如需回退二进制，使用上线前备份并优先只回退应用代码；不要删除 v5 数据来模拟降级。

故障排查优先读取 session 的稳定错误码和最近成功 preview 指针。`full_deck_preview_failed` 从已保存 segment 重组，`full_deck_finalization_failed` 从全部耐久 segment 重试发布，`full_deck_session_stale` 需要从新基线创建会话。不得把 `generated_html/` 当作恢复或预览来源。

### 审计导出

`GET /api/projects/{project_id}/audit/export` 在既有正式修订、时间线和 PromptCall 之外，增加有界的 `full_deck_generation`：会话锚点与发布修订哈希、批次目标和 PromptCall ID、指令正文及生效批次、页面内容引用、segment/preview 包哈希，以及每个包文件的 artifact ID 与 SHA-256。导出不包含包文件内容或内部磁盘路径；各集合有明确上限，并通过 `truncated` 标记是否截断。

## 本地图片素材

全局素材目录（默认 `frontend/data/images/`，可用环境变量 `PPT_AGENT_IMAGES_ROOT` 覆盖）存放扁平的图片/解读配对：`foo.png` 与 `foo.md` 文件名除后缀外完全相同，图像后缀限 png/jpg/jpeg/gif/webp/avif/ico。每次生成任务启动前，`storage/project_images.py` 的 `sync_project_images()` 把当时的全局快照覆盖式重建到 `<project>/images/`（先清空再拷贝，杜绝陈旧残留），一次生成内快照不再变化；continuation/resume 复用本次生成开始时的拷贝。脏素材——缺配对、子目录嵌套、超过 10MiB、不可读、非 UTF-8 解读、同名多图像后缀——一律跳过，诊断仅以 `[project-images]` 前缀输出到后端控制台，不进入任何 API 响应、不阻塞任务。

模型无视觉能力，仅通过 `.md` 文字解读理解图片：大纲阶段注入完整清单（`PROJECT_IMAGES_JSON`）与全部解读全文（`PROJECT_IMAGE_DESCRIPTIONS`）；样品阶段注入清单与每图截断摘要，全文按需用 `read` 读取；全稿分批/单体按该批目标页配图过滤注入解读，配图为空或与目标页无交集时回退全量注入。解读内容一律按图片的描述性数据处理，不是指令。逐页大纲用「配图：文件名」行规划每页用图；生成阶段只能用 `copy_project_image` 工具把项目 `images/` 内的规划图片复制进草稿包 `img/` 前缀下，再以包内相对路径引用；图片字节不进提示词，`read` 对 `images/` 前缀仅放行 `.md`。

图片相关限额在五层保持一致（当前值）：

| 层 | 位置 | 值 |
|---|---|---|
| 样品草稿默认 | `runtime/package_tool.py` | 128 文件 / 单文件 10MiB / 整包 80MiB |
| 全稿运行配置 | `runtime.yaml` | 384 / 10MiB / 200MiB |
| Policy schema 上下界 | `configs/runtime.py` | 单文件 ≤16MiB，整包 ≤256MiB |
| `PackageFile.content` 硬帽 | `agent_core/models.py` | ≤16,000,000 字符 |
| 包总字节硬帽 | `agent_core/models.py` | ≤256MiB |

素材目录为空或不存在时，同步为 no-op，提示词、工具面与限额行为与未启用该功能时完全一致（空素材零变化红线）。项目 `images/` 目录不经任何 HTTP 路由暴露，图片只能经 `copy_project_image` 进入草稿包；预览与 ZIP 导出沿用既有包内路由、CSP 与 UTF-8 文件名标志，中文逻辑名可直接服务与导出。

## 安全

- HTTP 请求体上限 512 KiB，Pydantic 请求模型拒绝未知字段。
- Markdown 前端渲染先进行 HTML 编码，仅支持少量结构语法。
- HTML-PPT 只接受受限相对路径、允许的静态文件类型和有界文件/整包大小；入口必须是 `index.html`。预览通过无 `allow-same-origin` 的 `sandbox` iframe 加载，响应 CSP 也强制 `sandbox allow-scripts`，因此直接打开预览 URL 仍是唯一源沙箱。包内脚本与资源可以加载，但 CSP 禁止联网、表单提交、对象和嵌套框架。
- `read` 拒绝绝对路径、`..`、越界、符号链接逃逸、未知扩展名和超预算读取；草稿写入与 Skill 静态资源复制使用相同的路径约束，Skill 中的脚本只可读或复制、不会被 Agent 执行。
- 模型密钥只通过配置指定的环境变量读取，不进入项目快照、设置接口或日志。
