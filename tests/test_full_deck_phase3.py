from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.full_deck_generation import _validate_offline_package
from agent_core.jobs import JobRegistry
from agent_core.models import (
    DocumentRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
)
from agent_core.workflow import (
    FullDeckGenerationError,
    Workflow,
    pending_full_deck_segments,
)
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


OUTLINE = "# 逐页大纲\n\n" + "\n".join(
    f"## 第 {number} 页｜页面 {number}\n- 本页目的：推进第 {number} 个叙事节点"
    for number in range(1, 11)
)


def _sample_package() -> HtmlPptPackage:
    slides = [
        PackageSlide(slide_id="sample-4", title="页面 4", source_slide_number=4),
        PackageSlide(slide_id="sample-5", title="页面 5", source_slide_number=5),
    ]
    return HtmlPptPackage(
        title="中段样品",
        slide_count=2,
        slides=slides,
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><head><link rel="stylesheet" href="assets/deck.css">'
                    '</head><body><section class="slide" data-slide-id="sample-4">'
                    '<h1>页面 4</h1></section><section class="slide" data-slide-id="sample-5">'
                    '<h1>页面 5</h1></section><script src="assets/deck.js"></script></body></html>'
                ),
            ),
            PackageFile(path="assets/deck.css", content=".slide{width:100vw;height:100vh}"),
            PackageFile(path="assets/deck.js", content="addEventListener('keydown',()=>{})"),
        ],
    )


def _ready_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
    *,
    project_id: str = "phase3-deck",
) -> tuple[Workflow, dict]:
    store = ProjectStore(tmp_path / "projects", project_id)
    manifest = store.create(
        TaskCard(title="十页完整演示", objective="验证真实全稿生成").model_dump(),
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
        "phase3_fixture_ready",
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


def _wait_for_job(registry: JobRegistry, job_id: str) -> dict:
    for _ in range(500):
        job = registry.get(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def test_pending_pages_split_around_the_confirmed_middle_sample() -> None:
    pages = [
        {
            "slot_id": f"slot_{number:024x}",
            "position": number - 1,
            "outline_ref": {"source_slide_number": number},
            "status": "ready" if number in {4, 5} else "pending",
        }
        for number in range(1, 11)
    ]

    segments = pending_full_deck_segments(pages)

    assert [
        [page["outline_ref"]["source_slide_number"] for page in segment]
        for segment in segments
    ] == [[1, 2, 3], [6, 7, 8, 9, 10]]


@pytest.mark.parametrize(
    ("dependency", "message"),
    [
        ('<img src="https://example.invalid/pixel.png">', "网络或站点"),
        ('<link rel="stylesheet" href="assets/missing.css">', "不存在"),
        ('<script>fetch("slides.json")</script>', "网络或站点"),
    ],
)
def test_segment_packages_reject_external_or_missing_dependencies(
    dependency: str,
    message: str,
) -> None:
    package = HtmlPptPackage(
        title="依赖校验",
        slide_count=1,
        slides=[PackageSlide(slide_id="slide-1", title="页面", source_slide_number=1)],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    f'data-slide-id="slide-1">{dependency}</section></body></html>'
                ),
            )
        ],
    )

    with pytest.raises(FullDeckGenerationError, match=message):
        _validate_offline_package(package)


def test_full_deck_generation_targets_only_pending_segments_and_publishes_r2(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    original_generate = workflow.gateway.generate
    dispatched: list[list[int]] = []

    def capture(state: str, prompt: str, **kwargs):
        if state == "ppt_full":
            dispatched.append(_targets(prompt))
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = capture
    completed = workflow.generate_full_deck(entered["checkpoint_id"])

    assert dispatched == [[1, 2, 3], [6, 7, 8, 9, 10]]
    assert completed["state"] == "ppt_full"
    assert completed["phase"] == "waiting_human_approval"
    assert len(completed["full_deck_revisions"]) == 2
    revision = completed["full_deck_revisions"][-1]
    assert revision["revision"] == 2
    assert revision["parent_revision_hash"] == entered["full_deck"]["current_revision_hash"]
    assert revision["status"] == "pending_approval"
    assert revision["package"]["slide_count"] == 10
    assert [slide["source_slide_number"] for slide in revision["package"]["slides"]] == list(
        range(1, 11)
    )
    assert [page["status"] for page in revision["plan"]["pages"]] == ["ready"] * 10
    assert [page["source_type"] for page in revision["plan"]["pages"]] == [
        "generated_segment",
        "generated_segment",
        "generated_segment",
        "approved_sample",
        "approved_sample",
        "generated_segment",
        "generated_segment",
        "generated_segment",
        "generated_segment",
        "generated_segment",
    ]
    composition = revision["package"]["composition_manifest"]
    assert [slide["slot_id"] for slide in composition["slides"]] == [
        page["slot_id"] for page in revision["plan"]["pages"]
    ]
    assert all(
        slide["source_slide_content_hash"] == slide["composed_slide_content_hash"]
        for slide in composition["slides"]
    )
    assert {slide["source_id"] for slide in composition["slides"]} == {
        "approved_sample",
        "segment_1_3",
        "segment_6_10",
    }
    attempts = workflow.store.full_deck_attempts()
    assert [item["target_slide_numbers"] for item in attempts] == dispatched
    assert all(item["published"] for item in attempts)
    assert workflow.store.events()[-1]["event"] == "full_deck_generated"

    retained = workflow.store.retained_full_deck_dir(revision["revision_hash"])
    retained_files = {
        path.relative_to(retained).as_posix()
        for path in retained.rglob("*")
        if path.is_file()
    }
    assert retained_files == {
        item["path"] for item in revision["package"]["files"]
    } | {"project.json"}
    retained_project = json.loads((retained / "project.json").read_text(encoding="utf-8"))
    assert retained_project["format"] == "ppt-agent-retained-project-v1"
    assert retained_project["full_deck_revision"]["package"]["slide_count"] == 10
    sample_documents = [
        slide["document_path"]
        for slide in retained_project["full_deck_revision"]["package"][
            "composition_manifest"
        ]["slides"]
        if slide["source_id"] == "approved_sample"
    ]
    assert len(sample_documents) == 2
    assert all((retained / path).is_file() for path in sample_documents)
    assert (workflow.store.artifacts_container_root / "README.md").is_file()
    assert workflow.store.package_artifacts_root.is_dir()

    shutil.rmtree(retained)
    ProjectStore._initialized_databases.pop(
        str(workflow.store.database_path),
        None,
    )
    restarted_store = ProjectStore(tmp_path / "projects", "phase3-deck")
    restarted_store.read(include_sample_html=False)
    assert restarted_store.retained_full_deck_dir(revision["revision_hash"]).is_dir()

    refreshed = ProjectStore(tmp_path / "projects", "phase3-deck").read(
        include_sample_html=False
    )
    assert refreshed["full_deck"]["current_revision_hash"] == revision["revision_hash"]
    assert refreshed["full_deck_revisions"][-1]["package"]["package_hash"] == revision[
        "package"
    ]["package_hash"]
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM full_deck_packages").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event = 'full_deck_generated'"
        ).fetchone()[0] == 1


def test_failed_segment_never_publishes_and_successful_retry_commits_once(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    original_generate = workflow.gateway.generate

    def fail_second_segment(state: str, prompt: str, **kwargs):
        target = _targets(prompt)
        if target[0] == 6:
            return json.dumps({"source_slide_numbers": [6]}), []
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = fail_second_segment
    with pytest.raises(FullDeckGenerationError, match="source_slide_numbers"):
        workflow.generate_full_deck(entered["checkpoint_id"])

    unchanged = workflow.store.read(include_sample_html=False)
    assert unchanged["full_deck"]["current_revision_hash"] == entered["full_deck"][
        "current_revision_hash"
    ]
    assert len(unchanged["full_deck_revisions"]) == 1
    assert not any(event["event"] == "full_deck_generated" for event in workflow.store.events())
    assert all(call["status"] == "failed" for call in workflow.store.prompt_calls())

    workflow.gateway.generate = original_generate
    completed = workflow.generate_full_deck(entered["checkpoint_id"])

    assert len(completed["full_deck_revisions"]) == 2
    assert completed["full_deck_revisions"][-1]["package"]["slide_count"] == 10
    with sqlite3.connect(workflow.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM full_deck_revisions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event = 'full_deck_generated'"
        ).fetchone()[0] == 1


def test_segment_contract_failure_is_repaired_within_the_same_generation(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    original_generate = workflow.gateway.generate
    calls = 0

    def fail_once(state: str, prompt: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"source_slide_numbers": [99]}), []
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = fail_once
    completed = workflow.generate_full_deck(entered["checkpoint_id"])

    assert calls == 3
    assert len(completed["full_deck_revisions"]) == 2
    attempts = workflow.store.full_deck_attempts()
    assert [item["status"] for item in attempts] == ["failed", "completed", "completed"]
    assert [item["attempt"] for item in attempts] == [1, 2, 1]
    assert attempts[0]["failure_code"] == "full_deck_target_mismatch"
    assert completed["full_deck_revisions"][-1]["provenance"]["segments"][0][
        "repair_attempts"
    ] == 1
    assert len(
        completed["full_deck_revisions"][-1]["provenance"]["prompt_call_ids"]
    ) == 3


def test_full_deck_api_job_survives_refresh_and_exports_offline_zip(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered = _ready_full_deck(tmp_path, mock_runtime, project_id="phase3-api")
    client = TestClient(main_front.app)

    started = client.post(
        "/api/projects/phase3-api/jobs",
        json={
            "operation": "generate_full_deck",
            "checkpoint_id": entered["checkpoint_id"],
        },
    )
    assert started.status_code == 202
    job = _wait_for_job(registry, started.json()["job_id"])
    assert job["status"] == "succeeded"

    refreshed = client.get("/api/projects/phase3-api").json()
    revision = refreshed["full_deck_revision"]
    assert revision["package"]["slide_count"] == 10
    assert refreshed["full_deck_attempts"]
    assert all(item["published"] for item in refreshed["full_deck_attempts"])
    preview = client.get(revision["preview_url"])
    assert preview.status_code == 200
    assert "sandbox allow-scripts" in preview.headers["content-security-policy"]
    exported = client.get(revision["export_url"])
    assert exported.status_code == 200
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        assert {"index.html", "composition_manifest.json"} <= names
        manifest = json.loads(archive.read("composition_manifest.json"))
        assert manifest["slide_count"] == 10


def test_running_full_deck_job_can_be_cancelled_from_another_worker(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    original_generate = workflow.gateway.generate
    model_entered = threading.Event()
    release_model = threading.Event()

    def blocking_generate(state: str, prompt: str, **kwargs):
        model_entered.set()
        assert release_model.wait(timeout=5)
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = blocking_generate
    jobs_root = tmp_path / "jobs"
    first_worker = JobRegistry(jobs_root)
    job = first_worker.submit(
        "phase3-deck",
        "generate_full_deck",
        entered["checkpoint_id"],
        lambda is_cancelled: workflow.generate_full_deck(
            entered["checkpoint_id"], cancel_requested=is_cancelled
        ),
        cancellable=True,
    )
    assert model_entered.wait(timeout=5)

    second_worker = JobRegistry(jobs_root)
    requested = second_worker.cancel(job["job_id"])
    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    release_model.set()
    terminal = _wait_for_job(first_worker, job["job_id"])

    assert terminal["status"] == "cancelled"
    assert "cancellation_requested" in {
        event["status"] for event in first_worker.events(job["job_id"])
    }
    unchanged = workflow.store.read(include_sample_html=False)
    assert len(unchanged["full_deck_revisions"]) == 1
    assert unchanged["full_deck"]["current_revision_hash"] == entered["full_deck"][
        "current_revision_hash"
    ]
    assert not any(event["event"] == "full_deck_generated" for event in workflow.store.events())


def test_multi_worker_duplicate_submission_reuses_the_same_full_deck_job(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered = _ready_full_deck(tmp_path, mock_runtime)
    original_generate = workflow.gateway.generate
    model_entered = threading.Event()
    release_model = threading.Event()

    def blocking_once(state: str, prompt: str, **kwargs):
        if not model_entered.is_set():
            model_entered.set()
            assert release_model.wait(timeout=5)
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = blocking_once
    jobs_root = tmp_path / "jobs"
    first_worker = JobRegistry(jobs_root)
    first = first_worker.submit(
        "phase3-deck",
        "generate_full_deck",
        entered["checkpoint_id"],
        lambda is_cancelled: workflow.generate_full_deck(
            entered["checkpoint_id"], cancel_requested=is_cancelled
        ),
        cancellable=True,
    )
    assert model_entered.wait(timeout=5)
    second_worker = JobRegistry(jobs_root)
    duplicate = second_worker.submit(
        "phase3-deck",
        "generate_full_deck",
        entered["checkpoint_id"],
        lambda: (_ for _ in ()).throw(AssertionError("duplicate action ran")),
        cancellable=True,
    )
    assert duplicate["job_id"] == first["job_id"]

    release_model.set()
    terminal = _wait_for_job(first_worker, first["job_id"])
    assert terminal["status"] == "succeeded"
    assert len(workflow.store.read(include_sample_html=False)["full_deck_revisions"]) == 2
