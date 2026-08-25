from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Acquire a process-safe advisory lock with a bounded wait."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as lock_file:
        while True:
            try:
                flock(lock_file.fileno(), LOCK_EX | LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring lock: {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            flock(lock_file.fileno(), LOCK_UN)


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync. Content hashes
            # make a retry safe after the file itself has been synchronized.
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
