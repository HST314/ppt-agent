const focusAttributes = {
  statusFilter: "data-status-filter",
  eventExpand: "data-event-expand",
  copyEvent: "data-copy-event",
};

export function createStatusSignature(activity, branches) {
  return JSON.stringify([activity, branches]);
}

export function statusElapsedLabel(summary, stageLabels) {
  const job = summary.active_job;
  const duration = job?.started_at ? Math.max(0, Math.floor((Date.now() - new Date(job.started_at).getTime()) / 1000)) : null;
  const elapsed = duration === null ? "—" : duration < 60 ? `${duration}s` : `${Math.floor(duration / 60)}m ${duration % 60}s`;
  return `${stageLabels[summary.stage] || summary.stage} · ${elapsed}`;
}

export function captureStatusViewport(content) {
  const active = document.activeElement;
  let focus = null;
  if (active && content.contains(active)) {
    if (active.id) focus = { id: active.id };
    else {
      const key = Object.keys(focusAttributes).find((name) => active.dataset[name] !== undefined);
      if (key) focus = { key, value: active.dataset[key] };
    }
  }
  return { scrollX: window.scrollX, scrollY: window.scrollY, focus };
}

export function restoreStatusViewport(content, snapshot) {
  if (!snapshot) return;
  let target = snapshot.focus?.id ? document.getElementById(snapshot.focus.id) : null;
  if (!target && snapshot.focus?.key) {
    const attribute = focusAttributes[snapshot.focus.key];
    target = Array.from(content.querySelectorAll(`[${attribute}]`))
      .find((node) => node.dataset[snapshot.focus.key] === snapshot.focus.value);
  }
  target?.focus({ preventScroll: true });
  const previousBehavior = document.documentElement.style.scrollBehavior;
  document.documentElement.style.scrollBehavior = "auto";
  window.scrollTo(snapshot.scrollX, snapshot.scrollY);
  document.documentElement.style.scrollBehavior = previousBehavior;
}
