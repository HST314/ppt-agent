import time
from pathlib import Path
from shutil import copy2
from threading import Event

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobCancelled, JobRegistry
from api_support import full_deck_session_routes
from api_support.full_deck_sessions import session_snapshot
from agent_core.models import utc_now
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore
from tests.job_support import wait_for_terminal_job
from tests.test_full_deck_session import _ready_full_deck


def finish_job(client: TestClient, response) -> dict:
    assert response.status_code == 202
    return wait_for_job(client, response.json()["job_id"])


def wait_for_job(client: TestClient, job_id: str) -> dict:
    job = wait_for_terminal_job(
        lambda current_job_id: client.get(
            f"/api/jobs/{current_job_id}"
        ).json(),
        job_id,
        fetch_events=lambda current_job_id: client.get(
            f"/api/jobs/{current_job_id}/events"
        ).json(),
    )
    assert job["status"] == "succeeded", job
    return job


def finish_active_project_job(
    client: TestClient,
    project_id: str,
    project: dict,
) -> dict:
    active_job = project.get("active_job")
    if active_job:
        wait_for_job(client, active_job["job_id"])
        project = client.get(f"/api/projects/{project_id}").json()
    return project


def test_api_drives_project_to_narrative(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)

    response = client.post("/api/projects", json={
        "project_id": "api-demo",
        "task_card": {"title": "业务复盘", "objective": "形成行动共识"},
    })
    assert response.status_code == 201
    project = response.json()

    response = client.post("/api/projects/api-demo/jobs", json={
        "operation": "start_clarification",
        "checkpoint_id": project["checkpoint_id"],
    })
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    wait_for_job(client, job_id)
    events = client.get(f"/api/jobs/{job_id}/events").json()
    assert events[0]["status"] == "queued"
    assert events[-1]["status"] == "succeeded"

    project = client.get("/api/projects/api-demo").json()
    card = project["question_card"]
    response = client.post("/api/projects/api-demo/clarification", json={
        "checkpoint_id": project["checkpoint_id"],
        "question_card_id": card["question_card_id"],
        "answers": {question["question_id"]: "answer" for question in card["questions"]},
    })
    assert response.status_code == 200
    project = finish_active_project_job(
        client,
        "api-demo",
        response.json(),
    )
    assert project["phase"] == "ready_to_generate"


def test_api_rejects_unknown_request_fields(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    client = TestClient(main_front.app)

    response = client.post("/api/projects", json={
        "project_id": "api-demo",
        "task_card": {"title": "业务复盘", "objective": "形成行动共识"},
        "provider": "should-not-be-request-controlled",
    })

    assert response.status_code == 422


def test_managed_frontend_opens_the_harness_project_without_duplicate_navigation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_PROJECT_ID", "managed-ppt")
    monkeypatch.setattr(main_front, "HARNESS_TASK_ID", "task-managed-ppt")
    monkeypatch.setattr(main_front, "HARNESS_INSTANCE_ID", "managed-ppt")
    client = TestClient(main_front.app)

    page = client.get("/")
    assert page.status_code == 200
    assert 'id="new-button"' not in page.text
    assert 'id="project-form"' not in page.text
    assert 'id="sidebar"' not in page.text

    context = client.get("/api/runtime-context").json()
    assert context["managed_by_harness"] is True
    assert context["navigation_mode"] == "harness"
    assert context["project_id"] == "managed-ppt"
    assert context["task_id"] == "task-managed-ppt"
    assert context["instance_id"] == "managed-ppt"


def test_api_requires_feedback_only_for_sample_revision(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    client = TestClient(main_front.app)

    missing = client.post("/api/projects/not-created/jobs", json={
        "operation": "revise_sample",
        "checkpoint_id": "checkpoint_value",
    })
    misplaced = client.post("/api/projects/not-created/jobs", json={
        "operation": "generate_sample",
        "checkpoint_id": "checkpoint_value",
        "feedback": "should not be accepted",
    })

    assert missing.status_code == 422
    assert misplaced.status_code == 422


def test_api_drives_sample_generation_feedback_and_approval(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)
    project_id = "sample-api"

    project = client.post("/api/projects", json={
        "project_id": project_id,
        "task_card": {"title": "样品演示", "objective": "确认视觉方向"},
    }).json()
    finish_job(client, client.post(f"/api/projects/{project_id}/jobs", json={
        "operation": "start_clarification",
        "checkpoint_id": project["checkpoint_id"],
    }))
    project = client.get(f"/api/projects/{project_id}").json()
    card = project["question_card"]
    project = client.post(f"/api/projects/{project_id}/clarification", json={
        "checkpoint_id": project["checkpoint_id"],
        "question_card_id": card["question_card_id"],
        "answers": {question["question_id"]: "answer" for question in card["questions"]},
    }).json()
    project = finish_active_project_job(client, project_id, project)

    for operation, document_type in (
        ("generate_narrative", "narrative_structure"),
        ("generate_outline", "slide_outline"),
    ):
        finish_job(client, client.post(f"/api/projects/{project_id}/jobs", json={
            "operation": operation,
            "checkpoint_id": project["checkpoint_id"],
        }))
        project = client.get(f"/api/projects/{project_id}").json()
        document = project["documents"][document_type][-1]
        project = client.post(f"/api/projects/{project_id}/documents/{document_type}/approve", json={
            "checkpoint_id": project["checkpoint_id"],
            "revision_hash": document["revision_hash"],
        }).json()

    assert project["state"] == "ppt_sample"
    assert project["sample_page_count"] == 2
    finish_job(client, client.post(f"/api/projects/{project_id}/jobs", json={
        "operation": "generate_sample",
        "checkpoint_id": project["checkpoint_id"],
    }))
    project = client.get(f"/api/projects/{project_id}").json()
    assert len(project["samples"][-1]["pages"]) == 2
    assert [
        item["source_slide_number"] for item in project["samples"][-1]["package"]["slides"]
    ] == [1, 2]
    assert project["sample_attempts"][-1]["published"] is True
    assert project["sample_attempts"][-1]["reason"].endswith("已发布为 PPT 样品。")

    finish_job(client, client.post(f"/api/projects/{project_id}/jobs", json={
        "operation": "revise_sample",
        "checkpoint_id": project["checkpoint_id"],
        "feedback": "标题更有冲击力，减少辅助文字",
    }))
    project = client.get(f"/api/projects/{project_id}").json()
    revised = project["samples"][-1]
    assert len(project["samples"]) == 1
    assert revised["revision"] == 2
    assert revised["feedback"] == "标题更有冲击力，减少辅助文字"
    assert [item["revision"] for item in project["sample_revisions"]] == [2, 1]
    assert project["sample_revisions"][0]["current"] is True

    first_hash = project["sample_revisions"][1]["revision_hash"]
    historical = client.get(
        f"/api/projects/{project_id}/samples/revisions/{first_hash}"
    )
    assert historical.status_code == 200
    assert historical.json()["revision"] == 1
    assert all("html" not in page for page in historical.json()["pages"])
    assert historical.json()["preview_url"].endswith("/preview/index.html")

    restored = client.post(
        f"/api/projects/{project_id}/samples/revisions/{first_hash}/restore",
        json={"checkpoint_id": project["checkpoint_id"]},
    )
    assert restored.status_code == 200
    project = restored.json()
    restored_sample = project["samples"][-1]
    assert restored_sample["revision"] == 1
    assert project["current_sample_revision_hash"] == first_hash
    assert [item["revision"] for item in project["sample_revisions"]] == [2, 1]

    finish_job(client, client.post(f"/api/projects/{project_id}/jobs", json={
        "operation": "revise_sample",
        "checkpoint_id": project["checkpoint_id"],
        "feedback": "从首版继续调整",
    }))
    project = client.get(f"/api/projects/{project_id}").json()
    restored_sample = project["samples"][-1]
    assert restored_sample["revision"] == 3
    assert restored_sample["parent_revision_hash"] == first_hash

    preview = client.get(restored_sample["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-security-policy"].startswith("sandbox allow-scripts; default-src 'none'")
    assert preview.headers["cross-origin-resource-policy"] == "cross-origin"
    assert "<script>" in preview.text
    exported = client.get(restored_sample["export_url"])
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"

    activity = client.get(f"/api/projects/{project_id}/activity").json()
    kinds = {item["kind"] for item in activity["events"]}
    assert {"job", "model", "artifact"} <= kinds

    approved = client.post(f"/api/projects/{project_id}/samples/approve", json={
        "checkpoint_id": project["checkpoint_id"],
        "revision_hash": restored_sample["revision_hash"],
    })
    assert approved.status_code == 200
    assert approved.json()["phase"] == "completed"
    sample_snapshot = next(
        item for item in approved.json()["progress_snapshots"] if item["stage"] == "ppt_sample"
    )
    snapshot_sample = sample_snapshot["snapshot"]["samples"][-1]
    assert snapshot_sample["package"]["slides"]
    assert all(
        "content" not in item for item in snapshot_sample["package"]["files"]
    )

    prompt_export = client.get(f"/api/projects/{project_id}/audit/prompt-calls.jsonl")
    assert prompt_export.status_code == 200
    assert prompt_export.headers["content-type"].startswith("application/x-ndjson")
    prompt_calls = [line for line in prompt_export.text.splitlines() if line]
    assert len(prompt_calls) >= 5

    branch = client.post(
        f"/api/projects/{project_id}/samples/revisions/{first_hash}/branches",
        json={
            "checkpoint_id": approved.json()["checkpoint_id"],
            "name": "sample-history",
        },
    )
    assert branch.status_code == 200
    assert branch.json()["branch"] == "sample-history"
    assert branch.json()["samples"][-1]["revision_hash"] == first_hash


def test_api_updates_runtime_and_switches_branches(tmp_path: Path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    root = Path(__file__).parents[1]
    copy2(root / "runtime.yaml", app_root / "runtime.yaml")
    copy2(root / "model_config.yaml", app_root / "model_config.yaml")
    (app_root / "skills").mkdir()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", ManagedRuntime(app_root))
    client = TestClient(main_front.app)

    context = client.get("/api/runtime-context").json()
    context["model_bindings"][2]["model"] = "outline-preview-v2"
    context["policy"]["sample_page_count"] = 3
    update = client.put("/api/runtime-context", json={
        "model_config_id": "api-editable",
        "model_bindings": context["model_bindings"],
        "policy": context["policy"],
    })
    assert update.status_code == 200
    assert update.json()["model_bindings"][2]["model"] == "outline-preview-v2"
    assert update.json()["policy"]["sample_page_count"] == 3
    assert "api_key_env" not in update.json()["model_bindings"][2]

    project = client.post("/api/projects", json={
        "project_id": "branch-demo",
        "task_card": {"title": "分支演示", "objective": "验证切换"},
    }).json()
    assert project["sample_page_count"] == 3
    main_head = project["checkpoint_id"]
    created = client.post("/api/projects/branch-demo/branches", json={
        "checkpoint_id": main_head,
        "name": "alternate",
    })
    assert created.status_code == 200
    alternate_head = created.json()["checkpoint_id"]
    switched = client.post("/api/projects/branch-demo/branches/switch", json={"checkpoint_id": main_head})
    assert switched.status_code == 200
    assert switched.json()["branch"] == "main"
    assert switched.json()["branches"]["alternate"] == alternate_head


def test_api_creates_rerun_branch_from_progress_snapshot(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)

    project = client.post("/api/projects", json={
        "project_id": "snapshot-branch",
        "task_card": {"title": "快照分支", "objective": "验证阶段回退"},
    }).json()
    snapshot = project["progress_snapshots"][0]
    response = client.post("/api/projects/snapshot-branch/branches", json={
        "checkpoint_id": snapshot["checkpoint_id"],
        "name": "intake-rerun",
        "mode": "rerun_stage",
        "stage": "intake",
    })

    assert response.status_code == 200
    branched = response.json()
    assert branched["branch"] == "intake-rerun"
    assert branched["state"] == "intake"
    assert branched["phase"] == "ready_for_clarification"
    assert branched["branch_meta"]["intake-rerun"]["from_checkpoint"] == snapshot["checkpoint_id"]
    assert branched["progress_snapshots"][0]["stage"] == "intake"


def test_api_rejects_stage_rerun_from_noncanonical_checkpoint(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)

    project = client.post("/api/projects", json={
        "project_id": "snapshot-boundary",
        "task_card": {"title": "快照边界", "objective": "拒绝错配阶段"},
    }).json()
    response = client.post("/api/projects/snapshot-boundary/branches", json={
        "checkpoint_id": project["checkpoint_id"],
        "name": "wrong-stage",
        "mode": "rerun_stage",
        "stage": "slide_outline",
    })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stage_snapshot_required"


@pytest.mark.parametrize(
    "unsafe_parameters",
    [
        {"api-key": "audit-secret"},
        {"Authorization ": "Bearer audit-secret"},
        {"headers": [{"name": "Authorization", "value": "Bearer audit-secret"}]},
    ],
    ids=["api-key", "authorization-trailing-space", "nested-headers"],
)
def test_api_rejects_non_whitelisted_model_parameters(
    tmp_path: Path,
    monkeypatch,
    unsafe_parameters: dict,
) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    root = Path(__file__).parents[1]
    copy2(root / "runtime.yaml", app_root / "runtime.yaml")
    copy2(root / "model_config.yaml", app_root / "model_config.yaml")
    (app_root / "skills").mkdir()
    monkeypatch.setattr(main_front, "runtime", ManagedRuntime(app_root))
    client = TestClient(main_front.app)

    context = client.get("/api/runtime-context").json()
    original_parameters = context["model_bindings"][0]["parameters"]
    context["model_bindings"][0]["parameters"] = unsafe_parameters

    update = client.put("/api/runtime-context", json={
        "model_config_id": context["model_config_id"],
        "model_bindings": context["model_bindings"],
        "policy": context["policy"],
    })

    assert update.status_code == 422
    persisted = client.get("/api/runtime-context").json()
    assert persisted["model_bindings"][0]["parameters"] == original_parameters
    assert "audit-secret" not in str(persisted)


def test_activity_exposes_live_tool_round_progress(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)
    project = client.post("/api/projects", json={
        "project_id": "live-activity",
        "task_card": {"title": "实时状态", "objective": "观察工具轮次"},
    }).json()
    store = ProjectStore(project_root, "live-activity")
    prompt_call_id = store.start_prompt_call(
        state="ppt_sample",
        messages=[{"role": "user", "content": "生成样品"}],
        template_id="ppt_sample",
        template_version=1,
        template_hash="sha256:template",
        model_config_hash="sha256:model",
        runtime_config_hash="sha256:runtime",
        skills_hash="sha256:skills",
        parameters={"provider": "test", "model": "status-model"},
    )
    traces = [{
        "type": "tool_call",
        "tool": "read",
        "path": "template.md",
        "round": 4,
        "round_limit": 20,
        "at": utc_now(),
    }]
    store.append_prompt_call_progress(
        prompt_call_id,
        status="tool_round_completed",
        details={
            "round": 4,
            "round_limit": 20,
            "tools": ["read"],
            "tool_call_count": 5,
            "skill_read_count": 4,
            "recent_action": "read · template.md",
            "elapsed_seconds": 12.5,
        },
        traces=traces,
        messages=[
            {"role": "user", "content": "生成样品"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_4",
                "function": {"name": "read", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_4", "content": "{}"},
        ],
    )

    activity = client.get(
        f"/api/projects/{project['project_id']}/activity"
    ).json()

    assert activity["summary"]["progress"]["round"] == 4
    assert any(
        item["title"] == "tool_round_completed"
        and item["summary"].startswith("第 4/20 轮")
        for item in activity["events"]
    )


def test_resume_sample_rejects_a_stale_project_checkpoint_before_queueing(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)
    client.post("/api/projects", json={
        "project_id": "stale-sample-resume",
        "task_card": {"title": "续跑边界", "objective": "拒绝旧检查点"},
    })

    response = client.post(
        "/api/projects/stale-sample-resume/samples/attempts/"
        "prompt_0123456789abcdef0123456789abcdef/resume",
        json={
            "checkpoint_id": "checkpoint_ffffffffffffffffffffffff",
            "additional_rounds": 10,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sample_resume_stale"


def test_full_deck_generation_session_api_start_is_idempotent_and_scoped(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-api-start",
    )
    release = Event()

    def hold_session(self, session_id, **kwargs):
        while not release.wait(timeout=0.01):
            cancel_requested = kwargs.get("cancel_requested")
            if cancel_requested is not None and cancel_requested():
                snapshot = self.store.full_deck_generation_session(session_id)
                self.store.update_full_deck_generation_session(
                    session_id,
                    snapshot["session_version"],
                    status="cancelled",
                    completed_batches=snapshot["completed_batches"],
                )
                raise JobCancelled("cancelled by API test worker")
        return self.store.full_deck_generation_session(session_id)

    monkeypatch.setattr(
        main_front.Workflow,
        "run_full_deck_generation_session",
        hold_session,
    )
    client = TestClient(main_front.app)
    payload = {
        "checkpoint_id": entered["checkpoint_id"],
        "revision_hash": entered["full_deck"]["current_revision_hash"],
    }

    first = client.post(
        "/api/projects/session-api-start/full-deck/generation-sessions",
        json=payload,
    )
    second = client.post(
        "/api/projects/session-api-start/full-deck/generation-sessions",
        json=payload,
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["session"]["session_id"] == second.json()["session"][
        "session_id"
    ]
    assert first.json()["job"]["job_id"] == second.json()["job"]["job_id"]
    session_id = first.json()["session"]["session_id"]
    fetched = client.get(
        f"/api/projects/session-api-start/full-deck/generation-sessions/{session_id}"
    )
    assert fetched.status_code == 200
    assert fetched.json()["progress"]["ready_pages"] == 2
    assert fetched.json()["progress"]["total_pages"] == 16
    project = client.get("/api/projects/session-api-start").json()
    assert project["full_deck_generation_session"]["session_id"] == session_id

    unknown = client.post(
        "/api/projects/session-api-start/full-deck/generation-sessions",
        json=payload | {"provider": "not-accepted"},
    )
    stale = client.post(
        "/api/projects/session-api-start/full-deck/generation-sessions",
        json=payload | {"checkpoint_id": "checkpoint_" + "f" * 24},
    )
    wrong_revision = client.post(
        "/api/projects/session-api-start/full-deck/generation-sessions",
        json=payload | {"revision_hash": "sha256:" + "f" * 64},
    )
    client.post(
        "/api/projects",
        json={
            "project_id": "other-project",
            "task_card": {"title": "其他工程", "objective": "验证会话隔离"},
        },
    )
    cross_project = client.get(
        f"/api/projects/other-project/full-deck/generation-sessions/{session_id}"
    )

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "full_deck_session_invalid"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "full_deck_session_stale"
    assert wrong_revision.status_code == 409
    assert wrong_revision.json()["error"]["code"] == "full_deck_session_stale"
    assert cross_project.status_code == 404
    assert cross_project.json()["error"]["code"] == "full_deck_session_not_found"

    current = client.get(
        f"/api/projects/session-api-start/full-deck/generation-sessions/{session_id}"
    ).json()
    cancelled = client.post(
        f"/api/projects/session-api-start/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": current["session_version"]},
    )
    repeated_cancel = client.post(
        f"/api/projects/session-api-start/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": current["session_version"]},
    )
    assert cancelled.status_code == repeated_cancel.status_code == 200
    assert cancelled.json().get("cancel_requested") or cancelled.json()[
        "status"
    ] == "cancelled"
    terminal = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        first.json()["job"]["job_id"],
    )
    assert terminal["status"] in {"succeeded", "cancelled"}
    final_session = client.get(
        f"/api/projects/session-api-start/full-deck/generation-sessions/{session_id}"
    ).json()
    assert final_session["status"] == "cancelled"


def test_full_deck_generation_session_cancel_retry_after_worker_commits(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-api-cancel-race",
    )
    release = Event()

    def hold_session(self, session_id, **kwargs):
        while not release.wait(timeout=0.01):
            cancel_requested = kwargs.get("cancel_requested")
            if cancel_requested is not None and cancel_requested():
                snapshot = self.store.full_deck_generation_session(session_id)
                self.store.update_full_deck_generation_session(
                    session_id,
                    snapshot["session_version"],
                    status="cancelled",
                    completed_batches=snapshot["completed_batches"],
                )
                raise JobCancelled("cancelled by API test worker")
        return self.store.full_deck_generation_session(session_id)

    monkeypatch.setattr(
        main_front.Workflow,
        "run_full_deck_generation_session",
        hold_session,
    )
    client = TestClient(main_front.app)
    payload = {
        "checkpoint_id": entered["checkpoint_id"],
        "revision_hash": entered["full_deck"]["current_revision_hash"],
    }
    started = client.post(
        "/api/projects/session-api-cancel-race/full-deck/generation-sessions",
        json=payload,
    )
    assert started.status_code == 202
    session_id = started.json()["session"]["session_id"]
    store = main_front.store_for("session-api-cancel-race")
    stale = session_snapshot(store, session_id)

    cancelled = client.post(
        f"/api/projects/session-api-cancel-race/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": stale["session_version"]},
    )
    assert cancelled.status_code == 200
    terminal = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        started.json()["job"]["job_id"],
    )
    assert terminal["status"] == "cancelled"

    # Reproduce the CI interleaving: the entry snapshot read still returns the
    # pre-cancel state while the job is already terminal.
    real_snapshot = full_deck_session_routes.session_snapshot
    reads = {"count": 0}

    def stale_once(store_arg, session_id_arg):
        reads["count"] += 1
        if reads["count"] == 1:
            return stale
        return real_snapshot(store_arg, session_id_arg)

    monkeypatch.setattr(
        full_deck_session_routes, "session_snapshot", stale_once
    )
    repeated = client.post(
        f"/api/projects/session-api-cancel-race/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": stale["session_version"]},
    )
    assert repeated.status_code == 200


def test_full_deck_generation_session_cancel_retry_while_running(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-api-cancel-running",
    )
    release = Event()

    def hold_session(self, session_id, **kwargs):
        snapshot = self.store.full_deck_generation_session(session_id)
        self.store.update_full_deck_generation_session(
            session_id,
            snapshot["session_version"],
            status="running",
        )
        while not release.wait(timeout=0.01):
            cancel_requested = kwargs.get("cancel_requested")
            if cancel_requested is not None and cancel_requested():
                snapshot = self.store.full_deck_generation_session(session_id)
                self.store.update_full_deck_generation_session(
                    session_id,
                    snapshot["session_version"],
                    status="cancelled",
                    completed_batches=snapshot["completed_batches"],
                )
                raise JobCancelled("cancelled by API test worker")
        return self.store.full_deck_generation_session(session_id)

    monkeypatch.setattr(
        main_front.Workflow,
        "run_full_deck_generation_session",
        hold_session,
    )
    client = TestClient(main_front.app)
    payload = {
        "checkpoint_id": entered["checkpoint_id"],
        "revision_hash": entered["full_deck"]["current_revision_hash"],
    }
    started = client.post(
        "/api/projects/session-api-cancel-running/full-deck/generation-sessions",
        json=payload,
    )
    assert started.status_code == 202
    session_id = started.json()["session"]["session_id"]
    store = main_front.store_for("session-api-cancel-running")
    for _ in range(200):
        if session_snapshot(store, session_id)["status"] == "running":
            break
        time.sleep(0.01)
    stale = session_snapshot(store, session_id)
    assert stale["status"] == "running"

    cancelled = client.post(
        f"/api/projects/session-api-cancel-running/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": stale["session_version"]},
    )
    assert cancelled.status_code == 200
    terminal = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        started.json()["job"]["job_id"],
    )
    assert terminal["status"] == "cancelled"

    # Same interleaving as above, with the entry snapshot read still showing
    # the running state: the endpoint must refresh instead of rejecting.
    real_snapshot = full_deck_session_routes.session_snapshot
    reads = {"count": 0}

    def stale_once(store_arg, session_id_arg):
        reads["count"] += 1
        if reads["count"] == 1:
            return stale
        return real_snapshot(store_arg, session_id_arg)

    monkeypatch.setattr(
        full_deck_session_routes, "session_snapshot", stale_once
    )
    repeated = client.post(
        f"/api/projects/session-api-cancel-running/full-deck/generation-sessions/"
        f"{session_id}/cancel",
        json={"session_version": stale["session_version"]},
    )
    assert repeated.status_code == 200


def test_full_deck_generation_feature_rollback_preserves_session_evidence(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-feature-rollback",
    )
    session = workflow.start_full_deck_generation_session(
        entered["checkpoint_id"]
    )
    mock_runtime.policy = mock_runtime.policy.model_copy(
        update={"full_deck_batched_generation_enabled": False}
    )
    client = TestClient(main_front.app)
    session_url = (
        "/api/projects/session-feature-rollback/full-deck/generation-sessions/"
        f"{session['session_id']}"
    )

    existing = client.get(session_url)
    rejected = client.post(
        "/api/projects/session-feature-rollback/full-deck/generation-sessions",
        json={
            "checkpoint_id": entered["checkpoint_id"],
            "revision_hash": entered["full_deck"]["current_revision_hash"],
        },
    )
    audit = client.get(
        "/api/projects/session-feature-rollback/audit/export"
    ).json()

    assert existing.status_code == 200
    assert existing.json()["session_id"] == session["session_id"]
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == (
        "full_deck_batched_generation_disabled"
    )
    assert audit["full_deck_generation"]["sessions"][0]["session_id"] == (
        session["session_id"]
    )


def test_disabled_batched_generation_keeps_legacy_full_deck_actions_available(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    mock_runtime.policy = mock_runtime.policy.model_copy(
        update={"full_deck_batched_generation_enabled": False}
    )
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="legacy-full-deck-rollback",
    )
    client = TestClient(main_front.app)

    generated = finish_job(client, client.post(
        "/api/projects/legacy-full-deck-rollback/jobs",
        json={
            "operation": "generate_full_deck",
            "checkpoint_id": entered["checkpoint_id"],
        },
    ))
    assert generated["session_id"] is None
    project = client.get("/api/projects/legacy-full-deck-rollback").json()
    first_revision = project["full_deck_revision"]
    assert project["full_deck_generation_session"] is None
    assert client.get(first_revision["preview_url"]).status_code == 200
    assert client.get(first_revision["export_url"]).status_code == 200

    revised = finish_job(client, client.post(
        "/api/projects/legacy-full-deck-rollback/jobs",
        json={
            "operation": "revise_full_deck",
            "checkpoint_id": project["checkpoint_id"],
            "revision_hash": first_revision["revision_hash"],
            "feedback": "强化最后一页的行动结论。",
        },
    ))
    assert revised["status"] == "succeeded"
    updated = client.get("/api/projects/legacy-full-deck-rollback").json()
    assert updated["full_deck_revision"]["revision_hash"] != (
        first_revision["revision_hash"]
    )


def test_full_deck_generation_session_controls_preview_and_conflicts(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-api-controls",
    )
    session = workflow.start_full_deck_generation_session(entered["checkpoint_id"])
    session_id = session["session_id"]
    original_generate = workflow.gateway.generate
    requested_pause = False

    def pause_during_first_batch(state, prompt, **kwargs):
        nonlocal requested_pause
        if not requested_pause:
            requested_pause = True
            running = workflow.store.full_deck_generation_session(session_id)
            workflow.request_full_deck_generation_pause(
                session_id,
                running["session_version"],
            )
        return original_generate(state, prompt, **kwargs)

    workflow.gateway.generate = pause_during_first_batch
    paused = workflow.run_full_deck_generation_session(session_id)
    assert paused["status"] == "paused"
    client = TestClient(main_front.app)

    details = client.get(
        f"/api/projects/session-api-controls/full-deck/generation-sessions/{session_id}"
    )
    assert details.status_code == 200
    body = details.json()
    assert body["progress"]["ready_pages"] == 6
    assert body["preview_url"].endswith(
        f"/preview/index.html?v={body['session_version']}"
    )
    preview = client.get(body["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-security-policy"].startswith(
        "sandbox allow-scripts; default-src 'none'"
    )
    traversal = client.get(
        f"/api/projects/session-api-controls/full-deck/generation-sessions/"
        f"{session_id}/preview/%2E%2E%2Findex.html"
    )
    assert traversal.status_code in {404, 422}

    directive = client.post(
        f"/api/projects/session-api-controls/full-deck/generation-sessions/"
        f"{session_id}/directives",
        json={
            "session_version": body["session_version"],
            "content": "  后续页面减少装饰元素。  ",
        },
    )
    assert directive.status_code == 200
    assert directive.json()["content"] == "后续页面减少装饰元素。"
    assert directive.json()["apply_from_batch_index"] == 2
    assert directive.json()["apply_from_slide_numbers"] == [7, 8, 9, 10]
    stale_directive = client.post(
        f"/api/projects/session-api-controls/full-deck/generation-sessions/"
        f"{session_id}/directives",
        json={
            "session_version": body["session_version"],
            "content": "这一条使用了陈旧版本。",
        },
    )
    assert stale_directive.status_code == 409
    assert stale_directive.json()["error"]["code"] == "full_deck_session_conflict"

    release = Event()

    def hold_resumed_session(self, held_session_id, **_kwargs):
        release.wait(timeout=5)
        return self.store.full_deck_generation_session(held_session_id)

    monkeypatch.setattr(
        main_front.Workflow,
        "run_full_deck_generation_session",
        hold_resumed_session,
    )
    current = client.get(
        f"/api/projects/session-api-controls/full-deck/generation-sessions/{session_id}"
    ).json()
    resume_url = (
        f"/api/projects/session-api-controls/full-deck/generation-sessions/"
        f"{session_id}/resume"
    )
    resumed = client.post(
        resume_url,
        json={"session_version": current["session_version"]},
    )
    repeated = client.post(
        resume_url,
        json={"session_version": current["session_version"]},
    )
    assert resumed.status_code == repeated.status_code == 202
    assert resumed.json()["job"]["job_id"] == repeated.json()["job"]["job_id"]
    release.set()
    wait_for_job(client, resumed.json()["job"]["job_id"])


def test_full_deck_generation_pause_and_retry_api_are_versioned_and_idempotent(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-api-retry",
    )
    created = workflow.start_full_deck_generation_session(
        entered["checkpoint_id"]
    )
    session_id = created["session_id"]
    claimed = workflow.store.claim_full_deck_generation_batch(
        session_id,
        expected_session_version=created["session_version"],
    )
    assert claimed is not None
    running = workflow.store.full_deck_generation_session(session_id)
    client = TestClient(main_front.app)
    pause_url = (
        f"/api/projects/session-api-retry/full-deck/generation-sessions/"
        f"{session_id}/pause"
    )

    paused_requested = client.post(
        pause_url,
        json={"session_version": running["session_version"]},
    )
    repeated_pause = client.post(
        pause_url,
        json={"session_version": running["session_version"]},
    )
    stale_pause = client.post(
        pause_url,
        json={"session_version": created["session_version"]},
    )
    unknown = client.post(
        pause_url,
        json={
            "session_version": running["session_version"],
            "force": True,
        },
    )

    assert paused_requested.status_code == repeated_pause.status_code == 200
    assert paused_requested.json()["status"] == "pause_requested"
    assert stale_pause.status_code == 409
    assert stale_pause.json()["error"]["code"] == "full_deck_session_conflict"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "full_deck_session_invalid"

    pause_snapshot = workflow.store.full_deck_generation_session(session_id)
    failed = workflow.store.fail_full_deck_generation_batch(
        session_id,
        1,
        expected_session_version=pause_snapshot["session_version"],
        error={
            "code": "full_deck_batch_failed",
            "message": "当前批未完成，可重试当前批。",
        },
    )
    retry_url = (
        f"/api/projects/session-api-retry/full-deck/generation-sessions/"
        f"{session_id}/retry"
    )
    retried = client.post(
        retry_url,
        json={"session_version": failed["session_version"]},
    )
    assert retried.status_code == 202
    wait_for_job(client, retried.json()["job"]["job_id"])
    repeated_retry = client.post(
        retry_url,
        json={"session_version": failed["session_version"]},
    )
    assert repeated_retry.status_code == 202
    assert repeated_retry.json()["job"]["job_id"] == retried.json()["job"][
        "job_id"
    ]
    completed = client.get(
        f"/api/projects/session-api-retry/full-deck/generation-sessions/{session_id}"
    ).json()
    assert completed["status"] == "completed"
    project = client.get("/api/projects/session-api-retry").json()
    assert project["full_deck_generation_session"]["status"] == "completed"
    audit = client.get("/api/projects/session-api-retry/audit/export").json()
    generation = audit["full_deck_generation"]
    audited_session = generation["sessions"][0]
    prompt_call_ids = {
        item["prompt_call_id"] for item in audit["prompt_calls"]
    }
    batch_prompt_call_ids = {
        prompt_call_id
        for batch in audited_session["batches"]
        for prompt_call_id in batch["prompt_call_ids"]
    }
    assert audited_session["published_revision_hash"] == (
        project["full_deck_revision"]["revision_hash"]
    )
    assert batch_prompt_call_ids
    assert batch_prompt_call_ids <= prompt_call_ids
    assert audited_session["packages"]
    assert all(package["files"] for package in audited_session["packages"])
    assert all(
        file["sha256"].startswith("sha256:")
        for package in audited_session["packages"]
        for file in package["files"]
    )


def test_full_deck_finalization_failure_reports_reason_and_retry_publishes(
    tmp_path: Path,
    monkeypatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    registry = JobRegistry(project_root / ".jobs")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", registry)
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    workflow, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-finalize-diagnosis",
    )
    client = TestClient(main_front.app)

    import agent_core.full_deck_session as session_module

    original_compose = session_module.compose_full_deck

    def failing_compose(spec):
        raise ValueError("injected final Composer failure")

    monkeypatch.setattr(session_module, "compose_full_deck", failing_compose)

    start = client.post(
        "/api/projects/session-finalize-diagnosis/full-deck/generation-sessions",
        json={
            "checkpoint_id": entered["checkpoint_id"],
            "revision_hash": entered["full_deck"]["current_revision_hash"],
        },
    )
    assert start.status_code == 202, start.text
    session_id = start.json()["session"]["session_id"]
    failed_job = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        start.json()["job"]["job_id"],
    )
    assert failed_job["status"] == "failed"
    assert failed_job["error"]["code"] == "full_deck_finalization_failed"

    failed = client.get(
        f"/api/projects/session-finalize-diagnosis/full-deck/generation-sessions/"
        f"{session_id}"
    ).json()
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "full_deck_finalization_failed"
    assert failed["error"]["detail"] == (
        "生成会话最终发布失败：injected final Composer failure"
    )
    assert "retry" in failed["capabilities"]

    monkeypatch.setattr(session_module, "compose_full_deck", original_compose)
    retry_url = (
        f"/api/projects/session-finalize-diagnosis/full-deck/generation-sessions/"
        f"{session_id}/retry"
    )
    retried = client.post(
        retry_url,
        json={"session_version": failed["session_version"]},
    )
    assert retried.status_code == 202, retried.text
    retry_job = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        retried.json()["job"]["job_id"],
    )
    assert retry_job["status"] == "succeeded"

    completed = client.get(
        f"/api/projects/session-finalize-diagnosis/full-deck/generation-sessions/"
        f"{session_id}"
    ).json()
    assert completed["status"] == "completed"
    assert completed["published_revision_hash"]
    export_url = (
        "/api/projects/session-finalize-diagnosis/full-deck/revisions/"
        f"{completed['published_revision_hash']}/export"
    )
    exported = client.get(export_url)
    assert exported.status_code == 200
