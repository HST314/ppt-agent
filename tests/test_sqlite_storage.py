import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from agent_core.jobs import JobRegistry
from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationContentRef,
    FullDeckGenerationDirective,
    FullDeckGenerationPackage,
    FullDeckGenerationPage,
    FullDeckGenerationSession,
    HtmlPptPackage,
    SamplePage,
    SampleRevision,
    TaskCard,
    utc_now,
)
from agent_core.processes import process_is_alive
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore
from tests.job_support import wait_for_terminal_job


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

    assert imported["format_version"] == 5
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
    job = wait_for_terminal_job(
        registry.get,
        job["job_id"],
        fetch_events=registry.events,
    )

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
    terminal = wait_for_terminal_job(
        first.get,
        job["job_id"],
        fetch_events=first.events,
    )
    assert terminal["status"] == "succeeded"


def _generation_records(
    *,
    session_id: str,
    batch_count: int,
) -> tuple[
    FullDeckGenerationSession,
    list[FullDeckGenerationBatch],
    list[FullDeckGenerationPage],
]:
    session = FullDeckGenerationSession(
        session_id=session_id,
        full_deck_id="deck_" + "d" * 24,
        branch="main",
        base_checkpoint_id="checkpoint_" + "c" * 24,
        base_revision_hash="sha256:" + "1" * 64,
        outline_revision_hash="sha256:" + "2" * 64,
        sample_revision_hash="sha256:" + "3" * 64,
        planner_version="balanced-3-4-v1",
        total_batches=batch_count,
    )
    batches: list[FullDeckGenerationBatch] = []
    pages: list[FullDeckGenerationPage] = []
    for batch_index in range(1, batch_count + 1):
        slot_id = f"slot_{batch_index:024x}"
        slide_number = batch_index + 2
        batches.append(FullDeckGenerationBatch(
            session_id=session_id,
            batch_index=batch_index,
            slot_ids=[slot_id],
            source_slide_numbers=[slide_number],
        ))
        pages.append(FullDeckGenerationPage(
            session_id=session_id,
            position=batch_index - 1,
            slot_id=slot_id,
            source_slide_number=slide_number,
            title=f"生成页 {slide_number}",
            generation_status="queued",
            batch_index=batch_index,
            source_type="pending",
        ))
    return session, batches, pages


def _generation_package(
    *,
    session_id: str,
    batch_index: int,
    kind: str,
    suffix: str,
) -> FullDeckGenerationPackage:
    slide_id = f"generated_{batch_index}"
    return FullDeckGenerationPackage.model_validate({
        "package_id": f"fullgenpkg_{suffix * 32}",
        "session_id": session_id,
        "batch_index": batch_index,
        "kind": kind,
        "entrypoint": "index.html",
        "title": f"{kind} {batch_index}",
        "slide_count": 1,
        "slides": [{
            "slide_id": slide_id,
            "title": f"生成页 {batch_index + 2}",
            "source_slide_number": batch_index + 2,
        }],
        "files": [{
            "path": "index.html",
            "content": f"<main data-slide-id='{slide_id}'>{kind}</main>",
            "encoding": "utf-8",
        }],
        "composition_manifest": {"kind": kind, "batch_index": batch_index},
    })


def test_generation_session_cas_directives_and_active_session_uniqueness(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "generation-cas")
    store.create(
        TaskCard(title="分批生成", objective="验证会话 CAS").model_dump(),
        mock_runtime.snapshot(),
    )
    session, batches, pages = _generation_records(
        session_id="fullsession_" + "a" * 32,
        batch_count=2,
    )
    created = store.create_full_deck_generation_session(session, batches, pages)

    assert created["session_version"] == 1
    directive = FullDeckGenerationDirective(
        directive_id="directive_" + "b" * 32,
        session_id=session.session_id,
        content="  后续页面减少装饰元素。  ",
        apply_from_batch_index=2,
    )
    added = store.add_full_deck_generation_directive(
        directive, expected_session_version=1
    )
    assert added["content"] == "后续页面减少装饰元素。"
    assert added["session_version"] == 2
    with pytest.raises(ConflictError, match="session_version_conflict"):
        store.update_full_deck_generation_session(
            session.session_id,
            1,
            status="paused",
        )
    with pytest.raises(ConflictError, match="transition_invalid"):
        store.update_full_deck_generation_session(
            session.session_id,
            2,
            status="completed",
            completed_batches=2,
            published_revision_hash="sha256:" + "8" * 64,
        )

    duplicate, duplicate_batches, duplicate_pages = _generation_records(
        session_id="fullsession_" + "c" * 32,
        batch_count=1,
    )
    with pytest.raises(ConflictError, match="session_conflict"):
        store.create_full_deck_generation_session(
            duplicate, duplicate_batches, duplicate_pages
        )
    assert store.active_full_deck_generation_session(
        session.full_deck_id, "main"
    )["session_id"] == session.session_id


def test_generation_batch_commit_preview_read_and_restart_recovery(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "generation-restart")
    store.create(
        TaskCard(title="分批生成", objective="验证重启恢复").model_dump(),
        mock_runtime.snapshot(),
    )
    session, batches, pages = _generation_records(
        session_id="fullsession_" + "d" * 32,
        batch_count=2,
    )
    store.create_full_deck_generation_session(session, batches, pages)
    claim = store.claim_full_deck_generation_batch(
        session.session_id, expected_session_version=1
    )
    assert claim is not None
    assert claim["batch"]["batch_index"] == 1
    assert claim["session_version"] == 2

    segment = _generation_package(
        session_id=session.session_id,
        batch_index=1,
        kind="segment",
        suffix="e",
    )
    preview = _generation_package(
        session_id=session.session_id,
        batch_index=1,
        kind="preview",
        suffix="f",
    )
    slot_id = batches[0].slot_ids[0]
    committed = store.commit_full_deck_generation_batch(
        session.session_id,
        1,
        expected_session_version=2,
        segment_package=segment,
        preview_package=preview,
        page_content_refs={
            slot_id: FullDeckGenerationContentRef(
                package_id=segment.package_id,
                package_hash=segment.package_hash,
                slide_id=segment.slides[0].slide_id,
                slide_content_hash="sha256:" + "4" * 64,
            )
        },
    )
    assert committed["completed_batches"] == 1
    assert committed["batches"][0]["status"] == "succeeded"
    assert committed["pages"][0]["generation_status"] == "ready"
    assert committed["latest_preview_package_id"] == preview.package_id

    restarted = ProjectStore(root, "generation-restart")
    restored = restarted.full_deck_generation_session(session.session_id)
    preview_path, media_type = restarted.full_deck_generation_preview_file(
        session.session_id, "index.html"
    )
    assert restored["batches"][0]["segment_package_id"] == segment.package_id
    assert preview_path.read_text(encoding="utf-8").endswith("preview</main>")
    assert media_type == "text/html; charset=utf-8"

    second_claim = restarted.claim_full_deck_generation_batch(
        session.session_id, expected_session_version=3
    )
    assert second_claim is not None
    assert second_claim["batch"]["batch_index"] == 2
    recovered = ProjectStore(root, "generation-restart").recover_full_deck_generation_sessions()
    assert len(recovered) == 1
    assert recovered[0]["status"] == "failed"
    assert recovered[0]["completed_batches"] == 1
    assert [item["status"] for item in recovered[0]["batches"]] == [
        "succeeded",
        "failed",
    ]
    assert [item["generation_status"] for item in recovered[0]["pages"]] == [
        "ready",
        "failed",
    ]
    assert ProjectStore(root, "generation-restart").full_deck_generation_package_contents(
        preview.package_id
    )[0]["content"].endswith(b"preview</main>")
    retry = ProjectStore(root, "generation-restart").claim_full_deck_generation_batch(
        session.session_id,
        expected_session_version=5,
        retry_failed=True,
    )
    assert retry is not None
    assert retry["batch"]["batch_index"] == 2
    assert retry["batch"]["attempt_count"] == 2
    after_retry = store.full_deck_generation_session(session.session_id)
    assert after_retry["batches"][0]["status"] == "succeeded"
    assert after_retry["latest_preview_package_id"] == preview.package_id


def test_generation_batch_claim_is_atomic_across_store_instances(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "generation-claim")
    store.create(
        TaskCard(title="分批生成", objective="验证并发领取").model_dump(),
        mock_runtime.snapshot(),
    )
    session, batches, pages = _generation_records(
        session_id="fullsession_" + "9" * 32,
        batch_count=1,
    )
    store.create_full_deck_generation_session(session, batches, pages)
    barrier = threading.Barrier(2)

    def claim() -> dict | None:
        worker_store = ProjectStore(root, "generation-claim")
        barrier.wait(timeout=5)
        return worker_store.claim_full_deck_generation_batch(session.session_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert sum(result is not None for result in results) == 1
    snapshot = store.full_deck_generation_session(session.session_id)
    assert snapshot["batches"][0]["status"] == "running"
    assert snapshot["batches"][0]["attempt_count"] == 1
    assert snapshot["session_version"] == 2


def test_generation_batch_commit_rolls_back_before_session_version_publish(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "generation-rollback")
    store.create(
        TaskCard(title="分批生成", objective="验证提交回滚").model_dump(),
        mock_runtime.snapshot(),
    )
    session, batches, pages = _generation_records(
        session_id="fullsession_" + "8" * 32,
        batch_count=1,
    )
    store.create_full_deck_generation_session(session, batches, pages)
    store.claim_full_deck_generation_batch(
        session.session_id, expected_session_version=1
    )
    segment = _generation_package(
        session_id=session.session_id,
        batch_index=1,
        kind="segment",
        suffix="6",
    )
    preview = _generation_package(
        session_id=session.session_id,
        batch_index=1,
        kind="preview",
        suffix="7",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_generation_session_publish
            BEFORE UPDATE ON full_deck_generation_sessions
            BEGIN
                SELECT RAISE(ABORT, 'injected session publish failure');
            END
            """
        )
        connection.commit()

    with pytest.raises(ConflictError, match="package_conflict"):
        store.commit_full_deck_generation_batch(
            session.session_id,
            1,
            expected_session_version=2,
            segment_package=segment,
            preview_package=preview,
            page_content_refs={
                batches[0].slot_ids[0]: FullDeckGenerationContentRef(
                    package_id=segment.package_id,
                    package_hash=segment.package_hash,
                    slide_id=segment.slides[0].slide_id,
                    slide_content_hash="sha256:" + "5" * 64,
                )
            },
        )

    snapshot = store.full_deck_generation_session(session.session_id)
    assert snapshot["session_version"] == 2
    assert snapshot["latest_preview_package_id"] is None
    assert snapshot["batches"][0]["status"] == "running"
    assert snapshot["pages"][0]["generation_status"] == "generating"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM full_deck_generation_packages"
        ).fetchone()[0] == 0


def test_v4_project_migrates_to_v5_without_changing_revisions_or_artifacts(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    root = tmp_path / "projects"
    store = ProjectStore(root, "generation-migration")
    manifest = store.create(
        TaskCard(title="迁移", objective="验证 v5 无损迁移").model_dump(),
        mock_runtime.snapshot(),
    )
    sample = SampleRevision.create(
        [SamplePage(page_id="sample_1", title="样品", html="<main>样品</main>")],
        revision=1,
        parent=None,
        feedback=None,
    )
    manifest = store.update(
        lambda value: value | {
            "samples": [sample.model_dump()],
            "current_sample_revision_hash": sample.revision_hash,
            "state": "ppt_sample",
            "phase": "waiting_human_approval",
        },
        "sample_generated",
        {"revision_hash": sample.revision_hash},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    with sqlite3.connect(store.database_path) as connection:
        artifact_rows = connection.execute(
            "SELECT artifact_id, sha256, size_bytes, relative_path FROM artifacts"
        ).fetchall()
        for table in (
            "full_deck_generation_package_files",
            "full_deck_generation_packages",
            "full_deck_generation_directives",
            "full_deck_generation_pages",
            "full_deck_generation_batches",
            "full_deck_generation_sessions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
        )
        for table in ("project_state", "checkpoints"):
            rows = connection.execute(
                f"SELECT rowid, payload_json FROM {table}"
            ).fetchall()
            for rowid, raw in rows:
                payload = json.loads(raw)
                payload["format_version"] = 4
                connection.execute(
                    f"UPDATE {table} SET payload_json = ? WHERE rowid = ?",
                    (json.dumps(payload), rowid),
                )
        connection.commit()
    artifact_bytes = {
        row[0]: (store.root / row[3]).read_bytes() for row in artifact_rows
    }
    ProjectStore._initialized_databases.pop(str(store.database_path), None)

    migrated = ProjectStore(root, "generation-migration").read()

    assert migrated["format_version"] == 5
    assert migrated["samples"][0]["revision_hash"] == sample.revision_hash
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "5"
        assert connection.execute(
            "SELECT count(*) FROM full_deck_generation_sessions"
        ).fetchone()[0] == 0
        migrated_artifacts = connection.execute(
            "SELECT artifact_id, sha256, size_bytes, relative_path FROM artifacts"
        ).fetchall()
    assert migrated_artifacts == artifact_rows
    assert {
        row[0]: (store.root / row[3]).read_bytes() for row in migrated_artifacts
    } == artifact_bytes
