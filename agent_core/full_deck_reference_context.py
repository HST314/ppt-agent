from __future__ import annotations

from collections.abc import Iterable

from agent_core.models import HtmlPptPackage
from configs.runtime import ManagedRuntime
from runtime.package_reference_tool import (
    PackageReferenceFile,
    PackageReferenceSource,
    PackageReferenceTool,
)
from storage.persistence import read_bytes
from storage.project_store import ProjectStore


def _file_records(package: HtmlPptPackage) -> tuple[PackageReferenceFile, ...]:
    return tuple(
        PackageReferenceFile(
            path=item.path,
            media_type=item.media_type,
            size_bytes=len(item.content_bytes()),
        )
        for item in package.files
    )


def _sample_source(
    store: ProjectStore,
    revision_hash: str,
    package: HtmlPptPackage,
) -> PackageReferenceSource:
    def read_registered_file(logical_path: str) -> bytes:
        artifact_path, _media_type = store.sample_package_file(
            revision_hash, logical_path
        )
        return read_bytes(artifact_path)

    return PackageReferenceSource(
        source_id="approved_sample",
        package_hash=package.package_hash,
        files=_file_records(package),
        read_bytes=read_registered_file,
        kind="approved_sample",
    )


def _validated_segment_source(
    source_id: str,
    package: HtmlPptPackage,
) -> PackageReferenceSource:
    file_by_path = {item.path: item for item in package.files}

    def read_validated_file(logical_path: str) -> bytes:
        try:
            return file_by_path[logical_path].content_bytes()
        except KeyError:
            raise FileNotFoundError(logical_path) from None

    return PackageReferenceSource(
        source_id=source_id,
        package_hash=package.package_hash,
        files=_file_records(package),
        read_bytes=read_validated_file,
        kind="successful_segment",
    )


def stored_generation_package_reference_source(
    store: ProjectStore,
    package_id: str,
    *,
    expected_session_id: str,
) -> PackageReferenceSource:
    """Authorize one durable segment through its owning generation session."""

    package = store.full_deck_generation_package(package_id)
    if (
        package.get("session_id") != expected_session_id
        or package.get("kind") != "segment"
    ):
        raise ValueError("generation reference package is not authorized for this session")

    def read_registered_file(logical_path: str) -> bytes:
        artifact_path, _media_type = store.full_deck_generation_package_file(
            package_id, logical_path
        )
        return read_bytes(artifact_path)

    return PackageReferenceSource(
        source_id=f"segment_batch_{int(package['batch_index'])}",
        package_hash=str(package["package_hash"]),
        files=tuple(
            PackageReferenceFile(
                path=str(item["logical_path"]),
                media_type=str(item["media_type"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in package["files"]
        ),
        read_bytes=read_registered_file,
        kind="successful_segment",
    )


def full_deck_package_reference_tool(
    store: ProjectStore,
    runtime: ManagedRuntime,
    *,
    sample_revision_hash: str,
    sample_package: HtmlPptPackage,
    recent_validated_segments: Iterable[tuple[str, HtmlPptPackage]] = (),
    recent_segment_package_ids: Iterable[str] = (),
    generation_session_id: str | None = None,
) -> PackageReferenceTool:
    """Register the visual anchor and no more than two recent successful segments."""

    validated_segments = list(recent_validated_segments)
    package_ids = list(recent_segment_package_ids)
    if validated_segments and package_ids:
        raise ValueError("recent segment references require one source mode")
    if len(validated_segments) > 2 or len(package_ids) > 2:
        raise ValueError("at most two recent segment reference packages are allowed")
    if package_ids and not generation_session_id:
        raise ValueError("generation session id is required for segment references")
    sources = [_sample_source(store, sample_revision_hash, sample_package)]
    sources.extend(
        _validated_segment_source(source_id, package)
        for source_id, package in validated_segments
    )
    sources.extend(
        stored_generation_package_reference_source(
            store,
            package_id,
            expected_session_id=str(generation_session_id),
        )
        for package_id in package_ids
    )
    return PackageReferenceTool(
        sources,
        per_call=runtime.policy.full_deck_reference_max_read_chars_per_call,
        per_job=runtime.policy.full_deck_reference_max_read_chars_per_batch,
    )
