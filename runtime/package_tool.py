from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.read_tool import ReadToolError, SkillReader


class PackageToolError(ValueError):
    pass


TEXT_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".txt", ".md",
}
STATIC_SUFFIXES = TEXT_SUFFIXES | {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".webm", ".wav",
}


def normalize_package_path(logical_path: str) -> str:
    if not isinstance(logical_path, str) or not logical_path or "\x00" in logical_path:
        raise PackageToolError("package path is empty or contains NUL")
    posix = PurePosixPath(logical_path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or not posix.name:
        raise PackageToolError("package path must stay inside the draft")
    if any(part in {"", "."} for part in posix.parts):
        raise PackageToolError("package path is not normalized")
    if posix.suffix.lower() not in STATIC_SUFFIXES:
        raise PackageToolError("package file type is not allowed")
    return posix.as_posix()


def package_media_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    overrides = {
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }
    return overrides.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class DraftPackage:
    """In-memory, path-confined HTML-PPT draft used only for one model attempt."""

    def __init__(
        self,
        skills_root: Path,
        *,
        max_files: int = 96,
        max_file_bytes: int = 2_000_000,
        max_total_bytes: int = 15_000_000,
    ):
        self.reader = SkillReader(skills_root, per_call=1, per_job=1)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._files: dict[str, bytes] = {}
        self._origins: dict[str, str] = {}

    def _put(self, logical_path: str, content: bytes, origin: str) -> dict[str, Any]:
        path = normalize_package_path(logical_path)
        if not content:
            raise PackageToolError("package files must not be empty")
        if len(content) > self.max_file_bytes:
            raise PackageToolError("package file exceeds the per-file limit")
        next_count = len(self._files) + (0 if path in self._files else 1)
        next_total = sum(len(value) for key, value in self._files.items() if key != path) + len(content)
        if next_count > self.max_files:
            raise PackageToolError("package contains too many files")
        if next_total > self.max_total_bytes:
            raise PackageToolError("package exceeds the total size limit")
        self._files[path] = content
        self._origins[path] = origin
        return {
            "path": path,
            "size_bytes": len(content),
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        }

    def write(self, logical_path: str, content: str) -> dict[str, Any]:
        path = normalize_package_path(logical_path)
        if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
            raise PackageToolError("write_package_file only accepts text files")
        if not isinstance(content, str):
            raise PackageToolError("package content must be UTF-8 text")
        return self._put(path, content.encode("utf-8"), "model_write")

    def copy_skill_asset(self, source_path: str, destination_path: str) -> dict[str, Any]:
        try:
            source = self.reader.resolve_asset(source_path)
        except ReadToolError as exc:
            raise PackageToolError(str(exc)) from exc
        if source.suffix.lower() not in STATIC_SUFFIXES:
            raise PackageToolError("skill asset type is not allowed in a package")
        record = self._put(destination_path, source.read_bytes(), f"skill:{source_path}")
        return {**record, "source_path": PurePosixPath(source_path).as_posix()}

    def replace_text(
        self,
        logical_path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        path = normalize_package_path(logical_path)
        if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
            raise PackageToolError("replace_package_text only accepts text files")
        if not isinstance(old, str) or not old or len(old) > 50_000:
            raise PackageToolError("replacement target is empty or too large")
        if not isinstance(new, str):
            raise PackageToolError("replacement content must be UTF-8 text")
        content = self._files.get(path)
        if content is None:
            raise PackageToolError("package file does not exist")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageToolError("binary package files cannot be edited as text") from exc
        occurrences = text.count(old)
        if occurrences == 0:
            raise PackageToolError("replacement target was not found")
        replacements = occurrences if replace_all else 1
        updated = text.replace(old, new, -1 if replace_all else 1)
        record = self._put(path, updated.encode("utf-8"), "model_edit")
        return {**record, "replacements": replacements}

    def read(self, logical_path: str, offset: int = 0, limit: int = 50_000) -> dict[str, Any]:
        path = normalize_package_path(logical_path)
        if offset < 0 or limit < 1:
            raise PackageToolError("invalid package read range")
        content = self._files.get(path)
        if content is None:
            raise PackageToolError("package file does not exist")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageToolError("binary package files cannot be read as text") from exc
        limit = min(limit, 50_000)
        chunk = text[offset : offset + limit]
        return {
            "path": path,
            "content": chunk,
            "offset": offset,
            "end": offset + len(chunk),
            "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
        }

    def ingest(self, files: list[dict[str, Any]]) -> None:
        for item in files:
            if not isinstance(item, dict):
                raise PackageToolError("package files must be objects")
            encoding = item.get("encoding", "utf-8")
            content = item.get("content")
            if not isinstance(content, str):
                raise PackageToolError("package file content must be a string")
            if encoding == "utf-8":
                decoded = content.encode("utf-8")
            elif encoding == "base64":
                try:
                    decoded = base64.b64decode(content, validate=True)
                except ValueError as exc:
                    raise PackageToolError("package file contains invalid base64") from exc
            else:
                raise PackageToolError("unsupported package file encoding")
            self._put(item.get("path", ""), decoded, "model_output")

    def payload(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path, content in sorted(self._files.items()):
            if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
                text = base64.b64encode(content).decode("ascii")
                encoding = "base64"
            else:
                try:
                    text = content.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    text = base64.b64encode(content).decode("ascii")
                    encoding = "base64"
            result.append({
                "path": path,
                "content": text,
                "encoding": encoding,
                "media_type": package_media_type(path),
                "origin": self._origins[path],
            })
        return result

    def has(self, logical_path: str) -> bool:
        return normalize_package_path(logical_path) in self._files

    @property
    def total_bytes(self) -> int:
        return sum(len(value) for value in self._files.values())
