from __future__ import annotations

import pytest

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
