"""Phase 4 of the local image reference plan: full-deck integration.

Covers T4.1 (the ppt_full_images.md discipline fragment), T4.2/T4.3/T4.4
(material injection into the batched, monolithic, revision, and sample-resume
calls with images_root plumbed through draft and gateway), the D-11 fallback
direction (empty intersection with the target pages means FULL injection,
never none), the empty-materials red line (prompts and draft/gateway calls
stay byte-for-byte identical to the pre-feature formulas), and T4.5 (offline
validation and Composer closure for both bare and percent-encoded Chinese
image references).
"""

from __future__ import annotations

import base64
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
from agent_core.full_deck_batch_generation import generate_full_deck_batch
from agent_core.full_deck_composer import (
    ComposerPage,
    ComposerSource,
    FullDeckComposerInput,
    compose_full_deck,
)
from agent_core.full_deck_generation import (
    FullDeckGenerationError,
    _validate_offline_package,
)
from agent_core.full_deck_reference_context import full_deck_package_reference_tool
from agent_core.full_deck_revision import (
    _reference_summary,
    create_full_deck_revision,
    full_deck_package_model,
)
from agent_core.full_deck_session import start_full_deck_generation_session
from agent_core.models import (
    DocumentRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
)
from agent_core.workflow import Workflow
from agent_core.workflow_support import (
    full_deck_image_description_paths,
    outline_slide_catalog,
)
from configs.runtime import ManagedRuntime
from model_router.client import MaxToolRoundsExceeded
from storage.project_store import ProjectStore


COVER_DESCRIPTION = "封面主视觉：城市天际线剪影，深蓝基调。"
ROADMAP_DESCRIPTION = "三阶段路线图：现状、试点、推广。"
TEAM_DESCRIPTION = "团队合影：六位核心成员站在公司前台。"

# Page 1 plans the cover image, page 2 (sample covered) plans the roadmap,
# page 4 has no plan at all. Pending pages are 1 and 4, so:
# - a [1] call filters to the cover description only;
# - a [4] call has an empty intersection and falls back to FULL injection.
OUTLINE_WITH_PLANS = """# 逐页大纲

## 第 1 页｜开场
配图：封面图.png
## 第 2 页｜关键判断
配图：路线图.jpg
## 第 3 页｜数据证据
## 第 4 页｜行动计划
"""


def install_project_images(workflow: Workflow) -> Path:
    images_dir = workflow.store.root / "images"
    images_dir.mkdir()
    (images_dir / "封面图.png").write_bytes(b"\x89PNG-fake-cover-bytes")
    (images_dir / "封面图.md").write_text(COVER_DESCRIPTION, encoding="utf-8")
    (images_dir / "路线图.jpg").write_bytes(b"\xff\xd8-fake-roadmap-bytes")
    (images_dir / "路线图.md").write_text(ROADMAP_DESCRIPTION, encoding="utf-8")
    (images_dir / "团队照.png").write_bytes(b"\x89PNG-fake-team-bytes")
    (images_dir / "团队照.md").write_text(TEAM_DESCRIPTION, encoding="utf-8")
    return images_dir


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="中段样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-2", title="关键判断", source_slide_number=2),
            PackageSlide(slide_id="sample-3", title="数据证据", source_slide_number=3),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    'data-slide-id="sample-2"><h1>关键判断</h1></section>'
                    '<section class="slide" data-slide-id="sample-3">'
                    '<h1>数据证据</h1></section></body></html>'
                ),
            ),
        ],
    )


def _entered_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
    project_id: str,
    *,
    outline_markdown: str = OUTLINE_WITH_PLANS,
) -> tuple[Workflow, dict]:
    store = ProjectStore(tmp_path / "projects", project_id)
    manifest = store.create(
        TaskCard(title="四页完整演示", objective="验证全稿图片引用").model_dump(),
        runtime.snapshot(),
    )
    outline = DocumentRevision.create(
        "slide_outline",
        outline_markdown,
        revision=1,
        parent=None,
        created_by="agent",
        provenance={"output_hash": "sha256:" + "1" * 64},
    ).model_copy(update={"status": "approved"})
    sample = SampleRevision.create_package(
        _sample_package(),
        revision=1,
        parent=None,
        feedback=None,
        provenance={
            "model_config_hash": "sha256:" + "2" * 64,
            "runtime_config_hash": "sha256:" + "3" * 64,
            "skills_hash": "sha256:" + "4" * 64,
        },
    )
    manifest = store.update(
        lambda value: value
        | {
            "documents": {
                "narrative_structure": [],
                "slide_outline": [outline.model_dump(mode="json")],
            },
            "samples": [sample.model_dump(mode="json")],
            "current_sample_revision_hash": sample.revision_hash,
            "state": "ppt_sample",
            "phase": "waiting_human_approval",
        },
        "phase4_fixture_ready",
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    workflow = Workflow(store, runtime)
    entered = workflow.enter_full_deck(
        manifest["checkpoint_id"],
        manifest["current_sample_revision_hash"],
    )
    return workflow, entered


def spy_gateway(workflow: Workflow, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record every gateway.generate call and delegate to the real mock."""

    captured: dict[str, list] = {"calls": []}
    original = workflow.gateway.generate

    def generate(state: str, prompt: str, **kwargs):
        captured["calls"].append(
            {"state": state, "prompt": prompt, "kwargs": kwargs}
        )
        return original(state, prompt, **kwargs)

    monkeypatch.setattr(workflow.gateway, "generate", generate)
    return captured


def _plan_pages(manifest: dict) -> list[dict[str, Any]]:
    revisions = manifest.get("full_deck_revisions", [])
    current_hash = manifest["full_deck"]["current_revision_hash"]
    current = next(
        item for item in revisions if item["revision_hash"] == current_hash
    )
    return current["plan"]["pages"]


def _batch_calls(workflow: Workflow, manifest: dict) -> list[dict[str, Any]]:
    """Drive every planned session batch against the (mock) gateway."""

    snapshot = start_full_deck_generation_session(
        workflow,
        manifest["checkpoint_id"],
    )
    plan_pages = _plan_pages(manifest)
    by_slot = {page["slot_id"]: page for page in plan_pages}
    results = []
    for batch_index in sorted(
        {
            page["batch_index"]
            for page in snapshot["pages"]
            if page["batch_index"] is not None
        }
    ):
        batch_pages = [
            page for page in snapshot["pages"] if page["batch_index"] == batch_index
        ]
        results.append(
            generate_full_deck_batch(
                workflow,
                workflow.store.read(),
                session_id=snapshot["session_id"],
                batch_index=batch_index,
                batch_pages=batch_pages,
                directives=[],
                recent_segment_package_ids=[],
            )
        )
    return results


def descriptions_block(prompt: str) -> str:
    match = re.search(
        r"PROJECT_IMAGE_DESCRIPTIONS:\n(.*)$", prompt, re.DOTALL
    )
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# D-11 helper — filtering and the full-injection fallback direction
# ---------------------------------------------------------------------------


def test_description_paths_helper_filters_and_falls_back() -> None:
    manifest = [
        {"image_path": "images/封面图.png", "description_path": "images/封面图.md", "size_bytes": 1},
        {"image_path": "images/路线图.jpg", "description_path": "images/路线图.md", "size_bytes": 1},
        {"image_path": "images/团队照.png", "description_path": "images/团队照.md", "size_bytes": 1},
    ]

    # Empty manifest: nothing to inject.
    assert full_deck_image_description_paths(OUTLINE_WITH_PLANS, [], [1, 4]) == []

    # Outline without any usable plan line: full injection.
    plain = "# 逐页大纲\n\n## 第 1 页｜开场\n## 第 4 页｜行动\n"
    assert full_deck_image_description_paths(plain, manifest, [1]) == [
        "images/封面图.md",
        "images/路线图.md",
        "images/团队照.md",
    ]

    # Plans exist but none intersect the target pages: STILL full injection.
    assert full_deck_image_description_paths(OUTLINE_WITH_PLANS, manifest, [4]) == [
        "images/封面图.md",
        "images/路线图.md",
        "images/团队照.md",
    ]

    # Intersecting target pages inject only their planned descriptions.
    assert full_deck_image_description_paths(OUTLINE_WITH_PLANS, manifest, [1]) == [
        "images/封面图.md",
    ]
    assert full_deck_image_description_paths(OUTLINE_WITH_PLANS, manifest, [1, 2]) == [
        "images/封面图.md",
        "images/路线图.md",
    ]

    # Unknown planned names never inject anything extra.
    unknown_only = "# 逐页大纲\n\n## 第 1 页｜开场\n配图：未登记图.png\n"
    assert full_deck_image_description_paths(unknown_only, manifest, [1]) == [
        "images/封面图.md",
        "images/路线图.md",
        "images/团队照.md",
    ]


def test_description_paths_helper_dedupes_shared_descriptions() -> None:
    # Two suffixes of one stem share a single description file.
    manifest = [
        {"image_path": "images/封面图.png", "description_path": "images/封面图.md", "size_bytes": 1},
        {"image_path": "images/封面图.jpg", "description_path": "images/封面图.md", "size_bytes": 1},
    ]
    markdown = "# 逐页大纲\n\n## 第 1 页｜开场\n配图：封面图.png、封面图.jpg\n"
    assert full_deck_image_description_paths(markdown, manifest, [1]) == [
        "images/封面图.md",
    ]


# ---------------------------------------------------------------------------
# T4.2 — batched full-deck injection + empty-materials red line
# ---------------------------------------------------------------------------


def test_batched_prompt_injects_manifest_and_filtered_descriptions(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _entered_full_deck(
        tmp_path, mock_runtime, "phase4-images-batch"
    )
    images_dir = install_project_images(workflow)
    captured = spy_gateway(workflow, monkeypatch)

    _batch_calls(workflow, workflow.store.read())

    full_calls = [call for call in captured["calls"] if call["state"] == "ppt_full"]
    assert [json.loads(re.search(r"FULL_DECK_TARGET_SLIDE_NUMBERS: (\[[^\]]*\])", call["prompt"]).group(1)) for call in full_calls] == [
        [1],
        [4],
    ]
    for call in full_calls:
        assert "全稿配图纪律" in call["prompt"]
        # The manifest itself is always injected in full.
        assert "images/封面图.png" in call["prompt"]
        assert "images/路线图.jpg" in call["prompt"]
        assert "images/团队照.png" in call["prompt"]
        assert call["kwargs"]["images_root"] == images_dir.resolve()
        assert call["kwargs"]["package_draft"].images_root == images_dir.resolve()

    # Batch [1]: filtered to the page-1 plan only.
    assert COVER_DESCRIPTION in full_calls[0]["prompt"]
    assert ROADMAP_DESCRIPTION not in full_calls[0]["prompt"]
    assert TEAM_DESCRIPTION not in full_calls[0]["prompt"]

    # Batch [4]: no intersection with any plan line → FULL fallback.
    assert COVER_DESCRIPTION in full_calls[1]["prompt"]
    assert ROADMAP_DESCRIPTION in full_calls[1]["prompt"]
    assert TEAM_DESCRIPTION in full_calls[1]["prompt"]


def test_batched_prompt_without_images_keeps_prompt_byte_identical(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _entered_full_deck(
        tmp_path, mock_runtime, "phase4-empty-batch"
    )
    captured = spy_gateway(workflow, monkeypatch)
    manifest = workflow.store.read()

    snapshot = start_full_deck_generation_session(
        workflow,
        manifest["checkpoint_id"],
    )
    batch_pages = [
        page for page in snapshot["pages"] if page["batch_index"] == 1
    ]
    generate_full_deck_batch(
        workflow,
        manifest,
        session_id=snapshot["session_id"],
        batch_index=1,
        batch_pages=batch_pages,
        directives=[],
        recent_segment_package_ids=[],
    )

    call = captured["calls"][0]
    assert call["state"] == "ppt_full"
    assert "PROJECT_IMAGES" not in call["prompt"]
    assert "配图纪律" not in call["prompt"]
    assert "images_root" not in call["kwargs"]
    assert call["kwargs"]["package_draft"].images_root is None

    # Pre-feature formula transcribed from the Phase 4 baseline
    # (main@24e61a5, agent_core/full_deck_batch_generation.py).
    plan_pages = _plan_pages(manifest)
    by_slot = {page["slot_id"]: page for page in plan_pages}
    segment_pages = [by_slot[page["slot_id"]] for page in batch_pages]
    target_numbers = [int(page["source_slide_number"]) for page in batch_pages]
    positions = [int(page["position"]) for page in segment_pages]
    neighbors = {
        "before": plan_pages[min(positions) - 1] if min(positions) > 0 else None,
        "after": (
            plan_pages[max(positions) + 1]
            if max(positions) + 1 < len(plan_pages)
            else None
        ),
    }
    outline = next(
        item
        for item in manifest["documents"]["slide_outline"]
        if item["revision_hash"] == manifest["full_deck"]["outline_revision_hash"]
    )
    sample = next(
        item
        for item in manifest["samples"]
        if item["revision_hash"]
        == manifest["full_deck"]["approved_sample_revision_hash"]
    )
    sample_package = _sample_package()
    sample_reference = {
        "source_id": "approved_sample",
        "title": sample_package.title,
        "revision_hash": sample["revision_hash"],
        "package_hash": sample_package.package_hash,
        "slides": [
            item.model_dump(mode="json") for item in sample_package.slides
        ],
        "files": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "size_bytes": len(item.content_bytes()),
            }
            for item in sample_package.files
        ],
    }
    references = full_deck_package_reference_tool(
        workflow.store,
        workflow.runtime,
        sample_revision_hash=sample["revision_hash"],
        sample_package=sample_package,
        recent_segment_package_ids=[],
        generation_session_id=snapshot["session_id"],
    )
    template = (workflow.templates_root / "ppt_full.md").read_text(encoding="utf-8")
    expected = (
        template
        + f"\n\nFULL_DECK_GENERATION_ID: {snapshot['session_id']}\n"
        + f"FULL_DECK_SESSION_ID: {snapshot['session_id']}\n"
        + "FULL_DECK_BATCH_INDEX: 1\n"
        + f"FULL_DECK_TARGET_SLIDE_NUMBERS: {json.dumps(target_numbers)}\n"
        + "FULL_DECK_SEGMENT_PAGES_JSON: "
        + json.dumps(segment_pages, ensure_ascii=False)
        + "\nALL_OUTLINE_SLIDES_JSON: "
        + json.dumps(
            outline_slide_catalog(outline["markdown_body"]), ensure_ascii=False
        )
        + "\nADJACENT_PAGES_JSON: "
        + json.dumps(neighbors, ensure_ascii=False)
        + "\nEFFECTIVE_NEXT_BATCH_DIRECTIVES_JSON: "
        + json.dumps([], ensure_ascii=False)
        + "\nSAMPLE_VISUAL_REFERENCE_JSON: "
        + json.dumps(sample_reference, ensure_ascii=False)
        + "\nPACKAGE_REFERENCE_SOURCES_JSON: "
        + json.dumps(references.summaries(), ensure_ascii=False)
        + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + "Skill index:\n[]"
    )
    assert call["prompt"] == expected


# ---------------------------------------------------------------------------
# T4.3 — monolithic full-deck injection + empty-materials red line
# ---------------------------------------------------------------------------


def test_monolithic_prompt_injects_per_segment_descriptions(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _entered_full_deck(
        tmp_path, mock_runtime, "phase4-images-monolithic"
    )
    images_dir = install_project_images(workflow)
    captured = spy_gateway(workflow, monkeypatch)

    workflow.generate_full_deck(entered["checkpoint_id"])

    full_calls = [call for call in captured["calls"] if call["state"] == "ppt_full"]
    assert [
        json.loads(
            re.search(
                r"FULL_DECK_TARGET_SLIDE_NUMBERS: (\[[^\]]*\])", call["prompt"]
            ).group(1)
        )
        for call in full_calls
    ] == [[1], [4]]
    for call in full_calls:
        assert "全稿配图纪律" in call["prompt"]
        assert "images/团队照.png" in call["prompt"]  # full manifest everywhere
        assert call["kwargs"]["images_root"] == images_dir.resolve()
        assert call["kwargs"]["package_draft"].images_root == images_dir.resolve()

    # Segment [1]: filtered to the page-1 plan only.
    assert COVER_DESCRIPTION in full_calls[0]["prompt"]
    assert ROADMAP_DESCRIPTION not in full_calls[0]["prompt"]
    assert TEAM_DESCRIPTION not in full_calls[0]["prompt"]

    # Segment [4]: no plan intersection → FULL fallback.
    assert COVER_DESCRIPTION in full_calls[1]["prompt"]
    assert ROADMAP_DESCRIPTION in full_calls[1]["prompt"]
    assert TEAM_DESCRIPTION in full_calls[1]["prompt"]


def test_monolithic_prompt_without_images_keeps_prompt_byte_identical(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _entered_full_deck(
        tmp_path, mock_runtime, "phase4-empty-monolithic"
    )
    pre_generation = workflow.store.read()
    plan_pages = _plan_pages(pre_generation)
    pending = [page for page in plan_pages if page.get("status") == "pending"]
    captured = spy_gateway(workflow, monkeypatch)

    workflow.generate_full_deck(entered["checkpoint_id"])

    full_calls = [call for call in captured["calls"] if call["state"] == "ppt_full"]
    assert len(full_calls) == 2
    for call in full_calls:
        assert "PROJECT_IMAGES" not in call["prompt"]
        assert "配图纪律" not in call["prompt"]
        assert "images_root" not in call["kwargs"]
        assert call["kwargs"]["package_draft"].images_root is None
        assert call["prompt"].endswith("Skill index:\n[]")

    # Pre-feature formula transcribed from the Phase 4 baseline
    # (main@24e61a5, agent_core/full_deck_generation.py) for the first
    # segment; the random generation id is read back from the capture.
    manifest = pre_generation
    outline = next(
        item
        for item in manifest["documents"]["slide_outline"]
        if item["revision_hash"] == manifest["full_deck"]["outline_revision_hash"]
    )
    sample = next(
        item
        for item in manifest["samples"]
        if item["revision_hash"]
        == manifest["full_deck"]["approved_sample_revision_hash"]
    )
    segment = [pending[0]]
    target_numbers = [
        int(page["outline_ref"]["source_slide_number"]) for page in segment
    ]
    first_position = int(segment[0]["position"])
    neighbors = {
        "before": plan_pages[first_position - 1] if first_position > 0 else None,
        "after": (
            plan_pages[first_position + 1]
            if first_position + 1 < len(plan_pages)
            else None
        ),
    }
    sample_package = _sample_package()
    sample_reference = {
        "source_id": "approved_sample",
        "title": sample_package.title,
        "revision_hash": sample["revision_hash"],
        "package_hash": sample_package.package_hash,
        "slides": [
            item.model_dump(mode="json") for item in sample_package.slides
        ],
        "files": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "size_bytes": len(item.content_bytes()),
            }
            for item in sample_package.files
        ],
    }
    references = full_deck_package_reference_tool(
        workflow.store,
        workflow.runtime,
        sample_revision_hash=sample["revision_hash"],
        sample_package=sample_package,
        recent_validated_segments=[],
    )
    generation_id = re.search(
        r"FULL_DECK_GENERATION_ID: (\S+)", full_calls[0]["prompt"]
    ).group(1)
    template = (workflow.templates_root / "ppt_full.md").read_text(encoding="utf-8")
    expected = (
        template
        + f"\n\nFULL_DECK_GENERATION_ID: {generation_id}\n"
        + f"FULL_DECK_TARGET_SLIDE_NUMBERS: {json.dumps(target_numbers)}\n"
        + "FULL_DECK_SEGMENT_PAGES_JSON: "
        + json.dumps(segment, ensure_ascii=False)
        + "\nALL_OUTLINE_SLIDES_JSON: "
        + json.dumps(
            outline_slide_catalog(outline["markdown_body"]), ensure_ascii=False
        )
        + "\nADJACENT_PAGES_JSON: "
        + json.dumps(neighbors, ensure_ascii=False)
        + "\nSAMPLE_VISUAL_REFERENCE_JSON: "
        + json.dumps(sample_reference, ensure_ascii=False)
        + "\nPACKAGE_REFERENCE_SOURCES_JSON: "
        + json.dumps(references.summaries(), ensure_ascii=False)
        + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + "Skill index:\n[]"
    )
    assert full_calls[0]["prompt"] == expected


# ---------------------------------------------------------------------------
# T4.4 — full-deck revision + sample resume
# ---------------------------------------------------------------------------


def _completed_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
    project_id: str,
) -> tuple[Workflow, dict]:
    workflow, entered = _entered_full_deck(tmp_path, runtime, project_id)
    completed = workflow.generate_full_deck(entered["checkpoint_id"])
    return workflow, completed


def test_revision_regenerate_filters_and_revise_falls_back_to_full(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, completed = _completed_full_deck(
        tmp_path, mock_runtime, "phase4-images-revision"
    )
    images_dir = install_project_images(workflow)
    captured = spy_gateway(workflow, monkeypatch)
    parent_hash = completed["full_deck"]["current_revision_hash"]

    create_full_deck_revision(
        workflow,
        completed["checkpoint_id"],
        parent_hash,
        operation="revise_full_deck",
        feedback="第 1 页标题更有力。",
    )
    revise_prompt = captured["calls"][0]["prompt"]
    # Revise targets are model-declared → unknown upfront → full texts.
    assert "全稿配图纪律" in revise_prompt
    assert COVER_DESCRIPTION in revise_prompt
    assert ROADMAP_DESCRIPTION in revise_prompt
    assert TEAM_DESCRIPTION in revise_prompt
    assert captured["calls"][0]["kwargs"]["images_root"] == images_dir.resolve()
    assert (
        captured["calls"][0]["kwargs"]["package_draft"].images_root
        == images_dir.resolve()
    )

    captured["calls"].clear()
    refreshed = workflow.store.read()
    create_full_deck_revision(
        workflow,
        refreshed["checkpoint_id"],
        refreshed["full_deck"]["current_revision_hash"],
        operation="regenerate_full_deck",
    )
    regenerate_prompt = captured["calls"][0]["prompt"]
    # Regenerate covers the non-sample pages 1 and 4 → only the page-1 plan.
    assert "全稿配图纪律" in regenerate_prompt
    assert COVER_DESCRIPTION in regenerate_prompt
    assert ROADMAP_DESCRIPTION not in regenerate_prompt
    assert TEAM_DESCRIPTION not in regenerate_prompt
    assert captured["calls"][0]["kwargs"]["images_root"] == images_dir.resolve()


def test_revision_prompt_without_images_keeps_prompt_byte_identical(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, completed = _completed_full_deck(
        tmp_path, mock_runtime, "phase4-empty-revision"
    )
    captured = spy_gateway(workflow, monkeypatch)
    parent_hash = completed["full_deck"]["current_revision_hash"]

    create_full_deck_revision(
        workflow,
        completed["checkpoint_id"],
        parent_hash,
        operation="revise_full_deck",
        feedback="第 1 页标题更有力。",
    )

    call = captured["calls"][0]
    assert call["state"] == "ppt_full"
    assert "PROJECT_IMAGES" not in call["prompt"]
    assert "配图纪律" not in call["prompt"]
    assert "images_root" not in call["kwargs"]
    assert call["kwargs"]["package_draft"].images_root is None

    # Pre-feature formula transcribed from the Phase 4 baseline
    # (main@24e61a5, agent_core/full_deck_revision.py); the random revision
    # id is read back from the capture.
    manifest = workflow.store.read()
    current = next(
        item
        for item in manifest["full_deck_revisions"]
        if item["revision_hash"] == parent_hash
    )
    pages = current["plan"]["pages"]
    outline = next(
        item
        for item in manifest["documents"]["slide_outline"]
        if item["revision_hash"] == manifest["full_deck"]["outline_revision_hash"]
    )
    sample = next(
        item
        for item in manifest["samples"]
        if item["revision_hash"]
        == manifest["full_deck"]["approved_sample_revision_hash"]
    )
    parent_package = full_deck_package_model(
        workflow, parent_hash, current["package"]
    )
    generation_id = re.search(
        r"FULL_DECK_REVISION_ID: (\S+)", call["prompt"]
    ).group(1)
    template = (
        workflow.templates_root / "ppt_full_revision.md"
    ).read_text(encoding="utf-8")
    expected = (
        template
        + "\n\nFULL_DECK_OPERATION: revise_full_deck\n"
        + f"FULL_DECK_REVISION_ID: {generation_id}\n"
        + f"FULL_DECK_PARENT_REVISION_HASH: {parent_hash}\n"
        + "FULL_DECK_TARGET_SLIDE_NUMBERS: model_declared"
        + "\nFULL_DECK_TARGET_SLOT_IDS: model_declared"
        + "\nFULL_DECK_MANDATORY_SLIDE_NUMBERS: "
        + json.dumps([], ensure_ascii=False)
        + "\nFULL_DECK_MANDATORY_SLOT_IDS: "
        + json.dumps([], ensure_ascii=False)
        + "\nFULL_DECK_REVISION_PAGE_SPECS_JSON: "
        + json.dumps(pages, ensure_ascii=False)
        + "\nCURRENT_FULL_DECK_REFERENCE_JSON: "
        + json.dumps(_reference_summary(parent_package), ensure_ascii=False)
        + "\nAPPROVED_SAMPLE_REFERENCE_JSON: "
        + json.dumps(_reference_summary(_sample_package()), ensure_ascii=False)
        + "\nALL_OUTLINE_SLIDES_JSON: "
        + json.dumps(
            outline_slide_catalog(outline["markdown_body"]), ensure_ascii=False
        )
        + "\nUSER_FEEDBACK: "
        + json.dumps("第 1 页标题更有力。", ensure_ascii=False)
        + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + "Skill index:\n[]"
    )
    assert call["prompt"] == expected


def _exhaust_sample_with_resume_context(
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, str, str]:
    """Run generate_sample until the tool limit, persisting resume context."""

    prompts: list[str] = []

    def exhaust(_state: str, prompt: str, **kwargs):
        prompts.append(prompt)
        draft = kwargs["package_draft"]
        draft.write(
            "index.html",
            '<section class="slide" data-slide-id="draft_1">草稿</section>',
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": prompt},
        ]
        traces = []
        for index in range(1, 21):
            messages.extend([
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": f"call_{index}",
                        "function": {"name": "read", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": f"call_{index}", "content": "{}"},
            ])
            traces.append({
                "type": "tool_call",
                "tool": "read",
                "path": "templates/deck.html",
                "content_hash": "sha256:read",
                "offset": index,
                "end": index + 1,
                "round": index,
                "round_limit": 20,
            })
        workflow.gateway.last_messages = messages
        workflow.gateway.last_traces = traces
        raise MaxToolRoundsExceeded("maximum tool rounds exceeded")

    monkeypatch.setattr(workflow.gateway, "generate", exhaust)
    manifest = workflow.store.read()
    with pytest.raises(MaxToolRoundsExceeded):
        workflow.generate_sample(manifest["checkpoint_id"])
    attempts = workflow.store.sample_attempts(
        current_checkpoint_id=manifest["checkpoint_id"],
    )
    failed = attempts[-1]
    assert failed["resume_available"] is True
    return manifest, failed["prompt_call_id"], prompts[0]


def _sample_workflow(
    tmp_path: Path, runtime: ManagedRuntime, project_id: str
) -> Workflow:
    store = ProjectStore(tmp_path / "projects", project_id)
    task = TaskCard(title="季度复盘", objective="形成下一季度投入共识")
    store.create(task.model_dump(), runtime.snapshot())
    return Workflow(store, runtime)


def _finish_clarifications(workflow: Workflow, manifest: dict) -> dict:
    while (
        manifest["state"] == "intake_clarify"
        and manifest["phase"] == "ready_for_clarification"
    ):
        manifest = workflow.start_clarification(manifest["checkpoint_id"])
    return manifest


def _ready_for_sample(workflow: Workflow) -> dict:
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
    manifest = workflow.generate_document(
        "slide_outline", manifest["checkpoint_id"]
    )
    document = manifest["documents"]["slide_outline"][-1]
    return workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], document["revision_hash"]
    )


def _sample_output_payload() -> str:
    slides = [
        {"slide_id": "sample_1", "title": "结论先行", "source_slide_number": 1},
        {"slide_id": "sample_2", "title": "行动路径", "source_slide_number": 2},
    ]
    sections = "".join(
        f'<section class="slide" data-slide-id="{slide["slide_id"]}">'
        f'<h1>样品页 {index}</h1><p>结论、证据与行动建议。</p></section>'
        for index, slide in enumerate(slides, start=1)
    )
    html = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="assets/deck.css"></head><body>'
        + sections
        + '<script src="assets/deck.js"></script></body></html>'
    )
    return json.dumps({
        "entrypoint": "index.html",
        "title": "样品",
        "slide_count": 2,
        "slides": slides,
        "files": [
            {"path": "index.html", "content": html, "encoding": "utf-8"},
            {"path": "assets/deck.css", "content": ".slide{padding:4rem}", "encoding": "utf-8"},
            {"path": "assets/deck.js", "content": "addEventListener('keydown',()=>{})", "encoding": "utf-8"},
        ],
    }, ensure_ascii=False)


def test_sample_resume_rearms_images_root_with_materials(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _sample_workflow(tmp_path, mock_runtime, "phase4-images-resume")
    manifest = _ready_for_sample(workflow)
    manifest, failed_call_id, original_prompt = _exhaust_sample_with_resume_context(
        workflow, monkeypatch
    )
    images_dir = install_project_images(workflow)

    captured: dict[str, list] = {"calls": []}

    def complete(_state: str, prompt: str, **kwargs):
        captured["calls"].append({"prompt": prompt, "kwargs": kwargs})
        return _sample_output_payload(), [{
            "type": "model_call",
            "provider": "test",
            "model": "resumed",
            "usage": {},
        }]

    monkeypatch.setattr(workflow.gateway, "generate", complete)
    generated = workflow.resume_sample(
        manifest["checkpoint_id"],
        failed_call_id,
        10,
    )

    sample = generated["samples"][-1]
    assert sample["provenance"]["sample_resumed"] is True
    call = captured["calls"][0]
    # The prompt is the persisted original, reused verbatim.
    assert call["prompt"] == original_prompt
    # The draft and the gateway call regain the project images root.
    assert call["kwargs"]["images_root"] == images_dir.resolve()
    assert call["kwargs"]["package_draft"].images_root == images_dir.resolve()


def test_sample_resume_without_images_keeps_gateway_call_identical(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _sample_workflow(tmp_path, mock_runtime, "phase4-empty-resume")
    manifest = _ready_for_sample(workflow)
    manifest, failed_call_id, _ = _exhaust_sample_with_resume_context(
        workflow, monkeypatch
    )
    assert not (workflow.store.root / "images").exists()

    captured: dict[str, list] = {"calls": []}

    def complete(_state: str, prompt: str, **kwargs):
        captured["calls"].append({"prompt": prompt, "kwargs": kwargs})
        return _sample_output_payload(), [{
            "type": "model_call",
            "provider": "test",
            "model": "resumed",
            "usage": {},
        }]

    monkeypatch.setattr(workflow.gateway, "generate", complete)
    workflow.resume_sample(manifest["checkpoint_id"], failed_call_id, 10)

    call = captured["calls"][0]
    # Exactly the pre-Phase-4 kwarg set — no images_root anywhere.
    assert set(call["kwargs"]) == {
        "json_mode",
        "package_draft",
        "resume_messages",
        "max_tool_rounds",
        "prior_tool_rounds",
        "prior_tool_call_count",
        "prior_skill_read_count",
    }
    assert call["kwargs"]["package_draft"].images_root is None


# ---------------------------------------------------------------------------
# T4.5 — offline validation + Composer closure, Chinese filename dual forms
# ---------------------------------------------------------------------------


def _image_package(reference: str) -> HtmlPptPackage:
    image_bytes = b"\x89PNG-fake-bytes"
    return HtmlPptPackage(
        title="中文图片包",
        slide_count=1,
        slides=[
            PackageSlide(slide_id="slide-1", title="封面", source_slide_number=1),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    f'data-slide-id="slide-1"><img src="{reference}" alt="配图">'
                    "</section></body></html>"
                ),
            ),
            PackageFile(
                path="img/中文.jpg",
                content=base64.b64encode(image_bytes).decode("ascii"),
                encoding="base64",
                origin="project_image:images/中文.jpg",
            ),
        ],
    )


def test_offline_validation_accepts_bare_and_encoded_chinese_references() -> None:
    _validate_offline_package(_image_package("img/中文.jpg"))
    _validate_offline_package(_image_package("img/%E4%B8%AD%E6%96%87.jpg"))


def test_offline_validation_rejects_missing_chinese_targets() -> None:
    with pytest.raises(FullDeckGenerationError):
        _validate_offline_package(_image_package("img/缺失.jpg"))
    with pytest.raises(FullDeckGenerationError):
        _validate_offline_package(_image_package("img/%E7%BC%BA%E5%A4%B1.jpg"))


def test_composer_reassembly_closes_chinese_references_in_both_forms() -> None:
    image_bytes = b"\x89PNG-fake-bytes"
    package = HtmlPptPackage(
        title="双形态引用页段",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="slide-1", title="裸路径页", source_slide_number=1),
            PackageSlide(slide_id="slide-2", title="编码路径页", source_slide_number=2),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body>'
                    '<section class="slide" data-slide-id="slide-1">'
                    '<img src="img/中文.jpg"></section>'
                    '<section class="slide" data-slide-id="slide-2">'
                    '<img src="img/%E4%B8%AD%E6%96%87.jpg"></section>'
                    "</body></html>"
                ),
            ),
            PackageFile(
                path="img/中文.jpg",
                content=base64.b64encode(image_bytes).decode("ascii"),
                encoding="base64",
                origin="project_image:images/中文.jpg",
            ),
        ],
    )
    composition = compose_full_deck(FullDeckComposerInput(
        title="重组全稿",
        sources=[ComposerSource(source_id="seg-1", package=package)],
        pages=[
            ComposerPage(
                slot_id="slot_" + "a" * 24,
                slide_id="slot-1",
                title="裸路径页",
                source_slide_number=1,
                source_id="seg-1",
                source_slide_id="slide-1",
            ),
            ComposerPage(
                slot_id="slot_" + "b" * 24,
                slide_id="slot-2",
                title="编码路径页",
                source_slide_number=2,
                source_id="seg-1",
                source_slide_id="slide-2",
            ),
        ],
    ))
    namespaced_images = [
        item for item in composition.package.files
        if item.path.endswith("img/中文.jpg")
    ]
    assert len(namespaced_images) == 1
    assert namespaced_images[0].content_bytes() == image_bytes
    assert namespaced_images[0].origin.startswith("composer:seg-1:")
    # Both reference forms still close after sources/<hash>/ reassembly.
    _validate_offline_package(composition.package)


# ---------------------------------------------------------------------------
# §7 Phase 4 — end-to-end correspondence with an obedient gateway
# ---------------------------------------------------------------------------

# The end-to-end outline plans the cover on generated page 1 and the team
# photo on generated page 4 (the sample covers pages 2–3, which carry no
# plans). Page 1 references its image with the bare path, page 4 with the
# percent-encoded form.
E2E_OUTLINE = """# 逐页大纲

## 第 1 页｜开场
配图：封面图.png
## 第 2 页｜关键判断
## 第 3 页｜数据证据
## 第 4 页｜行动计划
配图：团队照.png
"""
E2E_PLANS = {1: ["封面图.png"], 4: ["团队照.png"]}
ENCODED_REFERENCE_PAGES = {4}


def _obedient_gateway(
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
    plans: dict[int, list[str]],
    encoded_pages: set[int],
) -> dict[str, list]:
    """A model that copies exactly the planned images and references them."""

    captured: dict[str, list] = {"calls": []}
    original = workflow.gateway.generate

    def generate(state: str, prompt: str, **kwargs):
        captured["calls"].append(
            {"state": state, "prompt": prompt, "kwargs": kwargs}
        )
        if state != "ppt_full":
            return original(state, prompt, **kwargs)
        target_match = re.search(
            r"FULL_DECK_TARGET_SLIDE_NUMBERS: (\[[^\]]*\])", prompt
        )
        target_numbers = json.loads(target_match.group(1))
        draft = kwargs["package_draft"]
        slides = []
        sections = []
        for number in target_numbers:
            images = []
            for name in plans.get(number, []):
                draft.copy_project_image(f"images/{name}", f"img/{name}")
                reference = (
                    quote(f"img/{name}")
                    if number in encoded_pages
                    else f"img/{name}"
                )
                images.append(f'<img src="{reference}" alt="{name}">')
            slide_id = f"full-{number}"
            slides.append({
                "slide_id": slide_id,
                "title": f"第 {number} 页",
                "source_slide_number": number,
            })
            sections.append(
                f'<section class="slide" data-slide-id="{slide_id}">'
                f'<h1>第 {number} 页</h1>{"".join(images)}</section>'
            )
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
            "model": "obedient",
            "usage": {},
        }]

    monkeypatch.setattr(workflow.gateway, "generate", generate)
    return captured


def test_full_deck_with_images_end_to_end_corresponds_to_outline_plans(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(
        main_front, "jobs", main_front.JobRegistry(project_root / ".jobs")
    )
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _entered_full_deck(
        tmp_path,
        mock_runtime,
        "phase4-images-e2e",
        outline_markdown=E2E_OUTLINE,
    )
    images_dir = install_project_images(workflow)
    captured = _obedient_gateway(
        workflow, monkeypatch, E2E_PLANS, ENCODED_REFERENCE_PAGES
    )

    completed = workflow.generate_full_deck(entered["checkpoint_id"])

    # Every segment call carried the images root and the filtered texts.
    full_calls = [call for call in captured["calls"] if call["state"] == "ppt_full"]
    assert [call["kwargs"]["images_root"] for call in full_calls] == [
        images_dir.resolve(),
        images_dir.resolve(),
    ]
    assert COVER_DESCRIPTION in full_calls[0]["prompt"]
    assert TEAM_DESCRIPTION not in full_calls[0]["prompt"]
    assert TEAM_DESCRIPTION in full_calls[1]["prompt"]
    assert COVER_DESCRIPTION not in full_calls[1]["prompt"]

    # The composed package carries exactly the planned images, byte-identical.
    revision_hash = completed["full_deck"]["current_revision_hash"]
    stored_files = {
        item["path"]: item["content"]
        for item in workflow.store.full_deck_package_contents(revision_hash)
    }
    img_paths = {
        path.rsplit("img/", 1)[1]
        for path in stored_files
        if "/img/" in path
    }
    assert img_paths == {"封面图.png", "团队照.png"}

    # Composed documents inline image references as data URIs (the sandboxed
    # shell cannot load file:// subresources); any remaining package-relative
    # reference must still close, in both the bare and percent-encoded form.
    for path, content in stored_files.items():
        if not path.endswith(".html"):
            continue
        for reference in re.findall(r'src="([^"]+)"', content.decode("utf-8")):
            if reference.startswith("data:"):
                continue
            target = posixpath.normpath(
                str(PurePosixPath(path).parent / PurePosixPath(unquote(reference)))
            )
            assert target in stored_files, (path, reference)

    # ZIP export keeps the unicode logical names readable.
    client = TestClient(main_front.app)
    response = client.get(
        f"/api/projects/phase4-images-e2e/full-deck/revisions/{revision_hash}/export"
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert any(name.endswith("img/封面图.png") for name in names)
        assert any(name.endswith("img/团队照.png") for name in names)
        assert all(
            archive.read(name) == (images_dir / name.rsplit("img/", 1)[1]).read_bytes()
            for name in names
            if name.endswith(("img/封面图.png", "img/团队照.png"))
        )


def test_sample_with_images_end_to_end_corresponds_to_outline_plans(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sample half of the §7 Phase 4 correspondence criterion."""

    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(
        main_front, "jobs", main_front.JobRegistry(project_root / ".jobs")
    )
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow = _sample_workflow(tmp_path, mock_runtime, "phase4-images-sample-e2e")
    manifest = _ready_for_sample(workflow)

    # Plan one image per sample page: page 1 bare, page 2 percent-encoded.
    outline = workflow.store.read()["documents"]["slide_outline"][-1]
    edited = outline["markdown_body"].replace(
        "## 第 1 页｜封面\n",
        "## 第 1 页｜封面\n配图：封面图.png\n",
    ).replace(
        "## 第 2 页｜结论先行\n",
        "## 第 2 页｜结论先行\n配图：路线图.jpg\n",
    )
    manifest = workflow.edit_document(
        "slide_outline", manifest["checkpoint_id"], edited
    )
    document = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], document["revision_hash"]
    )
    images_dir = install_project_images(workflow)

    page_plans = {1: ("封面图.png", False), 2: ("路线图.jpg", True)}
    captured: dict[str, list] = {"calls": []}
    original = workflow.gateway.generate

    def obedient_sample(state: str, prompt: str, **kwargs):
        captured["calls"].append(
            {"state": state, "prompt": prompt, "kwargs": kwargs}
        )
        if state != "ppt_sample":
            return original(state, prompt, **kwargs)
        draft = kwargs["package_draft"]
        slides = []
        sections = []
        for number, (name, encoded) in page_plans.items():
            draft.copy_project_image(f"images/{name}", f"img/{name}")
            reference = quote(f"img/{name}") if encoded else f"img/{name}"
            slide_id = f"sample_{number}"
            slides.append({
                "slide_id": slide_id,
                "title": f"第 {number} 页",
                "source_slide_number": number,
            })
            sections.append(
                f'<section class="slide" data-slide-id="{slide_id}">'
                f'<h1>第 {number} 页</h1><img src="{reference}" alt="{name}"></section>'
            )
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

    monkeypatch.setattr(workflow.gateway, "generate", obedient_sample)
    generated = workflow.generate_sample(manifest["checkpoint_id"])

    call = captured["calls"][0]
    assert call["kwargs"]["images_root"] == images_dir.resolve()
    assert call["kwargs"]["package_draft"].images_root == images_dir.resolve()
    assert "样品配图纪律" in call["prompt"]

    sample = generated["samples"][-1]
    assert sample["package"]["slide_count"] == 2
    files = {
        item["path"]: item for item in sample["package"]["files"]
    }
    assert {path for path in files if path.startswith("img/")} == {
        "img/封面图.png",
        "img/路线图.jpg",
    }
    index_html = files["index.html"]["content"]
    for reference in re.findall(r'src="([^"]+)"', index_html):
        target = posixpath.normpath(
            str(PurePosixPath("index.html").parent / PurePosixPath(unquote(reference)))
        )
        assert target in files, reference

    # ZIP export keeps the unicode sample image names readable.
    client = TestClient(main_front.app)
    response = client.get(
        f"/api/projects/phase4-images-sample-e2e/samples/revisions/"
        f"{sample['revision_hash']}/export"
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "img/封面图.png" in names
        assert "img/路线图.jpg" in names
        assert (
            archive.read("img/封面图.png")
            == (images_dir / "封面图.png").read_bytes()
        )
