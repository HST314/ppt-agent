const SAMPLE_CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  "img-src data:",
  "font-src data:",
  "media-src data:",
  "script-src 'none'",
  "connect-src 'none'",
  "frame-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

let frameObserver = null;

export function isolatedSampleHtml(html) {
  const policy = `<meta http-equiv="Content-Security-Policy" content="${SAMPLE_CSP}">`;
  if (/<head(?:\s[^>]*)?>/i.test(html)) {
    return html.replace(/<head(?:\s[^>]*)?>/i, (match) => `${match}${policy}`);
  }
  if (/<html(?:\s[^>]*)?>/i.test(html)) {
    return html.replace(/<html(?:\s[^>]*)?>/i, (match) => `${match}<head>${policy}</head>`);
  }
  return `<!doctype html><html><head>${policy}</head><body>${html}</body></html>`;
}

export function readySampleBody(count, sparkIcon) {
  return `<div class="sample-canvas sample-canvas--empty"><div><span class="empty-state__icon">${sparkIcon}</span><h3>准备生成 ${count} 页 HTML 样品</h3><p>Agent 会从已确认大纲中选择代表性页面，统一设计语言后放入隔离画框预览。</p><button class="btn btn--primary" data-action="generate_sample">生成 PPT 样品</button></div></div>`;
}

export function sampleBody(sample, requestedIndex, escapeHtml) {
  const pageIndex = Math.min(Math.max(requestedIndex, 0), sample.pages.length - 1);
  const current = sample.pages[pageIndex];
  const approved = sample.status === "approved";
  const tabs = sample.pages.map((page, index) => `<button class="sample-page-tab" type="button" data-sample-page="${index}" ${index === pageIndex ? 'aria-current="page"' : ""}><span>${String(index + 1).padStart(2, "0")}</span><strong>${escapeHtml(page.title)}</strong></button>`).join("");
  const preview = `<div class="sample-preview"><div class="sample-preview__toolbar"><div><span id="sample-current-counter">${pageIndex + 1} / ${sample.pages.length}</span><strong id="sample-current-title">${escapeHtml(current.title)}</strong></div><span class="badge badge--info">16:9 HTML</span></div><div class="sample-canvas"><iframe id="sample-preview-frame" sandbox="" referrerpolicy="no-referrer" title="PPT 样品预览"></iframe></div><nav class="sample-page-tabs" aria-label="选择样品页">${tabs}</nav></div>`;
  const completion = approved ? '<div class="callout"><strong>样品已确认。</strong> 后续可继续提交意见创建新修订；PPT 全稿阶段将在下一阶段开放。</div>' : "";
  const feedback = `<form class="sample-feedback" id="sample-feedback-form"><div class="field"><label for="sample-feedback">修改意见</label><textarea class="input" id="sample-feedback" name="feedback" maxlength="4000" required placeholder="例如：第 1 页减少文字，把核心数字放大；整体改为更克制的深色风格。" aria-describedby="sample-feedback-help"></textarea><small id="sample-feedback-help">说明希望保留和调整的内容，AI 会基于当前样品生成新修订。</small><div class="field-error" id="sample-feedback-error" role="alert"></div></div><div class="sample-feedback__actions"><button class="btn btn--secondary" type="button" data-action="regenerate_sample">重新生成</button>${approved ? "" : '<button class="btn btn--secondary" type="button" data-action="approve_sample">确认样品</button>'}<button class="btn btn--primary" type="submit">让 AI 修改</button></div></form>`;
  const status = `<div class="document-status"><span class="badge ${approved ? "badge--success" : "badge--warning"}">${approved ? "已确认" : "等待反馈"}</span><span class="badge badge--info">修订 ${sample.revision}</span></div>`;
  return { pageIndex, body: `${completion}${preview}${feedback}`, status };
}

export function hydrateSampleFrame(root, sample, requestedIndex) {
  frameObserver?.disconnect();
  frameObserver = null;
  const frame = root.querySelector("#sample-preview-frame");
  if (!sample || !frame) return;
  const index = Math.min(Math.max(requestedIndex, 0), sample.pages.length - 1);
  const page = sample.pages[index];
  frame.title = `PPT 样品第 ${index + 1} 页：${page.title}`;
  frame.srcdoc = isolatedSampleHtml(page.html);
  const canvas = frame.closest(".sample-canvas");
  const scaleFrame = () => {
    const scale = canvas.clientWidth / 1280;
    frame.style.transform = `scale(${scale})`;
  };
  scaleFrame();
  if ("ResizeObserver" in window) {
    frameObserver = new ResizeObserver(scaleFrame);
    frameObserver.observe(canvas);
  }
}

export function selectSamplePage(root, sample, index) {
  if (!sample || index < 0 || index >= sample.pages.length) return false;
  root.querySelectorAll("[data-sample-page]").forEach((button) => {
    if (Number(button.dataset.samplePage) === index) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const page = sample.pages[index];
  const title = root.querySelector("#sample-current-title");
  const counter = root.querySelector("#sample-current-counter");
  if (title) title.textContent = page.title;
  if (counter) counter.textContent = `${index + 1} / ${sample.pages.length}`;
  hydrateSampleFrame(root, sample, index);
  return true;
}
