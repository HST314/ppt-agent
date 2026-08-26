from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_frontend_has_three_top_level_views_and_seven_stages() -> None:
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")

    assert all(f'data-view="{view}"' in html for view in ("workspace", "status", "settings"))
    assert all(name in app for name in ("任务卡", "澄清问题", "叙事结构", "逐页大纲", "PPT 样品", "PPT 全稿", "确认验收"))
    assert 'id="topnav-branch"' in html
    assert 'id="sidebar-toggle"' in html
    assert "创作进度" in app
    assert 'data-snapshot-stage="${stage.id}"' in app
    assert "重跑此阶段并创建分支" in app
    assert 'mode: "rerun_stage"' in app
    assert 'id="branch-create-form"' not in app
    assert 'id="settings-form"' in app
    assert all(action in app for action in ("openBranchDialog", "createBranch", "switchBranch", "updateRuntime"))
    assert 'audience: document.querySelector("#audience").value.trim(),' in app
    assert 'audience: document.querySelector("#audience").value.trim() || null' not in app
    assert 'name="sample_page_count"' in app
    assert 'id="sample-feedback-form"' in samples
    assert 'data-action="generate_sample"' in samples
    assert '${completion}${preview}${feedback}${attemptsMarkup}${historyMarkup}' in samples
    assert "本次生成尝试" in samples
    assert "已发布为 PPT 样品" in samples
    assert "source_slide_number" in samples
    assert 'id="max-tool-rounds" name="max_tool_rounds" type="number" min="0" max="100"' in app
    assert "不会创建新修订" in samples
    assert 'data-action="branch_sample_revision"' in samples
    assert 'data-sample-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}"' in samples
    assert "restoreSample:" in (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")


def test_full_deck_workspace_contracts_match_the_sample_interaction_model() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")
    full_deck = (ROOT / "frontend/static/js/full-deck.js").read_text(encoding="utf-8")
    api = (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")

    assert '{ id: "ppt_full", label: "PPT 全稿" }' in app
    assert '{ id: "ppt_full", label: "PPT 全稿", future: true }' not in app
    assert 'data-action="enter_full_deck"' in samples
    assert "确认样品并进入全稿" in samples
    assert "confirm(" not in app
    assert "正在进入全稿" in app
    assert all(code in app for code in (
        "full_deck_already_initialized", "full_deck_plan_invalid",
        "full_deck_revision_not_found", "stale_revision", "active_job",
    ))
    assert 'document.querySelector("#full-deck-title")?.focus()' in app
    assert "enterFullDeck:" in api
    assert all(name in api for name in (
        "fullDeckRevisions:", "fullDeckRevision:", "restoreFullDeck:",
        "branchFromFullDeck:", "fullDeckPreviewUrl:", "fullDeckExportUrl:",
    ))

    assert 'id="full-deck-title"' in app
    assert 'id="full-deck-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer"' in full_deck
    assert 'id="full-deck-feedback-form"' in full_deck
    assert "完整页面清单" in full_deck
    assert all(label in full_deck for label in (
        "已就绪", "待生成", "样品来源", "本次全稿操作尝试", "全稿修订历史",
    ))
    assert 'data-full-deck-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}"' in full_deck
    assert 'data-action="branch_full_deck_revision"' in full_deck
    assert "不会创建新修订" in full_deck
    assert "aspect-ratio:16/9" in css
    assert ".full-deck-page-grid" in css
    assert "@media(max-width:767px)" in css


def test_full_deck_revision_actions_expose_feedback_change_and_loading_context() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    full_deck = (ROOT / "frontend/static/js/full-deck.js").read_text(encoding="utf-8")
    server = (ROOT / "main_front.py").read_text(encoding="utf-8")

    assert all(operation in server for operation in (
        '"revise_full_deck"', '"regenerate_full_deck"', "revision_hash",
    ))
    assert 'capabilities.includes("revise_full_deck")' in app
    assert 'capabilities.includes("regenerate_full_deck")' in app
    assert "正在启动全稿修改" in app
    assert "正在启动重新生成" in app
    assert "只有声明的变更页会获得新内容引用" in full_deck
    assert "变更第 ${changedPages.map" in full_deck
    assert "attempt.reason" in full_deck
    assert 'role="alert"' in full_deck


def test_phase5_acceptance_workspace_is_accessible_and_auditable() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    full_deck = (ROOT / "frontend/static/js/full-deck.js").read_text(encoding="utf-8")
    api = (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")
    server = (ROOT / "main_front.py").read_text(encoding="utf-8")

    assert '{ id: "acceptance", label: "确认验收" }' in app
    assert '{ id: "acceptance", label: "确认验收", future: true }' not in app
    assert 'ready_for_review: "待最终验收"' in app
    assert "function acceptanceView()" in app
    assert 'id="acceptance-title" tabindex="-1"' in app
    assert 'document.querySelector("#acceptance-title")?.focus()' in app
    assert 'data-action="approve_full_deck"' in full_deck
    assert "确认全稿并进入验收" in full_deck
    assert "正在进入验收" in app
    assert 'id="full-deck-approve-error" role="alert"' in full_deck
    assert "验收基线已锁定" in full_deck
    assert "已确认版本保持只读" in full_deck
    assert "创建后续全稿修订" in full_deck
    assert "导出验收审计" in full_deck
    assert "后台完整项目" in full_deck
    assert "retained_project_path" in server
    assert 'stage === "ppt_full" || stage === "acceptance"' in app
    assert "已确认为验收基线" in app
    assert "approveFullDeck:" in api
    assert "/full-deck/approve" in api
    assert "/audit/export" in server
    assert ".acceptance-overview" in css
    assert ".full-deck-approval .btn{width:100%}" in css


def test_full_deck_preview_is_sandboxed_and_uses_package_routes() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    full_deck = (ROOT / "frontend/static/js/full-deck.js").read_text(encoding="utf-8")
    server = (ROOT / "main_front.py").read_text(encoding="utf-8")

    assert "hydrateFullDeckFrame" in app
    assert "frame.src = revision.preview_url" in full_deck
    assert "frame.src = sample.preview_url" in full_deck
    assert "/full-deck/revisions/{revision_hash}/preview/{logical_path:path}" in server
    assert "/full-deck/revisions/{revision_hash}/export" in server
    assert '"sandbox allow-scripts; default-src \'none\'' in server
    assert "innerHTML = page.html" not in app + full_deck


def test_sample_preview_is_sandboxed_and_never_inserted_into_parent_dom() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")
    server = (ROOT / "main_front.py").read_text(encoding="utf-8")

    assert 'id="sample-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer"' in samples
    assert '"sandbox allow-scripts; default-src \'none\'' in server
    assert 'frame.src = sample.preview_url' in samples
    assert "script-src 'self' 'unsafe-inline' data: blob:" in server
    assert "connect-src 'none'" in server
    assert "frame-src 'self'" in server
    assert "innerHTML = page.html" not in app + samples


def test_status_console_supports_stable_live_filter_search_and_expand() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    status_view = (ROOT / "frontend/static/js/status-view.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")
    api = (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")

    assert "api.activity" in app
    assert 'id="status-search"' in app
    assert "data-status-filter" in app
    assert "data-event-expand" in app
    assert "data-copy-event" in app
    assert "window.setInterval" in app
    assert "refreshStatusView" in app
    assert "preserveStatusViewport: true" in app
    assert "skipUnchangedStatus: skipUnchanged" in app
    assert 'id="status-elapsed"' in app
    assert "signature === statusDataSignature" in app
    assert "statusPollInFlight" in app
    assert 'from "./status-view.js"' in app
    assert "window.scrollTo(snapshot.scrollX, snapshot.scrollY)" in status_view
    assert "需要关注" not in app
    assert ".status-errors" not in css
    assert "/activity" in api
    assert ".event-density" in css
    assert ".activity-event" in css


def test_markdown_renderer_escapes_raw_html() -> None:
    source = (ROOT / "frontend/static/js/markdown.js").read_text(encoding="utf-8")

    assert '.replaceAll("<", "&lt;")' in source
    assert "innerHTML = source" not in source


def test_accessibility_and_reduced_motion_contracts() -> None:
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")

    assert 'class="skip-link"' in html
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css


def test_job_errors_use_bounded_chinese_messages() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    api = (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")

    assert all(code in app for code in (
        "sample_json_incomplete", "sample_html_rejected", "sample_output_invalid", "sample_package_invalid"
    ))
    assert "userErrorMessage(job.error" in app
    assert "job.error?.message" not in app
    assert "normalized.length > 96" in app
    assert "overflow-wrap:anywhere" in css
    assert "error.code = payload?.error?.code" in api
