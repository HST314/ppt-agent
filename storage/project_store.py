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
CHECKPOINT_ID = re.compile(r"^checkpoint_[a-f0-9]{24}$")
STAGE_IDS = ("intake", "intake_clarify", "narrative_structure", "slide_outline")


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
                "branch_meta": {"main": {"parent": None, "from_checkpoint": None, "created_at": utc_now()}},
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
        self._append_event(event, manifest["checkpoint_id"], details)

    def _append_event(self, event: str, checkpoint_id: str, details: dict[str, Any]) -> None:
        line = json.dumps({"at": utc_now(), "event": event, "checkpoint_id": checkpoint_id, **details}, ensure_ascii=False)
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
                    "branch": payload.get("branch", "main"),
                    "state": payload["state"],
                    "phase": payload["phase"],
                    "updated_at": payload["updated_at"],
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def _checkpoint_lineage(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        """Load immutable checkpoints along the active branch's ancestry."""

        payloads: list[dict[str, Any]] = []
        for path in (self.root / "checkpoints").glob("checkpoint_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("checkpoint_id") != path.stem:
                    continue
                payloads.append(payload)
            except (OSError, json.JSONDecodeError):
                continue
        by_branch: dict[str, list[dict[str, Any]]] = {}
        for payload in payloads:
            by_branch.setdefault(payload.get("branch", "main"), []).append(payload)
        for items in by_branch.values():
            items.sort(key=lambda item: (item.get("updated_at") or "", item["checkpoint_id"]))

        branch_meta = manifest.get("branch_meta", {})

        def walk(branch: str, head_id: str, visiting: set[str]) -> list[dict[str, Any]]:
            if branch in visiting:
                raise ConflictError("branch_lineage_corrupt")
            visiting = {*visiting, branch}
            meta = branch_meta.get(branch, {})
            result: list[dict[str, Any]] = []
            parent = meta.get("parent")
            fork_id = meta.get("from_checkpoint")
            if parent and fork_id:
                result.extend(walk(parent, fork_id, visiting))

            items = by_branch.get(branch, [])
            head_index = next(
                (index for index, item in enumerate(items) if item["checkpoint_id"] == head_id),
                None,
            )
            if head_index is None:
                raise ConflictError("branch_head_missing")
            result.extend(items[: head_index + 1])
            return result

        return walk(manifest.get("branch", "main"), manifest["checkpoint_id"], set())

    @staticmethod
    def _latest_document(snapshot: dict[str, Any], document_type: str) -> dict[str, Any] | None:
        history = snapshot.get("documents", {}).get(document_type, [])
        return history[-1] if history else None

    def progress_snapshots(self) -> list[dict[str, Any]]:
        """Return one canonical, immutable snapshot for every reachable stage."""

        with self.lock:
            manifest = self.read()
            lineage = self._checkpoint_lineage(manifest)
        if not lineage:
            return []

        active_index = STAGE_IDS.index(manifest["state"])
        outline = self._latest_document(manifest, "slide_outline")
        completed_through = 3 if outline and outline.get("status") == "approved" else active_index - 1

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
            matches = [
                item for item in lineage
                if item.get("state") == "slide_outline"
                and item.get("phase") == "completed"
                and (self._latest_document(item, stage) or {}).get("status") == "approved"
            ]
            return matches[-1] if matches else None

        sequence = {item["checkpoint_id"]: index for index, item in enumerate(lineage, 1)}
        response = []
        for index, stage in enumerate(STAGE_IDS):
            completed = index <= completed_through
            if completed:
                snapshot = boundary(stage)
            elif index == active_index:
                snapshot = lineage[-1]
            else:
                snapshot = None
            if snapshot is None:
                continue
            response.append({
                "checkpoint_id": snapshot["checkpoint_id"],
                "branch": snapshot.get("branch", "main"),
                "stage": stage,
                "source_state": snapshot["state"],
                "phase": snapshot["phase"],
                "updated_at": snapshot["updated_at"],
                "sequence": sequence[snapshot["checkpoint_id"]],
                "completed": completed,
                "snapshot": deepcopy(snapshot),
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
                state="intake",
                phase="ready_for_clarification",
                question_card=None,
                clarification_answers={},
                documents={"narrative_structure": [], "slide_outline": []},
            )
        elif stage == "narrative_structure":
            if not value.get("clarification_answers"):
                raise ValueError("narrative rerun requires clarification answers")
            value.update(
                state="narrative_structure",
                phase="ready_to_generate",
                documents={"narrative_structure": [], "slide_outline": []},
            )
        else:
            narrative = self._latest_document(value, "narrative_structure")
            if not narrative or narrative.get("status") != "approved":
                raise ValueError("outline rerun requires an approved narrative")
            value["documents"]["slide_outline"] = []
            value.update(state="slide_outline", phase="ready_to_generate")
        return value

    def fork(
        self,
        checkpoint_id: str,
        name: str,
        *,
        mode: str = "fork_after",
        stage: str | None = None,
    ) -> dict[str, Any]:
        if not PROJECT_ID.fullmatch(name):
            raise ValueError("invalid branch name")
        if not CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("invalid checkpoint id")
        with self.lock:
            snapshot_path = self.root / "checkpoints" / f"{checkpoint_id}.json"
            if not snapshot_path.is_file():
                raise FileNotFoundError(checkpoint_id)
            source = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if mode not in {"fork_after", "rerun_stage"}:
                raise ValueError("invalid branch mode")
            if mode == "rerun_stage" and stage is None:
                raise ValueError("rerun stage is required")
            current = self.read()
            if mode == "rerun_stage":
                stage_snapshots = {
                    item["stage"]: item["checkpoint_id"]
                    for item in self.progress_snapshots()
                }
                if stage_snapshots.get(stage) != checkpoint_id:
                    raise ConflictError("stage_snapshot_required")
            snapshot = self._rewind_stage(source, stage) if mode == "rerun_stage" else source
            branches = current.get("branches", {current.get("branch", "main"): current["checkpoint_id"]})
            if name in branches:
                raise ConflictError("branch already exists")
            next_checkpoint = "checkpoint_" + uuid4().hex[:24]
            parent = source.get("branch", current.get("branch", "main"))
            branch_meta = deepcopy(current.get("branch_meta", {}))
            branch_meta.setdefault("main", {"parent": None, "from_checkpoint": None, "created_at": current.get("created_at")})
            branch_meta[name] = {
                "parent": parent,
                "from_checkpoint": checkpoint_id,
                "created_at": utc_now(),
                "mode": mode,
                "source_stage": stage,
            }
            snapshot["branches"] = {**branches, name: next_checkpoint}
            snapshot["branch_meta"] = branch_meta
            snapshot["branch"] = name
            snapshot["checkpoint_id"] = next_checkpoint
            snapshot["updated_at"] = utc_now()
            self._commit(snapshot, "branch_created", {
                "branch": name,
                "parent": parent,
                "from_checkpoint": checkpoint_id,
                "mode": mode,
                "source_stage": stage,
            })
            return snapshot

    def branches_view(self) -> dict[str, Any]:
        with self.lock:
            manifest = self.read()
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
        """Move the active manifest to a branch head without changing its history."""

        if not CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("invalid checkpoint id")
        with self.lock:
            current = self.read()
            branches = current.get("branches", {current.get("branch", "main"): current["checkpoint_id"]})
            matches = [name for name, head in branches.items() if head == checkpoint_id]
            if len(matches) != 1:
                raise ConflictError("branch_head_required")
            target_branch = matches[0]
            snapshot_path = self.root / "checkpoints" / f"{checkpoint_id}.json"
            if not snapshot_path.is_file():
                raise FileNotFoundError(checkpoint_id)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if snapshot.get("checkpoint_id") != checkpoint_id:
                raise ConflictError("checkpoint_corrupt")
            snapshot["branches"] = branches
            snapshot["branch_meta"] = deepcopy(current.get("branch_meta", {}))
            snapshot["branch"] = target_branch
            snapshot["updated_at"] = utc_now()
            atomic_json(self.manifest_path, snapshot)
            self._append_event("branch_switched", checkpoint_id, {"branch": target_branch})
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
