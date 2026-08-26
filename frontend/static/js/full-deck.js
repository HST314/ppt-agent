const fallbackEscapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function revisionDate(value) {
  if (!value) return "时间未知";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(new Date(value));
  } catch { return String(value); }
}

function statusMeta(status) {
  return ({
    draft: ["草稿", "badge--warning"],
    pending_approval: ["等待反馈", "badge--warning"],
    approved: ["已确认", "badge--success"],
    stale: ["上游已变化", "badge--warning"],
  })[status] || [status || "状态未知", "badge--info"];
}

const SESSION_STATUS_META = {
  queued: ["等待开始", "badge--info", "queued"],
  running: ["正在生成", "badge--info", "running"],
  pause_requested: ["正在请求暂停", "badge--warning", "pause"],
  paused: ["已暂停", "badge--warning", "pause"],
  failed: ["当前批失败", "badge--danger", "failed"],
  finalizing: ["正在组装正式全稿", "badge--info", "running"],
  completed: ["生成完成", "badge--success", "ready"],
  cancelled: ["已取消", "badge--warning", "cancelled"],
  stale: ["工程基线已变化", "badge--warning", "failed"],
};

const PAGE_STATUS_META = {
  sample_ready: ["样品已就绪", "badge--success", "ready"],
  queued: ["排队中", "badge--info", "queued"],
  generating: ["生成中", "badge--info", "running"],
  ready: ["已就绪", "badge--success", "ready"],
  failed: ["生成失败", "badge--danger", "failed"],
};

const BATCH_STATUS_META = {
  pending: ["排队中", "badge--info", "queued"],
  running: ["生成中", "badge--info", "running"],
  succeeded: ["已完成", "badge--success", "ready"],
  failed: ["失败，可重试", "badge--danger", "failed"],
};

function stateIcon(kind) {
  const path = ({
    ready: '<path d="m5 12 4 4L19 6"/>',
    running: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    failed: '<path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/>',
    cancelled: '<path d="m7 7 10 10M17 7 7 17"/><circle cx="12" cy="12" r="9"/>',
    queued: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  })[kind] || '<circle cx="12" cy="12" r="9"/>';
  return `<svg class="generation-state-icon" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
}

function sourceLabel(sourceType) {
  return ({
    approved_sample: "样品来源",
    generated_segment: "生成页段",
    full_deck_edit: "全稿修改",
    pending: "等待生成",
  })[sourceType] || "来源未知";
}

function pageNumber(page) {
  return page.source_slide_number || page.outline_ref?.source_slide_number || page.position + 1;
}

function slideRange(numbers = []) {
  if (!numbers.length) return "页面未知";
  if (numbers.length === 1) return `第 ${numbers[0]} 页`;
  const contiguous = numbers.every((number, index) => index === 0 || number === numbers[index - 1] + 1);
  return contiguous ? `第 ${numbers[0]}–${numbers.at(-1)} 页` : `第 ${numbers.join("、")} 页`;
}

function elapsedLabel(startedAt, completedAt) {
  if (!startedAt) return "尚未开始";
  if (!completedAt) return "进行中";
  const elapsed = Math.max(0, new Date(completedAt).getTime() - new Date(startedAt).getTime());
  if (!Number.isFinite(elapsed)) return "用时未知";
  const seconds = Math.round(elapsed / 1000);
  if (seconds < 60) return `用时 ${seconds} 秒`;
  return `用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function sessionProgressSummary(session) {
  const progress = session.progress || {};
  const ready = progress.ready_pages || 0;
  const total = progress.total_pages || session.pages?.length || 0;
  const batchIndex = progress.active_batch_index;
  const batchTotal = progress.total_batches || session.batches?.length || 0;
  const range = slideRange(progress.active_slide_numbers || []);
  if (session.status === "running" && batchIndex) return `已完成 ${ready}/${total} 页 · 第 ${batchIndex}/${batchTotal} 批正在生成${range}`;
  if (session.status === "pause_requested" && batchIndex) return `已完成 ${ready}/${total} 页 · 第 ${batchIndex}/${batchTotal} 批完成后暂停`;
  if (session.status === "paused") return `已完成 ${ready}/${total} 页 · 已在批次边界暂停`;
  if (session.status === "failed") return `已完成 ${ready}/${total} 页 · 当前批未完成`;
  if (session.status === "finalizing") return `已完成 ${ready}/${total} 页 · 正在校验并组装正式全稿`;
  if (session.status === "completed") return `已完成 ${ready}/${total} 页 · ${batchTotal}/${batchTotal} 批全部完成`;
  if (session.status === "cancelled") return `已保留 ${ready}/${total} 页 · 会话已取消`;
  if (session.status === "stale") return `已保留 ${ready}/${total} 页 · 工程基线已变化`;
  return `已完成 ${ready}/${total} 页 · 等待第 1/${batchTotal} 批开始`;
}

export function fullDeckDirectiveTarget(session) {
  if (!session) return null;
  const nextIndex = session.progress?.active_batch_index
    ? Number(session.progress.active_batch_index) + 1
    : Math.min(...(session.batches || []).filter((batch) => batch.status !== "succeeded").map((batch) => Number(batch.batch_index)));
  if (!Number.isFinite(nextIndex)) return null;
  return (session.batches || []).find((batch) => Number(batch.batch_index) === nextIndex) || null;
}

function pageCards(pages, session, escapeHtml) {
  const hasSessionPreview = Boolean(session?.preview_url);
  const activeSlideId = session?.activeSlideId || "";
  return pages.map((page) => {
    const sessionStatus = page.generation_status;
    const ready = sessionStatus ? ["sample_ready", "ready"].includes(sessionStatus) : page.status === "ready";
    const [label, badgeClass, icon] = sessionStatus
      ? (PAGE_STATUS_META[sessionStatus] || ["状态未知", "badge--info", "queued"])
      : [ready ? "已就绪" : "待生成", ready ? "badge--success" : "badge--warning", ready ? "ready" : "queued"];
    const cardClass = `full-deck-page-card is-${sessionStatus || (ready ? "ready" : "pending")}`;
    const content = `<span class="full-deck-page-card__number" aria-hidden="true">${pageNumber(page)}</span><span class="full-deck-page-card__content"><span><strong>${escapeHtml(page.title)}</strong><span class="badge ${badgeClass}">${stateIcon(icon)}${escapeHtml(label)}</span></span><small>大纲第 ${pageNumber(page)} 页 · ${escapeHtml(sourceLabel(page.source_type))}</small>${page.error ? `<span class="generation-inline-error">${escapeHtml(page.error.message)}</span>` : ""}</span>`;
    if (ready && hasSessionPreview) {
      const current = activeSlideId === page.slot_id;
      return `<button class="${cardClass} is-navigable${current ? " is-current" : ""}" type="button" data-full-deck-slide="${escapeHtml(page.slot_id)}" aria-current="${current ? "true" : "false"}" aria-label="在预览中查看第 ${pageNumber(page)} 页：${escapeHtml(page.title)}">${content}</button>`;
    }
    return `<article class="${cardClass}">${content}</article>`;
  }).join("");
}

function pagePlanInner(revision, session, options, escapeHtml) {
  const pages = session?.pages || revision.plan?.pages || [];
  const readyCount = pages.filter((page) => ["sample_ready", "ready"].includes(page.generation_status) || page.status === "ready").length;
  const action = options.canGenerate && !session
    ? '<button class="btn btn--primary" type="button" data-action="generate_full_deck">生成完整 HTML-PPT</button>'
    : "";
  const hint = session
    ? "页面按原大纲顺序持续更新；已就绪页面可用键盘聚焦，并在部分预览中直接切页。"
    : "页面顺序来自已确认大纲；样品来源页保留原始内容引用，其余页面按状态等待生成。";
  return `<div class="sample-history__head"><div><h3 id="full-deck-plan-title">完整页面清单</h3><p>${hint}</p></div><span class="badge badge--info">${readyCount}/${pages.length} 页已就绪</span></div><div class="full-deck-page-grid">${pageCards(pages, session, escapeHtml)}</div>${action ? `<div class="full-deck-plan__actions">${action}</div>` : ""}`;
}

function pagePlanBody(revision, session, options, escapeHtml) {
  return `<section class="full-deck-plan" aria-labelledby="full-deck-plan-title" data-session-region="pages">${pagePlanInner(revision, session, options, escapeHtml)}</section>`;
}

function progressInner(session, escapeHtml) {
  const [label, badgeClass, icon] = SESSION_STATUS_META[session.status] || ["状态未知", "badge--info", "queued"];
  const progress = session.progress || {};
  return `<div class="generation-progress__copy"><span class="generation-progress__icon">${stateIcon(icon)}</span><div><span class="eyebrow">Batched generation</span><h3>${escapeHtml(sessionProgressSummary(session))}</h3><p>每批成功后，页面状态与安全预览会自动增量更新。</p></div></div><div class="generation-progress__meta"><span class="badge ${badgeClass}">${stateIcon(icon)}${escapeHtml(label)}</span><span>第 ${progress.completed_batches || 0}/${progress.total_batches || 0} 批完成</span></div><progress max="${progress.total_pages || 1}" value="${progress.ready_pages || 0}" aria-label="全稿已完成 ${progress.ready_pages || 0} 页，共 ${progress.total_pages || 0} 页"></progress>${session.error ? `<div class="generation-session-error" role="alert">${stateIcon("failed")}<span>${escapeHtml(session.error.message)}</span></div>` : ""}`;
}

function progressBody(session, escapeHtml) {
  return `<section class="generation-progress" data-session-region="progress" role="status" aria-live="polite" aria-atomic="true">${progressInner(session, escapeHtml)}</section>`;
}

function controlsInner(session) {
  const capabilities = new Set(session.capabilities || []);
  const buttons = [];
  if (capabilities.has("pause")) buttons.push('<button class="btn btn--secondary" type="button" data-session-action="pause">暂停生成</button>');
  if (session.status === "pause_requested") buttons.push('<button class="btn btn--secondary" type="button" disabled aria-disabled="true"><span class="spinner" aria-hidden="true"></span>正在请求暂停…</button>');
  if (capabilities.has("resume")) buttons.push('<button class="btn btn--primary" type="button" data-session-action="resume">继续生成</button>');
  if (capabilities.has("retry")) buttons.push('<button class="btn btn--primary" type="button" data-session-action="retry">重试当前批</button>');
  const explanation = ({
    running: "暂停会在当前批安全提交后生效，不会中断正在进行的模型调用。",
    pause_requested: "暂停请求已登记；当前批提交后会停在安全边界。",
    paused: "继续后会从第一个未完成批次开始，已成功批次不会重复生成。",
    failed: "重试只会执行当前失败批次，已就绪页面和预览保持不变。",
    finalizing: "所有页段已保存，正在校验并发布正式全稿。",
    completed: "所有批次与正式全稿均已完成。",
    cancelled: "已成功页段仍保留在会话记录中。",
    stale: "工程基线已变化，此会话不能继续。",
  })[session.status] || "生成会在安全批次边界保存进度。";
  return `<div><h3 id="full-deck-controls-title">生成控制</h3><p>${explanation}</p></div><div class="button-row">${buttons.join("") || '<span class="badge badge--info">当前无需操作</span>'}</div><div class="field-error" id="full-deck-session-control-error" role="alert"></div>`;
}

function controlsBody(session) {
  return `<section class="generation-controls" data-session-region="controls" aria-labelledby="full-deck-controls-title" tabindex="-1">${controlsInner(session)}</section>`;
}

function directiveTargetLabel(session) {
  const target = fullDeckDirectiveTarget(session);
  return target ? `将从第 ${target.batch_index} 批（${slideRange(target.source_slide_numbers)}）开始生效。` : "没有尚未生成的批次可接收补充要求。";
}

function directiveHistoryInner(session, escapeHtml) {
  if (!session.directives?.length) return '<p class="generation-empty">尚未提交下一批指令。</p>';
  return `<ol class="generation-directive-list">${session.directives.map((directive) => `<li><div><strong>从第 ${directive.apply_from_batch_index} 批生效</strong><span>${directive.first_applied_at ? "已应用" : "等待应用"}</span></div><p>${escapeHtml(directive.content)}</p><small>${escapeHtml(revisionDate(directive.created_at))}</small></li>`).join("")}</ol>`;
}

function directivesBody(session, escapeHtml) {
  const canAdd = (session.capabilities || []).includes("add_directive") && Boolean(fullDeckDirectiveTarget(session));
  return `<section class="generation-directives" aria-labelledby="full-deck-directive-title"><div class="sample-history__head"><div><h3 id="full-deck-directive-title">下一批指令</h3><p>补充要求只进入尚未开始的批次，不会改写已完成页面。</p></div><span class="badge badge--info">不可变记录</span></div><form id="full-deck-directive-form"><div class="field"><label for="full-deck-directive">给后续页面的补充要求</label><textarea class="input" id="full-deck-directive" name="directive" maxlength="4000" placeholder="例如：后续数据页减少装饰，突出同比变化。" aria-describedby="full-deck-directive-help" ${canAdd ? "" : "disabled"}></textarea><small id="full-deck-directive-help" data-session-directive-target>${directiveTargetLabel(session)}</small><div class="field-error" id="full-deck-directive-error" role="alert"></div><div class="generation-directive-confirmation" id="full-deck-directive-confirmation" role="status" aria-live="polite"></div></div><div class="generation-directives__action"><button class="btn btn--secondary" type="submit" ${canAdd ? "" : "disabled"}>提交下一批指令</button></div></form><div data-session-region="directive-history">${directiveHistoryInner(session, escapeHtml)}</div></section>`;
}

function batchHistoryInner(session, escapeHtml) {
  const directiveById = new Map((session.directives || []).map((directive) => [directive.directive_id, directive]));
  return `<div class="generation-batch-list">${(session.batches || []).map((batch) => {
    const [label, badgeClass, icon] = BATCH_STATUS_META[batch.status] || ["状态未知", "badge--info", "queued"];
    const directives = (batch.applied_directive_ids || []).map((id) => directiveById.get(id)).filter(Boolean);
    return `<article class="generation-batch-card is-${batch.status}"><div class="generation-batch-card__head"><div><span class="generation-batch-card__index">第 ${batch.batch_index} 批</span><strong>${slideRange(batch.source_slide_numbers)}</strong></div><span class="badge ${badgeClass}">${stateIcon(icon)}${label}</span></div><dl><div><dt>尝试</dt><dd>${batch.attempt_count || 0} 次</dd></div><div><dt>耗时</dt><dd>${elapsedLabel(batch.started_at, batch.completed_at)}</dd></div><div><dt>指令</dt><dd>${directives.length} 条</dd></div></dl>${directives.length ? `<ul class="generation-batch-directives">${directives.map((directive) => `<li>${escapeHtml(directive.content)}</li>`).join("")}</ul>` : '<p class="generation-batch-card__empty">未应用补充指令</p>'}${batch.error ? `<div class="generation-inline-error" role="alert">${escapeHtml(batch.error.message)}</div>` : ""}</article>`;
  }).join("")}</div>`;
}

function batchHistoryBody(session, escapeHtml) {
  return `<section class="generation-batches" aria-labelledby="full-deck-batches-title"><div class="sample-history__head"><div><h3 id="full-deck-batches-title">批次记录</h3><p>查看目标页、尝试次数、耗时、实际应用指令和可恢复错误。</p></div><span class="badge badge--info">${session.batches?.length || 0} 批</span></div><div data-session-region="batches">${batchHistoryInner(session, escapeHtml)}</div></section>`;
}

function attemptsBody(attempts, escapeHtml) {
  if (!attempts.length) return "";
  const cards = attempts.map((attempt, index) => {
    const published = Boolean(attempt.published);
    const range = attempt.segment_range || attempt.target_slide_numbers?.join("、") || "完整页面";
    const operation = ({ revise_full_deck: "按意见修改", regenerate_full_deck: "重新生成", generate_full_deck: "首次生成" })[attempt.operation] || "全稿操作";
    return `<li class="sample-attempt-card"><div class="sample-attempt-card__head"><strong>${escapeHtml(operation)} · 尝试 ${attempt.attempt || index + 1} · ${escapeHtml(range)}</strong><span class="badge ${published ? "badge--success" : "badge--warning"}">${published ? "已发布" : "未发布"}</span></div><p>${escapeHtml(attempt.reason || "等待结果")}</p><small>${attempt.tool_rounds || 0} 个工具轮次 · ${attempt.tool_call_count || 0} 次工具调用 · ${attempt.skill_read_count || 0} 次 Skill 读取 · ${escapeHtml(revisionDate(attempt.completed_at || attempt.started_at))}</small></li>`;
  }).join("");
  return `<section class="sample-attempts" aria-labelledby="full-deck-attempts-title" aria-live="polite"><div class="sample-history__head"><div><h3 id="full-deck-attempts-title">本次全稿操作尝试</h3><p>目标页声明、页面契约和最终组装均通过后，新版本才会进入修订历史。</p></div><span class="badge badge--info">${attempts.length} 次</span></div><ol class="sample-attempt-list">${cards}</ol></section>`;
}

function historyBody(history, selectedHash, options, escapeHtml) {
  if (!history.length) return "";
  const cards = history.map((revision) => {
    const selected = revision.revision_hash === selectedHash;
    const [status, statusClass] = statusMeta(revision.status);
    const changedPages = revision.changed_pages || [];
    const changedCount = changedPages.length || revision.changed_slot_ids?.length || 0;
    const changedLabel = changedPages.length ? ` · 变更第 ${changedPages.map((page) => page.source_slide_number || "补充").join("、")} 页` : changedCount ? ` · 变更 ${changedCount} 页` : "";
    const packageInfo = revision.package;
    const description = revision.feedback || (revision.revision === 1 ? "由已确认样品初始化" : "基于父版本创建的全稿修订");
    const branchAction = selected && options.canBranch ? `<div class="full-deck-revision-card__actions"><button class="btn btn--secondary" type="button" data-action="branch_full_deck_revision" data-revision-hash="${escapeHtml(revision.revision_hash)}">从当前版本创建分支</button></div>` : "";
    return `<article class="full-deck-revision-card${selected ? " is-selected" : ""}"><button class="full-deck-revision-card__selector" type="button" data-full-deck-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}"><span class="sample-revision-card__number">R${revision.revision}</span><span class="sample-revision-card__content"><span class="sample-revision-card__title">修订 ${revision.revision}${selected ? '<span class="badge badge--success">当前</span>' : ""}<span class="badge ${statusClass}">${escapeHtml(status)}</span></span><p>${escapeHtml(description)}</p><small>${escapeHtml(revisionDate(revision.created_at))} · ${revision.page_count || packageInfo?.slide_count || 0} 页${changedLabel}${packageInfo ? ` · ${packageInfo.file_count || 0} 个文件` : ""}</small></span></button>${branchAction}</article>`;
  }).join("");
  return `<section class="full-deck-history" aria-labelledby="full-deck-history-title"><div class="sample-history__head"><div><h3 id="full-deck-history-title">全稿修订历史</h3><p>点击任意版本只移动当前指针，不会创建新修订；下一次操作会从选中版本继续。</p></div><span class="badge badge--info">${history.length} 个版本</span></div><div class="full-deck-revision-list">${cards}</div></section>`;
}

function feedbackBody(revision, options) {
  const packageReady = Boolean(revision.package);
  const regenerate = options.canRegenerate ? `<button class="btn btn--secondary" type="button" data-action="regenerate_full_deck">${packageReady ? "重新生成非样品页" : "重新生成待完成页"}</button>` : "";
  const revise = options.canRevise ? `<button class="btn btn--primary" type="submit">${options.acceptanceMode ? "创建后续全稿修订" : "让 AI 修改"}</button>` : "";
  if (!regenerate && !revise) return "";
  const label = options.acceptanceMode ? "后续修改意见" : "修改意见";
  const help = options.acceptanceMode ? "已确认版本保持只读；提交后会以它为父版本创建不可变子修订，并返回全稿工作区。" : "AI 会从当前选中的全稿版本继续；只有声明的变更页会获得新内容引用，成功后保存为不可变子修订。";
  return `<form class="sample-feedback full-deck-feedback" id="full-deck-feedback-form"><div class="field"><label for="full-deck-feedback">${label}</label><textarea class="input" id="full-deck-feedback" name="feedback" maxlength="4000" required placeholder="例如：第 5 页把核心结论放进标题，并保持其余页面不变。" aria-describedby="full-deck-feedback-help"></textarea><small id="full-deck-feedback-help">${help}</small><div class="field-error" id="full-deck-feedback-error" role="alert"></div></div><div class="sample-feedback__actions">${regenerate}${revise}</div></form>`;
}

function approvalBody(options) {
  if (!options.canApprove) return "";
  return '<div class="sticky-actions full-deck-approval"><p>确认后将锁定当前修订为验收基线，并原子进入最终验收工作区。</p><div><button class="btn btn--primary" type="button" data-action="approve_full_deck">确认全稿并进入验收</button><div class="field-error" id="full-deck-approve-error" role="alert"></div></div></div>';
}

function acceptanceBody(revision, options, escapeHtml) {
  if (!options.acceptanceMode) return "";
  const packageInfo = revision.package || {};
  return `<section class="acceptance-overview" aria-labelledby="acceptance-overview-title"><div><span class="badge badge--success">验收基线已锁定</span><h3 id="acceptance-overview-title">修订 R${revision.revision} 已进入最终验收</h3><p>当前全稿信息与产物保持只读；如需继续调整，请在下方提交新意见创建后续修订。</p></div><dl><div><dt>页面</dt><dd>${packageInfo.slide_count || 0} 页</dd></div><div><dt>修订哈希</dt><dd class="code">${escapeHtml(revision.revision_hash)}</dd></div><div><dt>包哈希</dt><dd class="code">${escapeHtml(packageInfo.package_hash || "—")}</dd></div></dl><div class="button-row"><a class="btn btn--secondary" href="${escapeHtml(options.auditExportUrl || "#")}" download>导出验收审计</a></div></section>`;
}

function previewBody(revision, sample, session, escapeHtml) {
  const packageInfo = revision.package;
  const readyPages = session?.progress?.ready_pages || 0;
  const sessionPreview = session?.preview_url;
  const hasPreview = sessionPreview || packageInfo || sample?.preview_url;
  if (!hasPreview) return "";
  const previewTitle = sessionPreview ? `部分全稿预览，共 ${readyPages} 页` : packageInfo ? `修订 R${revision.revision} · ${packageInfo.title} HTML-PPT 预览` : `修订 R${revision.revision} · 样品来源页 HTML-PPT 预览`;
  const toolbar = session ? `<div><span>${readyPages} 页已就绪</span><strong>${session.status === "completed" ? "全稿生成预览" : "安全部分预览"}</strong></div><span class="badge badge--info">随批次增量更新</span>` : packageInfo ? `<div><span>${packageInfo.slide_count} 页</span><strong>${escapeHtml(packageInfo.title)}</strong></div><div class="button-row"><span class="badge badge--info">HTML-PPT 全稿 · ${packageInfo.files?.length || 0} 文件</span><a class="btn btn--secondary" href="${escapeHtml(revision.export_url)}" download>导出 ZIP</a></div>` : `<div><span>${revision.plan?.pages?.length || 0} 页清单</span><strong>样品来源页预览</strong></div><span class="badge badge--warning">完整包待生成</span>`;
  const hint = session ? "预览仅加载当前会话已验证、已登记的不可变页面包；点击下方已就绪页面可直接切页。" : packageInfo ? `翻页与总览由完整 HTML-PPT 自身提供。后台完整项目：${escapeHtml(revision.retained_project_path || "artifacts/full_decks/")}` : "这里安全加载已确认样品包；下方页面清单显示它在全稿中的对应位置。";
  return `<div class="sample-preview full-deck-preview"><div class="sample-preview__toolbar" data-session-region="preview-toolbar">${toolbar}</div><div class="sample-canvas"><iframe id="full-deck-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="${escapeHtml(previewTitle)}" data-preview-package-id="${escapeHtml(session?.latest_preview_package_id || "")}"></iframe></div><p class="sample-preview__hint">${hint}</p></div>`;
}

export function fullDeckBody(revision, escapeHtml = fallbackEscapeHtml, options = {}) {
  const packageInfo = revision.package;
  const session = options.generationSession || null;
  const [status, statusClass] = statusMeta(revision.status);
  const preview = previewBody(revision, options.sample, session, escapeHtml);
  const callout = revision.status === "approved" ? '<div class="callout"><strong>当前全稿已确认。</strong> 历史版本仍可查看、切换和创建工程分支。</div>' : "";
  const generation = session ? `<div id="full-deck-generation-workspace" data-session-id="${escapeHtml(session.session_id)}" data-session-version="${session.session_version}" data-session-status="${escapeHtml(session.status)}">${progressBody(session, escapeHtml)}${preview}${pagePlanBody(revision, session, options, escapeHtml)}${controlsBody(session)}${directivesBody(session, escapeHtml)}${batchHistoryBody(session, escapeHtml)}</div>` : `${preview}${pagePlanBody(revision, null, options, escapeHtml)}`;
  const acceptance = acceptanceBody(revision, options, escapeHtml);
  const approval = approvalBody(options);
  const feedback = feedbackBody(revision, options);
  const attempts = attemptsBody(options.attempts || [], escapeHtml);
  const history = historyBody(options.history || [], options.selectedHash || revision.revision_hash, options, escapeHtml);
  const statusMarkup = session ? SESSION_STATUS_META[session.status] || ["生成会话", "badge--info"] : [status, statusClass];
  return {
    body: `${callout}${acceptance}${generation}${approval}${feedback}${attempts}${history}`,
    status: `<div class="document-status"><span class="badge ${statusMarkup[1]}"${session ? ' data-full-deck-session-badge' : ""}>${escapeHtml(statusMarkup[0])}</span><span class="badge badge--info">修订 ${revision.revision}</span></div>`,
  };
}

export function createFullDeckNavigateMessage(slideId) {
  return { type: "ppt-agent:navigate", slide_id: String(slideId) };
}

export function isFullDeckSlideChangeMessage(value) {
  return Boolean(value && value.type === "ppt-agent:slidechange" && typeof value.slide_id === "string");
}

export function setFullDeckActiveSlide(root, slideId) {
  const workspace = root.querySelector("#full-deck-generation-workspace");
  if (!workspace) return false;
  const buttons = [...workspace.querySelectorAll("[data-full-deck-slide]")];
  if (!buttons.some((button) => button.dataset.fullDeckSlide === slideId)) return false;
  workspace.dataset.activeSlideId = slideId;
  buttons.forEach((button) => {
    const current = button.dataset.fullDeckSlide === slideId;
    button.classList.toggle("is-current", current);
    button.setAttribute("aria-current", current ? "true" : "false");
  });
  return true;
}

function focusDescriptor(workspace) {
  const focused = workspace.ownerDocument?.activeElement;
  if (!focused || !workspace.contains(focused)) return null;
  if (focused.id) return { type: "id", value: focused.id };
  if (focused.dataset?.sessionAction) return { type: "action", value: focused.dataset.sessionAction };
  if (focused.dataset?.fullDeckSlide) return { type: "slide", value: focused.dataset.fullDeckSlide };
  return null;
}

function restoreFocus(workspace, descriptor) {
  if (!descriptor) return;
  let target = null;
  if (descriptor.type === "id") target = workspace.ownerDocument?.getElementById(descriptor.value);
  if (descriptor.type === "action") target = [...workspace.querySelectorAll("[data-session-action]")].find((node) => node.dataset.sessionAction === descriptor.value);
  if (descriptor.type === "slide") target = [...workspace.querySelectorAll("[data-full-deck-slide]")].find((node) => node.dataset.fullDeckSlide === descriptor.value);
  if (!target && descriptor.type === "action") target = workspace.querySelector('[data-session-region="controls"]');
  target?.focus?.({ preventScroll: true });
}

function patchRegion(workspace, name, markup) {
  const region = workspace.querySelector(`[data-session-region="${name}"]`);
  if (region && region.innerHTML !== markup) region.innerHTML = markup;
}

export function updateFullDeckGenerationWorkspace(root, revision, session, escapeHtml = fallbackEscapeHtml) {
  const workspace = root.querySelector("#full-deck-generation-workspace");
  if (!workspace || workspace.dataset.sessionId !== session.session_id) return { requiresRender: true, previewReloaded: false };
  const doc = workspace.ownerDocument;
  const view = doc?.defaultView;
  const viewport = view ? { x: view.scrollX, y: view.scrollY } : null;
  const focused = focusDescriptor(workspace);
  const activeSlideId = workspace.dataset.activeSlideId || "";
  const renderSession = { ...session, activeSlideId };

  patchRegion(workspace, "progress", progressInner(renderSession, escapeHtml));
  patchRegion(workspace, "pages", pagePlanInner(revision, renderSession, {}, escapeHtml));
  patchRegion(workspace, "controls", controlsInner(renderSession));
  patchRegion(workspace, "directive-history", directiveHistoryInner(renderSession, escapeHtml));
  patchRegion(workspace, "batches", batchHistoryInner(renderSession, escapeHtml));

  const target = workspace.querySelector("[data-session-directive-target]");
  if (target) target.textContent = directiveTargetLabel(renderSession);
  const form = workspace.querySelector("#full-deck-directive-form");
  const canAdd = (session.capabilities || []).includes("add_directive") && Boolean(fullDeckDirectiveTarget(session));
  const textarea = form?.elements?.directive;
  const submit = form?.querySelector('button[type="submit"]');
  if (textarea) textarea.disabled = !canAdd;
  if (submit && submit.dataset.submitting !== "true") submit.disabled = !canAdd;

  const frame = workspace.querySelector("#full-deck-preview-frame");
  const packageId = session.latest_preview_package_id || "";
  const previewReloaded = Boolean(frame && packageId && frame.dataset.previewPackageId !== packageId && session.preview_url);
  if (frame) {
    frame.title = `部分全稿预览，共 ${session.progress?.ready_pages || 0} 页`;
    if (previewReloaded) {
      frame.dataset.previewPackageId = packageId;
      frame.src = session.preview_url;
      if (activeSlideId) frame.addEventListener("load", () => frame.contentWindow?.postMessage(createFullDeckNavigateMessage(activeSlideId), "*"), { once: true });
    }
  }
  const toolbar = session ? `<div><span>${session.progress?.ready_pages || 0} 页已就绪</span><strong>${session.status === "completed" ? "全稿生成预览" : "安全部分预览"}</strong></div><span class="badge badge--info">随批次增量更新</span>` : "";
  patchRegion(workspace, "preview-toolbar", toolbar);
  const sessionBadge = root.querySelector("[data-full-deck-session-badge]");
  const [sessionLabel, sessionBadgeClass] = SESSION_STATUS_META[session.status] || ["生成会话", "badge--info"];
  if (sessionBadge) {
    sessionBadge.className = `badge ${sessionBadgeClass}`;
    sessionBadge.textContent = sessionLabel;
  }
  workspace.dataset.sessionVersion = String(session.session_version);
  workspace.dataset.sessionStatus = session.status;
  restoreFocus(workspace, focused);
  if (viewport) view.scrollTo(viewport.x, viewport.y);
  return { requiresRender: false, previewReloaded };
}

export function hydrateFullDeckFrame(root, revision, sample, session = null) {
  const frame = root.querySelector("#full-deck-preview-frame");
  if (!frame) return;
  if (session?.preview_url) {
    frame.src = session.preview_url;
    frame.dataset.previewPackageId = session.latest_preview_package_id || "";
    return;
  }
  if (revision?.preview_url) {
    frame.src = revision.preview_url;
    return;
  }
  if (sample?.preview_url) frame.src = sample.preview_url;
}
