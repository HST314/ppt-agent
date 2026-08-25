from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.sample_html import SampleHtmlError, sanitize_sample_html


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCard(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4000)
    audience: str = Field(default="", max_length=500)
    occasion: str = Field(default="", max_length=500)
    language: str = Field(default="zh-CN", max_length=32)
    target_slide_count: str = Field(default="", max_length=50)
    duration_minutes: int | None = Field(default=None, ge=1, le=600)
    known_facts: list[str] = Field(default_factory=list, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    forbidden_items: list[str] = Field(default_factory=list, max_length=100)
    source_refs: list[str] = Field(default_factory=list, max_length=100)


class QuestionOption(StrictModel):
    value: str
    label: str
    recommended: bool = False


class Question(StrictModel):
    question_id: str = Field(default_factory=lambda: "q_" + uuid4().hex[:12])
    field: str
    prompt: str
    impact: str = ""
    options: list[QuestionOption] = Field(default_factory=list)
    allow_free_text: bool = True


class QuestionCard(StrictModel):
    question_card_id: str = Field(default_factory=lambda: "questions_" + uuid4().hex[:16])
    checkpoint_id: str
    questions: list[Question]
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class DocumentRevision(StrictModel):
    document_id: str
    document_type: Literal["narrative_structure", "slide_outline"]
    revision: int
    revision_hash: str
    parent_revision_hash: str | None = None
    markdown_body: str
    status: Literal["pending_approval", "approved", "stale"] = "pending_approval"
    created_by: Literal["agent", "human"]
    created_at: str = Field(default_factory=utc_now)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        document_type: Literal["narrative_structure", "slide_outline"],
        markdown: str,
        *,
        revision: int,
        parent: str | None,
        created_by: Literal["agent", "human"],
        provenance: dict[str, Any] | None = None,
    ) -> "DocumentRevision":
        return cls(
            document_id=f"doc_{document_type}",
            document_type=document_type,
            revision=revision,
            revision_hash=digest(f"{document_type}\n{revision}\n{markdown}"),
            parent_revision_hash=parent,
            markdown_body=markdown,
            created_by=created_by,
            provenance=provenance or {},
        )


class SamplePage(StrictModel):
    page_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=160)
    html: str = Field(min_length=1, max_length=150_000)

    @field_validator("html")
    @classmethod
    def validate_isolated_html(cls, value: str) -> str:
        """Keep model HTML passive before it reaches a sandboxed srcdoc frame."""
        try:
            return sanitize_sample_html(value)
        except SampleHtmlError as exc:
            raise ValueError("sample HTML contains active or external content") from exc


class SampleOutput(StrictModel):
    pages: list[SamplePage] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_total_size(self) -> "SampleOutput":
        if sum(len(page.html) for page in self.pages) > 500_000:
            raise ValueError("sample HTML exceeds the total size limit")
        return self


class SampleRevision(StrictModel):
    sample_id: str = "sample_ppt"
    revision: int
    revision_hash: str
    parent_revision_hash: str | None = None
    pages: list[SamplePage] = Field(min_length=1, max_length=6)
    feedback: str | None = Field(default=None, max_length=4000)
    status: Literal["pending_approval", "approved", "stale"] = "pending_approval"
    created_at: str = Field(default_factory=utc_now)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        pages: list[SamplePage],
        *,
        revision: int,
        parent: str | None,
        feedback: str | None,
        provenance: dict[str, Any] | None = None,
    ) -> "SampleRevision":
        serialized = json.dumps([page.model_dump() for page in pages], ensure_ascii=False, sort_keys=True)
        return cls(
            revision=revision,
            revision_hash=digest(f"ppt_sample\n{revision}\n{serialized}"),
            parent_revision_hash=parent,
            pages=pages,
            feedback=feedback,
            provenance=provenance or {},
        )
