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

function sourceLabel(sourceType) {
  return ({
    approved_sample: "样品来源",
    generated_segment: "生成页段",
    full_deck_edit: "全稿修改",
    pending: "等待生成",
  })[sourceType] || "来源未知";
}

function pageNumber(page) {
  return page.outline_ref?.source_slide_number || page.position + 1;
}

function pagePlanBody(revision, options, escapeHtml) {
  const pages = revision.plan?.pages || [];
  const readyCount = pages.filter((page) => page.status === "ready").length;
  const cards = pages.map((page) => {
    const ready = page.status === "ready";
    return `<article class="full-deck-page-card ${ready ? "is-ready" : "is-pending"}"><div class="full-deck-page-card__number" aria-hidden="true">${pageNumber(page)}</div><div class="full-deck-page-card__content"><div><strong>${escapeHtml(page.title)}</strong><span class="badge ${ready ? "badge--success" : "badge--warning"}">${ready ? "已就绪" : "待生成"}</span></div><small>大纲第 ${pageNumber(page)} 页 · ${escapeHtml(sourceLabel(page.source_type))}</small></div></article>`;
  }).join("");
  const action = options.canGenerate
    ? '<button class="btn btn--primary" type="button" data-action="generate_full_deck">生成完整 HTML-PPT</button>'
    : "";
  return `<section class="full-deck-plan" aria-labelledby="full-deck-plan-title"><div class="sample-history__head"><div><h3 id="full-deck-plan-title">完整页面清单</h3><p>页面顺序来自已确认大纲；样品来源页保留原始内容引用，其余页面按状态等待生成。</p></div><span class="badge badge--info">${readyCount}/${pages.length} 页已就绪</span></div><div class="full-deck-page-grid">${cards}</div>${action ? `<div class="full-deck-plan__actions">${action}</div>` : ""}</section>`;
}

function attemptsBody(attempts, escapeHtml) {
  if (!attempts.length) return "";
  const cards = attempts.map((attempt, index) => {
    const published = Boolean(attempt.published);
    const range = attempt.segment_range || attempt.target_slide_numbers?.join("、") || "完整页面";
    const operation = ({
      revise_full_deck: "按意见修改",
      regenerate_full_deck: "重新生成",
      generate_full_deck: "首次生成",
    })[attempt.operation] || "全稿操作";
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
    const changedLabel = changedPages.length
      ? ` · 变更第 ${changedPages.map((page) => page.source_slide_number || "补充").join("、")} 页`
      : changedCount ? ` · 变更 ${changedCount} 页` : "";
    const packageInfo = revision.package;
    const description = revision.feedback || (revision.revision === 1
      ? "由已确认样品初始化"
      : "基于父版本创建的全稿修订");
    const branchAction = selected && options.canBranch
      ? `<div class="full-deck-revision-card__actions"><button class="btn btn--secondary" type="button" data-action="branch_full_deck_revision" data-revision-hash="${escapeHtml(revision.revision_hash)}">从当前版本创建分支</button></div>`
      : "";
    return `<article class="full-deck-revision-card${selected ? " is-selected" : ""}"><button class="full-deck-revision-card__selector" type="button" data-full-deck-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}"><span class="sample-revision-card__number">R${revision.revision}</span><span class="sample-revision-card__content"><span class="sample-revision-card__title">修订 ${revision.revision}${selected ? '<span class="badge badge--success">当前</span>' : ""}<span class="badge ${statusClass}">${escapeHtml(status)}</span></span><p>${escapeHtml(description)}</p><small>${escapeHtml(revisionDate(revision.created_at))} · ${revision.page_count || packageInfo?.slide_count || 0} 页${changedLabel}${packageInfo ? ` · ${packageInfo.file_count || 0} 个文件` : ""}</small></span></button>${branchAction}</article>`;
  }).join("");
  return `<section class="full-deck-history" aria-labelledby="full-deck-history-title"><div class="sample-history__head"><div><h3 id="full-deck-history-title">全稿修订历史</h3><p>点击任意版本只移动当前指针，不会创建新修订；下一次操作会从选中版本继续。</p></div><span class="badge badge--info">${history.length} 个版本</span></div><div class="full-deck-revision-list">${cards}</div></section>`;
}

function feedbackBody(revision, options) {
  const packageReady = Boolean(revision.package);
  const regenerate = options.canRegenerate
    ? `<button class="btn btn--secondary" type="button" data-action="regenerate_full_deck">${packageReady ? "重新生成非样品页" : "重新生成待完成页"}</button>`
    : "";
  const revise = options.canRevise
    ? '<button class="btn btn--primary" type="submit">让 AI 修改</button>'
    : "";
  if (!regenerate && !revise) return "";
  return `<form class="sample-feedback full-deck-feedback" id="full-deck-feedback-form"><div class="field"><label for="full-deck-feedback">修改意见</label><textarea class="input" id="full-deck-feedback" name="feedback" maxlength="4000" required placeholder="例如：第 5 页把核心结论放进标题，并保持其余页面不变。" aria-describedby="full-deck-feedback-help"></textarea><small id="full-deck-feedback-help">AI 会从当前选中的全稿版本继续；只有声明的变更页会获得新内容引用，成功后保存为不可变子修订。</small><div class="field-error" id="full-deck-feedback-error" role="alert"></div></div><div class="sample-feedback__actions">${regenerate}${revise}</div></form>`;
}

export function fullDeckBody(
  revision,
  escapeHtml = fallbackEscapeHtml,
  options = {},
) {
  const packageInfo = revision.package;
  const sample = options.sample;
  const pages = revision.plan?.pages || [];
  const [status, statusClass] = statusMeta(revision.status);
  const previewTitle = packageInfo
    ? `修订 R${revision.revision} · ${packageInfo.title}`
    : `修订 R${revision.revision} · 样品来源页`;
  const toolbar = packageInfo
    ? `<div><span>${packageInfo.slide_count} 页</span><strong>${escapeHtml(packageInfo.title)}</strong></div><div class="button-row"><span class="badge badge--info">HTML-PPT 全稿 · ${packageInfo.files?.length || 0} 文件</span><a class="btn btn--secondary" href="${escapeHtml(revision.export_url)}" download>导出 ZIP</a></div>`
    : `<div><span>${pages.length} 页清单</span><strong>样品来源页预览</strong></div><span class="badge badge--warning">完整包待生成</span>`;
  const preview = packageInfo || sample?.preview_url
    ? `<div class="sample-preview full-deck-preview"><div class="sample-preview__toolbar">${toolbar}</div><div class="sample-canvas"><iframe id="full-deck-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="${escapeHtml(previewTitle)} HTML-PPT 预览"></iframe></div><p class="sample-preview__hint">${packageInfo ? "翻页与总览由完整 HTML-PPT 自身提供。" : "这里安全加载已确认样品包；下方页面清单显示它在全稿中的对应位置。"}</p></div>`
    : "";
  const callout = revision.status === "approved"
    ? '<div class="callout"><strong>当前全稿已确认。</strong> 历史版本仍可查看、切换和创建工程分支。</div>'
    : "";
  const plan = pagePlanBody(revision, options, escapeHtml);
  const feedback = feedbackBody(revision, options);
  const attempts = attemptsBody(options.attempts || [], escapeHtml);
  const history = historyBody(
    options.history || [],
    options.selectedHash || revision.revision_hash,
    options,
    escapeHtml,
  );
  return {
    body: `${callout}${preview}${plan}${feedback}${attempts}${history}`,
    status: `<div class="document-status"><span class="badge ${statusClass}">${escapeHtml(status)}</span><span class="badge badge--info">修订 ${revision.revision}</span></div>`,
  };
}

export function hydrateFullDeckFrame(root, revision, sample) {
  const frame = root.querySelector("#full-deck-preview-frame");
  if (!frame) return;
  if (revision?.preview_url) {
    frame.src = revision.preview_url;
    return;
  }
  if (sample?.preview_url) frame.src = sample.preview_url;
}
