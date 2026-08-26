from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ReadToolError(ValueError):
    pass


IMAGES_PREFIX = "images/"


@dataclass
class ReadResult:
    path: str
    content: str
    offset: int
    end: int
    content_hash: str


class SkillReader:
    READABLE_SUFFIXES = {
        ".md", ".txt", ".html", ".htm", ".css", ".js", ".mjs",
        ".json", ".yaml", ".yml", ".svg", ".xml",
    }

    def __init__(
        self,
        root: Path,
        *,
        per_call: int,
        per_job: int,
        images_root: Path | None = None,
    ):
        self.root = root.resolve()
        self.images_root = (
            images_root.resolve() if images_root is not None else None
        )
        self.per_call = per_call
        self.per_job = per_job
        self.consumed = 0

    def index(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            logical = path.relative_to(self.root).as_posix()
            candidate = self._resolve_file(logical)
            lines = candidate.read_text(encoding="utf-8").splitlines()
            frontmatter: dict[str, str] = {}
            if lines[:1] == ["---"]:
                for line in lines[1:]:
                    if line == "---":
                        break
                    key, separator, value = line.partition(":")
                    if separator and key.strip() in {"name", "description"}:
                        frontmatter[key.strip()] = value.strip().strip("\"'")
            title = frontmatter.get("name") or next(
                (line.lstrip("# ").strip() for line in lines if line.startswith("#")),
                path.parent.name,
            )
            description = frontmatter.get("description") or next(
                (
                    line.strip() for line in lines
                    if line.strip() and not line.startswith("#") and line != "---"
                ),
                "",
            )
            result.append({"name": title, "description": description, "path": logical})
        return result

    def _resolve_file(self, logical_path: str) -> Path:
        if not logical_path or "\x00" in logical_path:
            raise ReadToolError("path is empty or contains NUL")
        if self.images_root is not None and logical_path.startswith(IMAGES_PREFIX):
            return self._resolve_images_file(logical_path)
        posix = PurePosixPath(logical_path)
        if posix.is_absolute() or ".." in posix.parts:
            raise ReadToolError("only relative paths inside skills_root are allowed")
        if posix.suffix.lower() not in self.READABLE_SUFFIXES:
            raise ReadToolError("file type is not readable")
        try:
            candidate = (self.root / Path(*posix.parts)).resolve(strict=True)
            is_regular = stat.S_ISREG(candidate.stat().st_mode)
        except (OSError, RuntimeError):
            raise ReadToolError("file is outside skills_root or does not exist") from None
        if not candidate.is_relative_to(self.root) or not is_regular:
            raise ReadToolError("file is outside skills_root or does not exist")
        return candidate

    def _resolve_images_file(self, logical_path: str) -> Path:
        """Resolve ``images/<name>.md`` against the project images root.

        The synced images directory is flat, so exactly one path segment is
        accepted; only Markdown description files are readable there and
        image bytes never pass through the text read tool.
        """

        rest = logical_path[len(IMAGES_PREFIX):]
        posix = PurePosixPath(rest)
        if (
            not rest
            or posix.is_absolute()
            or ".." in posix.parts
            or len(posix.parts) != 1
        ):
            raise ReadToolError("only flat relative paths inside the images directory are allowed")
        if posix.suffix.lower() != ".md":
            raise ReadToolError("only Markdown description files are readable inside images/")
        try:
            candidate = (self.images_root / Path(*posix.parts)).resolve(strict=True)
            is_regular = stat.S_ISREG(candidate.stat().st_mode)
        except (OSError, RuntimeError):
            raise ReadToolError("file is outside the images directory or does not exist") from None
        if not candidate.is_relative_to(self.images_root) or not is_regular:
            raise ReadToolError("file is outside the images directory or does not exist")
        return candidate

    def resolve_asset(self, logical_path: str) -> Path:
        """Resolve a regular file for a bounded copy without exposing host paths."""

        if not logical_path or "\x00" in logical_path:
            raise ReadToolError("path is empty or contains NUL")
        posix = PurePosixPath(logical_path)
        if posix.is_absolute() or ".." in posix.parts:
            raise ReadToolError("only relative paths inside skills_root are allowed")
        try:
            candidate = (self.root / Path(*posix.parts)).resolve(strict=True)
            is_regular = stat.S_ISREG(candidate.stat().st_mode)
        except (OSError, RuntimeError):
            raise ReadToolError("file is outside skills_root or does not exist") from None
        if not candidate.is_relative_to(self.root) or not is_regular:
            raise ReadToolError("file is outside skills_root or does not exist")
        return candidate

    def read(self, logical_path: str, offset: int = 0, limit: int | None = None) -> ReadResult:
        candidate = self._resolve_file(logical_path)
        posix = PurePosixPath(logical_path)
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
