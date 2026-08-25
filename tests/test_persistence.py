from multiprocessing import get_context
from pathlib import Path

import pytest

from storage.persistence import exclusive_file_lock


def _hold_file_lock(path: str, acquired, release) -> None:
    with exclusive_file_lock(Path(path), timeout_seconds=5):
        acquired.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test did not release held file lock")


def test_exclusive_file_lock_times_out_and_can_be_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "project.lock"
    context = get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_file_lock,
        args=(str(path), acquired, release),
    )

    process.start()
    try:
        assert acquired.wait(timeout=10)
        with pytest.raises(TimeoutError, match="timed out acquiring lock: project.lock"):
            with exclusive_file_lock(path, timeout_seconds=0.1):
                pytest.fail("a second process already holds the lock")
    finally:
        release.set()
        process.join(timeout=10)

    assert process.exitcode == 0
    with exclusive_file_lock(path, timeout_seconds=1):
        assert path.is_file()
