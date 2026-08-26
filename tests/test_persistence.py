from multiprocessing import get_context
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationDirective,
    FullDeckGenerationSession,
)
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
