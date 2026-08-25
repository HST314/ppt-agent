from __future__ import annotations

import hashlib
import json
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
from storage.persistence import atomic_bytes, json_text
from storage.prompt_audit import PromptAuditMixin
from storage.sqlite_schema import PROJECT_SCHEMA


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
CHECKPOINT_ID = re.compile(r"^checkpoint_[a-f0-9]{24}$")
REVISION_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
ARTIFACT_ID = REVISION_HASH
STAGE_IDS = ("intake", "intake_clarify", "narrative_structure", "slide_outline", "ppt_sample")
SCHEMA_VERSION = 2


class ConflictError(RuntimeError):
    pass


class ProjectStore(PromptAuditMixin):
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: Path, project_id: str):
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project id")
        self.projects_root = root.resolve()
        self.project_id = project_id
        self.root = (root / project_id).resolve()
        if not self.root.is_relative_to(self.projects_root):
            raise ValueError("invalid project root")
        self.database_path = self.root / "project.db"
        self.manifest_path = self.root / "manifest.json"
        self.artifacts_root = self.root / "artifacts" / "html"
        self.lock = self._locks.setdefault(str(self.root), threading.RLock())

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
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
        with self.lock:
            connection = self._connect()
            try:
                connection.executescript(PROJECT_SCHEMA)
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.commit()
            finally:
                connection.close()
            if self.manifest_path.is_file():
                self._migrate_legacy_files()

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
        relative_path = f"artifacts/html/{hexadecimal}.html"
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

    def _externalize_manifest(
        self,
        manifest: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        projection = deepcopy(manifest)
        projection["format_version"] = SCHEMA_VERSION
        projection["storage"] = {
            "engine": "sqlite-wal",
            "database": "project.db",
            "artifacts": "artifacts/html",
        }
        artifacts: dict[str, dict[str, Any]] = {}
        for sample in projection.get("samples", []):
            for page in sample.get("pages", []):
                html = page.pop("html", None)
                if html is not None:
                    record = self._artifact_record(html.encode("utf-8"))
                    artifacts[record["artifact_id"]] = record
                    page.update({
                        "artifact_id": record["artifact_id"],
                        "sha256": record["sha256"],
                        "size": record["size_bytes"],
                        "sanitizer_version": record["sanitizer_version"],
                    })
                elif not ARTIFACT_ID.fullmatch(str(page.get("artifact_id", ""))):
                    raise ConflictError("sample_artifact_missing")
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
            if "pages" in sample:
                expanded_samples.append(sample)
                continue
            row = connection.execute(
                "SELECT * FROM sample_revisions WHERE revision_hash = ?",
                (sample.get("revision_hash"),),
            ).fetchone()
            if row is None:
                raise ConflictError("sample_revision_missing")
            pages = connection.execute(
                """
                SELECT page_id, title, artifact_id, sha256, size_bytes, sanitizer_version
                FROM sample_pages WHERE revision_hash = ? ORDER BY page_index
                """,
                (row["revision_hash"],),
            ).fetchall()
            expanded_samples.append({
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
            })
        value["samples"] = expanded_samples
        samples = value.get("samples", [])
        targets = (samples[-1:] if latest_sample_only else samples) if include_sample_html else []
        for sample in targets:
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
                if not path.is_relative_to(self.artifacts_root.resolve()) or not path.is_file():
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
        event: str,
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
        connection.execute(
            "INSERT INTO events(at, event, checkpoint_id, details_json) VALUES(?, ?, ?, ?)",
            (utc_now(), event, payload["checkpoint_id"], json_text(details)),
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
                "question_card": None,
                "documents": {"narrative_structure": [], "slide_outline": []},
                "samples": [],
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
                connection, payload, projection, artifacts, event, details or {}, parent_checkpoint_id,
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

    def events(self) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT at, event, checkpoint_id, details_json FROM events ORDER BY event_id"
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
        sample = (manifest.get("samples") or [None])[-1]
        if sample and sample.get("status") == "approved":
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
                    and bool(item.get("clarification_answers"))
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
            matches = [
                item for item in lineage
                if item.get("state") == "ppt_sample"
                and item.get("phase") == "completed"
                and ((item.get("samples") or [None])[-1] or {}).get("status") == "approved"
            ]
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
                current_sample = deepcopy(public_snapshot["samples"][-1])
                current_sample["pages"] = [
                    {"page_id": page["page_id"], "title": page["title"]}
                    for page in current_sample.get("pages", [])
                ]
                public_snapshot["samples"] = [current_sample]
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
                clarification_answers={}, documents={"narrative_structure": [], "slide_outline": []},
                samples=[],
            )
        elif stage == "narrative_structure":
            if not value.get("clarification_answers"):
                raise ValueError("narrative rerun requires clarification answers")
            value.update(
                state="narrative_structure", phase="ready_to_generate",
                documents={"narrative_structure": [], "slide_outline": []}, samples=[],
            )
        elif stage == "slide_outline":
            narrative = self._latest_document(value, "narrative_structure")
            if not narrative or narrative.get("status") != "approved":
                raise ValueError("outline rerun requires an approved narrative")
            value["documents"]["slide_outline"] = []
            value["samples"] = []
            value.update(state="slide_outline", phase="ready_to_generate")
        else:
            outline = self._latest_document(value, "slide_outline")
            if not outline or outline.get("status") != "approved":
                raise ValueError("sample rerun requires an approved outline")
            value["samples"] = []
            value.update(state="ppt_sample", phase="ready_to_generate")
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
                    snapshot.update(state="ppt_sample", phase="waiting_human_approval")
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
            current_hash = samples[-1]["revision_hash"] if samples else None
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
                })
        return result

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
