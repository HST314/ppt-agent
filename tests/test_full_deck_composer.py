from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from agent_core.full_deck_batching import compose_partial_full_deck_preview
from agent_core.full_deck_composer import (
    COMPOSER_VERSION,
    PAGE_CONTENT_GRAPH_VERSION,
    ComposerPage,
    ComposerSource,
    FullDeckComposerInput,
    compose_full_deck,
    normalized_page_content_graph,
)
from agent_core.models import (
    FullDeckGenerationContentRef,
    FullDeckGenerationPage,
    HtmlPptPackage,
    SampleRevision,
    TaskCard,
)
from storage.project_store import ProjectStore


ROOT = Path(__file__).parents[1]


def _segment_package(
    segment: str,
    slide_numbers: tuple[int, ...],
    color: str,
) -> HtmlPptPackage:
    slides = [
        {
            "slide_id": f"{segment}-{number}",
            "title": f"第 {number} 页",
            "source_slide_number": number,
        }
        for number in slide_numbers
    ]
    sections = "".join(
        f'<section class="slide" data-slide-id="{item["slide_id"]}">'
        f'<h1>{item["title"]}</h1><img src="assets/shared.svg" alt="{segment}"></section>'
        for item in slides
    )
    index = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="assets/deck.css"></head><body>'
        f'{sections}<script src="assets/deck.js"></script></body></html>'
    )
    return HtmlPptPackage.model_validate({
        "title": f"页段 {segment}",
        "slide_count": len(slides),
        "slides": slides,
        "files": [
            {"path": "index.html", "content": index},
            {
                "path": "assets/deck.css",
                "content": f".slide{{width:100vw;height:100vh;color:{color}}}",
            },
            {
                "path": "assets/deck.js",
                "content": "addEventListener('keydown',()=>{});",
            },
            {
                "path": "assets/shared.svg",
                "content": f'<svg xmlns="http://www.w3.org/2000/svg"><text>{segment}</text></svg>',
            },
        ],
    })


def _composition_input() -> FullDeckComposerInput:
    first = _segment_package("alpha", (1, 2), "#17324d")
    second = _segment_package("beta", (3, 4), "#9b2c2c")
    return FullDeckComposerInput(
        title="确定性全稿验证",
        sources=[
            ComposerSource(source_id="segment_alpha", package=first),
            ComposerSource(source_id="segment_beta", package=second),
        ],
        pages=[
            ComposerPage(
                slide_id=f"slide-{number}",
                title=f"第 {number} 页",
                source_slide_number=number,
                source_id="segment_alpha" if number < 3 else "segment_beta",
                source_slide_id=f"{'alpha' if number < 3 else 'beta'}-{number}",
            )
            for number in range(1, 5)
        ],
    )


def test_composer_is_deterministic_namespaces_assets_and_traces_each_page() -> None:
    spec = _composition_input()

    first = compose_full_deck(spec)
    second = compose_full_deck(spec)

    assert first.composer_version == COMPOSER_VERSION
    assert first.manifest.page_content_graph_version == PAGE_CONTENT_GRAPH_VERSION
    assert first.package.package_hash == second.package.package_hash
    assert first.package.model_dump() == second.package.model_dump()
    assert [slide.slide_id for slide in first.package.slides] == [
        "slide-1", "slide-2", "slide-3", "slide-4",
    ]

    source_namespaces = [source.namespace for source in first.manifest.sources]
    assert len(source_namespaces) == len(set(source_namespaces)) == 2
    css_resources = [
        resource
        for source in first.manifest.sources
        for resource in source.resources
        if resource.source_path == "assets/deck.css"
    ]
    assert len({resource.output_path for resource in css_resources}) == 2
    files = {item.path: item.content_bytes() for item in first.package.files}
    assert {files[item.output_path] for item in css_resources} == {
        b".slide{width:100vw;height:100vh;color:#17324d}",
        b".slide{width:100vw;height:100vh;color:#9b2c2c}",
    }

    package_by_source = {source.source_id: source.package for source in spec.sources}
    assert all(
        slide.source_slide_content_hash
        == normalized_page_content_graph(
            package_by_source[slide.source_id],
            slide.source_slide_id,
        ).content_hash
        for slide in first.manifest.slides
    )
    # Composed pages are self-contained (package-local refs inlined), so the
    # composed-bytes graph differs from the pre-transform source graph.
    assert all(
        slide.composed_slide_content_hash != slide.source_slide_content_hash
        for slide in first.manifest.slides
    )
    manifest_bytes = files["composition_manifest.json"]
    assert first.manifest.input_hash.encode("ascii") in manifest_bytes
    outer = files["index.html"].decode("utf-8")
    assert [outer.index(f'data-slide-id="slide-{number}"') for number in range(1, 5)] == sorted(
        outer.index(f'data-slide-id="slide-{number}"') for number in range(1, 5)
    )
    assert "ArrowRight" in outer
    assert 'sandbox="allow-scripts"' in outer
    assert "ppt-agent:navigate" in outer
    assert "ppt-agent:slidechange" in outer
    assert "event.source!==parent" in outer
    assert "slides.findIndex" in outer
    assert "min-width:44px" in outer
    assert 'aria-live="polite"' in outer


def test_content_graph_is_independent_of_source_file_order() -> None:
    """Graph hashes must not depend on how a package ordered its file list."""

    def ordered_package(reversed_files: bool) -> HtmlPptPackage:
        base = _segment_package("gamma", (1, 2), "#17324d")
        files = list(base.files)
        if reversed_files:
            files.reverse()
        return HtmlPptPackage.model_validate({
            **base.model_dump(mode="json"),
            "files": [item.model_dump(mode="json") for item in files],
        })

    forward = ordered_package(False)
    backward = ordered_package(True)
    assert [item.path for item in backward.files] == [
        "assets/shared.svg", "assets/deck.js", "assets/deck.css", "index.html",
    ]

    graph_forward = normalized_page_content_graph(forward, "gamma-1")
    graph_backward = normalized_page_content_graph(backward, "gamma-1")
    assert graph_forward.content_hash == graph_backward.content_hash

    spec = FullDeckComposerInput(
        title="文件顺序无关验证",
        sources=[ComposerSource(source_id="segment_gamma", package=backward)],
        pages=[
            ComposerPage(
                slide_id=f"slide-{number}",
                title=f"第 {number} 页",
                source_slide_number=number,
                source_id="segment_gamma",
                source_slide_id=f"gamma-{number}",
            )
            for number in (1, 2)
        ],
    )
    composition = compose_full_deck(spec)
    for number, slide in enumerate(composition.manifest.slides, start=1):
        expected = normalized_page_content_graph(backward, f"gamma-{number}")
        assert slide.source_slide_content_hash == expected.content_hash
    for source in composition.manifest.sources:
        paths = [resource.source_path for resource in source.resources]
        assert paths == sorted(paths)


def test_page_content_graph_handles_current_html_ppt_skill_template() -> None:
    template = (
        ROOT / "skills/guizang-ppt-skill/assets/template-swiss.html"
    ).read_text(encoding="utf-8")
    motion = (
        ROOT / "skills/guizang-ppt-skill/assets/motion.min.js"
    ).read_text(encoding="utf-8")
    package = HtmlPptPackage.model_validate({
        "title": "Skill 模板验证",
        "slide_count": 2,
        "slides": [
            {"slide_id": "cover", "title": "封面", "source_slide_number": 1},
            {"slide_id": "closing", "title": "封底", "source_slide_number": 2},
        ],
        "files": [
            {"path": "index.html", "content": template},
            {"path": "assets/motion.min.js", "content": motion},
        ],
    })

    first = normalized_page_content_graph(package, "cover")
    second = normalized_page_content_graph(package, "cover")
    closing = normalized_page_content_graph(package, "closing")

    assert first == second
    assert first.graph_version == PAGE_CONTENT_GRAPH_VERSION
    assert first.content_hash != closing.content_hash
    assert first.resources[0].path == "assets/motion.min.js"


def test_composed_package_uses_safe_preview_and_unzips_as_offline_deck(
    tmp_path: Path,
    monkeypatch,
    mock_runtime,
) -> None:
    composition = compose_full_deck(_composition_input())
    project_root = tmp_path / "projects"
    project_id = "composer-proof"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    store = ProjectStore(project_root, project_id)
    manifest = store.create(
        TaskCard(title="Composer 验证", objective="验证预览和离线导出").model_dump(),
        mock_runtime.snapshot(),
    )
    sample = SampleRevision.create_package(
        composition.package,
        revision=1,
        parent=None,
        feedback=None,
        provenance={"composer_version": composition.composer_version},
    )

    def publish(value: dict) -> dict:
        value["samples"].append(sample.model_dump())
        value["current_sample_revision_hash"] = sample.revision_hash
        value.update(state="ppt_sample", phase="waiting_human_approval")
        return value

    store.update(
        publish,
        "sample_generated",
        {"revision_hash": sample.revision_hash},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    client = TestClient(main_front.app)
    preview_base = f"/api/projects/{project_id}/samples/revisions/{sample.revision_hash}/preview"
    preview = client.get(f"{preview_base}/index.html")

    assert preview.status_code == 200
    assert "frame-src 'self'" in preview.headers["content-security-policy"]
    iframe_paths = re.findall(r'<iframe src="([^"]+)"', preview.text)
    assert len(iframe_paths) == 4
    for iframe_path in iframe_paths:
        page = client.get(f"{preview_base}/{iframe_path}")
        assert page.status_code == 200
        assert page.text.count('class="slide"') == 1

    exported = client.get(
        f"/api/projects/{project_id}/samples/revisions/{sample.revision_hash}/export"
    )
    assert exported.status_code == 200
    offline_root = tmp_path / "offline"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        archive.extractall(offline_root)

    offline_index = (offline_root / "index.html").read_text(encoding="utf-8")
    offline_iframes = re.findall(r'<iframe src="([^"]+)"', offline_index)
    assert offline_iframes == iframe_paths
    for iframe_path in offline_iframes:
        page_path = offline_root / iframe_path
        assert page_path.is_file()
        page_html = page_path.read_text(encoding="utf-8")
        # Pages are self-contained: package-local resources are inlined, so the
        # exported deck renders from file:// inside its sandboxed shell.
        assert 'src="data:image/svg+xml;base64,' in page_html
        assert not re.findall(r'(?:src|href)="assets/[^"]+"', page_html)


def test_single_slide_document_strips_source_pager_and_inlines_resources() -> None:
    index = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="assets/deck.css" media="screen"></head><body>'
        '<main>'
        '<section class="slide" data-slide-id="one">'
        '<div class="chrome"><div class="left"><span>第一幕</span></div>'
        '<div class="right"><span>01 / 02</span></div></div>'
        '<h1>第一页</h1><img src="assets/pic.png" alt="图">'
        '<div style="background:url(assets/pic.png)"></div>'
        '</section>'
        '<section class="slide" data-slide-id="two"><h1>第二页</h1></section>'
        '</main>'
        '<div aria-hidden="false" class="chrome"><div class="bar">'
        '<button class="navbtn" id="prev">‹</button>'
        '<button class="navbtn" id="next">›</button>'
        '<div class="count"><span id="pagenow">1</span> / 2 · 原稿第 1 – 2 页</div>'
        '</div></div>'
        '<script src="assets/deck.js"></script>'
        '<script>var pagenow=document.getElementById("pagenow");</script>'
        '<script>window.pageOwnScript=true;</script>'
        '</body></html>'
    )
    package = HtmlPptPackage.model_validate({
        "title": "翻页条剥离验证",
        "slide_count": 2,
        "slides": [
            {"slide_id": "one", "title": "第一页", "source_slide_number": 1},
            {"slide_id": "two", "title": "第二页", "source_slide_number": 2},
        ],
        "files": [
            {"path": "index.html", "content": index},
            {"path": "assets/deck.css", "content": ".slide{color:#111}"},
            {"path": "assets/deck.js", "content": "addEventListener('keydown',()=>{});"},
            {"path": "assets/pic.png", "content": "aGVsbG8=", "encoding": "base64"},
        ],
    })
    composition = compose_full_deck(FullDeckComposerInput(
        title="翻页条剥离验证",
        sources=[ComposerSource(source_id="segment", package=package)],
        pages=[
            ComposerPage(
                slide_id="slide-1",
                title="第一页",
                source_slide_number=1,
                source_id="segment",
                source_slide_id="one",
            ),
        ],
    ))
    files = {item.path: item.content_bytes() for item in composition.package.files}
    page_path = composition.manifest.slides[0].document_path
    page = files[page_path].decode("utf-8")

    # The source deck pager and its script are gone; page-level chrome stays.
    assert "pagenow" not in page
    assert "navbtn" not in page
    assert "原稿第" not in page
    assert "第一幕" in page
    assert "01 / 02" in page
    assert "window.pageOwnScript" in page

    # Package-local resources are inlined; nothing package-relative remains.
    assert 'src="data:image/png;base64,aGVsbG8="' in page
    assert 'url("data:image/png;base64,aGVsbG8=")' in page
    assert "url(assets/pic.png)" not in page
    assert ".slide{color:#111}" in page
    assert "addEventListener('keydown',()=>{});" in page
    assert not re.findall(r'(?:src|href)="assets/[^"]+"', page)


def test_partial_preview_keeps_refs_recorded_before_self_contained_composition() -> None:
    """Content refs hashed from the untransformed slide stay valid.

    Durable content refs predate self-contained composition: they hash the
    slide document before pager stripping and resource inlining. Partial
    previews must keep accepting those refs while still emitting transformed
    page bytes (pager stripped, local resources inlined).
    """

    index = (
        '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        '<main>'
        '<section class="slide" data-slide-id="one">'
        '<h1>第一页</h1><img src="assets/pic.png" alt="图">'
        '</section>'
        '</main>'
        '<div class="chrome"><div class="bar">'
        '<button class="navbtn" id="prev">‹</button>'
        '<div class="count"><span id="pagenow">1</span> / 1 · 原稿第 1 – 1 页</div>'
        '</div></div>'
        '<script>var pagenow=document.getElementById("pagenow");</script>'
        '</body></html>'
    )
    package = HtmlPptPackage.model_validate({
        "title": "旧哈希兼容验证",
        "slide_count": 1,
        "slides": [
            {"slide_id": "one", "title": "第一页", "source_slide_number": 1},
        ],
        "files": [
            {"path": "index.html", "content": index},
            {"path": "assets/pic.png", "content": "aGVsbG8=", "encoding": "base64"},
        ],
    })
    session_id = "fullsession_" + "0" * 32
    sample_revision = "sha256:" + "1" * 64
    legacy_ref = normalized_page_content_graph(package, "one").content_hash

    preview = compose_partial_full_deck_preview(
        session_id=session_id,
        batch_index=1,
        title="旧哈希兼容验证",
        pages=[
            FullDeckGenerationPage(
                session_id=session_id,
                position=0,
                slot_id="slot_" + "0" * 24,
                source_slide_number=1,
                title="第一页",
                generation_status="sample_ready",
                source_type="approved_sample",
                content_ref=FullDeckGenerationContentRef(
                    revision_hash=sample_revision,
                    package_hash=package.package_hash,
                    slide_id="one",
                    slide_content_hash=legacy_ref,
                ),
            )
        ],
        source_packages={sample_revision: package},
    )

    slide = preview.composition_manifest["slides"][0]
    assert slide["source_slide_content_hash"] == legacy_ref
    # The composed page bytes really are transformed, so their graph differs.
    assert slide["composed_slide_content_hash"] != legacy_ref
    page = next(
        item.content_bytes().decode("utf-8")
        for item in preview.files
        if item.path == slide["document_path"]
    )
    assert "pagenow" not in page
    assert 'src="data:image/png;base64,aGVsbG8="' in page
