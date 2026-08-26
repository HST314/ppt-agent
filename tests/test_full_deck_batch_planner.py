from __future__ import annotations

from collections.abc import Sequence

import pytest

from agent_core.full_deck_batching import (
    FULL_DECK_BATCH_PLANNER_VERSION,
    FullDeckBatchPlan,
    PlannedFullDeckBatch,
    compose_partial_full_deck_preview,
    plan_full_deck_batches,
    project_full_deck_generation_pages,
    project_succeeded_full_deck_batch_pages,
)
from agent_core.full_deck_composer import normalized_page_content_graph
from agent_core.models import (
    FullDeckGenerationContentRef,
    FullDeckGenerationPage,
    HtmlPptPackage,
)


SESSION_ID = "fullsession_" + "1" * 32
SAMPLE_REVISION_HASH = "sha256:" + "2" * 64
OUTLINE_REVISION_HASH = "sha256:" + "3" * 64


def _package(label: str, slide_numbers: Sequence[int]) -> HtmlPptPackage:
    slides = [
        {
            "slide_id": f"{label}-{number}",
            "title": f"第 {number} 页",
            "source_slide_number": number,
        }
        for number in slide_numbers
    ]
    sections = "".join(
        f'<section class="slide" data-slide-id="{slide["slide_id"]}">'
        f'<h1>{slide["title"]}</h1></section>'
        for slide in slides
    )
    return HtmlPptPackage.model_validate({
        "title": label,
        "slide_count": len(slides),
        "slides": slides,
        "files": [
            {"path": "index.html", "content": f"<!doctype html><body>{sections}</body>"},
            {"path": "assets/theme.css", "content": f"body{{--deck:{label}}}"},
        ],
    })


def _plan_pages(
    total: int,
    *,
    ready_numbers: set[int] | None = None,
) -> tuple[list[dict], HtmlPptPackage | None]:
    ready = ready_numbers or set()
    sample = _package("sample", sorted(ready)) if ready else None
    pages: list[dict] = []
    for number in range(1, total + 1):
        value = {
            "slot_id": f"slot_{number:024x}",
            "position": number - 1,
            "outline_ref": {
                "outline_revision_hash": OUTLINE_REVISION_HASH,
                "source_slide_number": number,
            },
            "title": f"第 {number} 页",
            "status": "pending",
            "source_type": "pending",
        }
        if number in ready:
            assert sample is not None
            slide_id = f"sample-{number}"
            value.update(
                status="ready",
                source_type="approved_sample",
                content_ref={
                    "revision_hash": SAMPLE_REVISION_HASH,
                    "package_hash": sample.package_hash,
                    "slide_id": slide_id,
                    "slide_content_hash": normalized_page_content_graph(
                        sample, slide_id
                    ).content_hash,
                },
                derived_from={
                    "sample_revision_hash": SAMPLE_REVISION_HASH,
                    "sample_slide_id": slide_id,
                },
            )
        pages.append(value)
    return pages, sample


@pytest.mark.parametrize("length", range(1, 21))
def test_balanced_planner_covers_pending_run_lengths_one_through_twenty(
    length: int,
) -> None:
    pages, _ = _plan_pages(length)

    plan = plan_full_deck_batches(pages)

    sizes = [len(batch.slot_ids) for batch in plan.batches]
    assert plan.planner_version == FULL_DECK_BATCH_PLANNER_VERSION
    assert sum(sizes) == length
    if length <= 5:
        assert sizes == [length]
    else:
        assert set(sizes).issubset({3, 4})
        assert sizes == sorted(sizes, reverse=True)
        assert max(sizes) - min(sizes) <= 1


def test_fourteen_pending_pages_have_the_fixed_four_four_three_three_plan() -> None:
    pages, _ = _plan_pages(16, ready_numbers={1, 2})

    first = plan_full_deck_batches(pages)
    second = plan_full_deck_batches(pages)

    assert first == second
    assert [batch.source_slide_numbers for batch in first.batches] == [
        [3, 4, 5, 6],
        [7, 8, 9, 10],
        [11, 12, 13],
        [14, 15, 16],
    ]
    assert [batch.batch_index for batch in first.batches] == [1, 2, 3, 4]


def test_ready_pages_and_outline_gaps_split_pending_runs() -> None:
    pages, _ = _plan_pages(12, ready_numbers={4, 5, 9})
    pages[10]["outline_ref"]["source_slide_number"] = 20

    plan = plan_full_deck_batches(pages)

    assert [batch.source_slide_numbers for batch in plan.batches] == [
        [1, 2, 3],
        [6, 7, 8],
        [10],
        [20],
        [12],
    ]


def test_page_projection_rejects_a_plan_with_mismatched_slide_numbers() -> None:
    pages, _ = _plan_pages(6, ready_numbers={1})
    batch_plan = plan_full_deck_batches(pages)
    wrong_plan = FullDeckBatchPlan(batches=[
        PlannedFullDeckBatch(
            batch_index=1,
            slot_ids=batch_plan.batches[0].slot_ids,
            source_slide_numbers=[102, 103, 104, 105, 106],
        )
    ])

    with pytest.raises(ValueError, match="do not match"):
        project_full_deck_generation_pages(SESSION_ID, pages, wrong_plan)


def _mark_batch_generating(
    pages: Sequence[FullDeckGenerationPage],
    batch_index: int,
) -> list[FullDeckGenerationPage]:
    return [
        FullDeckGenerationPage.model_validate({
            **page.model_dump(mode="json"),
            "generation_status": "generating",
        })
        if page.batch_index == batch_index
        else page
        for page in pages
    ]


def _segment_refs(
    package_id: str,
    package: HtmlPptPackage,
    pages: Sequence[FullDeckGenerationPage],
    batch_index: int,
) -> dict[str, FullDeckGenerationContentRef]:
    slide_by_number = {
        slide.source_slide_number: slide for slide in package.slides
    }
    result: dict[str, FullDeckGenerationContentRef] = {}
    for page in pages:
        if page.batch_index != batch_index:
            continue
        slide = slide_by_number[page.source_slide_number]
        result[page.slot_id] = FullDeckGenerationContentRef(
            package_id=package_id,
            package_hash=package.package_hash,
            slide_id=slide.slide_id,
            slide_content_hash=normalized_page_content_graph(
                package, slide.slide_id
            ).content_hash,
        )
    return result


def test_projection_and_partial_preview_include_only_ready_pages_in_plan_order() -> None:
    plan_pages, sample = _plan_pages(16, ready_numbers={1, 2})
    assert sample is not None
    batch_plan = plan_full_deck_batches(plan_pages)
    pages = project_full_deck_generation_pages(
        SESSION_ID,
        plan_pages,
        batch_plan,
    )

    assert [page.generation_status for page in pages[:3]] == [
        "sample_ready",
        "sample_ready",
        "queued",
    ]
    assert [page.batch_index for page in pages[2:]] == [
        1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4,
    ]

    segment_one = _package("segment-one", range(3, 7))
    pages = _mark_batch_generating(pages, 1)
    pages = project_succeeded_full_deck_batch_pages(
        pages,
        1,
        _segment_refs("segment_one", segment_one, pages, 1),
    )
    preview_one = compose_partial_full_deck_preview(
        session_id=SESSION_ID,
        batch_index=1,
        title="部分全稿",
        pages=list(reversed(pages)),
        source_packages={
            SAMPLE_REVISION_HASH: sample,
            "segment_one": segment_one,
        },
        package_id="preview_one",
    )

    assert preview_one.kind == "preview"
    assert preview_one.slide_count == 6
    assert [slide.source_slide_number for slide in preview_one.slides] == list(
        range(1, 7)
    )
    assert [
        slide["slot_id"] for slide in preview_one.composition_manifest["slides"]
    ] == [f"slot_{number:024x}" for number in range(1, 7)]
    assert all(
        slide["source_slide_content_hash"]
        == slide["composed_slide_content_hash"]
        for slide in preview_one.composition_manifest["slides"]
    )

    segment_two = _package("segment-two", range(7, 11))
    pages = _mark_batch_generating(pages, 2)
    pages = project_succeeded_full_deck_batch_pages(
        pages,
        2,
        _segment_refs("segment_two", segment_two, pages, 2),
    )
    preview_two = compose_partial_full_deck_preview(
        session_id=SESSION_ID,
        batch_index=2,
        title="部分全稿",
        pages=pages,
        source_packages={
            SAMPLE_REVISION_HASH: sample,
            "segment_one": segment_one,
            "segment_two": segment_two,
        },
        package_id="preview_two",
    )

    assert preview_two.slide_count == 10
    assert [slide.source_slide_number for slide in preview_two.slides] == list(
        range(1, 11)
    )


def test_partial_preview_rejects_changed_source_content() -> None:
    plan_pages, sample = _plan_pages(3, ready_numbers={1})
    assert sample is not None
    pages = project_full_deck_generation_pages(
        SESSION_ID,
        plan_pages,
        plan_full_deck_batches(plan_pages),
    )
    changed = _package("changed", [1])

    with pytest.raises(ValueError, match="package hash changed"):
        compose_partial_full_deck_preview(
            session_id=SESSION_ID,
            batch_index=1,
            title="不可信预览",
            pages=pages,
            source_packages={SAMPLE_REVISION_HASH: changed},
        )


def test_succeeded_projection_rejects_content_from_multiple_segment_packages() -> None:
    plan_pages, _ = _plan_pages(3)
    pages = project_full_deck_generation_pages(
        SESSION_ID,
        plan_pages,
        plan_full_deck_batches(plan_pages),
    )
    pages = _mark_batch_generating(pages, 1)
    segment = _package("segment", range(1, 4))
    references = _segment_refs("segment_one", segment, pages, 1)
    references[pages[1].slot_id] = references[pages[1].slot_id].model_copy(
        update={"package_id": "segment_two"}
    )

    with pytest.raises(ValueError, match="one segment package"):
        project_succeeded_full_deck_batch_pages(pages, 1, references)
