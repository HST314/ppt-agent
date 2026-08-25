import { api } from "./api.js";
import { fullDeckBody, hydrateFullDeckFrame } from "./full-deck.js";
import { isStoppedFullDeckSession } from "./full-deck-session.js";
import { createFullDeckWorkspaceController, isFullDeckSessionJob } from "./full-deck-workspace.js";
import { renderMarkdown } from "./markdown.js";
import { hydrateSampleFrame as hydrateFrame, readySampleBody, sampleBody } from "./samples.js";
import { captureStatusViewport, createStatusSignature, restoreStatusViewport, statusElapsedLabel } from "./status-view.js";

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
  { id: "ppt_sample", label: "PPT 样品" },
  { id: "ppt_full", label: "PPT 全稿" },
  { id: "acceptance", label: "确认验收" },
];

const STATE_LABELS = {
  intake: "任务卡",
  intake_clarify: "澄清问题",
  narrative_structure: "叙事结构",
  slide_outline: "逐页大纲",
  ppt_sample: "PPT 样品",
  ppt_full: "PPT 全稿",
  acceptance: "确认验收",
};

const PHASE_LABELS = {
  ready_for_clarification: "准备澄清",
  waiting_clarification: "等待回答",
  ready_to_generate: "待生成",
  waiting_human_approval: "等待确认",
  completed: "已完成",
  ready_for_review: "待最终验收",
};

const MODEL_STATE_LABELS = {
  intake_clarify: "澄清问题",
  narrative_structure: "叙事结构",
  slide_outline: "逐页大纲",
  ppt_sample: "PPT 样品",
  ppt_full: "PPT 全稿",
};

const EVENT_LABELS = {
  project_created: "创建工程",
  clarification_generated: "生成澄清问题",
  clarification_answered: "提交澄清答案",
  document_generated: "生成文档",
  document_revised: "保存文档修订",
  document_approved: "确认文档",
  sample_stage_started: "进入样品阶段",
  sample_generated: "生成 PPT 样品",
  sample_revised: "根据意见修改样品",
  sample_revision_selected: "切换当前样品版本",
  sample_approved: "确认 PPT 样品",
  full_deck_initialized: "初始化 PPT 全稿",
  full_deck_generated: "生成 PPT 全稿",
  full_deck_revised: "根据意见修改全稿",
  full_deck_revision_selected: "切换当前全稿版本",
  full_deck_approved: "确认 PPT 全稿",
  branch_created: "创建分支",
  branch_switched: "切换分支",
  tool_round_completed: "完成模型工具轮次",
};

const state = {
  projects: [], project: null, branches: null, runtime: null,
  view: "workspace", busy: false, focusStage: null,
  fullDeckSession: null,
  statusFilter: "all", statusQuery: "", statusOrder: "newest", expandedEventId: null,
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
let statusPollTimer = null;
let statusSearchTimer = null;
let statusPollInFlight = false;
let statusPollErrorShown = false;
let statusDataSignature = null;
let fullDeckWorkspace = null;

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function formatDate(value) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  } catch { return String(value); }
}

const ERROR_MESSAGES = {
  sample_json_incomplete: "样品输出不完整，自动修复后仍未成功，请重试。",
  sample_html_rejected: "样品含有不支持的内容，自动修复后仍未通过，请重试。",
  sample_output_invalid: "样品格式不正确，自动修复后仍未成功，请重试。",
  sample_package_invalid: "HTML-PPT 包格式不正确，自动修复后仍未成功，请重试。",
  full_deck_already_initialized: "全稿已经建立，请直接打开当前全稿工作区。",
  full_deck_plan_invalid: "样品与逐页大纲无法建立完整页面清单，请重新生成样品后重试。",
  full_deck_revision_not_found: "该全稿版本不存在于当前分支，请刷新历史列表。",
  full_deck_segment_invalid: "全稿页段未通过结构或安全校验，请重试。",
  full_deck_target_mismatch: "生成页段与目标页号不一致，请重试。",
  full_deck_composition_failed: "全稿组装未通过来源保真校验，请重试。",
  full_deck_package_invalid: "完整全稿包未通过安全或资源校验，请重试。",
  full_deck_incomplete: "全稿仍有未完成页面，请重新生成。",
  full_deck_session_active: "当前已有全稿生成会话，请继续查看现有进度。",
  full_deck_session_stale: "工程基线已变化，需要重新开始全稿生成。",
  full_deck_batch_failed: "当前批未完成，可重试当前批。",
  full_deck_preview_failed: "页段已保存，部分预览暂时无法组装，请重试当前批。",
  full_deck_finalization_failed: "页面已完成，正式全稿发布失败，请重试收尾。",
  full_deck_session_conflict: "会话版本已变化，已刷新到最新状态，请重试。",
  full_deck_directive_too_late: "全稿已进入最终组装，补充要求无法再进入生成链。",
  stale_revision: "工程已更新，请刷新后再执行此操作。",
  active_job: "当前有后台任务正在运行，请等待任务结束后重试。",
  invalid_model_output: "模型返回的内容格式不正确，请重试。",
  max_tool_rounds_exceeded: "达到当前工具轮次上限，可追加轮次从当前进度继续。",
  process_restarted: "服务已重启，请从上一成功点重试。",
  job_failed: "任务暂未完成，请重试。",
};

function userErrorMessage(error, fallback = "操作未完成，请稍后重试。") {
  const mapped = error?.code && ERROR_MESSAGES[error.code] ? ERROR_MESSAGES[error.code] : "";
  const message = typeof error?.message === "string" ? error.message.replace(/\s+/g, " ").trim() : "";
  const base = mapped || (message && /[\u3400-\u9fff]/.test(message) && message.length <= 96 ? message : "");
  const detail = typeof error?.detail === "string" ? error.detail.replace(/\s+/g, " ").trim().slice(0, 160) : "";
  if (base && detail) return `${base}（${detail}）`;
  if (base) return base;
  return fallback;
}

function announceWorkspace(message) {
  const region = document.querySelector("#workspace-status");
  if (region) region.textContent = message;
}

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " is-error" : ""}`;
  node.setAttribute("role", error ? "alert" : "status");
  const normalized = String(message || "").replace(/\s+/g, " ").trim();
  node.textContent = normalized.length > 96 ? `${normalized.slice(0, 95)}…` : normalized;
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

function headSample() {
  return state.project?.samples?.at(-1) || null;
}

function activeStageIndex(project = state.project) {
  return { intake: 0, intake_clarify: 1, narrative_structure: 2, slide_outline: 3, ppt_sample: 4, ppt_full: 5, acceptance: 6 }[project?.state] ?? 0;
}

function stageSnapshotMap(project = state.project) {
  return new Map((project?.progress_snapshots || []).map((item) => [item.stage, item]));
}

function jobLabel(operation) {
  return ({
    start_clarification: "生成澄清问题",
    generate_narrative: "生成叙事结构",
    generate_outline: "生成逐页大纲",
    regenerate_narrative: "重新生成叙事结构",
    regenerate_outline: "重新生成逐页大纲",
    generate_sample: "生成 PPT 样品",
    regenerate_sample: "重新生成 PPT 样品",
    revise_sample: "根据修改意见调整 PPT 样品",
    continue_sample: "追加轮次并继续生成 PPT 样品",
    generate_full_deck: "生成完整 HTML-PPT",
    regenerate_full_deck: "重新生成 PPT 全稿",
    revise_full_deck: "根据修改意见调整 PPT 全稿",
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
  const sampleApproved = headSample()?.status === "approved";
  const fullDeckApproved = state.project.full_deck_revision?.status === "approved";
  const snapshots = stageSnapshotMap(project);
  const steps = STAGES.map((stage, index) => {
    const done = index < active || (index === 3 && outlineApproved) || (index === 4 && sampleApproved) || (index === 5 && fullDeckApproved);
    const current = index === active && !(index === 3 && outlineApproved && state.project.state === "slide_outline") && !(index === 4 && sampleApproved) && !(index === 5 && fullDeckApproved);
    const snapshot = snapshots.get(stage.id);
    const viewable = Boolean(snapshot && !stage.future);
    const classes = ["step", done ? "is-done" : "", current ? "is-current" : "", viewable ? "is-viewable step--interactive" : ""].filter(Boolean).join(" ");
    const attrs = viewable
      ? `data-snapshot-stage="${stage.id}" aria-label="查看${stage.label}阶段任务快照" title="查看${stage.label}阶段任务快照"`
      : "disabled";
    return `<button class="${classes}" type="button" ${attrs}><span class="step__bar"></span><span>${done ? "✓ " : ""}${escapeHtml(stage.label)}</span></button>`;
  }).join("");
  const job = project.active_job;
  const jobRow = job
    ? `<div class="job-progress" role="status"><span class="spinner" aria-hidden="true"></span><strong>正在${escapeHtml(jobLabel(job.operation))}…</strong><span>刷新页面不会丢失进度。</span><button class="btn btn--secondary" type="button" data-action="cancel_job">取消任务</button></div>`
    : '<div class="job-progress" role="status"><span class="badge badge--success">已同步</span><span>当前没有正在运行的后台任务。</span></div>';
  return `<section class="panel section ia-section progress-card"><div class="section__head"><div><h2>创作进度</h2><p>当前分支 ${escapeHtml(project.branch || "main")} · 检查点 ${escapeHtml(project.checkpoint_id.slice(-8))} · 点击已保存阶段回看任务快照</p></div><div class="section__actions"><span class="badge badge--info">${escapeHtml(PHASE_LABELS[project.phase] || project.phase)}</span><button class="btn btn--secondary" type="button" data-action="open_branches">${icons.branch}<span>查看分支</span></button></div></div><div class="stepper" aria-label="工作流进度">${steps}</div>${jobRow}</section>`;
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
  if (!card) {
    const answeredRounds = (state.project.clarification_history || []).length;
    const body = `<div class="empty-state"><span class="empty-state__icon">${icons.spark}</span><h3>继续自动澄清</h3><p>前 ${answeredRounds} 轮答案已经保存。Agent 会带着这些答案判断是否还有值得追问的问题。</p><button class="btn btn--primary" data-action="start_clarification">继续分析</button></div>`;
    return stagePanel("澄清问题", "历史答案会完整传入下一轮，不会重复询问已回答内容。", body, `<span class="badge badge--info">已完成 ${answeredRounds} 轮</span>`);
  }
  const questions = card.questions.map((question, index) => `<article class="question-card"><fieldset><legend>${index + 1}. ${escapeHtml(question.prompt)}</legend><p class="question-impact">${escapeHtml(question.impact)}</p><div class="options">${question.options.map((option) => `<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="${escapeHtml(option.value)}" ${option.recommended ? "checked" : ""}><span>${escapeHtml(option.label)}</span>${option.recommended ? "<small>推荐</small>" : ""}</label>`).join("")}<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="__custom__"><span>自定义答案</span></label><input class="input" name="custom_${escapeHtml(question.question_id)}" aria-label="${escapeHtml(question.prompt)}的自定义答案" placeholder="输入你的答案"></div></fieldset></article>`).join("");
  const body = `<form id="clarification-form"><div class="question-list">${questions}</div><div class="sticky-actions"><p>答案会作为叙事结构的事实输入。</p><button class="btn btn--primary" type="submit">提交答案</button></div></form>`;
  return stagePanel("澄清问题", `第 ${card.round || 1} 轮；推荐项已标注，也可以填写自己的答案。`, body, `<span class="badge badge--info">${card.questions.length} 个问题</span>`);
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
  const legacyComplete = !narrative && approved && state.project.state === "slide_outline" && state.project.phase === "completed";
  const callout = legacyComplete ? '<div class="callout"><strong>逐页大纲已确认。</strong> 现在可以继续生成 PPT 样品。</div>' : "";
  const continueAction = narrative
    ? '<button class="btn btn--primary" data-action="continue_outline">查看逐页大纲</button>'
    : state.project.capabilities?.includes("start_sample_stage")
      ? '<button class="btn btn--primary" data-action="enter_sample">进入 PPT 样品</button>'
      : state.project.state === "ppt_sample"
        ? '<button class="btn btn--primary" data-action="continue_sample">查看 PPT 样品</button>'
        : "";
  const actions = approved
    ? `<div class="sticky-actions"><p>编辑会创建新修订，并使该阶段确认${narrative ? "及下游产物" : "及 PPT 样品"}失效。</p><div class="button-row"><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑此修订</button>${continueAction}</div></div>`
    : `<div class="sticky-actions"><p>保存编辑会创建新修订；重新生成会保留当前版本历史。</p><div class="button-row"><button class="btn btn--secondary" data-action="regenerate_document" data-type="${type}">重新生成</button><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑</button><button class="btn btn--primary" data-action="approve_document" data-type="${type}">确认并继续</button></div></div>`;
  const status = `<div class="document-status"><span class="badge ${approved ? "badge--success" : "badge--warning"}">${approved ? "已确认" : "待确认"}</span><span class="badge badge--info">修订 ${document.revision}</span></div>`;
  return stagePanel(narrative ? "叙事结构" : "逐页大纲", narrative ? "故事如何推进" : "每一页讲什么", `${callout}<article class="document">${renderMarkdown(document.markdown_body)}</article>${actions}`, status);
}

function samplePageCount() {
  return state.project?.sample_page_count || state.runtime?.policy?.sample_page_count || 2;
}

function readySampleView() {
  const count = samplePageCount();
  const preview = readySampleBody(
    count,
    icons.spark,
    state.project.sample_attempts || [],
    escapeHtml,
  );
  return stagePanel("PPT 样品", "确认视觉语言、信息层级与版式方向", preview, `<span class="badge badge--warning">${count} 页 · 待生成</span>`);
}

function hydrateSampleFrame() {
  hydrateFrame(content, headSample());
}

function sampleView() {
  const sample = headSample();
  if (!sample) return readySampleView();
  const view = sampleBody(sample, escapeHtml, {
    history: state.project.sample_revisions || [],
    attempts: state.project.sample_attempts || [],
    selectedHash: sample.revision_hash,
    canEnterFullDeck: state.project.capabilities?.includes("enter_full_deck"),
    fullDeckExists: Boolean(state.project.full_deck),
  });
  return stagePanel("HTML-PPT", "在通用安全预览器中检查完整文件包，再用自然语言让 AI 调整", view.body, view.status);
}

function fullDeckView() {
  const revision = state.project.full_deck_revision;
  if (!revision) return loadingPanel("正在读取全稿页面清单…");
  const capabilities = state.project.capabilities || [];
  const generationSession = state.fullDeckSession?.published_revision_hash
    && state.fullDeckSession.published_revision_hash !== revision.revision_hash
    ? null
    : state.fullDeckSession;
  const view = fullDeckBody(revision, escapeHtml, {
    history: state.project.full_deck_revisions || [],
    attempts: state.project.full_deck_attempts || [],
    selectedHash: revision.revision_hash,
    sample: headSample(),
    canGenerate: capabilities.includes("generate_full_deck"),
    canRegenerate: capabilities.includes("regenerate_full_deck"),
    canRevise: capabilities.includes("revise_full_deck"),
    canBranch: capabilities.includes("branch_full_deck_revision"),
    canApprove: capabilities.includes("approve_full_deck"),
    generationSession,
  });
  return `<section class="panel section ia-section stage"><div class="section__head"><div><h2 id="full-deck-title" tabindex="-1">HTML-PPT 全稿</h2><p>浏览完整页面清单、安全预览和不可变修订历史</p></div>${view.status}</div><div class="panel__body">${view.body}</div></section>`;
}

function acceptanceView() {
  const revision = state.project.full_deck_revision;
  if (!revision) return loadingPanel("正在读取最终验收基线…");
  const capabilities = state.project.capabilities || [];
  const view = fullDeckBody(revision, escapeHtml, {
    history: state.project.full_deck_revisions || [],
    attempts: state.project.full_deck_attempts || [],
    selectedHash: revision.revision_hash,
    sample: headSample(),
    canRevise: capabilities.includes("revise_full_deck"),
    canBranch: capabilities.includes("branch_full_deck_revision"),
    acceptanceMode: true,
    auditExportUrl: state.project.audit_export_url,
  });
  return `<section class="panel section ia-section stage acceptance-workspace"><div class="section__head"><div><h2 id="acceptance-title" tabindex="-1">确认验收</h2><p>只读核对已确认全稿、来源记录、时间线与审计证据</p></div><div class="document-status"><span class="badge badge--success">已进入验收</span><span class="badge badge--info">修订 ${revision.revision}</span></div></div><div class="panel__body">${view.body}</div></section>`;
}

function loadingPanel(message) {
  return stagePanel("Agent 正在创作", "任务在后台执行，刷新页面也不会丢失进度。", `<div class="empty-state"><span class="empty-state__icon"><span class="spinner" aria-hidden="true"></span></span><h3>${escapeHtml(message)}</h3><p>完成后工作区会自动刷新。</p></div>`, '<span class="badge badge--info">运行中</span>');
}

function workspaceMarkup() {
  if (!state.project) return welcomeMarkup();
  let stage;
  if (state.focusStage === "narrative_structure" && currentDocument("narrative_structure")) stage = documentView("narrative_structure");
  else if (state.focusStage === "slide_outline" && currentDocument("slide_outline")) stage = documentView("slide_outline");
  else if (state.focusStage === "ppt_sample" && headSample()) stage = sampleView();
  else if (state.focusStage === "ppt_full" && state.project.full_deck_revision) stage = fullDeckView();
  else if (state.fullDeckSession && isFullDeckSessionJob(state.project.active_job)) stage = fullDeckView();
  else if (state.project.active_job) stage = loadingPanel(`正在${jobLabel(state.project.active_job.operation)}…`);
  else if (state.project.state === "intake") stage = intakeView();
  else if (state.project.state === "intake_clarify") stage = clarificationView();
  else if (state.project.state === "narrative_structure") stage = state.project.phase === "ready_to_generate" ? readyDocumentView("narrative_structure") : documentView("narrative_structure");
  else if (state.project.state === "slide_outline") stage = state.project.phase === "ready_to_generate" ? readyDocumentView("slide_outline") : documentView("slide_outline");
  else if (state.project.state === "ppt_sample") stage = state.project.phase === "ready_to_generate" ? readySampleView() : sampleView();
  else if (state.project.state === "acceptance") stage = acceptanceView();
  else stage = fullDeckView();
  return `${progressCard()}<div class="workspace">${stage}</div>`;
}

function welcomeMarkup() {
  return `<div class="hero"><section class="panel hero__main"><p class="eyebrow">Presentation workspace</p><h1>从想法到完整演示</h1><p>PPT Agent 把关键决策留给你，把叙事组织、样品设计与完整页面规划交给 Agent。澄清、生成、编辑、确认和分支都保存在可恢复检查点中。</p><button class="btn btn--primary" data-action="new_project">新建 PPT 工程</button></section><aside class="panel hero__aside"><div><span class="stat-label">当前范围</span><div class="stat-value">7 个阶段</div></div><div class="hint">任务卡 → 澄清问题 → 叙事结构 → 逐页大纲 → PPT 样品 → PPT 全稿 → 确认验收</div></aside></div>`;
}

function settingsMarkup(ctx) {
  const cards = ctx.model_bindings.map((binding) => `<article class="model-card" data-model-state="${binding.state}"><div class="model-card__head"><div><h3>${escapeHtml(MODEL_STATE_LABELS[binding.state] || binding.state)}</h3><small>${escapeHtml(binding.state)}</small></div><span class="badge badge--info">推理模型</span></div><div class="field"><label for="provider-${binding.state}">Provider</label><input class="input" id="provider-${binding.state}" data-field="provider" list="provider-options" required value="${escapeHtml(binding.provider)}"></div><div class="field"><label for="model-${binding.state}">模型</label><input class="input" id="model-${binding.state}" data-field="model" required value="${escapeHtml(binding.model)}"></div><div class="field"><label for="base-url-${binding.state}">Base URL</label><input class="input" id="base-url-${binding.state}" data-field="base_url" type="url" placeholder="https://api.openai.com/v1" value="${escapeHtml(binding.base_url || "")}"><small>留空时使用 SDK 默认地址。</small></div><div class="field"><label for="fallback-${binding.state}">备用模型</label><input class="input" id="fallback-${binding.state}" data-field="fallback_model" value="${escapeHtml(binding.fallback_model || "")}" placeholder="可选"></div><div class="field"><label for="parameters-${binding.state}">调用参数（JSON）</label><textarea class="input json-input" id="parameters-${binding.state}" data-field="parameters" spellcheck="false">${escapeHtml(JSON.stringify(binding.parameters || {}, null, 2))}</textarea></div></article>`).join("");
  const p = ctx.policy;
  return `<div class="page-head"><div><p class="eyebrow">Runtime configuration</p><h1>设置</h1><p class="lede">模型路由与运行策略会写回配置文件并立即生效；密钥值始终只从环境变量读取。</p></div><span class="badge badge--success">可编辑</span></div><form id="settings-form"><section class="panel section ia-section"><div class="section__head"><div><h2>模型配置</h2><p>每个生成阶段可独立选择 Provider、模型、服务地址与调用参数。</p></div><div class="field" style="margin:0;min-width:210px"><label for="model-config-id">配置 ID</label><input class="input" id="model-config-id" name="model_config_id" required value="${escapeHtml(ctx.model_config_id)}"></div></div><datalist id="provider-options"><option value="ark"><option value="mock"><option value="openai"><option value="azure"><option value="openai-compatible"></datalist><div class="settings-grid">${cards}</div></section><section class="panel section ia-section"><div class="section__head"><div><h2>运行策略</h2><p>控制样品页数、澄清预算、工具轮次、超时和 Skill 读取预算。</p></div><span class="badge badge--info">runtime.yaml</span></div><div class="settings-field-grid"><div class="field"><label for="sample-page-count">HTML 样品页数</label><input class="input" id="sample-page-count" name="sample_page_count" type="number" min="1" max="6" required value="${p.sample_page_count}"><small>默认 2 页，下一次生成或重生成时生效。</small></div><div class="field"><label for="max-auto-questions">单轮自动提问上限</label><input class="input" id="max-auto-questions" name="max_auto_questions" type="number" min="0" max="8" required value="${p.max_auto_questions}"></div><div class="field"><label for="max-clarification-rounds">自动澄清轮数上限</label><input class="input" id="max-clarification-rounds" name="max_clarification_rounds" type="number" min="1" max="10" required value="${p.max_clarification_rounds}"><small>每轮答完后由模型判断是否继续。</small></div><div class="field"><label for="clarification-budget">澄清问题总预算</label><input class="input" id="clarification-budget" name="clarification_total_budget" type="number" min="0" max="30" required value="${p.clarification_total_budget}"></div><div class="field"><label for="question-preference">提问偏好</label><select class="input" id="question-preference" name="question_preference"><option value="proactive" ${p.question_preference === "proactive" ? "selected" : ""}>主动澄清</option><option value="minimal" ${p.question_preference === "minimal" ? "selected" : ""}>最少提问</option><option value="none" ${p.question_preference === "none" ? "selected" : ""}>不主动提问</option></select></div><div class="field"><label for="model-timeout">模型超时（秒）</label><input class="input" id="model-timeout" name="model_timeout_seconds" type="number" min="1" max="600" step="1" required value="${p.model_timeout_seconds}"></div><div class="field"><label for="max-tool-rounds">最大工具轮次</label><input class="input" id="max-tool-rounds" name="max_tool_rounds" type="number" min="0" max="100" required value="${p.max_tool_rounds}"></div><div class="field"><label for="read-per-call">单次读取上限（字符）</label><input class="input" id="read-per-call" name="max_read_chars_per_call" type="number" min="100" max="100000" required value="${p.max_read_chars_per_call}"></div><div class="field"><label for="read-per-job">单任务读取上限（字符）</label><input class="input" id="read-per-job" name="max_read_chars_per_job" type="number" min="100" max="500000" required value="${p.max_read_chars_per_job}"></div></div><div class="config-hashes"><div class="hint"><strong>运行配置哈希</strong><div class="code">${escapeHtml(ctx.runtime_hash)}</div></div><div class="hint"><strong>模型配置哈希</strong><div class="code">${escapeHtml(ctx.model_hash)}</div></div></div></section><section class="panel section ia-section"><div class="section__head"><div><h2>Agent 权限与 Skills</h2><p>${escapeHtml(ctx.read_permission)}</p></div><span class="badge badge--info">只读</span></div><div class="question-list">${ctx.skills.map((skill) => `<div class="question-card"><strong>${escapeHtml(skill.name)}</strong><p class="question-impact">${escapeHtml(skill.description)}</p><code class="code">${escapeHtml(skill.path)}</code></div>`).join("") || '<p class="lede">当前没有可用 Skill。</p>'}</div></section><div class="settings-actions"><div><strong>保存后立即用于下一次模型调用</strong><p>已创建工程的历史产物不会被改写。</p><div class="field-error" id="settings-error" role="alert"></div></div><button class="btn btn--primary" id="save-settings" type="submit">保存设置</button></div></form>`;
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
      max_clarification_rounds: number("max_clarification_rounds"),
      clarification_total_budget: number("clarification_total_budget"),
      question_preference: form.elements.question_preference.value,
      model_timeout_seconds: number("model_timeout_seconds"),
      max_tool_rounds: number("max_tool_rounds"),
      max_read_chars_per_call: number("max_read_chars_per_call"),
      max_read_chars_per_job: number("max_read_chars_per_job"),
      sample_page_count: number("sample_page_count"),
    },
  };
}

async function statusMarkup({ skipUnchanged = false } = {}) {
  if (!state.project) {
    const signature = "no-project";
    const markup = skipUnchanged && signature === statusDataSignature
      ? null
      : '<div class="empty-state"><span class="empty-state__icon">' + icons.file + '</span><h2>尚未打开工程</h2><p>从左侧目录打开一个工程后，这里会显示运行状态、分支和事件记录。</p></div>';
    return { markup, branches: null, activity: null, summary: null, signature };
  }
  const p = state.project;
  const [activity, branches] = await Promise.all([api.activity(p.project_id), api.branches(p.project_id)]);
  const summary = activity.summary;
  const signature = createStatusSignature(activity, branches);
  if (skipUnchanged && signature === statusDataSignature) return { markup: null, branches, activity, summary, signature };
  const job = summary.active_job;
  const liveProgress = job && summary.progress
    ? `第 ${summary.progress.round}/${summary.progress.round_limit} 轮 · ${summary.progress.tool_call_count} 次工具调用 · ${summary.progress.skill_read_count} 次 Skill 读取 · ${summary.progress.recent_action || "继续处理中"}`
    : "等待首个工具轮次返回…";
  const counts = activity.events.reduce((result, event) => {
    result[event.kind] = (result[event.kind] || 0) + 1;
    return result;
  }, {});
  const query = state.statusQuery.trim().toLocaleLowerCase();
  let filtered = activity.events.filter((event) => {
    if (state.statusFilter !== "all" && event.kind !== state.statusFilter) return false;
    if (!query) return true;
    return JSON.stringify([event.title, event.summary, event.details]).toLocaleLowerCase().includes(query);
  });
  filtered.sort((a, b) => state.statusOrder === "oldest"
    ? String(a.at).localeCompare(String(b.at))
    : String(b.at).localeCompare(String(a.at)));
  const kindLabels = { all: "全部", job: "任务", model: "模型", skill: "Skill", validation: "校验", artifact: "产物", project: "工程", error: "失败" };
  const filters = Object.entries(kindLabels).map(([kind, label]) => `<button class="status-filter${state.statusFilter === kind ? " is-active" : ""}" type="button" data-status-filter="${kind}" aria-pressed="${state.statusFilter === kind}">${label}<span>${kind === "all" ? activity.events.length : counts[kind] || 0}</span></button>`).join("");
  const density = activity.events.slice(0, 64).reverse().map((event) => `<span class="event-density__mark is-${escapeHtml(event.kind)}" title="${escapeHtml(`${kindLabels[event.kind] || event.kind} · ${formatDate(event.at)}`)}"></span>`).join("") || '<span class="event-density__empty">暂无事件</span>';

  const eventMarkup = (event) => {
    const expanded = state.expandedEventId === event.id;
    const title = EVENT_LABELS[event.title]
      || MODEL_STATE_LABELS[event.title]
      || (event.kind === "job" || event.kind === "error" ? jobLabel(event.title) : event.title);
    return `<article class="activity-event is-${escapeHtml(event.kind)}"><button class="activity-event__summary" type="button" data-event-expand="${escapeHtml(event.id)}" aria-expanded="${expanded}"><span class="activity-event__dot" aria-hidden="true"></span><span class="activity-event__main"><span class="activity-event__line"><strong>${escapeHtml(title)}</strong><span class="badge">${escapeHtml(kindLabels[event.kind] || event.kind)}</span></span><span>${escapeHtml(event.summary || "无摘要")}</span></span><time datetime="${escapeHtml(event.at)}">${escapeHtml(formatDate(event.at))}</time><span class="activity-event__chevron" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></svg></span></button>${expanded ? `<div class="activity-event__detail"><pre>${escapeHtml(JSON.stringify(event.details, null, 2))}</pre><button class="btn btn--secondary" type="button" data-copy-event="${escapeHtml(event.id)}">复制详情</button></div>` : ""}</article>`;
  };
  const eventsMarkup = filtered.length
    ? filtered.map((event) => eventMarkup(event)).join("")
    : `<div class="status-empty"><strong>没有匹配的事件</strong><p>尝试清空搜索词，或切换到“全部”类型。</p><button class="btn btn--secondary" type="button" data-status-reset>清除筛选</button></div>`;
  const markup = `<div class="page-head"><div><p class="eyebrow">Agent observability</p><h1>${escapeHtml(p.title)}</h1><p class="lede">实时汇总后台任务、模型调用、Skill 读取、校验、产物保存和错误。</p></div><span class="badge ${job ? "badge--info" : "badge--success"}">${job ? "实时运行中" : "已同步"}</span></div><section class="panel section ia-section status-overview"><div class="metric-grid metric-grid--status"><div class="metric"><span>当前任务</span><strong>${escapeHtml(job ? jobLabel(job.operation) : "暂无运行任务")}</strong></div><div class="metric"><span>阶段 / 耗时</span><strong id="status-elapsed">${escapeHtml(statusElapsedLabel(summary, STATE_LABELS))}</strong></div><div class="metric"><span>模型</span><strong>${escapeHtml(summary.model || "尚未调用")}</strong><small>${escapeHtml(summary.provider || "")}</small></div><div class="metric"><span>事件 / 失败</span><strong>${summary.event_count} / ${summary.error_count}</strong></div></div>${job ? `<div class="job-progress" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><strong>正在${escapeHtml(jobLabel(job.operation))}</strong><span>${escapeHtml(liveProgress)}</span><small>状态台每 2 秒自动更新。</small></div>` : ""}<div class="event-density" aria-label="最近 ${Math.min(activity.events.length, 64)} 条事件的类型密度"><span>事件密度</span><div>${density}</div></div></section><section class="panel section ia-section"><div class="section__head"><div><h2>统一事件流</h2><p>展开事件可查看经过脱敏的结构化详情并复制。</p></div><div class="status-sort"><label for="status-order">排序</label><select class="input" id="status-order"><option value="newest" ${state.statusOrder === "newest" ? "selected" : ""}>最新在前</option><option value="oldest" ${state.statusOrder === "oldest" ? "selected" : ""}>时间顺序</option></select></div></div><div class="status-controls"><div class="status-filters" aria-label="按事件类型筛选">${filters}</div><div class="status-search"><label class="sr-only" for="status-search">搜索事件</label><input class="input" id="status-search" type="search" value="${escapeHtml(state.statusQuery)}" placeholder="搜索操作、文件、模型或错误…"><span>${filtered.length} 条</span></div></div><div class="activity-list">${eventsMarkup}</div></section>`;
  return { markup, branches, activity, summary, signature };
}

async function refreshStatusView({ skipUnchanged = false } = {}) {
  await render({ showLoading: false, preserveStatusViewport: true, skipUnchangedStatus: skipUnchanged });
}

function wireStatus(activity) {
  content.querySelectorAll("[data-status-filter]").forEach((button) => button.addEventListener("click", async () => {
    state.statusFilter = button.dataset.statusFilter;
    await refreshStatusView();
  }));
  content.querySelector("#status-order")?.addEventListener("change", async (event) => {
    state.statusOrder = event.target.value;
    await refreshStatusView();
  });
  content.querySelector("#status-search")?.addEventListener("input", (event) => {
    state.statusQuery = event.target.value;
    if (statusSearchTimer) window.clearTimeout(statusSearchTimer);
    statusSearchTimer = window.setTimeout(async () => {
      statusSearchTimer = null;
      await refreshStatusView();
      const input = content.querySelector("#status-search");
      input?.focus();
      input?.setSelectionRange(input.value.length, input.value.length);
    }, 180);
  });
  content.querySelector("[data-status-reset]")?.addEventListener("click", async () => {
    state.statusFilter = "all";
    state.statusQuery = "";
    await refreshStatusView();
  });
  content.querySelectorAll("[data-event-expand]").forEach((button) => button.addEventListener("click", async () => {
    state.expandedEventId = state.expandedEventId === button.dataset.eventExpand ? null : button.dataset.eventExpand;
    await refreshStatusView();
  }));
  content.querySelectorAll("[data-copy-event]").forEach((button) => button.addEventListener("click", async () => {
    const item = activity.events.find((event) => event.id === button.dataset.copyEvent);
    if (!item) return;
    try { await navigator.clipboard.writeText(JSON.stringify(item.details, null, 2)); toast("事件详情已复制。"); }
    catch { toast("浏览器未允许复制，请手动选择详情。", true); }
  }));
}

async function render({ showLoading = true, preserveStatusViewport = false, skipUnchangedStatus = false } = {}) {
  const generation = ++renderGeneration;
  markActiveTab();
  setTopContext();
  if (state.view === "workspace") {
    content.innerHTML = workspaceMarkup();
    wireWorkspace();
    hydrateSampleFrame();
    hydrateFullDeckFrame(content, state.project?.full_deck_revision, headSample(), state.fullDeckSession);
    return;
  }
  if (showLoading) {
    hydrateFrame(content, null, 0);
    content.innerHTML = '<section class="panel section"><div class="empty-state"><span class="spinner" aria-hidden="true"></span><p>正在读取…</p></div></section>';
  }
  try {
    if (state.view === "status") {
      const result = await statusMarkup({ skipUnchanged: skipUnchangedStatus });
      if (generation !== renderGeneration) return;
      if (result.branches) state.branches = result.branches;
      if (result.markup === null) {
        const elapsed = content.querySelector("#status-elapsed");
        if (elapsed && result.summary) elapsed.textContent = statusElapsedLabel(result.summary, STATE_LABELS);
        statusPollErrorShown = false;
        return;
      }
      const viewport = preserveStatusViewport ? captureStatusViewport(content) : null;
      content.innerHTML = result.markup;
      wireStatus(result.activity);
      restoreStatusViewport(content, viewport);
      statusDataSignature = result.signature;
      statusPollErrorShown = false;
      return;
    }
    state.runtime ||= await api.runtime();
    if (generation !== renderGeneration) return;
    content.innerHTML = settingsMarkup(state.runtime);
    wireSettings();
  } catch (error) {
    if (generation !== renderGeneration) return;
    if (!showLoading && preserveStatusViewport) {
      if (!statusPollErrorShown) toast("状态自动更新失败，稍后将自动重试。", true);
      statusPollErrorShown = true;
      return;
    }
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
    fullDeckWorkspace.stopPolling();
    if (statusPollTimer) { window.clearInterval(statusPollTimer); statusPollTimer = null; }
    const [project, branches] = await Promise.all([api.project(id), api.branches(id)]);
    state.project = project;
    state.fullDeckSession = await fullDeckWorkspace.load(project);
    state.branches = branches;
    state.focusStage = null;
    state.view = "workspace";
    sidebarPinned = false;
    applySidebar(false);
    renderProjectList();
    await render();
    if (state.fullDeckSession && !isStoppedFullDeckSession(state.fullDeckSession.status)) fullDeckWorkspace.startPolling();
    else if (project.active_job) void pollJob(project.active_job.job_id);
  } catch (error) { toast(error.message, true); }
}

async function refreshCurrent() {
  if (!state.project) { await loadProjects(); await render(); return; }
  fullDeckWorkspace.stopPolling();
  const [project, branches] = await Promise.all([api.project(state.project.project_id), api.branches(state.project.project_id)]);
  state.project = project;
  state.fullDeckSession = await fullDeckWorkspace.load(project);
  state.branches = branches;
  await loadProjects();
  await render();
  if (state.fullDeckSession && !isStoppedFullDeckSession(state.fullDeckSession.status)) fullDeckWorkspace.startPolling();
  else if (project.active_job) void pollJob(project.active_job.job_id);
}

async function syncFullDeckSessionForCurrentProject() {
  fullDeckWorkspace.stopPolling();
  state.fullDeckSession = await fullDeckWorkspace.load(state.project);
}

async function pollJob(jobId) {
  if (trackedJobId === jobId) return;
  trackedJobId = jobId;
  try {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      let job;
      try { job = await api.job(jobId); } catch (error) { toast(userErrorMessage(error), true); return; }
      if (!["queued", "running"].includes(job.status)) {
        await refreshCurrent();
        toast(job.status === "succeeded" ? "任务已完成。" : userErrorMessage(job.error, "任务未完成，请重试。"), job.status !== "succeeded");
        return;
      }
      if (!state.project || state.project.active_job?.job_id !== jobId) return;
    }
  } finally {
    if (trackedJobId === jobId) trackedJobId = null;
  }
}

async function runJob(operation, extra = {}) {
  if (!state.project) return false;
  setBusy(true);
  try {
    const job = await api.startJob(state.project.project_id, { operation, checkpoint_id: state.project.checkpoint_id, ...extra });
    state.project.active_job = job;
    await render();
    void pollJob(job.job_id);
    return true;
  } catch (error) { toast(userErrorMessage(error), true); return false; }
  finally { setBusy(false); }
}

async function resumeSample(button) {
  if (!state.project) return false;
  const promptCallId = button.dataset.promptCallId;
  const select = content.querySelector(`[data-resume-rounds="${CSS.escape(promptCallId)}"]`);
  const additionalRounds = Number(select?.value || 10);
  setBusy(true);
  try {
    const job = await api.resumeSample(
      state.project.project_id,
      promptCallId,
      {
        checkpoint_id: state.project.checkpoint_id,
        additional_rounds: additionalRounds,
      },
    );
    state.project.active_job = job;
    await render();
    void pollJob(job.job_id);
    return true;
  } catch (error) {
    toast(userErrorMessage(error), true);
    return false;
  } finally {
    setBusy(false);
  }
}

function wireWorkspace() {
  fullDeckWorkspace.wire();
  content.querySelectorAll("[data-sample-revision]").forEach((button) => button.addEventListener("click", () => selectSampleRevision(button.dataset.sampleRevision)));
  content.querySelectorAll("[data-full-deck-revision]").forEach((button) => button.addEventListener("click", () => selectFullDeckRevision(button.dataset.fullDeckRevision)));
  content.querySelectorAll("[data-snapshot-stage]").forEach((button) => button.addEventListener("click", () => {
    openSnapshotDialog(button.dataset.snapshotStage);
  }));
  content.querySelectorAll("[data-resume-rounds]").forEach((select) => select.addEventListener("change", () => {
    const button = content.querySelector(`[data-action="resume_sample"][data-prompt-call-id="${CSS.escape(select.dataset.resumeRounds)}"]`);
    if (button) button.textContent = `追加 ${select.value} 轮并继续`;
  }));
  content.querySelectorAll("button[data-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.action;
    if (action === "new_project") { showProjectDialog(); return; }
    if (action === "open_branches") { await openBranchDialog(); return; }
    if (action === "start_clarification") await runJob("start_clarification");
    if (action === "generate_narrative") await runJob("generate_narrative");
    if (action === "generate_outline") await runJob("generate_outline");
    if (action === "generate_sample") await runJob("generate_sample");
    if (action === "continue_outline") { state.focusStage = "slide_outline"; await render(); }
    if (action === "continue_sample") { state.focusStage = "ppt_sample"; await render(); }
    if (action === "continue_full_deck") { state.focusStage = "ppt_full"; await render(); }
    if (action === "enter_sample") await enterSampleStage();
    if (action === "enter_full_deck") await enterFullDeck(button);
    if (action === "edit_document") openEditor(button.dataset.type);
    if (action === "regenerate_document") await runJob(button.dataset.type === "narrative_structure" ? "regenerate_narrative" : "regenerate_outline");
    if (action === "approve_document") await approveDocument(button.dataset.type);
    if (action === "regenerate_sample") await runJob("regenerate_sample");
    if (action === "resume_sample") await resumeSample(button);
    if (action === "approve_sample") await approveSample();
    if (action === "branch_sample_revision") await branchFromSampleRevision(button.dataset.revisionHash);
    if (action === "generate_full_deck") await fullDeckWorkspace.startGeneration(button);
    if (action === "approve_full_deck") await approveFullDeck(button);
    if (action === "regenerate_full_deck") {
      const original = button.innerHTML;
      button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在启动重新生成…';
      const started = await runJob("regenerate_full_deck", {
        revision_hash: state.project.full_deck_revision?.revision_hash,
      });
      if (!started && button.isConnected) button.innerHTML = original;
    }
    if (action === "branch_full_deck_revision") await branchFromFullDeckRevision(button.dataset.revisionHash);
    if (action === "cancel_job" && state.project?.active_job) {
      try { await api.cancelJob(state.project.active_job.job_id); toast("已提交取消请求。"); }
      catch (error) { toast(error.message, true); }
    }
  }));
  document.querySelector("#clarification-form")?.addEventListener("submit", submitClarification);
  document.querySelector("#sample-feedback-form")?.addEventListener("submit", submitSampleFeedback);
  document.querySelector("#full-deck-feedback-form")?.addEventListener("submit", submitFullDeckFeedback);
}

function wireSettings() {
  const form = document.querySelector("#settings-form");
  const toolRoundsInput = form?.elements.max_tool_rounds;
  if (toolRoundsInput) {
    const hint = document.createElement("small");
    hint.textContent = "默认 20，最多 100；更高值会增加最长耗时与模型调用。";
    toolRoundsInput.parentElement.append(hint);
  }
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
    if (state.project.active_job) void pollJob(state.project.active_job.job_id);
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

async function enterSampleStage() {
  setBusy(true);
  try {
    state.project = await api.enterSample(state.project.project_id, state.project.checkpoint_id);
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await render();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

async function enterFullDeck(button) {
  const sample = headSample();
  if (!sample) return;
  const scrollTop = window.scrollY;
  const original = button.innerHTML;
  const errorNode = document.querySelector("#sample-enter-error");
  if (errorNode) errorNode.textContent = "";
  setBusy(true);
  button.disabled = true;
  button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在进入全稿…';
  try {
    state.project = await api.enterFullDeck(state.project.project_id, {
      checkpoint_id: state.project.checkpoint_id,
      sample_revision_hash: sample.revision_hash,
    });
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await loadProjects();
    await render();
    document.querySelector("#full-deck-title")?.focus();
    toast("已确认样品并进入 PPT 全稿。");
  } catch (error) {
    const message = userErrorMessage(error, "暂时无法进入全稿，请刷新工程后重试。");
    if (errorNode) errorNode.textContent = message;
    button.disabled = false;
    button.innerHTML = original;
    window.scrollTo(0, scrollTop);
    button.focus();
    toast(message, true);
  } finally { setBusy(false); }
}

async function submitSampleFeedback(event) {
  event.preventDefault();
  const feedback = String(new FormData(event.currentTarget).get("feedback") || "").trim();
  const errorNode = document.querySelector("#sample-feedback-error");
  if (!feedback) {
    errorNode.textContent = "请先输入希望 AI 调整的内容。";
    event.currentTarget.elements.feedback.focus();
    return;
  }
  errorNode.textContent = "";
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在提交…';
  const started = await runJob("revise_sample", { feedback });
  if (!started && submit.isConnected) {
    submit.disabled = false;
    submit.textContent = "让 AI 修改";
  }
}

async function submitFullDeckFeedback(event) {
  event.preventDefault();
  const revision = state.project.full_deck_revision;
  if (!revision) return;
  const feedback = String(new FormData(event.currentTarget).get("feedback") || "").trim();
  const errorNode = document.querySelector("#full-deck-feedback-error");
  if (!feedback) {
    errorNode.textContent = "请先输入希望 AI 调整的页面或内容。";
    event.currentTarget.elements.feedback.focus();
    return;
  }
  errorNode.textContent = "";
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在启动全稿修改…';
  const started = await runJob("revise_full_deck", {
    revision_hash: revision.revision_hash,
    feedback,
  });
  if (!started && submit.isConnected) {
    submit.disabled = false;
    submit.textContent = state.project.state === "acceptance" ? "创建后续全稿修订" : "让 AI 修改";
  }
}

async function approveFullDeck(button) {
  const revision = state.project.full_deck_revision;
  if (!revision) return;
  const original = button.innerHTML;
  const errorNode = document.querySelector("#full-deck-approve-error");
  if (errorNode) errorNode.textContent = "";
  setBusy(true);
  button.disabled = true;
  button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在进入验收…';
  try {
    state.project = await api.approveFullDeck(state.project.project_id, {
      checkpoint_id: state.project.checkpoint_id,
      revision_hash: revision.revision_hash,
    });
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await loadProjects();
    await render();
    document.querySelector("#acceptance-title")?.focus();
    toast("全稿已确认并进入最终验收。");
  } catch (error) {
    const message = userErrorMessage(error, "暂时无法确认全稿，请刷新工程后重试。");
    if (errorNode) errorNode.textContent = message;
    if (button.isConnected) {
      button.disabled = false;
      button.innerHTML = original;
      button.focus();
    }
    toast(message, true);
  } finally { setBusy(false); }
}

async function approveSample() {
  const sample = headSample();
  if (!sample) return;
  setBusy(true);
  try {
    state.project = await api.approveSample(state.project.project_id, {
      checkpoint_id: state.project.checkpoint_id,
      revision_hash: sample.revision_hash,
    });
    state.focusStage = null;
    state.branches = await api.branches(state.project.project_id);
    await render();
    toast("PPT 样品已确认。");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

async function selectSampleRevision(revisionHash) {
  const current = headSample();
  if (!current) return;
  if (revisionHash === current.revision_hash) return;
  setBusy(true);
  try {
    state.project = await api.restoreSample(
      state.project.project_id,
      revisionHash,
      state.project.checkpoint_id,
    );
    state.focusStage = "ppt_sample";
    state.branches = await api.branches(state.project.project_id);
    await loadProjects();
    await render();
    document.querySelector("#sample-preview-frame")?.focus();
    toast("已切换当前版本；没有创建新修订。");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

async function branchFromSampleRevision(revisionHash) {
  const revision = (state.project.sample_revisions || []).find((item) => item.revision_hash === revisionHash);
  if (!revision) return;
  const name = automaticBranchName(`sample-r${revision.revision}`);
  setBusy(true);
  try {
    state.project = await api.branchFromSample(state.project.project_id, revisionHash, {
      checkpoint_id: state.project.checkpoint_id,
      name,
    });
    await syncFullDeckSessionForCurrentProject();
    state.branches = await api.branches(state.project.project_id);
    state.focusStage = "ppt_sample";
    await loadProjects();
    await render();
    toast(`已从修订 ${revision.revision} 创建并切换到分支 ${name}。`);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(false); }
}

async function selectFullDeckRevision(revisionHash) {
  const current = state.project.full_deck_revision;
  if (!current) return;
  if (revisionHash === current.revision_hash) return;
  setBusy(true);
  try {
    state.project = await api.restoreFullDeck(
      state.project.project_id,
      revisionHash,
      state.project.checkpoint_id,
    );
    state.focusStage = "ppt_full";
    state.branches = await api.branches(state.project.project_id);
    await loadProjects();
    await render();
    [...document.querySelectorAll("[data-full-deck-revision]")]
      .find((button) => button.dataset.fullDeckRevision === revisionHash)
      ?.focus();
    toast("已切换当前全稿版本；没有创建新修订。");
  } catch (error) { toast(userErrorMessage(error), true); }
  finally { setBusy(false); }
}

async function branchFromFullDeckRevision(revisionHash) {
  const revision = (state.project.full_deck_revisions || [])
    .find((item) => item.revision_hash === revisionHash);
  if (!revision) return;
  const name = automaticBranchName(`full-deck-r${revision.revision}`);
  setBusy(true);
  try {
    state.project = await api.branchFromFullDeck(
      state.project.project_id,
      revisionHash,
      { checkpoint_id: state.project.checkpoint_id, name },
    );
    await syncFullDeckSessionForCurrentProject();
    state.branches = await api.branches(state.project.project_id);
    state.focusStage = "ppt_full";
    await loadProjects();
    await render();
    document.querySelector("#full-deck-title")?.focus();
    toast(`已从全稿修订 ${revision.revision} 创建并切换到分支 ${name}。`);
  } catch (error) { toast(userErrorMessage(error), true); }
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
    fullDeckWorkspace.stopPolling();
    state.fullDeckSession = null;
    state.branches = await api.branches(state.project.project_id);
    state.view = "workspace";
    projectDialog.close();
    await loadProjects();
    await render();
    toast("工程已创建。");
  } catch (error) { errorNode.textContent = error.message; }
  finally { button.disabled = false; button.textContent = "创建工程"; }
}

function automaticBranchName(stage, date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${stage.replaceAll("_", "-")}-${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function snapshotDocument(snapshot, type) {
  return (snapshot?.documents?.[type] || []).at(-1) || null;
}

function snapshotContent(stage, item) {
  const snapshot = item.snapshot || {};
  if (stage === "intake") return taskSummary(snapshot.task_card || {});
  if (stage === "intake_clarify") {
    const answers = snapshot.clarification_answers || {};
    const history = snapshot.clarification_history || [];
    const questions = history.length
      ? history.flatMap((entry) => (entry.questions || []).map((question) => ({
          ...question,
          savedAnswer: entry.answers?.[question.question_id],
          round: entry.round,
        })))
      : (snapshot.question_card?.questions || []);
    if (!questions.length) return '<p class="snapshot-empty">该检查点尚未生成澄清问题。</p>';
    return `<ol class="snapshot-list">${questions.map((question) => {
      const answer = question.savedAnswer || answers[question.field]?.answer;
      const options = (question.options || []).map((option) => option.label).join(" / ");
      return `<li><strong>${question.round ? `第 ${question.round} 轮 · ` : ""}${escapeHtml(question.prompt)}</strong>${answer ? `<span class="snapshot-answer">回答：${escapeHtml(answer)}</span>` : options ? `<span>${escapeHtml(options)}</span>` : ""}</li>`;
    }).join("")}</ol>`;
  }
  if (stage === "ppt_sample") {
    const sample = (snapshot.samples || []).at(-1);
    if (!sample) return '<p class="snapshot-empty">该阶段的输入已保存，样品尚未生成。</p>';
    const slides = sample.package?.slides || sample.pages || [];
    return `<div class="sample-snapshot-summary"><strong>${slides.length} 页 HTML-PPT 包</strong><span>修订 ${sample.revision} · ${sample.status === "approved" ? "已确认" : "待确认"}</span><ol>${slides.map((page) => `<li>${escapeHtml(page.title)}</li>`).join("")}</ol></div>`;
  }
  if (stage === "ppt_full" || stage === "acceptance") {
    const root = snapshot.full_deck || {};
    const revision = (snapshot.full_deck_revisions || []).find(
      (item) => item.revision_hash === root.current_revision_hash,
    );
    if (!revision) return '<p class="snapshot-empty">该阶段的页面清单尚未建立。</p>';
    const pages = revision.plan?.pages || [];
    const ready = pages.filter((page) => page.status === "ready").length;
    const stateLabel = revision.status === "approved" ? "已确认为验收基线" : `${ready}/${pages.length} 页已就绪`;
    return `<div class="sample-snapshot-summary"><strong>${pages.length} 页 HTML-PPT 全稿</strong><span>修订 ${revision.revision} · ${stateLabel}</span><ol>${pages.map((page) => `<li>第 ${page.outline_ref?.source_slide_number || page.position + 1} 页 · ${escapeHtml(page.title)} · ${page.status === "ready" ? "已就绪" : "待生成"}</li>`).join("")}</ol></div>`;
  }
  const document = snapshotDocument(snapshot, stage);
  if (!document) return '<p class="snapshot-empty">该阶段的输入已保存，产物尚未生成。</p>';
  return `<article class="document snapshot-document">${renderMarkdown(document.markdown_body)}</article>`;
}

function openSnapshotDialog(stage) {
  if (!state.project) return;
  const item = stageSnapshotMap().get(stage);
  if (!item) { toast("该阶段还没有可回看的任务快照。", true); return; }
  const label = STATE_LABELS[stage] || STAGES.find((entry) => entry.id === stage)?.label || stage;
  const blocked = Boolean(state.project.active_job);
  const dialog = document.createElement("dialog");
  dialog.className = "dialog snapshot-dialog";
  dialog.setAttribute("aria-labelledby", "snapshot-dialog-title");
  dialog.innerHTML = `<div class="dialog__head"><div><p class="eyebrow">Task snapshot</p><h2 id="snapshot-dialog-title">${escapeHtml(label)} · 历史快照</h2><p>回看不会改变当前工程进度。</p></div><button class="icon-btn" type="button" data-dialog-close aria-label="关闭"><svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div class="dialog__body snapshot-dialog__body"><div class="snapshot-meta"><span class="badge badge--info">只读快照</span><span>第 ${item.sequence} 个检查点 · ${escapeHtml(formatDate(item.updated_at))}</span></div>${snapshotContent(stage, item)}${blocked ? '<div class="callout callout--warning">后台任务运行期间不能从快照创建分支。</div>' : ""}<div class="field-error" id="snapshot-branch-error" role="alert"></div></div><div class="dialog__foot snapshot-dialog__foot"><small>新分支会回到该阶段的输入边界并重新执行；原分支保持不变。</small><button class="btn btn--secondary" type="button" data-dialog-close>关闭</button><button class="btn btn--primary" type="button" data-snapshot-branch ${blocked ? "disabled" : ""}>重跑此阶段并创建分支</button></div>`;
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  dialog.querySelector("[data-snapshot-branch]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const errorNode = dialog.querySelector("#snapshot-branch-error");
    button.disabled = true;
    button.textContent = "正在创建分支…";
    try {
      const name = automaticBranchName(stage);
      state.project = await api.createBranch(state.project.project_id, {
        name,
        checkpoint_id: item.checkpoint_id,
        mode: "rerun_stage",
        stage,
      });
      await syncFullDeckSessionForCurrentProject();
      state.branches = await api.branches(state.project.project_id);
      state.focusStage = null;
      dialog.close();
      await loadProjects();
      await render();
      toast(`已从${label}快照创建并切换到分支 ${name}。`);
      const operation = {
        intake: "start_clarification",
        intake_clarify: "start_clarification",
        narrative_structure: "generate_narrative",
        slide_outline: "generate_outline",
        ppt_sample: "generate_sample",
      }[stage];
      if (operation) await runJob(operation);
    } catch (error) {
      errorNode.textContent = error.message;
      button.disabled = false;
      button.textContent = "重跑此阶段并创建分支";
    }
  });
  dialog.showModal();
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
  dialog.innerHTML = `<div class="dialog__head"><div><p class="eyebrow">Version branches</p><h2 id="branch-dialog-title">查看与切换分支</h2></div><button class="icon-btn" type="button" data-dialog-close aria-label="关闭"><svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div class="dialog__body"><p class="hint">在这里查看和切换已有分支；点击创作进度卡中的阶段快照可创建分支。</p>${blocked ? '<div class="callout callout--warning">后台任务运行期间不能切换分支。</div>' : ""}<div class="branch-list">${items}</div></div><div class="dialog__foot"><button class="btn btn--secondary" type="button" data-dialog-close>关闭</button></div>`;
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  dialog.querySelectorAll("[data-branch-switch]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "正在切换…";
    try {
      state.project = await api.switchBranch(state.project.project_id, button.dataset.branchSwitch);
      await syncFullDeckSessionForCurrentProject();
      state.branches = await api.branches(state.project.project_id);
      state.focusStage = null;
      dialog.close();
      await loadProjects();
      await render();
      toast(`已切换到分支 ${state.project.branch}。`);
    } catch (error) { button.disabled = false; button.textContent = "切换到此分支"; toast(error.message, true); }
  }));
  dialog.showModal();
}

async function setView(view) {
  if (statusSearchTimer) {
    window.clearTimeout(statusSearchTimer);
    statusSearchTimer = null;
  }
  if (statusPollTimer) {
    window.clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
  statusPollErrorShown = false;
  if (view !== "workspace") fullDeckWorkspace.stopPolling();
  state.view = view;
  await render();
  document.querySelector("#main").focus();
  if (view === "workspace") fullDeckWorkspace.startPolling();
  if (view === "status") {
    statusPollTimer = window.setInterval(() => {
      if (state.view !== "status" || document.hidden || document.activeElement?.id === "status-search" || statusPollInFlight) return;
      statusPollInFlight = true;
      void refreshStatusView({ skipUnchanged: true }).finally(() => { statusPollInFlight = false; });
    }, 2000);
  }
}

function bindChrome() {
  window.addEventListener("message", (event) => fullDeckWorkspace.handleMessage(event));
  document.addEventListener("visibilitychange", () => fullDeckWorkspace.visibilityChanged());
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
  fullDeckWorkspace = createFullDeckWorkspaceController({
    api,
    root: content,
    getProject: () => state.project,
    getSession: () => state.fullDeckSession,
    setSession: (session) => { state.fullDeckSession = session; },
    getView: () => state.view,
    setFocusStage: (stage) => { state.focusStage = stage; },
    render,
    refreshProject: refreshCurrent,
    notify: toast,
    errorMessage: userErrorMessage,
    announce: announceWorkspace,
    escapeHtml,
  });
  bindChrome();
  await loadProjects();
  await render();
}

void boot();
