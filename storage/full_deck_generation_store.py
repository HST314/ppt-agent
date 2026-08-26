from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationDirective,
    FullDeckGenerationPackage,
    FullDeckGenerationPage,
    FullDeckGenerationSession,
    utc_now,
)
from storage.errors import ConflictError
from storage.full_deck_generation_audit import FullDeckGenerationAuditMixin
from storage.full_deck_generation_packages import FullDeckGenerationPackageStoreMixin
from storage.persistence import json_text
from storage.prompt_audit import finish_prompt_calls_in_transaction


_UNSET = object()


class FullDeckGenerationStoreMixin(
    FullDeckGenerationAuditMixin,
    FullDeckGenerationPackageStoreMixin,
):
    @staticmethod
    def _generation_session_value(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "full_deck_id": row["full_deck_id"],
            "branch": row["branch"],
            "base_checkpoint_id": row["base_checkpoint_id"],
            "base_revision_hash": row["base_revision_hash"],
            "outline_revision_hash": row["outline_revision_hash"],
            "sample_revision_hash": row["sample_revision_hash"],
            "status": row["status"],
            "planner_version": row["planner_version"],
            "total_batches": row["total_batches"],
            "completed_batches": row["completed_batches"],
            "active_batch_index": row["active_batch_index"],
            "session_version": row["session_version"],
            "latest_preview_package_id": row["latest_preview_package_id"],
            "published_revision_hash": row["published_revision_hash"],
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _generation_batch_value(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "batch_index": row["batch_index"],
            "status": row["status"],
            "slot_ids": json.loads(row["slot_ids_json"]),
            "source_slide_numbers": json.loads(row["source_slide_numbers_json"]),
            "attempt_count": row["attempt_count"],
            "segment_package_id": row["segment_package_id"],
            "prompt_call_ids": json.loads(row["prompt_call_ids_json"]),
            "applied_directive_ids": json.loads(row["applied_directive_ids_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _generation_page_value(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "position": row["position"],
            "slot_id": row["slot_id"],
            "source_slide_number": row["source_slide_number"],
            "title": row["title"],
            "generation_status": row["generation_status"],
            "batch_index": row["batch_index"],
            "source_type": row["source_type"],
            "content_ref": (
                json.loads(row["content_ref_json"])
                if row["content_ref_json"]
                else None
            ),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    @staticmethod
    def _generation_directive_value(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "directive_id": row["directive_id"],
            "session_id": row["session_id"],
            "content": row["content"],
            "apply_from_batch_index": row["apply_from_batch_index"],
            "created_at": row["created_at"],
            "first_applied_at": row["first_applied_at"],
        }

    def _generation_session_snapshot(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM full_deck_generation_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(session_id)
        value = FullDeckGenerationSession.model_validate(
            self._generation_session_value(row)
        ).model_dump(mode="json")
        batch_rows = connection.execute(
            """
            SELECT * FROM full_deck_generation_batches
            WHERE session_id = ? ORDER BY batch_index
            """,
            (session_id,),
        ).fetchall()
        page_rows = connection.execute(
            """
            SELECT * FROM full_deck_generation_pages
            WHERE session_id = ? ORDER BY position
            """,
            (session_id,),
        ).fetchall()
        directive_rows = connection.execute(
            """
            SELECT * FROM full_deck_generation_directives
            WHERE session_id = ? ORDER BY created_at, directive_id
            """,
            (session_id,),
        ).fetchall()
        value["batches"] = [
            FullDeckGenerationBatch.model_validate(
                self._generation_batch_value(item)
            ).model_dump(mode="json")
            for item in batch_rows
        ]
        value["pages"] = [
            FullDeckGenerationPage.model_validate(
                self._generation_page_value(item)
            ).model_dump(mode="json")
            for item in page_rows
        ]
        value["directives"] = [
            FullDeckGenerationDirective.model_validate(
                self._generation_directive_value(item)
            ).model_dump(mode="json")
            for item in directive_rows
        ]
        return value

    def create_full_deck_generation_session(
        self,
        session: FullDeckGenerationSession | dict[str, Any],
        batches: list[FullDeckGenerationBatch | dict[str, Any]],
        pages: list[FullDeckGenerationPage | dict[str, Any]],
    ) -> dict[str, Any]:
        """Create one immutable generation plan and its page projection atomically."""

        session_value = FullDeckGenerationSession.model_validate(session)
        batch_values = [FullDeckGenerationBatch.model_validate(item) for item in batches]
        page_values = [FullDeckGenerationPage.model_validate(item) for item in pages]
        if (
            session_value.status != "queued"
            or session_value.completed_batches != 0
            or session_value.active_batch_index is not None
            or session_value.session_version != 1
            or session_value.latest_preview_package_id is not None
            or session_value.published_revision_hash is not None
            or session_value.error is not None
        ):
            raise ValueError("new generation sessions must start from a clean queued state")
        if any(item.status != "pending" for item in batch_values):
            raise ValueError("new generation batches must be pending")
        if any(item.session_id != session_value.session_id for item in batch_values):
            raise ValueError("generation batches belong to another session")
        if any(item.session_id != session_value.session_id for item in page_values):
            raise ValueError("generation pages belong to another session")
        if [item.batch_index for item in batch_values] != list(
            range(1, session_value.total_batches + 1)
        ):
            raise ValueError("generation batch indexes must be ordered and contiguous")
        if [item.position for item in page_values] != list(range(len(page_values))):
            raise ValueError("generation page positions must be ordered and contiguous")
        if len({item.slot_id for item in page_values}) != len(page_values):
            raise ValueError("generation page slot ids must be unique")
        page_by_slot = {item.slot_id: item for item in page_values}
        targeted_slot_ids: set[str] = set()
        for batch in batch_values:
            for slot_id, slide_number in zip(
                batch.slot_ids, batch.source_slide_numbers, strict=True
            ):
                page = page_by_slot.get(slot_id)
                if (
                    page is None
                    or page.batch_index != batch.batch_index
                    or page.source_slide_number != slide_number
                ):
                    raise ValueError("generation batch targets do not match page projection")
                targeted_slot_ids.add(slot_id)
        projected_slot_ids = {
            item.slot_id for item in page_values if item.batch_index is not None
        }
        if targeted_slot_ids != projected_slot_ids:
            raise ValueError("generation page projection contains an unplanned target")
        self._ensure_database()
        try:
            with self._transaction() as connection:
                self._raw_current(connection)
                connection.execute(
                    """
                    INSERT INTO full_deck_generation_sessions(
                        session_id, full_deck_id, branch, base_checkpoint_id,
                        base_revision_hash, outline_revision_hash, sample_revision_hash,
                        status, planner_version, total_batches, completed_batches,
                        active_batch_index, session_version, latest_preview_package_id,
                        published_revision_hash, error_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_value.session_id,
                        session_value.full_deck_id,
                        session_value.branch,
                        session_value.base_checkpoint_id,
                        session_value.base_revision_hash,
                        session_value.outline_revision_hash,
                        session_value.sample_revision_hash,
                        session_value.status,
                        session_value.planner_version,
                        session_value.total_batches,
                        session_value.completed_batches,
                        session_value.active_batch_index,
                        session_value.session_version,
                        session_value.latest_preview_package_id,
                        session_value.published_revision_hash,
                        json_text(session_value.error)
                        if session_value.error is not None
                        else None,
                        session_value.created_at,
                        session_value.updated_at,
                    ),
                )
                for batch in batch_values:
                    connection.execute(
                        """
                        INSERT INTO full_deck_generation_batches(
                            session_id, batch_index, status, slot_ids_json,
                            source_slide_numbers_json, attempt_count, segment_package_id,
                            prompt_call_ids_json, applied_directive_ids_json, error_json,
                            started_at, completed_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch.session_id,
                            batch.batch_index,
                            batch.status,
                            json_text(batch.slot_ids),
                            json_text(batch.source_slide_numbers),
                            batch.attempt_count,
                            batch.segment_package_id,
                            json_text(batch.prompt_call_ids),
                            json_text(batch.applied_directive_ids),
                            json_text(batch.error) if batch.error is not None else None,
                            batch.started_at,
                            batch.completed_at,
                        ),
                    )
                for page in page_values:
                    connection.execute(
                        """
                        INSERT INTO full_deck_generation_pages(
                            session_id, position, slot_id, source_slide_number,
                            title, generation_status, batch_index, source_type,
                            content_ref_json, error_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            page.session_id,
                            page.position,
                            page.slot_id,
                            page.source_slide_number,
                            page.title,
                            page.generation_status,
                            page.batch_index,
                            page.source_type,
                            json_text(page.content_ref.model_dump(mode="json"))
                            if page.content_ref is not None
                            else None,
                            json_text(page.error) if page.error is not None else None,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("full_deck_generation_session_conflict") from exc
        return self.full_deck_generation_session(session_value.session_id)

    def full_deck_generation_session(self, session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"fullsession_[a-f0-9]{32}", session_id):
            raise ValueError("invalid generation session id")
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            return self._generation_session_snapshot(connection, session_id)

    def list_full_deck_generation_sessions(
        self,
        full_deck_id: str,
        *,
        branch: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not re.fullmatch(r"deck_[a-f0-9]{24}", full_deck_id):
            raise ValueError("invalid full-deck id")
        if not 1 <= limit <= 1000:
            raise ValueError("generation session list limit must be between 1 and 1000")
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            if branch is None:
                rows = connection.execute(
                    """
                    SELECT * FROM full_deck_generation_sessions
                    WHERE full_deck_id = ? ORDER BY created_at DESC, session_id DESC
                    LIMIT ?
                    """,
                    (full_deck_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM full_deck_generation_sessions
                    WHERE full_deck_id = ? AND branch = ?
                    ORDER BY created_at DESC, session_id DESC LIMIT ?
                    """,
                    (full_deck_id, branch, limit),
                ).fetchall()
        return [
            FullDeckGenerationSession.model_validate(
                self._generation_session_value(row)
            ).model_dump(mode="json")
            for row in rows
        ]

    def active_full_deck_generation_session(
        self,
        full_deck_id: str,
        branch: str,
    ) -> dict[str, Any] | None:
        if not re.fullmatch(r"deck_[a-f0-9]{24}", full_deck_id):
            raise ValueError("invalid full-deck id")
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            row = connection.execute(
                """
                SELECT session_id FROM full_deck_generation_sessions
                WHERE full_deck_id = ? AND branch = ?
                  AND status IN (
                      'queued', 'running', 'pause_requested', 'paused',
                      'failed', 'finalizing'
                  )
                """,
                (full_deck_id, branch),
            ).fetchone()
            if row is None:
                return None
            return self._generation_session_snapshot(connection, row["session_id"])

    @staticmethod
    def _require_generation_session_version(
        row: sqlite3.Row,
        expected_session_version: int | None,
    ) -> None:
        if (
            expected_session_version is not None
            and row["session_version"] != expected_session_version
        ):
            raise ConflictError("full_deck_generation_session_version_conflict")

    def update_full_deck_generation_session(
        self,
        session_id: str,
        expected_session_version: int,
        *,
        status: Any = _UNSET,
        completed_batches: Any = _UNSET,
        active_batch_index: Any = _UNSET,
        latest_preview_package_id: Any = _UNSET,
        published_revision_hash: Any = _UNSET,
        error: Any = _UNSET,
    ) -> dict[str, Any]:
        """Apply one session-level compare-and-swap and increment its version."""

        if not re.fullmatch(r"fullsession_[a-f0-9]{32}", session_id):
            raise ValueError("invalid generation session id")
        self._ensure_database()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM full_deck_generation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(session_id)
            self._require_generation_session_version(row, expected_session_version)
            value = self._generation_session_value(row)
            current = FullDeckGenerationSession.model_validate(value)
            updates = {
                "status": status,
                "completed_batches": completed_batches,
                "active_batch_index": active_batch_index,
                "latest_preview_package_id": latest_preview_package_id,
                "published_revision_hash": published_revision_hash,
                "error": error,
            }
            for key, update in updates.items():
                if update is not _UNSET:
                    value[key] = update
            if not current.can_transition_to(value["status"]):
                raise ConflictError("full_deck_generation_session_transition_invalid")
            succeeded_count = connection.execute(
                """
                SELECT count(*) AS count FROM full_deck_generation_batches
                WHERE session_id = ? AND status = 'succeeded'
                """,
                (session_id,),
            ).fetchone()["count"]
            if value["completed_batches"] != succeeded_count:
                raise ValueError("completed batch count must match durable batch state")
            running_count = connection.execute(
                """
                SELECT count(*) AS count FROM full_deck_generation_batches
                WHERE session_id = ? AND status = 'running'
                """,
                (session_id,),
            ).fetchone()["count"]
            if value["status"] in {
                "paused",
                "failed",
                "finalizing",
                "completed",
                "cancelled",
                "stale",
            } and running_count:
                raise ConflictError("full_deck_generation_batch_still_running")
            if (
                value["status"] in {"finalizing", "completed"}
                and succeeded_count != row["total_batches"]
            ):
                raise ConflictError("full_deck_generation_batches_incomplete")
            if value["active_batch_index"] is not None:
                active = connection.execute(
                    """
                    SELECT status FROM full_deck_generation_batches
                    WHERE session_id = ? AND batch_index = ?
                    """,
                    (session_id, value["active_batch_index"]),
                ).fetchone()
                if active is None or active["status"] != "running":
                    raise ValueError("active batch must reference a running batch")
            if value["latest_preview_package_id"] is not None:
                preview = connection.execute(
                    """
                    SELECT kind FROM full_deck_generation_packages
                    WHERE package_id = ? AND session_id = ?
                    """,
                    (value["latest_preview_package_id"], session_id),
                ).fetchone()
                if preview is None or preview["kind"] != "preview":
                    raise ValueError("latest preview must reference a session preview package")
            if value["published_revision_hash"] is not None:
                revision = connection.execute(
                    """
                    SELECT 1 FROM full_deck_revisions
                    WHERE revision_hash = ? AND full_deck_id = ?
                    """,
                    (value["published_revision_hash"], row["full_deck_id"]),
                ).fetchone()
                if revision is None:
                    raise ValueError("published revision is not registered on this deck")
            value["session_version"] = row["session_version"] + 1
            value["updated_at"] = utc_now()
            validated = FullDeckGenerationSession.model_validate(value)
            cursor = connection.execute(
                """
                UPDATE full_deck_generation_sessions SET
                    status = ?, completed_batches = ?, active_batch_index = ?,
                    session_version = ?, latest_preview_package_id = ?,
                    published_revision_hash = ?, error_json = ?, updated_at = ?
                WHERE session_id = ? AND session_version = ?
                """,
                (
                    validated.status,
                    validated.completed_batches,
                    validated.active_batch_index,
                    validated.session_version,
                    validated.latest_preview_package_id,
                    validated.published_revision_hash,
                    json_text(validated.error)
                    if validated.error is not None
                    else None,
                    validated.updated_at,
                    session_id,
                    expected_session_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("full_deck_generation_session_version_conflict")
        return self.full_deck_generation_session(session_id)

    def claim_full_deck_generation_batch(
        self,
        session_id: str,
        *,
        expected_session_version: int | None = None,
        retry_failed: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically claim the next batch while enforcing one active batch per session."""

        if not re.fullmatch(r"fullsession_[a-f0-9]{32}", session_id):
            raise ValueError("invalid generation session id")
        self._ensure_database()
        with self._transaction() as connection:
            session_row = connection.execute(
                "SELECT * FROM full_deck_generation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise FileNotFoundError(session_id)
            self._require_generation_session_version(
                session_row, expected_session_version
            )
            allowed_statuses = {"failed"} if retry_failed else {"queued", "running"}
            if session_row["status"] not in allowed_statuses:
                raise ConflictError("full_deck_generation_batch_claim_not_allowed")
            already_running = connection.execute(
                """
                SELECT 1 FROM full_deck_generation_batches
                WHERE session_id = ? AND status = 'running'
                """,
                (session_id,),
            ).fetchone()
            if already_running is not None:
                return None
            target_status = "failed" if retry_failed else "pending"
            batch_row = connection.execute(
                """
                SELECT * FROM full_deck_generation_batches
                WHERE session_id = ? AND status = ?
                ORDER BY batch_index LIMIT 1
                """,
                (session_id, target_status),
            ).fetchone()
            if batch_row is None:
                return None
            now = utc_now()
            batch_index = batch_row["batch_index"]
            cursor = connection.execute(
                """
                UPDATE full_deck_generation_batches SET
                    status = 'running', attempt_count = attempt_count + 1,
                    error_json = NULL, started_at = ?, completed_at = NULL
                WHERE session_id = ? AND batch_index = ? AND status = ?
                """,
                (now, session_id, batch_index, target_status),
            )
            if cursor.rowcount != 1:
                raise ConflictError("full_deck_generation_batch_claim_conflict")
            page_cursor = connection.execute(
                """
                UPDATE full_deck_generation_pages SET
                    generation_status = 'generating', error_json = NULL
                WHERE session_id = ? AND batch_index = ?
                  AND generation_status IN ('queued', 'failed')
                """,
                (session_id, batch_index),
            )
            if page_cursor.rowcount != len(json.loads(batch_row["slot_ids_json"])):
                raise ConflictError("full_deck_generation_page_projection_conflict")
            new_version = session_row["session_version"] + 1
            session_cursor = connection.execute(
                """
                UPDATE full_deck_generation_sessions SET
                    status = 'running', active_batch_index = ?, session_version = ?,
                    error_json = NULL, updated_at = ?
                WHERE session_id = ? AND session_version = ?
                """,
                (
                    batch_index,
                    new_version,
                    now,
                    session_id,
                    session_row["session_version"],
                ),
            )
            if session_cursor.rowcount != 1:
                raise ConflictError("full_deck_generation_session_version_conflict")
            claimed = connection.execute(
                """
                SELECT * FROM full_deck_generation_batches
                WHERE session_id = ? AND batch_index = ?
                """,
                (session_id, batch_index),
            ).fetchone()
            return {
                "batch": FullDeckGenerationBatch.model_validate(
                    self._generation_batch_value(claimed)
                ).model_dump(mode="json"),
                "session_version": new_version,
            }

    def add_full_deck_generation_directive(
        self,
        directive: FullDeckGenerationDirective | dict[str, Any],
        *,
        expected_session_version: int,
    ) -> dict[str, Any]:
        directive_value = FullDeckGenerationDirective.model_validate(directive)
        self._ensure_database()
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM full_deck_generation_sessions WHERE session_id = ?",
                    (directive_value.session_id,),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(directive_value.session_id)
                self._require_generation_session_version(
                    row, expected_session_version
                )
                if directive_value.apply_from_batch_index > row["total_batches"]:
                    raise ValueError("directive starts after the final batch")
                if row["status"] in {"finalizing", "completed", "cancelled", "stale"}:
                    raise ConflictError("full_deck_generation_directive_not_allowed")
                connection.execute(
                    """
                    INSERT INTO full_deck_generation_directives(
                        directive_id, session_id, content, apply_from_batch_index,
                        created_at, first_applied_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        directive_value.directive_id,
                        directive_value.session_id,
                        directive_value.content,
                        directive_value.apply_from_batch_index,
                        directive_value.created_at,
                        directive_value.first_applied_at,
                    ),
                )
                new_version = row["session_version"] + 1
                connection.execute(
                    """
                    UPDATE full_deck_generation_sessions SET
                        session_version = ?, updated_at = ?
                    WHERE session_id = ? AND session_version = ?
                    """,
                    (
                        new_version,
                        utc_now(),
                        directive_value.session_id,
                        row["session_version"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("full_deck_generation_directive_conflict") from exc
        result = directive_value.model_dump(mode="json")
        result["session_version"] = expected_session_version + 1
        return result

    def fail_full_deck_generation_batch(
        self,
        session_id: str,
        batch_index: int,
        *,
        expected_session_version: int,
        error: dict[str, Any],
        prompt_call_ids: list[str] | None = None,
        applied_directive_ids: list[str] | None = None,
        segment_package: FullDeckGenerationPackage | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not error:
            raise ValueError("generation batch failure requires an error")
        self._ensure_database()
        now = utc_now()
        segment = (
            FullDeckGenerationPackage.model_validate(segment_package)
            if segment_package is not None
            else None
        )
        if segment is not None and (
            segment.session_id != session_id
            or segment.batch_index != batch_index
            or segment.kind != "segment"
        ):
            raise ValueError("generation segment identity does not match the batch")
        segment_artifacts: list[dict[str, Any]] = []
        segment_files: list[dict[str, Any]] = []
        if segment is not None:
            segment_artifacts, segment_files = (
                self._prepare_full_deck_generation_package(segment)
            )
        try:
            with self._transaction() as connection:
                session_row = connection.execute(
                    "SELECT * FROM full_deck_generation_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session_row is None:
                    raise FileNotFoundError(session_id)
                self._require_generation_session_version(
                    session_row, expected_session_version
                )
                if session_row["status"] not in {"running", "pause_requested"}:
                    raise ConflictError("full_deck_generation_batch_failure_not_allowed")
                batch_row = connection.execute(
                    """
                    SELECT * FROM full_deck_generation_batches
                    WHERE session_id = ? AND batch_index = ?
                    """,
                    (session_id, batch_index),
                ).fetchone()
                if batch_row is None or batch_row["status"] != "running":
                    raise ConflictError("full_deck_generation_batch_not_running")
                batch_value = self._generation_batch_value(batch_row)
                stored_prompt_call_ids = (
                    list(prompt_call_ids)
                    if prompt_call_ids is not None
                    else batch_value["prompt_call_ids"]
                )
                stored_directive_ids = (
                    list(applied_directive_ids)
                    if applied_directive_ids is not None
                    else batch_value["applied_directive_ids"]
                )
                if segment is not None:
                    if [
                        item.source_slide_number for item in segment.slides
                    ] != batch_value["source_slide_numbers"]:
                        raise ValueError("segment slides do not match the claimed batch")
                    batch_value["segment_package_id"] = segment.package_id
                batch_value.update(
                    status="failed",
                    prompt_call_ids=stored_prompt_call_ids,
                    applied_directive_ids=stored_directive_ids,
                    error=error,
                    completed_at=now,
                )
                validated_batch = FullDeckGenerationBatch.model_validate(batch_value)
                if stored_directive_ids:
                    directive_rows = connection.execute(
                        """
                        SELECT directive_id, apply_from_batch_index
                        FROM full_deck_generation_directives
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall()
                    effective = {
                        row["directive_id"]: row
                        for row in directive_rows
                        if row["directive_id"] in set(stored_directive_ids)
                    }
                    if set(effective) != set(stored_directive_ids) or any(
                        row["apply_from_batch_index"] > batch_index
                        for row in effective.values()
                    ):
                        raise ValueError("batch references an ineffective directive")
                if segment is not None:
                    self._insert_artifacts(connection, segment_artifacts)
                    self._insert_full_deck_generation_package(
                        connection, segment, segment_files
                    )
                cursor = connection.execute(
                    """
                    UPDATE full_deck_generation_batches SET
                        status = 'failed', segment_package_id = ?,
                        prompt_call_ids_json = ?, applied_directive_ids_json = ?,
                        error_json = ?, completed_at = ?
                    WHERE session_id = ? AND batch_index = ? AND status = 'running'
                    """,
                    (
                        validated_batch.segment_package_id,
                        json_text(validated_batch.prompt_call_ids),
                        json_text(validated_batch.applied_directive_ids),
                        json_text(error),
                        now,
                        session_id,
                        batch_index,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("full_deck_generation_batch_not_running")
                if stored_directive_ids:
                    connection.executemany(
                        """
                        UPDATE full_deck_generation_directives
                        SET first_applied_at = COALESCE(first_applied_at, ?)
                        WHERE session_id = ? AND directive_id = ?
                        """,
                        [(now, session_id, item) for item in stored_directive_ids],
                    )
                page_cursor = connection.execute(
                    """
                    UPDATE full_deck_generation_pages SET
                        generation_status = 'failed', error_json = ?
                    WHERE session_id = ? AND batch_index = ?
                      AND generation_status = 'generating'
                    """,
                    (json_text(error), session_id, batch_index),
                )
                if page_cursor.rowcount != len(validated_batch.slot_ids):
                    raise ConflictError("full_deck_generation_page_projection_conflict")
                session_cursor = connection.execute(
                    """
                    UPDATE full_deck_generation_sessions SET
                        status = 'failed', active_batch_index = NULL,
                        session_version = session_version + 1,
                        error_json = ?, updated_at = ?
                    WHERE session_id = ? AND session_version = ?
                    """,
                    (
                        json_text(error),
                        now,
                        session_id,
                        expected_session_version,
                    ),
                )
                if session_cursor.rowcount != 1:
                    raise ConflictError("full_deck_generation_session_version_conflict")
        except sqlite3.IntegrityError as exc:
            raise ConflictError("full_deck_generation_package_conflict") from exc
        return self.full_deck_generation_session(session_id)

    def recover_full_deck_generation_sessions(self) -> list[dict[str, Any]]:
        """Recover interrupted batches and terminalize their durable PromptCalls."""

        self._ensure_database()
        error = {
            "code": "full_deck_generation_worker_interrupted",
            "message": "Generation stopped before the active batch was committed.",
        }
        recovered_ids: list[str] = []
        now = utc_now()
        with self._transaction() as connection:
            batch_rows = connection.execute(
                """
                SELECT b.session_id, b.batch_index, b.status,
                       b.segment_package_id, b.prompt_call_ids_json
                FROM full_deck_generation_batches b
                """
            ).fetchall()
            batches_by_identity = {
                (row["session_id"], row["batch_index"]): row
                for row in batch_rows
            }
            started_prompt_rows = connection.execute(
                """
                SELECT prompt_call_id, messages_json, parameters_json,
                       tool_calls_json
                FROM prompt_calls
                WHERE state = 'ppt_full' AND status = 'started'
                ORDER BY started_at, prompt_call_id
                """
            ).fetchall()
            prompt_call_updates: list[dict[str, Any]] = []
            for prompt_row in started_prompt_rows:
                parameters = json.loads(prompt_row["parameters_json"])
                if parameters.get("operation") != "generate_full_deck_batch":
                    continue
                session_id = parameters.get("generation_session_id")
                batch_index = parameters.get("batch_index")
                if not isinstance(session_id, str) or not isinstance(batch_index, int):
                    continue
                batch_row = batches_by_identity.get((session_id, batch_index))
                if batch_row is None:
                    continue
                prompt_call_id = prompt_row["prompt_call_id"]
                recorded_prompt_call_ids = json.loads(
                    batch_row["prompt_call_ids_json"]
                )
                committed = (
                    batch_row["status"] == "succeeded"
                    and prompt_call_id in recorded_prompt_call_ids
                    and batch_row["segment_package_id"] is not None
                )
                prompt_call_updates.append({
                    "prompt_call_id": prompt_call_id,
                    "status": "completed" if committed else "failed",
                    "traces": json.loads(prompt_row["tool_calls_json"]),
                    "messages": json.loads(prompt_row["messages_json"]),
                    "output_ref": (
                        batch_row["segment_package_id"] if committed else None
                    ),
                    # The durable segment proves publication, but its package hash
                    # is not the hash of the raw model response recorded here.
                    "output_hash": None,
                    "error": None if committed else error,
                })
            rows = connection.execute(
                """
                SELECT DISTINCT s.session_id
                FROM full_deck_generation_sessions s
                JOIN full_deck_generation_batches b ON b.session_id = s.session_id
                WHERE b.status = 'running'
                  AND s.status IN ('running', 'pause_requested')
                ORDER BY s.created_at, s.session_id
                """
            ).fetchall()
            for row in rows:
                session_id = row["session_id"]
                running_batches = connection.execute(
                    """
                    SELECT batch_index FROM full_deck_generation_batches
                    WHERE session_id = ? AND status = 'running'
                    """,
                    (session_id,),
                ).fetchall()
                batch_indexes = [item["batch_index"] for item in running_batches]
                connection.execute(
                    """
                    UPDATE full_deck_generation_batches SET
                        status = 'failed', error_json = ?, completed_at = ?
                    WHERE session_id = ? AND status = 'running'
                    """,
                    (json_text(error), now, session_id),
                )
                for batch_index in batch_indexes:
                    connection.execute(
                        """
                        UPDATE full_deck_generation_pages SET
                            generation_status = 'failed', error_json = ?
                        WHERE session_id = ? AND batch_index = ?
                          AND generation_status = 'generating'
                        """,
                        (json_text(error), session_id, batch_index),
                    )
                connection.execute(
                    """
                    UPDATE full_deck_generation_sessions SET
                        status = 'failed', active_batch_index = NULL,
                        session_version = session_version + 1,
                        error_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_text(error), now, session_id),
                )
                recovered_ids.append(session_id)
            finish_prompt_calls_in_transaction(
                connection,
                prompt_call_updates,
                completed_at=now,
            )
        return [self.full_deck_generation_session(item) for item in recovered_ids]
