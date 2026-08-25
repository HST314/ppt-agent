from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

from agent_core.models import SampleOutput, SamplePage, TaskCard, digest
from agent_core.workflow import Workflow, capabilities, stable_hash
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore


@pytest.fixture
def workflow(tmp_path: Path, mock_runtime: ManagedRuntime) -> Workflow:
    runtime = mock_runtime
    store = ProjectStore(tmp_path / "projects", "demo")
    task = TaskCard(title="季度复盘", objective="形成下一季度投入共识")
    store.create(task.model_dump(), runtime.snapshot())
    return Workflow(store, runtime)


def test_sample_stage_happy_path_and_feedback_revision(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    assert capabilities(manifest) == ["inspect", "branch", "start_clarification"]

    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    assert card["checkpoint_id"] == manifest["checkpoint_id"]
    assert card["provenance"]["skills_hash"] == stable_hash(card["provenance"]["skill_index"])

    manifest = workflow.answer_clarification(
        manifest["checkpoint_id"],
        card["question_card_id"],
        {question["question_id"]: "management" for question in card["questions"]},
    )
    assert manifest["phase"] == "ready_to_generate"

    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    assert narrative["provenance"]["template_id"] == "narrative_structure"
    assert narrative["provenance"]["traces"][0]["type"] == "model_call"
    assert narrative["provenance"]["output_hash"] == digest(narrative["markdown_body"])
    manifest = workflow.approve_document("narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"])
    assert manifest["state"] == "slide_outline"

    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document("slide_outline", manifest["checkpoint_id"], outline["revision_hash"])
    assert manifest["state"] == "ppt_sample"
    assert manifest["phase"] == "ready_to_generate"

    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    sample = manifest["samples"][-1]
    assert len(sample["pages"]) == 2
    assert sample["provenance"]["sample_page_count"] == 2
    assert {"revise_sample", "approve_sample", "regenerate_sample"} <= set(capabilities(manifest))

    manifest = workflow.generate_sample(manifest["checkpoint_id"], feedback="放大主标题并减少文字")
    revised = manifest["samples"][-1]
    assert revised["revision"] == 2
    assert revised["parent_revision_hash"] == sample["revision_hash"]
    assert revised["feedback"] == "放大主标题并减少文字"

    workflow.runtime.policy = workflow.runtime.policy.model_copy(update={"sample_page_count": 3})
    manifest = workflow.generate_sample(manifest["checkpoint_id"], regenerate=True)
    regenerated = manifest["samples"][-1]
    assert regenerated["revision"] == 3
    assert len(regenerated["pages"]) == 3

    manifest = workflow.approve_sample(manifest["checkpoint_id"], regenerated["revision_hash"])
    assert manifest["phase"] == "completed"
    assert manifest["samples"][-1]["status"] == "approved"
    assert {"revise_sample", "regenerate_sample"} <= set(capabilities(manifest))
    sample_snapshot = next(
        item for item in workflow.store.progress_snapshots() if item["stage"] == "ppt_sample"
    )
    branched = workflow.store.fork(
        sample_snapshot["checkpoint_id"],
        "sample-rerun",
        mode="rerun_stage",
        stage="ppt_sample",
    )
    assert branched["state"] == "ppt_sample"
    assert branched["phase"] == "ready_to_generate"
    assert branched["samples"] == []


@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script>",
        '<img src="https://example.com/tracker.png">',
        '<img src="&#x68;ttps://example.com/tracker.png">',
        '<img src="/relative-tracker.png">',
        '<div onclick="alert(1)">unsafe</div>',
        '<style>@import "https://example.com/theme.css";</style>',
        '<style>@\\69mport "https://example.com/theme.css";</style>',
        '<style>body{background-image:image-set("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:\\69mage-set("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:u\\72l("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:url("\\68 ttps://example.com/tracker.png")}</style>',
        '<style>body{b\\61ckground-image:url("https://example.com/tracker.png")}</style>',
        '<svg><rect fill="u\\72l(https://example.com/paint.svg#gradient)"></rect></svg>',
        '<div data-remote="https://example.com">unsafe attribute</div>',
    ],
)
def test_sample_page_rejects_active_or_external_html(html: str) -> None:
    with pytest.raises(ValueError, match="active or external"):
        SamplePage(page_id="sample_1", title="Unsafe", html=html)


def test_sample_page_allows_passive_inline_content() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Inline",
        html=(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<style>.mark{background-image:linear-gradient(135deg,#fff,#ddd)}'
            '.dot{background-image:url("data:image/png;base64,AA==")}</style></head>'
            '<body><!-- harmless --><a href="#detail">内容</a>'
            '<svg viewBox="0 0 10 10"><defs><linearGradient id="paint">'
            '<stop offset="0" stop-color="#fff"></stop></linearGradient></defs>'
            '<rect width="10" height="10" fill="url(#paint)"></rect></svg></body></html>'
        ),
    )

    assert page.page_id == "sample_1"
    assert "<!doctype" not in page.html
    assert "<html" not in page.html
    assert "<!--" not in page.html
    assert "linear-gradient" in page.html
    assert 'fill="url(#paint)"' in page.html


def test_sample_page_rejects_comment_capture_payload() -> None:
    html = (
        '<!-- <head> --><html><head></head><body><style>'
        'body{background-image:image-set("https://audit.invalid/pixel")}'
        '</style></body></html>'
    )

    with pytest.raises(ValueError, match="active or external"):
        SamplePage(page_id="sample_1", title="Unsafe", html=html)


def test_sample_output_caps_total_html_size() -> None:
    pages = [
        SamplePage(page_id=f"sample_{index}", title="Large", html="x" * 130_000)
        for index in range(4)
    ]

    with pytest.raises(ValueError, match="total size limit"):
        SampleOutput(pages=pages)


def test_stale_checkpoint_cannot_mutate(workflow: Workflow) -> None:
    stale = workflow.store.read()["checkpoint_id"]
    workflow.start_clarification(stale)

    with pytest.raises(ConflictError, match="stale_revision"):
        workflow.start_clarification(stale)


def test_editing_approved_narrative_invalidates_outline(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = workflow.answer_clarification(manifest["checkpoint_id"], card["question_card_id"], {q["question_id"]: "answer" for q in card["questions"]})
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document("narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"])
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])

    manifest = workflow.edit_document("narrative_structure", manifest["checkpoint_id"], "# 新叙事")

    assert manifest["state"] == "narrative_structure"
    assert manifest["documents"]["slide_outline"][-1]["status"] == "stale"
    assert manifest["documents"]["narrative_structure"][-1]["revision"] == 2
    assert manifest["documents"]["narrative_structure"][-1]["provenance"]["output_hash"] == digest("# 新叙事")


def test_old_question_card_is_rejected(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])

    with pytest.raises(ConflictError, match="stale_question_card"):
        workflow.answer_clarification(manifest["checkpoint_id"], "questions_old", {})


def test_branch_pointer_tracks_latest_checkpoint(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    assert manifest["branches"]["main"] == manifest["checkpoint_id"]

    branched = workflow.store.fork(manifest["checkpoint_id"], "alternate")
    assert branched["branch"] == "alternate"
    assert branched["branches"]["alternate"] == branched["checkpoint_id"]


def test_branch_switch_restores_branch_head_without_rewriting_history(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    main = workflow.start_clarification(manifest["checkpoint_id"])
    main_head = main["checkpoint_id"]
    alternate = workflow.store.fork(main_head, "alternate")
    alternate_head = alternate["checkpoint_id"]

    switched = workflow.store.switch_branch(main_head)
    assert switched["branch"] == "main"
    assert switched["checkpoint_id"] == main_head
    assert switched["branches"] == {"main": main_head, "alternate": alternate_head}

    view = workflow.store.branches_view()
    assert view["current"] == "main"
    assert {item["name"] for item in view["items"]} == {"main", "alternate"}
    assert next(item for item in view["items"] if item["name"] == "alternate")["head_checkpoint_id"] == alternate_head

    switched_back = workflow.store.switch_branch(alternate_head)
    assert switched_back["branch"] == "alternate"
    assert switched_back["checkpoint_id"] == alternate_head


def test_progress_snapshots_drive_stage_rerun_branch(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = workflow.answer_clarification(
        manifest["checkpoint_id"],
        card["question_card_id"],
        {question["question_id"]: "answer" for question in card["questions"]},
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"]
    )
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    completed = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], outline["revision_hash"]
    )

    snapshots = workflow.store.progress_snapshots()
    assert [item["stage"] for item in snapshots] == [
        "intake", "intake_clarify", "narrative_structure", "slide_outline", "ppt_sample"
    ]
    assert [item["completed"] for item in snapshots] == [True, True, True, True, False]
    narrative_snapshot = next(item for item in snapshots if item["stage"] == "narrative_structure")
    assert narrative_snapshot["snapshot"]["documents"]["narrative_structure"][-1]["status"] == "approved"

    branched = workflow.store.fork(
        narrative_snapshot["checkpoint_id"],
        "narrative-rerun",
        mode="rerun_stage",
        stage="narrative_structure",
    )

    assert branched["state"] == "narrative_structure"
    assert branched["phase"] == "ready_to_generate"
    assert branched["documents"] == {"narrative_structure": [], "slide_outline": []}
    assert branched["samples"] == []
    assert branched["clarification_answers"]
    assert branched["branches"]["main"] == completed["checkpoint_id"]
    assert branched["branch_meta"]["narrative-rerun"]["mode"] == "rerun_stage"
    assert branched["branch_meta"]["narrative-rerun"]["source_stage"] == "narrative_structure"

    branched_progress = workflow.store.progress_snapshots()
    assert [item["stage"] for item in branched_progress] == [
        "intake", "intake_clarify", "narrative_structure"
    ]
    assert [item["completed"] for item in branched_progress] == [True, True, False]


def test_completed_outline_from_previous_release_can_enter_sample_stage(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = workflow.answer_clarification(
        manifest["checkpoint_id"],
        card["question_card_id"],
        {question["question_id"]: "answer" for question in card["questions"]},
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"]
    )
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], outline["revision_hash"]
    )
    legacy = workflow.store.update(
        lambda value: value | {"state": "slide_outline", "phase": "completed"},
        "legacy_fixture",
        {},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )

    assert "start_sample_stage" in capabilities(legacy)
    migrated = workflow.start_sample_stage(legacy["checkpoint_id"])

    assert migrated["state"] == "ppt_sample"
    assert migrated["phase"] == "ready_to_generate"


def test_concurrent_edits_from_same_checkpoint_use_atomic_cas(workflow: Workflow, monkeypatch) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = workflow.answer_clarification(
        manifest["checkpoint_id"],
        card["question_card_id"],
        {question["question_id"]: "answer" for question in card["questions"]},
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    shared_checkpoint = manifest["checkpoint_id"]

    original_require = workflow._require
    both_validated = Barrier(2)

    def synchronized_require(value, capability, checkpoint_id=None):
        original_require(value, capability, checkpoint_id)
        if capability == "edit_narrative":
            both_validated.wait(timeout=5)

    monkeypatch.setattr(workflow, "_require", synchronized_require)

    def save(markdown: str):
        try:
            return workflow.edit_document("narrative_structure", shared_checkpoint, markdown)
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["# 并发版本 A", "# 并发版本 B"]))

    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert str(next(result for result in results if isinstance(result, ConflictError))) == "stale_revision"
    history = workflow.store.read()["documents"]["narrative_structure"]
    assert [revision["revision"] for revision in history] == [1, 2]
    assert history[1]["parent_revision_hash"] == history[0]["revision_hash"]


def test_generation_provenance_hashes_skill_index_reads_and_output(workflow: Workflow, monkeypatch) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = workflow.answer_clarification(
        manifest["checkpoint_id"],
        card["question_card_id"],
        {question["question_id"]: "answer" for question in card["questions"]},
    )
    traces = [
        {"type": "model_call", "provider": "test", "model": "test-model", "usage": {}},
        {
            "type": "tool_call",
            "tool": "read",
            "path": "narrative-structure/SKILL.md",
            "content_hash": "sha256:skill-content",
            "offset": 0,
            "end": 128,
        },
    ]
    monkeypatch.setattr(workflow.gateway, "generate", lambda *args, **kwargs: ("# 可复现叙事", traces))

    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    document = manifest["documents"]["narrative_structure"][-1]
    provenance = document["provenance"]

    assert provenance["skills_hash"] == stable_hash(provenance["skill_index"])
    assert provenance["skill_reads"] == [{
        "path": "narrative-structure/SKILL.md",
        "content_hash": "sha256:skill-content",
        "offset": 0,
        "end": 128,
    }]
    assert provenance["skill_reads_hash"] == stable_hash(provenance["skill_reads"])
    assert provenance["output_hash"] == digest("# 可复现叙事")
