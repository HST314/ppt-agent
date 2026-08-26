async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.error?.message || payload?.detail || `请求失败 (${response.status})`;
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    error.code = payload?.error?.code || null;
    throw error;
  }
  return payload;
}

export const api = {
  health: () => request("/api/health"),
  runtime: () => request("/api/runtime-context"),
  updateRuntime: (payload) => request("/api/runtime-context", { method: "PUT", body: JSON.stringify(payload) }),
  projects: () => request("/api/projects"),
  project: (id) => request(`/api/projects/${encodeURIComponent(id)}`),
  create: (payload) => request("/api/projects", { method: "POST", body: JSON.stringify(payload) }),
  startJob: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/jobs`, { method: "POST", body: JSON.stringify(payload) }),
  resumeSample: (id, promptCallId, payload) => request(`/api/projects/${encodeURIComponent(id)}/samples/attempts/${encodeURIComponent(promptCallId)}/resume`, { method: "POST", body: JSON.stringify(payload) }),
  job: (id) => request(`/api/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id) => request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" }),
  answer: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/clarification`, { method: "POST", body: JSON.stringify(payload) }),
  revise: (id, type, payload) => request(`/api/projects/${encodeURIComponent(id)}/documents/${type}/revisions`, { method: "POST", body: JSON.stringify(payload) }),
  approve: (id, type, payload) => request(`/api/projects/${encodeURIComponent(id)}/documents/${type}/approve`, { method: "POST", body: JSON.stringify(payload) }),
  enterSample: (id, checkpointId) => request(`/api/projects/${encodeURIComponent(id)}/samples/enter`, { method: "POST", body: JSON.stringify({ checkpoint_id: checkpointId }) }),
  approveSample: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/samples/approve`, { method: "POST", body: JSON.stringify(payload) }),
  restoreSample: (id, revisionHash, checkpointId) => request(`/api/projects/${encodeURIComponent(id)}/samples/revisions/${encodeURIComponent(revisionHash)}/restore`, { method: "POST", body: JSON.stringify({ checkpoint_id: checkpointId }) }),
  branchFromSample: (id, revisionHash, payload) => request(`/api/projects/${encodeURIComponent(id)}/samples/revisions/${encodeURIComponent(revisionHash)}/branches`, { method: "POST", body: JSON.stringify(payload) }),
  enterFullDeck: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/enter`, { method: "POST", body: JSON.stringify(payload) }),
  approveFullDeck: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/approve`, { method: "POST", body: JSON.stringify(payload) }),
  fullDeckRevisions: (id) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/revisions`),
  fullDeckRevision: (id, revisionHash) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/revisions/${encodeURIComponent(revisionHash)}`),
  restoreFullDeck: (id, revisionHash, checkpointId) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/revisions/${encodeURIComponent(revisionHash)}/restore`, { method: "POST", body: JSON.stringify({ checkpoint_id: checkpointId }) }),
  branchFromFullDeck: (id, revisionHash, payload) => request(`/api/projects/${encodeURIComponent(id)}/full-deck/revisions/${encodeURIComponent(revisionHash)}/branches`, { method: "POST", body: JSON.stringify(payload) }),
  fullDeckPreviewUrl: (id, revisionHash, path) => `/api/projects/${encodeURIComponent(id)}/full-deck/revisions/${encodeURIComponent(revisionHash)}/preview/${String(path).split("/").map(encodeURIComponent).join("/")}`,
  fullDeckExportUrl: (id, revisionHash) => `/api/projects/${encodeURIComponent(id)}/full-deck/revisions/${encodeURIComponent(revisionHash)}/export`,
  timeline: (id) => request(`/api/projects/${encodeURIComponent(id)}/timeline`),
  activity: (id) => request(`/api/projects/${encodeURIComponent(id)}/activity`),
  branches: (id) => request(`/api/projects/${encodeURIComponent(id)}/branches`),
  createBranch: (id, payload) => request(`/api/projects/${encodeURIComponent(id)}/branches`, { method: "POST", body: JSON.stringify(payload) }),
  switchBranch: (id, checkpointId) => request(`/api/projects/${encodeURIComponent(id)}/branches/switch`, { method: "POST", body: JSON.stringify({ checkpoint_id: checkpointId }) }),
};
