from __future__ import annotations

import sqlite3

import pytest

from agent_core.jobs import JobRegistry
from tests.job_support import wait_for_terminal_job


def test_wait_for_terminal_job_is_not_limited_by_poll_count() -> None:
    polls = 0

    def fetch_job(job_id: str) -> dict:
        nonlocal polls
        polls += 1
        return {
            "job_id": job_id,
            "status": "running" if polls <= 150 else "succeeded",
        }

    job = wait_for_terminal_job(
        fetch_job,
        "job_slow_runner",
        timeout=1,
        poll_interval=0,
    )

    assert job["status"] == "succeeded"
    assert polls == 151


def test_wait_for_terminal_job_reports_state_and_events_on_timeout() -> None:
    with pytest.raises(AssertionError) as raised:
        wait_for_terminal_job(
            lambda job_id: {"job_id": job_id, "status": "running"},
            "job_stuck",
            fetch_events=lambda _job_id: [{"status": "queued"}],
            timeout=0,
        )

    message = str(raised.value)
    assert "job_stuck" in message
    assert "'status': 'running'" in message
    assert "'status': 'queued'" in message


def test_job_registry_persists_generation_session_progress(tmp_path) -> None:
    registry = JobRegistry(tmp_path / "jobs")

    job = registry.submit(
        "progress-project",
        "generate_full_deck",
        "checkpoint_" + "1" * 24,
        lambda report: report({
            "stage": "generating",
            "current_batch": 2,
            "total_batches": 4,
            "ready_pages": 10,
            "total_pages": 16,
        }),
        session_id="fullsession_" + "2" * 32,
        initial_progress={"stage": "queued"},
        progress_reporting=True,
    )
    terminal = wait_for_terminal_job(registry.get, job["job_id"], timeout=2)

    assert terminal["status"] == "succeeded"
    assert terminal["session_id"] == "fullsession_" + "2" * 32
    assert terminal["progress"] == {
        "stage": "generating",
        "current_batch": 2,
        "total_batches": 4,
        "ready_pages": 10,
        "total_pages": 16,
    }


def test_job_registry_migrates_existing_database_for_session_progress(tmp_path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    database = root / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_json TEXT,
                owner_pid INTEGER,
                cancellable INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                request_key TEXT
            );
            CREATE TABLE job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                at TEXT NOT NULL,
                status TEXT NOT NULL,
                operation TEXT NOT NULL,
                error_json TEXT
            );
            INSERT INTO jobs(
                job_id, project_id, operation, checkpoint_id, status, created_at
            ) VALUES(
                'job_existing', 'project', 'generate_full_deck',
                'checkpoint_existing', 'succeeded', '2026-01-01T00:00:00Z'
            );
            """
        )

    registry = JobRegistry(root)
    existing = registry.get("job_existing")

    assert existing["session_id"] is None
    assert existing["progress"] is None
