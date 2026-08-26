from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import agent_core.full_deck_session as session_module
from agent_core.jobs import JobCancelled, JobRegistry
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
from storage.project_store import ProjectStore
from tests.job_support import wait_for_terminal_job


OUTLINE = "# 逐页大纲\n\n" + "\n".join(
    f"## 第 {number} 页｜页面 {number}\n- 本页目的：推进第 {number} 个叙事节点"
    for number in range(1, 17)
)


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="前两页样品",
        slide_count=2,
        slides=[
            PackageSlide(
                slide_id=f"sample-{number}",
                title=f"页面 {number}",
                source_slide_number=number,
            )
            for number in (1, 2)
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    'data-slide-id="sample-1"><h1>页面 1</h1></section>'
                    '<section class="slide" data-slide-id="sample-2">'
                    "<h1>页面 2</h1></section></body></html>"
                ),
            )
        ],
    )


def _ready_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
    *,
    project_id: str,
) -> tuple[Workflow, dict]:
    store = ProjectStore(tmp_path / "projects", project_id)
    manifest = store.create(
        TaskCard(title="十六页演示", objective="验证分批全稿会话").model_dump(),
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
        "session_fixture_ready",
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    workflow = Workflow(store, runtime)
    entered = workflow.enter_full_deck(
        manifest["checkpoint_id"],
        sample.revision_hash,
    )
    return workflow, entered


def _targets(prompt: str) -> list[int]:
    match = re.search(r"FULL_DECK_TARGET_SLIDE_NUMBERS:\s*(\[[^\n]*\])", prompt)
    assert match
    return json.loads(match.group(1))


def test_session_pause_directive_failure_retry_and_single_publish(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-orchestration",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    session_id = session["session_id"]
    assert [batch["source_slide_numbers"] for batch in session["batches"]] == [
        [3, 4, 5, 6],
        [7, 8, 9, 10],
        [11, 12, 13],
        [14, 15, 16],
    ]

    original_generate = workflow.gateway.generate
    dispatched: list[list[int]] = []
    pause_requested = False

    def pause_during_first_batch(state: str, prompt: str, **kwargs):
        nonlocal pause_requested
        target = _targets(prompt)
        dispatched.append(target)
        if target == [3, 4, 5, 6] and not pause_requested:
            pause_requested = True
            running = workflow.store.full_deck_generation_session(session_id)
            workflow.request_full_deck_generation_pause(
                session_id,
                running["session_version"],
            )
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = pause_during_first_batch
    paused = workflow.run_full_deck_generation_session(session_id)

    assert paused["status"] == "paused"
    assert [batch["status"] for batch in paused["batches"]] == [
        "succeeded",
        "pending",
        "pending",
        "pending",
    ]
    first_package_hash = paused["batches"][0]["segment_package_id"]
    directive = workflow.add_full_deck_generation_directive(
        session_id,
        paused["session_version"],
        "后续页面减少装饰元素，强化数据层级。",
    )
    assert directive["apply_from_batch_index"] == 2
    with_directive = workflow.store.full_deck_generation_session(session_id)
    workflow.resume_full_deck_generation_session(
        session_id,
        with_directive["session_version"],
    )
    running_directive: dict | None = None

    def fail_third_batch(state: str, prompt: str, **kwargs):
        nonlocal running_directive
        target = _targets(prompt)
        dispatched.append(target)
        if target == [7, 8, 9, 10] and running_directive is None:
            running = workflow.store.full_deck_generation_session(session_id)
            running_directive = workflow.add_full_deck_generation_directive(
                session_id,
                running["session_version"],
                "从下一批开始使用更紧凑的图表标注。",
            )
        if target == [11, 12, 13]:
            return json.dumps({"source_slide_numbers": [99]}), []
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = fail_third_batch
    with pytest.raises(Exception) as raised:
        workflow.run_full_deck_generation_session(session_id)
    assert raised.value.public_code == "full_deck_batch_failed"

    failed = workflow.store.full_deck_generation_session(session_id)
    assert failed["status"] == "failed"
    assert [batch["status"] for batch in failed["batches"]] == [
        "succeeded",
        "succeeded",
        "failed",
        "pending",
    ]
    assert failed["batches"][0]["segment_package_id"] == first_package_hash
    assert failed["latest_preview_package_id"] is not None
    assert len(failed["batches"][2]["prompt_call_ids"]) == 3
    assert running_directive is not None
    assert running_directive["apply_from_batch_index"] == 3

    retried_targets: list[list[int]] = []

    restarted = Workflow(
        ProjectStore(tmp_path / "projects", "session-orchestration"),
        mock_runtime,
    )
    restarted_generate = restarted.gateway.generate

    def capture_retry(state: str, prompt: str, **kwargs):
        target = _targets(prompt)
        retried_targets.append(target)
        return restarted_generate(state, prompt, **kwargs)

    restarted.gateway.generate = capture_retry
    completed = restarted.run_full_deck_generation_session(session_id)

    assert completed["status"] == "completed"
    assert retried_targets == [[11, 12, 13], [14, 15, 16]]
    assert all(batch["status"] == "succeeded" for batch in completed["batches"])
    assert completed["batches"][2]["attempt_count"] == 2
    directive_id = directive["directive_id"]
    running_directive_id = running_directive["directive_id"]
    assert completed["batches"][0]["applied_directive_ids"] == []
    assert all(
        directive_id in batch["applied_directive_ids"]
        for batch in completed["batches"][1:]
    )
    assert running_directive_id not in completed["batches"][1][
        "applied_directive_ids"
    ]
    assert all(
        running_directive_id in batch["applied_directive_ids"]
        for batch in completed["batches"][2:]
    )
    manifest = workflow.store.read(include_sample_html=False)
    assert len(manifest["full_deck_revisions"]) == 2
    revision = manifest["full_deck_revisions"][-1]
    assert revision["status"] == "pending_approval"
    assert revision["package"]["slide_count"] == 16
    assert revision["provenance"]["generation_session_id"] == session_id
    assert completed["published_revision_hash"] == revision["revision_hash"]
    assert all(call["status"] != "started" for call in workflow.store.prompt_calls())
    assert sum(
        event["event"] == "full_deck_generated"
        for event in workflow.store.events()
    ) == 1


def test_finalization_retry_reuses_every_successful_segment(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-finalization-retry",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    session_id = session["session_id"]
    original_generate = workflow.gateway.generate
    generated_targets: list[list[int]] = []

    def capture(state: str, prompt: str, **kwargs):
        generated_targets.append(_targets(prompt))
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = capture
    original_compose = session_module.compose_full_deck
    finalization_calls = 0

    def fail_finalization_once(composer_input):
        nonlocal finalization_calls
        finalization_calls += 1
        if finalization_calls == 1:
            raise ValueError("injected final Composer failure")
        return original_compose(composer_input)

    monkeypatch.setattr(session_module, "compose_full_deck", fail_finalization_once)
    with pytest.raises(Exception, match="最终发布失败"):
        workflow.run_full_deck_generation_session(session_id)

    failed = workflow.store.full_deck_generation_session(session_id)
    assert failed["status"] == "failed"
    assert failed["completed_batches"] == 4
    assert all(batch["status"] == "succeeded" for batch in failed["batches"])
    assert len(workflow.store.read(include_sample_html=False)["full_deck_revisions"]) == 1

    restarted = Workflow(
        ProjectStore(tmp_path / "projects", "session-finalization-retry"),
        mock_runtime,
    )
    completed = restarted.run_full_deck_generation_session(session_id)

    assert completed["status"] == "completed"
    assert generated_targets == [
        [3, 4, 5, 6],
        [7, 8, 9, 10],
        [11, 12, 13],
        [14, 15, 16],
    ]
    manifest = workflow.store.read(include_sample_html=False)
    assert len(manifest["full_deck_revisions"]) == 2
    assert sum(
        revision.get("provenance", {}).get("generation_session_id") == session_id
        for revision in manifest["full_deck_revisions"]
    ) == 1


def test_preview_retry_reuses_the_durable_segment_without_model_recall(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-preview-retry",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    session_id = session["session_id"]
    original_generate = workflow.gateway.generate
    generated_targets: list[list[int]] = []

    def capture(state: str, prompt: str, **kwargs):
        generated_targets.append(_targets(prompt))
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = capture
    original_preview = session_module.compose_partial_full_deck_preview
    preview_calls = 0

    def fail_first_preview(**kwargs):
        nonlocal preview_calls
        preview_calls += 1
        if preview_calls == 1:
            raise ValueError("injected preview failure")
        return original_preview(**kwargs)

    monkeypatch.setattr(
        session_module,
        "compose_partial_full_deck_preview",
        fail_first_preview,
    )
    with pytest.raises(Exception) as raised:
        workflow.run_full_deck_generation_session(session_id)
    assert raised.value.public_code == "full_deck_preview_failed"

    failed = workflow.store.full_deck_generation_session(session_id)
    first_batch = failed["batches"][0]
    assert failed["status"] == "failed"
    assert first_batch["status"] == "failed"
    assert first_batch["segment_package_id"] is not None
    assert workflow.store.prompt_calls()[-1]["status"] == "completed"

    restarted = Workflow(
        ProjectStore(tmp_path / "projects", "session-preview-retry"),
        mock_runtime,
    )
    restarted_generate = restarted.gateway.generate

    def capture_after_restart(state: str, prompt: str, **kwargs):
        generated_targets.append(_targets(prompt))
        return restarted_generate(state, prompt, **kwargs)

    restarted.gateway.generate = capture_after_restart
    completed = restarted.run_full_deck_generation_session(session_id)

    assert completed["status"] == "completed"
    assert generated_targets.count([3, 4, 5, 6]) == 1
    assert len(generated_targets) == 4


def test_interrupted_running_batch_recovers_and_retries_from_that_batch(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-worker-recovery",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    session_id = session["session_id"]
    claimed = workflow.store.claim_full_deck_generation_batch(
        session_id,
        expected_session_version=session["session_version"],
    )
    assert claimed["batch"]["batch_index"] == 1

    restarted = Workflow(
        ProjectStore(tmp_path / "projects", "session-worker-recovery"),
        mock_runtime,
    )
    recovered = restarted.recover_full_deck_generation_sessions()

    assert len(recovered) == 1
    assert recovered[0]["status"] == "failed"
    assert recovered[0]["batches"][0]["status"] == "failed"
    completed = restarted.run_full_deck_generation_session(session_id)

    assert completed["status"] == "completed"
    assert completed["batches"][0]["attempt_count"] == 2
    assert all(batch["attempt_count"] == 1 for batch in completed["batches"][1:])


def test_session_runner_publishes_live_progress_to_its_bound_job(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-job-progress",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    registry = JobRegistry(tmp_path / "jobs")
    job = registry.submit(
        "session-job-progress",
        "generate_full_deck",
        entered["checkpoint_id"],
        lambda report: workflow.run_full_deck_generation_session(
            session["session_id"],
            progress_callback=report,
        ),
        session_id=session["session_id"],
        initial_progress={"stage": "queued"},
        progress_reporting=True,
    )

    terminal = wait_for_terminal_job(registry.get, job["job_id"], timeout=3)

    assert terminal["status"] == "succeeded"
    assert terminal["session_id"] == session["session_id"]
    assert terminal["progress"] == {
        "session_id": session["session_id"],
        "stage": "completed",
        "current_batch": None,
        "total_batches": 4,
        "completed_batches": 4,
        "ready_pages": 16,
        "total_pages": 16,
        "active_slide_numbers": [],
    }


def test_session_cancellation_waits_for_the_current_batch_safe_point(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-safe-cancel",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    original_generate = workflow.gateway.generate
    cancellation_requested = False

    def request_cancel_during_model(state: str, prompt: str, **kwargs):
        nonlocal cancellation_requested
        cancellation_requested = True
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = request_cancel_during_model
    with pytest.raises(JobCancelled):
        workflow.run_full_deck_generation_session(
            session["session_id"],
            cancel_requested=lambda: cancellation_requested,
        )

    cancelled = workflow.store.full_deck_generation_session(session["session_id"])
    assert cancelled["status"] == "cancelled"
    assert [batch["status"] for batch in cancelled["batches"]] == [
        "succeeded",
        "pending",
        "pending",
        "pending",
    ]
    assert cancelled["latest_preview_package_id"] is not None
    assert sum(
        page["generation_status"] in {"sample_ready", "ready"}
        for page in cancelled["pages"]
    ) == 6
