"""Phase 3 of the local image reference plan: outline and sample integration.

Covers T3.1 (outline_page_image_map loose parsing), T3.3/T3.5 (material
injection into the outline/sample prompts with images_root plumbed through),
and the empty-materials red line: with no project images the prompts stay
byte-for-byte identical to the pre-feature formula.
"""

import json
from pathlib import Path

import pytest

from agent_core.models import TaskCard
from agent_core.workflow import SAMPLE_HTML_CHAR_BUDGET, Workflow
from agent_core.workflow_support import (
    outline_page_image_map,
    outline_slide_catalog,
)
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


COVER_DESCRIPTION = "封面主视觉：城市天际线剪影，深蓝基调。" + "细节" * 200
ROADMAP_DESCRIPTION = "三阶段路线图：现状、试点、推广。"


@pytest.fixture
def workflow(tmp_path: Path, mock_runtime: ManagedRuntime) -> Workflow:
    store = ProjectStore(tmp_path / "projects", "demo")
    task = TaskCard(title="季度复盘", objective="形成下一季度投入共识")
    store.create(task.model_dump(), mock_runtime.snapshot())
    return Workflow(store, mock_runtime)


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


def install_project_images(workflow: Workflow) -> Path:
    images_dir = workflow.store.root / "images"
    images_dir.mkdir()
    (images_dir / "封面图.png").write_bytes(b"\x89PNG-fake-bytes")
    (images_dir / "封面图.md").write_text(COVER_DESCRIPTION, encoding="utf-8")
    (images_dir / "路线图.jpg").write_bytes(b"\xff\xd8-fake-bytes")
    (images_dir / "路线图.md").write_text(ROADMAP_DESCRIPTION, encoding="utf-8")
    return images_dir


def finish_automatic_clarification(workflow: Workflow, manifest: dict) -> dict:
    while (
        manifest["state"] == "intake_clarify"
        and manifest["phase"] == "ready_for_clarification"
    ):
        manifest = workflow.start_clarification(manifest["checkpoint_id"])
    return manifest


def ready_for_outline(workflow: Workflow) -> dict:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
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
    return workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], document["revision_hash"]
    )


def ready_for_sample(workflow: Workflow) -> dict:
    manifest = ready_for_outline(workflow)
    manifest = workflow.generate_document(
        "slide_outline", manifest["checkpoint_id"]
    )
    document = manifest["documents"]["slide_outline"][-1]
    return workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], document["revision_hash"]
    )


# ---------------------------------------------------------------------------
# T3.1 — outline_page_image_map
# ---------------------------------------------------------------------------


def test_outline_page_image_map_parses_loose_lines() -> None:
    markdown = (
        "# 大纲\n\n"
        "## 第 1 页｜封面\n"
        "- 本页目的：建立主题\n"
        "配图：封面图.png\n\n"
        "## 第 2 页｜结论\n"
        "- 配图: 路线图.jpg，未登记图.png\n\n"
        "## 第 3 页｜背景\n"
        "- 无配图规划\n\n"
        "## 第 4 页｜行动\n"
        "* 配图 ： 封面图.png、另一个未登记图.webp\n\n"
        "## 第 5 页｜收尾\n"
        "配图：\n"
    )
    available = {"封面图.png", "路线图.jpg"}
    assert outline_page_image_map(markdown, available) == {
        1: ["封面图.png"],
        2: ["路线图.jpg"],
        4: ["封面图.png"],
    }


def test_outline_page_image_map_dedupes_and_splits_mixed_separators() -> None:
    markdown = (
        "## 第 1 页｜封面\n"
        "配图：a.png、b.png，c.png, a.png\n"
        "配图：b.png\n"
    )
    available = {"a.png", "b.png", "c.png"}
    assert outline_page_image_map(markdown, available) == {
        1: ["a.png", "b.png", "c.png"]
    }


def test_outline_page_image_map_fallback_headings_use_positional_numbers() -> None:
    markdown = "## 封面\n配图：a.png\n\n## 结论\n没有图\n\n## 行动\n配图：b.png\n"
    assert outline_page_image_map(markdown, {"a.png", "b.png"}) == {
        1: ["a.png"],
        3: ["b.png"],
    }


def test_outline_page_image_map_ignores_everything_without_a_match() -> None:
    markdown = "## 第 1 页｜封面\n配图：a.png\n"
    assert outline_page_image_map(markdown, set()) == {}
    assert outline_page_image_map("配图：a.png", {"a.png"}) == {}
    assert outline_page_image_map("", {"a.png"}) == {}


def test_outline_catalog_is_unaffected_by_image_plan_lines() -> None:
    with_plans = "## 第 1 页｜封面\n配图：a.png\n\n## 第 2 页｜结论\n- 配图：b.png、c.png\n"
    without_plans = "## 第 1 页｜封面\n\n## 第 2 页｜结论\n"
    assert outline_slide_catalog(with_plans) == outline_slide_catalog(without_plans)


# ---------------------------------------------------------------------------
# T3.3 — generate_document injection + empty-materials red line
# ---------------------------------------------------------------------------


def test_generate_document_injects_project_images(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ready_for_outline(workflow)
    images_dir = install_project_images(workflow)
    captured = spy_gateway(workflow, monkeypatch)

    manifest = workflow.generate_document(
        "slide_outline", manifest["checkpoint_id"]
    )

    call = captured["calls"][0]
    assert call["state"] == "slide_outline"
    assert "项目图片素材" in call["prompt"]
    assert "PROJECT_IMAGES_JSON: [" in call["prompt"]
    assert "images/封面图.png" in call["prompt"]
    assert "PROJECT_IMAGE_DESCRIPTIONS:" in call["prompt"]
    assert COVER_DESCRIPTION in call["prompt"]  # full text, not a summary
    assert ROADMAP_DESCRIPTION in call["prompt"]
    assert call["kwargs"]["images_root"] == images_dir.resolve()
    document = manifest["documents"]["slide_outline"][-1]
    assert document["provenance"]["template_id"] == "slide_outline"


def test_generate_document_without_images_keeps_prompt_byte_identical(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ready_for_outline(workflow)
    captured = spy_gateway(workflow, monkeypatch)

    workflow.generate_document("slide_outline", manifest["checkpoint_id"])

    call = captured["calls"][0]
    template = (workflow.templates_root / "slide_outline.md").read_text(
        encoding="utf-8"
    )
    current = workflow.store.read()
    upstream = current["documents"]["narrative_structure"][-1]
    # Pre-feature formula transcribed from the Phase 3 baseline (main@a875ee8).
    expected = (
        template
        + "\n\nCreate the final slide_outline artifact as Markdown. Do not wrap it in a code fence. "
        "Choose an appropriate narrative method freely. You may use read to consult the skill index.\n"
        + f"Task card:\n{json.dumps(current['task_card'], ensure_ascii=False)}\n"
        + f"Clarification answers:\n{json.dumps(current['clarification_answers'], ensure_ascii=False)}\n"
        + f"Approved upstream document:\n{upstream['markdown_body']}\n"
        + "Skill index:\n[]"
    )
    assert call["prompt"] == expected
    assert "images_root" not in call["kwargs"]
    assert "PROJECT_IMAGES" not in call["prompt"]


# ---------------------------------------------------------------------------
# T3.5 — generate_sample injection + empty-materials red line
# ---------------------------------------------------------------------------


def approve_outline_with_image_plans(
    workflow: Workflow, manifest: dict
) -> dict:
    outline = workflow.store.read()["documents"]["slide_outline"][-1]
    edited = outline["markdown_body"].replace(
        "## 第 1 页｜封面\n",
        "## 第 1 页｜封面\n配图：封面图.png\n",
    )
    manifest = workflow.edit_document(
        "slide_outline", manifest["checkpoint_id"], edited
    )
    document = manifest["documents"]["slide_outline"][-1]
    return workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], document["revision_hash"]
    )


def test_generate_sample_injects_images_and_registers_tool_root(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ready_for_sample(workflow)
    images_dir = install_project_images(workflow)
    manifest = approve_outline_with_image_plans(workflow, manifest)
    captured = spy_gateway(workflow, monkeypatch)

    manifest = workflow.generate_sample(manifest["checkpoint_id"])

    outline_body = manifest["documents"]["slide_outline"][-1]["markdown_body"]
    assert outline_page_image_map(outline_body, {"封面图.png", "路线图.jpg"}) == {
        1: ["封面图.png"]
    }
    call = captured["calls"][0]
    assert call["state"] == "ppt_sample"
    assert "样品配图纪律" in call["prompt"]
    assert "PROJECT_IMAGES_JSON: [" in call["prompt"]
    assert "PROJECT_IMAGE_SUMMARIES:" in call["prompt"]
    assert "PROJECT_IMAGE_DESCRIPTIONS" not in call["prompt"]
    assert COVER_DESCRIPTION[:300] + "…" in call["prompt"]
    assert COVER_DESCRIPTION not in call["prompt"]  # truncated, not full text
    assert ROADMAP_DESCRIPTION in call["prompt"]  # short text stays intact
    assert "copy_project_image" in call["prompt"]
    assert call["kwargs"]["images_root"] == images_dir.resolve()
    assert call["kwargs"]["package_draft"].images_root == images_dir.resolve()
    assert manifest["samples"][-1]["package"]["entrypoint"] == "index.html"


def test_generate_sample_without_images_keeps_prompt_byte_identical(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ready_for_sample(workflow)
    captured = spy_gateway(workflow, monkeypatch)

    workflow.generate_sample(manifest["checkpoint_id"])

    call = captured["calls"][0]
    template = (workflow.templates_root / "ppt_sample.md").read_text(
        encoding="utf-8"
    )
    current = workflow.store.read()
    outline = current["documents"]["slide_outline"][-1]
    page_count = workflow.runtime.policy.sample_page_count
    # Pre-feature formula transcribed from the Phase 3 baseline (main@a875ee8).
    expected = (
        template
        + f"\n\nSAMPLE_PAGE_COUNT: {page_count}\n"
        + "SAMPLE_STAGE_ONLY: true\n"
        + f"OUTLINE_SLIDES_JSON: {json.dumps(outline_slide_catalog(outline['markdown_body']), ensure_ascii=False)}\n"
        + "PRESERVE_SOURCE_SLIDE_NUMBERS: none\n"
        + f"SAMPLE_HTML_CHAR_BUDGET_PER_PAGE: {SAMPLE_HTML_CHAR_BUDGET}\n"
        + f"Task card:\n{json.dumps(current['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + "Previous HTML-PPT package manifest:\nnone\n"
        + "Revision feedback:\nnone\n"
        + "Skill index:\n[]"
    )
    assert call["prompt"] == expected
    assert "images_root" not in call["kwargs"]
    assert call["kwargs"]["package_draft"].images_root is None
    assert "PROJECT_IMAGES" not in call["prompt"]


# ---------------------------------------------------------------------------
# §7 Phase 3 — empty materials, whole mock chain unchanged
# ---------------------------------------------------------------------------


def test_empty_materials_full_mock_chain_stays_on_the_legacy_path(
    workflow: Workflow,
) -> None:
    manifest = ready_for_sample(workflow)
    assert not (workflow.store.root / "images").exists()
    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    outline_body = manifest["documents"]["slide_outline"][-1]["markdown_body"]
    assert "配图" not in outline_body
    sample = manifest["samples"][-1]
    assert sample["status"] == "pending_approval"
    assert sample["package"]["slide_count"] == workflow.runtime.policy.sample_page_count
