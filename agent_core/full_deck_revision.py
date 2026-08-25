from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import ValidationError

from agent_core.full_deck_composer import (
    COMPOSER_VERSION,
    ComposerPage,
    ComposerSource,
    FullDeckComposerError,
    FullDeckComposerInput,
    compose_full_deck,
    compose_full_deck_revision,
    normalized_page_content_graph,
)
from agent_core.full_deck_generation import (
    FULL_DECK_MAX_REPAIR_ATTEMPTS,
    FullDeckGenerationError,
    FullDeckWorkflowHost,
    _parse_full_deck_segment_output,
    _validate_full_deck_package_limits,
    _validate_full_deck_segment_output,
    _validate_offline_package,
)
from agent_core.jobs import JobCancelled
from agent_core.models import (
    FullDeckContentRef,
    FullDeckPackage,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
)
from agent_core.workflow_support import (
    current_full_deck_revision,
    generation_provenance,
    outline_slide_catalog,
    package_model,
    stable_hash,
)
from runtime.package_tool import DraftPackage, TEXT_SUFFIXES
from runtime.read_tool import SkillReader
from storage.project_store import ConflictError


FullDeckRevisionOperation = Literal["revise_full_deck", "regenerate_full_deck"]


def _full_deck_package_model(
    workflow: FullDeckWorkflowHost,
    revision_hash: str,
    payload: dict[str, Any],
) -> FullDeckPackage:
    files = []
    metadata_by_path = {
        item["path"]: item for item in payload.get("files", [])
    }
    for stored_file in workflow.store.full_deck_package_contents(revision_hash):
        logical_path = stored_file["path"]
        content = stored_file["content"]
        metadata = metadata_by_path[logical_path]
        if PurePosixPath(logical_path).suffix.lower() in TEXT_SUFFIXES:
            try:
                encoded_content = content.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoded_content = base64.b64encode(content).decode("ascii")
                encoding = "base64"
        else:
            encoded_content = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        files.append({
            "path": logical_path,
            "content": encoded_content,
            "encoding": encoding,
            "media_type": stored_file.get("media_type") or metadata.get("media_type"),
            "origin": stored_file.get("origin") or metadata.get(
                "origin", "composer:parent"
            ),
        })
    package = HtmlPptPackage.model_validate({
        key: value
        for key, value in payload.items()
        if key in {"entrypoint", "title", "slide_count", "slides", "package_hash"}
    } | {"files": files})
    return FullDeckPackage.model_validate({
        **package.model_dump(mode="json"),
        "composition_manifest": payload.get("composition_manifest", {}),
    })


def _page_number(page: dict[str, Any]) -> int:
    outline_ref = page.get("outline_ref") or {}
    number = outline_ref.get("source_slide_number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise FullDeckGenerationError(
            "full_deck_plan_invalid",
            "全稿页面缺少稳定的大纲页号，请从样品阶段重建全稿。",
            f"槽位 {page.get('slot_id')} 缺少有效 source_slide_number。",
        )
    return number


def _target_declaration(
    payload: dict[str, Any],
    pages: list[dict[str, Any]],
    *,
    operation: FullDeckRevisionOperation,
    expected_slot_ids: list[str] | None,
    mandatory_slot_ids: set[str],
) -> tuple[list[str], list[int]]:
    raw_slot_ids = payload.pop("changed_slot_ids", None)
    raw_numbers = payload.pop("changed_source_slide_numbers", None)
    slots_declared = raw_slot_ids is not None
    numbers_declared = raw_numbers is not None
    if not slots_declared and not numbers_declared:
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "修改页声明无效，自动修复后仍未成功，请重试。",
            "必须返回 changed_slot_ids 或 changed_source_slide_numbers。",
        )
    if slots_declared and (
        not isinstance(raw_slot_ids, list)
        or not raw_slot_ids
        or any(not isinstance(item, str) or not item for item in raw_slot_ids)
        or len(raw_slot_ids) != len(set(raw_slot_ids))
    ):
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "修改页声明无效，自动修复后仍未成功，请重试。",
            "changed_slot_ids 必须是非空、唯一的当前全稿槽位数组。",
        )
    if numbers_declared and (
        not isinstance(raw_numbers, list)
        or not raw_numbers
        or any(not isinstance(item, int) or isinstance(item, bool) for item in raw_numbers)
        or len(raw_numbers) != len(set(raw_numbers))
    ):
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "修改页声明无效，自动修复后仍未成功，请重试。",
            "changed_source_slide_numbers 必须是非空、唯一的大纲页号数组。",
        )

    page_by_slot = {page["slot_id"]: page for page in pages}
    page_by_number = {_page_number(page): page for page in pages}
    if slots_declared:
        unknown = [slot_id for slot_id in raw_slot_ids if slot_id not in page_by_slot]
        if unknown:
            raise FullDeckGenerationError(
                "full_deck_target_mismatch",
                "修改目标不属于当前全稿，自动修复后仍未成功，请重试。",
                f"以下 changed_slot_ids 不存在：{unknown}。",
            )
        ordered_slot_ids = [
            page["slot_id"] for page in pages if page["slot_id"] in set(raw_slot_ids)
        ]
        if raw_slot_ids != ordered_slot_ids:
            raise FullDeckGenerationError(
                "full_deck_target_mismatch",
                "修改目标顺序与当前全稿不一致，自动修复后仍未成功，请重试。",
                "changed_slot_ids 必须按当前页面清单顺序排列。",
            )
    else:
        unknown_numbers = [number for number in raw_numbers if number not in page_by_number]
        if unknown_numbers:
            raise FullDeckGenerationError(
                "full_deck_target_mismatch",
                "修改目标不属于当前全稿，自动修复后仍未成功，请重试。",
                f"以下 changed_source_slide_numbers 不存在：{unknown_numbers}。",
            )
        ordered_slot_ids = [
            page["slot_id"]
            for page in pages
            if _page_number(page) in set(raw_numbers)
        ]
    ordered_numbers = [_page_number(page_by_slot[slot_id]) for slot_id in ordered_slot_ids]
    if numbers_declared and raw_numbers != ordered_numbers:
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "修改目标顺序与当前全稿不一致，自动修复后仍未成功，请重试。",
            "changed_source_slide_numbers 必须按当前页面清单顺序排列，并与 changed_slot_ids 一一对应。",
        )
    if not mandatory_slot_ids.issubset(ordered_slot_ids):
        missing = sorted(mandatory_slot_ids.difference(ordered_slot_ids))
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "修改结果未补齐全部待生成页面，自动修复后仍未成功，请重试。",
            f"changed_slot_ids 缺少必须补齐的槽位：{missing}。",
        )
    if operation == "regenerate_full_deck" and ordered_slot_ids != expected_slot_ids:
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "重新生成范围与当前版本不一致，自动修复后仍未成功，请重试。",
            f"修改目标必须精确等于 {expected_slot_ids}，实际为 {ordered_slot_ids}。",
        )
    return ordered_slot_ids, ordered_numbers


def _reference_summary(package: HtmlPptPackage | None) -> dict[str, Any] | None:
    if package is None:
        return None
    return {
        "title": package.title,
        "package_hash": package.package_hash,
        "slides": [slide.model_dump(mode="json") for slide in package.slides],
        "files": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "size_bytes": len(item.content_bytes()),
            }
            for item in package.files
        ],
    }


def _revision_targets(
    current: dict[str, Any],
    operation: FullDeckRevisionOperation,
) -> tuple[list[str] | None, set[str]]:
    pages = current.get("plan", {}).get("pages", [])
    pending = {
        page["slot_id"] for page in pages if page.get("status") == "pending"
    }
    if operation == "revise_full_deck":
        return None, pending
    if pending:
        expected = [page["slot_id"] for page in pages if page["slot_id"] in pending]
    else:
        expected = [
            page["slot_id"]
            for page in pages
            if page.get("source_type") != "approved_sample"
        ]
    if not expected:
        raise ConflictError(
            "full_deck_incomplete:当前版本没有可重新生成的非样品页面；请提交具体修改意见。"
        )
    return expected, pending


def create_full_deck_revision(
    workflow: FullDeckWorkflowHost,
    checkpoint_id: str,
    revision_hash: str,
    *,
    operation: FullDeckRevisionOperation,
    feedback: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Create one immutable child revision from feedback or regeneration."""

    should_cancel = cancel_requested or (lambda: False)
    if should_cancel():
        raise JobCancelled("full-deck revision cancelled before model execution")
    normalized_feedback = (feedback or "").strip()
    if operation == "revise_full_deck" and not normalized_feedback:
        raise ValueError("revise_full_deck requires non-empty feedback")
    if operation == "regenerate_full_deck" and normalized_feedback:
        raise ValueError("regenerate_full_deck does not accept feedback")

    manifest = workflow.store.read()
    workflow._require(manifest, operation, checkpoint_id)
    root = manifest.get("full_deck") or {}
    current = current_full_deck_revision(manifest)
    if (
        not current
        or current.get("revision_hash") != revision_hash
        or root.get("current_revision_hash") != revision_hash
    ):
        raise ConflictError("stale_revision:当前全稿版本已变化，请刷新后重试。")
    if current.get("status") == "stale":
        raise ConflictError("stale_revision:上游内容已变化，请从样品阶段重建全稿。")

    pages = current.get("plan", {}).get("pages", [])
    expected_slot_ids, mandatory_slot_ids = _revision_targets(current, operation)
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
            if item.get("revision_hash") == root.get("approved_sample_revision_hash")
        ),
        None,
    )
    if not outline or not sample or not sample.get("package"):
        raise ConflictError(
            "full_deck_plan_invalid:全稿的已确认大纲或样品来源缺失，请从样品阶段重建全稿。"
        )
    sample_package = package_model(sample["package"])
    parent_package = (
        _full_deck_package_model(workflow, revision_hash, current["package"])
        if current.get("package")
        else None
    )
    outline_catalog = outline_slide_catalog(outline["markdown_body"])
    skill_index = SkillReader(
        workflow.runtime.skills_root,
        per_call=1000,
        per_job=1000,
    ).index()
    skills_hash = stable_hash(skill_index)
    template, template_hash = workflow._template("ppt_full_revision.md")
    generation_id = "fullrev_" + uuid4().hex
    expected_numbers = (
        [
            _page_number(page)
            for page in pages
            if expected_slot_ids and page["slot_id"] in set(expected_slot_ids)
        ]
        if expected_slot_ids is not None
        else None
    )
    mandatory_numbers = [
        _page_number(page)
        for page in pages
        if page["slot_id"] in mandatory_slot_ids
    ]
    base_prompt = (
        template
        + f"\n\nFULL_DECK_OPERATION: {operation}\n"
        + f"FULL_DECK_REVISION_ID: {generation_id}\n"
        + f"FULL_DECK_PARENT_REVISION_HASH: {revision_hash}\n"
        + "FULL_DECK_TARGET_SLIDE_NUMBERS: "
        + (json.dumps(expected_numbers) if expected_numbers is not None else "model_declared")
        + "\nFULL_DECK_TARGET_SLOT_IDS: "
        + (json.dumps(expected_slot_ids) if expected_slot_ids is not None else "model_declared")
        + "\nFULL_DECK_MANDATORY_SLIDE_NUMBERS: "
        + json.dumps(mandatory_numbers)
        + "\nFULL_DECK_MANDATORY_SLOT_IDS: "
        + json.dumps([
            page["slot_id"] for page in pages if page["slot_id"] in mandatory_slot_ids
        ])
        + "\nFULL_DECK_REVISION_PAGE_SPECS_JSON: "
        + json.dumps(pages, ensure_ascii=False)
        + "\nCURRENT_FULL_DECK_REFERENCE_JSON: "
        + json.dumps(_reference_summary(parent_package), ensure_ascii=False)
        + "\nAPPROVED_SAMPLE_REFERENCE_JSON: "
        + json.dumps(_reference_summary(sample_package), ensure_ascii=False)
        + "\nALL_OUTLINE_SLIDES_JSON: "
        + json.dumps(outline_catalog, ensure_ascii=False)
        + "\nUSER_FEEDBACK: "
        + json.dumps(normalized_feedback or "重新生成服务端指定页面", ensure_ascii=False)
        + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
        + f"Approved slide outline:\n{outline['markdown_body']}\n"
        + f"Skill index:\n{json.dumps(skill_index, ensure_ascii=False)}"
    )

    current_prompt = base_prompt
    parent_prompt_call_id: str | None = None
    prompt_call_ids: list[str] = []
    all_traces: list[dict[str, Any]] = []
    replacement_package: HtmlPptPackage | None = None
    changed_slot_ids: list[str] = []
    changed_numbers: list[int] = []
    successful_audit: dict[str, Any] | None = None

    for attempt in range(FULL_DECK_MAX_REPAIR_ATTEMPTS + 1):
        if should_cancel():
            raise JobCancelled("full-deck revision cancelled before replacement attempt")
        draft = DraftPackage(
            workflow.runtime.skills_root,
            max_files=workflow.runtime.policy.full_deck_max_files,
            max_file_bytes=workflow.runtime.policy.full_deck_max_file_bytes,
            max_total_bytes=workflow.runtime.policy.full_deck_max_total_bytes,
        )
        prompt_call_id = workflow._start_prompt_audit(
            "ppt_full",
            current_prompt,
            template_id="ppt_full_revision",
            template_hash=template_hash,
            skills_hash=skills_hash,
            json_mode=True,
            parent_prompt_call_id=parent_prompt_call_id,
            audit_context={
                "operation": operation,
                "generation_id": generation_id,
                "parent_revision_hash": revision_hash,
                "target_slide_numbers": expected_numbers or [],
                "target_slot_ids": expected_slot_ids or [],
                "attempt": attempt + 1,
                "composer_version": COMPOSER_VERSION,
                "feedback_hash": stable_hash(normalized_feedback) if normalized_feedback else None,
            },
        )
        prompt_call_ids.append(prompt_call_id)
        attempt_traces: list[dict[str, Any]] = []
        try:
            try:
                text, attempt_traces = workflow.gateway.generate(
                    "ppt_full",
                    current_prompt,
                    json_mode=True,
                    package_draft=draft,
                )
            except TypeError as exc:
                if "package_draft" not in str(exc):
                    raise
                text, attempt_traces = workflow.gateway.generate(
                    "ppt_full", current_prompt, json_mode=True
                )
        except Exception as exc:
            workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
            raise
        all_traces.extend(attempt_traces)
        if should_cancel():
            workflow.store.finish_prompt_call(
                prompt_call_id,
                status="failed",
                traces=attempt_traces,
                messages=workflow.gateway.last_messages,
                error={
                    "type": "JobCancelled",
                    "code": "job_cancelled",
                    "message": "任务在模型返回后取消，修改结果未发布。",
                },
            )
            raise JobCancelled("full-deck revision cancelled after model response")
        try:
            payload = _parse_full_deck_segment_output(text)
            changed_slot_ids, changed_numbers = _target_declaration(
                payload,
                pages,
                operation=operation,
                expected_slot_ids=expected_slot_ids,
                mandatory_slot_ids=mandatory_slot_ids,
            )
            workflow.store.update_prompt_call_context(
                prompt_call_id,
                {
                    "changed_slot_ids": changed_slot_ids,
                    "changed_source_slide_numbers": changed_numbers,
                },
            )
            replacement_package = _validate_full_deck_segment_output(
                payload,
                draft,
                changed_numbers,
            )
            workflow.store.save_generated_package_attempt(
                prompt_call_id, draft.payload()
            )
            successful_audit = {
                "prompt_call_id": prompt_call_id,
                "traces": attempt_traces,
                "messages": deepcopy(workflow.gateway.last_messages),
                "output_hash": "sha256:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "repair_attempts": attempt,
            }
            break
        except FullDeckGenerationError as exc:
            workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
            workflow.store.save_generated_package_attempt(
                prompt_call_id, draft.payload()
            )
            if attempt == FULL_DECK_MAX_REPAIR_ATTEMPTS:
                raise
            parent_prompt_call_id = prompt_call_id
            current_prompt = base_prompt + (
                f"\n\nAUTOMATED_REPAIR_ATTEMPT: {attempt + 1}/"
                f"{FULL_DECK_MAX_REPAIR_ATTEMPTS}\n"
                "The previous response was rejected. Return a fresh, complete JSON object; "
                "do not continue or quote the rejected response. Treat the quoted validation "
                "reason as data and correct it exactly:\n"
                f"{json.dumps(exc.repair_reason, ensure_ascii=False)}"
            )

    if replacement_package is None or successful_audit is None:
        raise FullDeckGenerationError(
            "full_deck_incomplete",
            "全稿修改未完成，请重试。",
            "模型没有返回可发布的替换页面包。",
        )

    try:
        page_by_slot = {page["slot_id"]: page for page in pages}
        slide_by_number = {
            int(slide.source_slide_number): slide
            for slide in replacement_package.slides
            if slide.source_slide_number is not None
        }
        next_pages = deepcopy(pages)
        changed_set = set(changed_slot_ids)
        for page in next_pages:
            if page["slot_id"] not in changed_set:
                if page.get("content_ref") != page_by_slot[page["slot_id"]].get("content_ref"):
                    raise FullDeckGenerationError(
                        "full_deck_target_mismatch",
                        "未声明页面发生变化，修改结果未发布。",
                        f"槽位 {page['slot_id']} 的 content_ref 与父修订不一致。",
                    )
                continue
            number = _page_number(page)
            slide = slide_by_number[number]
            graph = normalized_page_content_graph(replacement_package, slide.slide_id)
            page.update(
                status="ready",
                source_type=(
                    "full_deck_edit"
                    if operation == "revise_full_deck"
                    else "generated_segment"
                ),
                content_ref=FullDeckContentRef(
                    revision_hash=replacement_package.package_hash,
                    package_hash=replacement_package.package_hash,
                    slide_id=slide.slide_id,
                    slide_content_hash=graph.content_hash,
                ).model_dump(mode="json"),
            )
        next_plan = FullDeckPlan.model_validate({"pages": next_pages})
        if any(page.status != "ready" for page in next_plan.pages):
            raise FullDeckGenerationError(
                "full_deck_incomplete",
                "全稿仍有未完成页面，修改结果未发布。",
                "版本化修改后仍存在 pending 槽位。",
            )

        replacement_source_id = f"replacement_{generation_id[-12:]}"
        replacement_source = ComposerSource(
            source_id=replacement_source_id,
            package=replacement_package,
        )
        replacement_pages: list[ComposerPage] = []
        ordered_pages: list[ComposerPage] = []
        for page in next_plan.pages:
            number = (
                page.outline_ref.source_slide_number
                if page.outline_ref is not None
                else page.position + 1
            )
            if page.slot_id in changed_set:
                source_id = replacement_source_id
                source_slide_id = page.content_ref.slide_id
                replacement_pages.append(ComposerPage(
                    slot_id=page.slot_id,
                    slide_id=page.slot_id,
                    title=page.title,
                    source_slide_number=number,
                    source_id=source_id,
                    source_slide_id=source_slide_id,
                ))
            else:
                source_id = "parent"
                source_slide_id = page.content_ref.slide_id
            ordered_pages.append(ComposerPage(
                slot_id=page.slot_id,
                slide_id=page.slot_id,
                title=page.title,
                source_slide_number=number,
                source_id=source_id,
                source_slide_id=source_slide_id,
            ))
        if parent_package is not None:
            composition = compose_full_deck_revision(
                title=manifest["title"],
                parent_package=parent_package,
                replacement_sources=[replacement_source],
                replacement_pages=replacement_pages,
                ordered_pages=ordered_pages,
            )
        else:
            sources = [replacement_source]
            initial_pages: list[ComposerPage] = []
            if len(changed_set) < len(next_plan.pages):
                sources.append(ComposerSource(
                    source_id="approved_sample",
                    package=sample_package,
                ))
            for page in ordered_pages:
                if page.slot_id in changed_set:
                    initial_pages.append(page)
                    continue
                source_page = next(
                    item for item in next_plan.pages if item.slot_id == page.slot_id
                )
                if source_page.source_type != "approved_sample":
                    raise FullDeckGenerationError(
                        "full_deck_incomplete",
                        "父版本缺少完整包，无法保留未修改页面。",
                        f"槽位 {page.slot_id} 没有可复用的已确认样品来源。",
                    )
                initial_pages.append(page.model_copy(update={
                    "source_id": "approved_sample",
                }))
            composition = compose_full_deck(FullDeckComposerInput(
                title=manifest["title"],
                sources=sources,
                pages=initial_pages,
            ))
        for page, slide in zip(next_plan.pages, composition.manifest.slides, strict=True):
            if (
                page.content_ref is None
                or slide.slot_id != page.slot_id
                or slide.source_slide_content_hash != page.content_ref.slide_content_hash
                or slide.composed_slide_content_hash != page.content_ref.slide_content_hash
            ):
                raise FullDeckGenerationError(
                    "full_deck_composition_failed",
                    "全稿页面来源保真校验失败，修改结果未发布。",
                    f"槽位 {page.slot_id} 的规范化内容图在重新组装前后不一致。",
                )
        _validate_offline_package(composition.package)
        _validate_full_deck_package_limits(composition.package, workflow.runtime)
        full_package = FullDeckPackage.model_validate({
            **composition.package.model_dump(mode="json"),
            "composition_manifest": composition.manifest.model_dump(mode="json"),
        })
    except (
        FullDeckGenerationError,
        FullDeckComposerError,
        ValidationError,
        ValueError,
    ) as exc:
        failure = (
            exc
            if isinstance(exc, FullDeckGenerationError)
            else FullDeckGenerationError(
                "full_deck_composition_failed",
                "完整全稿重新组装失败，修改结果未发布。",
                f"Composer 拒绝了版本化修改：{exc}",
            )
        )
        workflow.store.finish_prompt_call(
            successful_audit["prompt_call_id"],
            status="failed",
            traces=successful_audit["traces"],
            messages=successful_audit["messages"],
            error={
                "type": type(failure).__name__,
                "code": failure.public_code,
                "message": failure.repair_reason[:1000],
            },
        )
        raise failure from exc

    if should_cancel():
        workflow.store.finish_prompt_call(
            successful_audit["prompt_call_id"],
            status="failed",
            traces=successful_audit["traces"],
            messages=successful_audit["messages"],
            error={
                "type": "JobCancelled",
                "code": "job_cancelled",
                "message": "任务在最终提交前取消，当前全稿版本未改变。",
            },
        )
        raise JobCancelled("full-deck revision cancelled before commit")

    next_revision_number = max(
        (item["revision"] for item in manifest.get("full_deck_revisions", [])),
        default=0,
    ) + 1
    provenance_output = json.dumps(
        composition.manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    revision = FullDeckRevision.create(
        full_deck_id=root["full_deck_id"],
        revision=next_revision_number,
        parent=revision_hash,
        feedback=(
            normalized_feedback
            if operation == "revise_full_deck"
            else "重新生成当前全稿中的非样品页面"
        ),
        plan=next_plan,
        package=full_package,
        status="pending_approval",
        provenance={
            **generation_provenance(skill_index, all_traces, provenance_output),
            "operation": operation,
            "outline_revision_hash": root["outline_revision_hash"],
            "approved_sample_revision_hash": root["approved_sample_revision_hash"],
            "model_config_hash": workflow.runtime.model_hash,
            "runtime_config_hash": workflow.runtime.runtime_hash,
            "template_id": "ppt_full_revision",
            "template_version": 1,
            "template_hash": template_hash,
            "composer_version": COMPOSER_VERSION,
            "composition_input_hash": composition.manifest.input_hash,
            "package_hash": full_package.package_hash,
            "changed_slot_ids": changed_slot_ids,
            "changed_source_slide_numbers": changed_numbers,
            "generation_id": generation_id,
            "prompt_call_ids": prompt_call_ids,
            "repair_attempts": successful_audit["repair_attempts"],
            "feedback_hash": stable_hash(normalized_feedback) if normalized_feedback else None,
            "resource_limits": {
                "max_files": workflow.runtime.policy.full_deck_max_files,
                "max_file_bytes": workflow.runtime.policy.full_deck_max_file_bytes,
                "max_total_bytes": workflow.runtime.policy.full_deck_max_total_bytes,
            },
        },
    )

    def apply(value: dict[str, Any]) -> dict[str, Any]:
        selected_root = value.get("full_deck") or {}
        if selected_root.get("current_revision_hash") != revision_hash:
            raise ConflictError("stale_revision")
        value["full_deck_revisions"].append(revision.model_dump(mode="json"))
        selected_root["revision_refs"].append({
            "revision_hash": revision.revision_hash,
            "status": revision.status,
        })
        selected_root["current_revision_hash"] = revision.revision_hash
        value["full_deck"] = selected_root
        value.update(state="ppt_full", phase="waiting_human_approval")
        return value

    try:
        committed = workflow.store.update(
            apply,
            "full_deck_revised",
            {
                "operation": operation,
                "revision_hash": revision.revision_hash,
                "parent_revision_hash": revision_hash,
                "changed_slot_ids": changed_slot_ids,
                "changed_source_slide_numbers": changed_numbers,
                "package_hash": full_package.package_hash,
                "composer_version": COMPOSER_VERSION,
            },
            expected_checkpoint_id=checkpoint_id,
        )
    except ConflictError:
        workflow.store.finish_prompt_call(
            successful_audit["prompt_call_id"],
            status="conflicted",
            traces=successful_audit["traces"],
            error={
                "type": "ConflictError",
                "code": "stale_revision",
                "message": "修改完成时工程版本已变化，未发布新全稿修订。",
            },
        )
        raise
    except Exception as exc:
        workflow.store.finish_prompt_call(
            successful_audit["prompt_call_id"],
            status="failed",
            traces=successful_audit["traces"],
            error={
                "type": type(exc).__name__,
                "code": "full_deck_package_invalid",
                "message": str(exc)[:1000],
            },
        )
        raise

    workflow.store.finish_prompt_call(
        successful_audit["prompt_call_id"],
        status="completed",
        traces=successful_audit["traces"],
        messages=successful_audit["messages"],
        output_ref=revision.revision_hash,
        output_hash=successful_audit["output_hash"],
    )
    return committed
