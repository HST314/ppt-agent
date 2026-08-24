from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent_core.models import utc_now
from storage.project_store import atomic_json


class JobRegistry:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ppt-agent")
        self.lock = threading.RLock()
        self.futures: dict[str, Any] = {}
        self._recover()

    def _recover(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") in {"queued", "running"}:
                    record.update(status="failed", finished_at=utc_now(), error={"code": "process_restarted", "message": "服务重启，请从上一成功点重试。"})
                    atomic_json(path, record)
            except (OSError, json.JSONDecodeError):
                continue

    def submit(self, project_id: str, operation: str, checkpoint_id: str, action: Callable[[], Any]) -> dict[str, Any]:
        with self.lock:
            for path in self.root.glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("project_id") == project_id and record.get("operation") == operation and record.get("checkpoint_id") == checkpoint_id and record.get("status") in {"queued", "running", "succeeded"}:
                    return record
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
            }
            self._write(record)
            self.futures[record["job_id"]] = self.pool.submit(self._run, record, action)
            return record

    def _run(self, record: dict[str, Any], action: Callable[[], Any]) -> None:
        record.update(status="running", started_at=utc_now())
        self._write(record)
        try:
            action()
            record["status"] = "succeeded"
        except Exception as exc:  # error details stay server-side and are normalized
            record.update(status="failed", error={"code": type(exc).__name__, "message": str(exc)[:500]})
        record["finished_at"] = utc_now()
        self._write(record)

    def get(self, job_id: str) -> dict[str, Any]:
        path = self.root / f"{job_id}.json"
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def latest_for_project(self, project_id: str) -> dict[str, Any] | None:
        records = []
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("project_id") == project_id:
                    records.append(record)
            except (OSError, json.JSONDecodeError):
                continue
        return max(records, key=lambda item: item.get("created_at", ""), default=None)

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        if record["status"] not in {"queued", "running"}:
            return record
        future = self.futures.get(job_id)
        if not future or not future.cancel():
            raise RuntimeError("running jobs cannot be interrupted safely")
        record.update(status="cancelled", finished_at=utc_now(), error=None)
        self._write(record)
        return record

    def events(self, job_id: str) -> list[dict[str, Any]]:
        self.get(job_id)
        path = self.root / f"{job_id}.events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _write(self, record: dict[str, Any]) -> None:
        atomic_json(self.root / f"{record['job_id']}.json", record)
        event = json.dumps({"at": utc_now(), "status": record["status"], "operation": record["operation"], "error": record.get("error")}, ensure_ascii=False)
        with (self.root / f"{record['job_id']}.events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(event + "\n")
