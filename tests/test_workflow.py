from pathlib import Path

import pytest

from agent_core.models import TaskCard
from agent_core.workflow import Workflow, capabilities
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore


@pytest.fixture
def workflow(tmp_path: Path) -> Workflow:
    runtime = ManagedRuntime(Path(__file__).parents[1])
    store = ProjectStore(tmp_path / "projects", "demo")
    task = TaskCard(title="季度复盘", objective="形成下一季度投入共识")
    store.create(task.model_dump(), runtime.snapshot())
    return Workflow(store, runtime)


def test_phase_one_happy_path(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    assert capabilities(manifest) == ["inspect", "branch", "start_clarification"]

    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    assert card["checkpoint_id"] == manifest["checkpoint_id"]

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
    manifest = workflow.approve_document("narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"])
    assert manifest["state"] == "slide_outline"

    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document("slide_outline", manifest["checkpoint_id"], outline["revision_hash"])
    assert manifest["phase"] == "completed"


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
