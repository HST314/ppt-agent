from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

if os.name == "nt":
    import errno
    import msvcrt

    def _try_exclusive_lock(lock_file: BinaryIO) -> bool:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            return False
        return True

    def _release_exclusive_lock(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_exclusive_lock(lock_file: BinaryIO) -> bool:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _release_exclusive_lock(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Acquire a process-safe advisory lock with a bounded wait."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as lock_file:
        while True:
            if _try_exclusive_lock(lock_file):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring lock: {path.name}")
            time.sleep(0.05)
        try:
            yield
        finally:
            _release_exclusive_lock(lock_file)


def _windows_extended_length_text(text: str) -> str:
    """Prefix an absolute Windows path with ``\\\\?\\`` to bypass MAX_PATH.

    The retained full-deck layout nests content-addressed directories deeply
    enough that absolute paths can exceed the classic 260-character limit;
    without the prefix, ``os.open`` fails with a misleading
    ``FileNotFoundError`` on Windows.
    """

    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def os_level_path(path: Path) -> str:
    """Return an OS-level path string, extended-length safe on Windows."""

    text = os.fspath(path)
    if os.name == "nt":
        text = _windows_extended_length_text(os.path.abspath(text))
    return text


def path_is_file(path: Path) -> bool:
    """Return whether *path* is a regular file, including long Windows paths."""

    return os.path.isfile(os_level_path(path))


def read_bytes(path: Path) -> bytes:
    """Read *path* without reintroducing the classic Windows MAX_PATH limit."""

    with open(os_level_path(path), "rb") as stream:
        return stream.read()


def atomic_bytes(path: Path, content: bytes) -> None:
    target = os_level_path(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
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
