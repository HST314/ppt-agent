from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.full_deck_generation import FullDeckGenerationError
from agent_core.jobs import ActiveJobError, JobCancelled, JobRegistry
from agent_core.models import (
    DocumentRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
)
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore
from tests.job_support import wait_for_terminal_job


OUTLINE = "# 逐页大纲\n\n" + "\n".join(
    f"## 第 {number} 页｜页面 {number}\n- 本页目的：推进第 {number} 个叙事节点"
    for number in range(1, 7)
)


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="中段样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-3", title="页面 3", source_slide_number=3),
            PackageSlide(slide_id="sample-4", title="页面 4", source_slide_number=4),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><head><link rel="stylesheet" '
                    'href="assets/deck.css"></head><body><section class="slide" '
                    'data-slide-id="sample-3"><h1>页面 3</h1></section>'
                    '<section class="slide" data-slide-id="sample-4">'
                    '<h1>页面 4</h1></section></body></html>'
                ),
            ),
            PackageFile(path="assets/deck.css", content=".slide{padding:4rem}"),
        ],
    )


def _ready_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
    *,
    project_id: str = "phase4-deck",
) -> tuple[Workflow, dict]:
    store = ProjectStore(tmp_path / "projects", project_id)
    manifest = store.create(
        TaskCard(title="六页完整演示", objective="验证全稿版本化修改").model_dump(),
        runtime.snapshot(),
    )
    outline = DocumentRevision.create(
        "slide_outline",
        OUTLINE,
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
        manifest["checkpoint_id"], sample.revision_hash
    )
    return workflow, entered


def _revision(manifest: dict, revision_hash: str) -> dict:
    return next(
        item
        for item in manifest["full_deck_revisions"]
        if item["revision_hash"] == revision_hash
    )


def _wait_for_job(registry: JobRegistry, job_id: str) -> dict:
    return wait_for_terminal_job(
        registry.get,
        job_id,
        fetch_events=registry.events,
    )


def test_feedback_can_replace_a_sample_page_without_changing_other_refs(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    generated = workflow.generate_full_deck(entered["checkpoint_id"])
    parent_hash = generated["full_deck"]["current_revision_hash"]
    parent = _revision(generated, parent_hash)

    revised = workflow.revise_full_deck(
        generated["checkpoint_id"],
        parent_hash,
        "第 3 页把核心结论提前，并保持其余页面不变。",
    )
    child = _revision(revised, revised["full_deck"]["current_revision_hash"])
    parent_pages = {page["slot_id"]: page for page in parent["plan"]["pages"]}
    child_pages = {page["slot_id"]: page for page in child["plan"]["pages"]}
    changed_slot = next(
        page["slot_id"]
        for page in parent["plan"]["pages"]
        if page["outline_ref"]["source_slide_number"] == 3
    )

    assert child["parent_revision_hash"] == parent_hash
    assert child["provenance"]["changed_slot_ids"] == [changed_slot]
    assert child_pages[changed_slot]["source_type"] == "full_deck_edit"
    assert child_pages[changed_slot]["content_ref"] != parent_pages[changed_slot]["content_ref"]
    assert child_pages[changed_slot]["derived_from"] == parent_pages[changed_slot]["derived_from"]
    assert all(
        child_pages[slot_id]["content_ref"] == page["content_ref"]
        for slot_id, page in parent_pages.items()
        if slot_id != changed_slot
    )
    assert workflow.store.full_deck_package_files(parent_hash)
    assert workflow.store.events()[-1]["event"] == "full_deck_revised"
    prompt = workflow.store.prompt_calls()[-1]
    assert prompt["parameters"]["changed_slot_ids"] == [changed_slot]
    assert prompt["output_ref"] == child["revision_hash"]


def test_restored_r1_and_r2_each_form_a_child_and_new_revision_can_branch(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    r1_hash = entered["full_deck"]["current_revision_hash"]
    generated = workflow.generate_full_deck(entered["checkpoint_id"])
    r2_hash = generated["full_deck"]["current_revision_hash"]

    selected_r1 = workflow.restore_full_deck(generated["checkpoint_id"], r1_hash)
    from_r1 = workflow.revise_full_deck(
        selected_r1["checkpoint_id"], r1_hash, "第 3 页改为结论先行。"
    )
    r3_hash = from_r1["full_deck"]["current_revision_hash"]
    r3 = _revision(from_r1, r3_hash)
    assert r3["parent_revision_hash"] == r1_hash
    assert r3["package"]["slide_count"] == 6
    assert {
        page["outline_ref"]["source_slide_number"]
        for page in r3["plan"]["pages"]
        if page["slot_id"] in r3["provenance"]["changed_slot_ids"]
    } == {1, 2, 3, 5, 6}

    selected_r2 = workflow.restore_full_deck(from_r1["checkpoint_id"], r2_hash)
    from_r2 = workflow.revise_full_deck(
        selected_r2["checkpoint_id"], r2_hash, "第 2 页强化数据证据。"
    )
    r4_hash = from_r2["full_deck"]["current_revision_hash"]
    assert _revision(from_r2, r4_hash)["parent_revision_hash"] == r2_hash

    source_checkpoint = workflow.store.full_deck_revision_checkpoint(r3_hash)
    branched = workflow.store.fork(
        source_checkpoint,
        "from-r3",
        full_deck_revision_hash=r3_hash,
    )
    assert branched["branch"] == "from-r3"
    assert branched["full_deck"]["current_revision_hash"] == r3_hash


def test_regeneration_replaces_only_non_sample_pages(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    generated = workflow.generate_full_deck(entered["checkpoint_id"])
    parent_hash = generated["full_deck"]["current_revision_hash"]
    parent = _revision(generated, parent_hash)
    regenerated = workflow.regenerate_full_deck(
        generated["checkpoint_id"], parent_hash
    )
    child = _revision(
        regenerated, regenerated["full_deck"]["current_revision_hash"]
    )
    parent_by_number = {
        page["outline_ref"]["source_slide_number"]: page
        for page in parent["plan"]["pages"]
    }
    child_by_number = {
        page["outline_ref"]["source_slide_number"]: page
        for page in child["plan"]["pages"]
    }

    assert child["parent_revision_hash"] == parent_hash
    assert child["provenance"]["operation"] == "regenerate_full_deck"
    assert child["provenance"]["changed_source_slide_numbers"] == [1, 2, 5, 6]
    assert all(
        child_by_number[number]["content_ref"] == parent_by_number[number]["content_ref"]
        for number in (3, 4)
    )
    assert all(
        child_by_number[number]["content_ref"] != parent_by_number[number]["content_ref"]
        for number in (1, 2, 5, 6)
    )
    assert sum(
        item["path"].endswith("/assets/deck.css")
        for item in child["package"]["files"]
    ) == 1


def test_failure_cancellation_and_cas_conflict_leave_pointer_unchanged(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    generated = workflow.generate_full_deck(entered["checkpoint_id"])
    parent_hash = generated["full_deck"]["current_revision_hash"]

    original_generate = workflow.gateway.generate
    workflow.gateway.generate = lambda *_args, **_kwargs: (
        json.dumps({
            "changed_slot_ids": ["slot_" + "f" * 24],
            "changed_source_slide_numbers": [99],
            "source_slide_numbers": [99],
        }),
        [],
    )
    with pytest.raises(FullDeckGenerationError, match="不存在"):
        workflow.regenerate_full_deck(generated["checkpoint_id"], parent_hash)
    assert workflow.store.read()["full_deck"]["current_revision_hash"] == parent_hash

    workflow.gateway.generate = original_generate
    with pytest.raises(JobCancelled):
        workflow.revise_full_deck(
            generated["checkpoint_id"],
            parent_hash,
            "第 3 页调整标题。",
            cancel_requested=lambda: True,
        )
    assert workflow.store.read()["full_deck"]["current_revision_hash"] == parent_hash

    changed_checkpoint = None

    def race(state: str, prompt: str, **kwargs):
        nonlocal changed_checkpoint
        result = original_generate(state, prompt, **kwargs)
        if changed_checkpoint is None:
            concurrent = workflow.store.update(
                lambda value: value,
                "concurrent_change",
                expected_checkpoint_id=generated["checkpoint_id"],
            )
            changed_checkpoint = concurrent["checkpoint_id"]
        return result

    workflow.gateway.generate = race
    with pytest.raises(ConflictError, match="stale_revision"):
        workflow.revise_full_deck(
            generated["checkpoint_id"], parent_hash, "第 3 页调整标题。"
        )
    unchanged = workflow.store.read()
    assert unchanged["checkpoint_id"] == changed_checkpoint
    assert unchanged["full_deck"]["current_revision_hash"] == parent_hash
    assert workflow.store.prompt_calls()[-1]["status"] == "conflicted"
    assert workflow.store.prompt_calls()[-1]["output_ref"] is None


def test_phase4_job_api_is_strict_cancellable_and_idempotent(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _ready_full_deck(
        tmp_path, mock_runtime, project_id="phase4-api"
    )
    generated = workflow.generate_full_deck(entered["checkpoint_id"])
    parent_hash = generated["full_deck"]["current_revision_hash"]
    client = TestClient(main_front.app)
    payload = {
        "operation": "revise_full_deck",
        "checkpoint_id": generated["checkpoint_id"],
        "revision_hash": parent_hash,
        "feedback": "第 3 页突出核心结论。",
    }

    missing_revision = client.post(
        "/api/projects/phase4-api/jobs",
        json={key: value for key, value in payload.items() if key != "revision_hash"},
    )
    assert missing_revision.status_code == 422
    first = client.post("/api/projects/phase4-api/jobs", json=payload)
    assert first.status_code == 202
    duplicate = client.post("/api/projects/phase4-api/jobs", json=payload)
    assert duplicate.status_code == 202
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert first.json()["cancellable"] is True
    assert first.json()["request_key"].startswith("sha256:")

    terminal = _wait_for_job(registry, first.json()["job_id"])
    assert terminal["status"] == "succeeded"
    refreshed = client.get("/api/projects/phase4-api").json()
    assert refreshed["full_deck_revision"]["parent_revision_hash"] == parent_hash
    assert refreshed["full_deck_revisions"][0]["changed_pages"] == [{
        "slot_id": refreshed["full_deck_revision"]["provenance"]["changed_slot_ids"][0],
        "source_slide_number": 3,
        "title": "页面 3",
    }]


def test_project_rejects_a_distinct_job_while_one_is_active(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "jobs")
    entered = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        entered.set()
        assert release.wait(timeout=5)

    first = registry.submit(
        "phase4-active",
        "revise_full_deck",
        "checkpoint_" + "a" * 24,
        blocking,
        idempotency_key="sha256:" + "1" * 64,
    )
    assert entered.wait(timeout=5)
    with pytest.raises(ActiveJobError, match="active_job"):
        registry.submit(
            "phase4-active",
            "regenerate_full_deck",
            "checkpoint_" + "a" * 24,
            lambda: None,
            idempotency_key="sha256:" + "2" * 64,
        )
    release.set()
    assert _wait_for_job(registry, first["job_id"])["status"] == "succeeded"
