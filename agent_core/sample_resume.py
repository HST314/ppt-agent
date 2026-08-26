from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Protocol

from agent_core.models import HtmlPptPackage, SampleRevision
from agent_core.workflow_support import generation_provenance, stable_hash
from configs.runtime import ManagedRuntime
from model_router.client import ModelGateway
from runtime.package_tool import DraftPackage
from runtime.read_tool import SkillReader
from storage.project_store import ConflictError, ProjectStore, mark_full_deck_stale


class SampleResumeError(RuntimeError):
    """A rejected sample continuation with a stable browser error contract."""

    def __init__(self, public_code: str, public_message: str):
        super().__init__(f"{public_code}:{public_message}")
        self.public_code = public_code
        self.public_message = public_message


class SampleResumeWorkflowHost(Protocol):
    store: ProjectStore
    runtime: ManagedRuntime
    gateway: ModelGateway

    def _current(
        self,
        manifest: dict[str, Any],
        document_type: str,
    ) -> dict[str, Any] | None: ...

    def _start_prompt_audit(
        self,
        state: str,
        prompt: str,
        *,
        template_id: str,
        template_hash: str,
        skills_hash: str,
        json_mode: bool = False,
        parent_prompt_call_id: str | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> str: ...

    def _fail_prompt_audit(
        self,
        prompt_call_id: str,
        exc: Exception,
        traces: list[dict[str, Any]] | None = None,
    ) -> None: ...

    def _commit_generated_output(
        self,
        prompt_call_id: str,
        commit: Callable[[], dict[str, Any]],
        *,
        traces: list[dict[str, Any]],
        messages: list[dict[str, Any]] | None,
        output_ref: str,
        output_hash: str,
    ) -> dict[str, Any]: ...


def resume_sample(
    workflow: SampleResumeWorkflowHost,
    checkpoint_id: str,
    prompt_call_id: str,
    additional_rounds: int,
    *,
    parse_output: Callable[[str], dict[str, Any]],
    validate_output: Callable[
        [dict[str, Any], DraftPackage, int, set[int], list[int] | None],
        HtmlPptPackage,
    ],
    sample_html_char_budget: int,
) -> dict[str, Any]:
    """Continue a persisted sample tool loop without replaying completed work."""

    if additional_rounds not in {5, 10, 20}:
        raise SampleResumeError(
            "sample_resume_rounds",
            "追加轮次仅支持 5、10 或 20。",
        )
    manifest = workflow.store.read()
    if manifest["checkpoint_id"] != checkpoint_id:
        raise ConflictError("sample_resume_stale:工程检查点已变化，不能继续旧生成。")
    try:
        resume = workflow.store.sample_resume_context(
            prompt_call_id,
            checkpoint_id=checkpoint_id,
        )
    except RuntimeError as exc:
        raise ConflictError(str(exc)) from exc
    if additional_rounds > resume["remaining_tool_rounds"]:
        raise SampleResumeError(
            "sample_resume_limit",
            "追加后会超过整条生成链 100 轮上限。",
        )
    request = resume["parameters"].get("sample_request")
    if not isinstance(request, dict):
        raise SampleResumeError(
            "sample_resume_not_available",
            "该历史尝试缺少续跑上下文。",
        )
    outline = workflow._current(manifest, "slide_outline")
    if (
        not outline
        or outline.get("status") != "approved"
        or outline.get("revision_hash") != request.get("outline_revision_hash")
    ):
        raise ConflictError("sample_resume_stale:逐页大纲已变化，不能继续旧生成。")
    history = manifest.get("samples", [])
    current_hash = manifest.get("current_sample_revision_hash")
    current = next(
        (item for item in history if item.get("revision_hash") == current_hash),
        history[-1] if history else None,
    )
    if request.get("parent_sample_revision_hash") != (
        current.get("revision_hash") if current else None
    ):
        raise ConflictError("sample_resume_stale:当前样品版本已变化，不能继续旧生成。")

    draft = DraftPackage(workflow.runtime.skills_root)
    draft.ingest(workflow.store.load_generated_package_attempt(prompt_call_id))
    messages = resume["messages"]
    prompt = next(
        (
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "user"
        ),
        "Continue the persisted sample generation and return the final package manifest.",
    )
    skill_index = SkillReader(
        workflow.runtime.skills_root,
        per_call=1000,
        per_job=1000,
    ).index()
    template_hash = str(resume["template_hash"])
    cumulative_rounds = int(resume["cumulative_tool_rounds"])
    next_prompt_call_id = workflow._start_prompt_audit(
        "ppt_sample",
        prompt,
        template_id="ppt_sample",
        template_hash=template_hash,
        skills_hash=stable_hash(skill_index),
        json_mode=True,
        parent_prompt_call_id=prompt_call_id,
        audit_context={
            "operation": "continue_sample",
            "generation_checkpoint_id": checkpoint_id,
            "round_limit": cumulative_rounds + additional_rounds,
            "resume_from_prompt_call_id": prompt_call_id,
            "resume_additional_rounds": additional_rounds,
            "sample_request": request,
        },
    )
    attempt_traces: list[dict[str, Any]] = []
    try:
        text, attempt_traces = workflow.gateway.generate(
            "ppt_sample",
            prompt,
            json_mode=True,
            package_draft=draft,
            resume_messages=messages,
            max_tool_rounds=additional_rounds,
            prior_tool_rounds=cumulative_rounds,
            prior_tool_call_count=resume["cumulative_tool_call_count"],
            prior_skill_read_count=resume["cumulative_skill_read_count"],
        )
    except Exception as exc:
        workflow._fail_prompt_audit(next_prompt_call_id, exc, attempt_traces)
        workflow.store.save_generated_package_attempt(
            next_prompt_call_id,
            draft.payload(),
        )
        raise

    try:
        payload = parse_output(text)
        if "pages" in payload:
            raise SampleResumeError(
                "sample_package_invalid",
                "HTML-PPT 包格式不正确，请重新生成。",
            )
        package_output = validate_output(
            payload,
            draft,
            int(request["page_count"]),
            set(request["outline_slide_numbers"]),
            request.get("preserve_source_slide_numbers"),
        )
        workflow.store.save_generated_package_attempt(
            next_prompt_call_id,
            draft.payload(),
        )
    except Exception as exc:
        workflow._fail_prompt_audit(next_prompt_call_id, exc, attempt_traces)
        workflow.store.save_generated_package_attempt(
            next_prompt_call_id,
            draft.payload(),
        )
        raise

    next_revision = max((item["revision"] for item in history), default=0) + 1
    artifact_payload = package_output.model_dump(
        exclude={"files": {"__all__": {"content"}}}
    )
    serialized = json.dumps(artifact_payload, ensure_ascii=False, sort_keys=True)
    all_traces = [*resume.get("tool_calls", []), *attempt_traces]
    resumed_tool_rounds = max(
        (
            int(item["round"])
            for item in all_traces
            if item.get("type") == "tool_call"
            and isinstance(item.get("round"), int)
        ),
        default=cumulative_rounds,
    )
    provenance = {
        **generation_provenance(skill_index, all_traces, serialized),
        "upstream_revision_hash": outline["revision_hash"],
        "model_config_hash": workflow.runtime.model_hash,
        "runtime_config_hash": workflow.runtime.runtime_hash,
        "template_id": "ppt_sample",
        "template_version": 1,
        "template_hash": template_hash,
        "sample_page_count": int(request["page_count"]),
        "source_slide_numbers": [
            item.source_slide_number for item in package_output.slides
        ],
        "sample_html_char_budget_per_page": sample_html_char_budget,
        "sample_repair_attempts": 0,
        "sample_resumed": True,
        "sample_cumulative_tool_rounds": resumed_tool_rounds,
        "prompt_call_id": next_prompt_call_id,
        "prompt_call_ids": [*resume["prompt_call_ids"], next_prompt_call_id],
        "traces": all_traces,
        "package_hash": package_output.package_hash,
        "package_file_count": len(package_output.files),
        "package_total_bytes": draft.total_bytes,
    }
    sample = SampleRevision.create_package(
        package_output,
        revision=next_revision,
        parent=current.get("revision_hash") if current else None,
        feedback=request.get("feedback"),
        provenance=provenance,
    )

    def apply(value: dict[str, Any]) -> dict[str, Any]:
        mark_full_deck_stale(value)
        value.setdefault("samples", []).append(sample.model_dump())
        value["current_sample_revision_hash"] = sample.revision_hash
        value.update(state="ppt_sample", phase="waiting_human_approval")
        return value

    return workflow._commit_generated_output(
        next_prompt_call_id,
        lambda: workflow.store.update(
            apply,
            "sample_generated",
            {
                "revision_hash": sample.revision_hash,
                "slide_count": package_output.slide_count,
                "source_slide_numbers": [
                    item.source_slide_number for item in package_output.slides
                ],
                "resumed_from_prompt_call_id": prompt_call_id,
            },
            expected_checkpoint_id=checkpoint_id,
        ),
        traces=attempt_traces,
        messages=workflow.gateway.last_messages,
        output_ref=sample.revision_hash,
        output_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
    )
