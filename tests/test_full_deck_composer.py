from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from agent_core.full_deck_composer import (
    COMPOSER_VERSION,
    PAGE_CONTENT_GRAPH_VERSION,
    ComposerPage,
    ComposerSource,
    FullDeckComposerInput,
    compose_full_deck,
    normalized_page_content_graph,
)
from agent_core.models import HtmlPptPackage, SampleRevision, TaskCard
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

    assert all(
        slide.source_slide_content_hash == slide.composed_slide_content_hash
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
        for asset_path in re.findall(r'(?:src|href)="(assets/[^"]+)"', page_html):
            assert (page_path.parent / asset_path).is_file()
