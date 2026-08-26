from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from agent_core.models import utc_now
from agent_core.sample_html import SANITIZER_VERSION
from runtime.package_tool import (
    SAMPLE_MAX_FILE_BYTES,
    SAMPLE_MAX_FILES,
    SAMPLE_MAX_TOTAL_BYTES,
    TEXT_SUFFIXES,
    normalize_package_path,
    package_media_type,
)
from storage.errors import ConflictError
from storage.full_deck_generation_store import FullDeckGenerationStoreMixin
from storage.persistence import atomic_bytes, exclusive_file_lock, json_text
from storage.prompt_audit import PromptAuditMixin
from storage.retained_project import (
    RetainedProjectError,
    materialize_full_deck_revision,
    retained_full_deck_dir as resolve_retained_full_deck_dir,
    write_artifacts_readme,
)
from storage.sqlite_schema import PROJECT_SCHEMA


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
CHECKPOINT_ID = re.compile(r"^checkpoint_[a-f0-9]{24}$")
REVISION_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
ARTIFACT_ID = REVISION_HASH
PROMPT_CALL_ID = re.compile(r"^prompt_[a-f0-9]{32}$")
STAGE_IDS = (
    "intake",
    "intake_clarify",
    "narrative_structure",
    "slide_outline",
    "ppt_sample",
    "ppt_full",
    "acceptance",
)
SCHEMA_VERSION = 5
INITIALIZATION_LOCK_TIMEOUT_SECONDS = 30
MAX_GENERATED_HTML_FILES_PER_ATTEMPT = 12
MAX_GENERATED_HTML_BYTES_PER_FILE = 1_000_000


def mark_full_deck_stale(manifest: dict[str, Any]) -> None:
    """Retain downstream history while invalidating it after an upstream change."""

    root = manifest.get("full_deck")
    if not root:
        return
    for reference in root.get("revision_refs", []):
        reference["status"] = "stale"
    for revision in manifest.get("full_deck_revisions", []):
        revision["status"] = "stale"


def full_deck_phase(revision: dict[str, Any]) -> str:
    """Project the workflow phase from the selected immutable revision."""

    if revision.get("status") == "approved":
        return "completed"
    if revision.get("status") == "draft":
        return "ready_to_generate"
    return "waiting_human_approval"


class ProjectStore(FullDeckGenerationStoreMixin, PromptAuditMixin):
    _locks: dict[str, threading.RLock] = {}
    _initialized_databases: dict[str, int] = {}

    def __init__(self, root: Path, project_id: str):
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project id")
        self.projects_root = root.resolve()
        self.project_id = project_id
        self.root = (root / project_id).resolve()
        if not self.root.is_relative_to(self.projects_root):
            raise ValueError("invalid project root")
        self.database_path = self.root / "project.db"
        self.initialization_lock_path = self.root / ".project-db-init.lock"
        self.manifest_path = self.root / "manifest.json"
        self.artifacts_container_root = self.root / "artifacts"
        self.artifacts_root = self.artifacts_container_root / "_objects" / "html"
        self.package_artifacts_root = (
            self.artifacts_container_root / "_objects" / "packages"
        )
        self.legacy_artifacts_root = self.artifacts_container_root / "html"
        self.legacy_package_artifacts_root = self.artifacts_container_root / "packages"
        self.full_deck_artifacts_root = self.artifacts_container_root / "full_decks"
        self.generated_html_root = self.root / "generated_html"
        self.lock = self._locks.setdefault(str(self.root), threading.RLock())

    @staticmethod
    def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
        resolved = path.resolve()
        return any(resolved.is_relative_to(root.resolve()) for root in roots)

    def _is_sample_artifact_path(self, path: Path) -> bool:
        return self._inside(path, (self.artifacts_root, self.legacy_artifacts_root))

    def _is_package_artifact_path(self, path: Path) -> bool:
        return self._inside(
            path,
            (self.package_artifacts_root, self.legacy_package_artifacts_root),
        )

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_database(self) -> None:
        database_key = str(self.database_path)
        process_id = os.getpid()
        if (
            self.database_path.is_file()
            and self._initialized_databases.get(database_key) == process_id
        ):
            return
        with self.lock:
            if (
                self.database_path.is_file()
                and self._initialized_databases.get(database_key) == process_id
            ):
                return
            with exclusive_file_lock(
                self.initialization_lock_path,
                timeout_seconds=INITIALIZATION_LOCK_TIMEOUT_SECONDS,
            ):
                connection = self._connect()
                try:
                    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                    if journal_mode.lower() != "wal":
                        connection.execute("PRAGMA journal_mode = WAL")
                    previous_row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone() if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                    ).fetchone() else None
                    previous_version = int(previous_row["value"]) if previous_row else 0
                    connection.executescript(PROJECT_SCHEMA)
                    connection.execute("BEGIN IMMEDIATE")
                    if previous_version < 4:
                        for table in ("project_state", "checkpoints"):
                            rows = connection.execute(
                                f"SELECT rowid AS storage_rowid, payload_json FROM {table}"
                            ).fetchall()
                            for row in rows:
                                payload = json.loads(row["payload_json"])
                                payload["format_version"] = 4
                                payload.setdefault("full_deck", None)
                                payload.pop("full_deck_revisions", None)
                                connection.execute(
                                    f"UPDATE {table} SET payload_json = ? WHERE rowid = ?",
                                    (json_text(payload), row["storage_rowid"]),
                                )
                    if previous_version < 5:
                        for table in ("project_state", "checkpoints"):
                            rows = connection.execute(
                                f"SELECT rowid AS storage_rowid, payload_json FROM {table}"
                            ).fetchall()
                            for row in rows:
                                payload = json.loads(row["payload_json"])
                                payload["format_version"] = 5
                                connection.execute(
                                    f"UPDATE {table} SET payload_json = ? WHERE rowid = ?",
                                    (json_text(payload), row["storage_rowid"]),
                                )
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                if self.manifest_path.is_file():
                    self._migrate_legacy_files()
                self._backfill_retained_full_decks()
            self._initialized_databases[database_key] = process_id

    def _backfill_retained_full_decks(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM project_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                return
            manifest = self._hydrate_manifest(
                json.loads(row["payload_json"]),
                connection,
                include_sample_html=False,
            )
        write_artifacts_readme(self.artifacts_container_root)
        for revision in manifest.get("full_deck_revisions", []):
            self._materialize_full_deck_revision(manifest, revision)

    def exists(self) -> bool:
        if self.database_path.is_file():
            try:
                self._ensure_database()
                with self._connect() as connection:
                    return connection.execute(
                        "SELECT 1 FROM project_state WHERE singleton = 1"
                    ).fetchone() is not None
            except sqlite3.DatabaseError:
                return False
        return self.manifest_path.is_file()

    def _raw_current(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM project_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise FileNotFoundError(self.project_id)
        return json.loads(row["payload_json"])

    def read(
        self,
        *,
        latest_sample_only: bool = False,
        include_sample_html: bool = True,
    ) -> dict[str, Any]:
        self._ensure_database()
        with self._connect() as connection:
            payload = self._raw_current(connection)
            return self._hydrate_manifest(
                payload,
                connection,
                latest_sample_only=latest_sample_only,
                include_sample_html=include_sample_html,
            )

    def _artifact_record(self, content: bytes) -> dict[str, Any]:
        hexadecimal = hashlib.sha256(content).hexdigest()
        artifact_id = f"sha256:{hexadecimal}"
        legacy_relative_path = f"artifacts/html/{hexadecimal}.html"
        legacy_path = self.root / legacy_relative_path
        relative_path = (
            legacy_relative_path
            if legacy_path.is_file()
            else f"artifacts/_objects/html/{hexadecimal}.html"
        )
        path = self.root / relative_path
        if path.is_file():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != hexadecimal:
                raise ConflictError("artifact_corrupt")
        else:
            atomic_bytes(path, content)
        return {
            "artifact_id": artifact_id,
            "kind": "sample_html",
            "media_type": "text/html; charset=utf-8",
            "sha256": artifact_id,
            "size_bytes": len(content),
            "relative_path": relative_path,
            "sanitizer_version": SANITIZER_VERSION,
            "created_at": utc_now(),
        }

    def _package_artifact_record(
        self,
        logical_path: str,
        content: bytes,
        media_type: str,
    ) -> dict[str, Any]:
        path = normalize_package_path(logical_path)
        content_checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        artifact_id = "sha256:" + hashlib.sha256(
            path.encode("utf-8") + b"\0" + content
        ).hexdigest()
        suffix = Path(path).suffix.lower()
        preferred_relative_path = (
            "artifacts/_objects/packages/"
            f"{artifact_id.removeprefix('sha256:')}{suffix}"
        )
        legacy_relative_path = (
            f"artifacts/packages/{artifact_id.removeprefix('sha256:')}{suffix}"
        )
        relative_path = (
            legacy_relative_path
            if (self.root / legacy_relative_path).is_file()
            else preferred_relative_path
        )
        artifact_path = (self.root / relative_path).resolve()
        if not self._is_package_artifact_path(artifact_path):
            raise ValueError("invalid package artifact path")
        if artifact_path.is_file():
            if artifact_path.read_bytes() != content:
                raise ConflictError("artifact_corrupt")
        else:
            atomic_bytes(artifact_path, content)
        return {
            "artifact_id": artifact_id,
            "kind": "html_ppt_package_file",
            "media_type": media_type or package_media_type(path),
            "sha256": content_checksum,
            "size_bytes": len(content),
            "relative_path": relative_path,
            "sanitizer_version": "sandbox-csp-v1",
            "created_at": utc_now(),
        }

    def _materialize_full_deck_revision(
        self,
        manifest: dict[str, Any],
        revision: dict[str, Any],
    ) -> None:
        try:
            materialize_full_deck_revision(
                manifest,
                revision,
                full_deck_root=self.full_deck_artifacts_root,
                package_artifact_roots=(
                    self.package_artifacts_root,
                    self.legacy_package_artifacts_root,
                ),
            )
        except RetainedProjectError as exc:
            raise ConflictError(str(exc)) from exc

    def retained_full_deck_dir(self, revision_hash: str) -> Path:
        return resolve_retained_full_deck_dir(
            self.full_deck_artifacts_root,
            revision_hash,
        )

    def save_generated_html_attempt(
        self,
        prompt_call_id: str,
        payload: dict[str, Any],
    ) -> list[str]:
        """Persist model HTML before validation without publishing it as an artifact."""

        if not PROMPT_CALL_ID.fullmatch(prompt_call_id):
            raise ValueError("invalid prompt call id")
        pages = payload.get("pages")
        if not isinstance(pages, list):
            return []
        relative_paths: list[str] = []
        for index, page in enumerate(pages[:MAX_GENERATED_HTML_FILES_PER_ATTEMPT], start=1):
            if not isinstance(page, dict) or not isinstance(page.get("html"), str):
                continue
            content = page["html"].encode("utf-8")
            if not content or len(content) > MAX_GENERATED_HTML_BYTES_PER_FILE:
                continue
            relative_path = f"generated_html/{prompt_call_id}/page-{index:02d}.html"
            path = (self.root / relative_path).resolve()
            if not path.is_relative_to(self.generated_html_root.resolve()):
                raise ValueError("invalid generated HTML path")
            atomic_bytes(path, content)
            relative_paths.append(relative_path)
        return relative_paths

    def save_generated_package_attempt(
        self,
        prompt_call_id: str,
        files: list[dict[str, Any]],
    ) -> list[str]:
        """Persist a bounded draft package before publication for repair diagnostics."""

        if not PROMPT_CALL_ID.fullmatch(prompt_call_id):
            raise ValueError("invalid prompt call id")
        relative_paths: list[str] = []
        total = 0
        for item in files[:SAMPLE_MAX_FILES]:
            path = normalize_package_path(str(item.get("path", "")))
            content_value = item.get("content")
            if not isinstance(content_value, str):
                continue
            if item.get("encoding", "utf-8") == "base64":
                try:
                    content = base64.b64decode(content_value, validate=True)
                except ValueError:
                    continue
            else:
                content = content_value.encode("utf-8")
            total += len(content)
            if (
                not content
                or len(content) > SAMPLE_MAX_FILE_BYTES
                or total > SAMPLE_MAX_TOTAL_BYTES
            ):
                continue
            relative_path = f"generated_html/{prompt_call_id}/package/{path}"
            candidate = (self.root / relative_path).resolve()
            if not candidate.is_relative_to(self.generated_html_root.resolve()):
                raise ValueError("invalid generated package path")
            atomic_bytes(candidate, content)
            relative_paths.append(relative_path)
        return relative_paths

    def load_generated_package_attempt(
        self,
        prompt_call_id: str,
    ) -> list[dict[str, Any]]:
        """Restore a bounded draft package for a resumable sample tool loop."""

        if not PROMPT_CALL_ID.fullmatch(prompt_call_id):
            raise ValueError("invalid prompt call id")
        package_root = (
            self.generated_html_root / prompt_call_id / "package"
        ).resolve()
        if not package_root.is_dir() or not package_root.is_relative_to(
            self.generated_html_root.resolve()
        ):
            return []
        result: list[dict[str, Any]] = []
        total = 0
        for candidate in sorted(path for path in package_root.rglob("*") if path.is_file()):
            resolved = candidate.resolve()
            if not resolved.is_relative_to(package_root):
                continue
            content = resolved.read_bytes()
            total += len(content)
            if (
                not content
                or len(content) > SAMPLE_MAX_FILE_BYTES
                or total > SAMPLE_MAX_TOTAL_BYTES
            ):
                continue
            logical_path = resolved.relative_to(package_root).as_posix()
            if resolved.suffix.lower() in TEXT_SUFFIXES:
                try:
                    value = content.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    value = base64.b64encode(content).decode("ascii")
                    encoding = "base64"
            else:
                value = base64.b64encode(content).decode("ascii")
                encoding = "base64"
            result.append({
                "path": logical_path,
                "content": value,
                "encoding": encoding,
            })
        return result

    def _externalize_manifest(
        self,
        manifest: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        write_artifacts_readme(self.artifacts_container_root)
        projection = deepcopy(manifest)
        if projection.get("samples") and not projection.get("current_sample_revision_hash"):
            projection["current_sample_revision_hash"] = projection["samples"][-1]["revision_hash"]
        projection["format_version"] = SCHEMA_VERSION
        projection["storage"] = {
            "engine": "sqlite-wal",
            "database": "project.db",
            "artifacts": "artifacts",
        }
        artifacts: dict[str, dict[str, Any]] = {}
        for sample in projection.get("samples", []):
            package = sample.get("package")
            if package:
                for item in package.get("files", []):
                    content_value = item.pop("content", None)
                    if content_value is not None:
                        if item.get("encoding", "utf-8") == "base64":
                            try:
                                content = base64.b64decode(content_value, validate=True)
                            except ValueError as exc:
                                raise ConflictError("package_file_invalid") from exc
                        else:
                            content = content_value.encode("utf-8")
                        record = self._package_artifact_record(
                            item["path"], content, item.get("media_type") or package_media_type(item["path"]),
                        )
                        artifacts[record["artifact_id"]] = record
                        item.update({
                            "artifact_id": record["artifact_id"],
                            "sha256": record["sha256"],
                            "size": record["size_bytes"],
                            "media_type": record["media_type"],
                        })
                    elif not ARTIFACT_ID.fullmatch(str(item.get("artifact_id", ""))):
                        raise ConflictError("package_artifact_missing")
            for page in sample.get("pages", []):
                html = page.pop("html", None)
                if html is not None:
                    content = html.encode("utf-8")
                    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
                    existing_reference = (
                        page.get("artifact_id") == checksum
                        and page.get("sha256") == checksum
                        and page.get("size") == len(content)
                        and isinstance(page.get("sanitizer_version"), str)
                    )
                    if not existing_reference:
                        record = self._artifact_record(content)
                        artifacts[record["artifact_id"]] = record
                        page.update({
                            "artifact_id": record["artifact_id"],
                            "sha256": record["sha256"],
                            "size": record["size_bytes"],
                            "sanitizer_version": record["sanitizer_version"],
                        })
                elif not ARTIFACT_ID.fullmatch(str(page.get("artifact_id", ""))):
                    raise ConflictError("sample_artifact_missing")
        for revision in projection.get("full_deck_revisions", []):
            package = revision.get("package")
            if not package:
                continue
            for item in package.get("files", []):
                content_value = item.pop("content", None)
                if content_value is not None:
                    if item.get("encoding", "utf-8") == "base64":
                        try:
                            content = base64.b64decode(content_value, validate=True)
                        except ValueError as exc:
                            raise ConflictError("package_file_invalid") from exc
                    else:
                        content = content_value.encode("utf-8")
                    record = self._package_artifact_record(
                        item["path"],
                        content,
                        item.get("media_type") or package_media_type(item["path"]),
                    )
                    artifacts[record["artifact_id"]] = record
                    item.update({
                        "artifact_id": record["artifact_id"],
                        "sha256": record["sha256"],
                        "size": record["size_bytes"],
                        "media_type": record["media_type"],
                    })
                elif not ARTIFACT_ID.fullmatch(str(item.get("artifact_id", ""))):
                    raise ConflictError("package_artifact_missing")
            self._materialize_full_deck_revision(projection, revision)
        payload = deepcopy(projection)
        payload["documents"] = {
            document_type: [
                {"revision_hash": revision["revision_hash"], "status": revision["status"]}
                for revision in revisions
            ]
            for document_type, revisions in projection.get("documents", {}).items()
        }
        payload["samples"] = [
            {"revision_hash": sample["revision_hash"], "status": sample["status"]}
            for sample in projection.get("samples", [])
        ]
        payload.pop("full_deck_revisions", None)
        return payload, list(artifacts.values()), projection

    def _hydrate_manifest(
        self,
        payload: dict[str, Any],
        connection: sqlite3.Connection,
        *,
        latest_sample_only: bool = False,
        include_sample_html: bool = True,
    ) -> dict[str, Any]:
        value = deepcopy(payload)
        value["format_version"] = SCHEMA_VERSION
        value.setdefault("clarification_history", [])
        value.setdefault(
            "clarification_completed",
            value.get("state") not in {"intake", "intake_clarify"},
        )
        value.setdefault("storage", {
            "engine": "sqlite-wal",
            "database": "project.db",
            "artifacts": "artifacts",
        })
        for document_type, revisions in value.get("documents", {}).items():
            expanded = []
            for revision in revisions:
                if "markdown_body" in revision:
                    expanded.append(revision)
                    continue
                row = connection.execute(
                    "SELECT * FROM document_revisions WHERE revision_hash = ?",
                    (revision.get("revision_hash"),),
                ).fetchone()
                if row is None:
                    raise ConflictError("document_revision_missing")
                expanded.append({
                    "document_id": row["document_id"],
                    "document_type": row["document_type"],
                    "revision": row["revision"],
                    "revision_hash": row["revision_hash"],
                    "parent_revision_hash": row["parent_revision_hash"],
                    "markdown_body": row["markdown_body"],
                    "status": revision.get("status", row["status"]),
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "provenance": json.loads(row["provenance_json"]),
                })
            value["documents"][document_type] = expanded
        expanded_samples = []
        for sample in value.get("samples", []):
            if "pages" in sample or "package" in sample:
                expanded_samples.append(sample)
                continue
            row = connection.execute(
                "SELECT * FROM sample_revisions WHERE revision_hash = ?",
                (sample.get("revision_hash"),),
            ).fetchone()
            if row is None:
                raise ConflictError("sample_revision_missing")
            package_row = connection.execute(
                "SELECT * FROM sample_packages WHERE revision_hash = ?",
                (row["revision_hash"],),
            ).fetchone()
            pages = connection.execute(
                """
                SELECT page_id, title, artifact_id, sha256, size_bytes, sanitizer_version
                FROM sample_pages WHERE revision_hash = ? ORDER BY page_index
                """,
                (row["revision_hash"],),
            ).fetchall()
            expanded = {
                "sample_id": row["sample_id"],
                "revision": row["revision"],
                "revision_hash": row["revision_hash"],
                "parent_revision_hash": row["parent_revision_hash"],
                "pages": [{
                    "page_id": page["page_id"],
                    "title": page["title"],
                    "artifact_id": page["artifact_id"],
                    "sha256": page["sha256"],
                    "size": page["size_bytes"],
                    "sanitizer_version": page["sanitizer_version"],
                } for page in pages],
                "feedback": row["feedback"],
                "status": sample.get("status", row["status"]),
                "created_at": row["created_at"],
                "provenance": json.loads(row["provenance_json"]),
            }
            if package_row is not None:
                package_files = connection.execute(
                    """
                    SELECT f.logical_path, f.artifact_id, a.sha256, f.size_bytes,
                           f.media_type, f.origin
                    FROM sample_package_files f
                    JOIN artifacts a ON a.artifact_id = f.artifact_id
                    WHERE f.revision_hash = ? ORDER BY f.file_index
                    """,
                    (row["revision_hash"],),
                ).fetchall()
                expanded["package"] = {
                    "entrypoint": package_row["entrypoint"],
                    "title": package_row["title"],
                    "slide_count": package_row["slide_count"],
                    "slides": json.loads(package_row["slides_json"]),
                    "package_hash": package_row["package_hash"],
                    "files": [{
                        "path": item["logical_path"],
                        "artifact_id": item["artifact_id"],
                        "sha256": item["sha256"],
                        "size": item["size_bytes"],
                        "media_type": item["media_type"],
                        "origin": item["origin"],
                    } for item in package_files],
                }
            expanded_samples.append(expanded)
        value["samples"] = expanded_samples
        full_deck = value.get("full_deck")
        expanded_full_deck_revisions = value.get("full_deck_revisions", [])
        if full_deck and not expanded_full_deck_revisions:
            for reference in full_deck.get("revision_refs", []):
                row = connection.execute(
                    "SELECT * FROM full_deck_revisions WHERE revision_hash = ?",
                    (reference.get("revision_hash"),),
                ).fetchone()
                if row is None:
                    raise ConflictError("full_deck_revision_missing")
                page_rows = connection.execute(
                    """
                    SELECT position, slot_id, source_slide_number, title, status,
                           source_type, content_ref_json, derived_from_json
                    FROM full_deck_pages
                    WHERE revision_hash = ? ORDER BY position
                    """,
                    (row["revision_hash"],),
                ).fetchall()
                revision = {
                    "full_deck_id": row["full_deck_id"],
                    "revision": row["revision"],
                    "revision_hash": row["revision_hash"],
                    "parent_revision_hash": row["parent_revision_hash"],
                    "feedback": row["feedback"],
                    "status": reference.get("status", row["status"]),
                    "plan": {
                        "pages": [{
                            "slot_id": page["slot_id"],
                            "position": page["position"],
                            "outline_ref": {
                                "outline_revision_hash": full_deck["outline_revision_hash"],
                                "source_slide_number": page["source_slide_number"],
                            } if page["source_slide_number"] is not None else None,
                            "title": page["title"],
                            "status": page["status"],
                            "source_type": page["source_type"],
                            "content_ref": json.loads(page["content_ref_json"])
                            if page["content_ref_json"] else None,
                            "derived_from": json.loads(page["derived_from_json"])
                            if page["derived_from_json"] else None,
                        } for page in page_rows],
                    },
                    "package": None,
                    "created_at": row["created_at"],
                    "provenance": json.loads(row["provenance_json"]),
                }
                package_row = connection.execute(
                    "SELECT * FROM full_deck_packages WHERE revision_hash = ?",
                    (row["revision_hash"],),
                ).fetchone()
                if package_row is not None:
                    package_files = connection.execute(
                        """
                        SELECT f.logical_path, f.artifact_id, a.sha256, f.size_bytes,
                               f.media_type, f.origin
                        FROM full_deck_package_files f
                        JOIN artifacts a ON a.artifact_id = f.artifact_id
                        WHERE f.revision_hash = ? ORDER BY f.file_index
                        """,
                        (row["revision_hash"],),
                    ).fetchall()
                    revision["package"] = {
                        "entrypoint": package_row["entrypoint"],
                        "title": package_row["title"],
                        "slide_count": package_row["slide_count"],
                        "slides": json.loads(package_row["slides_json"]),
                        "package_hash": package_row["package_hash"],
                        "composition_manifest": json.loads(
                            package_row["composition_manifest_json"]
                        ),
                        "files": [{
                            "path": item["logical_path"],
                            "artifact_id": item["artifact_id"],
                            "sha256": item["sha256"],
                            "size": item["size_bytes"],
                            "media_type": item["media_type"],
                            "origin": item["origin"],
                        } for item in package_files],
                    }
                expanded_full_deck_revisions.append(revision)
        value["full_deck"] = full_deck or None
        value["full_deck_revisions"] = expanded_full_deck_revisions
        samples = value.get("samples", [])
        current_hash = value.get("current_sample_revision_hash")
        if not current_hash and samples:
            current_hash = samples[-1]["revision_hash"]
            value["current_sample_revision_hash"] = current_hash
        selected = [item for item in samples if item.get("revision_hash") == current_hash]
        targets = (selected if latest_sample_only else samples) if include_sample_html else []
        for sample in targets:
            package = sample.get("package")
            if package:
                for item in package.get("files", []):
                    artifact_id = str(item.get("artifact_id", ""))
                    row = connection.execute(
                        "SELECT sha256, size_bytes, relative_path FROM artifacts WHERE artifact_id = ?",
                        (artifact_id,),
                    ).fetchone()
                    if row is None:
                        raise ConflictError("package_artifact_missing")
                    path = (self.root / row["relative_path"]).resolve()
                    if not self._is_package_artifact_path(path) or not path.is_file():
                        raise ConflictError("package_artifact_missing")
                    content = path.read_bytes()
                    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
                    if checksum != row["sha256"] or len(content) != row["size_bytes"]:
                        raise ConflictError("artifact_corrupt")
                    if Path(item["path"]).suffix.lower() not in TEXT_SUFFIXES:
                        item["content"] = base64.b64encode(content).decode("ascii")
                        item["encoding"] = "base64"
                    else:
                        try:
                            item["content"] = content.decode("utf-8")
                            item["encoding"] = "utf-8"
                        except UnicodeDecodeError:
                            item["content"] = base64.b64encode(content).decode("ascii")
                            item["encoding"] = "base64"
            for page in sample.get("pages", []):
                artifact_id = str(page.get("artifact_id", ""))
                if not ARTIFACT_ID.fullmatch(artifact_id):
                    if "html" in page:  # Transitional in-memory payload.
                        continue
                    raise ConflictError("sample_artifact_missing")
                row = connection.execute(
                    "SELECT sha256, size_bytes, relative_path FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    raise ConflictError("sample_artifact_missing")
                path = (self.root / row["relative_path"]).resolve()
                if not self._is_sample_artifact_path(path) or not path.is_file():
                    raise ConflictError("sample_artifact_missing")
                content = path.read_bytes()
                checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
                if checksum != row["sha256"] or len(content) != row["size_bytes"]:
                    raise ConflictError("artifact_corrupt")
                page["html"] = content.decode("utf-8")
        return value

    @staticmethod
    def _insert_artifacts(connection: sqlite3.Connection, artifacts: list[dict[str, Any]]) -> None:
        for item in artifacts:
            existing = connection.execute(
                "SELECT sha256, size_bytes, relative_path FROM artifacts WHERE artifact_id = ?",
                (item["artifact_id"],),
            ).fetchone()
            if existing is not None and (
                existing["sha256"] != item["sha256"]
                or existing["size_bytes"] != item["size_bytes"]
                or existing["relative_path"] != item["relative_path"]
            ):
                raise ConflictError("artifact_metadata_conflict")
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    artifact_id, kind, media_type, sha256, size_bytes,
                    relative_path, sanitizer_version, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["artifact_id"], item["kind"], item["media_type"], item["sha256"],
                    item["size_bytes"], item["relative_path"], item["sanitizer_version"],
                    item["created_at"],
                ),
            )

    @staticmethod
    def _sync_revisions(
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        checkpoint_id: str,
    ) -> None:
        for document_type, revisions in payload.get("documents", {}).items():
            for revision in revisions:
                connection.execute(
                    """
                    INSERT INTO document_revisions(
                        revision_hash, document_id, document_type, revision,
                        parent_revision_hash, markdown_body, status, created_by,
                        created_at, provenance_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(revision_hash) DO UPDATE SET status = excluded.status
                    """,
                    (
                        revision["revision_hash"], revision["document_id"], document_type,
                        revision["revision"], revision.get("parent_revision_hash"),
                        revision["markdown_body"], revision["status"], revision["created_by"],
                        revision["created_at"], json_text(revision.get("provenance", {})),
                    ),
                )
        for sample in payload.get("samples", []):
            connection.execute(
                """
                INSERT INTO sample_revisions(
                    revision_hash, sample_id, revision, parent_revision_hash,
                    feedback, status, created_at, provenance_json, checkpoint_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_hash) DO UPDATE SET status = excluded.status
                """,
                (
                    sample["revision_hash"], sample.get("sample_id", "sample_ppt"),
                    sample["revision"], sample.get("parent_revision_hash"), sample.get("feedback"),
                    sample["status"], sample["created_at"], json_text(sample.get("provenance", {})),
                    checkpoint_id,
                ),
            )
            for index, page in enumerate(sample.get("pages", [])):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sample_pages(
                        revision_hash, page_index, page_id, title, artifact_id,
                        sha256, size_bytes, sanitizer_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample["revision_hash"], index, page["page_id"], page["title"],
                        page["artifact_id"], page["sha256"], page["size"],
                        page["sanitizer_version"],
                    ),
                )
            package = sample.get("package")
            if package:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sample_packages(
                        revision_hash, package_hash, entrypoint, title,
                        slide_count, slides_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample["revision_hash"], package["package_hash"],
                        package["entrypoint"], package["title"], package["slide_count"],
                        json_text(package.get("slides", [])),
                    ),
                )
                connection.execute(
                    "DELETE FROM sample_package_files WHERE revision_hash = ?",
                    (sample["revision_hash"],),
                )
                for index, item in enumerate(package.get("files", [])):
                    connection.execute(
                        """
                        INSERT INTO sample_package_files(
                            revision_hash, file_index, logical_path, artifact_id,
                            media_type, size_bytes, origin
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sample["revision_hash"], index, item["path"], item["artifact_id"],
                            item["media_type"], item["size"], item.get("origin", "model_output"),
                        ),
                    )
        for revision in payload.get("full_deck_revisions", []):
            connection.execute(
                """
                INSERT INTO full_deck_revisions(
                    revision_hash, full_deck_id, revision, parent_revision_hash,
                    feedback, status, created_at, provenance_json, checkpoint_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_hash) DO UPDATE SET
                    checkpoint_id = CASE
                        WHEN full_deck_revisions.status != excluded.status
                        THEN excluded.checkpoint_id
                        ELSE full_deck_revisions.checkpoint_id
                    END,
                    status = excluded.status
                """,
                (
                    revision["revision_hash"], revision["full_deck_id"],
                    revision["revision"], revision.get("parent_revision_hash"),
                    revision.get("feedback"), revision["status"], revision["created_at"],
                    json_text(revision.get("provenance", {})), checkpoint_id,
                ),
            )
            connection.execute(
                "DELETE FROM full_deck_pages WHERE revision_hash = ?",
                (revision["revision_hash"],),
            )
            for page in revision.get("plan", {}).get("pages", []):
                outline_ref = page.get("outline_ref") or {}
                connection.execute(
                    """
                    INSERT INTO full_deck_pages(
                        revision_hash, position, slot_id, source_slide_number,
                        title, status, source_type, content_ref_json, derived_from_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision["revision_hash"], page["position"], page["slot_id"],
                        outline_ref.get("source_slide_number"), page["title"], page["status"],
                        page["source_type"],
                        json_text(page["content_ref"]) if page.get("content_ref") else None,
                        json_text(page["derived_from"]) if page.get("derived_from") else None,
                    ),
                )
            package = revision.get("package")
            if package:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO full_deck_packages(
                        revision_hash, package_hash, entrypoint, title, slide_count,
                        slides_json, composition_manifest_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision["revision_hash"], package["package_hash"],
                        package["entrypoint"], package["title"], package["slide_count"],
                        json_text(package.get("slides", [])),
                        json_text(package.get("composition_manifest", {})),
                    ),
                )
                connection.execute(
                    "DELETE FROM full_deck_package_files WHERE revision_hash = ?",
                    (revision["revision_hash"],),
                )
                for index, item in enumerate(package.get("files", [])):
                    connection.execute(
                        """
                        INSERT INTO full_deck_package_files(
                            revision_hash, file_index, logical_path, artifact_id,
                            media_type, size_bytes, origin
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision["revision_hash"], index, item["path"], item["artifact_id"],
                            item["media_type"], item["size"],
                            item.get("origin", "model_output"),
                        ),
                    )

    @staticmethod
    def _sync_branches(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
        branches = payload.get("branches", {payload.get("branch", "main"): payload["checkpoint_id"]})
        metadata = payload.get("branch_meta", {})
        for name, head in branches.items():
            meta = metadata.get(name, {})
            connection.execute(
                """
                INSERT INTO branches(
                    name, head_checkpoint_id, parent_branch, from_checkpoint_id,
                    created_at, mode, source_stage
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    head_checkpoint_id = excluded.head_checkpoint_id,
                    parent_branch = excluded.parent_branch,
                    from_checkpoint_id = excluded.from_checkpoint_id,
                    created_at = excluded.created_at,
                    mode = excluded.mode,
                    source_stage = excluded.source_stage
                """,
                (
                    name, head, meta.get("parent"), meta.get("from_checkpoint"),
                    meta.get("created_at"), meta.get("mode", "fork_after"),
                    meta.get("source_stage"),
                ),
            )

    def _write_project_state(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO project_state(
                singleton, project_id, title, branch, state, phase,
                checkpoint_id, created_at, updated_at, payload_json
            ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["project_id"], payload["title"], payload.get("branch", "main"),
                payload["state"], payload["phase"], payload["checkpoint_id"],
                payload["created_at"], payload["updated_at"], json_text(payload),
            ),
        )

    def _commit_raw(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        projection: dict[str, Any],
        artifacts: list[dict[str, Any]],
        event: str | list[tuple[str, dict[str, Any]]],
        details: dict[str, Any],
        parent_checkpoint_id: str | None,
    ) -> None:
        self._insert_artifacts(connection, artifacts)
        connection.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, parent_checkpoint_id, branch, state,
                phase, updated_at, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["checkpoint_id"], parent_checkpoint_id, payload.get("branch", "main"),
                payload["state"], payload["phase"], payload["updated_at"], json_text(payload),
            ),
        )
        self._sync_revisions(connection, projection, payload["checkpoint_id"])
        self._sync_branches(connection, payload)
        self._write_project_state(connection, payload)
        events = [(event, details)] if isinstance(event, str) else event
        for event_name, event_details in events:
            connection.execute(
                "INSERT INTO events(at, event, checkpoint_id, details_json) VALUES(?, ?, ?, ?)",
                (
                    utc_now(), event_name, payload["checkpoint_id"],
                    json_text(event_details),
                ),
            )

    def create(self, task: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        self._ensure_database()
        with self.lock, self._transaction() as connection:
            if connection.execute("SELECT 1 FROM project_state WHERE singleton = 1").fetchone():
                raise ConflictError("project already exists")
            checkpoint = "checkpoint_" + uuid4().hex[:24]
            created_at = utc_now()
            manifest = {
                "project_id": self.project_id,
                "title": task["title"],
                "branch": "main",
                "branches": {"main": checkpoint},
                "branch_meta": {"main": {"parent": None, "from_checkpoint": None, "created_at": created_at}},
                "state": "intake",
                "phase": "ready_for_clarification",
                "task_card": task,
                "clarification_answers": {},
                "clarification_history": [],
                "clarification_completed": False,
                "question_card": None,
                "documents": {"narrative_structure": [], "slide_outline": []},
                "samples": [],
                "current_sample_revision_hash": None,
                "full_deck": None,
                "full_deck_revisions": [],
                "checkpoint_id": checkpoint,
                "active_job_id": None,
                "created_at": created_at,
                "updated_at": created_at,
                "runtime": runtime,
            }
            payload, artifacts, projection = self._externalize_manifest(manifest)
            self._commit_raw(
                connection, payload, projection, artifacts, "project_created",
                {"checkpoint_id": checkpoint}, None,
            )
            return self._hydrate_manifest(payload, connection)

    def update(
        self,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
        event: str,
        details: dict[str, Any] | None = None,
        *,
        expected_checkpoint_id: str,
    ) -> dict[str, Any]:
        return self.update_events(
            transform,
            [(event, details or {})],
            expected_checkpoint_id=expected_checkpoint_id,
        )

    def update_events(
        self,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
        events: list[tuple[str, dict[str, Any]]],
        *,
        expected_checkpoint_id: str,
    ) -> dict[str, Any]:
        if not events:
            raise ValueError("at least one project event is required")
        self._ensure_database()
        with self.lock, self._transaction() as connection:
            raw = self._raw_current(connection)
            if raw["checkpoint_id"] != expected_checkpoint_id:
                raise ConflictError("stale_revision")
            manifest = self._hydrate_manifest(raw, connection)
            parent_checkpoint_id = manifest["checkpoint_id"]
            candidate = deepcopy(manifest)
            candidate["checkpoint_id"] = "checkpoint_" + uuid4().hex[:24]
            updated = transform(candidate)
            updated["updated_at"] = utc_now()
            updated.setdefault("branches", {})[updated.get("branch", "main")] = updated["checkpoint_id"]
            payload, artifacts, projection = self._externalize_manifest(updated)
            self._commit_raw(
                connection, payload, projection, artifacts, events, {}, parent_checkpoint_id,
            )
            return self._hydrate_manifest(payload, connection)

    def _migrate_legacy_files(self) -> None:
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM project_state WHERE singleton = 1").fetchone():
                return
            try:
                current = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConflictError("legacy_manifest_corrupt") from exc

            snapshots: list[dict[str, Any]] = []
            for path in (self.root / "checkpoints").glob("checkpoint_*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if item.get("checkpoint_id") == path.stem:
                        snapshots.append(item)
                except (OSError, json.JSONDecodeError):
                    continue
            if not any(item.get("checkpoint_id") == current.get("checkpoint_id") for item in snapshots):
                snapshots.append(current)
            snapshots.sort(key=lambda item: (item.get("updated_at") or "", item.get("checkpoint_id") or ""))

            prior_by_branch: dict[str, str] = {}
            final_meta = current.get("branch_meta", {})
            for item in snapshots:
                branch = item.get("branch", "main")
                parent = item.get("parent_checkpoint_id") or prior_by_branch.get(branch)
                if parent is None:
                    parent = final_meta.get(branch, {}).get("from_checkpoint")
                payload, artifacts, projection = self._externalize_manifest(item)
                self._insert_artifacts(connection, artifacts)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO checkpoints(
                        checkpoint_id, parent_checkpoint_id, branch, state,
                        phase, updated_at, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["checkpoint_id"], parent, branch, payload["state"],
                        payload["phase"], payload["updated_at"], json_text(payload),
                    ),
                )
                self._sync_revisions(connection, projection, payload["checkpoint_id"])
                prior_by_branch[branch] = payload["checkpoint_id"]

            current_payload, current_artifacts, current_projection = self._externalize_manifest(current)
            self._insert_artifacts(connection, current_artifacts)
            self._sync_revisions(connection, current_projection, current_payload["checkpoint_id"])
            self._sync_branches(connection, current_payload)
            self._write_project_state(connection, current_payload)

            events_path = self.root / "events.jsonl"
            if events_path.is_file():
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    details = {
                        key: value for key, value in item.items()
                        if key not in {"at", "event", "checkpoint_id"}
                    }
                    connection.execute(
                        "INSERT INTO events(at, event, checkpoint_id, details_json) VALUES(?, ?, ?, ?)",
                        (
                            item.get("at", utc_now()), item.get("event", "legacy_event"),
                            item.get("checkpoint_id", current_payload["checkpoint_id"]), json_text(details),
                        ),
                    )
            connection.execute(
                "INSERT INTO events(at, event, checkpoint_id, details_json) VALUES(?, ?, ?, ?)",
                (
                    utc_now(), "storage_migrated", current_payload["checkpoint_id"],
                    json_text({"format_version": SCHEMA_VERSION}),
                ),
            )

    def events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT at, event, checkpoint_id, details_json FROM events ORDER BY event_id"
                ).fetchall()
            else:
                if limit < 1 or limit > 2000:
                    raise ValueError("event limit out of range")
                rows = connection.execute(
                    """
                    SELECT at, event, checkpoint_id, details_json FROM (
                        SELECT event_id, at, event, checkpoint_id, details_json
                        FROM events ORDER BY event_id DESC LIMIT ?
                    ) ORDER BY event_id
                    """,
                    (limit,),
                ).fetchall()
        return [
            {
                "at": row["at"], "event": row["event"], "checkpoint_id": row["checkpoint_id"],
                **json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def checkpoints(self) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT checkpoint_id, parent_checkpoint_id, branch, state, phase, updated_at
                FROM checkpoints ORDER BY updated_at DESC, checkpoint_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _checkpoint_lineage(
        self,
        manifest: dict[str, Any],
        *,
        include_sample_html: bool = True,
    ) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            checkpoint_id = manifest["checkpoint_id"]
            visiting: set[str] = set()
            result: list[dict[str, Any]] = []
            while checkpoint_id:
                if checkpoint_id in visiting:
                    raise ConflictError("branch_lineage_corrupt")
                visiting.add(checkpoint_id)
                row = connection.execute(
                    "SELECT parent_checkpoint_id, payload_json FROM checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                ).fetchone()
                if row is None:
                    raise ConflictError("branch_head_missing")
                result.append(self._hydrate_manifest(
                    json.loads(row["payload_json"]),
                    connection,
                    include_sample_html=include_sample_html,
                ))
                checkpoint_id = row["parent_checkpoint_id"]
        result.reverse()
        return result

    @staticmethod
    def _latest_document(snapshot: dict[str, Any], document_type: str) -> dict[str, Any] | None:
        history = snapshot.get("documents", {}).get(document_type, [])
        return history[-1] if history else None

    def progress_snapshots(self) -> list[dict[str, Any]]:
        manifest = self.read(include_sample_html=False)
        lineage = self._checkpoint_lineage(manifest, include_sample_html=False)
        if not lineage:
            return []

        active_index = STAGE_IDS.index(manifest["state"])
        outline = self._latest_document(manifest, "slide_outline")
        current_hash = manifest.get("current_sample_revision_hash")
        sample = next(
            (item for item in manifest.get("samples", []) if item.get("revision_hash") == current_hash),
            (manifest.get("samples") or [None])[-1],
        )
        full_deck = manifest.get("full_deck") or {}
        current_full_deck_hash = full_deck.get("current_revision_hash")
        current_full_deck = next(
            (
                item for item in manifest.get("full_deck_revisions", [])
                if item.get("revision_hash") == current_full_deck_hash
            ),
            None,
        )
        if current_full_deck and current_full_deck.get("status") == "approved":
            completed_through = 5
        elif sample and sample.get("status") == "approved":
            completed_through = 4
        elif outline and outline.get("status") == "approved":
            completed_through = 3
        else:
            completed_through = active_index - 1

        def boundary(stage: str) -> dict[str, Any] | None:
            matches: list[dict[str, Any]]
            if stage == "intake":
                matches = [item for item in lineage if item.get("state") == "intake"]
                return matches[0] if matches else None
            if stage == "intake_clarify":
                matches = [
                    item for item in lineage
                    if item.get("state") == "narrative_structure"
                    and item.get("phase") == "ready_to_generate"
                    and (
                        item.get("clarification_completed")
                        or bool(item.get("clarification_answers"))
                    )
                ]
                return matches[-1] if matches else None
            if stage == "narrative_structure":
                matches = [
                    item for item in lineage
                    if item.get("state") == "slide_outline"
                    and item.get("phase") == "ready_to_generate"
                    and (self._latest_document(item, stage) or {}).get("status") == "approved"
                ]
                return matches[-1] if matches else None
            if stage == "slide_outline":
                matches = [
                    item for item in lineage
                    if item.get("state") in {"slide_outline", "ppt_sample"}
                    and (item.get("phase") == "completed" or item.get("state") == "ppt_sample")
                    and (self._latest_document(item, stage) or {}).get("status") == "approved"
                ]
                return matches[0] if matches else None
            if stage == "ppt_sample":
                matches = []
                for item in lineage:
                    selected_sample = next(
                        (
                            candidate for candidate in item.get("samples", [])
                            if candidate.get("revision_hash")
                            == item.get("current_sample_revision_hash")
                        ),
                        (item.get("samples") or [None])[-1],
                    )
                    if (
                        item.get("state") in {"ppt_sample", "ppt_full", "acceptance"}
                        and selected_sample
                        and selected_sample.get("status") == "approved"
                    ):
                        matches.append(item)
                return matches[0] if matches else None
            if stage == "ppt_full":
                matches = []
                for item in lineage:
                    root = item.get("full_deck") or {}
                    selected_revision = next(
                        (
                            candidate for candidate in item.get("full_deck_revisions", [])
                            if candidate.get("revision_hash") == root.get("current_revision_hash")
                        ),
                        None,
                    )
                    if selected_revision and selected_revision.get("status") == "approved":
                        matches.append(item)
                return matches[-1] if matches else None
            matches = [item for item in lineage if item.get("state") == "acceptance"]
            return matches[-1] if matches else None

        sequence = {item["checkpoint_id"]: index for index, item in enumerate(lineage, 1)}
        response = []
        for index, stage in enumerate(STAGE_IDS):
            completed = index <= completed_through
            snapshot = boundary(stage) if completed else lineage[-1] if index == active_index else None
            if snapshot is None:
                continue
            public_snapshot = deepcopy(snapshot)
            if public_snapshot.get("samples"):
                snapshot_hash = public_snapshot.get("current_sample_revision_hash")
                current_sample = deepcopy(next(
                    (
                        item for item in public_snapshot["samples"]
                        if item.get("revision_hash") == snapshot_hash
                    ),
                    public_snapshot["samples"][-1],
                ))
                current_sample["pages"] = [
                    {"page_id": page["page_id"], "title": page["title"]}
                    for page in current_sample.get("pages", [])
                ]
                if current_sample.get("package"):
                    current_sample["package"]["files"] = [
                        {
                            key: item[key] for key in (
                                "path", "artifact_id", "sha256", "size", "media_type", "origin"
                            ) if key in item
                        }
                        for item in current_sample["package"].get("files", [])
                    ]
                public_snapshot["samples"] = [current_sample]
            if public_snapshot.get("full_deck_revisions"):
                snapshot_root = public_snapshot.get("full_deck") or {}
                current_revision = deepcopy(next(
                    (
                        item for item in public_snapshot["full_deck_revisions"]
                        if item.get("revision_hash") == snapshot_root.get("current_revision_hash")
                    ),
                    public_snapshot["full_deck_revisions"][-1],
                ))
                package = current_revision.get("package")
                if package:
                    package["files"] = [
                        {
                            key: item[key] for key in (
                                "path", "artifact_id", "sha256", "size", "media_type", "origin"
                            ) if key in item
                        }
                        for item in package.get("files", [])
                    ]
                public_snapshot["full_deck_revisions"] = [current_revision]
            response.append({
                "checkpoint_id": snapshot["checkpoint_id"],
                "branch": snapshot.get("branch", "main"),
                "stage": stage,
                "source_state": snapshot["state"],
                "phase": snapshot["phase"],
                "updated_at": snapshot["updated_at"],
                "sequence": sequence[snapshot["checkpoint_id"]],
                "completed": completed,
                "snapshot": public_snapshot,
            })
        return response

    def _rewind_stage(self, snapshot: dict[str, Any], stage: str) -> dict[str, Any]:
        if stage not in STAGE_IDS:
            raise ValueError("invalid rerun stage")
        value = deepcopy(snapshot)
        value["active_job_id"] = None
        value.pop("last_tool_traces", None)
        value.pop("last_template", None)
        if stage in {"intake", "intake_clarify"}:
            value.update(
                state="intake", phase="ready_for_clarification", question_card=None,
                clarification_answers={}, clarification_history=[],
                clarification_completed=False,
                documents={"narrative_structure": [], "slide_outline": []},
                samples=[],
                current_sample_revision_hash=None,
                full_deck=None,
                full_deck_revisions=[],
            )
        elif stage == "narrative_structure":
            if not (
                value.get("clarification_completed")
                or value.get("clarification_answers")
            ):
                raise ValueError("narrative rerun requires clarification answers")
            value.update(
                state="narrative_structure", phase="ready_to_generate",
                documents={"narrative_structure": [], "slide_outline": []}, samples=[],
                current_sample_revision_hash=None,
                full_deck=None, full_deck_revisions=[],
            )
        elif stage == "slide_outline":
            narrative = self._latest_document(value, "narrative_structure")
            if not narrative or narrative.get("status") != "approved":
                raise ValueError("outline rerun requires an approved narrative")
            value["documents"]["slide_outline"] = []
            value["samples"] = []
            value["current_sample_revision_hash"] = None
            value["full_deck"] = None
            value["full_deck_revisions"] = []
            value.update(state="slide_outline", phase="ready_to_generate")
        elif stage == "ppt_sample":
            outline = self._latest_document(value, "slide_outline")
            if not outline or outline.get("status") != "approved":
                raise ValueError("sample rerun requires an approved outline")
            value["samples"] = []
            value["current_sample_revision_hash"] = None
            value["full_deck"] = None
            value["full_deck_revisions"] = []
            value.update(state="ppt_sample", phase="ready_to_generate")
        elif stage == "ppt_full":
            outline = self._latest_document(value, "slide_outline")
            current_sample_hash = value.get("current_sample_revision_hash")
            sample = next(
                (
                    item for item in value.get("samples", [])
                    if item.get("revision_hash") == current_sample_hash
                ),
                None,
            )
            root = value.get("full_deck") or {}
            references = root.get("revision_refs", [])
            if (
                not outline or outline.get("status") != "approved"
                or not sample or sample.get("status") != "approved"
                or not references
            ):
                raise ValueError("full-deck rerun requires an approved outline and sample")
            initial_hash = references[0]["revision_hash"]
            root["current_revision_hash"] = initial_hash
            root["revision_refs"] = references[:1]
            value["full_deck"] = root
            value["full_deck_revisions"] = [
                item for item in value.get("full_deck_revisions", [])
                if item.get("revision_hash") == initial_hash
            ]
            value.update(state="ppt_full", phase="ready_to_generate")
        else:
            root = value.get("full_deck") or {}
            current_revision = next(
                (
                    item for item in value.get("full_deck_revisions", [])
                    if item.get("revision_hash") == root.get("current_revision_hash")
                ),
                None,
            )
            if not current_revision or current_revision.get("status") != "approved":
                raise ValueError("acceptance rerun requires an approved full deck")
            value.update(state="acceptance", phase="ready_for_review")
        return value

    def _checkpoint(self, connection: sqlite3.Connection, checkpoint_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(checkpoint_id)
        return self._hydrate_manifest(json.loads(row["payload_json"]), connection)

    def fork(
        self,
        checkpoint_id: str,
        name: str,
        *,
        mode: str = "fork_after",
        stage: str | None = None,
        sample_revision_hash: str | None = None,
        full_deck_revision_hash: str | None = None,
    ) -> dict[str, Any]:
        if not PROJECT_ID.fullmatch(name):
            raise ValueError("invalid branch name")
        if not CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("invalid checkpoint id")
        if mode not in {"fork_after", "rerun_stage"}:
            raise ValueError("invalid branch mode")
        if mode == "rerun_stage" and stage is None:
            raise ValueError("rerun stage is required")
        if sample_revision_hash is not None and not REVISION_HASH.fullmatch(sample_revision_hash):
            raise ValueError("invalid revision hash")
        if full_deck_revision_hash is not None and not REVISION_HASH.fullmatch(
            full_deck_revision_hash
        ):
            raise ValueError("invalid revision hash")
        if sample_revision_hash is not None and full_deck_revision_hash is not None:
            raise ValueError("only one revision source may be selected")
        self._ensure_database()
        with self.lock:
            if mode == "rerun_stage":
                stage_snapshots = {item["stage"]: item["checkpoint_id"] for item in self.progress_snapshots()}
                if stage_snapshots.get(stage) != checkpoint_id:
                    raise ConflictError("stage_snapshot_required")
            with self._transaction() as connection:
                source = self._checkpoint(connection, checkpoint_id)
                current = self._hydrate_manifest(self._raw_current(connection), connection)
                snapshot = self._rewind_stage(source, stage) if mode == "rerun_stage" else deepcopy(source)
                if sample_revision_hash is not None:
                    source_index = next(
                        (
                            index for index, sample in enumerate(snapshot.get("samples", []))
                            if sample["revision_hash"] == sample_revision_hash
                        ),
                        None,
                    )
                    if source_index is None:
                        raise FileNotFoundError(sample_revision_hash)
                    snapshot["samples"] = snapshot["samples"][: source_index + 1]
                    if (
                        snapshot.get("full_deck")
                        and snapshot["full_deck"].get("approved_sample_revision_hash")
                        != sample_revision_hash
                    ):
                        mark_full_deck_stale(snapshot)
                    snapshot["current_sample_revision_hash"] = sample_revision_hash
                    snapshot.update(state="ppt_sample", phase="waiting_human_approval")
                if full_deck_revision_hash is not None:
                    full_deck = snapshot.get("full_deck") or {}
                    selected = next(
                        (
                            revision
                            for revision in snapshot.get("full_deck_revisions", [])
                            if revision.get("revision_hash") == full_deck_revision_hash
                        ),
                        None,
                    )
                    if (
                        selected is None
                        or full_deck_revision_hash
                        not in {
                            reference.get("revision_hash")
                            for reference in full_deck.get("revision_refs", [])
                        }
                    ):
                        raise FileNotFoundError(full_deck_revision_hash)
                    full_deck["current_revision_hash"] = full_deck_revision_hash
                    snapshot["full_deck"] = full_deck
                    snapshot.update(
                        state="ppt_full",
                        phase=full_deck_phase(selected),
                    )
                branches = current.get("branches", {current.get("branch", "main"): current["checkpoint_id"]})
                if name in branches:
                    raise ConflictError("branch already exists")
                next_checkpoint = "checkpoint_" + uuid4().hex[:24]
                parent = source.get("branch", current.get("branch", "main"))
                branch_meta = deepcopy(current.get("branch_meta", {}))
                branch_meta.setdefault(
                    "main", {"parent": None, "from_checkpoint": None, "created_at": current.get("created_at")}
                )
                branch_meta[name] = {
                    "parent": parent, "from_checkpoint": checkpoint_id, "created_at": utc_now(),
                    "mode": mode, "source_stage": stage,
                    "source_sample_revision_hash": sample_revision_hash,
                    "source_full_deck_revision_hash": full_deck_revision_hash,
                }
                snapshot["branches"] = {**branches, name: next_checkpoint}
                snapshot["branch_meta"] = branch_meta
                snapshot["branch"] = name
                snapshot["checkpoint_id"] = next_checkpoint
                snapshot["updated_at"] = utc_now()
                payload, artifacts, projection = self._externalize_manifest(snapshot)
                self._commit_raw(
                    connection, payload, projection, artifacts, "branch_created",
                    {
                        "branch": name, "parent": parent, "from_checkpoint": checkpoint_id,
                        "mode": mode, "source_stage": stage,
                        "sample_revision_hash": sample_revision_hash,
                        "full_deck_revision_hash": full_deck_revision_hash,
                    },
                    checkpoint_id,
                )
                return self._hydrate_manifest(payload, connection)

    def branches_view(self) -> dict[str, Any]:
        manifest = self.read(latest_sample_only=True, include_sample_html=False)
        checkpoints = self.checkpoints()
        branches = manifest.get("branches", {manifest.get("branch", "main"): manifest["checkpoint_id"]})
        metadata = manifest.get("branch_meta", {})
        items = []
        for name, head_checkpoint in branches.items():
            branch_checkpoints = [item for item in checkpoints if item["branch"] == name]
            meta = metadata.get(name, {})
            items.append({
                "name": name,
                "current": name == manifest.get("branch", "main"),
                "head_checkpoint_id": head_checkpoint,
                "parent": meta.get("parent"),
                "from_checkpoint": meta.get("from_checkpoint"),
                "mode": meta.get("mode", "fork_after"),
                "source_stage": meta.get("source_stage"),
                "created_at": meta.get("created_at"),
                "checkpoints": branch_checkpoints,
            })
        items.sort(key=lambda item: (not item["current"], item["created_at"] or ""))
        return {
            "current": manifest.get("branch", "main"),
            "current_checkpoint_id": manifest["checkpoint_id"],
            "branches": branches,
            "checkpoints": checkpoints,
            "items": items,
        }

    def switch_branch(self, checkpoint_id: str) -> dict[str, Any]:
        if not CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("invalid checkpoint id")
        self._ensure_database()
        with self.lock, self._transaction() as connection:
            current = self._hydrate_manifest(self._raw_current(connection), connection)
            branches = current.get("branches", {current.get("branch", "main"): current["checkpoint_id"]})
            matches = [name for name, head in branches.items() if head == checkpoint_id]
            if len(matches) != 1:
                raise ConflictError("branch_head_required")
            target_branch = matches[0]
            snapshot = self._checkpoint(connection, checkpoint_id)
            snapshot["branches"] = branches
            snapshot["branch_meta"] = deepcopy(current.get("branch_meta", {}))
            snapshot["branch"] = target_branch
            snapshot["updated_at"] = utc_now()
            payload, artifacts, projection = self._externalize_manifest(snapshot)
            self._insert_artifacts(connection, artifacts)
            self._sync_revisions(connection, projection, payload["checkpoint_id"])
            self._sync_branches(connection, payload)
            self._write_project_state(connection, payload)
            connection.execute(
                "INSERT INTO events(at, event, checkpoint_id, details_json) VALUES(?, ?, ?, ?)",
                (utc_now(), "branch_switched", checkpoint_id, json_text({"branch": target_branch})),
            )
            return self._hydrate_manifest(payload, connection)

    def sample_history(self) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._hydrate_manifest(
                self._raw_current(connection), connection,
                latest_sample_only=True, include_sample_html=False,
            )
            samples = manifest.get("samples", [])
            current_hash = manifest.get("current_sample_revision_hash")
            if current_hash is None and samples:
                current_hash = samples[-1]["revision_hash"]
            result = []
            for sample in reversed(samples):
                row = connection.execute(
                    "SELECT checkpoint_id FROM sample_revisions WHERE revision_hash = ?",
                    (sample["revision_hash"],),
                ).fetchone()
                result.append({
                    "sample_id": sample.get("sample_id", "sample_ppt"),
                    "revision": sample["revision"],
                    "revision_hash": sample["revision_hash"],
                    "parent_revision_hash": sample.get("parent_revision_hash"),
                    "feedback": sample.get("feedback"),
                    "status": sample["status"],
                    "created_at": sample["created_at"],
                    "source_checkpoint_id": row["checkpoint_id"] if row else None,
                    "current": sample["revision_hash"] == current_hash,
                    "pages": [
                        {"page_id": page["page_id"], "title": page["title"]}
                        for page in sample.get("pages", [])
                    ],
                    "package": {
                        "package_hash": sample["package"]["package_hash"],
                        "entrypoint": sample["package"]["entrypoint"],
                        "title": sample["package"]["title"],
                        "slide_count": sample["package"]["slide_count"],
                        "slides": sample["package"].get("slides", []),
                        "file_count": len(sample["package"].get("files", [])),
                    } if sample.get("package") else None,
                })
        return result

    def select_sample_revision(self, checkpoint_id: str, revision_hash: str) -> dict[str, Any]:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            selected = next(
                (
                    item for item in value.get("samples", [])
                    if item.get("revision_hash") == revision_hash
                ),
                None,
            )
            if selected is None:
                raise FileNotFoundError(revision_hash)
            if value.get("current_sample_revision_hash") != revision_hash:
                mark_full_deck_stale(value)
            value["current_sample_revision_hash"] = revision_hash
            value.update(
                state="ppt_sample",
                phase="completed" if selected.get("status") == "approved" else "waiting_human_approval",
            )
            return value

        return self.update(
            apply,
            "sample_revision_selected",
            {"revision_hash": revision_hash},
            expected_checkpoint_id=checkpoint_id,
        )

    def sample_package_file(
        self,
        revision_hash: str,
        logical_path: str,
    ) -> tuple[Path, str]:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        path = normalize_package_path(logical_path)
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            if revision_hash not in {
                item["revision_hash"] for item in manifest.get("samples", [])
            }:
                raise FileNotFoundError(revision_hash)
            row = connection.execute(
                """
                SELECT a.relative_path, a.sha256, a.size_bytes, f.media_type
                FROM sample_package_files f
                JOIN artifacts a ON a.artifact_id = f.artifact_id
                WHERE f.revision_hash = ? AND f.logical_path = ?
                """,
                (revision_hash, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
        artifact_path = (self.root / row["relative_path"]).resolve()
        if not self._is_package_artifact_path(artifact_path) or not artifact_path.is_file():
            raise ConflictError("package_artifact_missing")
        content = artifact_path.read_bytes()
        if (
            "sha256:" + hashlib.sha256(content).hexdigest() != row["sha256"]
            or len(content) != row["size_bytes"]
        ):
            raise ConflictError("artifact_corrupt")
        return artifact_path, row["media_type"]

    def sample_package_files(self, revision_hash: str) -> list[tuple[str, Path]]:
        sample = self.sample_revision(revision_hash)
        package = sample.get("package")
        if not package:
            raise FileNotFoundError(revision_hash)
        return [
            (item["path"], self.sample_package_file(revision_hash, item["path"])[0])
            for item in package.get("files", [])
        ]

    def sample_revision(self, revision_hash: str) -> dict[str, Any]:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            sample = next(
                (item for item in manifest.get("samples", []) if item["revision_hash"] == revision_hash),
                None,
            )
            if sample is None:
                raise FileNotFoundError(revision_hash)
            hydrated = self._hydrate_manifest({"samples": [sample]}, connection)
            return hydrated["samples"][0]

    def sample_revision_checkpoint(self, revision_hash: str) -> str:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            if revision_hash not in {item["revision_hash"] for item in manifest.get("samples", [])}:
                raise FileNotFoundError(revision_hash)
            row = connection.execute(
                "SELECT checkpoint_id FROM sample_revisions WHERE revision_hash = ?",
                (revision_hash,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(revision_hash)
            return str(row["checkpoint_id"])

    def full_deck_history(self) -> list[dict[str, Any]]:
        """Return bounded metadata for every revision on the current deck root."""

        manifest = self.read(include_sample_html=False)
        root = manifest.get("full_deck") or {}
        current_hash = root.get("current_revision_hash")
        self._ensure_database()
        with self._connect() as connection:
            checkpoints = {
                row["revision_hash"]: row["checkpoint_id"]
                for row in connection.execute(
                    """
                    SELECT revision_hash, checkpoint_id
                    FROM full_deck_revisions WHERE full_deck_id = ?
                    """,
                    (root.get("full_deck_id"),),
                ).fetchall()
            }
        result = []
        for revision in reversed(manifest.get("full_deck_revisions", [])):
            package = revision.get("package")
            changed_slot_ids = revision.get("provenance", {}).get(
                "changed_slot_ids", []
            )
            changed_set = set(changed_slot_ids)
            result.append({
                "full_deck_id": revision["full_deck_id"],
                "revision": revision["revision"],
                "revision_hash": revision["revision_hash"],
                "parent_revision_hash": revision.get("parent_revision_hash"),
                "feedback": revision.get("feedback"),
                "status": revision["status"],
                "created_at": revision["created_at"],
                "source_checkpoint_id": checkpoints.get(revision["revision_hash"]),
                "current": revision["revision_hash"] == current_hash,
                "page_count": len(revision.get("plan", {}).get("pages", [])),
                "changed_slot_ids": changed_slot_ids,
                "changed_pages": [
                    {
                        "slot_id": page["slot_id"],
                        "source_slide_number": (page.get("outline_ref") or {}).get(
                            "source_slide_number"
                        ),
                        "title": page["title"],
                    }
                    for page in revision.get("plan", {}).get("pages", [])
                    if page.get("slot_id") in changed_set
                ],
                "package": {
                    "package_hash": package["package_hash"],
                    "entrypoint": package["entrypoint"],
                    "title": package["title"],
                    "slide_count": package["slide_count"],
                    "file_count": len(package.get("files", [])),
                } if package else None,
            })
        return result

    def select_full_deck_revision(
        self,
        checkpoint_id: str,
        revision_hash: str,
    ) -> dict[str, Any]:
        """Move the deck pointer while preserving immutable revision history."""

        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            root = value.get("full_deck") or {}
            selected = next(
                (
                    revision
                    for revision in value.get("full_deck_revisions", [])
                    if revision.get("revision_hash") == revision_hash
                ),
                None,
            )
            if (
                selected is None
                or revision_hash
                not in {
                    reference.get("revision_hash")
                    for reference in root.get("revision_refs", [])
                }
            ):
                raise FileNotFoundError(revision_hash)
            root["current_revision_hash"] = revision_hash
            value["full_deck"] = root
            value.update(state="ppt_full", phase=full_deck_phase(selected))
            return value

        return self.update(
            apply,
            "full_deck_revision_selected",
            {"revision_hash": revision_hash},
            expected_checkpoint_id=checkpoint_id,
        )

    def full_deck_revision(self, revision_hash: str) -> dict[str, Any]:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        manifest = self.read(include_sample_html=False)
        revision = next(
            (
                item
                for item in manifest.get("full_deck_revisions", [])
                if item.get("revision_hash") == revision_hash
            ),
            None,
        )
        if revision is None:
            raise FileNotFoundError(revision_hash)
        return revision

    def full_deck_revision_checkpoint(self, revision_hash: str) -> str:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            if revision_hash not in {
                reference.get("revision_hash")
                for reference in (manifest.get("full_deck") or {}).get(
                    "revision_refs", []
                )
            }:
                raise FileNotFoundError(revision_hash)
            row = connection.execute(
                "SELECT checkpoint_id FROM full_deck_revisions WHERE revision_hash = ?",
                (revision_hash,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(revision_hash)
            return str(row["checkpoint_id"])

    def full_deck_package_file(
        self,
        revision_hash: str,
        logical_path: str,
    ) -> tuple[Path, str]:
        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        path = normalize_package_path(logical_path)
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            if revision_hash not in {
                reference.get("revision_hash")
                for reference in (manifest.get("full_deck") or {}).get(
                    "revision_refs", []
                )
            }:
                raise FileNotFoundError(revision_hash)
            row = connection.execute(
                """
                SELECT a.relative_path, a.sha256, a.size_bytes, f.media_type
                FROM full_deck_package_files f
                JOIN artifacts a ON a.artifact_id = f.artifact_id
                WHERE f.revision_hash = ? AND f.logical_path = ?
                """,
                (revision_hash, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
        artifact_path = (self.root / row["relative_path"]).resolve()
        if (
            not self._is_package_artifact_path(artifact_path)
            or not artifact_path.is_file()
        ):
            raise ConflictError("package_artifact_missing")
        content = artifact_path.read_bytes()
        if (
            "sha256:" + hashlib.sha256(content).hexdigest() != row["sha256"]
            or len(content) != row["size_bytes"]
        ):
            raise ConflictError("artifact_corrupt")
        return artifact_path, row["media_type"]

    def full_deck_package_files(
        self,
        revision_hash: str,
    ) -> list[tuple[str, Path]]:
        revision = self.full_deck_revision(revision_hash)
        package = revision.get("package")
        if not package:
            raise FileNotFoundError(revision_hash)
        return [
            (
                item["path"],
                self.full_deck_package_file(revision_hash, item["path"])[0],
            )
            for item in package.get("files", [])
        ]

    def full_deck_package_contents(
        self,
        revision_hash: str,
    ) -> list[dict[str, Any]]:
        """Load and verify one immutable full-deck package with one database read."""

        if not REVISION_HASH.fullmatch(revision_hash):
            raise ValueError("invalid revision hash")
        self._ensure_database()
        with self._connect() as connection:
            manifest = self._raw_current(connection)
            if revision_hash not in {
                reference.get("revision_hash")
                for reference in (manifest.get("full_deck") or {}).get(
                    "revision_refs", []
                )
            }:
                raise FileNotFoundError(revision_hash)
            rows = connection.execute(
                """
                SELECT f.logical_path, f.media_type, f.origin,
                       a.relative_path, a.sha256, a.size_bytes
                FROM full_deck_package_files f
                JOIN artifacts a ON a.artifact_id = f.artifact_id
                WHERE f.revision_hash = ?
                ORDER BY f.file_index
                """,
                (revision_hash,),
            ).fetchall()
        if not rows:
            raise FileNotFoundError(revision_hash)
        result: list[dict[str, Any]] = []
        for row in rows:
            path = (self.root / row["relative_path"]).resolve()
            if (
                not self._is_package_artifact_path(path)
                or not path.is_file()
            ):
                raise ConflictError("package_artifact_missing")
            content = path.read_bytes()
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

def list_projects(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    project_ids = {
        path.parent.name for path in root.glob("*/project.db")
    } | {
        path.parent.name for path in root.glob("*/manifest.json")
    }
    result = []
    for project_id in project_ids:
        try:
            payload = ProjectStore(root, project_id).read(
                latest_sample_only=True, include_sample_html=False
            )
            result.append({
                key: payload.get(key)
                for key in ("project_id", "title", "state", "phase", "updated_at")
            })
        except (OSError, sqlite3.DatabaseError, json.JSONDecodeError, ConflictError, ValueError):
            continue
    return sorted(result, key=lambda item: item.get("updated_at") or "", reverse=True)
