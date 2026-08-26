from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from agent_core.models import (
    FullDeckGenerationBatch,
    FullDeckGenerationDirective,
    FullDeckGenerationPage,
    FullDeckGenerationSession,
)


class FullDeckGenerationAuditMixin:
    """Bounded evidence projection for generation sessions and immutable packages."""

    def full_deck_generation_audit(
        self,
        full_deck_id: str,
        *,
        session_limit: int = 100,
        row_limit: int = 5_000,
        package_limit: int = 1_000,
        file_limit: int = 5_000,
    ) -> dict[str, Any]:
        """Export session evidence without loading package file contents."""

        if not re.fullmatch(r"deck_[a-f0-9]{24}", full_deck_id):
            raise ValueError("invalid full-deck id")
        for value, upper, label in (
            (session_limit, 500, "session"),
            (row_limit, 20_000, "row"),
            (package_limit, 5_000, "package"),
            (file_limit, 20_000, "file"),
        ):
            if not 1 <= value <= upper:
                raise ValueError(f"generation audit {label} limit is invalid")

        self._ensure_database()
        with self._connect() as connection:
            self._raw_current(connection)
            session_rows = connection.execute(
                """
                SELECT * FROM full_deck_generation_sessions
                WHERE full_deck_id = ?
                ORDER BY created_at DESC, session_id DESC LIMIT ?
                """,
                (full_deck_id, session_limit + 1),
            ).fetchall()
            session_truncated = len(session_rows) > session_limit
            session_rows = session_rows[:session_limit]
            session_ids = [row["session_id"] for row in session_rows]
            if not session_ids:
                return self._empty_full_deck_generation_audit()
            placeholders = ",".join("?" for _ in session_ids)

            def limited_rows(table: str, order_by: str) -> list[sqlite3.Row]:
                return connection.execute(
                    f"SELECT * FROM {table} WHERE session_id IN ({placeholders}) "
                    f"ORDER BY {order_by} LIMIT ?",
                    (*session_ids, row_limit + 1),
                ).fetchall()

            batch_rows = limited_rows(
                "full_deck_generation_batches",
                "session_id, batch_index",
            )
            page_rows = limited_rows(
                "full_deck_generation_pages",
                "session_id, position",
            )
            directive_rows = limited_rows(
                "full_deck_generation_directives",
                "session_id, created_at, directive_id",
            )
            package_rows = connection.execute(
                f"""
                SELECT * FROM full_deck_generation_packages
                WHERE session_id IN ({placeholders})
                ORDER BY session_id, batch_index, kind, created_at, package_id
                LIMIT ?
                """,
                (*session_ids, package_limit + 1),
            ).fetchall()
            package_truncated = len(package_rows) > package_limit
            package_rows = package_rows[:package_limit]
            file_rows = self._generation_audit_file_rows(
                connection,
                session_ids,
                package_limit=package_limit,
                file_limit=file_limit,
            )

        truncated = {
            "sessions": session_truncated,
            "batches": len(batch_rows) > row_limit,
            "pages": len(page_rows) > row_limit,
            "directives": len(directive_rows) > row_limit,
            "packages": package_truncated,
            "package_files": len(file_rows) > file_limit,
        }
        return {
            "sessions": self._generation_audit_sessions(
                session_rows,
                batch_rows[:row_limit],
                page_rows[:row_limit],
                directive_rows[:row_limit],
                package_rows,
                file_rows[:file_limit],
            ),
            "truncated": truncated,
        }

    @staticmethod
    def _empty_full_deck_generation_audit() -> dict[str, Any]:
        return {
            "sessions": [],
            "truncated": {
                name: False
                for name in (
                    "sessions",
                    "batches",
                    "pages",
                    "directives",
                    "packages",
                    "package_files",
                )
            },
        }

    @staticmethod
    def _generation_audit_file_rows(
        connection: sqlite3.Connection,
        session_ids: list[str],
        *,
        package_limit: int,
        file_limit: int,
    ) -> list[sqlite3.Row]:
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        return connection.execute(
            f"""
            SELECT f.package_id, f.file_index, f.logical_path,
                   f.artifact_id, a.sha256, f.media_type,
                   f.size_bytes, f.origin
            FROM full_deck_generation_package_files f
            JOIN artifacts a ON a.artifact_id = f.artifact_id
            WHERE f.package_id IN (
                SELECT package_id FROM full_deck_generation_packages
                WHERE session_id IN ({placeholders})
                ORDER BY session_id, batch_index, kind, created_at, package_id
                LIMIT ?
            )
            ORDER BY f.package_id, f.file_index LIMIT ?
            """,
            (*session_ids, package_limit, file_limit + 1),
        ).fetchall()

    def _generation_audit_sessions(
        self,
        session_rows: list[sqlite3.Row],
        batch_rows: list[sqlite3.Row],
        page_rows: list[sqlite3.Row],
        directive_rows: list[sqlite3.Row],
        package_rows: list[sqlite3.Row],
        file_rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        result_by_session: dict[str, dict[str, Any]] = {}
        for row in session_rows:
            value = FullDeckGenerationSession.model_validate(
                self._generation_session_value(row)
            ).model_dump(mode="json")
            value.update(batches=[], pages=[], directives=[], packages=[])
            result_by_session[value["session_id"]] = value
        for row in batch_rows:
            result_by_session[row["session_id"]]["batches"].append(
                FullDeckGenerationBatch.model_validate(
                    self._generation_batch_value(row)
                ).model_dump(mode="json")
            )
        for row in page_rows:
            result_by_session[row["session_id"]]["pages"].append(
                FullDeckGenerationPage.model_validate(
                    self._generation_page_value(row)
                ).model_dump(mode="json")
            )
        for row in directive_rows:
            result_by_session[row["session_id"]]["directives"].append(
                FullDeckGenerationDirective.model_validate(
                    self._generation_directive_value(row)
                ).model_dump(mode="json")
            )
        files_by_package: dict[str, list[dict[str, Any]]] = {}
        for row in file_rows:
            files_by_package.setdefault(row["package_id"], []).append({
                key: row[key]
                for key in (
                    "logical_path",
                    "artifact_id",
                    "sha256",
                    "media_type",
                    "size_bytes",
                    "origin",
                )
            })
        for row in package_rows:
            result_by_session[row["session_id"]]["packages"].append({
                "package_id": row["package_id"],
                "batch_index": row["batch_index"],
                "kind": row["kind"],
                "package_hash": row["package_hash"],
                "entrypoint": row["entrypoint"],
                "title": row["title"],
                "slide_count": row["slide_count"],
                "slides": json.loads(row["slides_json"]),
                "composition_manifest": json.loads(
                    row["composition_manifest_json"]
                ),
                "created_at": row["created_at"],
                "files": files_by_package.get(row["package_id"], []),
            })
        return [result_by_session[row["session_id"]] for row in session_rows]
