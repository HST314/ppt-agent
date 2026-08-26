from __future__ import annotations

from typing import Any, NoReturn

from agent_core.full_deck_generation import FullDeckGenerationError
from storage.project_store import ConflictError, ProjectStore


class FullDeckSessionAPIError(RuntimeError):
    """A bounded, browser-safe failure raised at the session API boundary."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.public_code = code
        self.public_message = message


_CONFLICT_CODES = {
    "full_deck_generation_session_version_conflict",
    "full_deck_generation_pause_not_allowed",
    "full_deck_generation_resume_not_allowed",
    "full_deck_generation_session_transition_invalid",
    "full_deck_generation_batch_claim_not_allowed",
    "full_deck_generation_batch_claim_conflict",
    "full_deck_generation_batch_still_running",
    "full_deck_generation_batches_incomplete",
    "full_deck_generation_directive_not_allowed",
    "full_deck_generation_directive_conflict",
}


def raise_session_api_error(exc: Exception) -> NoReturn:
    """Translate storage/domain failures without exposing internal exception text."""

    if isinstance(exc, FullDeckSessionAPIError):
        raise exc
    if isinstance(exc, FileNotFoundError):
        raise FullDeckSessionAPIError(
            404,
            "full_deck_session_not_found",
            "全稿生成会话不存在。",
        ) from exc
    if isinstance(exc, ValueError) and not isinstance(exc, ConflictError):
        raise FullDeckSessionAPIError(
            422,
            "full_deck_session_invalid",
            "全稿生成会话请求无效。",
        ) from exc
    if isinstance(exc, FullDeckGenerationError):
        raise FullDeckSessionAPIError(
            409,
            exc.public_code,
            exc.public_message,
            retryable=exc.public_code in {
                "full_deck_batch_failed",
                "full_deck_preview_failed",
                "full_deck_finalization_failed",
            },
        ) from exc
    if isinstance(exc, ConflictError):
        code = str(exc).split(":", 1)[0]
        if code in {"full_deck_session_stale", "stale_revision"}:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_stale",
                "工程基线已变化，需要重新开始全稿生成。",
            ) from exc
        if code == "full_deck_session_active":
            raise FullDeckSessionAPIError(
                409,
                code,
                "当前已有全稿生成会话。",
            ) from exc
        if code in _CONFLICT_CODES or code.startswith("full_deck_generation_"):
            raise FullDeckSessionAPIError(
                409,
                "full_deck_session_conflict",
                "会话版本或状态已变化，请刷新后重试。",
            ) from exc
    raise exc


def session_snapshot(store: ProjectStore, session_id: str) -> dict[str, Any]:
    try:
        return store.full_deck_generation_session(session_id)
    except Exception as exc:
        raise_session_api_error(exc)


def session_progress(snapshot: dict[str, Any]) -> dict[str, Any]:
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
        "ready_pages": sum(
            page["generation_status"] in {"sample_ready", "ready"}
            for page in snapshot["pages"]
        ),
        "total_pages": len(snapshot["pages"]),
        "completed_batches": snapshot["completed_batches"],
        "total_batches": snapshot["total_batches"],
        "active_batch_index": active_index,
        "active_slide_numbers": (
            list(active_batch["source_slide_numbers"]) if active_batch else []
        ),
    }


def session_capabilities(snapshot: dict[str, Any]) -> list[str]:
    capabilities = {
        "queued": ["cancel"],
        "running": ["pause", "add_directive", "cancel"],
        "pause_requested": ["add_directive", "cancel"],
        "paused": ["resume", "add_directive", "cancel"],
        "failed": ["retry", "cancel"],
        "finalizing": [],
        "completed": [],
        "cancelled": [],
        "stale": [],
    }[snapshot["status"]]
    if snapshot["status"] == "failed" and any(
        batch["status"] != "succeeded" and not batch.get("segment_package_id")
        for batch in snapshot["batches"]
    ):
        capabilities.insert(1, "add_directive")
    return capabilities


_PUBLIC_ERROR_MESSAGES = {
    "full_deck_batch_failed": "当前批未完成，可重试当前批。",
    "full_deck_preview_failed": "页段已保存，部分预览组装失败。",
    "full_deck_finalization_failed": "页段已完成，正式全稿发布失败。",
    "full_deck_session_stale": "工程基线已变化，需要重新开始全稿生成。",
}


def _public_error(error: Any) -> dict[str, str] | None:
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if code == "full_deck_generation_worker_interrupted":
        return {
            "code": "full_deck_batch_failed",
            "message": "服务已重启，请重试当前批。",
        }
    if not isinstance(code, str) or code not in _PUBLIC_ERROR_MESSAGES:
        return {
            "code": "full_deck_batch_failed",
            "message": _PUBLIC_ERROR_MESSAGES["full_deck_batch_failed"],
        }
    return {"code": code, "message": _PUBLIC_ERROR_MESSAGES[code]}


def public_session_summary(
    store: ProjectStore,
    project_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    preview_url = None
    package_id = snapshot.get("latest_preview_package_id")
    if package_id is not None:
        try:
            package = store.full_deck_generation_package(package_id)
        except (FileNotFoundError, ValueError, ConflictError) as exc:
            raise FullDeckSessionAPIError(
                409,
                "full_deck_preview_failed",
                "部分预览暂不可用，请重试当前批。",
                retryable=True,
            ) from exc
        if package["session_id"] != snapshot["session_id"] or package["kind"] != "preview":
            raise FullDeckSessionAPIError(
                409,
                "full_deck_preview_failed",
                "部分预览暂不可用，请重试当前批。",
                retryable=True,
            )
        preview_url = (
            f"/api/projects/{project_id}/full-deck/generation-sessions/"
            f"{snapshot['session_id']}/preview/{package['entrypoint']}"
            f"?v={snapshot['session_version']}"
        )
    return {
        "session_id": snapshot["session_id"],
        "status": snapshot["status"],
        "session_version": snapshot["session_version"],
        "progress": session_progress(snapshot),
        "latest_preview_package_id": package_id,
        "preview_url": preview_url,
        "published_revision_hash": snapshot.get("published_revision_hash"),
        "error": _public_error(snapshot.get("error")),
        "capabilities": session_capabilities(snapshot),
        "created_at": snapshot["created_at"],
        "updated_at": snapshot["updated_at"],
    }


def public_session(
    store: ProjectStore,
    project_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    value = public_session_summary(store, project_id, snapshot)
    value["pages"] = [
        {
            key: page[key]
            for key in (
                "position",
                "slot_id",
                "source_slide_number",
                "title",
                "generation_status",
                "batch_index",
                "source_type",
            )
        }
        | {"error": _public_error(page.get("error"))}
        for page in snapshot["pages"]
    ]
    value["batches"] = [
        {
            key: batch[key]
            for key in (
                "batch_index",
                "status",
                "slot_ids",
                "source_slide_numbers",
                "attempt_count",
                "applied_directive_ids",
                "started_at",
                "completed_at",
            )
        }
        | {"error": _public_error(batch.get("error"))}
        for batch in snapshot["batches"]
    ]
    value["directives"] = [
        {
            key: directive[key]
            for key in (
                "directive_id",
                "content",
                "apply_from_batch_index",
                "created_at",
                "first_applied_at",
            )
        }
        for directive in snapshot["directives"]
    ]
    return value


def current_session_summary(
    store: ProjectStore,
    project_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    root = manifest.get("full_deck") or {}
    full_deck_id = root.get("full_deck_id")
    if full_deck_id is None:
        return None
    snapshot = store.active_full_deck_generation_session(
        full_deck_id,
        manifest.get("branch", "main"),
    )
    if snapshot is None:
        recent = store.list_full_deck_generation_sessions(
            full_deck_id,
            branch=manifest.get("branch", "main"),
            limit=1,
        )
        if not recent:
            return None
        snapshot = store.full_deck_generation_session(recent[0]["session_id"])
    return public_session_summary(store, project_id, snapshot)


def job_progress(snapshot: dict[str, Any], stage: str | None = None) -> dict[str, Any]:
    return {
        "session_id": snapshot["session_id"],
        "stage": stage or snapshot["status"],
        "current_batch": snapshot.get("active_batch_index"),
        **session_progress(snapshot),
    }
