from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationContentRef,
    FullDeckGenerationPackage,
    utc_now,
)
from runtime.package_tool import normalize_package_path
from storage.errors import ConflictError
from storage.persistence import json_text, path_is_file, read_bytes


class FullDeckGenerationPackageStoreMixin:
    def _prepare_full_deck_generation_package(
        self,
        package: FullDeckGenerationPackage,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        artifacts: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for index, item in enumerate(package.files):
            artifact = self._package_artifact_record(
                item.path, item.content_bytes(), item.media_type
            )
            artifacts.append(artifact)
            files.append({
                "file_index": index,
                "logical_path": item.path,
                "artifact_id": artifact["artifact_id"],
                "media_type": item.media_type,
                "size_bytes": artifact["size_bytes"],
                "origin": item.origin,
            })
        return artifacts, files

    @staticmethod
    def _insert_full_deck_generation_package(
        connection: sqlite3.Connection,
        package: FullDeckGenerationPackage,
        files: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            """
            INSERT INTO full_deck_generation_packages(
                package_id, session_id, batch_index, kind, package_hash,
                entrypoint, title, slide_count, slides_json,
                composition_manifest_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package.package_id,
                package.session_id,
                package.batch_index,
                package.kind,
                package.package_hash,
                package.entrypoint,
                package.title,
                package.slide_count,
                json_text([item.model_dump(mode="json") for item in package.slides]),
                json_text(package.composition_manifest),
                package.created_at,
            ),
        )
        for item in files:
            connection.execute(
                """
                INSERT INTO full_deck_generation_package_files(
                    package_id, file_index, logical_path, artifact_id,
                    media_type, size_bytes, origin
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package.package_id,
                    item["file_index"],
                    item["logical_path"],
                    item["artifact_id"],
                    item["media_type"],
                    item["size_bytes"],
                    item["origin"],
                ),
            )

    def commit_full_deck_generation_batch(
        self,
        session_id: str,
        batch_index: int,
        *,
        expected_session_version: int,
        segment_package: FullDeckGenerationPackage | dict[str, Any],
        preview_package: FullDeckGenerationPackage | dict[str, Any],
        page_content_refs: dict[str, FullDeckGenerationContentRef | dict[str, Any]],
        prompt_call_ids: list[str] | None = None,
        applied_directive_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Durably commit a successful segment, preview, and page projection."""

        segment = FullDeckGenerationPackage.model_validate(segment_package)
        preview = FullDeckGenerationPackage.model_validate(preview_package)
        if (
            segment.session_id != session_id
            or preview.session_id != session_id
            or segment.batch_index != batch_index
            or preview.batch_index != batch_index
            or segment.kind != "segment"
            or preview.kind != "preview"
        ):
            raise ValueError("generation package identity does not match the batch")
        if segment.package_id == preview.package_id:
            raise ValueError("segment and preview packages require distinct identities")
        content_refs = {
            slot_id: FullDeckGenerationContentRef.model_validate(reference)
            for slot_id, reference in page_content_refs.items()
        }
        if any(
            reference.package_id != segment.package_id
            or reference.package_hash != segment.package_hash
            for reference in content_refs.values()
        ):
            raise ValueError("page content reference does not match the segment package")
        segment_slide_ids = {item.slide_id for item in segment.slides}
        if {reference.slide_id for reference in content_refs.values()} != segment_slide_ids:
            raise ValueError("page content references must cover the segment slides")
        self._ensure_database()
        with self._connect() as connection:
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
                raise ConflictError("full_deck_generation_batch_commit_not_allowed")
            batch_row = connection.execute(
                """
                SELECT * FROM full_deck_generation_batches
                WHERE session_id = ? AND batch_index = ?
                """,
                (session_id, batch_index),
            ).fetchone()
            if batch_row is None:
                raise FileNotFoundError(f"{session_id}:{batch_index}")
            if batch_row["status"] != "running":
                raise ConflictError("full_deck_generation_batch_not_running")
            if set(json.loads(batch_row["slot_ids_json"])) != set(content_refs):
                raise ValueError("page content references do not cover the claimed batch")
            if [
                item.source_slide_number for item in segment.slides
            ] != json.loads(batch_row["source_slide_numbers_json"]):
                raise ValueError("segment slides do not match the claimed slide numbers")
        segment_artifacts, segment_files = self._prepare_full_deck_generation_package(
            segment
        )
        preview_artifacts, preview_files = self._prepare_full_deck_generation_package(
            preview
        )
        now = utc_now()
        prompt_ids = list(prompt_call_ids or [])
        directive_ids = list(applied_directive_ids or [])
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
                    raise ConflictError("full_deck_generation_batch_commit_not_allowed")
                batch_row = connection.execute(
                    """
                    SELECT * FROM full_deck_generation_batches
                    WHERE session_id = ? AND batch_index = ?
                    """,
                    (session_id, batch_index),
                ).fetchone()
                if batch_row is None or batch_row["status"] != "running":
                    raise ConflictError("full_deck_generation_batch_not_running")
                if set(json.loads(batch_row["slot_ids_json"])) != set(content_refs):
                    raise ValueError("page content references do not cover the claimed batch")
                batch_value = self._generation_batch_value(batch_row)
                batch_value.update(
                    status="succeeded",
                    segment_package_id=segment.package_id,
                    prompt_call_ids=prompt_ids,
                    applied_directive_ids=directive_ids,
                    error=None,
                    completed_at=now,
                )
                validated_batch = FullDeckGenerationBatch.model_validate(batch_value)
                if directive_ids:
                    directive_id_set = set(directive_ids)
                    rows = connection.execute(
                        """
                        SELECT directive_id, apply_from_batch_index
                        FROM full_deck_generation_directives
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall()
                    directive_rows = {
                        row["directive_id"]: row for row in rows
                        if row["directive_id"] in directive_id_set
                    }
                    if set(directive_rows) != directive_id_set:
                        raise ValueError("batch references an unknown generation directive")
                    if any(
                        row["apply_from_batch_index"] > batch_index
                        for row in directive_rows.values()
                    ):
                        raise ValueError("batch references a directive that is not effective yet")
                existing_segment = connection.execute(
                    """
                    SELECT session_id, batch_index, kind, package_hash
                    FROM full_deck_generation_packages WHERE package_id = ?
                    """,
                    (segment.package_id,),
                ).fetchone()
                if existing_segment is not None and (
                    existing_segment["session_id"] != session_id
                    or existing_segment["batch_index"] != batch_index
                    or existing_segment["kind"] != "segment"
                    or existing_segment["package_hash"] != segment.package_hash
                ):
                    raise ConflictError("full_deck_generation_package_conflict")
                self._insert_artifacts(
                    connection, segment_artifacts + preview_artifacts
                )
                if existing_segment is None:
                    self._insert_full_deck_generation_package(
                        connection, segment, segment_files
                    )
                self._insert_full_deck_generation_package(
                    connection, preview, preview_files
                )
                batch_cursor = connection.execute(
                    """
                    UPDATE full_deck_generation_batches SET
                        status = 'succeeded', segment_package_id = ?,
                        prompt_call_ids_json = ?, applied_directive_ids_json = ?,
                        error_json = NULL, completed_at = ?
                    WHERE session_id = ? AND batch_index = ? AND status = 'running'
                    """,
                    (
                        segment.package_id,
                        json_text(validated_batch.prompt_call_ids),
                        json_text(validated_batch.applied_directive_ids),
                        now,
                        session_id,
                        batch_index,
                    ),
                )
                if batch_cursor.rowcount != 1:
                    raise ConflictError("full_deck_generation_batch_not_running")
                for slot_id, reference in content_refs.items():
                    cursor = connection.execute(
                        """
                        UPDATE full_deck_generation_pages SET
                            generation_status = 'ready',
                            source_type = 'generated_segment',
                            content_ref_json = ?, error_json = NULL
                        WHERE session_id = ? AND slot_id = ? AND batch_index = ?
                          AND generation_status = 'generating'
                        """,
                        (
                            json_text(reference.model_dump(mode="json")),
                            session_id,
                            slot_id,
                            batch_index,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("full_deck_generation_page_not_generating")
                if directive_ids:
                    connection.executemany(
                        """
                        UPDATE full_deck_generation_directives
                        SET first_applied_at = COALESCE(first_applied_at, ?)
                        WHERE session_id = ? AND directive_id = ?
                        """,
                        [(now, session_id, item) for item in directive_ids],
                    )
                completed_batches = connection.execute(
                    """
                    SELECT count(*) AS count FROM full_deck_generation_batches
                    WHERE session_id = ? AND status = 'succeeded'
                    """,
                    (session_id,),
                ).fetchone()["count"]
                new_version = session_row["session_version"] + 1
                session_cursor = connection.execute(
                    """
                    UPDATE full_deck_generation_sessions SET
                        completed_batches = ?, active_batch_index = NULL,
                        session_version = ?, latest_preview_package_id = ?,
                        error_json = NULL, updated_at = ?
                    WHERE session_id = ? AND session_version = ?
                    """,
                    (
                        completed_batches,
                        new_version,
                        preview.package_id,
                        now,
                        session_id,
                        session_row["session_version"],
                    ),
                )
                if session_cursor.rowcount != 1:
                    raise ConflictError("full_deck_generation_session_version_conflict")
        except sqlite3.IntegrityError as exc:
            raise ConflictError("full_deck_generation_package_conflict") from exc
        return self.full_deck_generation_session(session_id)

    def full_deck_generation_package(self, package_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", package_id):
            raise ValueError("invalid generation package id")
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            row = connection.execute(
                "SELECT * FROM full_deck_generation_packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(package_id)
            files = connection.execute(
                """
                SELECT logical_path, artifact_id, media_type, size_bytes, origin
                FROM full_deck_generation_package_files
                WHERE package_id = ? ORDER BY file_index
                """,
                (package_id,),
            ).fetchall()
        return {
            "package_id": row["package_id"],
            "session_id": row["session_id"],
            "batch_index": row["batch_index"],
            "kind": row["kind"],
            "package_hash": row["package_hash"],
            "entrypoint": row["entrypoint"],
            "title": row["title"],
            "slide_count": row["slide_count"],
            "slides": json.loads(row["slides_json"]),
            "composition_manifest": json.loads(row["composition_manifest_json"]),
            "created_at": row["created_at"],
            "files": [dict(item) for item in files],
        }

    def full_deck_generation_package_file(
        self,
        package_id: str,
        logical_path: str,
    ) -> tuple[Path, str]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", package_id):
            raise ValueError("invalid generation package id")
        path = normalize_package_path(logical_path)
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            row = connection.execute(
                """
                SELECT a.relative_path, a.sha256, a.size_bytes, f.media_type
                FROM full_deck_generation_package_files f
                JOIN artifacts a ON a.artifact_id = f.artifact_id
                WHERE f.package_id = ? AND f.logical_path = ?
                """,
                (package_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
        artifact_path = (self.root / row["relative_path"]).resolve()
        if (
            not self._is_package_artifact_path(artifact_path)
            or not path_is_file(artifact_path)
        ):
            raise ConflictError("package_artifact_missing")
        content = read_bytes(artifact_path)
        if (
            "sha256:" + hashlib.sha256(content).hexdigest() != row["sha256"]
            or len(content) != row["size_bytes"]
        ):
            raise ConflictError("artifact_corrupt")
        return artifact_path, row["media_type"]

    def full_deck_generation_package_contents(
        self,
        package_id: str,
    ) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", package_id):
            raise ValueError("invalid generation package id")
        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            package = connection.execute(
                "SELECT 1 FROM full_deck_generation_packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
            if package is None:
                raise FileNotFoundError(package_id)
            rows = connection.execute(
                """
                SELECT f.logical_path, f.media_type, f.origin,
                       a.relative_path, a.sha256, a.size_bytes
                FROM full_deck_generation_package_files f
                JOIN artifacts a ON a.artifact_id = f.artifact_id
                WHERE f.package_id = ? ORDER BY f.file_index
                """,
                (package_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            artifact_path = (self.root / row["relative_path"]).resolve()
            if (
                not self._is_package_artifact_path(artifact_path)
                or not path_is_file(artifact_path)
            ):
                raise ConflictError("package_artifact_missing")
            content = read_bytes(artifact_path)
            if (
                "sha256:" + hashlib.sha256(content).hexdigest() != row["sha256"]
                or len(content) != row["size_bytes"]
            ):
                raise ConflictError("artifact_corrupt")
            result.append({
                "path": row["logical_path"],
                "media_type": row["media_type"],
                "origin": row["origin"],
                "content": content,
            })
        return result

    def full_deck_generation_preview_file(
        self,
        session_id: str,
        logical_path: str,
    ) -> tuple[Path, str]:
        session = self.full_deck_generation_session(session_id)
        package_id = session.get("latest_preview_package_id")
        if package_id is None:
            raise FileNotFoundError("generation preview is not available")
        package = self.full_deck_generation_package(package_id)
        if package["session_id"] != session_id or package["kind"] != "preview":
            raise ConflictError("generation_preview_reference_invalid")
        return self.full_deck_generation_package_file(package_id, logical_path)
