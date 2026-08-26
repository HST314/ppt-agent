from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_core.full_deck_generation as full_deck_generation
import main_front
from agent_core.full_deck_generation import FullDeckGenerationError
from agent_core.jobs import JobRegistry
from agent_core.models import (
    DocumentRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
    utc_now,
)
from agent_core.workflow import Workflow, capabilities
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore


OUTLINE = """# 逐页大纲

## 第 1 页｜开场
## 第 2 页｜关键判断
## 第 3 页｜数据证据
## 第 4 页｜行动计划
"""

SIXTEEN_PAGE_OUTLINE = "# 逐页大纲\n\n" + "\n".join(
    f"## 第 {number} 页｜页面 {number}\n- 本页目的：推进第 {number} 个叙事节点"
    for number in range(1, 17)
)


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="阶段五样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-2", title="关键判断", source_slide_number=2),
            PackageSlide(slide_id="sample-3", title="数据证据", source_slide_number=3),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><head><link rel="stylesheet" '
                    'href="assets/deck.css"></head><body>'
                    '<section class="slide" data-slide-id="sample-2">'
                    '<h1>关键判断</h1></section><section class="slide" '
                    'data-slide-id="sample-3"><h1>数据证据</h1></section>'
                    '<script src="assets/deck.js"></script></body></html>'
                ),
            ),
            PackageFile(path="assets/deck.css", content=".slide{width:100vw;height:100vh}"),
            PackageFile(path="assets/deck.js", content="addEventListener('keydown',()=>{})"),
        ],
    )


def _leading_sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="前两页样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-1", title="页面 1", source_slide_number=1),
            PackageSlide(slide_id="sample-2", title="页面 2", source_slide_number=2),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    'data-slide-id="sample-1"><h1>页面 1</h1></section>'
                    '<section class="slide" data-slide-id="sample-2">'
                    '<h1>页面 2</h1></section></body></html>'
                ),
            ),
        ],
    )


def _sample_project(
    root: Path,
    runtime: ManagedRuntime,
    project_id: str,
    *,
    outline_markdown: str = OUTLINE,
    sample_package: HtmlPptPackage | None = None,
) -> tuple[Workflow, dict]:
    store = ProjectStore(root, project_id)
    manifest = store.create(
        TaskCard(title="阶段五验收", objective="验证全稿确认闭环").model_dump(),
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
        sample_package or _sample_package(),
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
        lambda value: value | {
            "documents": {
                "narrative_structure": [],
                "slide_outline": [outline.model_dump(mode="json")],
            },
            "samples": [sample.model_dump(mode="json")],
            "current_sample_revision_hash": sample.revision_hash,
            "state": "ppt_sample",
            "phase": "waiting_human_approval",
        },
        "phase5_fixture_ready",
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    return Workflow(store, runtime), manifest


def _complete_full_deck(
    root: Path,
    runtime: ManagedRuntime,
    project_id: str = "phase5-deck",
) -> tuple[Workflow, dict, dict]:
    workflow, sample_manifest = _sample_project(root, runtime, project_id)
    entered = workflow.enter_full_deck(
        sample_manifest["checkpoint_id"],
        sample_manifest["current_sample_revision_hash"],
    )
    completed = workflow.generate_full_deck(entered["checkpoint_id"])
    return workflow, entered, completed


def test_failed_read_trace_does_not_block_valid_fourteen_page_generation(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, sample_manifest = _sample_project(
        tmp_path / "projects",
        mock_runtime,
        "phase5-failed-read",
        outline_markdown=SIXTEEN_PAGE_OUTLINE,
        sample_package=_leading_sample_package(),
    )
    entered = workflow.enter_full_deck(
        sample_manifest["checkpoint_id"],
        sample_manifest["current_sample_revision_hash"],
    )
    original_generate = workflow.gateway.generate
    failed_read = {
        "type": "tool_call",
        "tool": "read",
        "error": "path_not_found",
        "round": 1,
    }
    successful_read = {
        "type": "tool_call",
        "tool": "read",
        "path": "ppt-layout/SKILL.md",
        "content_hash": "sha256:successful-read",
        "offset": 0,
        "end": 96,
        "round": 1,
    }

    def generate_with_read_traces(state: str, prompt: str, **kwargs):
        output, traces = original_generate(state, prompt, **kwargs)
        if state == "ppt_full":
            traces.extend([failed_read, successful_read])
        return output, traces

    workflow.gateway.generate = generate_with_read_traces
    completed = workflow.generate_full_deck(entered["checkpoint_id"])

    revision = completed["full_deck_revisions"][-1]
    assert revision["package"]["slide_count"] == 16
    assert revision["provenance"]["segments"][0]["target_slide_numbers"] == list(
        range(3, 17)
    )
    assert revision["provenance"]["skill_reads"] == [{
        "path": successful_read["path"],
        "content_hash": successful_read["content_hash"],
        "offset": successful_read["offset"],
        "end": successful_read["end"],
    }]
    prompt_calls = workflow.store.prompt_calls()
    assert prompt_calls
    assert all(call["status"] == "completed" for call in prompt_calls)
    assert all(call["status"] != "started" for call in prompt_calls)
    assert failed_read in prompt_calls[-1]["tool_calls"]


@pytest.mark.parametrize("failure_site", ["provenance", "store_update"])
def test_finalization_failures_close_every_started_prompt_call(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    workflow, sample_manifest = _sample_project(
        tmp_path / "projects",
        mock_runtime,
        "phase5-finalization-failure",
    )
    entered = workflow.enter_full_deck(
        sample_manifest["checkpoint_id"],
        sample_manifest["current_sample_revision_hash"],
    )

    def fail_finalization(*args, **kwargs):
        raise RuntimeError(f"injected {failure_site} failure")

    if failure_site == "provenance":
        monkeypatch.setattr(
            full_deck_generation,
            "generation_provenance",
            fail_finalization,
        )
    else:
        monkeypatch.setattr(workflow.store, "update", fail_finalization)

    with pytest.raises(FullDeckGenerationError) as error:
        workflow.generate_full_deck(entered["checkpoint_id"])

    assert error.value.public_code == "full_deck_finalization_failed"
    unchanged = workflow.store.read(include_sample_html=False)
    assert len(unchanged["full_deck_revisions"]) == 1
    prompt_calls = workflow.store.prompt_calls()
    assert prompt_calls
    assert all(call["status"] == "failed" for call in prompt_calls)
    assert all(call["status"] != "started" for call in prompt_calls)
    assert {
        call["error"]["code"] for call in prompt_calls
    } == {"full_deck_finalization_failed"}


def test_approve_full_deck_is_atomic_and_completes_progress_snapshot(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered, completed = _complete_full_deck(
        tmp_path / "projects",
        mock_runtime,
    )
    revision_hash = completed["full_deck"]["current_revision_hash"]

    with pytest.raises(ConflictError, match="stale_revision"):
        workflow.approve_full_deck(
            completed["checkpoint_id"],
            entered["full_deck"]["current_revision_hash"],
        )

    approved = workflow.approve_full_deck(completed["checkpoint_id"], revision_hash)

    assert approved["state"] == "acceptance"
    assert approved["phase"] == "ready_for_review"
    assert approved["full_deck"]["current_revision_hash"] == revision_hash
    assert approved["full_deck"]["revision_refs"][-1]["status"] == "approved"
    assert approved["full_deck_revisions"][-1]["status"] == "approved"
    assert "approve_full_deck" not in capabilities(approved)
    assert {
        "inspect_full_deck",
        "revise_full_deck",
        "restore_full_deck_revision",
        "branch_full_deck_revision",
    } <= set(capabilities(approved))

    snapshots = workflow.store.progress_snapshots()
    full_deck_snapshot = next(item for item in snapshots if item["stage"] == "ppt_full")
    acceptance_snapshot = next(item for item in snapshots if item["stage"] == "acceptance")
    assert full_deck_snapshot["completed"] is True
    assert full_deck_snapshot["snapshot"]["full_deck_revisions"][0]["status"] == "approved"
    assert acceptance_snapshot["phase"] == "ready_for_review"
    assert acceptance_snapshot["completed"] is False

    event = workflow.store.events()[-1]
    assert event == {
        "at": event["at"],
        "event": "full_deck_approved",
        "checkpoint_id": approved["checkpoint_id"],
        "full_deck_id": approved["full_deck"]["full_deck_id"],
        "revision_hash": revision_hash,
        "package_hash": approved["full_deck_revisions"][-1]["package"]["package_hash"],
        "page_count": 4,
    }
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM full_deck_revisions WHERE revision_hash = ?",
            (revision_hash,),
        ).fetchone()[0] == "approved"
        persisted = json.loads(connection.execute(
            "SELECT payload_json FROM project_state WHERE singleton = 1"
        ).fetchone()[0])
        assert persisted["state"] == "acceptance"
        assert persisted["phase"] == "ready_for_review"


def test_incomplete_or_failed_approval_never_moves_the_project(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, sample_manifest = _sample_project(
        tmp_path / "projects",
        mock_runtime,
        "phase5-rollback",
    )
    entered = workflow.enter_full_deck(
        sample_manifest["checkpoint_id"],
        sample_manifest["current_sample_revision_hash"],
    )
    with pytest.raises(ConflictError, match="full_deck_incomplete"):
        workflow.approve_full_deck(
            entered["checkpoint_id"],
            entered["full_deck"]["current_revision_hash"],
        )

    completed = workflow.generate_full_deck(entered["checkpoint_id"])
    revision_hash = completed["full_deck"]["current_revision_hash"]
    original_sync = workflow.store._sync_revisions

    def fail_after_projection(connection, projection, checkpoint_id):
        original_sync(connection, projection, checkpoint_id)
        if projection.get("state") == "acceptance":
            raise RuntimeError("injected acceptance transaction failure")

    monkeypatch.setattr(workflow.store, "_sync_revisions", fail_after_projection)
    with pytest.raises(RuntimeError, match="injected acceptance transaction failure"):
        workflow.approve_full_deck(completed["checkpoint_id"], revision_hash)

    persisted = workflow.store.read(include_sample_html=False)
    assert persisted["checkpoint_id"] == completed["checkpoint_id"]
    assert persisted["state"] == "ppt_full"
    assert persisted["phase"] == "waiting_human_approval"
    assert persisted["full_deck_revisions"][-1]["status"] == "pending_approval"
    assert not any(
        item["event"] == "full_deck_approved"
        for item in workflow.store.events()
    )
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM full_deck_revisions WHERE revision_hash = ?",
            (revision_hash,),
        ).fetchone()[0] == "pending_approval"


def test_approved_revision_accepts_feedback_as_an_immutable_child(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, _, completed = _complete_full_deck(
        tmp_path / "projects",
        mock_runtime,
        "phase5-followup",
    )
    parent_hash = completed["full_deck"]["current_revision_hash"]
    approved = workflow.approve_full_deck(completed["checkpoint_id"], parent_hash)

    revised = workflow.revise_full_deck(
        approved["checkpoint_id"],
        parent_hash,
        "第 3 页把核心结论提前，其他页面保持不变。",
    )

    child = revised["full_deck_revisions"][-1]
    assert revised["state"] == "ppt_full"
    assert revised["phase"] == "waiting_human_approval"
    assert child["status"] == "pending_approval"
    assert child["parent_revision_hash"] == parent_hash
    assert revised["full_deck_revisions"][-2]["status"] == "approved"
    assert revised["full_deck"]["revision_refs"][-2]["status"] == "approved"
    assert revised["full_deck"]["current_revision_hash"] == child["revision_hash"]


def test_acceptance_api_exports_review_evidence_and_branches_from_baseline(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, _, completed = _complete_full_deck(
        project_root,
        mock_runtime,
        "phase5-api",
    )
    revision_hash = completed["full_deck"]["current_revision_hash"]
    client = TestClient(main_front.app)

    approved_response = client.post(
        "/api/projects/phase5-api/full-deck/approve",
        json={
            "checkpoint_id": completed["checkpoint_id"],
            "revision_hash": revision_hash,
        },
    )
    assert approved_response.status_code == 200
    approved = approved_response.json()
    assert approved["state"] == "acceptance"
    assert approved["phase"] == "ready_for_review"
    assert approved["audit_export_url"] == "/api/projects/phase5-api/audit/export"
    assert "revise_full_deck" in approved["capabilities"]

    detail = client.get(
        f"/api/projects/phase5-api/full-deck/revisions/{revision_hash}"
    )
    exported = client.get(approved["full_deck_revision"]["export_url"])
    timeline = client.get("/api/projects/phase5-api/timeline")
    audit = client.get(approved["audit_export_url"])
    assert detail.status_code == 200
    assert detail.json()["status"] == "approved"
    assert detail.json()["current"] is True
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert timeline.status_code == 200
    assert timeline.json()[-1]["event"] == "full_deck_approved"
    assert audit.status_code == 200
    assert audit.headers["content-disposition"] == 'attachment; filename="phase5-api-audit.json"'
    evidence = audit.json()
    assert evidence["format"] == "ppt-agent-audit-v1"
    assert evidence["project"]["state"] == "acceptance"
    assert evidence["full_deck"]["current_revision_hash"] == revision_hash
    assert evidence["full_deck_revisions"][0]["status"] == "approved"
    assert any(
        item["stage"] == "ppt_full" and item["completed"]
        for item in evidence["progress_snapshots"]
    )
    assert evidence["timeline"][-1]["event"] == "full_deck_approved"
    assert evidence["prompt_calls"]

    branched = client.post(
        f"/api/projects/phase5-api/full-deck/revisions/{revision_hash}/branches",
        json={
            "checkpoint_id": approved["checkpoint_id"],
            "name": "accepted-baseline",
        },
    )
    assert branched.status_code == 200
    assert branched.json()["branch"] == "accepted-baseline"
    assert branched.json()["full_deck_revision"]["revision_hash"] == revision_hash
    assert branched.json()["full_deck_revision"]["status"] == "approved"


def test_legacy_sample_project_migrates_and_remains_reviewable_exportable(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    project_id = "legacy-phase5"
    project_path = project_root / project_id
    checkpoint_id = "checkpoint_0123456789abcdef01234567"
    created_at = utc_now()
    sample = SampleRevision.create_package(
        _sample_package(),
        revision=1,
        parent=None,
        feedback=None,
    )
    manifest = {
        "project_id": project_id,
        "title": "旧样品工程",
        "branch": "main",
        "branches": {"main": checkpoint_id},
        "branch_meta": {
            "main": {
                "parent": None,
                "from_checkpoint": None,
                "created_at": created_at,
            }
        },
        "state": "ppt_sample",
        "phase": "waiting_human_approval",
        "task_card": TaskCard(title="旧样品工程", objective="迁移并复核").model_dump(),
        "clarification_answers": {},
        "question_card": None,
        "documents": {"narrative_structure": [], "slide_outline": []},
        "samples": [sample.model_dump(mode="json")],
        "current_sample_revision_hash": sample.revision_hash,
        "checkpoint_id": checkpoint_id,
        "active_job_id": None,
        "created_at": created_at,
        "updated_at": created_at,
        "runtime": mock_runtime.snapshot(),
    }
    (project_path / "checkpoints").mkdir(parents=True)
    (project_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    (project_path / "checkpoints" / f"{checkpoint_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    (project_path / "events.jsonl").write_text(
        json.dumps({
            "at": created_at,
            "event": "sample_generated",
            "checkpoint_id": checkpoint_id,
            "revision_hash": sample.revision_hash,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)

    opened = client.get(f"/api/projects/{project_id}")
    history = client.get(f"/api/projects/{project_id}/samples/revisions")
    exported = client.get(
        f"/api/projects/{project_id}/samples/revisions/{sample.revision_hash}/export"
    )
    branch = client.post(
        f"/api/projects/{project_id}/branches",
        json={
            "checkpoint_id": checkpoint_id,
            "name": "legacy-review",
            "mode": "fork_after",
        },
    )

    assert opened.status_code == 200
    assert opened.json()["state"] == "ppt_sample"
    assert opened.json()["full_deck"] is None
    assert history.status_code == 200
    assert history.json()[0]["revision_hash"] == sample.revision_hash
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert branch.status_code == 200
    assert branch.json()["branch"] == "legacy-review"
    migrated = ProjectStore(project_root, project_id)
    assert migrated.database_path.is_file()
    assert migrated.events()[-1]["event"] == "branch_created"
