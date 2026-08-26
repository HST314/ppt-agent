from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from pydantic import ValidationError

from agent_core.models import utc_now
from agent_core.processes import process_is_alive


class ActiveJobError(RuntimeError):
    pass


def public_job_error(exc: Exception) -> dict[str, str]:
    """Map internal failures to a bounded browser-safe error contract."""

    public_code = getattr(exc, "public_code", None)
    public_message = getattr(exc, "public_message", None)
    if isinstance(public_code, str) and isinstance(public_message, str):
        return {"code": public_code[:80], "message": public_message[:96]}
    if isinstance(exc, ValidationError):
        return {"code": "invalid_model_output", "message": "模型返回的内容格式不正确，请重试。"}
    return {"code": "job_failed", "message": "任务暂未完成，请重试。"}


JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT,
    owner_pid INTEGER,
    cancellable INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    request_key TEXT,
    session_id TEXT,
    progress_json TEXT
);
CREATE INDEX IF NOT EXISTS jobs_project_time_idx ON jobs(project_id, created_at, job_id);
CREATE INDEX IF NOT EXISTS jobs_dedup_idx ON jobs(project_id, operation, checkpoint_id, status);
CREATE TABLE IF NOT EXISTS job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    operation TEXT NOT NULL,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events(job_id, event_id);
"""


class JobRegistry:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "jobs.db"
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ppt-agent")
        self.lock = threading.RLock()
        self.futures: dict[str, Any] = {}
        self._initialize()
        self._recover()

    def _connect(self) -> sqlite3.Connection:
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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(JOB_SCHEMA)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "owner_pid" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN owner_pid INTEGER")
            if "cancellable" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN cancellable INTEGER NOT NULL DEFAULT 0"
                )
            if "cancel_requested" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "request_key" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN request_key TEXT")
            if "session_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN session_id TEXT")
            if "progress_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_request_key_idx "
                "ON jobs(project_id, request_key, status)"
            )
            connection.commit()
        finally:
            connection.close()
        self._migrate_legacy_files()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("owner_pid", None)
        item["cancellable"] = bool(item.get("cancellable"))
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        item["error"] = json.loads(item.pop("error_json")) if item.get("error_json") else None
        progress_json = item.pop("progress_json", None)
        item["progress"] = json.loads(progress_json) if progress_json else None
        return item

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO job_events(job_id, at, status, operation, error_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                record["job_id"], utc_now(), record["status"], record["operation"],
                json.dumps(record.get("error"), ensure_ascii=False) if record.get("error") else None,
            ),
        )

    @staticmethod
    def _upsert(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, project_id, operation, checkpoint_id, status,
                created_at, started_at, finished_at, error_json, owner_pid,
                cancellable, cancel_requested, request_key, session_id, progress_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                error_json = excluded.error_json,
                owner_pid = excluded.owner_pid,
                cancellable = excluded.cancellable,
                cancel_requested = MAX(jobs.cancel_requested, excluded.cancel_requested),
                request_key = COALESCE(excluded.request_key, jobs.request_key),
                session_id = COALESCE(excluded.session_id, jobs.session_id),
                progress_json = COALESCE(excluded.progress_json, jobs.progress_json)
            """,
            (
                record["job_id"], record["project_id"], record["operation"],
                record["checkpoint_id"], record["status"], record["created_at"],
                record.get("started_at"), record.get("finished_at"),
                json.dumps(record.get("error"), ensure_ascii=False) if record.get("error") else None,
                record.get("_owner_pid"),
                int(bool(record.get("cancellable"))),
                int(bool(record.get("cancel_requested"))),
                record.get("request_key"),
                record.get("session_id"),
                json.dumps(record.get("progress"), ensure_ascii=False)
                if record.get("progress") is not None
                else None,
            ),
        )

    def _migrate_legacy_files(self) -> None:
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
                return
            for path in self.root.glob("job_*.json"):
                if path.name.endswith(".events.jsonl"):
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    self._upsert(connection, record)
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
                events_path = self.root / f"{record['job_id']}.events.jsonl"
                if not events_path.is_file():
                    self._insert_event(connection, record)
                    continue
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    connection.execute(
                        """
                        INSERT INTO job_events(job_id, at, status, operation, error_json)
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (
                            record["job_id"], event.get("at", utc_now()),
                            event.get("status", record["status"]),
                            event.get("operation", record["operation"]),
                            json.dumps(event.get("error"), ensure_ascii=False) if event.get("error") else None,
                        ),
                    )

    def _recover(self) -> None:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                record = self._record(row)
                if process_is_alive(row["owner_pid"]):
                    continue
                if record.get("cancellable") and record.get("cancel_requested"):
                    record.update(status="cancelled", finished_at=utc_now(), error=None)
                else:
                    record.update(
                        status="failed",
                        finished_at=utc_now(),
                        error={
                            "code": "process_restarted",
                            "message": "服务重启，请从上一成功点重试。",
                        },
                    )
                self._upsert(connection, record)
                self._insert_event(connection, record)

    @contextmanager
    def project_guard(self, project_id: str) -> Iterator[dict[str, Any] | None]:
        """Serialize job submission with branch mutations across worker processes."""

        with self.lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            yield self._record(row) if row else None

    def submit(
        self,
        project_id: str,
        operation: str,
        checkpoint_id: str,
        action: Callable[..., Any],
        *,
        cancellable: bool = False,
        idempotency_key: str | None = None,
        session_id: str | None = None,
        initial_progress: dict[str, Any] | None = None,
        progress_reporting: bool = False,
    ) -> dict[str, Any]:
        request_key = idempotency_key or f"{operation}\n{checkpoint_id}"
        with self.lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ?
                  AND (
                    request_key = ?
                    OR (request_key IS NULL AND operation = ? AND checkpoint_id = ?)
                  )
                  AND status IN ('queued', 'running', 'succeeded')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id, request_key, operation, checkpoint_id),
            ).fetchone()
            if row:
                return self._record(row)
            active = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE project_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active:
                raise ActiveJobError(
                    "active_job:请等待当前任务结束后再启动新的生成操作。"
                )
            record = {
                "job_id": "job_" + uuid4().hex,
                "project_id": project_id,
                "operation": operation,
                "checkpoint_id": checkpoint_id,
                "status": "queued",
                "created_at": utc_now(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "_owner_pid": os.getpid(),
                "cancellable": cancellable,
                "cancel_requested": False,
                "request_key": request_key,
                "session_id": session_id,
                "progress": self._validated_progress(initial_progress),
            }
            self._upsert(connection, record)
            self._insert_event(connection, record)
        self.futures[record["job_id"]] = self.pool.submit(
            self._run,
            record,
            action,
            progress_reporting,
        )
        return {key: value for key, value in record.items() if not key.startswith("_")}

    @staticmethod
    def _validated_progress(progress: dict[str, Any] | None) -> dict[str, Any] | None:
        if progress is None:
            return None
        if not isinstance(progress, dict):
            raise ValueError("job progress must be an object")
        encoded = json.dumps(progress, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 32_768:
            raise ValueError("job progress exceeds the storage limit")
        return json.loads(encoded)

    def update_progress(self, job_id: str, progress: dict[str, Any]) -> dict[str, Any]:
        """Persist a bounded resumable-operation summary without adding event noise."""

        value = self._validated_progress(progress)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET progress_json = ? WHERE job_id = ?",
                (json.dumps(value, ensure_ascii=False), job_id),
            )
            if cursor.rowcount != 1:
                raise FileNotFoundError(job_id)
        return self.get(job_id)

    def _write(self, record: dict[str, Any]) -> None:
        with self._transaction() as connection:
            self._upsert(connection, record)
            self._insert_event(connection, record)

    def _cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def _run(
        self,
        record: dict[str, Any],
        action: Callable[..., Any],
        progress_reporting: bool,
    ) -> None:
        record.update(status="running", started_at=utc_now())
        self._write(record)
        try:
            arguments: list[Callable[..., Any]] = []
            if record.get("cancellable"):
                arguments.append(lambda: self._cancel_requested(record["job_id"]))
            if progress_reporting:
                def report_progress(progress: dict[str, Any]) -> None:
                    record["progress"] = self._validated_progress(progress)
                    self.update_progress(record["job_id"], progress)

                arguments.append(report_progress)
            action(*arguments)
            record["status"] = "succeeded"
        except JobCancelled:
            record.update(
                status="cancelled",
                cancel_requested=True,
                error=None,
            )
        except Exception as exc:
            record.update(status="failed", error=public_job_error(exc))
        record["cancel_requested"] = self._cancel_requested(record["job_id"])
        record["finished_at"] = utc_now()
        self._write(record)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(job_id)
        return self._record(row)

    def latest_for_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs WHERE project_id = ?
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return self._record(row) if row else None

    def find_for_request(
        self,
        project_id: str,
        request_key: str,
    ) -> dict[str, Any] | None:
        """Return a durable idempotent result before re-validating mutable state."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ? AND request_key = ?
                  AND status IN ('queued', 'running', 'succeeded')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id, request_key),
            ).fetchone()
        return self._record(row) if row else None

    def find_for_request_prefix(
        self,
        project_id: str,
        request_key_prefix: str,
    ) -> dict[str, Any] | None:
        """Find a versioned request whose immutable target suffix is now historical."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ?
                  AND substr(request_key, 1, length(?)) = ?
                  AND status IN ('queued', 'running', 'succeeded')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id, request_key_prefix, request_key_prefix),
            ).fetchone()
        return self._record(row) if row else None

    def active_for_session(
        self,
        project_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ? AND session_id = ?
                  AND status IN ('queued', 'running')
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (project_id, session_id),
            ).fetchone()
        return self._record(row) if row else None

    def list_for_project(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("job limit out of range")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE project_id = ?
                ORDER BY created_at DESC, job_id DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def events_for_project(self, project_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if limit < 1 or limit > 2000:
            raise ValueError("job event limit out of range")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.event_id, e.job_id, e.at, e.status, e.operation, e.error_json
                FROM job_events e
                JOIN jobs j ON j.job_id = e.job_id
                WHERE j.project_id = ?
                ORDER BY e.event_id DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [{
            "event_id": row["event_id"],
            "job_id": row["job_id"],
            "at": row["at"],
            "status": row["status"],
            "operation": row["operation"],
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        } for row in rows]

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        if record["status"] not in {"queued", "running"}:
            return record
        future = self.futures.get(job_id)
        if future and future.cancel():
            record.update(
                status="cancelled",
                finished_at=utc_now(),
                error=None,
                cancel_requested=True,
            )
            self._write(record)
            return record
        if not record.get("cancellable"):
            raise RuntimeError("running jobs cannot be interrupted safely")
        with self._transaction() as connection:
            connection.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?",
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events(job_id, at, status, operation, error_json)
                VALUES(?, ?, 'cancellation_requested', ?, NULL)
                """,
                (job_id, utc_now(), record["operation"]),
            )
        return self.get(job_id)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        self.get(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT at, status, operation, error_json
                FROM job_events WHERE job_id = ? ORDER BY event_id
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "at": row["at"],
                "status": row["status"],
                "operation": row["operation"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
            }
            for row in rows
        ]

    def shutdown(self, *, wait: bool = True) -> None:
        """Release worker threads after queued jobs have finished."""

        self.pool.shutdown(wait=wait)
        self.futures.clear()


class JobCancelled(RuntimeError):
    """A cooperative job stopped before publishing any project mutation."""
