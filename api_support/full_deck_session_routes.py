from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from api_support.full_deck_sessions import (
    FullDeckSessionAPIError,
    job_progress,
    public_session,
    raise_session_api_error,
    session_snapshot,
)
from agent_core.jobs import ActiveJobError, JobRegistry
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_images import SyncReport
from storage.project_store import ConflictError, ProjectStore


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FullDeckGenerationStartRequest(StrictRequest):
    checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{24}$")
    revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class FullDeckGenerationControlRequest(StrictRequest):
    session_version: int = Field(ge=1)


class FullDeckGenerationDirectiveRequest(FullDeckGenerationControlRequest):
    content: str = Field(min_length=1, max_length=4000)


@dataclass(frozen=True)
class FullDeckSessionRouteContext:
    store_for: Callable[[str], ProjectStore]
    current_runtime: Callable[[], ManagedRuntime]
    current_jobs: Callable[[], JobRegistry]
    sync_images: Callable[[ProjectStore], SyncReport]
    package_file_response: Callable[[Path, str], FileResponse]


def _session_target(snapshot: dict[str, Any]) -> str:
    remaining = [
        str(batch["batch_index"])
        for batch in snapshot["batches"]
        if batch["status"] != "succeeded"
    ]
    return remaining[0] if remaining else "finalizing"


def _session_job_key(
    operation: str,
    session_id: str,
    session_version: int,
    target: str,
) -> str:
    return (
        f"full_deck_session:{operation}:{session_id}:"
        f"{session_version}:{target}"
    )


def _session_job_key_prefix(
    operation: str,
    session_id: str,
    session_version: int,
) -> str:
    return f"full_deck_session:{operation}:{session_id}:{session_version}:"


def submit_full_deck_session_job(
    context: FullDeckSessionRouteContext,
    project_id: str,
    workflow: Workflow,
    snapshot: dict[str, Any],
    *,
    request_key: str,
    operation: str,
    prepare: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    jobs = context.current_jobs()
    existing = jobs.find_for_request(project_id, request_key)
    if existing is not None:
        return existing

    def run(cancel_requested, report_progress):
        if prepare is not None:
            prepare()
        return workflow.run_full_deck_generation_session(
            snapshot["session_id"],
            cancel_requested=cancel_requested,
            progress_callback=report_progress,
        )

    return jobs.submit(
        project_id,
        operation,
        snapshot["base_checkpoint_id"],
        run,
        cancellable=True,
        idempotency_key=request_key,
        session_id=snapshot["session_id"],
        initial_progress=job_progress(snapshot),
        progress_reporting=True,
    )


def start_full_deck_session_job(
    context: FullDeckSessionRouteContext,
    project_id: str,
    workflow: Workflow,
    checkpoint_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        snapshot = workflow.start_full_deck_generation_session(checkpoint_id)
        job = submit_full_deck_session_job(
            context,
            project_id,
            workflow,
            snapshot,
            request_key=f"full_deck_session:start:{snapshot['session_id']}",
            operation="generate_full_deck",
        )
    except ActiveJobError as exc:
        raise FullDeckSessionAPIError(
            409,
            "full_deck_session_conflict",
            "当前有其他任务正在运行，请稍后重试。",
        ) from exc
    except Exception as exc:
        raise_session_api_error(exc)
    return snapshot, job


def _require_batched_generation_enabled(
    context: FullDeckSessionRouteContext,
) -> None:
    if context.current_runtime().policy.full_deck_batched_generation_enabled:
        return
    raise FullDeckSessionAPIError(
        409,
        "full_deck_batched_generation_disabled",
        "分批全稿生成当前未启用。",
    )


def _session_job_response(
    store: ProjectStore,
    project_id: str,
    session_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    return {
        "session": public_session(
            store,
            project_id,
            session_snapshot(store, session_id),
        ),
        "job": job,
    }


def build_full_deck_session_router(
    context: FullDeckSessionRouteContext,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions",
        status_code=202,
    )
    def start_session(
        project_id: str,
        request: FullDeckGenerationStartRequest,
    ) -> dict[str, Any]:
        _require_batched_generation_enabled(context)
        store = context.store_for(project_id)
        manifest = store.read(latest_sample_only=True, include_sample_html=False)
        root = manifest.get("full_deck") or {}
        if root.get("current_revision_hash") != request.revision_hash:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_stale",
                "工程基线已变化，需要重新开始全稿生成。",
            )
        workflow = Workflow(store, context.current_runtime())
        context.sync_images(store)
        snapshot, job = start_full_deck_session_job(
            context,
            project_id,
            workflow,
            request.checkpoint_id,
        )
        return _session_job_response(
            store,
            project_id,
            snapshot["session_id"],
            job,
        )

    @router.get(
        "/api/projects/{project_id}/full-deck/generation-sessions/{session_id}"
    )
    def get_session(project_id: str, session_id: str) -> dict[str, Any]:
        store = context.store_for(project_id)
        return public_session(
            store,
            project_id,
            session_snapshot(store, session_id),
        )

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/pause"
    )
    def pause_session(
        project_id: str,
        session_id: str,
        request: FullDeckGenerationControlRequest,
    ) -> dict[str, Any]:
        store = context.store_for(project_id)
        snapshot = session_snapshot(store, session_id)
        if (
            snapshot["status"] == "pause_requested"
            and snapshot["session_version"] == request.session_version + 1
        ):
            return public_session(store, project_id, snapshot)
        try:
            updated = Workflow(
                store,
                context.current_runtime(),
            ).request_full_deck_generation_pause(
                session_id,
                request.session_version,
            )
        except Exception as exc:
            raise_session_api_error(exc)
        return public_session(store, project_id, updated)

    def submit_continuation(
        project_id: str,
        session_id: str,
        session_version: int,
        operation: str,
    ) -> dict[str, Any]:
        store = context.store_for(project_id)
        snapshot = session_snapshot(store, session_id)
        jobs = context.current_jobs()
        request_key_prefix = _session_job_key_prefix(
            operation,
            session_id,
            session_version,
        )
        existing = jobs.find_for_request_prefix(project_id, request_key_prefix)
        if existing is not None:
            return _session_job_response(store, project_id, session_id, existing)
        if snapshot["session_version"] != session_version:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "会话版本或状态已变化，请刷新后重试。",
            )
        if operation == "resume" and snapshot["status"] != "paused":
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "当前会话没有暂停，不能继续。",
            )
        if operation == "retry" and snapshot["status"] != "failed":
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "当前会话没有可重试的失败批次。",
            )
        workflow = Workflow(store, context.current_runtime())
        request_key = _session_job_key(
            operation,
            session_id,
            session_version,
            _session_target(snapshot),
        )
        prepare = None
        if operation == "resume":
            prepare = lambda: workflow.resume_full_deck_generation_session(
                session_id,
                session_version,
            )
        try:
            job = submit_full_deck_session_job(
                context,
                project_id,
                workflow,
                snapshot,
                request_key=request_key,
                operation=f"{operation}_full_deck_generation",
                prepare=prepare,
            )
        except ActiveJobError as exc:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "当前有其他任务正在运行，请稍后重试。",
            ) from exc
        except Exception as exc:
            raise_session_api_error(exc)
        return _session_job_response(store, project_id, session_id, job)

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/resume",
        status_code=202,
    )
    def resume_session(
        project_id: str,
        session_id: str,
        request: FullDeckGenerationControlRequest,
    ) -> dict[str, Any]:
        return submit_continuation(
            project_id,
            session_id,
            request.session_version,
            "resume",
        )

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/retry",
        status_code=202,
    )
    def retry_session(
        project_id: str,
        session_id: str,
        request: FullDeckGenerationControlRequest,
    ) -> dict[str, Any]:
        return submit_continuation(
            project_id,
            session_id,
            request.session_version,
            "retry",
        )

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/cancel"
    )
    def cancel_session(
        project_id: str,
        session_id: str,
        request: FullDeckGenerationControlRequest,
    ) -> dict[str, Any]:
        store = context.store_for(project_id)
        snapshot = session_snapshot(store, session_id)
        if (
            snapshot["status"] == "cancelled"
            and snapshot["session_version"] == request.session_version + 1
        ):
            return public_session(store, project_id, snapshot)
        if snapshot["session_version"] != request.session_version:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "会话版本或状态已变化，请刷新后重试。",
            )
        if snapshot["status"] in {
            "completed",
            "cancelled",
            "stale",
            "finalizing",
        }:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "当前会话状态不允许取消。",
            )
        jobs = context.current_jobs()
        active_job = jobs.active_for_session(project_id, session_id)
        if active_job is not None:
            cancellation = jobs.cancel(active_job["job_id"])
            if cancellation["status"] in {"queued", "running"}:
                result = public_session(store, project_id, snapshot)
                result["cancel_requested"] = True
                return result
        elif snapshot["status"] in {"running", "pause_requested"}:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "会话运行状态已变化，请刷新后重试。",
            )
        try:
            updated = store.update_full_deck_generation_session(
                session_id,
                request.session_version,
                status="cancelled",
                completed_batches=snapshot["completed_batches"],
            )
        except Exception as exc:
            raise_session_api_error(exc)
        return public_session(store, project_id, updated)

    @router.post(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/directives"
    )
    def add_directive(
        project_id: str,
        session_id: str,
        request: FullDeckGenerationDirectiveRequest,
    ) -> dict[str, Any]:
        store = context.store_for(project_id)
        session_snapshot(store, session_id)
        try:
            directive = Workflow(
                store,
                context.current_runtime(),
            ).add_full_deck_generation_directive(
                session_id,
                request.session_version,
                request.content,
            )
            updated = session_snapshot(store, session_id)
        except Exception as exc:
            raise_session_api_error(exc)
        target = next(
            batch
            for batch in updated["batches"]
            if batch["batch_index"] == directive["apply_from_batch_index"]
        )
        return {
            **directive,
            "apply_from_slide_numbers": target["source_slide_numbers"],
        }

    @router.get(
        "/api/projects/{project_id}/full-deck/generation-sessions/"
        "{session_id}/preview/{logical_path:path}"
    )
    def preview_session_file(
        project_id: str,
        session_id: str,
        logical_path: str,
    ) -> FileResponse:
        store = context.store_for(project_id)
        try:
            path, media_type = store.full_deck_generation_preview_file(
                session_id,
                logical_path,
            )
        except FileNotFoundError as exc:
            raise FullDeckSessionAPIError(
                404,
                "full_deck_preview_unavailable",
                "部分全稿预览尚不可用。",
            ) from exc
        except ValueError as exc:
            raise FullDeckSessionAPIError(
                422,
                "full_deck_preview_path_invalid",
                "部分全稿预览路径无效。",
            ) from exc
        except ConflictError as exc:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_preview_failed",
                "部分全稿预览暂不可用，请重试当前批。",
                retryable=True,
            ) from exc
        return context.package_file_response(path, media_type)

    return router
