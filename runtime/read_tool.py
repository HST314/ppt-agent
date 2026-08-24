from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ReadToolError(ValueError):
    pass


@dataclass
class ReadResult:
    path: str
    content: str
    offset: int
    end: int
    content_hash: str


class SkillReader:
    def __init__(self, root: Path, *, per_call: int, per_job: int):
        self.root = root.resolve()
        self.per_call = per_call
        self.per_job = per_job
        self.consumed = 0

    def index(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            logical = path.relative_to(self.root).as_posix()
            lines = path.read_text(encoding="utf-8").splitlines()
            title = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), path.parent.name)
            description = next((line.strip() for line in lines if line.strip() and not line.startswith("#")), "")
            result.append({"name": title, "description": description, "path": logical})
        return result

    def read(self, logical_path: str, offset: int = 0, limit: int | None = None) -> ReadResult:
        if not logical_path or "\x00" in logical_path:
            raise ReadToolError("path is empty or contains NUL")
        posix = PurePosixPath(logical_path)
        if posix.is_absolute() or ".." in posix.parts:
            raise ReadToolError("only relative paths inside skills_root are allowed")
        if posix.suffix.lower() not in {".md", ".txt"}:
            raise ReadToolError("only .md and .txt files are readable")
        candidate = (self.root / Path(*posix.parts)).resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file():
            raise ReadToolError("file is outside skills_root or does not exist")
        allowed = min(limit or self.per_call, self.per_call, self.per_job - self.consumed)
        if offset < 0 or allowed <= 0:
            raise ReadToolError("invalid offset or read budget exhausted")
        content = candidate.read_text(encoding="utf-8")
        chunk = content[offset : offset + allowed]
        self.consumed += len(chunk)
        return ReadResult(
            path=posix.as_posix(),
            content=chunk,
            offset=offset,
            end=offset + len(chunk),
            content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        )
