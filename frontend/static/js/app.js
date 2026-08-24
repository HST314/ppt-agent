import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";

const icons = {
  file: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 13h6M9 17h5"/></svg>',
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.3 4.2L17 9l-3.7 1.8L12 15l-1.3-4.2L7 9l3.7-1.8zM18.5 15l.7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7z"/></svg>',
};

const state = { projects: [], project: null, view: "workspace", runtime: null, busy: false, focusStage: null };
const content = document.querySelector("#content");
const projectList = document.querySelector("#project-list");
const dialog = document.querySelector("#project-dialog");
const editorDialog = document.querySelector("#editor-dialog");
const editor = document.querySelector("#editor");
let editorContext = null;

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " is-error" : ""}`;
  node.textContent = message;
  document.querySelector("#toasts").append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll("button[data-action]").forEach((button) => { button.disabled = busy; });
}

function currentDocument(type) {
  const history = state.project?.documents?.[type] || [];
  return history.at(-1) || null;
}

function activeStage() {
  const value = state.project?.state;
  return { intake: 0, intake_clarify: 1, narrative_structure: 2, slide_outline: 3 }[value] ?? 0;
}

function stageRail() {
  const names = ["任务卡", "澄清问题", "叙事结构", "逐页大纲", "PPT 样品", "PPT 全稿", "确认验收"];
  const active = activeStage();
  const outlineApproved = currentDocument("slide_outline")?.status === "approved";
  return `<aside class="panel stage-panel"><h2>制作流程</h2><ol class="stage-list">${names.map((name, index) => {
    const done = index < active || (index === 3 && outlineApproved);
    const current = index === active && !outlineApproved;
    const future = index >= 4;
    const status = future ? "后续版本" : done ? "已完成" : current ? "当前阶段" : "等待中";
    const selectable = index === 2 && currentDocument("narrative_structure") || index === 3 && currentDocument("slide_outline");
    return `<li class="stage${done ? " is-done" : ""}${current ? " is-current" : ""}${future ? " is-future" : ""}"${selectable ? ` data-stage-view="${index === 2 ? "narrative_structure" : "slide_outline"}" tabindex="0" role="button" aria-label="查看${name}"` : ""}><span class="stage__number">${done ? "✓" : index + 1}</span><span class="stage__copy"><strong>${name}</strong><small>${status}</small></span></li>`;
  }).join("")}</ol></aside>`;
}

function projectHeader() {
  const p = state.project;
  return `<div class="page-head"><div><p class="eyebrow">${escapeHtml(p.state.replaceAll("_", " "))}</p><h1>${escapeHtml(p.title)}</h1><p class="lede">从任务输入到逐页大纲，每个关键文档都由你确认后再继续。</p></div><span class="badge badge--info">${escapeHtml(p.phase)}</span></div>`;
}

function taskSummary() {
  const task = state.project.task_card;
  const values = [
    ["演示目标", task.objective], ["主要听众", task.audience || "待澄清"],
    ["演示场合", task.occasion || "待澄清"], ["目标页数", task.target_slide_count || "待澄清"],
  ];
  return `<div class="task-summary">${values.map(([label, value]) => `<div class="summary-item"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
}

function intakeView() {
  return `<section class="panel"><div class="panel__head"><div><p class="eyebrow">任务卡</p><h2>任务信息已保存</h2></div><span class="badge badge--success">可恢复</span></div><div class="panel__body">${taskSummary()}<div class="empty-state"><span class="empty-state__icon">${icons.spark}</span><h2>从关键问题开始</h2><p>Agent 会先识别影响叙事方向的缺失信息，再进入内容设计。</p><button class="btn btn--primary" data-action="start_clarification">生成澄清问题</button></div></div></section>`;
}

function clarificationView() {
  const card = state.project.question_card;
  if (!card) return loadingPanel("正在准备澄清问题…");
  return `<section class="panel"><div class="panel__head"><div><p class="eyebrow">澄清问题</p><h2>补齐影响叙事的关键信息</h2><p class="lede">只问必要问题；推荐项已标注，你也可以填写自己的答案。</p></div><span class="badge badge--info">${card.questions.length} 个问题</span></div><form class="panel__body" id="clarification-form"><div class="question-list">${card.questions.map((question, index) => `<article class="question-card"><fieldset><legend>${index + 1}. ${escapeHtml(question.prompt)}</legend><p class="question-impact">${escapeHtml(question.impact)}</p><div class="options">${question.options.map((option) => `<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="${escapeHtml(option.value)}" ${option.recommended ? "checked" : ""}><span>${escapeHtml(option.label)}</span>${option.recommended ? "<small>推荐</small>" : ""}</label>`).join("")}<label class="option"><input type="radio" name="${escapeHtml(question.question_id)}" value="__custom__"><span>自定义答案</span></label><input class="input" name="custom_${escapeHtml(question.question_id)}" placeholder="输入你的答案"></div></fieldset></article>`).join("")}</div><div class="sticky-actions"><p>答案会作为叙事结构的事实输入。</p><button class="btn btn--primary" type="submit">提交答案</button></div></form></section>`;
}

function readyDocumentView(type) {
  const narrative = type === "narrative_structure";
  return `<section class="panel"><div class="panel__head"><div><p class="eyebrow">${narrative ? "叙事结构" : "逐页大纲"}</p><h2>${narrative ? "建立完整的叙事主线" : "把叙事落到每一页"}</h2></div><span class="badge badge--warning">待生成</span></div><div class="empty-state"><span class="empty-state__icon">${icons.spark}</span><h2>${narrative ? "准备生成叙事结构" : "叙事结构已确认"}</h2><p>${narrative ? "Agent 会选择适合任务的叙事方法，并输出可直接编辑的 Markdown 文档。" : "Agent 将基于已确认叙事，规划逐页目的、核心信息和视觉方向。"}</p><button class="btn btn--primary" data-action="${narrative ? "generate_narrative" : "generate_outline"}">${narrative ? "生成叙事结构" : "生成逐页大纲"}</button></div></section>`;
}

function documentView(type) {
  const document = currentDocument(type);
  if (!document) return readyDocumentView(type);
  const narrative = type === "narrative_structure";
  const approved = document.status === "approved";
  const complete = !narrative && approved;
  return `<section class="panel"><div class="panel__head"><div><p class="eyebrow">${narrative ? "叙事结构" : "逐页大纲"}</p><h2>${narrative ? "故事如何推进" : "每一页讲什么"}</h2></div><div class="document-status"><span class="badge ${approved ? "badge--success" : "badge--warning"}">${approved ? "已确认" : "待确认"}</span><span class="badge badge--info">修订 ${document.revision}</span></div></div><div class="panel__body">${complete ? '<div class="callout"><strong>一期制作完成。</strong> 逐页大纲已确认，可作为后续 PPT 样品制作的输入。</div>' : ""}<article class="document">${renderMarkdown(document.markdown_body)}</article>${approved ? `<div class="sticky-actions"><p>编辑会创建新修订，并使该阶段确认${narrative ? "及下游大纲" : ""}失效。</p><div class="button-row"><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑此修订</button>${narrative ? '<button class="btn btn--primary" data-action="continue_outline">查看逐页大纲</button>' : ""}</div></div>` : `<div class="sticky-actions"><p>保存编辑会创建新修订；重新生成会保留当前版本历史。</p><div class="button-row"><button class="btn btn--secondary" data-action="regenerate_document" data-type="${type}">重新生成</button><button class="btn btn--secondary" data-action="edit_document" data-type="${type}">编辑</button><button class="btn btn--primary" data-action="approve_document" data-type="${type}">确认并继续</button></div></div>`}</div></section>`;
}

function loadingPanel(message) {
  return `<section class="panel"><div class="empty-state"><span class="empty-state__icon"><span class="spinner" style="color:var(--primary);border-color:#d9c8f4;border-top-color:var(--primary)"></span></span><h2>${escapeHtml(message)}</h2><p>任务在后台执行，刷新页面也不会丢失进度。</p></div></section>`;
}

function workspace() {
  if (!state.project) return welcome();
  let stage;
  if (state.focusStage === "narrative_structure" && currentDocument("narrative_structure")) stage = documentView("narrative_structure");
  else if (state.focusStage === "slide_outline" && currentDocument("slide_outline")) stage = documentView("slide_outline");
  else if (state.project.active_job) stage = loadingPanel(jobLabel(state.project.active_job.operation));
  else if (state.project.state === "intake") stage = intakeView();
  else if (state.project.state === "intake_clarify") stage = clarificationView();
  else if (state.project.state === "narrative_structure") stage = state.project.phase === "ready_to_generate" ? readyDocumentView("narrative_structure") : documentView("narrative_structure");
  else stage = state.project.phase === "ready_to_generate" ? readyDocumentView("slide_outline") : documentView("slide_outline");
  return `${projectHeader()}<div class="workspace-grid">${stageRail()}<div>${stage}</div></div>`;
}

function welcome() {
  return `<div class="page-head"><div><p class="eyebrow">Presentation workspace</p><h1>从想法到逐页大纲</h1><p class="lede">PPT Agent 把关键决策留给你，把叙事组织交给 Agent。</p></div></div><section class="panel empty-state"><span class="empty-state__icon">${icons.file}</span><h2>创建第一个 PPT 工程</h2><p>提交任务卡后，依次完成澄清问题、叙事结构与逐页大纲。每一步都可以编辑、恢复和确认。</p><button class="btn btn--primary" data-action="new_project">新建工程</button></section>`;
}

function jobLabel(operation) {
  return ({ start_clarification: "正在生成澄清问题…", generate_narrative: "正在生成叙事结构…", generate_outline: "正在生成逐页大纲…", regenerate_narrative: "正在重新生成叙事结构…", regenerate_outline: "正在重新生成逐页大纲…" })[operation] || "任务执行中…";
}

async function statusView() {
  if (!state.project) { content.innerHTML = welcome(); return; }
  const [events, branches] = await Promise.all([api.timeline(state.project.project_id), api.branches(state.project.project_id)]);
  const job = state.project.active_job;
  content.innerHTML = `${projectHeader()}<div class="metric-grid"><div class="metric"><span>当前阶段</span><strong>${escapeHtml(state.project.state)}</strong></div><div class="metric"><span>当前分支</span><strong>${escapeHtml(branches.current)}</strong></div><div class="metric"><span>Checkpoint</span><strong>${branches.checkpoints.length}</strong></div></div><section class="panel" style="margin-top:20px"><div class="panel__head"><div><p class="eyebrow">运行状态</p><h2>任务与恢复点</h2></div>${job ? '<span class="badge badge--info">运行中</span>' : '<span class="badge badge--success">已同步</span>'}</div><div class="panel__body">${job ? `<div class="callout"><strong>${escapeHtml(jobLabel(job.operation))}</strong><div class="code">${escapeHtml(job.job_id)}</div></div>` : '<p class="lede">当前没有正在运行的后台任务。</p>'}<h3 style="margin-top:28px">事件时间线</h3><ol class="timeline">${events.slice().reverse().map((event) => `<li><time>${escapeHtml(new Date(event.at).toLocaleString("zh-CN"))}</time><div><strong>${escapeHtml(event.event)}</strong><div class="code">${escapeHtml(event.checkpoint_id)}</div></div></li>`).join("") || "<li>暂无事件</li>"}</ol></div></section>`;
}

async function settingsView() {
  state.runtime ||= await api.runtime();
  const ctx = state.runtime;
  content.innerHTML = `<div class="page-head"><div><p class="eyebrow">只读配置</p><h1>设置</h1><p class="lede">运行参数由上层 Harness 注入，项目内不可修改 Provider、模型或密钥。</p></div><span class="badge badge--success">已锁定</span></div><div class="settings-grid">${ctx.model_bindings.map((binding) => `<div class="metric"><span>${escapeHtml(binding.state)}</span><strong>${escapeHtml(binding.model)}</strong><small>${escapeHtml(binding.provider)}</small></div>`).join("")}</div><section class="panel" style="margin-top:20px"><div class="panel__head"><div><p class="eyebrow">Agent 权限</p><h2>Skill 与工具边界</h2></div><span class="badge badge--info">只读</span></div><div class="panel__body"><div class="callout"><strong>文件权限：</strong> ${escapeHtml(ctx.read_permission)}</div><div class="task-summary"><div class="summary-item"><span>最大工具轮次</span><strong>${ctx.policy.max_tool_rounds}</strong></div><div class="summary-item"><span>单次读取上限</span><strong>${ctx.policy.max_read_chars_per_call} 字符</strong></div><div class="summary-item"><span>运行配置哈希</span><strong class="code">${escapeHtml(ctx.runtime_hash)}</strong></div><div class="summary-item"><span>模型配置哈希</span><strong class="code">${escapeHtml(ctx.model_hash)}</strong></div></div><h3 style="margin-top:26px">可用 Skills</h3><div class="question-list">${ctx.skills.map((skill) => `<div class="question-card"><strong>${escapeHtml(skill.name)}</strong><p class="question-impact">${escapeHtml(skill.description)}</p><code class="code">${escapeHtml(skill.path)}</code></div>`).join("")}</div></div></section>`;
}

function render() {
  document.querySelectorAll(".topnav__tab").forEach((tab) => {
    const active = tab.dataset.view === state.view;
    tab.classList.toggle("is-active", active);
    tab.toggleAttribute("aria-current", active);
  });
  if (state.view === "workspace") content.innerHTML = workspace();
  else if (state.view === "status") statusView().catch(handleError);
  else settingsView().catch(handleError);
}

function renderProjectList() {
  projectList.innerHTML = state.projects.length ? state.projects.map((project) => `<button class="project-item${state.project?.project_id === project.project_id ? " is-active" : ""}" type="button" data-project="${escapeHtml(project.project_id)}"><span class="project-item__icon">${icons.file}</span><span class="project-item__label"><strong>${escapeHtml(project.title)}</strong><small>${escapeHtml(project.phase)}</small></span></button>`).join("") : '<div class="sidebar-empty">还没有工程</div>';
}

function syncHeader() {
  const node = document.querySelector("#project-context");
  node.hidden = !state.project;
  if (state.project) {
    document.querySelector("#project-name").textContent = state.project.title;
    document.querySelector("#branch-badge").textContent = `分支 ${state.project.branch}`;
  }
}

async function loadProjects(preferred) {
  state.projects = await api.projects();
  const selected = preferred || state.project?.project_id || state.projects[0]?.project_id;
  state.project = selected ? await api.project(selected) : null;
  state.focusStage = null;
  renderProjectList(); syncHeader(); render();
  if (state.project?.active_job) pollJob(state.project.active_job.job_id);
}

async function runJob(operation) {
  if (!state.project || state.busy) return;
  setBusy(true);
  try {
    const job = await api.startJob(state.project.project_id, { operation, checkpoint_id: state.project.checkpoint_id });
    toast("后台任务已启动");
    state.project.active_job = job;
    render();
    await pollJob(job.job_id);
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
}

async function pollJob(jobId) {
  for (;;) {
    const job = await api.job(jobId);
    if (job.status === "succeeded") { await loadProjects(job.project_id); toast("生成完成"); return; }
    if (job.status === "failed") { await loadProjects(job.project_id); throw new Error(job.error?.message || "生成失败"); }
    await new Promise((resolve) => window.setTimeout(resolve, 650));
  }
}

function openEditor(type) {
  const revision = currentDocument(type);
  if (!revision) return;
  editorContext = { type, revisionHash: revision.revision_hash };
  const key = draftKey(type, revision.revision_hash);
  const draft = localStorage.getItem(key);
  editor.value = draft ?? revision.markdown_body;
  document.querySelector("#editor-title").textContent = type === "narrative_structure" ? "编辑叙事结构" : "编辑逐页大纲";
  document.querySelector("#draft-state").textContent = draft ? "已恢复浏览器草稿" : "草稿自动保存在此浏览器";
  editorDialog.showModal();
  editor.focus();
}

const draftKey = (type, hash) => `ppt-agent:draft:${state.project?.project_id}:${type}:${hash}`;

async function saveEditor() {
  if (!editorContext || !editor.value.trim()) return;
  const button = document.querySelector("#editor-save");
  button.disabled = true;
  try {
    state.project = await api.revise(state.project.project_id, editorContext.type, { checkpoint_id: state.project.checkpoint_id, markdown_body: editor.value });
    localStorage.removeItem(draftKey(editorContext.type, editorContext.revisionHash));
    editorDialog.close();
    renderProjectList(); syncHeader(); render(); toast("新修订已保存，原确认已失效");
  } catch (error) { handleError(error); }
  finally { button.disabled = false; }
}

async function approve(type) {
  const doc = currentDocument(type);
  if (!doc) return;
  try {
    state.project = await api.approve(state.project.project_id, type, { checkpoint_id: state.project.checkpoint_id, revision_hash: doc.revision_hash });
    renderProjectList(); syncHeader(); render(); toast(type === "slide_outline" ? "逐页大纲已确认，一期完成" : "叙事结构已确认");
  } catch (error) { handleError(error); }
}

function handleError(error) {
  console.error(error);
  toast(error.message || "操作失败", true);
}

document.querySelectorAll(".topnav__tab").forEach((tab) => tab.addEventListener("click", () => { state.view = tab.dataset.view; render(); document.querySelector("#main").focus(); }));
document.querySelector("#new-button").addEventListener("click", () => dialog.showModal());
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
document.querySelector("#refresh-button").addEventListener("click", () => loadProjects().catch(handleError));
projectList.addEventListener("click", (event) => { const button = event.target.closest("[data-project]"); if (button) loadProjects(button.dataset.project).catch(handleError); });

content.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "new_project") dialog.showModal();
  else if (["start_clarification", "generate_narrative", "generate_outline"].includes(action)) runJob(action);
  else if (action === "edit_document") openEditor(button.dataset.type);
  else if (action === "regenerate_document") runJob(button.dataset.type === "narrative_structure" ? "regenerate_narrative" : "regenerate_outline");
  else if (action === "approve_document") approve(button.dataset.type);
  else if (action === "continue_outline") { state.focusStage = "slide_outline"; render(); }
});

content.addEventListener("click", (event) => {
  const stage = event.target.closest("[data-stage-view]");
  if (stage) { state.focusStage = stage.dataset.stageView; render(); }
});
content.addEventListener("keydown", (event) => {
  const stage = event.target.closest("[data-stage-view]");
  if (stage && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); state.focusStage = stage.dataset.stageView; render(); }
});

content.addEventListener("submit", async (event) => {
  if (event.target.id !== "clarification-form") return;
  event.preventDefault();
  const form = new FormData(event.target);
  const card = state.project.question_card;
  const answers = {};
  for (const question of card.questions) {
    const selected = form.get(question.question_id);
    const custom = String(form.get(`custom_${question.question_id}`) || "").trim();
    answers[question.question_id] = selected === "__custom__" ? custom : String(selected || custom);
    if (!answers[question.question_id]) { toast("请回答全部问题", true); return; }
  }
  try {
    state.project = await api.answer(state.project.project_id, { checkpoint_id: state.project.checkpoint_id, question_card_id: card.question_card_id, answers });
    renderProjectList(); syncHeader(); render(); toast("答案已保存");
  } catch (error) { handleError(error); }
});

document.querySelector("#project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#create-button");
  const lines = document.querySelector("#constraints").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const duration = document.querySelector("#duration").value;
  const payload = {
    project_id: document.querySelector("#project-id").value.trim(),
    task_card: {
      title: document.querySelector("#task-title").value.trim(),
      objective: document.querySelector("#objective").value.trim(),
      audience: document.querySelector("#audience").value.trim(),
      occasion: document.querySelector("#occasion").value.trim(),
      language: "zh-CN",
      target_slide_count: document.querySelector("#slide-count").value.trim(),
      duration_minutes: duration ? Number(duration) : null,
      known_facts: lines,
      constraints: [], forbidden_items: [], source_refs: [],
    },
  };
  button.disabled = true;
  document.querySelector("#form-error").textContent = "";
  try {
    state.project = await api.create(payload);
    dialog.close(); event.target.reset();
    await loadProjects(payload.project_id);
    toast("工程已创建");
  } catch (error) { document.querySelector("#form-error").textContent = error.message; }
  finally { button.disabled = false; }
});

editor.addEventListener("input", () => {
  if (!editorContext) return;
  localStorage.setItem(draftKey(editorContext.type, editorContext.revisionHash), editor.value);
  document.querySelector("#draft-state").textContent = "草稿已自动保存";
});
document.querySelector("#editor-save").addEventListener("click", saveEditor);
document.querySelector("#editor-cancel").addEventListener("click", () => editorDialog.close());
document.querySelector("#editor-close").addEventListener("click", () => editorDialog.close());

Promise.all([api.health(), loadProjects()]).then(([health]) => {
  document.querySelector("#health-dot").classList.add("is-online");
  document.querySelector("#health-text").textContent = health.status === "ok" ? "服务正常" : "状态未知";
}).catch(handleError);
