from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import ValidationError

from agent_core.full_deck_generation import (
    FullDeckGenerationError,
    generate_full_deck as run_full_deck_generation,
    pending_full_deck_segments,
)
from agent_core.full_deck_revision import create_full_deck_revision
from agent_core.models import (
    DocumentRevision,
    FullDeck,
    FullDeckContentRef,
    FullDeckDerivedFrom,
    FullDeckOutlineRef,
    FullDeckPageSlot,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
    Question,
    QuestionCard,
    SampleRevision,
    TaskCard,
)
from agent_core.full_deck_composer import (
    FullDeckComposerError,
    normalized_page_content_graph,
)
from agent_core.workflow_support import (
    current_full_deck_revision as _current_full_deck_revision,
    generation_provenance,
    outline_slide_catalog as _outline_slide_catalog,
    package_model as _package_model,
    stable_hash,
)
from configs.runtime import ManagedRuntime
from model_router.client import ModelGateway, ModelOutputError, SYSTEM_MESSAGE
from runtime.package_tool import DraftPackage, PackageToolError
from runtime.read_tool import SkillReader
from storage.project_store import (
    ConflictError,
    ProjectStore,
    mark_full_deck_stale,
)


DocumentType = Literal["narrative_structure", "slide_outline"]
SAMPLE_HTML_CHAR_BUDGET = 7_000
SAMPLE_MAX_REPAIR_ATTEMPTS = 2


class WorkflowError(RuntimeError):
    pass


class SampleGenerationError(WorkflowError):
    """A rejected model sample with a stable public error contract."""

    def __init__(self, public_code: str, public_message: str, repair_reason: str):
        super().__init__(repair_reason)
        self.public_code = public_code
        self.public_message = public_message
        self.repair_reason = repair_reason


class _SlideMarkupParser(HTMLParser):
    """Collect the static HTML-PPT page markers without executing package code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.slide_ids: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if "slide" in classes:
            self.slide_ids.append(attributes.get("data-slide-id"))


def _package_slide_ids(draft: DraftPackage) -> list[str]:
    try:
        index_html = draft.read("index.html")["content"]
    except PackageToolError as exc:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 包格式不正确，自动修复后仍未成功，请重试。",
            f"无法读取 index.html：{exc}",
        ) from exc
    parser = _SlideMarkupParser()
    parser.feed(index_html)
    if any(not slide_id for slide_id in parser.slide_ids):
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 页面标识不完整，自动修复后仍未成功，请重试。",
            "每个 class=\"slide\" 的静态页面元素都必须包含非空 data-slide-id。",
        )
    return [str(slide_id) for slide_id in parser.slide_ids]


def _parse_sample_output(text: str) -> dict[str, Any]:
    try:
        return ModelGateway.parse_json(text)
    except json.JSONDecodeError as exc:
        raise SampleGenerationError(
            "sample_json_incomplete",
            "样品输出不完整，自动修复后仍未成功，请重试。",
            f"JSON 未完整闭合：{exc.msg}（字符 {exc.pos}）。请缩短 HTML/CSS 并重新返回完整 JSON。",
        ) from exc
    except (ModelOutputError, TypeError, ValueError) as exc:
        raise SampleGenerationError(
            "sample_output_invalid",
            "样品格式不正确，自动修复后仍未成功，请重试。",
            f"JSON 顶层结构无效：{exc}",
        ) from exc


def _validate_package_output(
    payload: dict[str, Any],
    draft: DraftPackage,
    slide_count: int,
    outline_slide_numbers: set[int],
    preserve_source_slide_numbers: list[int] | None = None,
) -> HtmlPptPackage:
    embedded = payload.pop("files", [])
    if embedded:
        if not isinstance(embedded, list):
            raise SampleGenerationError(
                "sample_package_invalid",
                "HTML-PPT 包格式不正确，自动修复后仍未成功，请重试。",
                "files 必须是文件对象数组。",
            )
        try:
            draft.ingest(embedded)
        except PackageToolError as exc:
            raise SampleGenerationError(
                "sample_package_invalid",
                "HTML-PPT 包含无效文件，自动修复后仍未成功，请重试。",
                f"包文件校验失败：{exc}",
            ) from exc
    try:
        output = HtmlPptPackage.model_validate({**payload, "files": draft.payload()})
    except ValidationError as exc:
        reasons = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            message = " ".join(error["msg"].removeprefix("Value error, ").split())[:180]
            reasons.append(f"{location}: {message}" if location else message)
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 包格式不正确，自动修复后仍未成功，请重试。",
            "包契约校验失败：" + ("；".join(reasons[:4]) or "未知错误"),
        ) from exc
    if output.slide_count != slide_count:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 页数不正确，自动修复后仍未成功，请重试。",
            f"slide_count 必须为 {slide_count}，实际为 {output.slide_count}。",
        )
    source_slide_numbers = [item.source_slide_number for item in output.slides]
    if any(number is None for number in source_slide_numbers):
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 样品范围缺失，自动修复后仍未成功，请重试。",
            "slides 中的每一项都必须返回 source_slide_number。",
        )
    selected_numbers = [int(number) for number in source_slide_numbers if number is not None]
    expected_contiguous = list(range(selected_numbers[0], selected_numbers[0] + slide_count))
    if selected_numbers != expected_contiguous:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 样品范围不连续，自动修复后仍未成功，请重试。",
            f"source_slide_number 必须按大纲顺序连续，实际为 {selected_numbers}。",
        )
    invalid_numbers = [number for number in selected_numbers if number not in outline_slide_numbers]
    if invalid_numbers:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 样品范围超出大纲，自动修复后仍未成功，请重试。",
            f"以下 source_slide_number 不在已确认大纲中：{invalid_numbers}。",
        )
    if preserve_source_slide_numbers and selected_numbers != preserve_source_slide_numbers:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 修改稿更换了样品范围，自动修复后仍未成功，请重试。",
            f"修改样品必须保持原大纲页 {preserve_source_slide_numbers}，实际为 {selected_numbers}。",
        )
    declared_slide_ids = [item.slide_id for item in output.slides]
    actual_slide_ids = _package_slide_ids(draft)
    if actual_slide_ids != declared_slide_ids:
        raise SampleGenerationError(
            "sample_package_invalid",
            "HTML-PPT 实际页数或页面标识不正确，自动修复后仍未成功，请重试。",
            "index.html 中 class=\"slide\" 的 data-slide-id 必须与清单 slides 一一对应；"
            f"清单为 {declared_slide_ids}，实际为 {actual_slide_ids}。",
        )
    return output


def _initialize_full_deck(
    outline: dict[str, Any],
    sample: dict[str, Any],
) -> tuple[FullDeck, FullDeckRevision]:
    package = _package_model(sample["package"])
    outline_catalog = sorted(
        _outline_slide_catalog(outline["markdown_body"]),
        key=lambda item: item["source_slide_number"],
    )
    sample_by_number: dict[int, Any] = {}
    for slide in package.slides:
        number = slide.source_slide_number
        if number is None or number in sample_by_number:
            raise WorkflowError("sample slides must map uniquely to approved outline pages")
        sample_by_number[number] = slide
    outline_numbers = {item["source_slide_number"] for item in outline_catalog}
    if not set(sample_by_number).issubset(outline_numbers):
        raise WorkflowError("sample slides must map to existing approved outline pages")

    pages: list[FullDeckPageSlot] = []
    for position, outline_page in enumerate(outline_catalog):
        number = outline_page["source_slide_number"]
        slide = sample_by_number.get(number)
        slot_id = "slot_" + hashlib.sha256(
            f"{outline['revision_hash']}\n{number}".encode("utf-8")
        ).hexdigest()[:24]
        if slide is None:
            pages.append(FullDeckPageSlot(
                slot_id=slot_id,
                position=position,
                outline_ref=FullDeckOutlineRef(
                    outline_revision_hash=outline["revision_hash"],
                    source_slide_number=number,
                ),
                title=outline_page["title"],
                status="pending",
                source_type="pending",
            ))
            continue
        graph = normalized_page_content_graph(package, slide.slide_id)
        pages.append(FullDeckPageSlot(
            slot_id=slot_id,
            position=position,
            outline_ref=FullDeckOutlineRef(
                outline_revision_hash=outline["revision_hash"],
                source_slide_number=number,
            ),
            title=outline_page["title"],
            status="ready",
            source_type="approved_sample",
            content_ref=FullDeckContentRef(
                revision_hash=sample["revision_hash"],
                package_hash=package.package_hash or package.content_hash(),
                slide_id=slide.slide_id,
                slide_content_hash=graph.content_hash,
            ),
            derived_from=FullDeckDerivedFrom(
                sample_revision_hash=sample["revision_hash"],
                sample_slide_id=slide.slide_id,
            ),
        ))

    plan = FullDeckPlan(pages=pages)
    full_deck_id = "deck_" + uuid4().hex[:24]
    sample_provenance = sample.get("provenance", {})
    provenance = {
        "outline_revision_hash": outline["revision_hash"],
        "approved_sample_revision_hash": sample["revision_hash"],
        "model_config_hash": sample_provenance.get("model_config_hash"),
        "runtime_config_hash": sample_provenance.get("runtime_config_hash"),
        "skills_hash": sample_provenance.get("skills_hash"),
        "changed_slot_ids": [page.slot_id for page in pages if page.status == "ready"],
    }
    revision = FullDeckRevision.create(
        full_deck_id=full_deck_id,
        revision=1,
        parent=None,
        feedback="由已确认样品初始化",
        plan=plan,
        provenance=provenance,
    )
    root = FullDeck(
        full_deck_id=full_deck_id,
        approved_sample_revision_hash=sample["revision_hash"],
        outline_revision_hash=outline["revision_hash"],
        current_revision_hash=revision.revision_hash,
        revision_refs=[{
            "revision_hash": revision.revision_hash,
            "status": revision.status,
        }],
    )
    return root, revision


def _current_sample(manifest: dict[str, Any]) -> dict[str, Any] | None:
    current_hash = manifest.get("current_sample_revision_hash")
    return next(
        (
            item for item in manifest.get("samples", [])
            if item.get("revision_hash") == current_hash
        ),
        (manifest.get("samples") or [None])[-1],
    )


def _sample_can_enter_full_deck(
    sample: dict[str, Any] | None,
    outline: dict[str, Any] | None = None,
) -> bool:
    if not sample or sample.get("status") == "stale" or not sample.get("package"):
        return False
    numbers = [
        slide.get("source_slide_number")
        for slide in sample["package"].get("slides", [])
    ]
    if not (
        numbers
        and all(isinstance(number, int) for number in numbers)
        and len(numbers) == len(set(numbers))
    ):
        return False
    if outline is None:
        return True
    if outline.get("status") != "approved":
        return False
    try:
        outline_numbers = {
            item["source_slide_number"]
            for item in _outline_slide_catalog(outline["markdown_body"])
        }
    except (KeyError, ValueError, WorkflowError):
        return False
    return set(numbers).issubset(outline_numbers)


def capabilities(
    manifest: dict[str, Any],
    *,
    active_job: bool = False,
) -> list[str]:
    active_job = active_job or bool(manifest.get("active_job_id"))
    state, phase = manifest["state"], manifest["phase"]
    caps = ["inspect", "branch"]
    if state == "intake" and phase == "ready_for_clarification":
        caps.append("start_clarification")
    elif state == "intake_clarify" and phase == "waiting_clarification":
        caps.append("answer_clarification")
    elif state == "narrative_structure":
        if phase == "ready_to_generate":
            caps.append("generate_narrative")
        elif phase == "waiting_human_approval":
            caps.extend(["edit_narrative", "approve_narrative", "regenerate_narrative"])
    elif state == "slide_outline":
        if phase == "ready_to_generate":
            caps.append("generate_outline")
        elif phase == "waiting_human_approval":
            caps.extend(["edit_outline", "approve_outline", "regenerate_outline"])
        elif phase == "completed":
            caps.append("start_sample_stage")
    elif state == "ppt_sample":
        if phase == "ready_to_generate":
            caps.append("generate_sample")
        elif phase == "waiting_human_approval":
            caps.extend(["revise_sample", "approve_sample", "regenerate_sample"])
    elif state == "ppt_full":
        current_full_deck = _current_full_deck_revision(manifest)
        if current_full_deck:
            caps.append("inspect_full_deck")
            if not active_job and current_full_deck.get("status") != "stale":
                caps.extend([
                    "regenerate_full_deck",
                    "revise_full_deck",
                    "restore_full_deck_revision",
                    "branch_full_deck_revision",
                ])
                if any(
                    page.get("status") == "pending"
                    for page in current_full_deck.get("plan", {}).get("pages", [])
                ):
                    caps.append("generate_full_deck")
                if (
                    current_full_deck.get("status") == "pending_approval"
                    and current_full_deck.get("package")
                ):
                    caps.append("approve_full_deck")
    if manifest.get("documents", {}).get("narrative_structure") and "edit_narrative" not in caps:
        caps.append("edit_narrative")
    if manifest.get("documents", {}).get("slide_outline") and "edit_outline" not in caps:
        caps.append("edit_outline")
    current_sample = _current_sample(manifest)
    if current_sample and current_sample.get("status") != "stale":
        for capability in ("revise_sample", "regenerate_sample"):
            if capability not in caps:
                caps.append(capability)
    if manifest.get("full_deck") and "inspect_full_deck" not in caps:
        caps.append("inspect_full_deck")
    if (
        not active_job
        and not manifest.get("full_deck")
        and _sample_can_enter_full_deck(
            current_sample,
            (manifest.get("documents", {}).get("slide_outline") or [None])[-1],
        )
    ):
        caps.append("enter_full_deck")
    return caps


class Workflow:
    def __init__(self, store: ProjectStore, runtime: ManagedRuntime):
        self.store = store
        self.runtime = runtime
        self.gateway = ModelGateway(runtime)
        self.templates_root = Path(__file__).parents[1] / "prompt_engine" / "templates"

    def _template(self, name: str) -> tuple[str, str]:
        path = self.templates_root / name
        content = path.read_text(encoding="utf-8")
        return content, "sha256:" + hashlib.sha256(content.encode()).hexdigest()

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
    ) -> str:
        self.gateway.last_messages = None
        binding = self.runtime.models.binding_for(state)
        parameters = dict(binding.parameters)
        if json_mode:
            parameters["response_format"] = {"type": "json_object"}
        return self.store.start_prompt_call(
            state=state,
            messages=[
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            template_id=template_id,
            template_version=1,
            template_hash=template_hash,
            model_config_hash=self.runtime.model_hash,
            runtime_config_hash=self.runtime.runtime_hash,
            skills_hash=skills_hash,
            parameters={
                "provider": binding.provider,
                "model": binding.model,
                "parameters": parameters,
                **(audit_context or {}),
            },
            parent_prompt_call_id=parent_prompt_call_id,
        )

    def _fail_prompt_audit(
        self,
        prompt_call_id: str,
        exc: Exception,
        traces: list[dict[str, Any]] | None = None,
    ) -> None:
        self.store.finish_prompt_call(
            prompt_call_id,
            status="failed",
            traces=traces,
            messages=self.gateway.last_messages,
            error={
                "type": type(exc).__name__,
                "code": getattr(exc, "public_code", None),
                "message": str(exc)[:1000],
            },
        )

    def _commit_generated_output(
        self,
        prompt_call_id: str,
        commit: Callable[[], dict[str, Any]],
        *,
        traces: list[dict[str, Any]],
        messages: list[dict[str, Any]] | None,
        output_ref: str,
        output_hash: str,
    ) -> dict[str, Any]:
        """Commit a generated artifact before publishing its successful audit terminal."""

        try:
            manifest = commit()
        except ConflictError as exc:
            self.store.finish_prompt_call(
                prompt_call_id,
                status="conflicted" if str(exc) == "stale_revision" else "failed",
                traces=traces,
                error={
                    "type": type(exc).__name__,
                    "code": str(exc)[:80],
                    "message": str(exc)[:1000],
                },
            )
            raise
        except Exception as exc:
            self._fail_prompt_audit(prompt_call_id, exc, traces)
            raise
        self.store.finish_prompt_call(
            prompt_call_id,
            status="completed",
            traces=traces,
            messages=messages,
            output_ref=output_ref,
            output_hash=output_hash,
        )
        return manifest

    @staticmethod
    def _require(manifest: dict[str, Any], capability: str, checkpoint_id: str | None = None) -> None:
        if checkpoint_id and manifest["checkpoint_id"] != checkpoint_id:
            raise ConflictError("stale_revision")
        if capability not in capabilities(manifest):
            raise ConflictError(f"capability_not_available:{capability}")

    def start_clarification(self, checkpoint_id: str) -> dict[str, Any]:
        manifest = self.store.read()
        self._require(manifest, "start_clarification", checkpoint_id)
        reader = SkillReader(self.runtime.skills_root, per_call=1000, per_job=1000)
        skill_index = reader.index()
        template, template_hash = self._template("clarify_questions.md")
        prompt = (
            template + "\n\nGenerate concise clarification questions for this presentation task. Return JSON with a questions array. "
            "Each item must contain field, prompt, impact, options[{value,label,recommended}], allow_free_text. "
            f"Ask no more than {self.runtime.policy.max_auto_questions}. Task:\n"
            + json.dumps(manifest["task_card"], ensure_ascii=False)
            + "\nAvailable skill index:\n"
            + json.dumps(skill_index, ensure_ascii=False)
        )
        skills_hash = stable_hash(skill_index)
        prompt_call_id = self._start_prompt_audit(
            "intake_clarify",
            prompt,
            template_id="clarify_questions",
            template_hash=template_hash,
            skills_hash=skills_hash,
            json_mode=True,
        )
        traces: list[dict[str, Any]] = []
        try:
            text, traces = self.gateway.generate("intake_clarify", prompt, json_mode=True)
            payload = self.gateway.parse_json(text)
            raw_questions = payload.get("questions", [])[: self.runtime.policy.max_auto_questions]
            questions = [Question.model_validate(item) for item in raw_questions]
            if not questions:
                raise WorkflowError("model returned no clarification questions")
        except Exception as exc:
            self._fail_prompt_audit(prompt_call_id, exc, traces)
            raise
        card_id = "questions_" + uuid4().hex[:16]
        def apply(value: dict[str, Any]) -> dict[str, Any]:
            card = QuestionCard(
                question_card_id=card_id,
                checkpoint_id=value["checkpoint_id"],
                questions=questions,
                provenance={
                    **generation_provenance(skill_index, traces, text),
                    "model_config_hash": self.runtime.model_hash,
                    "runtime_config_hash": self.runtime.runtime_hash,
                    "template_id": "clarify_questions",
                    "template_version": 1,
                    "template_hash": template_hash,
                    "prompt_call_id": prompt_call_id,
                    "traces": traces,
                },
            )
            value.update(state="intake_clarify", phase="waiting_clarification", question_card=card.model_dump())
            value["last_tool_traces"] = traces
            value["last_template"] = {"template_id": "clarify_questions", "template_version": 1, "template_hash": template_hash}
            return value

        return self._commit_generated_output(
            prompt_call_id,
            lambda: self.store.update(
                apply,
                "clarification_generated",
                {"question_card_id": card_id},
                expected_checkpoint_id=checkpoint_id,
            ),
            traces=traces,
            messages=self.gateway.last_messages,
            output_ref=card_id,
            output_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        )

    def answer_clarification(self, checkpoint_id: str, question_card_id: str, answers: dict[str, str]) -> dict[str, Any]:
        manifest = self.store.read()
        self._require(manifest, "answer_clarification", checkpoint_id)
        card = manifest.get("question_card") or {}
        if card.get("question_card_id") != question_card_id or card.get("checkpoint_id") != checkpoint_id:
            raise ConflictError("stale_question_card")
        expected = {question["question_id"] for question in card.get("questions", [])}
        if set(answers) != expected or any(not value.strip() for value in answers.values()):
            raise ValueError("all current clarification questions must be answered")
        clarified = {
            question["field"]: {"question": question["prompt"], "answer": answers[question["question_id"]]}
            for question in card["questions"]
        }

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["clarification_answers"] = clarified
            value.update(state="narrative_structure", phase="ready_to_generate")
            return value

        return self.store.update(
            apply,
            "clarification_answered",
            {"question_card_id": question_card_id},
            expected_checkpoint_id=checkpoint_id,
        )

    def generate_document(self, document_type: DocumentType, checkpoint_id: str, *, regenerate: bool = False) -> dict[str, Any]:
        manifest = self.store.read()
        if regenerate:
            cap = "regenerate_narrative" if document_type == "narrative_structure" else "regenerate_outline"
        else:
            cap = "generate_narrative" if document_type == "narrative_structure" else "generate_outline"
        self._require(manifest, cap, checkpoint_id)
        state = document_type
        upstream = None
        if document_type == "slide_outline":
            upstream = self._current(manifest, "narrative_structure")
            if not upstream or upstream["status"] != "approved":
                raise ConflictError("approved narrative required")
        skill_index = SkillReader(self.runtime.skills_root, per_call=1000, per_job=1000).index()
        template_name = "narrative_structure.md" if document_type == "narrative_structure" else "slide_outline.md"
        template, template_hash = self._template(template_name)
        prompt = (
            template + f"\n\nCreate the final {document_type} artifact as Markdown. Do not wrap it in a code fence. "
            "Choose an appropriate narrative method freely. You may use read to consult the skill index.\n"
            f"Task card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
            f"Clarification answers:\n{json.dumps(manifest['clarification_answers'], ensure_ascii=False)}\n"
            f"Approved upstream document:\n{(upstream or {}).get('markdown_body', 'none')}\n"
            f"Skill index:\n{json.dumps(skill_index, ensure_ascii=False)}"
        )
        skills_hash = stable_hash(skill_index)
        prompt_call_id = self._start_prompt_audit(
            state,
            prompt,
            template_id=template_name.removesuffix(".md"),
            template_hash=template_hash,
            skills_hash=skills_hash,
        )
        traces: list[dict[str, Any]] = []
        try:
            markdown, traces = self.gateway.generate(state, prompt)
        except Exception as exc:
            self._fail_prompt_audit(prompt_call_id, exc, traces)
            raise
        task_hash = "sha256:" + hashlib.sha256(json.dumps(manifest["task_card"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        history = manifest["documents"][document_type]
        parent = history[-1]["revision_hash"] if history else None
        document = DocumentRevision.create(
            document_type,
            markdown,
            revision=len(history) + 1,
            parent=parent,
            created_by="agent",
            provenance={
                **generation_provenance(skill_index, traces, markdown),
                "task_revision_hash": task_hash,
                "upstream_revision_hash": upstream["revision_hash"] if upstream else None,
                "model_config_hash": self.runtime.model_hash,
                "runtime_config_hash": self.runtime.runtime_hash,
                "template_id": template_name.removesuffix(".md"),
                "template_version": 1,
                "template_hash": template_hash,
                "prompt_call_id": prompt_call_id,
                "traces": traces,
            },
        )
        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["documents"][document_type].append(document.model_dump())
            value.update(state=document_type, phase="waiting_human_approval")
            return value

        return self._commit_generated_output(
            prompt_call_id,
            lambda: self.store.update(
                apply,
                "document_generated",
                {"document_type": document_type, "revision_hash": document.revision_hash},
                expected_checkpoint_id=checkpoint_id,
            ),
            traces=traces,
            messages=self.gateway.last_messages,
            output_ref=document.revision_hash,
            output_hash="sha256:" + hashlib.sha256(markdown.encode()).hexdigest(),
        )

    def edit_document(self, document_type: DocumentType, checkpoint_id: str, markdown: str) -> dict[str, Any]:
        if not markdown.strip():
            raise ValueError("markdown must not be empty")
        markdown = markdown.strip()
        manifest = self.store.read()
        cap = "edit_narrative" if document_type == "narrative_structure" else "edit_outline"
        self._require(manifest, cap, checkpoint_id)
        current = self._current(manifest, document_type)
        if not current:
            raise WorkflowError("document not found")
        document = DocumentRevision.create(
            document_type,
            markdown.strip(),
            revision=current["revision"] + 1,
            parent=current["revision_hash"],
            created_by="human",
            provenance={
                **current.get("provenance", {}),
                "output_hash": "sha256:" + hashlib.sha256(markdown.encode()).hexdigest(),
            },
        )

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["documents"][document_type].append(document.model_dump())
            if document_type == "narrative_structure":
                for prior in value["documents"]["slide_outline"]:
                    prior["status"] = "stale"
            for prior in value.get("samples", []):
                prior["status"] = "stale"
            mark_full_deck_stale(value)
            value.update(state=document_type, phase="waiting_human_approval")
            return value

        return self.store.update(
            apply,
            "document_revised",
            {"document_type": document_type, "revision_hash": document.revision_hash},
            expected_checkpoint_id=checkpoint_id,
        )

    def approve_document(self, document_type: DocumentType, checkpoint_id: str, revision_hash: str) -> dict[str, Any]:
        manifest = self.store.read()
        cap = "approve_narrative" if document_type == "narrative_structure" else "approve_outline"
        self._require(manifest, cap, checkpoint_id)
        current = self._current(manifest, document_type)
        if not current or current["revision_hash"] != revision_hash:
            raise ConflictError("stale_revision")

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["documents"][document_type][-1]["status"] = "approved"
            if document_type == "narrative_structure":
                value.update(state="slide_outline", phase="ready_to_generate")
            else:
                value.update(state="ppt_sample", phase="ready_to_generate")
            return value

        return self.store.update(
            apply,
            "document_approved",
            {"document_type": document_type, "revision_hash": revision_hash},
            expected_checkpoint_id=checkpoint_id,
        )

    def start_sample_stage(self, checkpoint_id: str) -> dict[str, Any]:
        """Move legacy completed-outline projects into the newly added sample stage."""

        manifest = self.store.read()
        self._require(manifest, "start_sample_stage", checkpoint_id)
        outline = self._current(manifest, "slide_outline")
        if not outline or outline["status"] != "approved":
            raise ConflictError("approved outline required")

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value.setdefault("samples", [])
            value.update(state="ppt_sample", phase="ready_to_generate")
            return value

        return self.store.update(
            apply,
            "sample_stage_started",
            {},
            expected_checkpoint_id=checkpoint_id,
        )

    def generate_sample(
        self,
        checkpoint_id: str,
        *,
        feedback: str | None = None,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        manifest = self.store.read()
        normalized_feedback = feedback.strip() if feedback else None
        capability = "revise_sample" if normalized_feedback else "regenerate_sample" if regenerate else "generate_sample"
        self._require(manifest, capability, checkpoint_id)
        outline = self._current(manifest, "slide_outline")
        if not outline or outline["status"] != "approved":
            raise ConflictError("approved outline required")
        history = manifest.get("samples", [])
        current_hash = manifest.get("current_sample_revision_hash")
        current = next(
            (item for item in history if item.get("revision_hash") == current_hash),
            history[-1] if history else None,
        )
        if normalized_feedback and not current:
            raise WorkflowError("sample not found")

        skill_index = SkillReader(self.runtime.skills_root, per_call=1000, per_job=1000).index()
        template, template_hash = self._template("ppt_sample.md")
        previous_package = (current or {}).get("package") or {}
        configured_page_count = self.runtime.policy.sample_page_count
        page_count = (
            previous_package.get("slide_count", configured_page_count)
            if normalized_feedback else configured_page_count
        )
        outline_catalog = _outline_slide_catalog(outline["markdown_body"])
        if len(outline_catalog) < page_count:
            raise WorkflowError(
                f"approved outline has {len(outline_catalog)} slides, fewer than the {page_count}-page sample"
            )
        outline_slide_numbers = {
            item["source_slide_number"] for item in outline_catalog
        }
        previous = {
            "entrypoint": previous_package.get("entrypoint"),
            "title": previous_package.get("title"),
            "slide_count": previous_package.get("slide_count"),
            "slides": previous_package.get("slides", []),
            "files": [item.get("path") for item in previous_package.get("files", [])],
        } if current else None
        preserve_source_slide_numbers = None
        if normalized_feedback and previous:
            previous_numbers = [
                item.get("source_slide_number") for item in previous.get("slides", [])
            ]
            if previous_numbers and all(isinstance(number, int) for number in previous_numbers):
                preserve_source_slide_numbers = previous_numbers
        prompt = (
            template
            + f"\n\nSAMPLE_PAGE_COUNT: {page_count}\n"
            + "SAMPLE_STAGE_ONLY: true\n"
            + f"OUTLINE_SLIDES_JSON: {json.dumps(outline_catalog, ensure_ascii=False)}\n"
            + "PRESERVE_SOURCE_SLIDE_NUMBERS: "
            + (
                json.dumps(preserve_source_slide_numbers, ensure_ascii=False)
                if preserve_source_slide_numbers else "none"
            )
            + "\n"
            + f"SAMPLE_HTML_CHAR_BUDGET_PER_PAGE: {SAMPLE_HTML_CHAR_BUDGET}\n"
            + f"Task card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
            + f"Approved slide outline:\n{outline['markdown_body']}\n"
            + f"Previous HTML-PPT package manifest:\n{json.dumps(previous, ensure_ascii=False) if current else 'none'}\n"
            + f"Revision feedback:\n{normalized_feedback or 'none'}\n"
            + f"Skill index:\n{json.dumps(skill_index, ensure_ascii=False)}"
        )
        traces: list[dict[str, Any]] = []
        prompt_call_ids: list[str] = []
        repair_attempts = 0
        current_prompt = prompt
        parent_prompt_call_id: str | None = None
        successful_prompt_call_id: str | None = None
        package_output: HtmlPptPackage | None = None
        successful_draft: DraftPackage | None = None
        for attempt in range(SAMPLE_MAX_REPAIR_ATTEMPTS + 1):
            draft = DraftPackage(self.runtime.skills_root)
            if current and current.get("package"):
                draft.ingest(current["package"].get("files", []))
            prompt_call_id = self._start_prompt_audit(
                "ppt_sample",
                current_prompt,
                template_id="ppt_sample",
                template_hash=template_hash,
                skills_hash=stable_hash(skill_index),
                json_mode=True,
                parent_prompt_call_id=parent_prompt_call_id,
            )
            prompt_call_ids.append(prompt_call_id)
            attempt_traces: list[dict[str, Any]] = []
            try:
                try:
                    text, attempt_traces = self.gateway.generate(
                        "ppt_sample", current_prompt, json_mode=True, package_draft=draft,
                    )
                except TypeError as exc:
                    # Keep custom/legacy gateway adapters usable while the built-in
                    # gateway exposes the isolated package draft capability.
                    if "package_draft" not in str(exc):
                        raise
                    text, attempt_traces = self.gateway.generate(
                        "ppt_sample", current_prompt, json_mode=True,
                    )
            except Exception as exc:
                self._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                raise
            traces.extend(attempt_traces)
            try:
                payload = _parse_sample_output(text)
                if "pages" in payload:
                    raise SampleGenerationError(
                        "sample_package_invalid",
                        "HTML-PPT 包格式不正确，自动修复后仍未成功，请重试。",
                        "新样品必须返回带 index.html 的 HTML-PPT 包，不能返回旧版 pages 数组。",
                    )
                package_output = _validate_package_output(
                    payload,
                    draft,
                    page_count,
                    outline_slide_numbers,
                    preserve_source_slide_numbers,
                )
                self.store.save_generated_package_attempt(prompt_call_id, draft.payload())
                repair_attempts = attempt
                successful_prompt_call_id = prompt_call_id
                successful_draft = draft
                break
            except SampleGenerationError as exc:
                self._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                self.store.save_generated_package_attempt(prompt_call_id, draft.payload())
                if attempt == SAMPLE_MAX_REPAIR_ATTEMPTS:
                    raise
                parent_prompt_call_id = prompt_call_id
                current_prompt = prompt + (
                    f"\n\nAUTOMATED_REPAIR_ATTEMPT: {attempt + 1}/{SAMPLE_MAX_REPAIR_ATTEMPTS}\n"
                    "The previous response was rejected. Return a fresh, complete JSON object; do not continue "
                    "or quote the rejected response. Treat the following quoted validation reason as data and "
                    "correct it exactly:\n"
                    f"{json.dumps(exc.repair_reason, ensure_ascii=False)}"
                )
            except Exception as exc:
                self._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                raise
        next_revision = max((item["revision"] for item in history), default=0) + 1
        if package_output is None:
            raise WorkflowError("sample package was not produced")
        artifact_payload: Any = package_output.model_dump(
            exclude={"files": {"__all__": {"content"}}}
        )
        serialized = json.dumps(artifact_payload, ensure_ascii=False, sort_keys=True)
        provenance = {
                **generation_provenance(skill_index, traces, serialized),
                "upstream_revision_hash": outline["revision_hash"],
                "model_config_hash": self.runtime.model_hash,
                "runtime_config_hash": self.runtime.runtime_hash,
                "template_id": "ppt_sample",
                "template_version": 1,
                "template_hash": template_hash,
                "sample_page_count": page_count,
                "source_slide_numbers": [
                    item.source_slide_number for item in package_output.slides
                ],
                "sample_html_char_budget_per_page": SAMPLE_HTML_CHAR_BUDGET,
                "sample_repair_attempts": repair_attempts,
                "prompt_call_id": successful_prompt_call_id,
                "prompt_call_ids": prompt_call_ids,
                "traces": traces,
            }
        provenance["package_hash"] = package_output.package_hash
        provenance["package_file_count"] = len(package_output.files)
        provenance["package_total_bytes"] = successful_draft.total_bytes if successful_draft else 0
        sample = SampleRevision.create_package(
            package_output,
            revision=next_revision,
            parent=current["revision_hash"] if current else None,
            feedback=normalized_feedback,
            provenance=provenance,
        )
        if successful_prompt_call_id is None:
            raise WorkflowError("sample prompt audit is incomplete")
        def apply(value: dict[str, Any]) -> dict[str, Any]:
            mark_full_deck_stale(value)
            value.setdefault("samples", []).append(sample.model_dump())
            value["current_sample_revision_hash"] = sample.revision_hash
            value.update(state="ppt_sample", phase="waiting_human_approval")
            return value

        return self._commit_generated_output(
            successful_prompt_call_id,
            lambda: self.store.update(
                apply,
                "sample_revised" if normalized_feedback else "sample_generated",
                {
                    "revision_hash": sample.revision_hash,
                    "slide_count": package_output.slide_count,
                    "source_slide_numbers": [
                        item.source_slide_number for item in package_output.slides
                    ],
                },
                expected_checkpoint_id=checkpoint_id,
            ),
            traces=attempt_traces,
            messages=self.gateway.last_messages,
            output_ref=sample.revision_hash,
            output_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        )

    def enter_full_deck(
        self,
        checkpoint_id: str,
        sample_revision_hash: str,
    ) -> dict[str, Any]:
        """Approve the selected sample and initialize full-deck R1 atomically."""

        manifest = self.store.read()
        if manifest["checkpoint_id"] != checkpoint_id:
            raise ConflictError("stale_revision:工程已更新，请刷新后重试。")
        if manifest.get("full_deck"):
            raise ConflictError(
                "full_deck_already_initialized:请继续当前全稿，或从样品检查点创建新分支。"
            )
        current = _current_sample(manifest)
        if not current or current.get("revision_hash") != sample_revision_hash:
            raise ConflictError("stale_revision:当前样品已变化，请刷新后重试。")
        if not _sample_can_enter_full_deck(current):
            raise ConflictError(
                "full_deck_plan_invalid:当前样品缺少可追溯的 HTML-PPT 页映射，请重新生成样品后重试。"
            )
        outline = self._current(manifest, "slide_outline")
        if not outline or outline.get("status") != "approved":
            raise ConflictError(
                "full_deck_plan_invalid:逐页大纲尚未确认，请先确认大纲后重试。"
            )
        try:
            root, revision = _initialize_full_deck(outline, current)
        except (FullDeckComposerError, ValidationError, ValueError, WorkflowError) as exc:
            raise ConflictError(
                "full_deck_plan_invalid:样品页与已确认大纲无法建立完整映射，请修复样品后重试。"
            ) from exc

        was_approved = current.get("status") == "approved"

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            selected = next(
                (
                    item for item in value.get("samples", [])
                    if item.get("revision_hash") == sample_revision_hash
                ),
                None,
            )
            if selected is None:
                raise ConflictError("stale_revision")
            selected["status"] = "approved"
            value["current_sample_revision_hash"] = sample_revision_hash
            value["full_deck"] = root.model_dump(mode="json")
            value["full_deck_revisions"] = [revision.model_dump(mode="json")]
            value.update(state="ppt_full", phase="ready_to_generate")
            return value

        ready_count = sum(page.status == "ready" for page in revision.plan.pages)
        try:
            return self.store.update_events(
                apply,
                [
                    (
                        "sample_approved",
                        {
                            "revision_hash": sample_revision_hash,
                            "already_approved": was_approved,
                        },
                    ),
                    (
                        "full_deck_initialized",
                        {
                            "full_deck_id": root.full_deck_id,
                            "revision_hash": revision.revision_hash,
                            "page_count": len(revision.plan.pages),
                            "ready_page_count": ready_count,
                            "pending_page_count": len(revision.plan.pages) - ready_count,
                        },
                    ),
                ],
                expected_checkpoint_id=checkpoint_id,
            )
        except ConflictError as exc:
            if str(exc) == "stale_revision":
                raise ConflictError("stale_revision:工程已更新，请刷新后重试。") from exc
            raise

    def generate_full_deck(
        self,
        checkpoint_id: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return run_full_deck_generation(
            self,
            checkpoint_id,
            cancel_requested=cancel_requested,
        )

    def revise_full_deck(
        self,
        checkpoint_id: str,
        revision_hash: str,
        feedback: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return create_full_deck_revision(
            self,
            checkpoint_id,
            revision_hash,
            operation="revise_full_deck",
            feedback=feedback,
            cancel_requested=cancel_requested,
        )

    def regenerate_full_deck(
        self,
        checkpoint_id: str,
        revision_hash: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return create_full_deck_revision(
            self,
            checkpoint_id,
            revision_hash,
            operation="regenerate_full_deck",
            cancel_requested=cancel_requested,
        )

    def restore_sample(self, checkpoint_id: str, revision_hash: str) -> dict[str, Any]:
        """Move the current sample pointer without creating a new revision."""

        manifest = self.store.read(include_sample_html=False)
        self._require(manifest, "revise_sample", checkpoint_id)
        if revision_hash not in {
            item.get("revision_hash") for item in manifest.get("samples", [])
        }:
            raise FileNotFoundError(revision_hash)
        return self.store.select_sample_revision(checkpoint_id, revision_hash)

    def restore_full_deck(
        self,
        checkpoint_id: str,
        revision_hash: str,
    ) -> dict[str, Any]:
        """Move the current full-deck pointer without creating a revision."""

        manifest = self.store.read(include_sample_html=False)
        self._require(manifest, "restore_full_deck_revision", checkpoint_id)
        root = manifest.get("full_deck") or {}
        if revision_hash not in {
            reference.get("revision_hash")
            for reference in root.get("revision_refs", [])
        }:
            raise FileNotFoundError(revision_hash)
        return self.store.select_full_deck_revision(checkpoint_id, revision_hash)

    def approve_sample(self, checkpoint_id: str, revision_hash: str) -> dict[str, Any]:
        manifest = self.store.read()
        self._require(manifest, "approve_sample", checkpoint_id)
        current_hash = manifest.get("current_sample_revision_hash")
        current = next(
            (item for item in manifest.get("samples", []) if item.get("revision_hash") == current_hash),
            (manifest.get("samples") or [None])[-1],
        )
        if not current or current["revision_hash"] != revision_hash:
            raise ConflictError("stale_revision")

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            for item in value["samples"]:
                if item["revision_hash"] == revision_hash:
                    item["status"] = "approved"
                    break
            value["current_sample_revision_hash"] = revision_hash
            value.update(state="ppt_sample", phase="completed")
            return value

        return self.store.update(
            apply,
            "sample_approved",
            {"revision_hash": revision_hash},
            expected_checkpoint_id=checkpoint_id,
        )

    @staticmethod
    def _current(manifest: dict[str, Any], document_type: DocumentType) -> dict[str, Any] | None:
        history = manifest["documents"].get(document_type, [])
        return history[-1] if history else None
