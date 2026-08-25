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
    assert '${completion}${preview}${feedback}${historyMarkup}' in samples
    assert "不会创建新修订" in samples
    assert 'data-action="branch_sample_revision"' in samples
    assert 'data-sample-revision="${escapeHtml(revision.revision_hash)}" aria-pressed="${selected}"' in samples
    assert "restoreSample:" in (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")


def test_sample_preview_is_sandboxed_and_never_inserted_into_parent_dom() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")
    server = (ROOT / "main_front.py").read_text(encoding="utf-8")

    assert 'id="sample-preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer"' in samples
    assert '"sandbox allow-scripts; default-src \'none\'' in server
    assert 'frame.src = sample.preview_url' in samples
    assert "script-src 'self' 'unsafe-inline' data: blob:" in server
    assert "connect-src 'none'" in server
    assert "frame-src 'none'" in server
    assert "innerHTML = page.html" not in app + samples


def test_status_console_supports_live_filter_search_expand_and_pinned_errors() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")
    api = (ROOT / "frontend/static/js/api.js").read_text(encoding="utf-8")

    assert "api.activity" in app
    assert 'id="status-search"' in app
    assert "data-status-filter" in app
    assert "data-event-expand" in app
    assert "data-copy-event" in app
    assert "固定可见" in app
    assert "window.setInterval" in app
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
