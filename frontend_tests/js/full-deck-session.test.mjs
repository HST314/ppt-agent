import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createFullDeckNavigateMessage,
  fullDeckBody,
  fullDeckDirectiveTarget,
  isFullDeckSlideChangeMessage,
  updateFullDeckGenerationWorkspace,
} from "../../frontend/static/js/full-deck.js";
import {
  createFullDeckSessionPoller,
  fullDeckSessionPollDelay,
  shouldApplyFullDeckSession,
} from "../../frontend/static/js/full-deck-session.js";
import {
  createFullDeckWorkspaceController,
  isFullDeckSessionJob,
} from "../../frontend/static/js/full-deck-workspace.js";

const revision = {
  revision: 1,
  revision_hash: "sha256:" + "a".repeat(64),
  status: "draft",
  plan: { pages: [] },
};

function session(overrides = {}) {
  return {
    session_id: "session_01",
    status: "running",
    session_version: 4,
    latest_preview_package_id: "package_01",
    preview_url: "/preview/index.html?v=4",
    progress: {
      ready_pages: 2,
      total_pages: 6,
      completed_batches: 0,
      total_batches: 2,
      active_batch_index: 1,
      active_slide_numbers: [3, 4],
    },
    capabilities: ["pause", "add_directive", "cancel"],
    pages: [
      { position: 0, slot_id: "slot_1", source_slide_number: 1, title: "样品封面", generation_status: "sample_ready", source_type: "approved_sample", batch_index: null, error: null },
      { position: 1, slot_id: "slot_2", source_slide_number: 2, title: "样品目录", generation_status: "sample_ready", source_type: "approved_sample", batch_index: null, error: null },
      { position: 2, slot_id: "slot_3", source_slide_number: 3, title: "核心结论", generation_status: "generating", source_type: "generated_segment", batch_index: 1, error: null },
      { position: 3, slot_id: "slot_4", source_slide_number: 4, title: "数据支撑", generation_status: "queued", source_type: "generated_segment", batch_index: 1, error: null },
    ],
    batches: [
      { batch_index: 1, status: "running", source_slide_numbers: [3, 4], attempt_count: 1, applied_directive_ids: [], started_at: "2026-08-26T08:00:00Z", completed_at: null, error: null },
      { batch_index: 2, status: "pending", source_slide_numbers: [5, 6], attempt_count: 0, applied_directive_ids: [], started_at: null, completed_at: null, error: null },
    ],
    directives: [],
    error: null,
    ...overrides,
  };
}

test("生成工作区同时表达批次、页面文字状态、安全预览和键盘按钮", () => {
  const value = session();
  const view = fullDeckBody(revision, undefined, { generationSession: value });

  assert.match(view.body, /已完成 2\/6 页/);
  assert.match(view.body, /第 1\/2 批正在生成第 3–4 页/);
  assert.match(view.body, /title="部分全稿预览，共 2 页"/);
  assert.match(view.body, /data-full-deck-slide="slot_1"/);
  assert.match(view.body, /aria-label="在预览中查看第 1 页：样品封面"/);
  assert.match(view.body, /生成中/);
  assert.match(view.body, /排队中/);
  assert.match(view.body, /data-session-action="pause"/);
  assert.match(view.body, /给后续页面的补充要求/);
  assert.match(view.body, /将从第 2 批（第 5–6 页）开始生效/);
  assert.equal(fullDeckDirectiveTarget(value).batch_index, 2);
});

test("轮询仅发布新 session_version，并在隐藏页面使用 5 秒节奏", async () => {
  const delays = [];
  const applied = [];
  const stopped = [];
  const snapshots = [
    session({ session_version: 4 }),
    session({ session_version: 5, progress: { ...session().progress, ready_pages: 4, completed_batches: 1, active_batch_index: 2, active_slide_numbers: [5, 6] } }),
    session({ session_version: 6, status: "completed", progress: { ...session().progress, ready_pages: 6, completed_batches: 2, active_batch_index: null, active_slide_numbers: [] }, capabilities: [] }),
  ];
  let hidden = false;
  const poller = createFullDeckSessionPoller({
    fetchSession: async () => snapshots.shift(),
    onSnapshot: (snapshot) => applied.push(snapshot.session_version),
    onStop: (snapshot) => stopped.push(snapshot.status),
    isHidden: () => hidden,
    wait: async (delay) => {
      delays.push(delay);
      hidden = delays.length === 1;
    },
  });

  await poller.start("session_01", 4);

  assert.deepEqual(applied, [5, 6]);
  assert.deepEqual(stopped, ["completed"]);
  assert.deepEqual(delays, [1000, 5000, 1000]);
  assert.equal(fullDeckSessionPollDelay(false), 1000);
  assert.equal(fullDeckSessionPollDelay(true), 5000);
  assert.equal(shouldApplyFullDeckSession(6, { session_version: 6 }), false);
});

test("失败批重试会等待会话版本前进后再判断终态", async () => {
  const applied = [];
  const snapshots = [
    session({ status: "failed", session_version: 8, capabilities: ["retry"] }),
    session({ status: "running", session_version: 9 }),
    session({ status: "failed", session_version: 10, capabilities: ["retry"] }),
  ];
  const poller = createFullDeckSessionPoller({
    fetchSession: async () => snapshots.shift(),
    onSnapshot: (snapshot) => applied.push(snapshot.session_version),
    isHidden: () => false,
    wait: async () => {},
  });

  await poller.start("session_01", 8, { waitForVersionChangeFromStopped: true });
  assert.deepEqual(applied, [9, 10]);
});

test("增量 DOM 更新保留指令输入，且只在预览包变化时重载 iframe", () => {
  const regions = new Map(["progress", "pages", "controls", "directive-history", "batches", "preview-toolbar"].map((name) => [name, { innerHTML: "old" }]));
  const textarea = { value: "不要覆盖这段正在输入的指令", disabled: false };
  const submit = { dataset: {}, disabled: false, isConnected: true, textContent: "提交" };
  const form = { elements: { directive: textarea }, querySelector: () => submit };
  const frame = {
    dataset: { previewPackageId: "package_01" },
    src: "/preview/index.html?v=4",
    title: "旧标题",
    addEventListener() {},
  };
  const view = { scrollX: 12, scrollY: 320, scrollTo(x, y) { this.scrollX = x; this.scrollY = y; } };
  const doc = { activeElement: textarea, defaultView: view, getElementById: () => textarea };
  const workspace = {
    dataset: { sessionId: "session_01", sessionVersion: "4", activeSlideId: "slot_1" },
    ownerDocument: doc,
    contains: (node) => node === textarea,
    querySelector(selector) {
      const match = selector.match(/^\[data-session-region="(.+)"\]$/);
      if (match) return regions.get(match[1]);
      if (selector === "[data-session-directive-target]") return { textContent: "" };
      if (selector === "#full-deck-directive-form") return form;
      if (selector === "#full-deck-preview-frame") return frame;
      return null;
    },
    querySelectorAll: () => [],
  };
  const root = {
    querySelector(selector) {
      if (selector === "#full-deck-generation-workspace") return workspace;
      return null;
    },
  };

  const samePackage = updateFullDeckGenerationWorkspace(root, revision, session({ session_version: 5 }));
  assert.equal(textarea.value, "不要覆盖这段正在输入的指令");
  assert.equal(samePackage.previewReloaded, false);
  assert.equal(frame.src, "/preview/index.html?v=4");
  assert.deepEqual([view.scrollX, view.scrollY], [12, 320]);

  const nextPackage = updateFullDeckGenerationWorkspace(root, revision, session({
    session_version: 6,
    latest_preview_package_id: "package_02",
    preview_url: "/preview/index.html?v=6",
  }));
  assert.equal(nextPackage.previewReloaded, true);
  assert.equal(frame.src, "/preview/index.html?v=6");
  assert.equal(textarea.value, "不要覆盖这段正在输入的指令");
});

test("预览消息协议只接受结构正确的 slidechange 消息", () => {
  assert.deepEqual(createFullDeckNavigateMessage("slot_3"), { type: "ppt-agent:navigate", slide_id: "slot_3" });
  assert.equal(isFullDeckSlideChangeMessage({ type: "ppt-agent:slidechange", slide_id: "slot_3", index: 2 }), true);
  assert.equal(isFullDeckSlideChangeMessage({ type: "ppt-agent:slidechange", slide_id: 3 }), false);
  assert.equal(isFullDeckSlideChangeMessage({ type: "other", slide_id: "slot_3" }), false);
});

test("工作区只接受当前 iframe 的切页回执，并识别会话型 Job", () => {
  const frameWindow = {};
  const frame = { contentWindow: frameWindow };
  const pageButton = {
    dataset: { fullDeckSlide: "slot_3" },
    classList: { toggle(_name, value) { pageButton.current = value; } },
    setAttribute(_name, value) { pageButton.ariaCurrent = value; },
  };
  const workspace = {
    dataset: {},
    querySelectorAll: () => [pageButton],
  };
  const root = {
    querySelector(selector) {
      if (selector === "#full-deck-preview-frame") return frame;
      if (selector === "#full-deck-generation-workspace") return workspace;
      return null;
    },
  };
  const controller = createFullDeckWorkspaceController({
    api: {},
    root,
    getProject: () => null,
    getSession: () => null,
    setSession() {},
    getView: () => "workspace",
    setFocusStage() {},
    render: async () => {},
    refreshProject: async () => {},
    notify() {},
    errorMessage: () => "错误",
    announce() {},
    escapeHtml: String,
  });

  controller.handleMessage({ source: {}, data: { type: "ppt-agent:slidechange", slide_id: "slot_3" } });
  assert.equal(pageButton.current, undefined);
  controller.handleMessage({ source: frameWindow, data: { type: "ppt-agent:slidechange", slide_id: "slot_3" } });
  assert.equal(pageButton.current, true);
  assert.equal(pageButton.ariaCurrent, "true");
  assert.equal(isFullDeckSessionJob({ operation: "generate_full_deck" }), true);
  assert.equal(isFullDeckSessionJob({ operation: "revise_full_deck" }), false);
});
