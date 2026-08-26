import time
from pathlib import Path
from shutil import copy2

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry
from agent_core.models import utc_now
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


def finish_job(client: TestClient, response) -> dict:
    assert response.status_code == 202
    return wait_for_job(client, response.json()["job_id"])


def wait_for_job(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
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
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert job["status"] == "succeeded"
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
