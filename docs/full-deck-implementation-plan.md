# PPT 全稿阶段实现任务书

> 文档状态：待按阶段实施  
> 基线版本：`8908946`  
> 适用范围：从已确认 PPT 样品进入全稿，到全稿生成、修改、历史版本与确认  
> 本文是后续开发、测试和验收的唯一任务边界；本轮只产出方案，不修改业务代码。

## 1. 已确认的产品结论

1. 样品页主操作为“确认样品并进入全稿”。点击后直接执行，不增加二次确认框；成功后立即切换到全稿工作区。
2. 全稿使用有序 `full_deck_plan.pages` 管理每一页。页面项保存稳定 ID、大纲来源、当前状态、内容来源和产物引用，数组顺序就是最终演示顺序。
3. 全稿初始化时，当前样品修订中的页面按原始产物引用进入对应页面槽位。用户在全稿工作区提出修改后，任何页面都可以形成新的全稿修订；历史修订及其来源关系始终可追溯。
4. 首次生成全稿时，Agent 只生成尚无内容的连续页段，确定性 Composer 按页面清单组装最终 HTML-PPT。样品来源页通过既有内容引用进入初版全稿。
5. 全稿页沿用样品页的页面结构和交互逻辑：大幅 16:9 安全预览、自然语言修改、重新生成、确认、生成尝试、修订历史、历史版本切换、导出和从当前版本创建工程分支。
6. 实现按阶段推进。先建立状态、数据和界面，再接入真实全稿生成，最后补齐全稿修改与验收阶段。

## 2. 目标与范围

### 2.1 最终目标

完成以下闭环：

```text
样品待确认
  → 确认当前样品修订
  → 初始化完整页面清单
  → 直接进入全稿工作区
  → 生成缺失页段
  → 确定性组装完整 HTML-PPT
  → 用户浏览并提交修改意见
  → 创建新的全稿修订
  → 切换、恢复或基于历史版本继续修改
  → 确认当前全稿
  → 进入确认验收阶段
```

### 2.2 本任务书覆盖

- `ppt_full` 工作流状态、phase 和 capabilities。
- 确认样品并进入全稿的原子事务。
- `FullDeckPlan`、页面槽位、全稿修订与全稿包的数据模型。
- 全稿工作区及其历史版本交互。
- 非样品页段的 Agent 生成契约。
- 页面级来源追踪、确定性组装、校验和修复。
- 全稿自然语言修改、重新生成、确认、历史恢复、分支。
- 数据库迁移、旧工程兼容、API、审计、测试和验收。

### 2.3 相关事项的边界

- 工程级 Job 表迁移独立实施，不与全稿功能耦合；全稿 Job 先沿用现有 `JobRegistry` 契约。
- 本轮不改变叙事结构、逐页大纲和样品生成的既有业务规则。
- 全稿初版仍以 HTML-PPT 包为交付对象；PPTX 转换不属于本任务。
- 页面插入、删除和拖拽重排先在数据结构中预留稳定语义；首轮 UI 只展示与切换页面，不新增复杂编排控件。

## 3. 当前基线与改造入口

当前仓库已经具备以下基础：

- `agent_core/models.py`：样品修订、HTML-PPT 包、页面映射和内容哈希模型。
- `agent_core/workflow.py`：样品生成、自动修复、修改、历史恢复和确认逻辑。
- `storage/project_store.py`：SQLite WAL、检查点 DAG、内容寻址 artifact、样品修订和分支。
- `main_front.py`：严格请求模型、样品 API、安全预览、ZIP 导出和工程视图投影。
- `frontend/static/js/samples.js`：样品预览、修改表单、生成尝试和修订历史。
- `frontend/static/js/app.js`：阶段导航、capability 驱动动作和 Job 轮询。
- `workflows/ppt_agent_v1.yaml`：工作流目前终止于 `ppt_sample`。

开发前先处理一个已知的一致性问题：运行策略允许 `max_tool_rounds <= 100`，设置页输入框的 `max` 也必须同步为 `100`，并增加前端契约测试。

## 4. 核心领域模型

### 4.1 全稿根对象

工程 Manifest 新增：

```json
{
  "full_deck": {
    "full_deck_id": "deck_4f9d...",
    "approved_sample_revision_hash": "sha256:...",
    "outline_revision_hash": "sha256:...",
    "current_revision_hash": "sha256:...",
    "revision_refs": [
      {"revision_hash": "sha256:...", "status": "draft"}
    ]
  }
}
```

约束：

- 一个工程分支同一时间只有一个 `full_deck_id`。
- `approved_sample_revision_hash` 记录全稿初始化所采用的样品基线。
- `outline_revision_hash` 固定页面清单初始化时的大纲来源。
- `current_revision_hash` 只移动指针；历史切换不复制 artifact，也不伪造新内容修订。
- 修改、重新生成和完整生成都创建新的 `FullDeckRevision`，并记录 `parent_revision_hash`。

### 4.2 全稿修订

```json
{
  "full_deck_id": "deck_4f9d...",
  "revision": 2,
  "revision_hash": "sha256:...",
  "parent_revision_hash": "sha256:...",
  "feedback": "第 5 页把结论提前，并调整数据层级",
  "status": "draft|pending_approval|approved|stale",
  "plan": {"pages": []},
  "package": null,
  "created_at": "2026-08-25T00:00:00Z",
  "provenance": {
    "outline_revision_hash": "sha256:...",
    "approved_sample_revision_hash": "sha256:...",
    "model_config_hash": "sha256:...",
    "runtime_config_hash": "sha256:...",
    "skills_hash": "sha256:...",
    "changed_slot_ids": ["slot_..."]
  }
}
```

状态语义：

- `draft`：清单已建立，但仍有 `pending` 页面，尚无完整可发布包。
- `pending_approval`：所有页面都已就绪，完整包已通过结构与安全校验，等待用户确认。
- `approved`：用户确认的全稿修订。
- `stale`：上游大纲或样品基线变化后保留的历史修订。

`revision_hash` 必须绑定以下内容：父修订、完整有序页面清单、每页内容引用、全稿包哈希、反馈和上游来源。仅改变当前指针不能改变该哈希。

### 4.3 `full_deck_plan.pages`

每一页采用以下结构：

```json
{
  "slot_id": "slot_8b2f...",
  "position": 0,
  "outline_ref": {
    "outline_revision_hash": "sha256:...",
    "source_slide_number": 1
  },
  "title": "封面",
  "status": "pending|ready",
  "source_type": "approved_sample|generated_segment|full_deck_edit|pending",
  "content_ref": {
    "artifact_type": "html_ppt_slide",
    "revision_hash": "sha256:...",
    "package_hash": "sha256:...",
    "slide_id": "slide-1",
    "slide_content_hash": "sha256:..."
  },
  "derived_from": {
    "sample_revision_hash": "sha256:...",
    "sample_slide_id": "slide-1"
  }
}
```

字段规则：

- `slot_id` 创建后保持稳定。全稿后续版本通过它识别“同一页”，不能用数组下标充当 ID。
- `position` 只用于持久化和查询；返回给客户端前按它排序，并校验连续、唯一。
- `outline_ref` 关联全稿初始化时的大纲修订。未来插入的补充页允许为 `null`。
- `source_type` 表示当前内容来自哪里，不限制后续修改。
- `content_ref` 指向不可变内容。修改页面时创建新内容引用和新全稿修订。
- `derived_from` 保留样品血缘。即使该页后续发生全稿修改，也能追溯它最初来自哪一个样品页。
- `slide_content_hash` 用于页面级保真校验，避免只校验整包哈希而无法判断具体页面是否变化。它不是简单的 HTML 字符串哈希，而是对“规范化页面 DOM + 该页传递依赖的 artifact 内容哈希”计算的内容图哈希；Composer 产生的命名空间路径会先还原为来源包逻辑路径再参与比较。

### 4.4 初始化规则

进入全稿时，根据已确认逐页大纲生成完整页面清单：

1. 为大纲中的每个页号创建一个槽位，初始顺序按大纲页号排列。
2. 样品清单中每个 `source_slide_number` 必须唯一对应一个大纲槽位。
3. 样品对应槽位置为 `ready + approved_sample`，保存样品修订、包、slide ID 和页面内容哈希。
4. 其他槽位置为 `pending`，`content_ref = null`。
5. 创建全稿 R1，状态为 `draft`，反馈说明为“由已确认样品初始化”。
6. 在同一数据库事务中确认样品、写入全稿 R1、更新当前指针、写事件并把状态切换为 `ppt_full / ready_to_generate`。

## 5. 持久化设计

数据库 Schema 从 v3 升级到 v4，新增：

```sql
CREATE TABLE full_deck_revisions (
    revision_hash TEXT PRIMARY KEY,
    full_deck_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision_hash TEXT,
    feedback TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL
);

CREATE INDEX full_deck_revision_number_idx
ON full_deck_revisions(full_deck_id, revision);

CREATE TABLE full_deck_pages (
    revision_hash TEXT NOT NULL REFERENCES full_deck_revisions(revision_hash) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    slot_id TEXT NOT NULL,
    source_slide_number INTEGER,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content_ref_json TEXT,
    derived_from_json TEXT,
    PRIMARY KEY (revision_hash, slot_id),
    UNIQUE (revision_hash, position)
);

CREATE TABLE full_deck_packages (
    revision_hash TEXT PRIMARY KEY REFERENCES full_deck_revisions(revision_hash) ON DELETE CASCADE,
    package_hash TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    title TEXT NOT NULL,
    slide_count INTEGER NOT NULL,
    slides_json TEXT NOT NULL,
    composition_manifest_json TEXT NOT NULL
);

CREATE TABLE full_deck_package_files (
    revision_hash TEXT NOT NULL REFERENCES full_deck_packages(revision_hash) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    logical_path TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (revision_hash, logical_path),
    UNIQUE (revision_hash, file_index)
);
```

实现要求：

- 继续复用 `artifacts` 内容寻址存储，相同文件不能重复落盘。
- Manifest 只保存全稿修订引用；完整页面项和包文件从关系表投影。
- Checkpoint 的 `payload_json` 必须能恢复 `full_deck.current_revision_hash` 和对应修订。
- 存储提交沿用“artifact 原子写入 → SQLite `BEGIN IMMEDIATE` → 状态与事件同事务提交”。
- v3 工程打开时按需执行幂等迁移；没有全稿的工程保持 `full_deck = null`。
- 回滚或迁移失败时不得产生半个全稿对象或丢失现有样品。

## 6. 状态机与 Capability

### 6.1 工作流状态

`workflows/ppt_agent_v1.yaml` 增加：

```yaml
states:
  - ppt_full
  - acceptance

transitions:
  ppt_sample: [ppt_full]
  ppt_full: [acceptance]
  acceptance: []
```

主要 phase：

| state | phase | 含义 |
| --- | --- | --- |
| `ppt_full` | `ready_to_generate` | 全稿 R1 已初始化，仍有待生成页 |
| `ppt_full` | `generating` | 正在生成页段或组装全稿 |
| `ppt_full` | `waiting_human_approval` | 完整全稿已发布，等待修改或确认 |
| `ppt_full` | `completed` | 当前全稿修订已确认 |
| `acceptance` | `ready_for_review` | 进入最终验收 |

### 6.2 Capability 清单

新增：

- `enter_full_deck`
- `generate_full_deck`
- `regenerate_full_deck`
- `revise_full_deck`
- `approve_full_deck`
- `restore_full_deck_revision`
- `branch_full_deck_revision`
- `inspect_full_deck`

规则：

- `enter_full_deck` 在当前样品修订可发布、全稿尚未初始化且无活动 Job 时出现。存量工程中的当前样品即使已经是 `approved`，也可以直接初始化全稿。
- `generate_full_deck` 只在全稿仍有 `pending` 页面且无活动 Job 时出现。
- `revise_full_deck` 在存在当前全稿修订时出现；修改请求必须携带当前检查点和当前修订哈希。
- `approve_full_deck` 只在当前修订有完整包、状态为 `pending_approval` 时出现。
- 历史恢复和分支操作不能与活动 Job 并发。
- 前端继续只根据服务端 capability 渲染动作，不自行推断权限。

## 7. API 契约

### 7.1 确认样品并进入全稿

```http
POST /api/projects/{project_id}/full-deck/enter
Content-Type: application/json

{
  "checkpoint_id": "checkpoint_...",
  "sample_revision_hash": "sha256:..."
}
```

后端步骤必须位于一个业务事务：

1. 校验 checkpoint、当前样品指针和样品修订。
2. 当前样品尚未确认时将其更新为 `approved`；已确认时保持原状态。
3. 从已确认大纲和样品页映射初始化全稿 R1。
4. 写 `sample_approved` 与 `full_deck_initialized` 的可审计事件信息；实际数据库状态只提交一次。
5. 切换到 `ppt_full / ready_to_generate`。
6. 返回新的 `project_view`。前端收到 200 后直接渲染全稿工作区。

幂等要求：相同 checkpoint 的重复请求要么返回已经创建的同一全稿，要么返回明确的 `409 stale_revision`，绝不能创建第二个 R1。

### 7.2 全稿 Job

沿用现有 `/api/projects/{project_id}/jobs`，扩展操作：

```text
generate_full_deck
regenerate_full_deck
revise_full_deck
```

`revise_full_deck` 请求：

```json
{
  "operation": "revise_full_deck",
  "checkpoint_id": "checkpoint_...",
  "revision_hash": "sha256:...",
  "feedback": "第 5 页把核心结论放在标题中"
}
```

校验规则：

- `feedback` 去除首尾空白后长度 1–4000。
- 修改和重新生成必须绑定当前全稿修订哈希。
- Job 幂等键包含 operation、checkpoint、revision、feedback hash、模型配置和 Skill hash。
- CAS 冲突的 PromptCall 终态不得携带未提交的 output ref。

### 7.3 全稿修订与产物

```text
GET  /api/projects/{id}/full-deck/revisions
GET  /api/projects/{id}/full-deck/revisions/{revision_hash}
POST /api/projects/{id}/full-deck/revisions/{revision_hash}/restore
POST /api/projects/{id}/full-deck/revisions/{revision_hash}/branches
GET  /api/projects/{id}/full-deck/revisions/{revision_hash}/preview/{path}
GET  /api/projects/{id}/full-deck/revisions/{revision_hash}/export
POST /api/projects/{id}/full-deck/approve
```

预览和导出沿用样品包的路径校验、媒体类型、CSP、`nosniff`、内容哈希复核和 ZIP 安全规则。

### 7.4 `project_view` 新增投影

```json
{
  "full_deck": {"current_revision_hash": "sha256:..."},
  "full_deck_revision": {},
  "full_deck_revisions": [],
  "full_deck_attempts": [],
  "capabilities": []
}
```

轮询工程详情时只返回当前全稿包元数据，不返回所有历史包文件清单；历史详情按需读取，避免响应随版本数线性膨胀。

## 8. 全稿生成与确定性组装

### 8.1 页段划分

首次生成时，服务从 R1 页面清单中找出 `pending` 槽位，并按连续大纲页号划分为页段。例如：

```text
总页：1 2 3 4 5 6 7 8 9 10
样品：      4 5
待生成段：1–3、6–10
```

每个页段是独立生成目标，包含：

- 全部已确认大纲，便于保持叙事连续性。
- 当前样品包的只读元数据、可读取文件清单和视觉参照。
- 当前页段的精确页号与标题。
- 相邻页的标题和内容摘要。
- `FULL_DECK_TARGET_SLIDE_NUMBERS` 明确写入 Prompt。

Agent 返回页段包及声明：

```json
{
  "source_slide_numbers": [1, 2, 3],
  "slides": [
    {"slide_id": "slide-1", "source_slide_number": 1, "title": "..."}
  ],
  "entrypoint": "index.html",
  "files": []
}
```

页段校验必须覆盖：页号唯一、连续、等于目标集合；清单页数等于静态 `.slide[data-slide-id]` 数量；文件路径、类型和大小安全；不存在外部网络依赖。

### 8.2 Composer 输入与输出

Composer 是普通程序，不调用模型。输入为：

- 一个不可变 `FullDeckRevision.plan.pages`。
- 每个 `ready` 页面所引用的来源包、slide ID 和页面内容哈希。
- 标准 HTML-PPT 外壳版本。

输出为：

- 完整 `index.html`。
- 命名空间化后的 CSS、JavaScript、字体、图片和其他静态资源。
- 与页面清单一一对应的 `slides` manifest。
- `composition_manifest.json`，记录每个最终 slide 来自哪个槽位和哪个来源哈希。
- 最终 `package_hash`。

组装原则：

1. 只按 `plan.pages` 顺序组装，模型不能决定最终页序。
2. 每个来源包复制到基于内容哈希的独立命名空间，避免同名资产覆盖。
3. 页面 DOM、样式和资源引用的转换必须是可重复的纯函数；相同输入得到相同输出哈希。
4. 初版全稿中，样品来源页面在组装前后必须通过规范化 `slide_content_hash` 对照；路径命名空间转换不能改变页面 DOM 语义或传递依赖内容，其来源关系写入 composition manifest。
5. 最终静态页数、slide ID 顺序和大纲页号必须与全稿清单一致。
6. 任一来源包损坏、页面缺失或哈希不符时停止发布，不生成半完整全稿修订。
7. Composer 的转换版本写入 provenance；升级转换算法时会自然得到新的全稿修订哈希。

### 8.3 自动修复与尝试摘要

每个生成页段沿用“首次生成 + 最多两次修复”的机制。全稿工作区展示本次 Job 的聚合摘要：

- 尝试序号和页段范围。
- 工具轮次、调用数和 Skill 读取数。
- 页段是否通过契约。
- Composer 是否成功。
- 未发布原因。

只有所有目标页段和 Composer 都成功，才创建状态为 `pending_approval` 的完整全稿修订。

## 9. 全稿修改与版本控制

### 9.1 修改流程

用户在全稿预览下提交自然语言意见后：

1. 以当前 `current_revision_hash` 为父修订启动 `revise_full_deck`。
2. Prompt 明确提供当前完整页面清单和用户反馈。
3. Agent 返回 `changed_source_slide_numbers` 或 `changed_slot_ids`，以及对应的替换页段包。
4. 后端验证声明目标存在，并验证未声明槽位的 `content_ref` 与父修订完全一致。
5. 被修改槽位写入新的内容引用，`source_type` 更新为 `full_deck_edit`，同时保留既有 `derived_from` 血缘。
6. Composer 重新组装完整包。
7. 创建新的全稿修订并移动当前指针；父修订和历史包保持不变。

修改目标可以包含最初来自样品的页面。版本控制保证：进入全稿时采用的样品页面仍可在 R1 中完整追溯，后续用户调整体现在新的全稿修订中。

### 9.2 重新生成

“重新生成”创建当前修订的子修订：

- R1 尚未完整时，重新生成所有 `pending` 页。
- 已有完整包时，重新生成当前不再直接引用 `approved_sample` 的页面；仍直接引用样品的页面保持原内容引用。
- 有具体修改意见时统一走 `revise_full_deck`，由 Agent 声明目标页并由后端核对实际变化范围。
- 生成失败只记录尝试和 PromptCall，不移动当前全稿指针。

### 9.3 历史版本

与样品历史保持同一交互语义：

- 历史列表按修订号倒序显示。
- 点击历史版本只移动 `current_revision_hash` 并打开该版本，不创建内容修订。
- 下一次 AI 修改以当前所选历史版本为父节点，可形成版本分叉。
- 当前版本可以创建工程分支；分支从该全稿修订所在 checkpoint 出发。
- 历史卡显示修订号、状态、页数、反馈摘要、创建时间、变更页和包文件数。

## 10. 全稿工作区 UX

全稿页面建立独立 `frontend/static/js/full-deck.js`，结构直接对齐 `samples.js`，便于首轮快速实现和单独演进。

### 10.1 页面结构

从上到下：

1. 阶段标题“HTML-PPT 全稿”、状态徽标和修订号。
2. 完整包提示或待生成提示。
3. 大幅 16:9 安全预览。
4. 包信息、总页数、导出 ZIP。
5. 修改意见表单。
6. 本次全稿生成尝试。
7. 全稿修订历史。

R1 尚未生成完整包时，预览区域显示完整页面槽位摘要：

- 已有内容页显示“已就绪”、大纲页号和来源。
- 待生成页显示“待生成”。
- 主按钮为“生成完整 HTML-PPT”。
- 样品来源页可在当前区域预览；页面列表不承担复杂编辑器职责。

完整包生成后，行为对齐样品页：

- iframe 使用 `sandbox="allow-scripts"`，不允许 same-origin、表单、弹窗、下载或导航权限。
- HTML-PPT 自身处理翻页和总览。
- 修改表单位于预览正下方。
- “确认全稿并进入验收”为主要确认动作。
- 当前修订已确认后仍可提交新意见，新意见创建新的全稿修订。

### 10.2 直接跳转交互

样品页按钮文案改为“确认样品并进入全稿”。点击后：

- 立即禁用按钮并显示“正在进入全稿…”。
- 请求成功后更新工程状态、清空 `focusStage`、刷新分支信息并重新渲染。
- 页面焦点移动到全稿阶段标题，屏幕阅读器通过 `aria-live` 获知成功。
- 失败时保留样品页面和当前滚动位置，在按钮附近及 toast 中给出可恢复错误。

### 10.3 可访问性和响应式要求

- 所有按钮和历史版本选择器至少 44×44 px，焦点环清晰。
- 状态不能只靠颜色表达，必须同时有文字。
- textarea 有可见 label、帮助文本和就近 `role="alert"` 错误。
- Job 按钮在提交后禁用并展示操作名称，避免重复触发。
- 历史切换后保留逻辑焦点，异步刷新不抢焦点。
- 16:9 预览始终保留宽高比；小于 768 px 时工具栏和按钮纵向堆叠，不产生页面级横向滚动。
- 页面标题层级按 h2 → h3 组织；iframe 有包含版本和标题的可访问名称。
- 动效使用现有 150–300ms 节奏，并遵循 `prefers-reduced-motion`。

## 11. 上游变更与恢复规则

- 修改叙事结构或逐页大纲后，当前全稿修订标记为 `stale`，保留全部历史和 artifact。
- 修改后的逐页大纲重新确认后，需要从样品阶段继续建立新的下游链路。
- 样品阶段新修订不会自动改写已经建立的全稿；用户从对应样品修订进入全稿时创建新的工程分支或新的全稿根对象。
- 阶段回看快照增加 `ppt_full` 边界；点击已完成全稿阶段可以只读打开当时的当前修订。
- `rerun_stage=ppt_full` 从已确认大纲和选定样品修订重建全稿 R1，不复用旧全稿当前指针。

## 12. 安全、审计与失败语义

### 12.1 安全

- 全稿包继续接受受限相对路径和允许的静态类型；禁止 `..`、绝对路径、符号链接逃逸和外部网络依赖。
- 单文件、文件数、单页和整包大小采用显式上限；全稿上限应高于样品并写入运行策略，不使用无界读取或写入。
- Composer 不执行来源包脚本，只处理静态文件和页面标记。
- 预览响应继续设置 CSP、`Cross-Origin-Resource-Policy`、`X-Content-Type-Options` 和私有不可变缓存。
- 导出时再次核对文件路径、artifact 哈希和大小。

### 12.2 审计

PromptCall 记录：

- 全稿 operation、父 PromptCall、目标页段。
- 模型、运行配置、模板和 Skills 哈希。
- 工具调用轨迹与生成尝试。
- changed slot 声明、Composer 版本和验证结果。
- 成功产物的 revision hash/package hash。

工程事件增加：

```text
full_deck_initialized
full_deck_generated
full_deck_revised
full_deck_revision_selected
full_deck_approved
```

### 12.3 公共错误码

至少提供：

```text
full_deck_already_initialized
full_deck_plan_invalid
full_deck_segment_invalid
full_deck_target_mismatch
full_deck_composition_failed
full_deck_package_invalid
full_deck_revision_not_found
full_deck_incomplete
stale_revision
active_job
```

错误消息必须同时说明原因和下一步操作。失败不能移动当前修订指针，也不能留下可被 project view 读取的半成品。

## 13. 分阶段实施任务

### 阶段 0：基线清理与技术验证

任务：

- 将设置页工具轮次 `max` 从 20 对齐为 100，补前端契约测试。
- 为现有样品包计算稳定的规范化页面内容图哈希，验证对当前 HTML-PPT Skill 输出和资产路径转换有效。
- 做一个最小 Composer 技术验证：从两个页段包按指定页序生成一个完整包，检查资产命名空间和离线导出。
- 固化 Composer 输入、输出和转换版本，不在业务路径中临时拼接 HTML。

验收：

- 同一输入连续组装两次得到相同 package hash。
- 样品来源页的页面内容哈希可在组装清单中逐页核对。
- 生成包能在安全预览和解压后的离线环境中翻页。

### 阶段 1：状态、模型、存储与原子进入

任务：

- 新增 Pydantic 模型和字段校验。
- Schema v4 迁移及全稿表投影/回填。
- 扩展 `STAGE_IDS`、工作流 YAML、capabilities 和进度快照。
- 实现 `enter_full_deck` 原子业务方法和 API。
- 初始化全稿 R1 页面清单及当前指针。
- 补充事件、分支、阶段重跑和上游 stale 传播。

验收：

- 点击一次即可从当前可发布样品进入 `ppt_full / ready_to_generate`，包括已有已确认样品的存量工程。
- 样品状态、全稿 R1、页面槽位、检查点和事件要么全部提交，要么全部不提交。
- R1 页数与大纲一致，样品映射页均为 `ready`，其余页均为 `pending`。
- 重复请求和旧 checkpoint 不会创建重复全稿。

### 阶段 2：全稿工作区与历史逻辑

任务：

- 新建 `frontend/static/js/full-deck.js`，按样品页结构实现全稿视图。
- 改造样品确认按钮和前端 action handler，成功后直接打开全稿。
- 增加全稿详情、历史列表、恢复、分支、预览和导出 API 客户端。
- R1 显示页面槽位摘要和样品来源页预览。
- 实现全稿历史指针切换及当前版本样式。
- 更新阶段导航、状态标签、欢迎页范围和事件文案。

验收：

- 页面跳转无确认弹窗、无整页刷新。
- 全稿 UI 的预览、反馈、尝试、历史与样品页保持同一层级和操作习惯。
- 历史切换只移动指针，下一次操作从选中版本继续。
- 键盘、焦点、错误提示、窄屏和 16:9 预览通过前端契约测试与人工检查。

### 阶段 3：真实全稿生成

任务：

- 新增 `ppt_full` 模型绑定和 Prompt 模板。
- 扩展 `StartJobRequest`、Job dispatcher、审计和状态页事件。
- 实现 pending 页段划分、页段 DraftPackage、自动修复和页段校验。
- 实现生产级 Composer、composition manifest 和完整包校验。
- 创建全稿修订、包 artifact、尝试摘要和预览/导出。
- 为全稿配置文件数、单文件和整包大小上限。

验收：

- 示例 10 页大纲、样品第 4–5 页时，只向 Agent 下发 1–3 和 6–10 两个目标段。
- 最终包严格为 10 页，页序与页面清单一致。
- composition manifest 能追踪每页来源。
- 任一页段失败时不发布全稿修订；成功重试后只提交一次。
- 真实模型运行、刷新恢复、取消和多 worker 并发行为通过验证。

### 阶段 4：全稿修改、重新生成与版本历史

任务：

- 实现 `revise_full_deck` 和 `regenerate_full_deck`。
- 增加 changed slots 声明及未声明页面引用不变校验。
- 支持修改任意全稿页面并保留样品血缘。
- 新修订生成后重新 Composer，移动当前指针并更新历史。
- 完成历史恢复后继续修改、历史版本分叉和从版本创建工程分支。
- UI 展示变更页、反馈、状态和尝试原因。

验收：

- 修改来自样品的页面会创建新全稿修订，父修订仍可恢复和预览。
- 未被声明为修改目标的页面 content ref 与父修订完全一致。
- 从 R1、R2 分别继续修改能形成正确父子链。
- 生成失败、CAS 冲突或用户取消时当前版本不改变。

### 阶段 5：全稿确认与最终验收入口

任务：

- 实现 `approve_full_deck`。
- 主按钮改为“确认全稿并进入验收”。
- 增加 `acceptance / ready_for_review` 工作区入口和只读全稿信息。
- 完成全稿阶段进度快照、时间线和审计导出。
- 运行全量回归和真实工程迁移测试。

验收：

- 只有完整且当前的全稿修订可以确认。
- 确认和阶段切换同事务完成。
- 已确认版本仍可通过提交新意见创建后续全稿修订。
- 老工程、样品工程和全稿工程均能正常打开、分支、回看和导出。

## 14. 预计文件改动清单

| 文件/目录 | 任务 |
| --- | --- |
| `agent_core/models.py` | FullDeck、页面槽位、修订和内容引用模型 |
| `agent_core/workflow.py` | 原子进入、生成、修改、恢复、确认和 capability |
| `agent_core/full_deck_composer.py` | 确定性组装与 composition manifest |
| `storage/sqlite_schema.py` | Schema v4 表和索引 |
| `storage/project_store.py` | 全稿 externalize/hydrate、历史、artifact、快照和迁移 |
| `storage/prompt_audit.py` | 全稿尝试链投影 |
| `main_front.py` | 请求模型、全稿 API、预览和导出 |
| `configs/runtime.py` | `ppt_full` 模型状态和全稿资源上限 |
| `model_config.yaml` | `ppt_full` 默认模型绑定 |
| `prompt_engine/templates/ppt_full.md` | 首次全稿页段生成 Prompt |
| `prompt_engine/templates/ppt_full_revision.md` | 全稿修改 Prompt |
| `workflows/ppt_agent_v1.yaml` | `ppt_full` 与 `acceptance` 状态 |
| `frontend/static/js/api.js` | 全稿 API 客户端 |
| `frontend/static/js/app.js` | 阶段路由、action、Job 和事件标签 |
| `frontend/static/js/full-deck.js` | 全稿预览、反馈、尝试与历史 |
| `frontend/static/css/main.css` | 全稿页面槽位和响应式样式 |
| `design-system/ppt-agent/pages/ppt-full.md` | 全稿页面设计覆盖规则 |
| `tests/` | 模型、存储、工作流、API、前端和 Composer 测试 |

## 15. 测试矩阵

### 15.1 单元测试

- 页面清单初始化、顺序、唯一性和样品映射。
- FullDeckRevision hash 的确定性和父修订绑定。
- 页面内容哈希提取。
- 页段划分：样品在开头、中间、末尾、多页和单页。
- Agent 返回页号缺失、重复、不连续、越界和数量不符。
- Composer 资产同名、缺失、路径越界、哈希损坏和重复 slide ID。
- 修改目标声明与实际内容引用变化核对。

### 15.2 存储测试

- v3 → v4 幂等迁移。
- 全稿修订、页面、包和 artifacts 同事务提交。
- 并发进入全稿只成功一次。
- 历史指针切换不新增修订。
- Checkpoint 恢复、分支、阶段回看和 stale 传播。
- 失败事务不留下数据库引用；未引用 artifact 可安全清理。

### 15.3 API 测试

- 样品确认并直接进入全稿。
- 旧 checkpoint、错误 revision hash、活动 Job 和未知字段。
- 当前全稿、历史详情、恢复、分支、预览、导出和确认。
- 全稿 Job 创建、轮询、取消、失败恢复和幂等。
- CSP、CORP、nosniff、缓存和 ZIP 文件名。

### 15.4 前端测试

- 阶段栏显示 PPT 全稿且不再标记为 future。
- capability 缺失时不展示无效动作。
- 进入按钮 loading、错误恢复和成功直达。
- R1 页面槽位状态、完整包预览、修改表单和历史版本。
- 历史选择器 `aria-pressed`、焦点环、键盘操作和当前状态。
- 工具轮次输入上限为 100。
- 320、375、768、1024、1280、1536 px 下无页面级横向滚动。

### 15.5 端到端场景

1. 生成样品 → 确认进入全稿 → R1 初始化。
2. 生成完整全稿 → 预览 → 导出 → 确认。
3. 修改普通页面 → 产生 R2 → 切回 R1 → 从 R1 继续产生 R3。
4. 修改样品来源页面 → 产生新修订 → 校验 R1 仍可恢复。
5. 页段生成失败 → 自动修复 → 成功发布；修复仍失败时不移动指针。
6. 页面刷新、服务重启、多 worker 竞争和取消任务。
7. 修改大纲后旧全稿变为 stale，历史仍可只读查看。

## 16. 完成定义

满足以下条件才算全稿阶段完成：

- 用户可从样品页一次点击直接进入全稿。
- 全稿拥有稳定、有序、可追溯、可扩展的页面清单。
- 初版全稿准确继承样品来源页面，并生成其余页面。
- Composer 以确定性方式产出完整、安全、可导出的 HTML-PPT。
- 用户可在全稿工作区修改任意页面，且每次成功修改形成不可变新修订。
- 全稿历史支持查看、恢复、继续修改和创建工程分支。
- 全稿页在结构、预览、修改、尝试和历史体验上与样品页一致。
- 所有写动作具有 checkpoint/revision 并发保护，失败不会覆盖成功状态。
- Schema 迁移、真实模型验证、全量自动化测试、安全检查和响应式检查全部通过。

## 17. 实施顺序与提交建议

建议按以下独立提交推进，每个提交都保持测试可运行：

1. `Align runtime bounds and prove composer contract`
2. `Add full-deck domain model and schema v4`
3. `Enter full deck atomically from approved sample`
4. `Add full-deck workspace and revision history`
5. `Generate missing full-deck segments and compose package`
6. `Support versioned full-deck revisions from user feedback`
7. `Approve full deck and enter acceptance`
8. `Harden migration, security, accessibility and end-to-end tests`

每一阶段完成后先运行定向测试，再运行全量测试；涉及真实模型的阶段还需保留一份“页段 → 工具轨迹 → 校验 → Composer → 修订”的实测审计记录。
