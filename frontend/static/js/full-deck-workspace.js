import {
  createFullDeckNavigateMessage,
  isFullDeckSlideChangeMessage,
  setFullDeckActiveSlide,
  updateFullDeckGenerationWorkspace,
} from "./full-deck.js";
import { createFullDeckSessionPoller, isStoppedFullDeckSession } from "./full-deck-session.js";

export function isFullDeckSessionJob(job) {
  return [
    "generate_full_deck",
    "resume_full_deck_generation",
    "retry_full_deck_generation",
  ].includes(job?.operation);
}

export function createFullDeckWorkspaceController({
  api,
  root,
  getProject,
  getSession,
  setSession,
  getView,
  setFocusStage,
  render,
  refreshProject,
  notify,
  errorMessage,
  announce,
  escapeHtml,
}) {
  let pollErrorShown = false;

  async function applySnapshot(session) {
    const project = getProject();
    const current = getSession();
    if (!project || (current && current.session_id !== session.session_id)) return;
    setSession(session);
    project.full_deck_generation_session = session;
    pollErrorShown = false;
    if (getView() !== "workspace") return;
    const result = updateFullDeckGenerationWorkspace(
      root,
      project.full_deck_revision,
      session,
      escapeHtml,
    );
    if (result.requiresRender) {
      await render();
      return;
    }
    announce(`全稿生成进度已更新：${session.progress?.ready_pages || 0}/${session.progress?.total_pages || 0} 页已就绪。`);
  }

  const poller = createFullDeckSessionPoller({
    fetchSession: (sessionId) => api.fullDeckGenerationSession(getProject().project_id, sessionId),
    onSnapshot: applySnapshot,
    onStop: async (session) => {
      const project = getProject();
      if (!project || getSession()?.session_id !== session.session_id) return;
      project.active_job = null;
      if (session.status === "completed") {
        await refreshProject();
        notify("全稿全部批次已完成，正式修订已发布。");
        return;
      }
      if (session.status === "paused") notify("全稿已在批次边界安全暂停。");
      if (session.status === "failed") notify(session.error?.message || "当前批未完成，可重试当前批。", true);
      if (session.status === "cancelled") notify("全稿生成已取消，已成功页面仍然保留。");
      if (session.status === "stale") notify("工程基线已变化，此生成会话不能继续。", true);
    },
    onError: (error) => {
      if (!pollErrorShown) notify(errorMessage(error, "生成进度同步失败，稍后将自动重试。"), true);
      pollErrorShown = true;
    },
  });

  function startPolling({ waitForVersionChangeFromStopped = false } = {}) {
    const session = getSession();
    if (!session || (isStoppedFullDeckSession(session.status) && !waitForVersionChangeFromStopped) || getView() !== "workspace") return;
    void poller.start(session.session_id, session.session_version, { waitForVersionChangeFromStopped });
  }

  async function load(project) {
    const summary = project?.full_deck_generation_session;
    if (!summary?.session_id) return null;
    try {
      return await api.fullDeckGenerationSession(project.project_id, summary.session_id);
    } catch (error) {
      notify(errorMessage(error, "生成会话详情暂时无法读取，请稍后刷新。"), true);
      return null;
    }
  }

  async function startGeneration(button) {
    const project = getProject();
    if (!project?.full_deck_revision) return false;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在启动分批生成…';
    try {
      const result = await api.startFullDeckGeneration(project.project_id, {
        checkpoint_id: project.checkpoint_id,
        revision_hash: project.full_deck_revision.revision_hash,
      });
      setSession(result.session);
      project.full_deck_generation_session = result.session;
      project.active_job = result.job;
      setFocusStage("ppt_full");
      await render();
      announce("全稿分批生成已开始。");
      startPolling();
      return true;
    } catch (error) {
      const message = errorMessage(error, "暂时无法开始全稿生成，请刷新后重试。");
      if (button.isConnected) {
        button.disabled = false;
        button.innerHTML = original;
        button.focus();
      }
      notify(message, true);
      return false;
    }
  }

  async function refreshAfterConflict() {
    const project = getProject();
    const current = getSession();
    if (!current || !project) return;
    try {
      await applySnapshot(await api.fullDeckGenerationSession(project.project_id, current.session_id));
    } catch { /* keep the bounded action error already shown */ }
  }

  async function control(action, button) {
    const project = getProject();
    const session = getSession();
    if (!session || !project) return;
    const methods = {
      pause: api.pauseFullDeckGeneration,
      resume: api.resumeFullDeckGeneration,
      retry: api.retryFullDeckGeneration,
    };
    const labels = {
      pause: "正在请求暂停…",
      resume: "正在继续…",
      retry: "正在重试当前批…",
    };
    const method = methods[action];
    if (!method) return;
    const original = button.innerHTML;
    const errorNode = root.querySelector("#full-deck-session-control-error");
    if (errorNode) errorNode.textContent = "";
    button.disabled = true;
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${labels[action]}`;
    try {
      const result = await method(project.project_id, session.session_id, session.session_version);
      const next = result.session || result;
      if (result.job) project.active_job = result.job;
      await applySnapshot(next);
      if (["resume", "retry"].includes(action)) {
        poller.stop();
        startPolling({ waitForVersionChangeFromStopped: action === "retry" });
      }
      if (action === "pause") announce("暂停请求已登记，当前批完成后会安全暂停。");
    } catch (error) {
      const message = errorMessage(error, "生成控制操作未完成，请刷新状态后重试。");
      await refreshAfterConflict();
      const currentError = root.querySelector("#full-deck-session-control-error");
      if (currentError) currentError.textContent = message;
      if (button.isConnected) {
        button.disabled = false;
        button.innerHTML = original;
        button.focus();
      }
    }
  }

  async function submitDirective(form) {
    const project = getProject();
    const session = getSession();
    if (!session || !project) return;
    const textarea = form.elements.directive;
    const content = String(textarea.value || "").trim();
    const errorNode = form.querySelector("#full-deck-directive-error");
    const confirmation = form.querySelector("#full-deck-directive-confirmation");
    if (!content) {
      errorNode.textContent = "请先输入希望后续批次遵循的要求。";
      textarea.setAttribute("aria-invalid", "true");
      textarea.focus();
      return;
    }
    errorNode.textContent = "";
    confirmation.textContent = "";
    textarea.removeAttribute("aria-invalid");
    const submit = form.querySelector('button[type="submit"]');
    submit.dataset.submitting = "true";
    submit.disabled = true;
    submit.innerHTML = '<span class="spinner" aria-hidden="true"></span>正在提交…';
    try {
      const directive = await api.addFullDeckGenerationDirective(
        project.project_id,
        session.session_id,
        { session_version: session.session_version, content },
      );
      textarea.value = "";
      confirmation.textContent = `已保存，将从第 ${directive.apply_from_batch_index} 批（${directive.apply_from_slide_numbers.join("、")} 页）开始生效。`;
      await applySnapshot(await api.fullDeckGenerationSession(project.project_id, session.session_id));
    } catch (error) {
      const message = errorMessage(error, "补充要求未保存，请刷新状态后重试。");
      await refreshAfterConflict();
      errorNode.textContent = message;
      textarea.focus();
    } finally {
      if (submit.isConnected) {
        submit.dataset.submitting = "false";
        submit.disabled = !(getSession()?.capabilities || []).includes("add_directive");
        submit.textContent = "提交下一批指令";
      }
    }
  }

  function wire() {
    const workspace = root.querySelector("#full-deck-generation-workspace");
    if (!workspace || workspace.dataset.wired === "true") return;
    workspace.dataset.wired = "true";
    workspace.addEventListener("click", async (event) => {
      const page = event.target.closest("[data-full-deck-slide]");
      if (page && workspace.contains(page)) {
        const frame = workspace.querySelector("#full-deck-preview-frame");
        setFullDeckActiveSlide(root, page.dataset.fullDeckSlide);
        frame?.contentWindow?.postMessage(createFullDeckNavigateMessage(page.dataset.fullDeckSlide), "*");
        return;
      }
      const action = event.target.closest("[data-session-action]");
      if (action && workspace.contains(action)) await control(action.dataset.sessionAction, action);
    });
    workspace.querySelector("#full-deck-directive-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await submitDirective(event.currentTarget);
    });
  }

  function handleMessage(event) {
    const frame = root.querySelector("#full-deck-preview-frame");
    if (!frame || event.source !== frame.contentWindow || !isFullDeckSlideChangeMessage(event.data)) return;
    setFullDeckActiveSlide(root, event.data.slide_id);
  }

  return {
    applySnapshot,
    handleMessage,
    load,
    startGeneration,
    startPolling,
    stopPolling: () => poller.stop(),
    visibilityChanged: () => { if (!document.hidden) poller.wake(); },
    wire,
  };
}
