from __future__ import annotations

import hashlib

import pytest

from agent_core.full_deck_reference_context import (
    stored_generation_package_reference_source,
)
from runtime.package_reference_tool import (
    PackageReferenceSource,
    PackageReferenceTool,
    PackageReferenceToolError,
)


def _source(
    source_id: str = "approved_sample",
    *,
    index: bytes = b"<main>sample reference</main>",
) -> PackageReferenceSource:
    contents = {
        "index.html": index,
        "styles/deck.css": b".slide { color: navy; }",
        "assets/logo.png": b"\x89PNG\r\n",
    }
    return PackageReferenceSource.from_contents(
        source_id=source_id,
        package_hash="sha256:" + "a" * 64,
        files=[
            {
                "path": path,
                "media_type": (
                    "image/png" if path.endswith(".png") else "text/plain; charset=utf-8"
                ),
                "size_bytes": len(content),
            }
            for path, content in contents.items()
        ],
        contents=contents,
        kind="approved_sample",
    )


def test_reference_tool_lists_only_readable_files_and_reads_bounded_chunks() -> None:
    index = b"<main>sample reference</main>"
    tool = PackageReferenceTool([_source(index=index)], per_call=8, per_job=30)

    listing = tool.list_reference_files("approved_sample")
    first = tool.read_reference_file(
        "approved_sample", "index.html", offset=6, limit=6
    )
    second = tool.read_reference_file(
        "approved_sample", "styles/deck.css", offset=0, limit=100
    )

    assert listing == {
        "source_id": "approved_sample",
        "kind": "approved_sample",
        "package_hash": "sha256:" + "a" * 64,
        "files": [
            {
                "path": "index.html",
                "media_type": "text/plain; charset=utf-8",
                "size_bytes": len(index),
            },
            {
                "path": "styles/deck.css",
                "media_type": "text/plain; charset=utf-8",
                "size_bytes": len(b".slide { color: navy; }"),
            },
        ],
    }
    assert first == {
        "source_id": "approved_sample",
        "path": "index.html",
        "content": "sample",
        "offset": 6,
        "end": 12,
        "content_hash": "sha256:" + hashlib.sha256(index).hexdigest(),
    }
    assert second["content"] == ".slide {"
    assert second["end"] == 8
    assert tool.consumed == 14


@pytest.mark.parametrize(
    ("source_id", "path", "offset", "limit", "message"),
    [
        ("unknown", "index.html", 0, 10, "not authorized"),
        ("approved_sample", "../secret.html", 0, 10, "stay inside"),
        ("approved_sample", "/etc/passwd.html", 0, 10, "stay inside"),
        ("approved_sample", "assets/logo.png", 0, 10, "not readable"),
        ("approved_sample", "missing.css", 0, 10, "not registered"),
        ("approved_sample", "index.html", 10_000, 10, "outside"),
        ("approved_sample", "index.html", -1, 10, "invalid"),
        ("approved_sample", "index.html", 0, 0, "invalid"),
    ],
)
def test_reference_tool_rejects_unauthorized_paths_and_ranges(
    source_id: str,
    path: str,
    offset: int,
    limit: int,
    message: str,
) -> None:
    tool = PackageReferenceTool([_source()], per_call=20, per_job=40)

    with pytest.raises(PackageReferenceToolError, match=message):
        tool.read_reference_file(source_id, path, offset=offset, limit=limit)


def test_reference_tool_enforces_one_budget_across_sources() -> None:
    tool = PackageReferenceTool(
        [_source(), _source("recent_segment")],
        per_call=10,
        per_job=12,
    )

    assert len(tool.read_reference_file("approved_sample", "index.html")["content"]) == 10
    assert len(tool.read_reference_file("recent_segment", "index.html")["content"]) == 2
    with pytest.raises(PackageReferenceToolError, match="budget exhausted"):
        tool.read_reference_file("approved_sample", "index.html")


def test_reference_sources_are_immutable_and_expose_no_write_operation() -> None:
    source = _source()
    tool = PackageReferenceTool([source], per_call=20, per_job=40)

    with pytest.raises(TypeError):
        source.files[0] = source.files[0]  # type: ignore[index]
    assert not hasattr(tool, "write_reference_file")


def test_reference_source_rejects_duplicate_or_mismatched_files() -> None:
    with pytest.raises(PackageReferenceToolError, match="exactly match"):
        PackageReferenceSource.from_contents(
            source_id="sample",
            package_hash="sha256:" + "b" * 64,
            files=[{
                "path": "index.html",
                "media_type": "text/html; charset=utf-8",
                "size_bytes": 5,
            }],
            contents={"index.html": b"short", "styles.css": b"extra"},
        )

    with pytest.raises(PackageReferenceToolError, match="size does not match"):
        PackageReferenceSource.from_contents(
            source_id="sample",
            package_hash="sha256:" + "b" * 64,
            files=[{
                "path": "index.html",
                "media_type": "text/html; charset=utf-8",
                "size_bytes": 4,
            }],
            contents={"index.html": b"short"},
        )


@pytest.mark.parametrize(
    ("session_id", "kind"),
    [("fullsession_other", "segment"), ("fullsession_expected", "preview")],
)
def test_stored_segment_reference_rejects_cross_session_or_preview_packages(
    session_id: str,
    kind: str,
) -> None:
    class FakeStore:
        @staticmethod
        def full_deck_generation_package(_package_id: str) -> dict:
            return {"session_id": session_id, "kind": kind}

    with pytest.raises(ValueError, match="not authorized"):
        stored_generation_package_reference_source(
            FakeStore(),  # type: ignore[arg-type]
            "fullgenpkg_reference",
            expected_session_id="fullsession_expected",
        )
