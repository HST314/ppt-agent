import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest


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


def test_sample_preview_is_sandboxed_and_never_inserted_into_parent_dom() -> None:
    app = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")

    assert 'id="sample-preview-frame" sandbox="" referrerpolicy="no-referrer"' in samples
    assert "Content-Security-Policy" in samples
    assert "frame.srcdoc = isolatedSampleHtml(page.html)" in samples
    assert "canvas.clientWidth / 1280" in samples
    assert "new ResizeObserver(scaleFrame)" in samples
    assert "innerHTML = page.html" not in app + samples


def test_sample_preview_csp_precedes_untrusted_comment_and_document_markup() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend security regression")
    samples = (ROOT / "frontend/static/js/samples.js").read_text(encoding="utf-8")
    module_url = "data:text/javascript;base64," + base64.b64encode(samples.encode()).decode()
    payload = '<!-- <head> --><html><head></head><body><p>sample</p></body></html>'
    script = (
        f"const module = await import({json.dumps(module_url)});"
        f"console.log(JSON.stringify(module.isolatedSampleHtml({json.dumps(payload)})));"
    )

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    isolated = json.loads(result.stdout)

    assert isolated.startswith("<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\"")
    assert isolated.index("Content-Security-Policy") < isolated.index("<!-- <head> -->")
    assert isolated.count("Content-Security-Policy") == 1


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
