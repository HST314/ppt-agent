import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main_front
from agent_core.jobs import JobRegistry
from agent_core.models import (
    DocumentRevision,
    FullDeckPageSlot,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
)
from agent_core.workflow import Workflow, capabilities
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore, STAGE_IDS


OUTLINE = """# 逐页大纲

## 第 1 页｜开场
## 第 2 页｜关键判断
## 第 3 页｜数据证据
## 第 4 页｜行动计划
"""


def test_workflow_contract_includes_full_deck_and_acceptance_states() -> None:
    contract = yaml.safe_load(
        (Path(__file__).parents[1] / "workflows" / "ppt_agent_v1.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert STAGE_IDS[-2:] == ("ppt_full", "acceptance")
    assert contract["states"][-2:] == ["ppt_full", "acceptance"]
    assert contract["transitions"]["ppt_sample"] == ["ppt_full"]
    assert contract["transitions"]["ppt_full"] == ["acceptance"]
    assert contract["terminal_states"] == ["acceptance", "blocked", "cancelled"]


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="已确认样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-2", title="关键判断", source_slide_number=2),
            PackageSlide(slide_id="sample-3", title="数据证据", source_slide_number=3),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><head><link rel="stylesheet" href="assets/deck.css">'
                    '</head><body><section class="slide" data-slide-id="sample-2">'
                    '<h1>关键判断</h1></section><section class="slide" data-slide-id="sample-3">'
                    '<h1>数据证据</h1></section><script src="assets/deck.js"></script></body></html>'
                ),
            ),
            PackageFile(path="assets/deck.css", content=".slide{width:100vw;height:100vh}"),
            PackageFile(path="assets/deck.js", content="addEventListener('keydown',()=>{})"),
        ],
    )


def _workflow_ready_to_enter(
    tmp_path: Path,
    runtime: ManagedRuntime,
    *,
    sample_status: str = "pending_approval",
) -> tuple[Workflow, dict]:
    store = ProjectStore(tmp_path / "projects", "full-deck-demo")
    manifest = store.create(
        TaskCard(title="全稿事务", objective="验证原子进入").model_dump(),
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
    ).model_copy(update={"status": sample_status})

    manifest = store.update(
        lambda value: value | {
            "documents": {
                "narrative_structure": [],
                "slide_outline": [outline.model_dump(mode="json")],
            },
            "samples": [sample.model_dump(mode="json")],
            "current_sample_revision_hash": sample.revision_hash,
            "state": "ppt_sample",
            "phase": "completed" if sample_status == "approved" else "waiting_human_approval",
        },
        "sample_fixture_ready",
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    return Workflow(store, runtime), manifest


def test_full_deck_plan_rejects_non_contiguous_positions() -> None:
    with pytest.raises(ValidationError, match="ordered, unique, and contiguous"):
        FullDeckPlan(pages=[
            FullDeckPageSlot(
                slot_id="slot_" + "a" * 24,
                position=1,
                title="待生成页",
                status="pending",
                source_type="pending",
            )
        ])


def test_full_deck_revision_hash_binds_ordered_plan_and_provenance() -> None:
    plan = FullDeckPlan(pages=[
        FullDeckPageSlot(
            slot_id="slot_" + "b" * 24,
            position=0,
            title="待生成页",
            status="pending",
            source_type="pending",
        )
    ])
    revision = FullDeckRevision.create(
        full_deck_id="deck_" + "c" * 24,
        revision=1,
        parent=None,
        feedback="初始化",
        plan=plan,
        provenance={"outline_revision_hash": "sha256:" + "d" * 64},
    )

    with pytest.raises(ValidationError, match="hash does not match"):
        FullDeckRevision.model_validate(
            revision.model_dump(mode="json") | {"feedback": "被篡改"}
        )


@pytest.mark.parametrize("sample_status", ["pending_approval", "approved"])
def test_enter_full_deck_initializes_r1_and_audit_events_atomically(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    sample_status: str,
) -> None:
    workflow, before = _workflow_ready_to_enter(
        tmp_path,
        mock_runtime,
        sample_status=sample_status,
    )
    sample_hash = before["current_sample_revision_hash"]

    entered = workflow.enter_full_deck(before["checkpoint_id"], sample_hash)

    assert entered["state"] == "ppt_full"
    assert entered["phase"] == "ready_to_generate"
    assert entered["current_sample_revision_hash"] == sample_hash
    assert entered["samples"][0]["status"] == "approved"
    root = entered["full_deck"]
    revision = entered["full_deck_revisions"][0]
    assert root["approved_sample_revision_hash"] == sample_hash
    assert root["outline_revision_hash"] == entered["documents"]["slide_outline"][-1]["revision_hash"]
    assert root["current_revision_hash"] == revision["revision_hash"]
    assert revision["revision"] == 1
    assert revision["parent_revision_hash"] is None
    assert revision["status"] == "draft"
    assert revision["package"] is None
    assert len(revision["plan"]["pages"]) == 4
    assert [page["position"] for page in revision["plan"]["pages"]] == [0, 1, 2, 3]
    assert [
        page["outline_ref"]["source_slide_number"]
        for page in revision["plan"]["pages"]
    ] == [1, 2, 3, 4]
    assert [page["status"] for page in revision["plan"]["pages"]] == [
        "pending", "ready", "ready", "pending"
    ]
    assert [page["source_type"] for page in revision["plan"]["pages"]] == [
        "pending", "approved_sample", "approved_sample", "pending"
    ]
    for page in revision["plan"]["pages"][1:3]:
        assert page["content_ref"]["revision_hash"] == sample_hash
        assert page["content_ref"]["slide_content_hash"].startswith("sha256:")
        assert page["derived_from"]["sample_revision_hash"] == sample_hash

    caps = capabilities(entered)
    assert "enter_full_deck" not in caps
    assert {"generate_full_deck", "inspect_full_deck", "revise_full_deck"} <= set(caps)
    assert "generate_full_deck" not in capabilities(entered, active_job=True)

    events = workflow.store.events()
    assert [item["event"] for item in events[-2:]] == [
        "sample_approved", "full_deck_initialized"
    ]
    assert {item["checkpoint_id"] for item in events[-2:]} == {entered["checkpoint_id"]}
    assert events[-1]["page_count"] == 4
    assert events[-1]["ready_page_count"] == 2

    with sqlite3.connect(workflow.store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM full_deck_pages").fetchone()[0] == 4
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM project_state WHERE singleton = 1"
        ).fetchone()[0])
    assert "full_deck_revisions" not in payload
    assert payload["full_deck"]["revision_refs"] == root["revision_refs"]


def test_enter_full_deck_rejects_duplicate_and_stale_requests(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, before = _workflow_ready_to_enter(tmp_path, mock_runtime)
    sample_hash = before["current_sample_revision_hash"]
    entered = workflow.enter_full_deck(before["checkpoint_id"], sample_hash)

    with pytest.raises(ConflictError, match="stale_revision"):
        workflow.enter_full_deck(before["checkpoint_id"], sample_hash)
    with pytest.raises(ConflictError, match="full_deck_already_initialized"):
        workflow.enter_full_deck(entered["checkpoint_id"], sample_hash)

    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 1


def test_full_deck_enter_api_returns_current_projection_and_strict_conflicts(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, before = _workflow_ready_to_enter(tmp_path, mock_runtime)
    client = TestClient(main_front.app)
    payload = {
        "checkpoint_id": before["checkpoint_id"],
        "sample_revision_hash": before["current_sample_revision_hash"],
    }

    response = client.post("/api/projects/full-deck-demo/full-deck/enter", json=payload)

    assert response.status_code == 200
    project = response.json()
    assert project["state"] == "ppt_full"
    assert project["full_deck_revision"]["revision"] == 1
    assert project["full_deck_revisions"][0]["current"] is True
    assert project["full_deck_attempts"] == []
    assert "generate_full_deck" in project["capabilities"]

    stale = client.post("/api/projects/full-deck-demo/full-deck/enter", json=payload)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    unknown = client.post(
        "/api/projects/full-deck-demo/full-deck/enter",
        json=payload | {"confirm": True},
    )
    assert unknown.status_code == 422


def test_full_deck_enter_api_rejects_active_job(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, before = _workflow_ready_to_enter(tmp_path, mock_runtime)

    @contextmanager
    def active_guard(_project_id):
        yield {"job_id": "job_active", "status": "running"}

    monkeypatch.setattr(registry, "project_guard", active_guard)
    response = TestClient(main_front.app).post(
        "/api/projects/full-deck-demo/full-deck/enter",
        json={
            "checkpoint_id": before["checkpoint_id"],
            "sample_revision_hash": before["current_sample_revision_hash"],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_job"


def test_concurrent_enter_full_deck_only_commits_one_r1(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, before = _workflow_ready_to_enter(tmp_path, mock_runtime)
    sample_hash = before["current_sample_revision_hash"]
    competitors = [
        workflow,
        Workflow(
            ProjectStore(workflow.store.projects_root, workflow.store.project_id),
            mock_runtime,
        ),
    ]

    def enter(candidate: Workflow):
        try:
            return candidate.enter_full_deck(before["checkpoint_id"], sample_hash)
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(enter, competitors))

    assert len([item for item in outcomes if isinstance(item, dict)]) == 1
    conflicts = [str(item) for item in outcomes if isinstance(item, ConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].startswith("stale_revision:")
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event = 'full_deck_initialized'"
        ).fetchone()[0] == 1


def test_enter_full_deck_storage_failure_rolls_back_every_database_change(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, before = _workflow_ready_to_enter(tmp_path, mock_runtime)
    original_sync = workflow.store._sync_revisions

    def fail_after_projection(connection, payload, checkpoint_id):
        original_sync(connection, payload, checkpoint_id)
        if payload.get("full_deck_revisions"):
            raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(workflow.store, "_sync_revisions", fail_after_projection)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        workflow.enter_full_deck(
            before["checkpoint_id"],
            before["current_sample_revision_hash"],
        )

    persisted = workflow.store.read()
    assert persisted["checkpoint_id"] == before["checkpoint_id"]
    assert persisted["samples"][0]["status"] == "pending_approval"
    assert persisted["full_deck"] is None
    assert persisted["full_deck_revisions"] == []
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event IN ('sample_approved', 'full_deck_initialized')"
        ).fetchone()[0] == 0


def test_full_deck_stales_on_outline_edit_and_reruns_from_initial_revision(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, before = _workflow_ready_to_enter(tmp_path, mock_runtime)
    entered = workflow.enter_full_deck(
        before["checkpoint_id"],
        before["current_sample_revision_hash"],
    )
    initial_hash = entered["full_deck"]["current_revision_hash"]
    snapshot = next(
        item for item in workflow.store.progress_snapshots() if item["stage"] == "ppt_full"
    )
    rerun = workflow.store.fork(
        snapshot["checkpoint_id"],
        "full-deck-rerun",
        mode="rerun_stage",
        stage="ppt_full",
    )
    assert rerun["state"] == "ppt_full"
    assert rerun["phase"] == "ready_to_generate"
    assert rerun["full_deck"]["current_revision_hash"] == initial_hash
    assert [item["revision_hash"] for item in rerun["full_deck_revisions"]] == [initial_hash]

    workflow.store.switch_branch(entered["checkpoint_id"])
    revised = workflow.edit_document(
        "slide_outline",
        entered["checkpoint_id"],
        OUTLINE.replace("行动计划", "新的行动计划"),
    )
    assert revised["full_deck"]["revision_refs"][0]["status"] == "stale"
    assert revised["full_deck_revisions"][0]["status"] == "stale"
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute(
            "SELECT status FROM full_deck_revisions WHERE revision_hash = ?",
            (initial_hash,),
        ).fetchone()[0] == "stale"


def test_v3_sqlite_project_migrates_to_v4_idempotently(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "migration-demo")
    store.create(
        TaskCard(title="迁移", objective="保持旧工程可读").model_dump(),
        mock_runtime.snapshot(),
    )
    with sqlite3.connect(store.database_path) as connection:
        for table in (
            "full_deck_package_files",
            "full_deck_packages",
            "full_deck_pages",
            "full_deck_revisions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'"
        )
        for table in ("project_state", "checkpoints"):
            rows = connection.execute(
                f"SELECT rowid, payload_json FROM {table}"
            ).fetchall()
            for rowid, raw in rows:
                payload = json.loads(raw)
                payload["format_version"] = 3
                payload.pop("full_deck", None)
                payload.pop("full_deck_revisions", None)
                connection.execute(
                    f"UPDATE {table} SET payload_json = ? WHERE rowid = ?",
                    (json.dumps(payload), rowid),
                )
        connection.commit()
    ProjectStore._initialized_databases.pop(str(store.database_path), None)

    migrated = ProjectStore(root, "migration-demo").read()
    assert migrated["format_version"] == 4
    assert migrated["full_deck"] is None
    assert migrated["full_deck_revisions"] == []
    ProjectStore._initialized_databases.pop(str(store.database_path), None)
    ProjectStore(root, "migration-demo").read()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
        persisted_payload = json.loads(connection.execute(
            "SELECT payload_json FROM project_state WHERE singleton = 1"
        ).fetchone()[0])
        assert persisted_payload["format_version"] == 4
        assert persisted_payload["full_deck"] is None
        assert {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } >= {
            "full_deck_revisions",
            "full_deck_pages",
            "full_deck_packages",
            "full_deck_package_files",
        }
