import time
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry


def test_api_drives_project_to_narrative(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
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
