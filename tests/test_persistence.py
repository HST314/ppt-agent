import os
from multiprocessing import get_context
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationDirective,
    FullDeckGenerationSession,
)
from storage.persistence import (
    _os_level_path,
    _windows_extended_length_text,
    atomic_bytes,
    exclusive_file_lock,
)
from storage.retained_project import (
    STAGING_NAME_MAX_LENGTH,
    _staging_dir_name,
)


def test_windows_extended_length_text_prefixes_drive_unc_and_passthrough() -> None:
    assert (
        _windows_extended_length_text(r"D:\a_test\deck\sources")
        == r"\\?\D:\a_test\deck\sources"
    )
    assert (
        _windows_extended_length_text(r"\\server\share\deck")
        == r"\\?\UNC\server\share\deck"
    )
    assert _windows_extended_length_text(r"\\?\D:\deck") == r"\\?\D:\deck"


def test_os_level_path_is_identity_off_windows(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "file.html"
    if os.name == "nt":
        assert _os_level_path(path).startswith("\\\\?\\")
    else:
        assert _os_level_path(path) == str(path)


def test_atomic_bytes_writes_target_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "deep" / "nested" / "page.html"
    atomic_bytes(target, b"<html></html>")
    assert target.read_bytes() == b"<html></html>"
    atomic_bytes(target, b"<html>replaced</html>")
    assert target.read_bytes() == b"<html>replaced</html>"
    assert [entry.name for entry in target.parent.iterdir()] == ["page.html"]


def test_staging_dir_name_stays_short_unique_and_hidden() -> None:
    revision = "sha256:" + "a" * 64
    names = {_staging_dir_name(revision) for _ in range(32)}

    assert len(names) == 32
    for name in names:
        assert len(name) <= STAGING_NAME_MAX_LENGTH
        assert name.startswith(f".{'a' * 12}.")
        assert name.endswith(".tmp")


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


def test_generation_models_enforce_transitions_and_trimmed_directive_limit() -> None:
    session = FullDeckGenerationSession(
        session_id="fullsession_" + "1" * 32,
        full_deck_id="deck_" + "2" * 24,
        branch="main",
        base_checkpoint_id="checkpoint_" + "3" * 24,
        base_revision_hash="sha256:" + "4" * 64,
        outline_revision_hash="sha256:" + "5" * 64,
        sample_revision_hash="sha256:" + "6" * 64,
        planner_version="balanced-3-4-v1",
        total_batches=1,
    )
    batch = FullDeckGenerationBatch(
        session_id=session.session_id,
        batch_index=1,
        slot_ids=["slot_" + "7" * 24],
        source_slide_numbers=[3],
    )

    assert session.can_transition_to("running")
    assert not session.can_transition_to("completed")
    assert batch.can_transition_to("running")
    assert not batch.can_transition_to("succeeded")
    directive = FullDeckGenerationDirective(
        session_id=session.session_id,
        content="  " + "x" * 4000 + "  ",
        apply_from_batch_index=1,
    )
    assert len(directive.content) == 4000
    with pytest.raises(ValidationError):
        FullDeckGenerationDirective(
            session_id=session.session_id,
            content="x" * 4001,
            apply_from_batch_index=1,
        )
    with pytest.raises(ValidationError, match="published revision"):
        FullDeckGenerationSession.model_validate({
            **session.model_dump(),
            "status": "completed",
            "completed_batches": 1,
        })
