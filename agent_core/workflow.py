from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from agent_core.models import DocumentRevision, Question, QuestionCard, TaskCard
from configs.runtime import ManagedRuntime
from model_router.client import ModelGateway
from runtime.read_tool import SkillReader
from storage.project_store import ConflictError, ProjectStore


DocumentType = Literal["narrative_structure", "slide_outline"]


class WorkflowError(RuntimeError):
    pass


def capabilities(manifest: dict[str, Any]) -> list[str]:
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
    if manifest.get("documents", {}).get("narrative_structure") and "edit_narrative" not in caps:
        caps.append("edit_narrative")
    if manifest.get("documents", {}).get("slide_outline") and "edit_outline" not in caps:
        caps.append("edit_outline")
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
        template, template_hash = self._template("clarify_questions.md")
        prompt = (
            template + "\n\nGenerate concise clarification questions for this presentation task. Return JSON with a questions array. "
            "Each item must contain field, prompt, impact, options[{value,label,recommended}], allow_free_text. "
            f"Ask no more than {self.runtime.policy.max_auto_questions}. Task:\n"
            + json.dumps(manifest["task_card"], ensure_ascii=False)
            + "\nAvailable skill index:\n"
            + json.dumps(reader.index(), ensure_ascii=False)
        )
        text, traces = self.gateway.generate("intake_clarify", prompt, json_mode=True)
        payload = self.gateway.parse_json(text)
        raw_questions = payload.get("questions", [])[: self.runtime.policy.max_auto_questions]
        questions = [Question.model_validate(item) for item in raw_questions]
        if not questions:
            raise WorkflowError("model returned no clarification questions")
        card_id = "questions_" + uuid4().hex[:16]

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            card = QuestionCard(question_card_id=card_id, checkpoint_id=value["checkpoint_id"], questions=questions)
            value.update(state="intake_clarify", phase="waiting_clarification", question_card=card.model_dump())
            value["last_tool_traces"] = traces
            value["last_template"] = {"template_id": "clarify_questions", "template_version": 1, "template_hash": template_hash}
            return value

        return self.store.update(apply, "clarification_generated", {"question_card_id": card_id})

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

        return self.store.update(apply, "clarification_answered", {"question_card_id": question_card_id})

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
        markdown, traces = self.gateway.generate(state, prompt)
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
                "task_revision_hash": task_hash,
                "upstream_revision_hash": upstream["revision_hash"] if upstream else None,
                "model_config_hash": self.runtime.model_hash,
                "runtime_config_hash": self.runtime.runtime_hash,
                "template_id": template_name.removesuffix(".md"),
                "template_version": 1,
                "template_hash": template_hash,
                "traces": traces,
            },
        )

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["documents"][document_type].append(document.model_dump())
            value.update(state=document_type, phase="waiting_human_approval")
            return value

        return self.store.update(apply, "document_generated", {"document_type": document_type, "revision_hash": document.revision_hash})

    def edit_document(self, document_type: DocumentType, checkpoint_id: str, markdown: str) -> dict[str, Any]:
        if not markdown.strip():
            raise ValueError("markdown must not be empty")
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
            provenance=current.get("provenance", {}),
        )

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["documents"][document_type].append(document.model_dump())
            if document_type == "narrative_structure":
                for prior in value["documents"]["slide_outline"]:
                    prior["status"] = "stale"
            value.update(state=document_type, phase="waiting_human_approval")
            return value

        return self.store.update(apply, "document_revised", {"document_type": document_type, "revision_hash": document.revision_hash})

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
                value.update(state="slide_outline", phase="completed")
            return value

        return self.store.update(apply, "document_approved", {"document_type": document_type, "revision_hash": revision_hash})

    @staticmethod
    def _current(manifest: dict[str, Any], document_type: DocumentType) -> dict[str, Any] | None:
        history = manifest["documents"].get(document_type, [])
        return history[-1] if history else None
