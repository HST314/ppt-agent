import json
import sqlite3
import threading
import time
from pathlib import Path

from agent_core.jobs import JobRegistry
from agent_core.models import SamplePage, SampleRevision, TaskCard, utc_now
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


def test_project_store_commits_sqlite_wal_artifacts_and_parent_links(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "durable-demo")
    manifest = store.create(
        TaskCard(title="可靠存储", objective="验证事务和内容寻址").model_dump(),
        mock_runtime.snapshot(),
    )
    page = SamplePage(
        page_id="sample_1",
        title="结论",
        html="<style>.slide{color:#171126}</style><main class='slide'>结论</main>",
    )
    sample = SampleRevision.create(
        [page], revision=1, parent=None, feedback=None,
    )
    manifest = store.update(
        lambda value: value | {
            "samples": [sample.model_dump()],
            "state": "ppt_sample",
            "phase": "waiting_human_approval",
        },
        "sample_generated",
        {"revision_hash": sample.revision_hash},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )

    assert store.database_path.is_file()
    assert not store.manifest_path.exists()
    assert manifest["samples"][0]["pages"][0]["html"].endswith("</main>")

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        raw = connection.execute(
            "SELECT payload_json FROM project_state WHERE singleton = 1"
        ).fetchone()[0]
        checkpoints = connection.execute(
            "SELECT checkpoint_id, parent_checkpoint_id FROM checkpoints ORDER BY updated_at"
        ).fetchall()
        artifact = connection.execute(
            "SELECT artifact_id, relative_path, size_bytes FROM artifacts"
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM sample_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sample_pages").fetchone()[0] == 1

    assert '"html"' not in raw
    assert '"pages"' not in raw
    assert len(checkpoints) == 2
    assert checkpoints[0][1] is None
    assert checkpoints[1][1] == checkpoints[0][0]
    assert artifact[0].startswith("sha256:")
    artifact_path = store.root / artifact[1]
    assert artifact_path.is_file()
    assert artifact_path.stat().st_size == artifact[2]


def test_legacy_file_project_is_imported_without_removing_source_files(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    project_root = root / "legacy-demo"
    checkpoint_id = "checkpoint_0123456789abcdef01234567"
    created_at = utc_now()
    manifest = {
        "project_id": "legacy-demo",
        "title": "旧工程",
        "branch": "main",
        "branches": {"main": checkpoint_id},
        "branch_meta": {"main": {"parent": None, "from_checkpoint": None, "created_at": created_at}},
        "state": "ppt_sample",
        "phase": "waiting_human_approval",
        "task_card": TaskCard(title="旧工程", objective="导入").model_dump(),
        "clarification_answers": {},
        "question_card": None,
        "documents": {"narrative_structure": [], "slide_outline": []},
        "samples": [{
            "sample_id": "sample_ppt",
            "revision": 1,
            "revision_hash": "sha256:" + "a" * 64,
            "parent_revision_hash": None,
            "pages": [{"page_id": "sample_1", "title": "旧样品", "html": "<main>旧样品</main>"}],
            "feedback": None,
            "status": "pending_approval",
            "created_at": created_at,
            "provenance": {},
        }],
        "checkpoint_id": checkpoint_id,
        "active_job_id": None,
        "created_at": created_at,
        "updated_at": created_at,
        "runtime": mock_runtime.snapshot(),
    }
    (project_root / "checkpoints").mkdir(parents=True)
    (project_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (project_root / "checkpoints" / f"{checkpoint_id}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (project_root / "events.jsonl").write_text(
        json.dumps({"at": created_at, "event": "project_created", "checkpoint_id": checkpoint_id}) + "\n",
        encoding="utf-8",
    )

    store = ProjectStore(root, "legacy-demo")
    imported = store.read()

    assert imported["format_version"] == 2
    assert imported["storage"]["engine"] == "sqlite-wal"
    assert imported["samples"][0]["pages"][0]["html"] == "<main>旧样品</main>"
    assert store.database_path.is_file()
    assert store.manifest_path.is_file()
    assert (project_root / "checkpoints" / f"{checkpoint_id}.json").is_file()
    assert store.events()[-1]["event"] == "storage_migrated"


def test_prompt_call_audit_is_redacted_linked_and_exportable(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    store = ProjectStore(tmp_path / "projects", "audit-demo")
    task = TaskCard(
        title="审计演示",
        objective="Authorization=Bearer audit-secret-token 生成提问",
    )
    manifest = store.create(task.model_dump(), mock_runtime.snapshot())
    workflow = Workflow(store, mock_runtime)

    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    calls = store.prompt_calls()
    exported = store.export_prompt_calls_jsonl()

    assert len(calls) == 1
    assert calls[0]["status"] == "completed"
    assert calls[0]["output_ref"] == manifest["question_card"]["question_card_id"]
    assert calls[0]["messages"][0]["role"] == "system"
    assert calls[0]["messages"][-1]["content"].startswith("[OUTPUT_REF questions_")
    assert calls[0]["parameters"]["model"] == "deterministic-preview"
    assert manifest["question_card"]["provenance"]["prompt_call_id"] == calls[0]["prompt_call_id"]
    assert "audit-secret-token" not in exported
    assert "[REDACTED]" in exported
    assert json.loads(exported.strip())["prompt_call_id"] == calls[0]["prompt_call_id"]
    assert not (store.root / "prompts.jsonl").exists()


def test_job_registry_uses_sqlite_and_deduplicates_across_instances(tmp_path: Path) -> None:
    root = tmp_path / ".jobs"
    registry = JobRegistry(root)
    job = registry.submit("demo", "generate_sample", "checkpoint_value", lambda: None)
    assert "_owner_pid" not in job
    for _ in range(100):
        job = registry.get(job["job_id"])
        if job["status"] == "succeeded":
            break
        time.sleep(0.01)

    assert job["status"] == "succeeded"
    assert (root / "jobs.db").is_file()
    assert not (root / f"{job['job_id']}.json").exists()
    assert [event["status"] for event in registry.events(job["job_id"])] == [
        "queued", "running", "succeeded"
    ]

    second_registry = JobRegistry(root)
    duplicate = second_registry.submit(
        "demo", "generate_sample", "checkpoint_value",
        lambda: (_ for _ in ()).throw(AssertionError("deduplicated action must not run")),
    )
    assert duplicate["job_id"] == job["job_id"]


def test_live_job_is_not_failed_when_another_worker_starts(tmp_path: Path) -> None:
    root = tmp_path / ".jobs"
    entered = threading.Event()
    release = threading.Event()

    def running_action() -> None:
        entered.set()
        assert release.wait(timeout=5)

    first = JobRegistry(root)
    job = first.submit("demo", "generate_outline", "checkpoint_live", running_action)
    assert entered.wait(timeout=5)

    second = JobRegistry(root)
    assert second.get(job["job_id"])["status"] == "running"

    release.set()
    for _ in range(100):
        if first.get(job["job_id"])["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert first.get(job["job_id"])["status"] == "succeeded"
