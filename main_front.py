from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agent_core.jobs import JobRegistry
from agent_core.models import TaskCard
from agent_core.workflow import Workflow, capabilities
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore, list_projects


APP_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = APP_ROOT / "frontend"
PROJECTS_ROOT = Path(os.getenv("PPT_AGENT_PROJECTS_ROOT", FRONTEND_ROOT / "data" / "projects")).resolve()
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
MAX_REQUEST_BYTES = 512 * 1024

app = FastAPI(title="PPT Agent Studio", version="0.1.0")
runtime = ManagedRuntime(APP_ROOT)
jobs = JobRegistry(PROJECTS_ROOT / ".jobs")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(StrictRequest):
    project_id: str = Field(min_length=2, max_length=64)
    task_card: TaskCard


class StartJobRequest(StrictRequest):
    operation: Literal["start_clarification", "generate_narrative", "generate_outline", "regenerate_narrative", "regenerate_outline"]
    checkpoint_id: str = Field(min_length=1, max_length=128)


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


class BranchRequest(StrictRequest):
    checkpoint_id: str
    name: str = Field(min_length=2, max_length=64)


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
    return {**manifest, "capabilities": capabilities(manifest), "active_job": latest_job if latest_job and latest_job["status"] in {"queued", "running"} else None}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ppt-agent", "version": app.version}


@app.get("/api/runtime-context")
def runtime_context() -> dict[str, Any]:
    from runtime.read_tool import SkillReader

    reader = SkillReader(runtime.skills_root, per_call=1000, per_job=1000)
    return {**runtime.public_context(), "skills": reader.index()}


@app.get("/api/projects")
def projects() -> list[dict[str, Any]]:
    return list_projects(PROJECTS_ROOT)


@app.post("/api/projects", status_code=201)
def create_project(request: CreateProjectRequest) -> dict[str, Any]:
    if not PROJECT_ID.fullmatch(request.project_id):
        raise HTTPException(status_code=422, detail="工程 ID 仅允许字母、数字、下划线和连字符")
    store = ProjectStore(PROJECTS_ROOT, request.project_id)
    manifest = store.create(request.task_card.model_dump(), runtime.snapshot())
    return {**manifest, "capabilities": capabilities(manifest), "active_job": None}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return project_view(store_for(project_id))


@app.post("/api/projects/{project_id}/jobs", status_code=202)
def start_job(project_id: str, request: StartJobRequest) -> dict[str, Any]:
    store = store_for(project_id)
    workflow = Workflow(store, runtime)
    actions = {
        "start_clarification": lambda: workflow.start_clarification(request.checkpoint_id),
        "generate_narrative": lambda: workflow.generate_document("narrative_structure", request.checkpoint_id),
        "generate_outline": lambda: workflow.generate_document("slide_outline", request.checkpoint_id),
        "regenerate_narrative": lambda: workflow.generate_document("narrative_structure", request.checkpoint_id, regenerate=True),
        "regenerate_outline": lambda: workflow.generate_document("slide_outline", request.checkpoint_id, regenerate=True),
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
    Workflow(store, runtime).answer_clarification(request.checkpoint_id, request.question_card_id, request.answers)
    return project_view(store)


@app.post("/api/projects/{project_id}/documents/{document_type}/revisions")
def revise_document(project_id: str, document_type: Literal["narrative_structure", "slide_outline"], request: RevisionRequest) -> dict[str, Any]:
    store = store_for(project_id)
    Workflow(store, runtime).edit_document(document_type, request.checkpoint_id, request.markdown_body)
    return project_view(store)


@app.post("/api/projects/{project_id}/documents/{document_type}/approve")
def approve_document(project_id: str, document_type: Literal["narrative_structure", "slide_outline"], request: ApproveRequest) -> dict[str, Any]:
    store = store_for(project_id)
    Workflow(store, runtime).approve_document(document_type, request.checkpoint_id, request.revision_hash)
    return project_view(store)


@app.get("/api/projects/{project_id}/timeline")
def timeline(project_id: str) -> list[dict[str, Any]]:
    return store_for(project_id).events()


@app.get("/api/projects/{project_id}/branches")
def branches(project_id: str) -> dict[str, Any]:
    store = store_for(project_id)
    manifest = store.read()
    return {"current": manifest["branch"], "branches": manifest.get("branches", {}), "checkpoints": store.checkpoints()}


@app.post("/api/projects/{project_id}/branches")
def create_branch(project_id: str, request: BranchRequest) -> dict[str, Any]:
    store = store_for(project_id)
    store.fork(request.checkpoint_id, request.name)
    return project_view(store)


app.mount("/static", StaticFiles(directory=FRONTEND_ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_ROOT / "index.html")
