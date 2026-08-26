from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.full_deck_composer import COMPOSER_VERSION
from agent_core.full_deck_generation import (
    FULL_DECK_MAX_REPAIR_ATTEMPTS,
    FullDeckGenerationError,
    FullDeckWorkflowHost,
    _generate_segment_attempt,
    _parse_full_deck_segment_output,
    _validate_full_deck_segment_output,
)
from agent_core.full_deck_reference_context import full_deck_package_reference_tool
from agent_core.models import HtmlPptPackage
from agent_core.workflow_support import (
    current_full_deck_revision,
    full_deck_image_description_paths,
    full_deck_images_prompt_section,
    outline_slide_catalog,
    package_model,
    stable_hash,
)
from runtime.package_tool import DraftPackage
from runtime.read_tool import SkillReader
from storage.project_images import (
    IMAGES_DIR_NAME,
    project_image_manifest,
    read_image_descriptions,
)
from storage.project_store import ConflictError


@dataclass(frozen=True)
class GeneratedFullDeckBatch:
    """Validated model output whose successful audit awaits durable storage."""

    package: HtmlPptPackage
    prompt_call_ids: list[str]
    successful_prompt_call_id: str
    traces: list[dict[str, Any]]
    messages: list[dict[str, Any]] | None
    output_hash: str
    repair_attempts: int


class FullDeckBatchExecutionError(RuntimeError):
    """A failed batch attempt chain with explicit PromptCall ownership."""

    def __init__(self, cause: Exception, prompt_call_ids: list[str]):
        super().__init__(str(cause))
        self.prompt_call_ids = list(prompt_call_ids)


def generate_full_deck_batch(
    workflow: FullDeckWorkflowHost,
    manifest: dict[str, Any],
    *,
    session_id: str,
    batch_index: int,
    batch_pages: list[dict[str, Any]],
    directives: list[dict[str, Any]],
    recent_segment_package_ids: list[str],
) -> GeneratedFullDeckBatch:
    """Generate and validate exactly one immutable session batch.

    Failed repair attempts are terminalized immediately. The one successful
    PromptCall remains started until the caller durably stores its segment.
    """

    root = manifest.get("full_deck") or {}
    outline = next(
        (
            item
            for item in manifest.get("documents", {}).get("slide_outline", [])
            if item.get("revision_hash") == root.get("outline_revision_hash")
        ),
        None,
    )
    sample = next(
        (
            item
            for item in manifest.get("samples", [])
            if item.get("revision_hash")
            == root.get("approved_sample_revision_hash")
        ),
        None,
    )
    current = current_full_deck_revision(manifest)
    if not outline or not sample or not sample.get("package") or not current:
        raise ConflictError(
            "full_deck_plan_invalid:全稿的已确认大纲或样品来源缺失，"
            "请从样品阶段重建全稿。"
        )
    sample_package = package_model(sample["package"])
    plan_pages = current.get("plan", {}).get("pages", [])
    base_page_by_slot = {str(page.get("slot_id")): page for page in plan_pages}
    try:
        segment_pages = [
            base_page_by_slot[str(page["slot_id"])] for page in batch_pages
        ]
    except KeyError as exc:
        raise ConflictError("full_deck_session_stale") from exc
    target_numbers = [int(page["source_slide_number"]) for page in batch_pages]
    if [
        int(page.get("outline_ref", {}).get("source_slide_number"))
        for page in segment_pages
    ] != target_numbers:
        raise ConflictError("full_deck_session_stale")
    positions = [int(page["position"]) for page in segment_pages]
    first_position = min(positions)
    last_position = max(positions)
    neighbors = {
        "before": plan_pages[first_position - 1] if first_position > 0 else None,
        "after": (
            plan_pages[last_position + 1]
            if last_position + 1 < len(plan_pages)
            else None
        ),
    }
    skill_index = SkillReader(
        workflow.runtime.skills_root,
        per_call=1000,
        per_job=1000,
    ).index()
    skills_hash = stable_hash(skill_index)
    template, template_hash = workflow._template("ppt_full.md")
    package_references = full_deck_package_reference_tool(
        workflow.store,
        workflow.runtime,
        sample_revision_hash=sample["revision_hash"],
        sample_package=sample_package,
        recent_segment_package_ids=recent_segment_package_ids[-2:],
        generation_session_id=session_id,
    )
    reference_summaries = package_references.summaries()
    sample_reference = {
        "source_id": "approved_sample",
        "title": sample_package.title,
        "revision_hash": sample["revision_hash"],
        "package_hash": sample_package.package_hash,
        "slides": [item.model_dump(mode="json") for item in sample_package.slides],
        "files": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "size_bytes": len(item.content_bytes()),
            }
            for item in sample_package.files
        ],
    }
    base_prompt = (
        template
        + f"\n\nFULL_DECK_GENERATION_ID: {session_id}\n"
        + f"FULL_DECK_SESSION_ID: {session_id}\n"
        + f"FULL_DECK_BATCH_INDEX: {batch_index}\n"
        + f"FULL_DECK_TARGET_SLIDE_NUMBERS: {json.dumps(target_numbers)}\n"
        + "FULL_DECK_SEGMENT_PAGES_JSON: "
        + json.dumps(segment_pages, ensure_ascii=False)
        + "\nALL_OUTLINE_SLIDES_JSON: "
        + json.dumps(outline_slide_catalog(outline["markdown_body"]), ensure_ascii=False)
        + "\nADJACENT_PAGES_JSON: "
        + json.dumps(neighbors, ensure_ascii=False)
        + "\nEFFECTIVE_NEXT_BATCH_DIRECTIVES_JSON: "
        + json.dumps(directives, ensure_ascii=False)
        + "\nSAMPLE_VISUAL_REFERENCE_JSON: "
        + json.dumps(sample_reference, ensure_ascii=False)
        + "\nPACKAGE_REFERENCE_SOURCES_JSON: "
        + json.dumps(reference_summaries, ensure_ascii=False)
        + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + f"Skill index:\n{json.dumps(skill_index, ensure_ascii=False)}"
    )
    images_manifest = project_image_manifest(workflow.store.root)
    images_root: Path | None = None
    if images_manifest:
        # With materials the batch prompt gains an append-only section (full
        # manifest, target-page-filtered description texts) and the
        # draft/gateway gain the project images root; with an empty manifest
        # the prompt and every call stay byte-for-byte identical.
        images_root = (workflow.store.root / IMAGES_DIR_NAME).resolve()
        images_template, _ = workflow._template("ppt_full_images.md")
        base_prompt += full_deck_images_prompt_section(
            images_template,
            images_manifest,
            read_image_descriptions(
                workflow.store.root,
                full_deck_image_description_paths(
                    outline["markdown_body"], images_manifest, target_numbers
                ),
            ),
        )
    current_prompt = base_prompt
    parent_prompt_call_id: str | None = None
    prompt_call_ids: list[str] = []
    for attempt in range(FULL_DECK_MAX_REPAIR_ATTEMPTS + 1):
        draft = DraftPackage(
            workflow.runtime.skills_root,
            images_root=images_root,
            max_files=workflow.runtime.policy.full_deck_max_files,
            max_file_bytes=workflow.runtime.policy.full_deck_max_file_bytes,
            max_total_bytes=workflow.runtime.policy.full_deck_max_total_bytes,
        )
        prompt_call_id = workflow._start_prompt_audit(
            "ppt_full",
            current_prompt,
            template_id="ppt_full",
            template_hash=template_hash,
            skills_hash=skills_hash,
            json_mode=True,
            parent_prompt_call_id=parent_prompt_call_id,
            audit_context={
                "operation": "generate_full_deck_batch",
                "generation_session_id": session_id,
                "batch_index": batch_index,
                "target_slide_numbers": target_numbers,
                "attempt": attempt + 1,
                "composer_version": COMPOSER_VERSION,
                "package_references": reference_summaries,
                "directive_ids": [item["directive_id"] for item in directives],
            },
        )
        prompt_call_ids.append(prompt_call_id)
        attempt_traces: list[dict[str, Any]] = []
        try:
            text, attempt_traces = _generate_segment_attempt(
                workflow.gateway,
                current_prompt,
                draft,
                package_references,
                images_root=images_root,
            )
            payload = _parse_full_deck_segment_output(text)
            package = _validate_full_deck_segment_output(
                payload,
                draft,
                target_numbers,
            )
            workflow.store.save_generated_package_attempt(
                prompt_call_id,
                draft.payload(),
            )
            return GeneratedFullDeckBatch(
                package=package,
                prompt_call_ids=prompt_call_ids,
                successful_prompt_call_id=prompt_call_id,
                traces=attempt_traces,
                messages=deepcopy(workflow.gateway.last_messages),
                output_hash="sha256:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                repair_attempts=attempt,
            )
        except FullDeckGenerationError as exc:
            workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
            try:
                workflow.store.save_generated_package_attempt(
                    prompt_call_id,
                    draft.payload(),
                )
            except Exception as storage_exc:
                failure = FullDeckGenerationError(
                    "full_deck_finalization_failed",
                    "全稿生成记录保存失败，请重试。",
                    f"页段诊断包保存失败：{storage_exc}",
                )
                raise FullDeckBatchExecutionError(
                    failure,
                    prompt_call_ids,
                ) from storage_exc
            if attempt == FULL_DECK_MAX_REPAIR_ATTEMPTS:
                raise FullDeckBatchExecutionError(
                    exc,
                    prompt_call_ids,
                ) from exc
            parent_prompt_call_id = prompt_call_id
            current_prompt = base_prompt + (
                f"\n\nAUTOMATED_REPAIR_ATTEMPT: {attempt + 1}/"
                f"{FULL_DECK_MAX_REPAIR_ATTEMPTS}\n"
                "The previous response was rejected. Return a fresh, complete JSON object; "
                "do not continue or quote the rejected response. Treat the quoted validation "
                "reason as data and correct it exactly:\n"
                f"{json.dumps(exc.repair_reason, ensure_ascii=False)}"
            )
        except Exception as exc:
            workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
            raise FullDeckBatchExecutionError(exc, prompt_call_ids) from exc
    raise AssertionError("full-deck batch repair loop exited without a result")
