from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from typing import Any

from agent_core.models import HtmlPptPackage


OUTLINE_PAGE_HEADING = re.compile(
    r"^\s{0,3}#{2,6}\s+第\s*(?P<number>\d+)\s*页(?:\s*[｜|:：—-]\s*(?P<title>.*?))?\s*$",
    re.MULTILINE,
)
OUTLINE_FALLBACK_HEADING = re.compile(
    r"^\s{0,3}##\s+(?P<title>\S.*?)\s*$",
    re.MULTILINE,
)
# Loose per-page image plan line: optional list marker, the keyword, a full- or
# half-width colon, then image names separated by ，、 or ,.
OUTLINE_IMAGE_LINE = re.compile(
    r"^\s{0,3}(?:[-*+]\s+)?配图\s*[:：]\s*(?P<names>\S.*?)\s*$",
    re.MULTILINE,
)
OUTLINE_IMAGE_NAME_SEPARATORS = "，、,"


def outline_slide_catalog(markdown: str) -> list[dict[str, Any]]:
    matches = list(OUTLINE_PAGE_HEADING.finditer(markdown))
    if matches:
        catalog = [
            {
                "source_slide_number": int(match.group("number")),
                "title": (
                    match.group("title") or f"第 {match.group('number')} 页"
                ).strip(),
            }
            for match in matches
        ]
    else:
        catalog = [
            {"source_slide_number": index, "title": match.group("title").strip()}
            for index, match in enumerate(
                OUTLINE_FALLBACK_HEADING.finditer(markdown),
                start=1,
            )
        ]
    numbers = [item["source_slide_number"] for item in catalog]
    if not numbers or len(numbers) != len(set(numbers)):
        raise ValueError("approved outline must contain uniquely numbered slide headings")
    return catalog


def outline_page_image_map(
    markdown: str,
    image_names: Collection[str],
) -> dict[int, list[str]]:
    """Loosely collect the per-page image plans (「配图：」 lines) of an outline.

    Keys match the ``source_slide_number`` values ``outline_slide_catalog``
    reports (falling back to positional numbering when no numbered headings
    exist). Only names present in ``image_names`` (typically the current
    project image manifest) are kept; anything else on the line — unknown
    names, stray tokens — is silently ignored. Pages without a usable plan
    are omitted. ``outline_slide_catalog`` itself is unaffected.
    """

    available = set(image_names)
    matches = list(OUTLINE_PAGE_HEADING.finditer(markdown))
    if matches:
        pages: list[tuple[int, re.Match[str]]] = [
            (int(match.group("number")), match) for match in matches
        ]
    else:
        pages = [
            (index, match)
            for index, match in enumerate(
                OUTLINE_FALLBACK_HEADING.finditer(markdown),
                start=1,
            )
        ]
    result: dict[int, list[str]] = {}
    for position, (number, heading) in enumerate(pages):
        body_start = heading.end()
        body_end = (
            pages[position + 1][1].start()
            if position + 1 < len(pages)
            else len(markdown)
        )
        names: list[str] = []
        for line in OUTLINE_IMAGE_LINE.finditer(markdown, body_start, body_end):
            for token in re.split(
                f"[{OUTLINE_IMAGE_NAME_SEPARATORS}]", line.group("names")
            ):
                name = token.strip()
                if not name or name not in available or name in names:
                    continue
                names.append(name)
        if names:
            result[number] = names
    return result


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


# generate() capabilities a custom/legacy gateway adapter may not know about.
# When such an adapter rejects one of them with a TypeError, the retry drops
# only the rejected capabilities and keeps the rest.
_LEGACY_GATEWAY_CAPABILITIES = ("package_draft", "images_root")


def generate_with_legacy_gateway(gateway: Any, state: str, prompt: str, **kwargs: Any):
    """Call gateway.generate, retrying without capabilities a legacy adapter rejects."""

    try:
        return gateway.generate(state, prompt, **kwargs)
    except TypeError as exc:
        message = str(exc)
        unsupported = {
            name for name in _LEGACY_GATEWAY_CAPABILITIES
            if name in kwargs and name in message
        }
        if not unsupported:
            raise
        return gateway.generate(
            state, prompt, **{
                key: value for key, value in kwargs.items() if key not in unsupported
            }
        )


def package_model(package: dict[str, Any]) -> HtmlPptPackage:
    """Discard storage-only file metadata at the immutable package boundary."""

    package_fields = {
        key: value
        for key, value in package.items()
        if key in {"entrypoint", "title", "slide_count", "slides", "package_hash"}
    }
    package_fields["files"] = [
        {
            key: value
            for key, value in item.items()
            if key in {"path", "content", "encoding", "media_type", "origin"}
        }
        for item in package.get("files", [])
    ]
    return HtmlPptPackage.model_validate(package_fields)


def generation_provenance(
    skill_index: list[dict[str, str]],
    traces: list[dict[str, Any]],
    output: str,
) -> dict[str, Any]:
    def is_successful_skill_read(trace: dict[str, Any]) -> bool:
        path = trace.get("path")
        content_hash = trace.get("content_hash")
        offset = trace.get("offset")
        end = trace.get("end")
        return (
            trace.get("type") == "tool_call"
            and trace.get("tool") == "read"
            and "error" not in trace
            and isinstance(path, str)
            and bool(path)
            and isinstance(content_hash, str)
            and bool(content_hash)
            and isinstance(offset, int)
            and not isinstance(offset, bool)
            and offset >= 0
            and isinstance(end, int)
            and not isinstance(end, bool)
            and end >= offset
        )

    skill_reads = sorted(
        (
            {
                "path": trace["path"],
                "content_hash": trace["content_hash"],
                "offset": trace["offset"],
                "end": trace["end"],
            }
            for trace in traces
            if is_successful_skill_read(trace)
        ),
        key=lambda item: (item["path"], item["content_hash"], item["offset"], item["end"]),
    )
    return {
        "skill_index": skill_index,
        "skills_hash": stable_hash(skill_index),
        "skill_reads": skill_reads,
        "skill_reads_hash": stable_hash(skill_reads),
        "output_hash": "sha256:" + hashlib.sha256(output.encode()).hexdigest(),
    }


def current_full_deck_revision(manifest: dict[str, Any]) -> dict[str, Any] | None:
    root = manifest.get("full_deck") or {}
    current_hash = root.get("current_revision_hash")
    return next(
        (
            item
            for item in manifest.get("full_deck_revisions", [])
            if item.get("revision_hash") == current_hash
        ),
        None,
    )
