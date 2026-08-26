import json
import os
import sqlite3
import threading
import time
from multiprocessing import get_context
from pathlib import Path

from agent_core.jobs import JobRegistry
from agent_core.models import HtmlPptPackage, SamplePage, SampleRevision, TaskCard, utc_now
from agent_core.processes import process_is_alive
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


def _write_legacy_project(root: Path, project_id: str, runtime: dict) -> str:
    project_root = root / project_id
    checkpoint_id = "checkpoint_0123456789abcdef01234567"
    created_at = utc_now()
    manifest = {
        "project_id": project_id,
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
        "runtime": runtime,
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
    return checkpoint_id


def _read_legacy_project_in_process(root: str, project_id: str, ready, start, results) -> None:
    ready.put(True)
    if not start.wait(timeout=10):
        results.put(("error", "start timeout"))
        return
    try:
        manifest = ProjectStore(Path(root), project_id).read()
        results.put(("ok", manifest["checkpoint_id"]))
    except Exception as exc:  # pragma: no cover - the assertion reports the child failure.
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_process_liveness_probe_is_non_destructive() -> None:
    assert process_is_alive(os.getpid())
    assert not process_is_alive(None)
    assert not process_is_alive(0)


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
    assert manifest["samples"][0]["pages"][0]["html"].startswith("<!doctype html>")
    assert manifest["samples"][0]["pages"][0]["html"].endswith("</body></html>")

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
    assert artifact_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_project_store_persists_and_hydrates_complete_html_ppt_package(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    store = ProjectStore(tmp_path / "projects", "package-demo")
    manifest = store.create(
        TaskCard(title="包测试", objective="验证多文件持久化").model_dump(),
        mock_runtime.snapshot(),
    )
    package = HtmlPptPackage.model_validate({
        "entrypoint": "index.html",
        "title": "多文件样品",
        "slide_count": 1,
        "slides": [{"slide_id": "cover", "title": "封面"}],
        "files": [
            {
                "path": "index.html",
                "content": '<link rel="stylesheet" href="assets/deck.css"><img src="assets/pixel.png">',
                "encoding": "utf-8",
            },
            {
                "path": "assets/deck.css",
                "content": "body{margin:0}",
                "encoding": "utf-8",
            },
            {
                "path": "assets/pixel.png",
                "content": "AAEC",
                "encoding": "base64",
                "media_type": "image/png",
            },
        ],
    })
    sample = SampleRevision.create_package(
        package,
        revision=1,
        parent=None,
        feedback=None,
    )

    def add_sample(value: dict) -> dict:
        value["samples"].append(sample.model_dump())
        value["current_sample_revision_hash"] = sample.revision_hash
        value.update(state="ppt_sample", phase="waiting_human_approval")
        return value

    stored = store.update(
        add_sample,
        "sample_generated",
        {"revision_hash": sample.revision_hash},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )

    files = stored["samples"][0]["package"]["files"]
    assert [item["path"] for item in files] == [
        "index.html", "assets/deck.css", "assets/pixel.png",
    ]
    assert files[-1]["encoding"] == "base64"
    assert files[0]["media_type"] == "text/html; charset=utf-8"
    assert files[1]["media_type"] == "text/css; charset=utf-8"
    binary_path, media_type = store.sample_package_file(
        sample.revision_hash, "assets/pixel.png"
    )
    assert binary_path.read_bytes() == b"\x00\x01\x02"
    assert media_type == "image/png"
    assert store.sample_history()[0]["package"]["file_count"] == 3
    assert all(
        "content" not in item
        for item in store.read(include_sample_html=False)["samples"][0]["package"]["files"]
    )


def test_legacy_file_project_is_imported_without_removing_source_files(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    project_root = root / "legacy-demo"
    checkpoint_id = _write_legacy_project(root, "legacy-demo", mock_runtime.snapshot())

    store = ProjectStore(root, "legacy-demo")
    imported = store.read()

    assert imported["format_version"] == 4
    assert imported["storage"]["engine"] == "sqlite-wal"
    assert imported["samples"][0]["pages"][0]["html"] == "<main>旧样品</main>"
    assert store.database_path.is_file()
    assert store.manifest_path.is_file()
    assert (project_root / "checkpoints" / f"{checkpoint_id}.json").is_file()
    assert store.events()[-1]["event"] == "storage_migrated"


def test_legacy_file_project_cold_start_is_safe_across_processes(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    project_id = "legacy-concurrent"
    checkpoint_id = _write_legacy_project(root, project_id, mock_runtime.snapshot())
    context = get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_read_legacy_project_in_process,
            args=(str(root), project_id, ready, start, results),
        )
        for _ in range(6)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15) is True
    start.set()
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert outcomes == [("ok", checkpoint_id)] * len(processes)
    store = ProjectStore(root, project_id)
    assert store.read()["checkpoint_id"] == checkpoint_id
    assert [event["event"] for event in store.events()].count("storage_migrated") == 1
    assert store.events(limit=1)[0]["event"] == "storage_migrated"


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


def test_prompt_call_audit_redacts_quoted_headers_tokens_and_nested_values(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "projects", "audit-redaction-matrix")
    secret_lines = [
        'api_key: "quoted-secret-01"',
        "password='quoted-secret-02'",
        "Authorization: Basic YmFzaWMtc2VjcmV0LTAz",
        "Authorization: Bearer bearer-secret-04",
        "token=generic-secret-05",
        '"refresh_token": "refresh-secret-06"',
        "client_secret: 'client-secret-07'",
        "OPENAI_API_KEY=environment-secret-11",
        "SERVICE_TOKEN=environment-secret-12",
    ]
    prompt_call_id = store.start_prompt_call(
        state="intake_clarify",
        messages=[{"role": "user", "content": "\n".join(secret_lines)}],
        template_id="redaction-test",
        template_version=1,
        template_hash="sha256:template",
        model_config_hash="sha256:model",
        runtime_config_hash="sha256:runtime",
        skills_hash="sha256:skills",
        parameters={
            "credentials": "dict-secret-08",
            "nested": {"X-API-Key": "dict-secret-09"},
        },
    )
    store.finish_prompt_call(
        prompt_call_id,
        status="completed",
        traces=[{
            "type": "tool_call",
            "tool": "read",
            "details": "Bearer trace-secret-10",
        }],
        output_ref="questions_safe",
        output_hash="sha256:output",
    )

    calls = json.dumps(store.prompt_calls(), ensure_ascii=False)
    exported = store.export_prompt_calls_jsonl()
    for secret in (
        "quoted-secret-01",
        "quoted-secret-02",
        "YmFzaWMtc2VjcmV0LTAz",
        "bearer-secret-04",
        "generic-secret-05",
        "refresh-secret-06",
        "client-secret-07",
        "dict-secret-08",
        "dict-secret-09",
        "trace-secret-10",
        "environment-secret-11",
        "environment-secret-12",
    ):
        assert secret not in calls
        assert secret not in exported
    assert calls.count("[REDACTED]") >= 12


def test_sample_attempt_summary_groups_latest_chain_and_counts_tool_rounds(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "projects", "sample-attempts")
    audit_args = {
        "state": "ppt_sample",
        "messages": [{"role": "user", "content": "生成样品"}],
        "template_id": "ppt_sample",
        "template_version": 1,
        "template_hash": "sha256:template",
        "model_config_hash": "sha256:model",
        "runtime_config_hash": "sha256:runtime",
        "skills_hash": "sha256:skills",
        "parameters": {"provider": "test", "model": "test-model"},
    }
    first = store.start_prompt_call(**audit_args)
    store.finish_prompt_call(
        first,
        status="failed",
        messages=[
            {"role": "user", "content": "生成样品"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "read", "arguments": "{}"}},
                {"function": {"name": "write_package_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "{}"},
        ],
        error={
            "type": "SampleGenerationError",
            "code": "sample_package_invalid",
            "message": "slide_count 必须为 2，实际为 17。",
        },
    )
    second = store.start_prompt_call(**audit_args, parent_prompt_call_id=first)
    store.finish_prompt_call(
        second,
        status="completed",
        messages=[
            {"role": "user", "content": "修复样品"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "{}"},
            {"role": "assistant", "content": "{}"},
        ],
        output_ref="sha256:" + "a" * 64,
        output_hash="sha256:" + "b" * 64,
    )

    attempts = store.sample_attempts()

    assert [item["status"] for item in attempts] == ["failed", "completed"]
    assert attempts[0]["reason"] == "slide_count 必须为 2，实际为 17。"
    assert attempts[0]["tool_rounds"] == 1
    assert attempts[0]["tool_call_count"] == 2
    assert attempts[0]["skill_read_count"] == 1
    assert attempts[1]["published"] is True


def test_prompt_round_progress_is_immediately_queryable_and_drives_resume(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "projects", "live-progress")
    checkpoint_id = "checkpoint_0123456789abcdef01234567"
    prompt_call_id = store.start_prompt_call(
        state="ppt_sample",
        messages=[{"role": "user", "content": "生成样品"}],
        template_id="ppt_sample",
        template_version=1,
        template_hash="sha256:template",
        model_config_hash="sha256:model",
        runtime_config_hash="sha256:runtime",
        skills_hash="sha256:skills",
        parameters={
            "provider": "test",
            "model": "test-model",
            "generation_checkpoint_id": checkpoint_id,
            "round_limit": 20,
            "sample_request": {},
        },
    )
    messages = [
        {"role": "user", "content": "生成样品"},
        {"role": "assistant", "tool_calls": [{
            "id": "call_1",
            "function": {"name": "read", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]
    traces = [{
        "type": "tool_call",
        "tool": "read",
        "path": "template.md",
        "content_hash": "sha256:content",
        "offset": 0,
        "end": 100,
        "round": 1,
        "round_limit": 20,
        "at": utc_now(),
    }]

    store.append_prompt_call_progress(
        prompt_call_id,
        status="tool_round_completed",
        details={
            "round": 1,
            "round_limit": 20,
            "tools": ["read"],
            "tool_call_count": 1,
            "skill_read_count": 1,
            "recent_action": "read · template.md",
            "elapsed_seconds": 0.1,
        },
        traces=traces,
        messages=messages,
    )

    attempt = store.sample_attempts(current_checkpoint_id=checkpoint_id)[0]
    event = store.prompt_call_events()[0]
    assert attempt["status"] == "started"
    assert attempt["tool_rounds"] == 1
    assert attempt["skill_read_count"] == 1
    assert event["status"] == "tool_round_completed"
    assert event["details"]["round"] == 1

    store.finish_prompt_call(
        prompt_call_id,
        status="failed",
        messages=messages,
        traces=traces,
        error={
            "type": "MaxToolRoundsExceeded",
            "code": "max_tool_rounds_exceeded",
            "message": "maximum tool rounds exceeded",
        },
    )
    attempt = store.sample_attempts(current_checkpoint_id=checkpoint_id)[0]
    assert attempt["resume_available"] is True
    assert attempt["resume_options"] == [5, 10, 20]
    stale = store.sample_attempts(
        current_checkpoint_id="checkpoint_ffffffffffffffffffffffff",
    )[0]
    assert stale["resume_available"] is False
    assert stale["resume_blocked_reason"] == "工程检查点已变化，不能继续旧生成。"


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
