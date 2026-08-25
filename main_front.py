from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
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

app = FastAPI(title="PPT Agent Studio", version="0.2.0")
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
    manifest = store.read()
    latest_job = jobs.latest_for_project(store.project_id)
    with runtime_config_lock:
        sample_page_count = runtime.policy.sample_page_count
    latest_sample = manifest.get("samples", [])[-1:]
    return {
        **manifest,
        # Revision history stays durable in the manifest/checkpoints; the UI
        # only needs the current HTML payload and should not download every
        # prior sample on each poll.
        "samples": latest_sample,
        "sample_page_count": sample_page_count,
        "capabilities": capabilities(manifest),
        "active_job": latest_job if latest_job and latest_job["status"] in {"queued", "running"} else None,
        "progress_snapshots": store.progress_snapshots(),
    }


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


@app.get("/api/projects/{project_id}/timeline")
def timeline(project_id: str) -> list[dict[str, Any]]:
    return store_for(project_id).events()


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
    with jobs.lock:
        active = jobs.latest_for_project(project_id)
        if active and active["status"] in {"queued", "running"}:
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
    with jobs.lock:
        active = jobs.latest_for_project(project_id)
        if active and active["status"] in {"queued", "running"}:
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
