from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from runtime.package_tool import TEXT_SUFFIXES, normalize_package_path


class PackageReferenceToolError(ValueError):
    """A rejected read from the server-registered package reference allowlist."""


_SOURCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_PACKAGE_HASH = re.compile(r"sha256:[a-f0-9]{64}")


@dataclass(frozen=True)
class PackageReferenceFile:
    path: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        try:
            path = normalize_package_path(self.path)
        except ValueError as exc:
            raise PackageReferenceToolError(str(exc)) from exc
        if not isinstance(self.media_type, str) or not self.media_type:
            raise PackageReferenceToolError("reference file media type is empty")
        if type(self.size_bytes) is not int or self.size_bytes < 1:
            raise PackageReferenceToolError("reference file size is invalid")
        object.__setattr__(self, "path", path)

    @property
    def readable(self) -> bool:
        return PurePosixPath(self.path).suffix.lower() in TEXT_SUFFIXES

    def public_value(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class PackageReferenceSource:
    source_id: str
    package_hash: str
    files: tuple[PackageReferenceFile, ...]
    read_bytes: Callable[[str], bytes]
    kind: str = "package"

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(self.source_id):
            raise PackageReferenceToolError("reference source id is invalid")
        if not isinstance(self.package_hash, str) or not _PACKAGE_HASH.fullmatch(
            self.package_hash
        ):
            raise PackageReferenceToolError("reference package hash is invalid")
        if not isinstance(self.kind, str) or not self.kind or len(self.kind) > 80:
            raise PackageReferenceToolError("reference source kind is invalid")
        if not callable(self.read_bytes):
            raise PackageReferenceToolError("reference source loader is invalid")
        normalized_files = tuple(
            item
            if isinstance(item, PackageReferenceFile)
            else PackageReferenceFile(**item)
            for item in self.files
        )
        paths = [item.path for item in normalized_files]
        if not paths or len(paths) != len(set(paths)):
            raise PackageReferenceToolError(
                "reference source files must be non-empty and unique"
            )
        object.__setattr__(self, "files", normalized_files)

    @classmethod
    def from_contents(
        cls,
        *,
        source_id: str,
        package_hash: str,
        files: Iterable[PackageReferenceFile | dict[str, Any]],
        contents: Mapping[str, bytes],
        kind: str = "package",
    ) -> "PackageReferenceSource":
        normalized_files = tuple(
            item
            if isinstance(item, PackageReferenceFile)
            else PackageReferenceFile(**item)
            for item in files
        )
        normalized_contents: dict[str, bytes] = {}
        for path, content in contents.items():
            try:
                normalized_path = normalize_package_path(path)
            except ValueError as exc:
                raise PackageReferenceToolError(str(exc)) from exc
            if not isinstance(content, bytes) or not content:
                raise PackageReferenceToolError(
                    "reference package contents must be non-empty bytes"
                )
            if normalized_path in normalized_contents:
                raise PackageReferenceToolError(
                    "reference package content paths must be unique"
                )
            normalized_contents[normalized_path] = bytes(content)
        file_paths = {item.path for item in normalized_files}
        if file_paths != set(normalized_contents):
            raise PackageReferenceToolError(
                "reference file metadata and contents must exactly match"
            )
        for item in normalized_files:
            if item.size_bytes != len(normalized_contents[item.path]):
                raise PackageReferenceToolError(
                    f"reference file size does not match content: {item.path}"
                )
        immutable_contents = MappingProxyType(normalized_contents)

        def load(logical_path: str) -> bytes:
            try:
                return immutable_contents[logical_path]
            except KeyError:
                raise FileNotFoundError(logical_path) from None

        return cls(
            source_id=source_id,
            package_hash=package_hash,
            files=normalized_files,
            read_bytes=load,
            kind=kind,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "package_hash": self.package_hash,
            "files": [
                item.public_value()
                for item in self.files
                if item.readable
            ],
        }


class PackageReferenceTool:
    """Read-only, budgeted access to an explicit set of immutable package sources."""

    def __init__(
        self,
        sources: Iterable[PackageReferenceSource],
        *,
        per_call: int,
        per_job: int,
    ) -> None:
        if type(per_call) is not int or type(per_job) is not int:
            raise PackageReferenceToolError("reference read budgets must be integers")
        if per_call < 1 or per_job < 1 or per_call > per_job:
            raise PackageReferenceToolError("reference read budgets are invalid")
        source_list = tuple(sources)
        source_ids = [item.source_id for item in source_list]
        if len(source_ids) != len(set(source_ids)):
            raise PackageReferenceToolError("reference source ids must be unique")
        self._sources = {item.source_id: item for item in source_list}
        self.per_call = per_call
        self.per_job = per_job
        self.consumed = 0

    def summaries(self) -> list[dict[str, Any]]:
        return [self._sources[source_id].summary() for source_id in self._sources]

    def _source(self, source_id: str) -> PackageReferenceSource:
        if not isinstance(source_id, str) or source_id not in self._sources:
            raise PackageReferenceToolError("reference source is not authorized")
        return self._sources[source_id]

    def list_reference_files(self, source_id: str) -> dict[str, Any]:
        return self._source(source_id).summary()

    def read_reference_file(
        self,
        source_id: str,
        logical_path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        source = self._source(source_id)
        try:
            path = normalize_package_path(logical_path)
        except ValueError as exc:
            raise PackageReferenceToolError(str(exc)) from exc
        if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
            raise PackageReferenceToolError("reference package file is not readable text")
        file_by_path = {item.path: item for item in source.files}
        metadata = file_by_path.get(path)
        if metadata is None:
            raise PackageReferenceToolError("reference package file is not registered")
        if type(offset) is not int or offset < 0:
            raise PackageReferenceToolError("invalid reference read range")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise PackageReferenceToolError("invalid reference read range")
        remaining = self.per_job - self.consumed
        allowed = min(limit or self.per_call, self.per_call, remaining)
        if allowed <= 0:
            raise PackageReferenceToolError("reference read budget exhausted")
        try:
            content = source.read_bytes(path)
        except Exception as exc:
            raise PackageReferenceToolError(
                "registered reference package file is unavailable"
            ) from exc
        if not isinstance(content, bytes):
            raise PackageReferenceToolError("reference package loader returned invalid content")
        if len(content) != metadata.size_bytes:
            raise PackageReferenceToolError("reference package file size changed")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageReferenceToolError(
                "reference package file is not valid UTF-8 text"
            ) from exc
        if offset > len(text):
            raise PackageReferenceToolError("reference read offset is outside the file")
        chunk = text[offset : offset + allowed]
        self.consumed += len(chunk)
        return {
            "source_id": source.source_id,
            "path": path,
            "content": chunk,
            "offset": offset,
            "end": offset + len(chunk),
            "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
