from __future__ import annotations

import base64
import json
from copy import deepcopy
from typing import Any, Callable

from agent_core.full_deck_batching import (
    FULL_DECK_BATCH_PLANNER_VERSION,
    compose_partial_full_deck_preview,
    plan_full_deck_batches,
    project_full_deck_generation_pages,
    project_succeeded_full_deck_batch_pages,
)
from agent_core.full_deck_composer import (
    COMPOSER_VERSION,
    ComposerPage,
    ComposerSource,
    FullDeckComposerInput,
    compose_full_deck,
    normalized_page_content_graph,
)
from agent_core.full_deck_batch_generation import (
    GeneratedFullDeckBatch,
    generate_full_deck_batch,
)
from agent_core.full_deck_generation import (
    FullDeckGenerationError,
    FullDeckWorkflowHost,
    _validate_full_deck_package_limits,
    _validate_offline_package,
)
from agent_core.jobs import JobCancelled
from agent_core.models import (
    FullDeckContentRef,
    FullDeckGenerationBatch,
    FullDeckGenerationContentRef,
    FullDeckGenerationDirective,
    FullDeckGenerationPackage,
    FullDeckGenerationSession,
    FullDeckPackage,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
    PackageFile,
)
from agent_core.workflow_support import (
    current_full_deck_revision,
    generation_provenance,
    package_model,
    stable_hash,
)
from runtime.read_tool import SkillReader
from storage.project_store import ConflictError


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]


class FullDeckSessionError(FullDeckGenerationError):
    """Stable generation-session failure exposed through JobRegistry."""


def _error_detail(message: Any) -> str | None:
    """Bound an internal failure reason to one short line for diagnosis."""

    if message is None:
        return None
    text = " ".join(str(message).split())
    return text[:200] or None


def _session_baseline(
    manifest: dict[str, Any],
    session: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = manifest.get("full_deck") or {}
    current = current_full_deck_revision(manifest)
    if (
        manifest.get("checkpoint_id") != session["base_checkpoint_id"]
        or manifest.get("branch", "main") != session["branch"]
        or current is None
        or current.get("revision_hash") != session["base_revision_hash"]
        or root.get("current_revision_hash") != session["base_revision_hash"]
        or root.get("outline_revision_hash") != session["outline_revision_hash"]
        or root.get("approved_sample_revision_hash")
        != session["sample_revision_hash"]
    ):
        raise ConflictError("full_deck_session_stale")
    return root, current


def start_full_deck_generation_session(
    workflow: FullDeckWorkflowHost,
    checkpoint_id: str,
) -> dict[str, Any]:
    """Freeze one deterministic session plan without mutating the project checkpoint."""

    manifest = workflow.store.read()
    workflow._require(manifest, "generate_full_deck", checkpoint_id)
    root = manifest.get("full_deck") or {}
    current = current_full_deck_revision(manifest)
    if current is None or current.get("revision_hash") != root.get(
        "current_revision_hash"
    ):
        raise ConflictError("stale_revision:当前全稿版本已变化，请刷新后重试。")
    active = workflow.store.active_full_deck_generation_session(
        root["full_deck_id"], manifest.get("branch", "main")
    )
    if active is not None:
        anchors = (
            "base_checkpoint_id",
            "base_revision_hash",
            "outline_revision_hash",
            "sample_revision_hash",
        )
        expected = {
            "base_checkpoint_id": checkpoint_id,
            "base_revision_hash": current["revision_hash"],
            "outline_revision_hash": root["outline_revision_hash"],
            "sample_revision_hash": root["approved_sample_revision_hash"],
        }
        if all(active[key] == expected[key] for key in anchors):
            return active
        raise FullDeckSessionError(
            "full_deck_session_active",
            "当前已有全稿生成会话，请先处理现有会话。",
            "同一分支已经存在不同基线的非终态生成会话。",
        )
    try:
        plan = FullDeckPlan.model_validate(current["plan"])
        batch_plan = plan_full_deck_batches(plan)
    except ValueError as exc:
        raise FullDeckSessionError(
            "full_deck_plan_invalid",
            "当前全稿页面清单不能建立生成批次，请重新进入全稿。",
            f"确定性批次规划失败：{exc}",
        ) from exc
    session = FullDeckGenerationSession(
        full_deck_id=root["full_deck_id"],
        branch=manifest.get("branch", "main"),
        base_checkpoint_id=checkpoint_id,
        base_revision_hash=current["revision_hash"],
        outline_revision_hash=root["outline_revision_hash"],
        sample_revision_hash=root["approved_sample_revision_hash"],
        planner_version=FULL_DECK_BATCH_PLANNER_VERSION,
        total_batches=len(batch_plan.batches),
    )
    batches = [
        FullDeckGenerationBatch(
            session_id=session.session_id,
            batch_index=item.batch_index,
            slot_ids=item.slot_ids,
            source_slide_numbers=item.source_slide_numbers,
        )
        for item in batch_plan.batches
    ]
    pages = project_full_deck_generation_pages(
        session.session_id,
        plan,
        batch_plan,
    )
    try:
        return workflow.store.create_full_deck_generation_session(
            session,
            batches,
            pages,
        )
    except ConflictError:
        active = workflow.store.active_full_deck_generation_session(
            root["full_deck_id"], manifest.get("branch", "main")
        )
        if active is not None and active["base_checkpoint_id"] == checkpoint_id:
            return active
        raise


def request_full_deck_generation_pause(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    expected_session_version: int,
) -> dict[str, Any]:
    snapshot = workflow.store.full_deck_generation_session(session_id)
    if snapshot["status"] not in {"running", "pause_requested"}:
        raise ConflictError("full_deck_generation_pause_not_allowed")
    return workflow.store.update_full_deck_generation_session(
        session_id,
        expected_session_version,
        status="pause_requested",
        completed_batches=snapshot["completed_batches"],
    )


def resume_full_deck_generation_session(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    expected_session_version: int,
) -> dict[str, Any]:
    snapshot = workflow.store.full_deck_generation_session(session_id)
    if snapshot["status"] != "paused":
        raise ConflictError("full_deck_generation_resume_not_allowed")
    _session_baseline(workflow.store.read(), snapshot)
    return workflow.store.update_full_deck_generation_session(
        session_id,
        expected_session_version,
        status="running",
        completed_batches=snapshot["completed_batches"],
        error=None,
    )


def add_full_deck_generation_directive(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    expected_session_version: int,
    content: str,
) -> dict[str, Any]:
    snapshot = workflow.store.full_deck_generation_session(session_id)
    if snapshot["status"] in {"finalizing", "completed", "cancelled", "stale"}:
        raise FullDeckSessionError(
            "full_deck_directive_too_late",
            "全稿已进入收尾，补充要求不能再进入生成批次。",
            f"会话状态 {snapshot['status']} 不接受下一批指令。",
        )
    if snapshot["active_batch_index"] is not None:
        apply_from = int(snapshot["active_batch_index"]) + 1
    else:
        remaining = [
            int(batch["batch_index"])
            for batch in snapshot["batches"]
            if batch["status"] != "succeeded"
            and not batch.get("segment_package_id")
        ]
        apply_from = min(remaining) if remaining else snapshot["total_batches"] + 1
    if apply_from > snapshot["total_batches"]:
        raise FullDeckSessionError(
            "full_deck_directive_too_late",
            "没有尚未生成的后续批次可应用补充要求。",
            "所有模型页段均已生成或正在执行最后一批。",
        )
    return workflow.store.add_full_deck_generation_directive(
        FullDeckGenerationDirective(
            session_id=session_id,
            content=content,
            apply_from_batch_index=apply_from,
        ),
        expected_session_version=expected_session_version,
    )


def _package_file(content: bytes, metadata: dict[str, Any]) -> PackageFile:
    return PackageFile(
        path=str(metadata["path"]),
        content=base64.b64encode(content).decode("ascii"),
        encoding="base64",
        media_type=str(metadata["media_type"]),
        origin=str(metadata.get("origin") or "generation_session"),
    )


def generation_package_model(
    workflow: FullDeckWorkflowHost,
    package_id: str,
) -> FullDeckGenerationPackage:
    metadata = workflow.store.full_deck_generation_package(package_id)
    contents = workflow.store.full_deck_generation_package_contents(package_id)
    files = [
        _package_file(item["content"], item)
        for item in contents
    ]
    return FullDeckGenerationPackage.model_validate({
        **{key: value for key, value in metadata.items() if key != "files"},
        "files": [item.model_dump(mode="json") for item in files],
    })


def _segment_record(
    package: HtmlPptPackage,
    *,
    session_id: str,
    batch_index: int,
) -> FullDeckGenerationPackage:
    return FullDeckGenerationPackage.model_validate({
        **package.model_dump(mode="json"),
        "session_id": session_id,
        "batch_index": batch_index,
        "kind": "segment",
        "composition_manifest": {
            "kind": "segment",
            "batch_index": batch_index,
            "source_slide_numbers": [
                slide.source_slide_number for slide in package.slides
            ],
        },
    })


def _segment_content_refs(
    segment: FullDeckGenerationPackage,
    batch_pages: list[dict[str, Any]],
) -> dict[str, FullDeckGenerationContentRef]:
    slide_by_number = {
        slide.source_slide_number: slide for slide in segment.slides
    }
    references: dict[str, FullDeckGenerationContentRef] = {}
    for page in batch_pages:
        number = int(page["source_slide_number"])
        slide = slide_by_number.get(number)
        if slide is None:
            raise ValueError("segment package does not cover the claimed page projection")
        references[str(page["slot_id"])] = FullDeckGenerationContentRef(
            package_id=segment.package_id,
            package_hash=segment.package_hash,
            slide_id=slide.slide_id,
            slide_content_hash=normalized_page_content_graph(
                segment,
                slide.slide_id,
            ).content_hash,
        )
    return references


def _progress(snapshot: dict[str, Any], stage: str) -> dict[str, Any]:
    active_index = snapshot.get("active_batch_index")
    active_batch = next(
        (
            batch
            for batch in snapshot["batches"]
            if batch["batch_index"] == active_index
        ),
        None,
    )
    return {
        "session_id": snapshot["session_id"],
        "stage": stage,
        "current_batch": active_index,
        "total_batches": snapshot["total_batches"],
        "completed_batches": snapshot["completed_batches"],
        "ready_pages": sum(
            page["generation_status"] in {"sample_ready", "ready"}
            for page in snapshot["pages"]
        ),
        "total_pages": len(snapshot["pages"]),
        "active_slide_numbers": (
            active_batch["source_slide_numbers"] if active_batch else []
        ),
    }


def _report(
    callback: ProgressCallback | None,
    snapshot: dict[str, Any],
    stage: str,
) -> None:
    if callback is not None:
        callback(_progress(snapshot, stage))


def _cancel_at_safe_point(
    workflow: FullDeckWorkflowHost,
    snapshot: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> None:
    if snapshot.get("active_batch_index") is not None:
        raise ConflictError("full_deck_generation_batch_still_running")
    cancelled = workflow.store.update_full_deck_generation_session(
        snapshot["session_id"],
        snapshot["session_version"],
        status="cancelled",
        completed_batches=snapshot["completed_batches"],
    )
    _report(progress_callback, cancelled, "cancelled")
    raise JobCancelled("full-deck generation session cancelled at a batch boundary")


def _current_commit_version(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    batch_index: int,
) -> dict[str, Any]:
    snapshot = workflow.store.full_deck_generation_session(session_id)
    batch = next(
        item for item in snapshot["batches"] if item["batch_index"] == batch_index
    )
    if (
        snapshot["status"] not in {"running", "pause_requested"}
        or snapshot["active_batch_index"] != batch_index
        or batch["status"] != "running"
    ):
        raise ConflictError("full_deck_generation_batch_not_running")
    return snapshot


def _fail_running_batch(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    batch_index: int,
    *,
    error: dict[str, Any],
    prompt_call_ids: list[str] | None = None,
    applied_directive_ids: list[str] | None = None,
    segment_package: FullDeckGenerationPackage | None = None,
) -> dict[str, Any]:
    for _ in range(5):
        snapshot = _current_commit_version(workflow, session_id, batch_index)
        try:
            return workflow.store.fail_full_deck_generation_batch(
                session_id,
                batch_index,
                expected_session_version=snapshot["session_version"],
                error=error,
                prompt_call_ids=prompt_call_ids,
                applied_directive_ids=applied_directive_ids,
                segment_package=segment_package,
            )
        except ConflictError as exc:
            if "session_version_conflict" not in str(exc):
                raise
    raise ConflictError("full_deck_generation_session_version_conflict")


def _commit_successful_batch(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    batch_index: int,
    *,
    segment: FullDeckGenerationPackage,
    preview: FullDeckGenerationPackage,
    page_content_refs: dict[str, FullDeckGenerationContentRef],
    prompt_call_ids: list[str],
    applied_directive_ids: list[str],
) -> dict[str, Any]:
    for _ in range(5):
        snapshot = _current_commit_version(workflow, session_id, batch_index)
        try:
            return workflow.store.commit_full_deck_generation_batch(
                session_id,
                batch_index,
                expected_session_version=snapshot["session_version"],
                segment_package=segment,
                preview_package=preview,
                page_content_refs=page_content_refs,
                prompt_call_ids=prompt_call_ids,
                applied_directive_ids=applied_directive_ids,
            )
        except ConflictError as exc:
            if "session_version_conflict" not in str(exc):
                raise
    raise ConflictError("full_deck_generation_session_version_conflict")


def _complete_generated_audit(
    workflow: FullDeckWorkflowHost,
    generated: GeneratedFullDeckBatch,
    segment_package_id: str,
) -> None:
    workflow.store.finish_prompt_call(
        generated.successful_prompt_call_id,
        status="completed",
        traces=generated.traces,
        messages=generated.messages,
        output_ref=segment_package_id,
        output_hash=generated.output_hash,
    )


def _execute_claimed_batch(
    workflow: FullDeckWorkflowHost,
    snapshot: dict[str, Any],
    batch: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    session_id = snapshot["session_id"]
    batch_index = int(batch["batch_index"])
    batch_pages = [
        page for page in snapshot["pages"] if page["batch_index"] == batch_index
    ]
    generated: GeneratedFullDeckBatch | None = None
    if batch.get("segment_package_id"):
        segment = generation_package_model(
            workflow,
            str(batch["segment_package_id"]),
        )
        prompt_call_ids = list(batch["prompt_call_ids"])
        applied_directive_ids = list(batch["applied_directive_ids"])
    else:
        effective_directives = [
            directive
            for directive in snapshot["directives"]
            if directive["apply_from_batch_index"] <= batch_index
        ]
        recent_segment_ids = [
            str(item["segment_package_id"])
            for item in snapshot["batches"]
            if item["status"] == "succeeded"
            and item["batch_index"] < batch_index
            and item.get("segment_package_id")
        ][-2:]
        generated = generate_full_deck_batch(
            workflow,
            workflow.store.read(),
            session_id=session_id,
            batch_index=batch_index,
            batch_pages=batch_pages,
            directives=effective_directives,
            recent_segment_package_ids=recent_segment_ids,
        )
        segment = _segment_record(
            generated.package,
            session_id=session_id,
            batch_index=batch_index,
        )
        prompt_call_ids = list(batch["prompt_call_ids"]) + generated.prompt_call_ids
        applied_directive_ids = [
            str(item["directive_id"]) for item in effective_directives
        ]
    content_refs = _segment_content_refs(segment, batch_pages)
    projected_pages = project_succeeded_full_deck_batch_pages(
        snapshot["pages"],
        batch_index,
        content_refs,
    )
    manifest = workflow.store.read()
    _session_baseline(manifest, snapshot)
    sample = next(
        item
        for item in manifest.get("samples", [])
        if item.get("revision_hash") == snapshot["sample_revision_hash"]
    )
    source_packages: dict[str, HtmlPptPackage] = {
        snapshot["sample_revision_hash"]: package_model(sample["package"]),
        segment.package_id: segment,
    }
    for succeeded in snapshot["batches"]:
        package_id = succeeded.get("segment_package_id")
        if succeeded["status"] == "succeeded" and package_id:
            source_packages[str(package_id)] = generation_package_model(
                workflow,
                str(package_id),
            )
    _report(progress_callback, snapshot, "composing_preview")
    try:
        preview = compose_partial_full_deck_preview(
            session_id=session_id,
            batch_index=batch_index,
            title=manifest["title"],
            pages=projected_pages,
            source_packages=source_packages,
        )
        _validate_offline_package(preview)
        _validate_full_deck_package_limits(preview, workflow.runtime)
        committed = _commit_successful_batch(
            workflow,
            session_id,
            batch_index,
            segment=segment,
            preview=preview,
            page_content_refs=content_refs,
            prompt_call_ids=prompt_call_ids,
            applied_directive_ids=applied_directive_ids,
        )
    except Exception as exc:
        failure = FullDeckSessionError(
            "full_deck_preview_failed",
            "页段已保存，但部分预览组装失败；重试不会再次生成该批。",
            f"批次 {batch_index} 部分预览失败：{exc}",
        )
        segment_to_store = segment if not batch.get("segment_package_id") else None
        _fail_running_batch(
            workflow,
            session_id,
            batch_index,
            error={
                "code": failure.public_code,
                "message": failure.public_message,
                "detail": _error_detail(failure.repair_reason),
            },
            prompt_call_ids=prompt_call_ids,
            applied_directive_ids=applied_directive_ids,
            segment_package=segment_to_store,
        )
        if generated is not None:
            _complete_generated_audit(workflow, generated, segment.package_id)
        raise failure from exc
    if generated is not None:
        _complete_generated_audit(workflow, generated, segment.package_id)
    return committed


def _final_plan_and_composition(
    workflow: FullDeckWorkflowHost,
    manifest: dict[str, Any],
    session: dict[str, Any],
) -> tuple[FullDeckPlan, Any, FullDeckPackage]:
    _root, current = _session_baseline(manifest, session)
    base_page_by_slot = {
        page["slot_id"]: deepcopy(page)
        for page in current["plan"]["pages"]
    }
    source_packages: dict[str, HtmlPptPackage] = {}
    sample = next(
        item
        for item in manifest.get("samples", [])
        if item.get("revision_hash") == session["sample_revision_hash"]
    )
    sample_package = package_model(sample["package"])
    source_packages[session["sample_revision_hash"]] = sample_package
    for batch in session["batches"]:
        package_id = batch.get("segment_package_id")
        if batch["status"] != "succeeded" or not package_id:
            raise ValueError("finalization requires every durable segment package")
        source_packages[str(package_id)] = generation_package_model(
            workflow,
            str(package_id),
        )
    next_pages: list[dict[str, Any]] = []
    for projected in session["pages"]:
        page = base_page_by_slot[projected["slot_id"]]
        if projected["generation_status"] == "sample_ready":
            next_pages.append(page)
            continue
        reference = projected.get("content_ref")
        if projected["generation_status"] != "ready" or reference is None:
            raise ValueError("finalization page projection is incomplete")
        page.update(
            status="ready",
            source_type="generated_segment",
            content_ref=FullDeckContentRef(
                revision_hash=reference["package_hash"],
                package_hash=reference["package_hash"],
                slide_id=reference["slide_id"],
                slide_content_hash=reference["slide_content_hash"],
            ).model_dump(mode="json"),
            derived_from=None,
        )
        next_pages.append(page)
    plan = FullDeckPlan.model_validate({"pages": next_pages})
    composer_sources: dict[str, ComposerSource] = {}
    composer_pages: list[ComposerPage] = []
    for projected, page in zip(session["pages"], plan.pages, strict=True):
        reference = projected["content_ref"]
        source_identity = reference.get("revision_hash") or reference.get("package_id")
        package = source_packages[str(source_identity)]
        source_id = (
            "approved_sample"
            if projected["generation_status"] == "sample_ready"
            else f"segment_batch_{projected['batch_index']}"
        )
        composer_sources.setdefault(
            source_id,
            ComposerSource(source_id=source_id, package=package),
        )
        composer_pages.append(ComposerPage(
            slot_id=page.slot_id,
            slide_id=page.slot_id,
            title=page.title,
            source_slide_number=projected["source_slide_number"],
            source_id=source_id,
            source_slide_id=reference["slide_id"],
        ))
    composition = compose_full_deck(FullDeckComposerInput(
        title=manifest["title"],
        sources=list(composer_sources.values()),
        pages=composer_pages,
    ))
    if [slide.slot_id for slide in composition.manifest.slides] != [
        page.slot_id for page in plan.pages
    ]:
        raise ValueError("final Composer changed the full-deck page order")
    for projected, slide in zip(
        session["pages"], composition.manifest.slides, strict=True
    ):
        expected_hash = projected["content_ref"]["slide_content_hash"]
        if (
            slide.source_slide_content_hash != expected_hash
            or slide.composed_slide_content_hash != expected_hash
        ):
            raise ValueError("final Composer changed a page content graph")
    _validate_offline_package(composition.package)
    _validate_full_deck_package_limits(composition.package, workflow.runtime)
    package = FullDeckPackage.model_validate({
        **composition.package.model_dump(mode="json"),
        "composition_manifest": composition.manifest.model_dump(mode="json"),
    })
    return plan, composition, package


def _complete_session_from_existing_revision(
    workflow: FullDeckWorkflowHost,
    session: dict[str, Any],
    revision_hash: str,
) -> dict[str, Any]:
    if session["status"] == "completed":
        return session
    if session["status"] != "finalizing":
        session = workflow.store.update_full_deck_generation_session(
            session["session_id"],
            session["session_version"],
            status="finalizing",
            completed_batches=session["total_batches"],
            error=None,
        )
    return workflow.store.update_full_deck_generation_session(
        session["session_id"],
        session["session_version"],
        status="completed",
        completed_batches=session["total_batches"],
        published_revision_hash=revision_hash,
        error=None,
    )


def finalize_full_deck_generation_session(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    session = workflow.store.full_deck_generation_session(session_id)
    manifest = workflow.store.read()
    existing = next(
        (
            revision
            for revision in manifest.get("full_deck_revisions", [])
            if revision.get("provenance", {}).get("generation_session_id")
            == session_id
        ),
        None,
    )
    if existing is not None:
        return _complete_session_from_existing_revision(
            workflow,
            session,
            existing["revision_hash"],
        )
    if session["status"] != "finalizing":
        raise ConflictError("full_deck_generation_finalization_not_allowed")
    _report(progress_callback, session, "finalizing")
    try:
        root, current = _session_baseline(manifest, session)
        plan, composition, package = _final_plan_and_composition(
            workflow,
            manifest,
            session,
        )
        prompt_call_ids = [
            prompt_call_id
            for batch in session["batches"]
            for prompt_call_id in batch["prompt_call_ids"]
        ]
        prompt_call_id_set = set(prompt_call_ids)
        calls_by_id = {
            item["prompt_call_id"]: item
            for item in workflow.store.prompt_calls(include_messages=False)
            if item["prompt_call_id"] in prompt_call_id_set
        }
        traces = [
            trace
            for prompt_call_id in prompt_call_ids
            for trace in (calls_by_id.get(prompt_call_id, {}).get("tool_calls") or [])
        ]
        skill_index = SkillReader(
            workflow.runtime.skills_root,
            per_call=1000,
            per_job=1000,
        ).index()
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
            parent=current["revision_hash"],
            feedback="首次分批生成完整 HTML-PPT",
            plan=plan,
            package=package,
            status="pending_approval",
            provenance={
                **generation_provenance(skill_index, traces, provenance_output),
                "generation_session_id": session_id,
                "outline_revision_hash": session["outline_revision_hash"],
                "approved_sample_revision_hash": session["sample_revision_hash"],
                "model_config_hash": workflow.runtime.model_hash,
                "runtime_config_hash": workflow.runtime.runtime_hash,
                "template_id": "ppt_full",
                "template_version": 1,
                "composer_version": COMPOSER_VERSION,
                "composition_input_hash": composition.manifest.input_hash,
                "package_hash": package.package_hash,
                "planner_version": session["planner_version"],
                "prompt_call_ids": prompt_call_ids,
                "batches": [
                    {
                        "batch_index": batch["batch_index"],
                        "source_slide_numbers": batch["source_slide_numbers"],
                        "segment_package_id": batch["segment_package_id"],
                        "prompt_call_ids": batch["prompt_call_ids"],
                        "applied_directive_ids": batch["applied_directive_ids"],
                    }
                    for batch in session["batches"]
                ],
                "directives_hash": stable_hash([
                    {
                        "directive_id": item["directive_id"],
                        "content": item["content"],
                        "apply_from_batch_index": item["apply_from_batch_index"],
                    }
                    for item in session["directives"]
                ]),
            },
        )

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["full_deck_revisions"].append(revision.model_dump(mode="json"))
            value["full_deck"]["revision_refs"].append({
                "revision_hash": revision.revision_hash,
                "status": revision.status,
            })
            value["full_deck"]["current_revision_hash"] = revision.revision_hash
            value.update(state="ppt_full", phase="waiting_human_approval")
            return value

        workflow.store.update(
            apply,
            "full_deck_generated",
            {
                "revision_hash": revision.revision_hash,
                "parent_revision_hash": current["revision_hash"],
                "page_count": len(plan.pages),
                "generation_session_id": session_id,
                "segment_ranges": [
                    batch["source_slide_numbers"] for batch in session["batches"]
                ],
                "package_hash": package.package_hash,
                "composer_version": COMPOSER_VERSION,
            },
            expected_checkpoint_id=session["base_checkpoint_id"],
        )
        refreshed = workflow.store.full_deck_generation_session(session_id)
        completed = _complete_session_from_existing_revision(
            workflow,
            refreshed,
            revision.revision_hash,
        )
        _report(progress_callback, completed, "completed")
        return completed
    except ConflictError as exc:
        if "stale" in str(exc):
            refreshed = workflow.store.full_deck_generation_session(session_id)
            workflow.store.update_full_deck_generation_session(
                session_id,
                refreshed["session_version"],
                status="stale",
                completed_batches=refreshed["completed_batches"],
                error={
                    "code": "full_deck_session_stale",
                    "message": "工程基线已变化，需要重新开始全稿生成。",
                },
            )
        raise
    except Exception as exc:
        failure = FullDeckSessionError(
            "full_deck_finalization_failed",
            "页段已全部完成，但正式全稿组装或发布失败；"
            "重试不会重新生成页面。",
            f"生成会话最终发布失败：{exc}",
        )
        refreshed = workflow.store.full_deck_generation_session(session_id)
        if refreshed["status"] == "finalizing":
            workflow.store.update_full_deck_generation_session(
                session_id,
                refreshed["session_version"],
                status="failed",
                completed_batches=refreshed["completed_batches"],
                error={
                    "code": failure.public_code,
                    "message": failure.public_message,
                    "detail": _error_detail(failure.repair_reason),
                },
            )
        raise failure from exc


def run_full_deck_generation_session(
    workflow: FullDeckWorkflowHost,
    session_id: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> dict[str, Any]:
    """Run automatically until pause, failure, staleness, or one final publish."""

    should_cancel = cancel_requested or (lambda: False)
    while True:
        snapshot = workflow.store.full_deck_generation_session(session_id)
        if (
            should_cancel()
            and snapshot["status"]
            not in {"completed", "cancelled", "stale", "finalizing"}
            and snapshot["active_batch_index"] is None
        ):
            _cancel_at_safe_point(workflow, snapshot, progress_callback)
        if snapshot["status"] in {"completed", "cancelled", "stale", "paused"}:
            return snapshot
        if snapshot["status"] == "pause_requested" and snapshot[
            "active_batch_index"
        ] is None:
            return workflow.store.update_full_deck_generation_session(
                session_id,
                snapshot["session_version"],
                status="paused",
                completed_batches=snapshot["completed_batches"],
            )
        if snapshot["status"] == "finalizing":
            return finalize_full_deck_generation_session(
                workflow,
                session_id,
                progress_callback,
            )
        try:
            _session_baseline(workflow.store.read(), snapshot)
        except ConflictError as exc:
            workflow.store.update_full_deck_generation_session(
                session_id,
                snapshot["session_version"],
                status="stale",
                completed_batches=snapshot["completed_batches"],
                error={
                    "code": "full_deck_session_stale",
                    "message": "工程基线已变化，需要重新开始全稿生成。",
                },
            )
            raise FullDeckSessionError(
                "full_deck_session_stale",
                "工程基线已变化，需要重新开始全稿生成。",
                str(exc),
            ) from exc
        if snapshot["completed_batches"] == snapshot["total_batches"]:
            finalizing = workflow.store.update_full_deck_generation_session(
                session_id,
                snapshot["session_version"],
                status="finalizing",
                completed_batches=snapshot["completed_batches"],
                error=None,
            )
            _report(progress_callback, finalizing, "finalizing")
            continue
        retry_failed = snapshot["status"] == "failed"
        claimed = workflow.store.claim_full_deck_generation_batch(
            session_id,
            expected_session_version=snapshot["session_version"],
            retry_failed=retry_failed,
        )
        if claimed is None:
            raise ConflictError("full_deck_generation_batch_claim_conflict")
        claimed_snapshot = workflow.store.full_deck_generation_session(session_id)
        batch = claimed["batch"]
        _report(progress_callback, claimed_snapshot, "generating")
        try:
            committed = _execute_claimed_batch(
                workflow,
                claimed_snapshot,
                batch,
                progress_callback,
            )
        except Exception as exc:
            refreshed = workflow.store.full_deck_generation_session(session_id)
            current_batch = next(
                item
                for item in refreshed["batches"]
                if item["batch_index"] == batch["batch_index"]
            )
            if current_batch["status"] == "running":
                public_code = "full_deck_batch_failed"
                public_message = "当前批未完成，可重试当前批。"
                effective_directive_ids = [
                    str(item["directive_id"])
                    for item in refreshed["directives"]
                    if item["apply_from_batch_index"] <= batch["batch_index"]
                ]
                _fail_running_batch(
                    workflow,
                    session_id,
                    int(batch["batch_index"]),
                    error={
                        "code": public_code,
                        "message": public_message,
                        "detail": _error_detail(
                            f"批次 {batch['batch_index']} 生成或校验失败：{exc}"
                        ),
                    },
                    prompt_call_ids=(
                        list(current_batch["prompt_call_ids"])
                        + list(getattr(exc, "prompt_call_ids", []))
                    ),
                    applied_directive_ids=effective_directive_ids,
                )
            if should_cancel():
                failed = workflow.store.full_deck_generation_session(session_id)
                if (
                    failed["status"] == "failed"
                    and failed["active_batch_index"] is None
                ):
                    _cancel_at_safe_point(
                        workflow,
                        failed,
                        progress_callback,
                    )
            if getattr(exc, "public_code", None) == "full_deck_preview_failed":
                raise
            raise FullDeckSessionError(
                "full_deck_batch_failed",
                "当前批未完成，可重试当前批。",
                f"批次 {batch['batch_index']} 生成或校验失败：{exc}",
            ) from exc
        _report(progress_callback, committed, "validating")
        if should_cancel():
            _cancel_at_safe_point(workflow, committed, progress_callback)
        if committed["status"] == "pause_requested":
            paused = workflow.store.update_full_deck_generation_session(
                session_id,
                committed["session_version"],
                status="paused",
                completed_batches=committed["completed_batches"],
            )
            _report(progress_callback, paused, "paused")
            return paused
