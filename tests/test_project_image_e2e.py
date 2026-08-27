"""Phase 5 of the local image reference plan: mock-provider end-to-end.

T5.1 — the whole chain against the deterministic mock provider with project
image materials present:

    global source images → sync_project_images (Phase 1 layer)
    → outline (materials injected, mock text) → edited to carry 配图 plans
    → sample (obedient gateway copies exactly the planned images; the package
       gains img/ and every HTML reference closes)
    → batched full-deck session via run_full_deck_generation_session
    → composition/publish
    → preview + ZIP-export smoke over the HTTP routes.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.models import TaskCard
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_images import sync_project_images
from storage.project_store import ProjectStore

COVER_DESCRIPTION = "封面主视觉：城市天际线剪影，深蓝基调。"
ROADMAP_DESCRIPTION = "三阶段路线图：现状、试点、推广。"
TEAM_DESCRIPTION = "团队合影：六位核心成员站在公司前台。"

GLOBAL_IMAGES: dict[str, bytes | str] = {
    "封面图.png": b"\x89PNG-fake-cover-bytes",
    "封面图.md": COVER_DESCRIPTION,
    "路线图.jpg": b"\xff\xd8-fake-roadmap-bytes",
    "路线图.md": ROADMAP_DESCRIPTION,
    "团队照.png": b"\x89PNG-fake-team-bytes",
    "团队照.md": TEAM_DESCRIPTION,
}

# 8 pages: the sample covers pages 1–2, so the full deck regenerates 3–8 as
# two balanced batches [3, 4, 5] and [6, 7, 8]. Planned images: pages 1, 3,
# 7 use bare references; pages 2, 5, 8 use percent-encoded references; pages
# 4 and 6 stay unplanned.
EIGHT_PAGE_OUTLINE = """# 逐页大纲

## 第 1 页｜封面
配图：封面图.png
## 第 2 页｜结论先行
配图：路线图.jpg
## 第 3 页｜背景与机会
配图：团队照.png
## 第 4 页｜数据证据
## 第 5 页｜行动方案
配图：团队照.png
## 第 6 页｜里程碑
## 第 7 页｜风险与对策
配图：封面图.png
## 第 8 页｜结语
配图：路线图.jpg
"""
PLANS = {
    1: ["封面图.png"],
    2: ["路线图.jpg"],
    3: ["团队照.png"],
    5: ["团队照.png"],
    7: ["封面图.png"],
    8: ["路线图.jpg"],
}
ENCODED_REFERENCE_PAGES = {2, 5, 8}
# Sample pages 1–2; batched segments regenerate the rest.
EXPECTED_BATCHES = [[3, 4, 5], [6, 7, 8]]


def _install_global_images(tmp_path: Path) -> Path:
    source = tmp_path / "global-images"
    source.mkdir()
    for name, payload in GLOBAL_IMAGES.items():
        if isinstance(payload, bytes):
            (source / name).write_bytes(payload)
        else:
            (source / name).write_text(payload, encoding="utf-8")
    # One dirty orphan image without a description must not enter the sync.
    (source / "孤儿图.png").write_bytes(b"\x89PNG-orphan")
    return source


def _capture_gateway(workflow: Workflow, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Wrap the mock provider, recording every call and making the sample and
    full-deck states copy exactly the planned images and reference them."""

    captured: dict[str, list] = {"calls": []}
    original = workflow.gateway.generate

    def generate(state: str, prompt: str, **kwargs):
        captured["calls"].append(
            {"state": state, "prompt": prompt, "kwargs": kwargs}
        )
        if state == "ppt_sample":
            return _obedient_sample(prompt, **kwargs)
        if state == "ppt_full":
            return _obedient_segment(prompt, **kwargs)
        return original(state, prompt, **kwargs)

    monkeypatch.setattr(workflow.gateway, "generate", generate)
    return captured


def _render_slides(target_numbers: list[int], slide_id_prefix: str) -> tuple[list, list]:
    slides: list[dict[str, Any]] = []
    sections: list[str] = []
    for number in target_numbers:
        images = []
        for name in PLANS.get(number, []):
            reference = (
                quote(f"img/{name}")
                if number in ENCODED_REFERENCE_PAGES
                else f"img/{name}"
            )
            images.append(f'<img src="{reference}" alt="{name}">')
        slide_id = f"{slide_id_prefix}-{number}"
        slides.append({
            "slide_id": slide_id,
            "title": f"第 {number} 页",
            "source_slide_number": number,
        })
        sections.append(
            f'<section class="slide" data-slide-id="{slide_id}">'
            f'<h1>第 {number} 页</h1>{"".join(images)}</section>'
        )
    return slides, sections


def _obedient_sample(prompt: str, **kwargs):
    source_numbers = [1, 2]
    draft = kwargs["package_draft"]
    slides, sections = _render_slides(source_numbers, "sample")
    for number in source_numbers:
        for name in PLANS.get(number, []):
            draft.copy_project_image(f"images/{name}", f"img/{name}")
    payload = {
        "entrypoint": "index.html",
        "title": "样品",
        "slide_count": len(slides),
        "slides": slides,
        "files": [
            {
                "path": "index.html",
                "content": f'<!doctype html><html><body>{"".join(sections)}</body></html>',
                "encoding": "utf-8",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False), [{
        "type": "model_call",
        "provider": "test",
        "model": "obedient-sample",
        "usage": {},
    }]


def _obedient_segment(prompt: str, **kwargs):
    target_match = re.search(
        r"FULL_DECK_TARGET_SLIDE_NUMBERS: (\[[^\]]*\])", prompt
    )
    target_numbers = json.loads(target_match.group(1))
    draft = kwargs["package_draft"]
    slides, sections = _render_slides(target_numbers, "full")
    for number in target_numbers:
        for name in PLANS.get(number, []):
            draft.copy_project_image(f"images/{name}", f"img/{name}")
    payload = {
        "source_slide_numbers": target_numbers,
        "entrypoint": "index.html",
        "title": f"第 {target_numbers[0]}–{target_numbers[-1]} 页",
        "slide_count": len(slides),
        "slides": slides,
        "files": [
            {
                "path": "index.html",
                "content": f'<!doctype html><html><body>{"".join(sections)}</body></html>',
                "encoding": "utf-8",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False), [{
        "type": "model_call",
        "provider": "test",
        "model": "obedient-full",
        "usage": {},
    }]


def _start_workflow(
    tmp_path: Path, runtime: ManagedRuntime, project_id: str
) -> Workflow:
    store = ProjectStore(tmp_path / "projects", project_id)
    task = TaskCard(title="八页完整演示", objective="验证图片端到端链路")
    store.create(task.model_dump(), runtime.snapshot())
    return Workflow(store, runtime)


def _finish_clarifications(workflow: Workflow, manifest: dict) -> dict:
    while (
        manifest["state"] == "intake_clarify"
        and manifest["phase"] == "ready_for_clarification"
    ):
        manifest = workflow.start_clarification(manifest["checkpoint_id"])
    return manifest


def _drive_to_outline(workflow: Workflow) -> dict:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = _finish_clarifications(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "management" for question in card["questions"]},
        ),
    )
    manifest = workflow.generate_document(
        "narrative_structure", manifest["checkpoint_id"]
    )
    document = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], document["revision_hash"]
    )
    return workflow.generate_document("slide_outline", manifest["checkpoint_id"])


def _all_references_close(files: dict[str, bytes]) -> None:
    for path, content in files.items():
        if not path.endswith(".html"):
            continue
        for reference in re.findall(r'src="([^"]+)"', content.decode("utf-8")):
            # Composed pages inline package-local resources as data URIs;
            # they are self-contained and need no file to close against.
            if reference.startswith("data:"):
                continue
            target = posixpath.normpath(
                str(PurePosixPath(path).parent / PurePosixPath(unquote(reference)))
            )
            assert target in files, (path, reference)


def test_mock_provider_end_to_end_with_images(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "phase5-images-e2e"
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(
        main_front, "jobs", main_front.JobRegistry(project_root / ".jobs")
    )
    monkeypatch.setattr(main_front, "runtime", mock_runtime)

    workflow = _start_workflow(tmp_path, mock_runtime, project_id)

    # Phase 1 layer: the global snapshot (with one dirty orphan) syncs into
    # the project before any generation runs.
    source_root = _install_global_images(tmp_path)
    report = sync_project_images(source_root, workflow.store.root)
    project_images = workflow.store.root / "images"
    synced = sorted(item.name for item in project_images.iterdir())
    assert synced == sorted(
        ["封面图.png", "封面图.md", "路线图.jpg", "路线图.md", "团队照.png", "团队照.md"]
    )
    assert report.image_count == 3
    assert report.file_count == 6
    assert any("孤儿图" in warning for warning in report.warnings)

    captured = _capture_gateway(workflow, monkeypatch)

    # Outline stage: the mock produces its own text, but the prompt carries
    # the manifest and every description in full.
    manifest = _drive_to_outline(workflow)
    outline_call = next(
        call for call in captured["calls"] if call["state"] == "slide_outline"
    )
    assert outline_call["state"] == "slide_outline"
    for name in ("封面图.png", "路线图.jpg", "团队照.png"):
        assert f"images/{name}" in outline_call["prompt"]
    assert COVER_DESCRIPTION in outline_call["prompt"]
    assert ROADMAP_DESCRIPTION in outline_call["prompt"]
    assert TEAM_DESCRIPTION in outline_call["prompt"]

    # The outline is edited into an 8-page plan carrying 配图 lines, then
    # approved — the "大纲（含配图）" input of the end-to-end chain.
    manifest = workflow.edit_document(
        "slide_outline", manifest["checkpoint_id"], EIGHT_PAGE_OUTLINE
    )
    document = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], document["revision_hash"]
    )

    # Sample stage: exactly the planned images enter img/, both reference
    # forms close, and the draft carried the project images root.
    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    sample_call = next(
        call for call in captured["calls"] if call["state"] == "ppt_sample"
    )
    images_root = project_images.resolve()
    assert sample_call["kwargs"]["images_root"] == images_root
    assert sample_call["kwargs"]["package_draft"].images_root == images_root
    assert "样品配图纪律" in sample_call["prompt"]

    sample = manifest["samples"][-1]
    sample_files = {
        item["path"]: item for item in sample["package"]["files"]
    }
    assert {path for path in sample_files if path.startswith("img/")} == {
        "img/封面图.png",
        "img/路线图.jpg",
    }
    assert sample_files["index.html"]["content"].count("<img ") == 2
    assert 'src="img/封面图.png"' in sample_files["index.html"]["content"]
    assert f'src="{quote("img/路线图.jpg")}"' in sample_files["index.html"]["content"]

    # Batched full-deck session: two segments, then one composed publish.
    entered = workflow.enter_full_deck(
        manifest["checkpoint_id"],
        sample["revision_hash"],
    )
    session = workflow.start_full_deck_generation_session(
        entered["checkpoint_id"]
    )
    assert [
        batch["source_slide_numbers"] for batch in session["batches"]
    ] == EXPECTED_BATCHES
    completed = workflow.run_full_deck_generation_session(session["session_id"])
    assert completed["status"] == "completed"

    segment_calls = [
        call for call in captured["calls"] if call["state"] == "ppt_full"
    ]
    dispatched = [
        json.loads(
            re.search(
                r"FULL_DECK_TARGET_SLIDE_NUMBERS: (\[[^\]]*\])", call["prompt"]
            ).group(1)
        )
        for call in segment_calls
    ]
    assert dispatched == EXPECTED_BATCHES
    for call in segment_calls:
        assert call["kwargs"]["images_root"] == images_root
        assert call["kwargs"]["package_draft"].images_root == images_root
        # The manifest is always injected in full.
        for name in ("封面图.png", "路线图.jpg", "团队照.png"):
            assert f"images/{name}" in call["prompt"]
    # Batch [3, 4, 5] plans 团队照 only; batch [6, 7, 8] plans 封面图+路线图.
    assert TEAM_DESCRIPTION in segment_calls[0]["prompt"]
    assert COVER_DESCRIPTION not in segment_calls[0]["prompt"]
    assert ROADMAP_DESCRIPTION not in segment_calls[0]["prompt"]
    assert COVER_DESCRIPTION in segment_calls[1]["prompt"]
    assert ROADMAP_DESCRIPTION in segment_calls[1]["prompt"]
    assert TEAM_DESCRIPTION not in segment_calls[1]["prompt"]

    # Composed package: img/ corresponds strictly to the outline plans of
    # every generated page (sample pages included via the sample source).
    manifest = workflow.store.read()
    revision_hash = completed["published_revision_hash"]
    assert (
        manifest["full_deck"]["current_revision_hash"] == revision_hash
    )
    stored_files = {
        item["path"]: item["content"]
        for item in workflow.store.full_deck_package_contents(revision_hash)
    }
    img_by_source: dict[str, set[str]] = {}
    for path in stored_files:
        match = re.match(r"(sources/[^/]+)/(.+)", path)
        if match and match.group(2).startswith("img/"):
            img_by_source.setdefault(match.group(1), set()).add(
                match.group(2)[len("img/"):]
            )
    all_img_names = {name for names in img_by_source.values() for name in names}
    assert all_img_names == {"封面图.png", "路线图.jpg", "团队照.png"}
    # The batch [3, 4, 5] segment carries exactly 团队照.png.
    team_only = [
        source for source, names in img_by_source.items() if names == {"团队照.png"}
    ]
    assert len(team_only) == 1
    # The sample and the [6, 7, 8] segment each carry 封面图+路线图.
    cover_roadmap = [
        source
        for source, names in img_by_source.items()
        if names == {"封面图.png", "路线图.jpg"}
    ]
    assert len(cover_roadmap) == 2

    # Every composed image is byte-identical to the synced source material.
    for path, content in stored_files.items():
        if re.match(r"sources/[^/]+/img/", path):
            assert content == GLOBAL_IMAGES[path.rsplit("img/", 1)[1]]

    # Every src reference in every composed HTML document closes, in both
    # the bare and the percent-encoded form.
    _all_references_close(stored_files)

    # Preview smoke over the HTTP routes: the outer document plus every
    # composed image (unicode logical paths) serve with CSP intact.
    client = TestClient(main_front.app)
    preview_base = (
        f"/api/projects/{project_id}/full-deck/revisions/{revision_hash}/preview"
    )
    index_response = client.get(f"{preview_base}/index.html")
    assert index_response.status_code == 200
    assert "Content-Security-Policy" in index_response.headers
    for logical_path in stored_files:
        if not re.match(r"sources/[^/]+/img/", logical_path):
            continue
        response = client.get(f"{preview_base}/{quote(logical_path)}")
        assert response.status_code == 200, logical_path
        assert response.content == GLOBAL_IMAGES[
            logical_path.rsplit("img/", 1)[1]
        ]
        assert response.headers["content-type"].startswith("image/")

    # Export smoke: unicode logical names survive the ZIP boundary.
    export_response = client.get(
        f"/api/projects/{project_id}/full-deck/revisions/{revision_hash}/export"
    )
    assert export_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export_response.content)) as archive:
        names = set(archive.namelist())
        assert any(name.endswith("img/团队照.png") for name in names)
        assert any(name.endswith("img/封面图.png") for name in names)
        assert any(name.endswith("img/路线图.jpg") for name in names)
        for name in names:
            if "img/" in name:
                assert archive.read(name) == GLOBAL_IMAGES[
                    name.rsplit("img/", 1)[1]
                ]
