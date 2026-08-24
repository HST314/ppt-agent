# 一期架构

## 边界

- `main_front.py`：HTTP 输入校验与静态前端，不承担业务判断。
- `agent_core/workflow.py`：阶段 capability、澄清、文档生成、修订、确认和失效规则。
- `storage/project_store.py`：原子 Manifest、Checkpoint、事件、分支创建和分支头切换。
- `model_router/client.py`：阶段模型绑定、工具循环与输出边界。
- `runtime/read_tool.py`：限定在 `skills_root` 内的只读 UTF-8 文件访问。

前端只渲染服务端返回的 `capabilities` 和状态，不自行推断可执行动作。写请求携带当前 `checkpoint_id`；过期页面返回 `409`。

工作区采用“顶部创作进度卡 + 下方当前阶段主渲染区”。分支切换只允许移动到已登记的分支头，不改写历史；后台 Job 运行时禁止创建或切换分支。设置更新不接受密钥值，服务在同一配置锁内校验并原子更新 `runtime.yaml` 与 `model_config.yaml`，后续模型调用使用热重载后的配置。

## 文档规则

叙事结构与逐页大纲正文不设业务 Schema，外围信封记录 revision、hash、父 revision、创建者、确认状态以及模型、运行配置和工具读取来源。编辑永远创建新修订。叙事结构的编辑会把既有逐页大纲标记为 stale。

## 安全

- HTTP 请求体上限 512 KiB，Pydantic 请求模型拒绝未知字段。
- Markdown 前端渲染先进行 HTML 编码，仅支持少量结构语法。
- `read` 拒绝绝对路径、`..`、越界、符号链接逃逸、未知扩展名和超预算读取。
- 模型密钥只通过配置指定的环境变量读取，不进入项目快照、设置接口或日志。
