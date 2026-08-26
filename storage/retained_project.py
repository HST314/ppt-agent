from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from runtime.package_tool import normalize_package_path
from storage.persistence import atomic_bytes


REVISION_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")


class RetainedProjectError(RuntimeError):
    pass


def write_artifacts_readme(artifacts_root: Path) -> None:
    content = (
        "# Project artifacts\n\n"
        "- `full_decks/<revision>/` contains a complete, directly openable retained "
        "project for each published full-deck revision, including `project.json`.\n"
        "- `_objects/` is the internal content-addressed object store used for "
        "deduplication and integrity checks; filenames there are not deliverables.\n"
        "- `../generated_html/prompt_*/` contains bounded raw model-attempt snapshots "
        "for diagnostics and may omit sample pages before composition.\n"
    ).encode("utf-8")
    path = artifacts_root / "README.md"
    if path.is_file() and path.read_bytes() == content:
        return
    atomic_bytes(path, content)


def retained_project_payload(
    manifest: dict[str, Any],
    revision: dict[str, Any],
) -> dict[str, Any]:
    documents = manifest.get("documents", {})
    return {
        "format": "ppt-agent-retained-project-v1",
        "project": {
            "project_id": manifest["project_id"],
            "title": manifest["title"],
            "branch": manifest.get("branch", "main"),
            "checkpoint_id": manifest["checkpoint_id"],
        },
        "inputs": {
            "task_card": manifest.get("task_card", {}),
            "clarification_answers": manifest.get("clarification_answers", {}),
            "clarification_history": manifest.get("clarification_history", []),
            "narrative_structure": (
                documents.get("narrative_structure") or [None]
            )[-1],
            "slide_outline": (documents.get("slide_outline") or [None])[-1],
            "sample_revision_hash": manifest.get("current_sample_revision_hash"),
        },
        "full_deck_revision": revision,
    }


def _package_reference_content(
    item: dict[str, Any],
    package_artifact_roots: tuple[Path, ...],
) -> bytes:
    artifact_id = str(item.get("artifact_id", ""))
    if not REVISION_HASH.fullmatch(artifact_id):
        raise RetainedProjectError("package_artifact_missing")
    suffix = Path(normalize_package_path(item["path"])).suffix.lower()
    filename = f"{artifact_id.removeprefix('sha256:')}{suffix}"
    artifact_path = next(
        (
            root / filename
            for root in package_artifact_roots
            if (root / filename).is_file()
            and (root / filename).resolve().is_relative_to(root.resolve())
        ),
        None,
    )
    if artifact_path is None:
        raise RetainedProjectError("package_artifact_missing")
    content = artifact_path.read_bytes()
    if (
        "sha256:" + hashlib.sha256(content).hexdigest() != item.get("sha256")
        or len(content) != item.get("size")
    ):
        raise RetainedProjectError("artifact_corrupt")
    return content


def materialize_full_deck_revision(
    manifest: dict[str, Any],
    revision: dict[str, Any],
    *,
    full_deck_root: Path,
    package_artifact_roots: tuple[Path, ...],
) -> None:
    package = revision.get("package")
    if not package:
        return
    revision_hash = str(revision.get("revision_hash", ""))
    if not REVISION_HASH.fullmatch(revision_hash):
        raise ValueError("invalid revision hash")
    final_root = full_deck_root / revision_hash.removeprefix("sha256:")
    if (
        not final_root.resolve().is_relative_to(full_deck_root.resolve())
        or final_root.is_symlink()
    ):
        raise ValueError("invalid retained project root")
    if final_root.is_dir():
        return
    project_json = json.dumps(
        retained_project_payload(manifest, revision),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    contents = {
        normalize_package_path(item["path"]): _package_reference_content(
            item,
            package_artifact_roots,
        )
        for item in package.get("files", [])
    }
    if package.get("entrypoint") not in contents:
        raise RetainedProjectError("package_artifact_missing")
    full_deck_root.mkdir(parents=True, exist_ok=True)
    staging = full_deck_root / (
        f".{revision_hash.removeprefix('sha256:')}.{uuid4().hex}.tmp"
    )
    try:
        for logical_path, content in contents.items():
            target = (staging / logical_path).resolve()
            if not target.is_relative_to(staging.resolve()):
                raise ValueError("invalid retained package path")
            atomic_bytes(target, content)
        atomic_bytes(staging / "project.json", project_json)
        os.replace(staging, final_root)
        try:
            directory_fd = os.open(full_deck_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def retained_full_deck_dir(full_deck_root: Path, revision_hash: str) -> Path:
    if not REVISION_HASH.fullmatch(revision_hash):
        raise ValueError("invalid revision hash")
    path = full_deck_root / revision_hash.removeprefix("sha256:")
    if not path.is_dir():
        raise FileNotFoundError(revision_hash)
    return path
