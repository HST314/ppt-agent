import time
from pathlib import Path
from shutil import copy2

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry
from configs.runtime import ManagedRuntime


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
    assert response.json()["phase"] == "ready_to_generate"


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
    update = client.put("/api/runtime-context", json={
        "model_config_id": "api-editable",
        "model_bindings": context["model_bindings"],
        "policy": context["policy"],
    })
    assert update.status_code == 200
    assert update.json()["model_bindings"][2]["model"] == "outline-preview-v2"
    assert "api_key_env" not in update.json()["model_bindings"][2]

    project = client.post("/api/projects", json={
        "project_id": "branch-demo",
        "task_card": {"title": "分支演示", "objective": "验证切换"},
    }).json()
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
