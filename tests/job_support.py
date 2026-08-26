from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
DEFAULT_JOB_TIMEOUT_SECONDS = 10.0


def wait_for_terminal_job(
    fetch_job: Callable[[str], dict[str, Any]],
    job_id: str,
    *,
    fetch_events: Callable[[str], list[dict[str, Any]]] | None = None,
    timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: float = 0.01,
) -> dict[str, Any]:
    """Wait for a job using elapsed time and report useful timeout evidence."""

    started = time.monotonic()
    deadline = started + timeout
    while True:
        job = fetch_job(job_id)
        if job["status"] not in ACTIVE_JOB_STATUSES:
            return job
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            events = fetch_events(job_id) if fetch_events else []
            elapsed = time.monotonic() - started
            raise AssertionError(
                f"job {job_id} did not reach a terminal state within "
                f"{elapsed:.2f}s; last_job={job!r}; events={events!r}"
            )
        time.sleep(min(poll_interval, remaining))
