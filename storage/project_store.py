from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent_core.models import utc_now


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")


class ConflictError(RuntimeError):
    pass


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ProjectStore:
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: Path, project_id: str):
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project id")
        self.project_id = project_id
        self.root = (root / project_id).resolve()
        if not self.root.is_relative_to(root.resolve()):
            raise ValueError("invalid project root")
        self.manifest_path = self.root / "manifest.json"
        self.lock = self._locks.setdefault(str(self.root), threading.RLock())

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    def read(self) -> dict[str, Any]:
        if not self.exists():
            raise FileNotFoundError(self.project_id)
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def create(self, task: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.exists():
                raise ConflictError("project already exists")
            checkpoint = "checkpoint_" + uuid4().hex[:24]
            manifest = {
                "project_id": self.project_id,
                "title": task["title"],
                "branch": "main",
                "branches": {"main": checkpoint},
                "state": "intake",
                "phase": "ready_for_clarification",
                "task_card": task,
                "clarification_answers": {},
                "question_card": None,
                "documents": {"narrative_structure": [], "slide_outline": []},
                "checkpoint_id": checkpoint,
                "active_job_id": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "runtime": runtime,
            }
            self._commit(manifest, "project_created", {"checkpoint_id": checkpoint})
            return manifest

    def update(
        self,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
        event: str,
        details: dict[str, Any] | None = None,
        *,
        expected_checkpoint_id: str,
    ) -> dict[str, Any]:
        with self.lock:
            manifest = self.read()
            if manifest["checkpoint_id"] != expected_checkpoint_id:
                raise ConflictError("stale_revision")
            candidate = deepcopy(manifest)
            candidate["checkpoint_id"] = "checkpoint_" + uuid4().hex[:24]
            updated = transform(candidate)
            updated["updated_at"] = utc_now()
            updated.setdefault("branches", {})[updated.get("branch", "main")] = updated["checkpoint_id"]
            self._commit(updated, event, details or {})
            return updated

    def _commit(self, manifest: dict[str, Any], event: str, details: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.manifest_path, manifest)
        checkpoints = self.root / "checkpoints"
        atomic_json(checkpoints / f"{manifest['checkpoint_id']}.json", manifest)
        line = json.dumps({"at": utc_now(), "event": event, "checkpoint_id": manifest["checkpoint_id"], **details}, ensure_ascii=False)
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def events(self) -> list[dict[str, Any]]:
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def checkpoints(self) -> list[dict[str, Any]]:
        result = []
        for path in (self.root / "checkpoints").glob("checkpoint_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result.append({
                    "checkpoint_id": payload["checkpoint_id"],
                    "state": payload["state"],
                    "phase": payload["phase"],
                    "updated_at": payload["updated_at"],
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def fork(self, checkpoint_id: str, name: str) -> dict[str, Any]:
        if not PROJECT_ID.fullmatch(name):
            raise ValueError("invalid branch name")
        snapshot_path = self.root / "checkpoints" / f"{checkpoint_id}.json"
        if not snapshot_path.is_file():
            raise FileNotFoundError(checkpoint_id)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        current = self.read()
        branches = current.get("branches", {current.get("branch", "main"): current["checkpoint_id"]})
        if name in branches:
            raise ConflictError("branch already exists")
        next_checkpoint = "checkpoint_" + uuid4().hex[:24]
        snapshot["branches"] = {**branches, name: next_checkpoint}
        snapshot["branch"] = name
        snapshot["checkpoint_id"] = next_checkpoint
        snapshot["updated_at"] = utc_now()
        self._commit(snapshot, "branch_created", {"branch": name, "from_checkpoint": checkpoint_id})
        return snapshot


def list_projects(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result = []
    for manifest in root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            result.append({key: payload.get(key) for key in ("project_id", "title", "state", "phase", "updated_at")})
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda item: item.get("updated_at") or "", reverse=True)
