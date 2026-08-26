from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal

from pydantic import Field, model_validator

from agent_core.full_deck_composer import (
    ComposerPage,
    ComposerSource,
    FullDeckComposerError,
    FullDeckComposerInput,
    compose_full_deck,
)
from agent_core.models import (
    FullDeckGenerationContentRef,
    FullDeckGenerationPackage,
    FullDeckGenerationPage,
    FullDeckPageSlot,
    FullDeckPlan,
    HtmlPptPackage,
    StrictModel,
)


FULL_DECK_BATCH_PLANNER_VERSION = "balanced-3-4-v1"


class PlannedFullDeckBatch(StrictModel):
    """One immutable target emitted by the deterministic batch planner."""

    batch_index: int = Field(ge=1, le=1000)
    slot_ids: list[str] = Field(min_length=1, max_length=5)
    source_slide_numbers: list[int] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_targets(self) -> "PlannedFullDeckBatch":
        if len(self.slot_ids) != len(self.source_slide_numbers):
            raise ValueError("planned batch slots and slide numbers must align")
        if len(self.slot_ids) != len(set(self.slot_ids)):
            raise ValueError("planned batch slot ids must be unique")
        if any(not re.fullmatch(r"slot_[a-f0-9]{24}", item) for item in self.slot_ids):
            raise ValueError("planned batch contains an invalid full-deck slot id")
        if any(not 1 <= item <= 1000 for item in self.source_slide_numbers):
            raise ValueError("planned batch slide numbers must be between 1 and 1000")
        if len(self.source_slide_numbers) != len(set(self.source_slide_numbers)):
            raise ValueError("planned batch slide numbers must be unique")
        if any(
            right != left + 1
            for left, right in zip(
                self.source_slide_numbers,
                self.source_slide_numbers[1:],
            )
        ):
            raise ValueError("planned batch slide numbers must be contiguous")
        return self


class FullDeckBatchPlan(StrictModel):
    """Auditable output of one planner version over an ordered full-deck plan."""

    planner_version: Literal["balanced-3-4-v1"] = FULL_DECK_BATCH_PLANNER_VERSION
    batches: list[PlannedFullDeckBatch] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_batch_order(self) -> "FullDeckBatchPlan":
        if [item.batch_index for item in self.batches] != list(
            range(1, len(self.batches) + 1)
        ):
            raise ValueError("planned batch indexes must be ordered and contiguous")
        slot_ids = [slot_id for item in self.batches for slot_id in item.slot_ids]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("a full-deck slot cannot occur in multiple batches")
        return self


def _full_deck_plan(
    pages: FullDeckPlan | Sequence[FullDeckPageSlot | dict[str, Any]],
) -> FullDeckPlan:
    if isinstance(pages, FullDeckPlan):
        return pages
    return FullDeckPlan.model_validate({"pages": list(pages)})


def _pending_runs(pages: Sequence[FullDeckPageSlot]) -> list[list[FullDeckPageSlot]]:
    runs: list[list[FullDeckPageSlot]] = []
    current: list[FullDeckPageSlot] = []
    previous_number: int | None = None
    for page in pages:
        if page.status != "pending":
            if current:
                runs.append(current)
                current = []
            previous_number = None
            continue
        if page.outline_ref is None:
            raise ValueError(
                f"pending full-deck slot has no source slide number: {page.slot_id}"
            )
        number = page.outline_ref.source_slide_number
        if current and number != previous_number + 1:
            runs.append(current)
            current = []
        current.append(page)
        previous_number = number
    if current:
        runs.append(current)
    return runs


def _balanced_batch_sizes(length: int) -> list[int]:
    if not 1 <= length <= 1000:
        raise ValueError("pending run length must be between 1 and 1000")
    if length <= 5:
        return [length]
    batch_count = (length + 3) // 4
    smaller_size, larger_batches = divmod(length, batch_count)
    return [smaller_size + 1] * larger_batches + [
        smaller_size
    ] * (batch_count - larger_batches)


def plan_full_deck_batches(
    pages: FullDeckPlan | Sequence[FullDeckPageSlot | dict[str, Any]],
) -> FullDeckBatchPlan:
    """Split pending contiguous runs using ``balanced-3-4-v1``.

    Ready pages terminate a run. Runs of at most five pages stay together; longer
    runs are distributed across the minimum number of batches whose sizes differ
    by at most one, with larger batches first.
    """

    plan = _full_deck_plan(pages)
    batches: list[PlannedFullDeckBatch] = []
    for run in _pending_runs(plan.pages):
        offset = 0
        for size in _balanced_batch_sizes(len(run)):
            target = run[offset:offset + size]
            batches.append(PlannedFullDeckBatch(
                batch_index=len(batches) + 1,
                slot_ids=[page.slot_id for page in target],
                source_slide_numbers=[
                    page.outline_ref.source_slide_number
                    for page in target
                    if page.outline_ref is not None
                ],
            ))
            offset += size
    if not batches:
        raise ValueError("full-deck plan has no pending pages to batch")
    return FullDeckBatchPlan(batches=batches)


def project_full_deck_generation_pages(
    session_id: str,
    pages: FullDeckPlan | Sequence[FullDeckPageSlot | dict[str, Any]],
    batch_plan: FullDeckBatchPlan,
) -> list[FullDeckGenerationPage]:
    """Create the immutable initial page projection for one generation session."""

    plan = _full_deck_plan(pages)
    batch_by_slot = {
        slot_id: batch.batch_index
        for batch in batch_plan.batches
        for slot_id in batch.slot_ids
    }
    planned_number_by_slot = {
        slot_id: source_slide_number
        for batch in batch_plan.batches
        for slot_id, source_slide_number in zip(
            batch.slot_ids,
            batch.source_slide_numbers,
            strict=True,
        )
    }
    pending_slot_ids = {page.slot_id for page in plan.pages if page.status == "pending"}
    if set(batch_by_slot) != pending_slot_ids:
        raise ValueError("batch plan does not exactly cover the pending full-deck pages")

    projected: list[FullDeckGenerationPage] = []
    for page in plan.pages:
        if page.outline_ref is None:
            raise ValueError(
                f"full-deck slot has no source slide number: {page.slot_id}"
            )
        if page.status == "pending":
            if planned_number_by_slot[page.slot_id] != page.outline_ref.source_slide_number:
                raise ValueError(
                    "batch plan source slide numbers do not match the full-deck plan"
                )
            projected.append(FullDeckGenerationPage(
                session_id=session_id,
                position=page.position,
                slot_id=page.slot_id,
                source_slide_number=page.outline_ref.source_slide_number,
                title=page.title,
                generation_status="queued",
                batch_index=batch_by_slot[page.slot_id],
                source_type="pending",
            ))
            continue
        if page.source_type != "approved_sample" or page.content_ref is None:
            raise ValueError(
                "ready generation baselines must come from an approved sample"
            )
        projected.append(FullDeckGenerationPage(
            session_id=session_id,
            position=page.position,
            slot_id=page.slot_id,
            source_slide_number=page.outline_ref.source_slide_number,
            title=page.title,
            generation_status="sample_ready",
            batch_index=None,
            source_type="approved_sample",
            content_ref=FullDeckGenerationContentRef(
                revision_hash=page.content_ref.revision_hash,
                package_hash=page.content_ref.package_hash,
                slide_id=page.content_ref.slide_id,
                slide_content_hash=page.content_ref.slide_content_hash,
            ),
        ))
    return projected


def project_succeeded_full_deck_batch_pages(
    pages: Sequence[FullDeckGenerationPage | dict[str, Any]],
    batch_index: int,
    page_content_refs: Mapping[
        str,
        FullDeckGenerationContentRef | dict[str, Any],
    ],
) -> list[FullDeckGenerationPage]:
    """Project one running batch as ready before its atomic storage commit."""

    projected = [FullDeckGenerationPage.model_validate(page) for page in pages]
    references = {
        slot_id: FullDeckGenerationContentRef.model_validate(reference)
        for slot_id, reference in page_content_refs.items()
    }
    targets = [page for page in projected if page.batch_index == batch_index]
    if not targets or {page.slot_id for page in targets} != set(references):
        raise ValueError("page content references do not exactly cover the batch")
    if any(page.generation_status != "generating" for page in targets):
        raise ValueError("only generating pages can be projected as succeeded")
    package_identities = {
        (reference.package_id, reference.package_hash)
        for reference in references.values()
    }
    if len(package_identities) != 1 or next(iter(package_identities))[0] is None:
        raise ValueError("a succeeded batch must reference one segment package")

    result: list[FullDeckGenerationPage] = []
    for page in projected:
        reference = references.get(page.slot_id)
        if reference is None:
            result.append(page)
            continue
        result.append(FullDeckGenerationPage.model_validate({
            **page.model_dump(mode="json"),
            "generation_status": "ready",
            "source_type": "generated_segment",
            "content_ref": reference.model_dump(mode="json"),
            "error": None,
        }))
    return result


def _source_id(source_identity: str) -> str:
    digest = sha256(source_identity.encode("utf-8")).hexdigest()[:20]
    return f"preview_source_{digest}"


def compose_partial_full_deck_preview(
    *,
    session_id: str,
    batch_index: int,
    title: str,
    pages: Sequence[FullDeckGenerationPage | dict[str, Any]],
    source_packages: Mapping[str, HtmlPptPackage | dict[str, Any]],
    package_id: str | None = None,
) -> FullDeckGenerationPackage:
    """Compose only durable sample and generated pages, ordered by plan position."""

    projected = sorted(
        (FullDeckGenerationPage.model_validate(page) for page in pages),
        key=lambda page: page.position,
    )
    if any(page.session_id != session_id for page in projected):
        raise ValueError("partial preview pages belong to another generation session")
    if [page.position for page in projected] != list(range(len(projected))):
        raise ValueError("partial preview page positions must be ordered and contiguous")
    ready_pages = [
        page
        for page in projected
        if page.generation_status in {"sample_ready", "ready"}
    ]
    if not ready_pages:
        raise ValueError("partial preview requires at least one ready page")

    packages = {
        identity: HtmlPptPackage.model_validate(package)
        for identity, package in source_packages.items()
    }
    composer_sources: dict[str, ComposerSource] = {}
    composer_pages: list[ComposerPage] = []
    expected_hashes: list[str] = []
    for page in ready_pages:
        reference = page.content_ref
        if reference is None or page.source_slide_number is None:
            raise ValueError("ready preview pages require source content and slide numbers")
        identity = reference.revision_hash or reference.package_id
        if identity is None:
            raise ValueError("ready preview page has no immutable source identity")
        package = packages.get(identity)
        if package is None:
            raise ValueError(f"partial preview source package is missing: {identity}")
        if package.package_hash != reference.package_hash:
            raise ValueError(f"partial preview source package hash changed: {identity}")
        source_id = _source_id(identity)
        composer_sources.setdefault(
            source_id,
            ComposerSource(source_id=source_id, package=package),
        )
        composer_pages.append(ComposerPage(
            slot_id=page.slot_id,
            slide_id=page.slot_id,
            title=page.title,
            source_slide_number=page.source_slide_number,
            source_id=source_id,
            source_slide_id=reference.slide_id,
        ))
        expected_hashes.append(reference.slide_content_hash)

    composition = compose_full_deck(FullDeckComposerInput(
        title=title,
        sources=list(composer_sources.values()),
        pages=composer_pages,
    ))
    if [slide.slot_id for slide in composition.manifest.slides] != [
        page.slot_id for page in ready_pages
    ]:
        raise FullDeckComposerError("partial preview changed the ready page order")
    if [slide.source_slide_number for slide in composition.manifest.slides] != [
        page.source_slide_number for page in ready_pages
    ]:
        raise FullDeckComposerError("partial preview changed the source slide order")
    for slide, expected_hash in zip(
        composition.manifest.slides,
        expected_hashes,
        strict=True,
    ):
        if (
            slide.source_slide_content_hash != expected_hash
            or slide.composed_slide_content_hash != expected_hash
        ):
            raise FullDeckComposerError(
                f"partial preview page content graph changed: {slide.slot_id}"
            )

    package_value: dict[str, Any] = {
        **composition.package.model_dump(mode="json"),
        "session_id": session_id,
        "batch_index": batch_index,
        "kind": "preview",
        "composition_manifest": composition.manifest.model_dump(mode="json"),
    }
    if package_id is not None:
        package_value["package_id"] = package_id
    return FullDeckGenerationPackage.model_validate(package_value)
