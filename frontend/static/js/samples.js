const fallbackEscapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

export function readySampleBody(count, sparkIcon, attempts = [], escapeHtml = fallbackEscapeHtml) {
  return `<div class="sample-canvas sample-canvas--empty"><div><span class="empty-state__icon">${sparkIcon}</span><h3>准备生成 ${count} 页 HTML-PPT</h3><p>Agent 会按需阅读 Skills，从大纲中选择连续 ${count} 页，并生成一个带自身翻页交互、可独立打开的 HTML-PPT 包。</p><button class="btn btn--primary" data-action="generate_sample">生成 HTML-PPT</button></div></div>${attemptsBody(attempts, escapeHtml)}`;
}

function revisionDate(value) {
  if (!value) return "时间未知";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(new Date(value));
  } catch { return String(value); }
}

function sourceRange(packageInfo) {
  const numbers = (packageInfo?.slides || [])
    .map((slide) => slide.source_slide_number)
    .filter((number) => Number.isInteger(number));
  if (!numbers.length) return "";
  return numbers.length === 1 ? ` · 大纲第 ${numbers[0]} 页` : ` · 大纲第 ${numbers[0]}–${numbers.at(-1)} 页`;
}

function attemptsBody(attempts, escapeHtml) {
  if (!attempts.length) return "";
  const cards = attempts.map((attempt) => {
    const badge = attempt.published
      ? '<span class="badge badge--success">已发布为 PPT 样品</span>'
      : attempt.status === "started"
        ? '<span class="badge badge--info">生成中</span>'
        : '<span class="badge badge--warning">未发布</span>';
    const options = attempt.resume_options || [];
    const preferredRounds = options.includes(10) ? 10 : options[0];
    const resume = attempt.resume_available && preferredRounds
      ? `<div class="sample-attempt-card__resume"><div class="field"><label for="resume-rounds-${escapeHtml(attempt.prompt_call_id)}">追加轮次</label><select class="input" id="resume-rounds-${escapeHtml(attempt.prompt_call_id)}" data-resume-rounds="${escapeHtml(attempt.prompt_call_id)}">${options.map((value) => `<option value="${value}" ${value === preferredRounds ? "selected" : ""}>${value} 轮</option>`).join("")}</select></div><button class="btn btn--primary" type="button" data-action="resume_sample" data-prompt-call-id="${escapeHtml(attempt.prompt_call_id)}">追加 ${preferredRounds} 轮并继续</button><small>从已保存的对话、Skill 读取结果和草稿包继续；整条生成链累计最多 100 轮。</small></div>`
      : attempt.resume_blocked_reason
        ? `<div class="callout callout--warning sample-attempt-card__blocked">${escapeHtml(attempt.resume_blocked_reason)}</div>`
        : "";
    return `<li class="sample-attempt-card"><div class="sample-attempt-card__head"><strong>尝试 ${attempt.attempt}</strong>${badge}</div><p>${escapeHtml(attempt.reason)}</p><small>${attempt.tool_rounds} 个工具轮次 · ${attempt.tool_call_count} 次工具调用 · ${attempt.skill_read_count} 次 Skill 读取 · ${escapeHtml(revisionDate(attempt.completed_at || attempt.started_at))}</small>${resume}</li>`;
  }).join("");
  return `<section class="sample-attempts" aria-labelledby="sample-attempts-title" aria-live="polite"><div class="sample-history__head"><div><h3 id="sample-attempts-title">本次生成尝试</h3><p>结构与安全问题会阻止发布；视觉质量建议仅用于人工判断，不会淘汰样品。</p></div><span class="badge badge--info">${attempts.length} 次</span></div><ol class="sample-attempt-list">${cards}</ol></section>`;
}

function historyBody(history, selectedHash, escapeHtml) {
  if (!history.length) return "";
  const cards = history.map((revision) => {
    const selected = revision.revision_hash === selectedHash;
    const packageInfo = revision.package;
    const slideCount = packageInfo?.slide_count ?? revision.pages?.length ?? 0;
    const feedback = revision.feedback
      ? `<p>${escapeHtml(revision.feedback)}</p>`
      : `<p>${revision.revision === 1 ? "首次生成的 HTML-PPT" : "基于当前版本生成的新修订"}</p>`;
    const branchAction = selected
      ? `<div class="sample-revision-card__actions"><button class="btn btn--secondary" type="button" data-action="branch_sample_revision" data-revision-hash="${escapeHtml(revision.revision_hash)}">从当前版本创建分支</button></div>`
      : "";
    return `<article class="sample-revision-card${selected ? " is-selected" : ""}"><button class="sample-revision-card__selector" type="button" data-sample-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}" ${selected ? "disabled" : ""}><span class="sample-revision-card__number">R${revision.revision}</span><span class="sample-revision-card__content"><span class="sample-revision-card__title">修订 ${revision.revision}${selected ? '<span class="badge badge--success">当前</span>' : ""}${revision.status === "approved" ? '<span class="badge badge--info">已确认</span>' : ""}</span>${feedback}<small>${escapeHtml(revisionDate(revision.created_at))} · ${slideCount} 页${sourceRange(packageInfo)}${packageInfo ? ` · ${packageInfo.file_count} 个文件` : ""}</small></span></button>${branchAction}</article>`;
  }).join("");
  return `<section class="sample-history" aria-labelledby="sample-history-title"><div class="sample-history__head"><div><h3 id="sample-history-title">修订历史</h3><p>点击任意历史版本会移动当前指针并立即打开；不会创建新修订。下一次 AI 修改将以它为父版本。</p></div><span class="badge badge--info">${history.length} 个版本</span></div><div class="sample-revision-list">${cards}</div></section>`;
}

export function sampleBody(sample, escapeHtml, options = {}) {
  const approved = sample.status === "approved";
  const packageInfo = sample.package;
  const history = options.history || [];
  const selectedHash = options.selectedHash || sample.revision_hash;
  const slideCount = packageInfo?.slide_count ?? sample.pages?.length ?? 0;
  const fileCount = packageInfo?.files?.length ?? 1;
  const preview = packageInfo
    ? `<div class="sample-preview"><div class="sample-preview__toolbar"><div><span>${slideCount} 页</span><strong>${escapeHtml(packageInfo.title)}</strong></div><div class="button-row"><span class="badge badge--info">HTML-PPT 包 · ${fileCount} 文件</span><a class="btn btn--secondary" href="${escapeHtml(sample.export_url)}" download>导出 ZIP</a></div></div><div class="sample-canvas"><iframe id="sample-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="${escapeHtml(packageInfo.title)} HTML-PPT 预览"></iframe></div><p class="sample-preview__hint">翻页、总览和演讲交互由 HTML-PPT 自身提供；预览器只负责安全加载完整文件包。</p></div>`
    : `<div class="sample-preview"><div class="sample-preview__toolbar"><div><span>${slideCount} 页</span><strong>旧版只读样品</strong></div><span class="badge badge--warning">旧格式</span></div><div class="sample-canvas"><iframe id="sample-preview-frame" sandbox="" referrerpolicy="no-referrer" title="旧版 PPT 样品预览"></iframe></div></div>`;
  const completion = approved ? '<div class="callout"><strong>HTML-PPT 已确认。</strong> 仍可继续提交意见创建新修订；导出会包含整个文件包。</div>' : "";
  const fullDeckAction = options.canEnterFullDeck
    ? '<button class="btn btn--primary" type="button" data-action="enter_full_deck">确认样品并进入全稿</button>'
    : options.fullDeckExists
      ? '<button class="btn btn--primary" type="button" data-action="continue_full_deck">查看 PPT 全稿</button>'
      : "";
  const feedback = `<form class="sample-feedback" id="sample-feedback-form"><div class="field"><label for="sample-feedback">修改意见</label><textarea class="input" id="sample-feedback" name="feedback" maxlength="4000" required placeholder="例如：封面减少文字，把核心数字放大；保留当前翻页方式。" aria-describedby="sample-feedback-help"></textarea><small id="sample-feedback-help">AI 会读取当前指针对应的完整文件包，并生成一个以当前版本为父节点的新修订。</small><div class="field-error" id="sample-feedback-error" role="alert"></div></div><div class="sample-feedback__actions"><button class="btn btn--secondary" type="button" data-action="regenerate_sample">重新生成</button>${fullDeckAction}<button class="btn btn--secondary" type="submit">让 AI 修改</button></div></form><div class="field-error sample-enter-error" id="sample-enter-error" role="alert"></div>`;
  const historyMarkup = historyBody(history, selectedHash, escapeHtml);
  const attemptsMarkup = attemptsBody(options.attempts || [], escapeHtml);
  const status = `<div class="document-status"><span class="badge ${approved ? "badge--success" : "badge--warning"}">${approved ? "已确认" : "等待反馈"}</span><span class="badge badge--info">修订 ${sample.revision}</span></div>`;
  return { body: `${completion}${preview}${feedback}${attemptsMarkup}${historyMarkup}`, status };
}

export function hydrateSampleFrame(root, sample) {
  const frame = root.querySelector("#sample-preview-frame");
  if (!sample || !frame) return;
  if (sample.preview_url) {
    frame.src = sample.preview_url;
    return;
  }
  const legacy = sample.pages?.[0];
  if (legacy?.html) frame.srcdoc = legacy.html;
}
