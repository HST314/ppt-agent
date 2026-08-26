from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from pathlib import PurePosixPath
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


def full_deck_image_description_paths(
    outline_markdown: str,
    images_manifest: list[dict[str, Any]],
    target_numbers: Collection[int],
) -> list[str]:
    """Pick the description files a full-deck call should inject (plan D-11).

    The manifest itself is always injected in full; only the full-text
    descriptions are filtered to the images planned on the target pages.
    When the outline carries no usable image plans at all, or when none of
    them intersect the target pages, every description is injected — an
    empty intersection means "fall back to full injection", never "inject
    nothing". Returns deterministic (deduplicated) project-relative paths.
    """

    if not images_manifest:
        return []
    description_by_name = {
        PurePosixPath(entry["image_path"]).name: entry["description_path"]
        for entry in images_manifest
    }
    page_map = outline_page_image_map(outline_markdown, description_by_name)
    if not page_map:
        return [entry["description_path"] for entry in images_manifest]
    planned: list[str] = []
    seen: set[str] = set()
    for number in target_numbers:
        for name in page_map.get(int(number), []):
            path = description_by_name[name]
            if path not in seen:
                seen.add(path)
                planned.append(path)
    if not planned:
        return [entry["description_path"] for entry in images_manifest]
    return planned


def full_deck_images_prompt_section(
    images_template: str,
    images_manifest: list[dict[str, Any]],
    descriptions: dict[str, str],
) -> str:
    """Append-only full-deck block: usage rules plus full description texts."""

    description_blocks = [
        f"[{path}]\n{text}" for path, text in sorted(descriptions.items())
    ]
    return (
        f"\n\n{images_template.strip()}\n\n"
        f"PROJECT_IMAGES_JSON: {json.dumps(images_manifest, ensure_ascii=False)}\n"
        "PROJECT_IMAGE_DESCRIPTIONS:\n"
        + "\n".join(description_blocks)
    )


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
