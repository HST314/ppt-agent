# PPT Agent

PPT Agent 是一个带持久化确认门的演示文稿策划工作台。一期覆盖：

`任务卡 → 澄清问题 → 叙事结构 → 逐页大纲`

叙事结构和逐页大纲以版本化 Markdown 保存，支持安全预览、全屏编辑、浏览器草稿恢复、修订确认和上游失效传播。工作台同时支持从任意历史检查点创建分支、切换分支头，以及按阶段配置模型。Agent 仅能通过标准 `read` 工具读取 `skills/` 内的 Markdown / 文本文件。

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

打开 `http://127.0.0.1:8000`。仓库自带 `mock` 模型配置，可完整体验工作流且不会发起外部模型请求。

## 接入真实模型

可以直接在工作台“设置”页修改三个一期阶段的 `provider`、`model`、`base_url`、备用模型、调用参数，以及 `runtime.yaml` 中的澄清/工具/读取预算。保存时服务会先完整校验，再原子写回配置文件并热重载。

模型密钥不在页面中录入或返回，仍由 `model_config.yaml` 的 `api_key_env` 指定环境变量。也可以通过环境变量指定独立配置文件：

```bash
export PPT_AGENT_MODEL_CONFIG=/absolute/path/model_config.yaml
export PPT_AGENT_RUNTIME_POLICY=/absolute/path/runtime.yaml
export YOUR_API_KEY_ENV=...
python main.py
```

设置页不会读取、保存或展示密钥值。若配置文件位于受保护目录，需确保启动服务的账号对该文件有写权限。

## 数据与恢复

项目数据默认保存在 `frontend/data/projects/`。每次状态变化都会原子写入 Manifest 和 Checkpoint，并追加事件日志。也可通过 `PPT_AGENT_PROJECTS_ROOT` 指定受管数据目录。

## 测试

```bash
python -m pytest
```

设计与 API 说明见 [docs/architecture.md](docs/architecture.md)。
