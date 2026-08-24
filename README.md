# PPT Agent

PPT Agent 是一个带持久化确认门的演示文稿策划工作台。一期覆盖：

`任务卡 → 澄清问题 → 叙事结构 → 逐页大纲`

叙事结构和逐页大纲以版本化 Markdown 保存，支持安全预览、全屏编辑、浏览器草稿恢复、修订确认和上游失效传播。Agent 仅能通过标准 `read` 工具读取 `skills/` 内的 Markdown / 文本文件。

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

复制 `model_config.yaml`，把各阶段的 `provider` 改为非 `mock` 值，并设置 OpenAI 兼容模型的 `model`、`base_url` 和 `api_key_env`。通过只读环境变量注入配置：

```bash
export PPT_AGENT_MODEL_CONFIG=/absolute/path/model_config.yaml
export PPT_AGENT_RUNTIME_POLICY=/absolute/path/runtime.yaml
export YOUR_API_KEY_ENV=...
python main.py
```

应用只在服务启动时读取配置；设置页仅展示已生效的非敏感字段。

## 数据与恢复

项目数据默认保存在 `frontend/data/projects/`。每次状态变化都会原子写入 Manifest 和 Checkpoint，并追加事件日志。也可通过 `PPT_AGENT_PROJECTS_ROOT` 指定受管数据目录。

## 测试

```bash
python -m pytest
```

设计与 API 说明见 [docs/architecture.md](docs/architecture.md)。
