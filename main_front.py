from __future__ import annotations

import os
import re
import threading
import io
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.jobs import JobRegistry
from agent_core.models import TaskCard
from agent_core.workflow import Workflow, capabilities
from configs.runtime import ManagedRuntime, RuntimeConfigUpdate
from storage.project_store import ConflictError, ProjectStore, list_projects


APP_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = APP_ROOT / "frontend"
PROJECTS_ROOT = Path(os.getenv("PPT_AGENT_PROJECTS_ROOT", FRONTEND_ROOT / "data" / "projects")).resolve()
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
MAX_REQUEST_BYTES = 512 * 1024

app = FastAPI(title="PPT Agent Studio", version="0.3.0")
runtime = ManagedRuntime(APP_ROOT)
runtime_config_lock = threading.RLock()
jobs = JobRegistry(PROJECTS_ROOT / ".jobs")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(StrictRequest):
    project_id: str = Field(min_length=2, max_length=64)
    task_card: TaskCard


class StartJobRequest(StrictRequest):
    operation: Literal[
        "start_clarification",
        "generate_narrative",
        "generate_outline",
        "regenerate_narrative",
        "regenerate_outline",
        "generate_sample",
        "regenerate_sample",
        "revise_sample",
    ]
    checkpoint_id: str = Field(min_length=1, max_length=128)
    feedback: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_feedback(self) -> "StartJobRequest":
        if self.operation == "revise_sample" and not (self.feedback and self.feedback.strip()):
            raise ValueError("revise_sample requires feedback")
        if self.operation != "revise_sample" and self.feedback is not None:
            raise ValueError("feedback is only accepted for revise_sample")
        return self


class ClarificationRequest(StrictRequest):
    checkpoint_id: str
    question_card_id: str
    answers: dict[str, str]


class RevisionRequest(StrictRequest):
    checkpoint_id: str
    markdown_body: str = Field(min_length=1, max_length=200_000)


class ApproveRequest(StrictRequest):
    checkpoint_id: str
    revision_hash: str


class CheckpointRequest(StrictRequest):
    checkpoint_id: str = Field(min_length=1, max_length=128)


class BranchRequest(StrictRequest):
    checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{24}$")
    name: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
    mode: Literal["fork_after", "rerun_stage"] = "fork_after"
    stage: Literal["intake", "intake_clarify", "narrative_structure", "slide_outline", "ppt_sample"] | None = None


class BranchSwitchRequest(StrictRequest):
    checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{24}$")


class SampleRevisionRequest(StrictRequest):
    checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{24}$")


class SampleRevisionBranchRequest(SampleRevisionRequest):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")


@app.middleware("http")
async def request_size_limit(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"error": {"code": "request_too_large", "message": "请求内容超过 512 KiB。", "retryable": False}})
        except ValueError:
            return JSONResponse(status_code=400, content={"error": {"code": "invalid_content_length", "message": "Content-Length 无效。", "retryable": False}})
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"error": {"code": "request_too_large", "message": "请求内容超过 512 KiB。", "retryable": False}})
        chunks.append(chunk)
    request._body = b"".join(chunks)
    return await call_next(request)


@app.exception_handler(ConflictError)
async def conflict_handler(_: Request, exc: ConflictError):
    return JSONResponse(status_code=409, content={"error": {"code": str(exc).split(":", 1)[0], "message": str(exc), "retryable": False}})


def store_for(project_id: str) -> ProjectStore:
    if not PROJECT_ID.fullmatch(project_id):
        raise HTTPException(status_code=422, detail="工程 ID 格式无效")
    store = ProjectStore(PROJECTS_ROOT, project_id)
    if not store.exists():
        raise HTTPException(status_code=404, detail="工程不存在")
    return store


def project_view(store: ProjectStore) -> dict[str, Any]:
    manifest = store.read(latest_sample_only=True)
    latest_job = jobs.latest_for_project(store.project_id)
    with runtime_config_lock:
        sample_page_count = runtime.policy.sample_page_count
    samples = manifest.get("samples", [])
    current_hash = manifest.get("current_sample_revision_hash")
    selected = next(
        (item for item in samples if item.get("revision_hash") == current_hash),
        samples[-1] if samples else None,
    )
    latest_sample = [_public_sample(store.project_id, selected)] if selected else []
    history = store.sample_history()
    for item in history:
        package = item.get("package")
        if package and not item.get("pages"):
            item["pages"] = [
                {"page_id": slide["slide_id"], "title": slide["title"]}
                for slide in package.get("slides", [])
            ]
    return {
        **manifest,
        # Revision history stays durable in the manifest/checkpoints; the UI
        # only needs the current package metadata and should not download
        # every prior sample on each poll.
        "samples": latest_sample,
        "sample_revisions": history,
        "sample_attempts": store.sample_attempts(),
        "sample_page_count": sample_page_count,
        "capabilities": capabilities(manifest),
        "active_job": latest_job if latest_job and latest_job["status"] in {"queued", "running"} else None,
        "progress_snapshots": store.progress_snapshots(),
    }


def _public_sample(
    project_id: str,
    sample: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if sample is None:
        return None
    value = deepcopy(sample)
    package = value.get("package")
    if not package:
        return value
    package["files"] = [{
        key: item[key] for key in (
            "path", "artifact_id", "sha256", "size", "media_type", "origin"
        ) if key in item
    } for item in package.get("files", [])]
    revision_hash = value["revision_hash"]
    base = f"/api/projects/{project_id}/samples/revisions/{revision_hash}"
    value["preview_url"] = f"{base}/preview/{package['entrypoint']}"
    value["export_url"] = f"{base}/export"
    value["pages"] = [{
        "page_id": slide["slide_id"],
        "title": slide["title"],
    } for slide in package.get("slides", [])]
    return value


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ppt-agent", "version": app.version}


@app.get("/api/runtime-context")
def runtime_context() -> dict[str, Any]:
    from runtime.read_tool import SkillReader

    with runtime_config_lock:
        current = runtime
        reader = SkillReader(current.skills_root, per_call=1000, per_job=1000)
        return {**current.public_context(), "skills": reader.index()}


@app.put("/api/runtime-context")
def update_runtime_context(request: RuntimeConfigUpdate) -> dict[str, Any]:
    """Persist validated model/runtime settings without accepting credential values."""

    global runtime
    with runtime_config_lock:
        try:
            runtime = runtime.apply_update(request)
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"运行配置不可写：{exc}") from exc
        current = runtime
        from runtime.read_tool import SkillReader

        reader = SkillReader(current.skills_root, per_call=1000, per_job=1000)
        return {**current.public_context(), "skills": reader.index()}


@app.get("/api/projects")
def projects() -> list[dict[str, Any]]:
    return list_projects(PROJECTS_ROOT)


@app.post("/api/projects", status_code=201)
def create_project(request: CreateProjectRequest) -> dict[str, Any]:
    if not PROJECT_ID.fullmatch(request.project_id):
        raise HTTPException(status_code=422, detail="工程 ID 仅允许字母、数字、下划线和连字符")
    store = ProjectStore(PROJECTS_ROOT, request.project_id)
    with runtime_config_lock:
        runtime_snapshot = runtime.snapshot()
    store.create(request.task_card.model_dump(), runtime_snapshot)
    return project_view(store)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return project_view(store_for(project_id))


@app.post("/api/projects/{project_id}/jobs", status_code=202)
def start_job(project_id: str, request: StartJobRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    workflow = Workflow(store, current_runtime)
    actions = {
        "start_clarification": lambda: workflow.start_clarification(request.checkpoint_id),
        "generate_narrative": lambda: workflow.generate_document("narrative_structure", request.checkpoint_id),
        "generate_outline": lambda: workflow.generate_document("slide_outline", request.checkpoint_id),
        "regenerate_narrative": lambda: workflow.generate_document("narrative_structure", request.checkpoint_id, regenerate=True),
        "regenerate_outline": lambda: workflow.generate_document("slide_outline", request.checkpoint_id, regenerate=True),
        "generate_sample": lambda: workflow.generate_sample(request.checkpoint_id),
        "regenerate_sample": lambda: workflow.generate_sample(request.checkpoint_id, regenerate=True),
        "revise_sample": lambda: workflow.generate_sample(request.checkpoint_id, feedback=request.feedback),
    }
    return jobs.submit(project_id, request.operation, request.checkpoint_id, actions[request.operation])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return jobs.get(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str) -> list[dict[str, Any]]:
    try:
        return jobs.events(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return jobs.cancel(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/clarification")
def answer_clarification(project_id: str, request: ClarificationRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    Workflow(store, current_runtime).answer_clarification(request.checkpoint_id, request.question_card_id, request.answers)
    return project_view(store)


@app.post("/api/projects/{project_id}/documents/{document_type}/revisions")
def revise_document(project_id: str, document_type: Literal["narrative_structure", "slide_outline"], request: RevisionRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    Workflow(store, current_runtime).edit_document(document_type, request.checkpoint_id, request.markdown_body)
    return project_view(store)


@app.post("/api/projects/{project_id}/documents/{document_type}/approve")
def approve_document(project_id: str, document_type: Literal["narrative_structure", "slide_outline"], request: ApproveRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    Workflow(store, current_runtime).approve_document(document_type, request.checkpoint_id, request.revision_hash)
    return project_view(store)


@app.post("/api/projects/{project_id}/samples/enter")
def enter_sample_stage(project_id: str, request: CheckpointRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    Workflow(store, current_runtime).start_sample_stage(request.checkpoint_id)
    return project_view(store)


@app.post("/api/projects/{project_id}/samples/approve")
def approve_sample(project_id: str, request: ApproveRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    Workflow(store, current_runtime).approve_sample(request.checkpoint_id, request.revision_hash)
    return project_view(store)


@app.get("/api/projects/{project_id}/samples/revisions")
def sample_revisions(project_id: str) -> list[dict[str, Any]]:
    return store_for(project_id).sample_history()


@app.get("/api/projects/{project_id}/samples/revisions/{revision_hash}")
def sample_revision(project_id: str, revision_hash: str) -> dict[str, Any]:
    try:
        return _public_sample(
            project_id,
            store_for(project_id).sample_revision(revision_hash),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="样品修订不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="样品修订标识无效") from exc


@app.post("/api/projects/{project_id}/samples/revisions/{revision_hash}/restore")
def restore_sample_revision(
    project_id: str,
    revision_hash: str,
    request: SampleRevisionRequest,
) -> dict[str, Any]:
    store = store_for(project_id)
    with runtime_config_lock:
        current_runtime = runtime
    try:
        Workflow(store, current_runtime).restore_sample(request.checkpoint_id, revision_hash)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="样品修订不存在") from exc
    return project_view(store)


PACKAGE_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline' data: blob:; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; media-src 'self' data: blob:; connect-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "worker-src blob:; manifest-src 'none'"
)


@app.get("/api/projects/{project_id}/samples/revisions/{revision_hash}/preview/{logical_path:path}")
def preview_sample_package_file(
    project_id: str,
    revision_hash: str,
    logical_path: str,
) -> FileResponse:
    try:
        path, media_type = store_for(project_id).sample_package_file(revision_hash, logical_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="HTML-PPT 包文件不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="HTML-PPT 包路径无效") from exc
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Content-Security-Policy": PACKAGE_CSP,
            # A sandbox without allow-same-origin has an opaque origin. Package
            # subresources therefore need CORP cross-origin even though their
            # URLs share this route prefix. CSP still blocks external sources.
            "Cross-Origin-Resource-Policy": "cross-origin",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@app.get("/api/projects/{project_id}/samples/revisions/{revision_hash}/export")
def export_sample_package(project_id: str, revision_hash: str) -> Response:
    try:
        files = store_for(project_id).sample_package_files(revision_hash)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="HTML-PPT 包不存在") from exc
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for logical_path, path in files:
            archive.writestr(logical_path, path.read_bytes())
    return Response(
        output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}-html-ppt-{revision_hash[-8:]}.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/projects/{project_id}/samples/revisions/{revision_hash}/branches")
def branch_from_sample_revision(
    project_id: str,
    revision_hash: str,
    request: SampleRevisionBranchRequest,
) -> dict[str, Any]:
    store = store_for(project_id)
    with jobs.project_guard(project_id) as active:
        if active:
            raise ConflictError("active_job")
        current = store.read(latest_sample_only=True)
        if current["checkpoint_id"] != request.checkpoint_id:
            raise ConflictError("stale_revision")
        try:
            source_checkpoint = store.sample_revision_checkpoint(revision_hash)
            store.fork(
                source_checkpoint,
                request.name,
                sample_revision_hash=revision_hash,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="样品修订不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="样品修订标识无效") from exc
    return project_view(store)


@app.get("/api/projects/{project_id}/timeline")
def timeline(project_id: str) -> list[dict[str, Any]]:
    return store_for(project_id).events()


@app.get("/api/projects/{project_id}/activity")
def project_activity(
    project_id: str,
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, Any]:
    store = store_for(project_id)
    project_events = store.events(limit=min(limit, 1000))
    prompt_items = store.prompt_calls(limit=min(limit, 500))
    job_items = jobs.list_for_project(project_id, limit=min(limit, 500))
    job_events = jobs.events_for_project(project_id, limit=min(limit, 1000))
    events: list[dict[str, Any]] = []

    artifact_events = {"sample_generated", "sample_revised", "sample_revision_selected"}
    validation_events = {"document_approved", "sample_approved"}
    for index, item in enumerate(project_events):
        kind = "artifact" if item["event"] in artifact_events else "validation" if item["event"] in validation_events else "project"
        events.append({
            "id": f"project-{index}-{item['checkpoint_id']}",
            "at": item["at"],
            "kind": kind,
            "status": "succeeded",
            "title": item["event"],
            "summary": item.get("revision_hash") or item.get("branch") or item["checkpoint_id"],
            "details": {key: value for key, value in item.items() if key != "at"},
        })

    for item in job_events:
        events.append({
            "id": f"job-{item['job_id']}-{item['event_id']}",
            "at": item["at"],
            "kind": "error" if item["status"] == "failed" else "job",
            "status": item["status"],
            "title": item["operation"],
            "summary": (item.get("error") or {}).get("message") or item["job_id"],
            "details": item,
        })

    for call in prompt_items:
        parameters = call.get("parameters") or {}
        events.append({
            "id": f"model-{call['prompt_call_id']}",
            "at": call.get("completed_at") or call["started_at"],
            "started_at": call["started_at"],
            "kind": "error" if call["status"] == "failed" else "model",
            "status": call["status"],
            "title": call["state"],
            "summary": f"{parameters.get('provider', 'model')} · {parameters.get('model', 'unknown')}",
            "details": {
                "prompt_call_id": call["prompt_call_id"],
                "parent_prompt_call_id": call.get("parent_prompt_call_id"),
                "state": call["state"],
                "status": call["status"],
                "started_at": call["started_at"],
                "completed_at": call.get("completed_at"),
                "model": parameters.get("model"),
                "provider": parameters.get("provider"),
                "output_ref": call.get("output_ref"),
                "output_hash": call.get("output_hash"),
                "error": call.get("error"),
            },
        })
        for tool_index, tool_call in enumerate(call.get("tool_calls") or []):
            tool = tool_call.get("tool", "tool")
            kind = "skill" if tool == "read" else "artifact"
            events.append({
                "id": f"tool-{call['prompt_call_id']}-{tool_index}",
                "at": call.get("completed_at") or call["started_at"],
                "kind": kind,
                "status": "succeeded",
                "title": tool,
                "summary": tool_call.get("path") or tool_call.get("source_path") or "工具调用",
                "details": tool_call,
            })

    events.sort(key=lambda item: (item.get("at") or "", item["id"]), reverse=True)
    events = events[:limit]
    active = next(
        (item for item in job_items if item["status"] in {"queued", "running"}),
        None,
    )
    latest_prompt = prompt_items[0] if prompt_items else None
    model_parameters = (latest_prompt or {}).get("parameters") or {}
    return {
        "summary": {
            "active_job": active,
            "stage": store.read(latest_sample_only=True, include_sample_html=False)["state"],
            "model": model_parameters.get("model"),
            "provider": model_parameters.get("provider"),
            "event_count": len(events),
            "error_count": sum(item["kind"] == "error" for item in events),
        },
        "events": events,
    }


@app.get("/api/projects/{project_id}/audit/prompt-calls")
def prompt_calls(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return store_for(project_id).prompt_calls(limit=limit)


@app.get("/api/projects/{project_id}/audit/prompt-calls.jsonl", response_class=PlainTextResponse)
def export_prompt_calls(project_id: str) -> PlainTextResponse:
    store = store_for(project_id)
    return PlainTextResponse(
        store.export_prompt_calls_jsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-prompt-calls.jsonl"'},
    )


@app.get("/api/projects/{project_id}/branches")
def branches(project_id: str) -> dict[str, Any]:
    store = store_for(project_id)
    return store.branches_view()


@app.post("/api/projects/{project_id}/branches")
def create_branch(project_id: str, request: BranchRequest) -> dict[str, Any]:
    store = store_for(project_id)
    if request.mode == "rerun_stage" and request.stage is None:
        raise HTTPException(status_code=422, detail="重跑分支必须指定来源阶段")
    # The registry lock closes the race with job submission: either the job is
    # visible and branching is rejected, or the branch commits before submit.
    with jobs.project_guard(project_id) as active:
        if active:
            raise ConflictError("active_job")
        try:
            store.fork(
                request.checkpoint_id,
                request.name,
                mode=request.mode,
                stage=request.stage,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="检查点不存在") from exc
    return project_view(store)


@app.post("/api/projects/{project_id}/branches/switch")
def switch_branch(project_id: str, request: BranchSwitchRequest) -> dict[str, Any]:
    store = store_for(project_id)
    with jobs.project_guard(project_id) as active:
        if active:
            raise ConflictError("active_job")
        try:
            store.switch_branch(request.checkpoint_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="检查点不存在") from exc
    return project_view(store)


app.mount("/static", StaticFiles(directory=FRONTEND_ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_ROOT / "index.html")
