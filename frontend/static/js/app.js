import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";

const icons = {
  file: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 13h6M9 17h5"/></svg>',
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.3 4.2L17 9l-3.7 1.8L12 15l-1.3-4.2L7 9l3.7-1.8zM18.5 15l.7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7z"/></svg>',
  branch: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M8 9c4 0 4-2 8-2"/></svg>',
};

const STAGES = [
  { id: "intake", label: "任务卡" },
  { id: "intake_clarify", label: "澄清问题" },
  { id: "narrative_structure", label: "叙事结构" },
  { id: "slide_outline", label: "逐页大纲" },
  { id: "ppt_sample", label: "PPT 样品", future: true },
  { id: "ppt_full", label: "PPT 全稿", future: true },
  { id: "acceptance", label: "确认验收", future: true },
];

const STATE_LABELS = {
  intake: "任务卡",
  intake_clarify: "澄清问题",
  narrative_structure: "叙事结构",
  slide_outline: "逐页大纲",
};

const PHASE_LABELS = {
  ready_for_clarification: "准备澄清",
  waiting_clarification: "等待回答",
  ready_to_generate: "待生成",
  waiting_human_approval: "等待确认",
  completed: "已完成",
};

const MODEL_STATE_LABELS = {
  intake_clarify: "澄清问题",
  narrative_structure: "叙事结构",
  slide_outline: "逐页大纲",
};

const EVENT_LABELS = {
  project_created: "创建工程",
  clarification_generated: "生成澄清问题",
  clarification_answered: "提交澄清答案",
  document_generated: "生成文档",
  document_revised: "保存文档修订",
  document_approved: "确认文档",
  branch_created: "创建分支",
  branch_switched: "切换分支",
};

const state = {
  projects: [], project: null, branches: null, runtime: null,
  view: "workspace", busy: false, focusStage: null,
};

const content = document.querySelector("#content");
const projectList = document.querySelector("#project-list");
const projectDialog = document.querySelector("#project-dialog");
const editorDialog = document.querySelector("#editor-dialog");
const editor = document.querySelector("#editor");
let editorContext = null;
let sidebarPinned = false;
let renderGeneration = 0;
let trackedJobId = null;

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function formatDate(value) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch { return String(value); }
}

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " is-error" : ""}`;
  node.setAttribute("role", error ? "alert" : "status");
  node.textContent = message;
  document.querySelector("#toasts").append(node);
  window.setTimeout(() => node.remove(), 4400);
}

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* browser storage is optional */ } }
function storageRemove(key) { try { localStorage.removeItem(key); } catch { /* browser storage is optional */ } }

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll("button[data-action]").forEach((button) => { button.disabled = busy; });
}

function currentDocument(type) {
  const history = state.project?.documents?.[type] || [];
  return history.at(-1) || null;
}

function activeStageIndex(project = state.project) {
  return { intake: 0, intake_clarify: 1, narrative_structure: 2, slide_outline: 3 }[project?.state] ?? 0;
}

function jobLabel(operation) {
  return ({
    start_clarification: "生成澄清问题",
    generate_narrative: "生成叙事结构",
    generate_outline: "生成逐页大纲",
    regenerate_narrative: "重新生成叙事结构",
    regenerate_outline: "重新生成逐页大纲",
  })[operation] || "执行后台任务";
}

function setTopContext() {
  const wrap = document.querySelector("#topnav-project");
  wrap.hidden = !state.project;
  if (!state.project) return;
  document.querySelector("#topnav-project-name").textContent = state.project.title || state.project.project_id;
  document.querySelector("#topnav-branch").textContent = `分支 ${state.project.branch || "main"}`;
}

function markActiveTab() {
  document.querySelectorAll(".topnav__tab").forEach((tab) => {
    if (tab.dataset.view === state.view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
}

function applySidebar(hover = false) {
  const expanded = sidebarPinned || hover;
  document.querySelector("#app").classList.toggle("sidebar-expanded", expanded);
  document.querySelector("#sidebar-toggle").setAttribute("aria-expanded", String(expanded));
}

function progressCard() {
  const project = state.project;
  const active = activeStageIndex();
  const outlineApproved = currentDocument("slide_outline")?.status === "approved";
  const steps = STAGES.map((stage, index) => {
    const done = index < active || (index === 3 && outlineApproved);
    const current = index === active && !(index === 3 && outlineApproved);
    const viewable = stage.id === "narrative_structure" && currentDocument("narrative_structure")
      || stage.id === "slide_outline" && currentDocument("slide_outline");
    const classes = ["step", done ? "is-done" : "", current ? "is-current" : "", viewable ? "is-viewable" : ""].filter(Boolean).join(" ");
    const attrs = viewable ? `data-stage-view="${stage.id}" title="查看${stage.label}"` : "disabled";
    return `<button class="${classes}" type="button" ${attrs}><span class="step__bar"></span><span>${done ? "✓ " : ""}${escapeHtml(stage.label)}</span></button>`;
  }).join("");
  const job = project.active_job;
  const jobRow = job
    ? `<div class="job-progress" role="status"><span class="spinner" aria-hidden="true"></span><strong>正在${escapeHtml(jobLabel(job.operation))}…</strong><span>刷新页面不会丢失进度。</span><button class="btn btn--secondary" type="button" data-action="cancel_job">取消任务</button></div>`
    : '<div class="job-progress" role="status"><span class="badge badge--success">已同步</span><span>当前没有正在运行的后台任务。</span></div>';
  return `<section class="panel section ia-section progress-card"><div class="section__head"><div><h2>创作进度</h2><p>当前分支 ${escapeHtml(project.branch || "main")} · 检查点 ${escapeHtml(project.checkpoint_id.slice(-8))}</p></div><div class="section__actions"><span class="badge badge--info">${escapeHtml(PHASE_LABELS[project.phase] || project.phase)}</span><button class="btn btn--secondary" type="button" data-action="open_branches">${icons.branch}<span>分支管理</span></button></div></div><div class="stepper" aria-label="工作流进度">${steps}</div>${jobRow}</section>`;
}

function stagePanel(title, subtitle, body, status = "") {
  return `<section class="panel section ia-section stage"><div class="section__head"><div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(subtitle)}</p></div>${status}</div><div class="panel__body">${body}</div></section>`;
}

function taskSummary(task = state.project.task_card) {
  const values = [
    ["演示目标", task.objective], ["主要听众", task.audience || "待澄清"],
    ["演示场合", task.occasion || "待澄清"], ["目标页数", task.target_slide_count || "待澄清"],
  ];
  return `<div class="task-summary">${values.map(([label, value]) => `<div class="summary-item"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
}

function intakeView() {
  const body = `${taskSummary()}<div class="empty-state"><span class="empty-state__icon">${icons.spark}</span><h3>从关键问题开始</h3><p>Agent 会先识别影响叙事方向的缺失信息，再进入内容设计。</p><button class="btn btn--primary" data-action="start_clarification">生成澄清问题</button></div>`;
  return stagePanel("任务卡", "任务信息已保存；每一步都会生成可恢复的检查点。", body, '<span class="badge badge--success">可恢复</span>');
}

function clarificationView() {
  const card = state.project.question_card;
  if (!card) return loadingPanel("正在准备澄清问题…");
  const questions = card.questions.map((question, index) => `<article class="question-card"><fieldset><legend>${index + 1}. ${escapeHtml(question.prompt)}</legend><p class="question-impact">${escapeHtml(question.impact)}</p><div class="options">${question.options.map((option) => `<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="${escapeHtml(option.value)}" ${option.recommended ? "checked" : ""}><span>${escapeHtml(option.label)}</span>${option.recommended ? "<small>推荐</small>" : ""}</label>`).join("")}<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="__custom__"><span>自定义答案</span></label><input class="input" name="custom_${escapeHtml(question.question_id)}" aria-label="${escapeHtml(question.prompt)}的自定义答案" placeholder="输入你的答案"></div></fieldset></article>`).join("");
  const body = `<form id="clarification-form"><div class="question-list">${questions}</div><div class="sticky-actions"><p>答案会作为叙事结构的事实输入。</p><button class="btn btn--primary" type="submit">提交答案</button></div></form>`;
  return stagePanel("澄清问题", "只问必要问题；推荐项已标注，也可以填写自己的答案。", body, `<span class="badge badge--info">${card.questions.length} 个问题</span>`);
}

function readyDocumentView(type) {
  const narrative = type === "narrative_structure";
  const title = narrative ? "叙事结构" : "逐页大纲";
  const body = `<div class="empty-state"><span class="empty-state__icon">${icons.spark}</span><h3>${narrative ? "准备生成叙事结构" : "叙事结构已确认"}</h3><p>${narrative ? "Agent 会选择适合任务的叙事方法，并输出可直接编辑的 Markdown 文档。" : "Agent 将基于已确认叙事，规划每一页的目的、核心信息和视觉方向。"}</p><button class="btn btn--primary" data-action="${narrative ? "generate_narrative" : "generate_outline"}">${narrative ? "生成叙事结构" : "生成逐页大纲"}</button></div>`;
  return stagePanel(title, narrative ? "建立完整叙事主线" : "把叙事落到每一页", body, '<span class="badge badge--warning">待生成</span>');
}

function documentView(type) {
  const document = currentDocument(type);
  if (!document) return readyDocumentView(type);
  const narrative = type === "narrative_structure";
  const approved = document.status === "approved";
  const complete = !narrative && approved;
  const callout = complete ? '<div class="callout"><strong>一期制作完成。</strong> 逐页大纲已确认，可作为后续 PPT 样品制作的输入。</div>' : "";
  const actions = approved
    ? `<div class="sticky-actions"><p>编辑会创建新修订，并使该阶段确认${narrative ? "及下游大纲" : ""}失效。</p><div class="button-row"><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑此修订</button>${narrative ? '<button class="btn btn--primary" data-action="continue_outline">查看逐页大纲</button>' : ""}</div></div>`
    : `<div class="sticky-actions"><p>保存编辑会创建新修订；重新生成会保留当前版本历史。</p><div class="button-row"><button class="btn btn--secondary" data-action="regenerate_document" data-type="${type}">重新生成</button><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑</button><button class="btn btn--primary" data-action="approve_document" data-type="${type}">确认并继续</button></div></div>`;
  const status = `<div class="document-status"><span class="badge ${approved ? "badge--success" : "badge--warning"}">${approved ? "已确认" : "待确认"}</span><span class="badge badge--info">修订 ${document.revision}</span></div>`;
  return stagePanel(narrative ? "叙事结构" : "逐页大纲", narrative ? "故事如何推进" : "每一页讲什么", `${callout}<article class="document">${renderMarkdown(document.markdown_body)}</article>${actions}`, status);
}

function loadingPanel(message) {
  return stagePanel("Agent 正在创作", "任务在后台执行，刷新页面也不会丢失进度。", `<div class="empty-state"><span class="empty-state__icon"><span class="spinner" aria-hidden="true"></span></span><h3>${escapeHtml(message)}</h3><p>完成后工作区会自动刷新。</p></div>`, '<span class="badge badge--info">运行中</span>');
}

function workspaceMarkup() {
  if (!state.project) return welcomeMarkup();
  let stage;
  if (state.focusStage === "narrative_structure" && currentDocument("narrative_structure")) stage = documentView("narrative_structure");
  else if (state.focusStage === "slide_outline" && currentDocument("slide_outline")) stage = documentView("slide_outline");
  else if (state.project.active_job) stage = loadingPanel(`正在${jobLabel(state.project.active_job.operation)}…`);
  else if (state.project.state === "intake") stage = intakeView();
  else if (state.project.state === "intake_clarify") stage = clarificationView();
  else if (state.project.state === "narrative_structure") stage = state.project.phase === "ready_to_generate" ? readyDocumentView("narrative_structure") : documentView("narrative_structure");
  else stage = state.project.phase === "ready_to_generate" ? readyDocumentView("slide_outline") : documentView("slide_outline");
  return `${progressCard()}<div class="workspace">${stage}</div>`;
}

function welcomeMarkup() {
  return `<div class="hero"><section class="panel hero__main"><p class="eyebrow">Presentation workspace</p><h1>从想法到逐页大纲</h1><p>PPT Agent 把关键决策留给你，把叙事组织交给 Agent。澄清、生成、编辑、确认和分支都保存在可恢复检查点中。</p><button class="btn btn--primary" data-action="new_project">新建 PPT 工程</button></section><aside class="panel hero__aside"><div><span class="stat-label">一期范围</span><div class="stat-value">4 个阶段</div></div><div class="hint">任务卡 → 澄清问题 → 叙事结构 → 逐页大纲</div></aside></div>`;
}

function settingsMarkup(ctx) {
  const cards = ctx.model_bindings.map((binding) => `<article class="model-card" data-model-state="${binding.state}"><div class="model-card__head"><div><h3>${escapeHtml(MODEL_STATE_LABELS[binding.state] || binding.state)}</h3><small>${escapeHtml(binding.state)}</small></div><span class="badge badge--info">推理模型</span></div><div class="field"><label for="provider-${binding.state}">Provider</label><input class="input" id="provider-${binding.state}" data-field="provider" list="provider-options" required value="${escapeHtml(binding.provider)}"></div><div class="field"><label for="model-${binding.state}">模型</label><input class="input" id="model-${binding.state}" data-field="model" required value="${escapeHtml(binding.model)}"></div><div class="field"><label for="base-url-${binding.state}">Base URL</label><input class="input" id="base-url-${binding.state}" data-field="base_url" type="url" placeholder="https://api.openai.com/v1" value="${escapeHtml(binding.base_url || "")}"><small>留空时使用 SDK 默认地址。</small></div><div class="field"><label for="fallback-${binding.state}">备用模型</label><input class="input" id="fallback-${binding.state}" data-field="fallback_model" value="${escapeHtml(binding.fallback_model || "")}" placeholder="可选"></div><div class="field"><label for="parameters-${binding.state}">调用参数（JSON）</label><textarea class="input json-input" id="parameters-${binding.state}" data-field="parameters" spellcheck="false">${escapeHtml(JSON.stringify(binding.parameters || {}, null, 2))}</textarea></div></article>`).join("");
  const p = ctx.policy;
  return `<div class="page-head"><div><p class="eyebrow">Runtime configuration</p><h1>设置</h1><p class="lede">模型路由与运行策略会写回配置文件并立即生效；密钥值始终只从环境变量读取。</p></div><span class="badge badge--success">可编辑</span></div><form id="settings-form"><section class="panel section ia-section"><div class="section__head"><div><h2>模型配置</h2><p>每个一期阶段可独立选择 Provider、模型、服务地址与调用参数。</p></div><div class="field" style="margin:0;min-width:210px"><label for="model-config-id">配置 ID</label><input class="input" id="model-config-id" name="model_config_id" required value="${escapeHtml(ctx.model_config_id)}"></div></div><datalist id="provider-options"><option value="mock"><option value="openai"><option value="azure"><option value="openai-compatible"></datalist><div class="settings-grid">${cards}</div></section><section class="panel section ia-section"><div class="section__head"><div><h2>运行策略</h2><p>控制澄清预算、工具轮次、超时和 Skill 读取预算。</p></div><span class="badge badge--info">runtime.yaml</span></div><div class="settings-field-grid"><div class="field"><label for="max-auto-questions">单轮自动提问上限</label><input class="input" id="max-auto-questions" name="max_auto_questions" type="number" min="0" max="8" required value="${p.max_auto_questions}"></div><div class="field"><label for="clarification-budget">澄清问题总预算</label><input class="input" id="clarification-budget" name="clarification_total_budget" type="number" min="0" max="30" required value="${p.clarification_total_budget}"></div><div class="field"><label for="question-preference">提问偏好</label><select class="input" id="question-preference" name="question_preference"><option value="proactive" ${p.question_preference === "proactive" ? "selected" : ""}>主动澄清</option><option value="minimal" ${p.question_preference === "minimal" ? "selected" : ""}>最少提问</option><option value="none" ${p.question_preference === "none" ? "selected" : ""}>不主动提问</option></select></div><div class="field"><label for="model-timeout">模型超时（秒）</label><input class="input" id="model-timeout" name="model_timeout_seconds" type="number" min="1" max="600" step="1" required value="${p.model_timeout_seconds}"></div><div class="field"><label for="max-tool-rounds">最大工具轮次</label><input class="input" id="max-tool-rounds" name="max_tool_rounds" type="number" min="0" max="20" required value="${p.max_tool_rounds}"></div><div class="field"><label for="read-per-call">单次读取上限（字符）</label><input class="input" id="read-per-call" name="max_read_chars_per_call" type="number" min="100" max="100000" required value="${p.max_read_chars_per_call}"></div><div class="field"><label for="read-per-job">单任务读取上限（字符）</label><input class="input" id="read-per-job" name="max_read_chars_per_job" type="number" min="100" max="500000" required value="${p.max_read_chars_per_job}"></div></div><div class="config-hashes"><div class="hint"><strong>运行配置哈希</strong><div class="code">${escapeHtml(ctx.runtime_hash)}</div></div><div class="hint"><strong>模型配置哈希</strong><div class="code">${escapeHtml(ctx.model_hash)}</div></div></div></section><section class="panel section ia-section"><div class="section__head"><div><h2>Agent 权限与 Skills</h2><p>${escapeHtml(ctx.read_permission)}</p></div><span class="badge badge--info">只读</span></div><div class="question-list">${ctx.skills.map((skill) => `<div class="question-card"><strong>${escapeHtml(skill.name)}</strong><p class="question-impact">${escapeHtml(skill.description)}</p><code class="code">${escapeHtml(skill.path)}</code></div>`).join("") || '<p class="lede">当前没有可用 Skill。</p>'}</div></section><div class="settings-actions"><div><strong>保存后立即用于下一次模型调用</strong><p>已创建工程的历史产物不会被改写。</p><div class="field-error" id="settings-error" role="alert"></div></div><button class="btn btn--primary" id="save-settings" type="submit">保存设置</button></div></form>`;
}

function readSettingsForm(form) {
  const bindings = [...form.querySelectorAll("[data-model-state]")].map((card) => {
    const parametersInput = card.querySelector('[data-field="parameters"]');
    let parameters;
    try { parameters = JSON.parse(parametersInput.value || "{}"); }
    catch { parametersInput.setAttribute("aria-invalid", "true"); throw new Error(`${MODEL_STATE_LABELS[card.dataset.modelState]}的调用参数不是有效 JSON。`); }
    if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") {
      parametersInput.setAttribute("aria-invalid", "true");
      throw new Error(`${MODEL_STATE_LABELS[card.dataset.modelState]}的调用参数必须是 JSON 对象。`);
    }
    parametersInput.removeAttribute("aria-invalid");
    const value = (name) => card.querySelector(`[data-field="${name}"]`).value.trim();
    return {
      state: card.dataset.modelState,
      model_role: "reasoning_llm",
      provider: value("provider"), model: value("model"), parameters,
      fallback_model: value("fallback_model") || null,
      base_url: value("base_url") || null,
    };
  });
  const number = (name) => Number(form.elements[name].value);
  return {
    model_config_id: form.elements.model_config_id.value.trim(),
    model_bindings: bindings,
    policy: {
      max_auto_questions: number("max_auto_questions"),
      clarification_total_budget: number("clarification_total_budget"),
      question_preference: form.elements.question_preference.value,
      model_timeout_seconds: number("model_timeout_seconds"),
      max_tool_rounds: number("max_tool_rounds"),
      max_read_chars_per_call: number("max_read_chars_per_call"),
      max_read_chars_per_job: number("max_read_chars_per_job"),
    },
  };
}

async function statusMarkup() {
  if (!state.project) return { markup: '<div class="empty-state"><span class="empty-state__icon">' + icons.file + '</span><h2>尚未打开工程</h2><p>从左侧目录打开一个工程后，这里会显示运行状态、分支和事件记录。</p></div>', branches: null };
  const p = state.project;
  const [events, branches] = await Promise.all([api.timeline(p.project_id), api.branches(p.project_id)]);
  const job = p.active_job;
  const timeline = events.slice().reverse().map((event) => `<li><time>${escapeHtml(formatDate(event.at))}</time><div><strong>${escapeHtml(EVENT_LABELS[event.event] || "记录进度")}</strong><div class="code">${escapeHtml(event.checkpoint_id)}</div></div></li>`).join("") || "<li>暂无事件</li>";
  return { markup: `<div class="page-head"><div><p class="eyebrow">Project status</p><h1>${escapeHtml(p.title)}</h1><p class="lede">集中查看 Agent 状态、工程信息、分支与真实事件记录。</p></div><span class="badge ${job ? "badge--info" : "badge--success"}">${job ? "运行中" : "已同步"}</span></div><section class="panel section ia-section"><div class="section__head"><div><h2>Agent 运行状态</h2><p>来自后台任务与当前检查点。</p></div></div><div class="metric-grid"><div class="metric"><span>当前阶段</span><strong>${escapeHtml(STATE_LABELS[p.state] || p.state)}</strong></div><div class="metric"><span>当前分支</span><strong>${escapeHtml(branches.current)}</strong></div><div class="metric"><span>分支数量</span><strong>${branches.items.length}</strong></div></div>${job ? `<div class="job-progress"><span class="spinner"></span>正在${escapeHtml(jobLabel(job.operation))}…</div>` : ""}</section><div class="status-grid"><section class="panel section ia-section"><div class="section__head"><div><h2>事件日志</h2><p>所有修改、确认和分支操作均保留记录。</p></div></div><ol class="timeline">${timeline}</ol></section><div class="status-side"><section class="panel section ia-section"><div class="section__head"><div><h2>工程信息</h2></div></div><div class="task-summary" style="grid-template-columns:1fr"><div class="summary-item"><span>工程 ID</span><strong>${escapeHtml(p.project_id)}</strong></div><div class="summary-item"><span>当前阶段</span><strong>${escapeHtml(PHASE_LABELS[p.phase] || p.phase)}</strong></div><div class="summary-item"><span>当前检查点</span><strong class="code">${escapeHtml(p.checkpoint_id)}</strong></div></div></section><section class="panel section ia-section"><div class="section__head"><div><h2>原始任务</h2></div></div>${taskSummary(p.task_card)}</section></div></div>`, branches };
}

async function render() {
  const generation = ++renderGeneration;
  markActiveTab();
  setTopContext();
  if (state.view === "workspace") {
    content.innerHTML = workspaceMarkup();
    wireWorkspace();
    return;
  }
  content.innerHTML = '<section class="panel section"><div class="empty-state"><span class="spinner" aria-hidden="true"></span><p>正在读取…</p></div></section>';
  try {
    if (state.view === "status") {
      const result = await statusMarkup();
      if (generation !== renderGeneration) return;
      if (result.branches) state.branches = result.branches;
      content.innerHTML = result.markup;
      return;
    }
    state.runtime ||= await api.runtime();
    if (generation !== renderGeneration) return;
    content.innerHTML = settingsMarkup(state.runtime);
    wireSettings();
  } catch (error) {
    if (generation !== renderGeneration) return;
    content.innerHTML = `<section class="panel section"><div class="empty-state"><h2>页面加载失败</h2><p>${escapeHtml(error.message)}</p></div></section>`;
    toast(error.message, true);
  }
}

function renderProjectList() {
  if (!state.projects.length) {
    projectList.innerHTML = '<div class="sidebar__empty">还没有工程。创建第一个 PPT 任务开始工作。</div>';
    return;
  }
  projectList.innerHTML = state.projects.map((project) => `<button class="project-item" type="button" data-project="${escapeHtml(project.project_id)}" aria-current="${state.project?.project_id === project.project_id}"><span class="project-item__avatar" aria-hidden="true">${escapeHtml(project.project_id.slice(0, 1).toUpperCase())}</span><span class="project-item__text"><strong>${escapeHtml(project.title)}</strong><span>${escapeHtml(STATE_LABELS[project.state] || "等待开始")}</span></span></button>`).join("");
  projectList.querySelectorAll("[data-project]").forEach((button) => button.addEventListener("click", () => openProject(button.dataset.project)));
}

async function loadProjects() {
  try {
    const [health, projects] = await Promise.all([api.health(), api.projects()]);
    state.projects = projects;
    document.querySelector("#health-text").textContent = health.status === "ok" ? "服务已就绪" : "服务部分降级";
    document.querySelector("#health-dot").style.background = health.status === "ok" ? "var(--success)" : "var(--warning)";
  } catch (error) {
    document.querySelector("#health-text").textContent = "服务未连接";
    document.querySelector("#health-dot").style.background = "var(--danger)";
    toast(error.message, true);
  }
  renderProjectList();
}

async function openProject(id) {
  try {
    const [project, branches] = await Promise.all([api.project(id), api.branches(id)]);
    state.project = project;
    state.branches = branches;
    state.focusStage = null;
    state.view = "workspace";
    sidebarPinned = false;
    applySidebar(false);
    renderProjectList();
    await render();
    if (project.active_job) void pollJob(project.active_job.job_id);
  } catch (error) { toast(error.message, true); }
}

async function refreshCurrent() {
  if (!state.project) { await loadProjects(); await render(); return; }
  const [project, branches] = await Promise.all([api.project(state.project.project_id), api.branches(state.project.project_id)]);
  state.project = project;
  state.branches = branches;
  await loadProjects();
  await render();
}

async function pollJob(jobId) {
  if (trackedJobId === jobId) return;
  trackedJobId = jobId;
  try {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      let job;
      try { job = await api.job(jobId); } catch (error) { toast(error.message, true); return; }
      if (!["queued", "running"].includes(job.status)) {
        await refreshCurrent();
        toast(job.status === "succeeded" ? "任务已完成。" : `任务未完成：${job.error?.message || job.status}`, job.status !== "succeeded");
        return;
      }
      if (!state.project || state.project.active_job?.job_id !== jobId) return;
    }
  } finally {
    if (trackedJobId === jobId) trackedJobId = null;
  }
}

async function runJob(operation) {
  if (!state.project) return;
  setBusy(true);
  try {
    const job = await api.startJob(state.project.project_id, { operation, checkpoint_id: state.project.checkpoint_id });
    state.project.active_job = job;
    await render();
    void pollJob(job.job_id);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

function wireWorkspace() {
  content.querySelectorAll("[data-stage-view]").forEach((button) => button.addEventListener("click", () => {
    state.focusStage = button.dataset.stageView;
    void render();
  }));
  content.querySelectorAll("button[data-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.action;
    if (action === "new_project") { showProjectDialog(); return; }
    if (action === "open_branches") { await openBranchDialog(); return; }
    if (action === "start_clarification") await runJob("start_clarification");
    if (action === "generate_narrative") await runJob("generate_narrative");
    if (action === "generate_outline") await runJob("generate_outline");
    if (action === "continue_outline") { state.focusStage = "slide_outline"; await render(); }
    if (action === "edit_document") openEditor(button.dataset.type);
    if (action === "regenerate_document") await runJob(button.dataset.type === "narrative_structure" ? "regenerate_narrative" : "regenerate_outline");
    if (action === "approve_document") await approveDocument(button.dataset.type);
    if (action === "cancel_job" && state.project?.active_job) {
      try { await api.cancelJob(state.project.active_job.job_id); toast("已提交取消请求。"); }
      catch (error) { toast(error.message, true); }
    }
  }));
  document.querySelector("#clarification-form")?.addEventListener("submit", submitClarification);
}

function wireSettings() {
  const form = document.querySelector("#settings-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorNode = document.querySelector("#settings-error");
    errorNode.textContent = "";
    const button = document.querySelector("#save-settings");
    let payload;
    try { payload = readSettingsForm(form); }
    catch (error) { errorNode.textContent = error.message; return; }
    button.disabled = true;
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在保存…';
    try {
      state.runtime = await api.updateRuntime(payload);
      toast("设置已保存，将用于下一次模型调用。");
      await render();
    } catch (error) {
      errorNode.textContent = error.message;
      button.disabled = false;
      button.textContent = "保存设置";
    }
  });
}

async function submitClarification(event) {
  event.preventDefault();
  const card = state.project.question_card;
  const data = new FormData(event.currentTarget);
  const answers = {};
  for (const question of card.questions) {
    const selected = data.get(question.question_id);
    answers[question.question_id] = selected === "__custom__" ? String(data.get(`custom_${question.question_id}`) || "").trim() : String(selected || "");
    if (!answers[question.question_id]) { toast(`请回答：${question.prompt}`, true); return; }
  }
  setBusy(true);
  try {
    state.project = await api.answer(state.project.project_id, { checkpoint_id: state.project.checkpoint_id, question_card_id: card.question_card_id, answers });
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await render();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

async function approveDocument(type) {
  const document = currentDocument(type);
  setBusy(true);
  try {
    state.project = await api.approve(state.project.project_id, type, { checkpoint_id: state.project.checkpoint_id, revision_hash: document.revision_hash });
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await render();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

function openEditor(type) {
  const revision = currentDocument(type);
  editorContext = { type, revision: revision.revision };
  document.querySelector("#editor-title").textContent = `编辑${type === "narrative_structure" ? "叙事结构" : "逐页大纲"}`;
  const key = `ppt-agent-draft:${state.project.project_id}:${type}:${revision.revision}`;
  editorContext.key = key;
  const draft = storageGet(key);
  editor.value = draft || revision.markdown_body;
  document.querySelector("#draft-state").textContent = draft ? "已恢复浏览器草稿" : "草稿自动保存在此浏览器";
  editorDialog.showModal();
  editor.focus();
}

function closeEditor() { editorDialog.close(); editorContext = null; }

async function saveEditor() {
  if (!editorContext || !editor.value.trim()) return;
  const button = document.querySelector("#editor-save");
  button.disabled = true;
  try {
    state.project = await api.revise(state.project.project_id, editorContext.type, { checkpoint_id: state.project.checkpoint_id, markdown_body: editor.value });
    storageRemove(editorContext.key);
    state.focusStage = editorContext.type;
    state.branches = await api.branches(state.project.project_id);
    closeEditor();
    await render();
    toast("已保存为新修订。");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function showProjectDialog() {
  document.querySelector("#project-form").reset();
  document.querySelector("#form-error").textContent = "";
  projectDialog.showModal();
  window.setTimeout(() => document.querySelector("#project-id").focus(), 0);
}

async function createProject(event) {
  event.preventDefault();
  const button = document.querySelector("#create-button");
  const errorNode = document.querySelector("#form-error");
  errorNode.textContent = "";
  button.disabled = true;
  button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在创建…';
  const lines = document.querySelector("#constraints").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const duration = document.querySelector("#duration").value;
  try {
    state.project = await api.create({
      project_id: document.querySelector("#project-id").value.trim(),
      task_card: {
        title: document.querySelector("#task-title").value.trim(),
        objective: document.querySelector("#objective").value.trim(),
        audience: document.querySelector("#audience").value.trim(),
        occasion: document.querySelector("#occasion").value.trim(),
        target_slide_count: document.querySelector("#slide-count").value.trim(),
        duration_minutes: duration ? Number(duration) : null,
        known_facts: lines, constraints: [], source_refs: [],
      },
    });
    state.branches = await api.branches(state.project.project_id);
    state.view = "workspace";
    projectDialog.close();
    await loadProjects();
    await render();
    toast("工程已创建。");
  } catch (error) { errorNode.textContent = error.message; }
  finally { button.disabled = false; button.textContent = "创建工程"; }
}

async function openBranchDialog() {
  if (!state.project) { toast("请先打开一个工程。", true); return; }
  let payload;
  try { payload = await api.branches(state.project.project_id); }
  catch (error) { toast(error.message, true); return; }
  state.branches = payload;
  const blocked = Boolean(state.project.active_job);
  const dialog = document.createElement("dialog");
  dialog.className = "dialog branch-dialog";
  dialog.setAttribute("aria-labelledby", "branch-dialog-title");
  const items = payload.items.map((item) => {
    const head = item.checkpoints.find((checkpoint) => checkpoint.checkpoint_id === item.head_checkpoint_id) || item.checkpoints[0];
    const lineage = item.parent ? `自 ${item.parent} 创建` : item.name === "main" ? "主分支" : "历史分支";
    const meta = [lineage, head ? `${STATE_LABELS[head.state] || head.state} · ${formatDate(head.updated_at)}` : null].filter(Boolean).join(" · ");
    return `<div class="branch-item${item.current ? " is-current" : ""}"><div class="branch-item__name"><span>${escapeHtml(item.name)}</span>${item.current ? '<span class="badge badge--success">当前分支</span>' : ""}</div><div class="branch-item__meta">${escapeHtml(meta)}</div>${!item.current ? `<button class="btn btn--primary branch-item__actions" type="button" data-branch-switch="${escapeHtml(item.head_checkpoint_id)}" ${blocked ? "disabled" : ""}>切换到此分支</button>` : ""}</div>`;
  }).join("");
  const options = payload.checkpoints.map((checkpoint) => `<option value="${escapeHtml(checkpoint.checkpoint_id)}" ${checkpoint.checkpoint_id === state.project.checkpoint_id ? "selected" : ""}>${escapeHtml(checkpoint.branch)} · ${escapeHtml(STATE_LABELS[checkpoint.state] || checkpoint.state)} · ${escapeHtml(formatDate(checkpoint.updated_at))}</option>`).join("");
  dialog.innerHTML = `<div class="dialog__head"><div><p class="eyebrow">Version branches</p><h2 id="branch-dialog-title">查看与切换分支</h2></div><button class="icon-btn" type="button" data-dialog-close aria-label="关闭"><svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div class="dialog__body"><p class="hint">切换分支只改变当前查看与继续的位置，各分支历史保持不变。</p><div class="branch-list">${items}</div><form class="branch-create" id="branch-create-form"><h3>创建新分支</h3><p>可以从当前或任一历史检查点继续探索新的叙事方向。</p>${blocked ? '<div class="callout callout--warning">后台任务运行期间不能创建或切换分支。</div>' : ""}<div class="field"><label for="branch-name">分支名称</label><input class="input" id="branch-name" name="name" required minlength="2" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]{1,63}" placeholder="story-angle-b"></div><div class="field"><label for="branch-checkpoint">起点检查点</label><select class="input" id="branch-checkpoint" name="checkpoint_id">${options}</select></div><div class="field-error" id="branch-error" role="alert"></div><div class="button-row"><button class="btn btn--primary" type="submit" ${blocked ? "disabled" : ""}>创建并切换</button></div></form></div><div class="dialog__foot"><button class="btn btn--secondary" type="button" data-dialog-close>关闭</button></div>`;
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  dialog.querySelectorAll("[data-branch-switch]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "正在切换…";
    try {
      state.project = await api.switchBranch(state.project.project_id, button.dataset.branchSwitch);
      state.branches = await api.branches(state.project.project_id);
      state.focusStage = null;
      dialog.close();
      await loadProjects();
      await render();
      toast(`已切换到分支 ${state.project.branch}。`);
    } catch (error) { button.disabled = false; button.textContent = "切换到此分支"; toast(error.message, true); }
  }));
  dialog.querySelector("#branch-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const errorNode = form.querySelector("#branch-error");
    submit.disabled = true;
    submit.textContent = "正在创建…";
    try {
      state.project = await api.createBranch(state.project.project_id, { name: form.elements.name.value.trim(), checkpoint_id: form.elements.checkpoint_id.value });
      state.branches = await api.branches(state.project.project_id);
      state.focusStage = null;
      dialog.close();
      await loadProjects();
      await render();
      toast(`已创建并切换到分支 ${state.project.branch}。`);
    } catch (error) { errorNode.textContent = error.message; submit.disabled = false; submit.textContent = "创建并切换"; }
  });
  dialog.showModal();
}

async function setView(view) {
  state.view = view;
  await render();
  document.querySelector("#main").focus();
}

function bindChrome() {
  document.querySelector("#new-button").addEventListener("click", showProjectDialog);
  document.querySelector("#refresh-button").addEventListener("click", async () => {
    if (state.view === "settings") { state.runtime = null; await render(); return; }
    await refreshCurrent();
  });
  document.querySelector("#topnav-branch").addEventListener("click", openBranchDialog);
  document.querySelectorAll(".topnav__tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
  document.querySelector("#project-form").addEventListener("submit", createProject);
  document.querySelectorAll("#project-dialog [data-close]").forEach((button) => button.addEventListener("click", () => projectDialog.close()));
  projectDialog.addEventListener("cancel", (event) => { event.preventDefault(); projectDialog.close(); });
  document.querySelector("#editor-close").addEventListener("click", closeEditor);
  document.querySelector("#editor-cancel").addEventListener("click", closeEditor);
  document.querySelector("#editor-save").addEventListener("click", saveEditor);
  editor.addEventListener("input", () => { if (editorContext) { storageSet(editorContext.key, editor.value); document.querySelector("#draft-state").textContent = "草稿已保存"; } });
  const sidebar = document.querySelector("#sidebar");
  sidebar.addEventListener("mouseenter", () => applySidebar(true));
  sidebar.addEventListener("mouseleave", () => applySidebar(false));
  document.querySelector("#sidebar-toggle").addEventListener("click", () => { sidebarPinned = !sidebarPinned; applySidebar(false); });
}

async function boot() {
  bindChrome();
  await loadProjects();
  await render();
}

void boot();
