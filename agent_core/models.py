from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.sample_html import SampleHtmlError, sanitize_sample_html
from runtime.package_tool import normalize_package_path, package_media_type


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
    round: int = Field(default=1, ge=1, le=20)
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


class PackageSlide(StrictModel):
    slide_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=160)
    # Legacy packages do not carry this mapping. New sample generations make
    # it mandatory at the workflow boundary so a future full-deck page list
    # can reuse the immutable sample slide without guessing its outline page.
    source_slide_number: int | None = Field(default=None, ge=1, le=1000)


class PackageFile(StrictModel):
    path: str = Field(min_length=1, max_length=240)
    # Base64 expands binary resources by roughly one third. Keep this string
    # ceiling above the configurable 8 MB raw-file maximum.
    content: str = Field(min_length=1, max_length=12_000_000)
    encoding: Literal["utf-8", "base64"] = "utf-8"
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=160)
    origin: str = Field(default="model_output", min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_package_path(value)

    @model_validator(mode="after")
    def normalize_media_type(self) -> "PackageFile":
        # The model controls file content, not response interpretation.
        self.media_type = package_media_type(self.path)
        return self

    def content_bytes(self) -> bytes:
        if self.encoding == "utf-8":
            return self.content.encode("utf-8")
        try:
            return base64.b64decode(self.content, validate=True)
        except ValueError as exc:
            raise ValueError("package file contains invalid base64") from exc


class HtmlPptPackage(StrictModel):
    entrypoint: Literal["index.html"] = "index.html"
    title: str = Field(min_length=1, max_length=160)
    slide_count: int = Field(ge=1, le=80)
    slides: list[PackageSlide] = Field(min_length=1, max_length=80)
    files: list[PackageFile] = Field(min_length=1, max_length=512)
    package_hash: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_package(self) -> "HtmlPptPackage":
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("package file paths must be unique")
        if self.entrypoint not in paths:
            raise ValueError("package must contain index.html")
        if self.slide_count != len(self.slides):
            raise ValueError("slide_count must match slides")
        if len({item.slide_id for item in self.slides}) != len(self.slides):
            raise ValueError("slide_id values must be unique")
        # Workflow-specific DraftPackage limits keep samples at 96 files / 15 MB.
        # The immutable package model also carries composed full decks, whose
        # explicit runtime ceiling is intentionally higher.
        if sum(len(item.content_bytes()) for item in self.files) > 64_000_000:
            raise ValueError("package exceeds the total size limit")
        expected = self.content_hash()
        if self.package_hash is not None and self.package_hash != expected:
            raise ValueError("package hash does not match files")
        self.package_hash = expected
        return self

    def content_hash(self) -> str:
        hasher = sha256()
        for item in sorted(self.files, key=lambda candidate: candidate.path):
            content = item.content_bytes()
            hasher.update(item.path.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(str(len(content)).encode("ascii"))
            hasher.update(b"\0")
            hasher.update(content)
        return "sha256:" + hasher.hexdigest()


class FullDeckOutlineRef(StrictModel):
    outline_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_slide_number: int = Field(ge=1, le=1000)


class FullDeckContentRef(StrictModel):
    artifact_type: Literal["html_ppt_slide"] = "html_ppt_slide"
    revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    package_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    slide_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    slide_content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class FullDeckDerivedFrom(StrictModel):
    sample_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sample_slide_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class FullDeckPageSlot(StrictModel):
    slot_id: str = Field(min_length=1, max_length=80, pattern=r"^slot_[a-f0-9]{24}$")
    position: int = Field(ge=0, le=999)
    outline_ref: FullDeckOutlineRef | None = None
    title: str = Field(min_length=1, max_length=160)
    status: Literal["pending", "ready"]
    source_type: Literal[
        "approved_sample", "generated_segment", "full_deck_edit", "pending"
    ]
    content_ref: FullDeckContentRef | None = None
    derived_from: FullDeckDerivedFrom | None = None

    @model_validator(mode="after")
    def validate_content_state(self) -> "FullDeckPageSlot":
        if self.status == "pending":
            if self.source_type != "pending" or self.content_ref is not None:
                raise ValueError("pending full-deck pages cannot carry content")
        elif self.source_type == "pending" or self.content_ref is None:
            raise ValueError("ready full-deck pages require an immutable content reference")
        if self.source_type == "approved_sample" and self.derived_from is None:
            raise ValueError("approved sample pages require sample provenance")
        return self


class FullDeckPlan(StrictModel):
    pages: list[FullDeckPageSlot] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_order(self) -> "FullDeckPlan":
        positions = [page.position for page in self.pages]
        if positions != list(range(len(self.pages))):
            raise ValueError("full-deck page positions must be ordered, unique, and contiguous")
        slot_ids = [page.slot_id for page in self.pages]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("full-deck slot_id values must be unique")
        outline_numbers = [
            page.outline_ref.source_slide_number
            for page in self.pages
            if page.outline_ref is not None
        ]
        if len(outline_numbers) != len(set(outline_numbers)):
            raise ValueError("full-deck outline slide numbers must be unique")
        return self


class FullDeckPackage(HtmlPptPackage):
    composition_manifest: dict[str, Any]


FullDeckGenerationSessionStatus = Literal[
    "queued",
    "running",
    "pause_requested",
    "paused",
    "failed",
    "finalizing",
    "completed",
    "cancelled",
    "stale",
]
FullDeckGenerationBatchStatus = Literal["pending", "running", "succeeded", "failed"]
FullDeckGenerationPageStatus = Literal[
    "sample_ready", "queued", "generating", "ready", "failed"
]
FULL_DECK_GENERATION_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "stale"}),
    "running": frozenset(
        {"pause_requested", "failed", "finalizing", "cancelled", "stale"}
    ),
    "pause_requested": frozenset({"paused", "failed", "cancelled", "stale"}),
    "paused": frozenset({"running", "cancelled", "stale"}),
    "failed": frozenset({"running", "finalizing", "cancelled", "stale"}),
    "finalizing": frozenset({"completed", "failed", "stale"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "stale": frozenset(),
}
FULL_DECK_GENERATION_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed"}),
    "failed": frozenset({"running"}),
    "succeeded": frozenset(),
}


class FullDeckGenerationContentRef(StrictModel):
    """Immutable slide source used by a generation-session page projection."""

    artifact_type: Literal["html_ppt_slide"] = "html_ppt_slide"
    revision_hash: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    package_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    package_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    slide_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    slide_content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_source_identity(self) -> "FullDeckGenerationContentRef":
        if (self.revision_hash is None) == (self.package_id is None):
            raise ValueError(
                "generation content references require exactly one revision or package id"
            )
        return self


class FullDeckGenerationSession(StrictModel):
    session_id: str = Field(
        default_factory=lambda: "fullsession_" + uuid4().hex,
        pattern=r"^fullsession_[a-f0-9]{32}$",
    )
    full_deck_id: str = Field(pattern=r"^deck_[a-f0-9]{24}$")
    branch: str = Field(min_length=1, max_length=120)
    base_checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{24}$")
    base_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    outline_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sample_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    status: FullDeckGenerationSessionStatus = "queued"
    planner_version: str = Field(min_length=1, max_length=80)
    total_batches: int = Field(ge=1, le=1000)
    completed_batches: int = Field(default=0, ge=0, le=1000)
    active_batch_index: int | None = Field(default=None, ge=1, le=1000)
    session_version: int = Field(default=1, ge=1)
    latest_preview_package_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    published_revision_hash: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    error: dict[str, Any] | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_progress(self) -> "FullDeckGenerationSession":
        if self.completed_batches > self.total_batches:
            raise ValueError("completed batches cannot exceed total batches")
        if (
            self.active_batch_index is not None
            and self.active_batch_index > self.total_batches
        ):
            raise ValueError("active batch index exceeds total batches")
        if self.published_revision_hash is not None and self.status != "completed":
            raise ValueError("only completed sessions can reference a published revision")
        if self.status == "completed" and self.published_revision_hash is None:
            raise ValueError("completed sessions require a published revision")
        if self.status == "completed" and self.completed_batches != self.total_batches:
            raise ValueError("completed sessions require every batch to be complete")
        if self.status in {
            "queued",
            "paused",
            "failed",
            "finalizing",
            "completed",
            "cancelled",
            "stale",
        } and self.active_batch_index is not None:
            raise ValueError(f"{self.status} sessions cannot carry an active batch")
        return self

    def can_transition_to(self, status: FullDeckGenerationSessionStatus) -> bool:
        return status == self.status or status in FULL_DECK_GENERATION_SESSION_TRANSITIONS[
            self.status
        ]


class FullDeckGenerationBatch(StrictModel):
    session_id: str = Field(pattern=r"^fullsession_[a-f0-9]{32}$")
    batch_index: int = Field(ge=1, le=1000)
    status: FullDeckGenerationBatchStatus = "pending"
    slot_ids: list[str] = Field(min_length=1, max_length=5)
    source_slide_numbers: list[int] = Field(min_length=1, max_length=5)
    attempt_count: int = Field(default=0, ge=0)
    segment_package_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    prompt_call_ids: list[str] = Field(default_factory=list, max_length=100)
    applied_directive_ids: list[str] = Field(default_factory=list, max_length=1000)
    error: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @field_validator("slot_ids")
    @classmethod
    def validate_slot_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("batch slot ids must be unique")
        if any(not re.fullmatch(r"slot_[a-f0-9]{24}", item) for item in value):
            raise ValueError("invalid full-deck slot id")
        return value

    @field_validator("prompt_call_ids")
    @classmethod
    def validate_prompt_call_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("batch prompt call ids must be unique")
        if any(not re.fullmatch(r"prompt_[a-f0-9]{32}", item) for item in value):
            raise ValueError("invalid prompt call id")
        return value

    @field_validator("applied_directive_ids")
    @classmethod
    def validate_directive_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("batch directive ids must be unique")
        if any(not re.fullmatch(r"directive_[a-f0-9]{32}", item) for item in value):
            raise ValueError("invalid generation directive id")
        return value

    @model_validator(mode="after")
    def validate_targets(self) -> "FullDeckGenerationBatch":
        if len(self.slot_ids) != len(self.source_slide_numbers):
            raise ValueError("batch slots and source slide numbers must align")
        if len(self.source_slide_numbers) != len(set(self.source_slide_numbers)):
            raise ValueError("batch source slide numbers must be unique")
        if any(not 1 <= item <= 1000 for item in self.source_slide_numbers):
            raise ValueError("batch source slide numbers must be between 1 and 1000")
        if self.source_slide_numbers != sorted(self.source_slide_numbers):
            raise ValueError("batch source slide numbers must be ordered")
        if any(
            right != left + 1
            for left, right in zip(
                self.source_slide_numbers,
                self.source_slide_numbers[1:],
            )
        ):
            raise ValueError("batch source slide numbers must be contiguous")
        if self.status == "succeeded" and self.segment_package_id is None:
            raise ValueError("succeeded batches require a segment package")
        return self

    def can_transition_to(self, status: FullDeckGenerationBatchStatus) -> bool:
        return status == self.status or status in FULL_DECK_GENERATION_BATCH_TRANSITIONS[
            self.status
        ]


class FullDeckGenerationPage(StrictModel):
    session_id: str = Field(pattern=r"^fullsession_[a-f0-9]{32}$")
    position: int = Field(ge=0, le=999)
    slot_id: str = Field(pattern=r"^slot_[a-f0-9]{24}$")
    source_slide_number: int | None = Field(default=None, ge=1, le=1000)
    title: str = Field(min_length=1, max_length=160)
    generation_status: FullDeckGenerationPageStatus
    batch_index: int | None = Field(default=None, ge=1, le=1000)
    source_type: Literal[
        "approved_sample", "generated_segment", "full_deck_edit", "pending"
    ]
    content_ref: FullDeckGenerationContentRef | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_generation_state(self) -> "FullDeckGenerationPage":
        if self.generation_status == "sample_ready":
            if self.batch_index is not None or self.source_type != "approved_sample":
                raise ValueError("sample-ready pages must come from an approved sample")
            if self.content_ref is None or self.content_ref.revision_hash is None:
                raise ValueError("sample-ready pages require a revision content reference")
        elif self.batch_index is None:
            raise ValueError("generated pages require a batch index")
        if self.generation_status == "ready":
            if self.source_type != "generated_segment":
                raise ValueError("ready generated pages must come from a generated segment")
            if self.content_ref is None or self.content_ref.package_id is None:
                raise ValueError("ready generated pages require a package content reference")
        elif self.generation_status != "sample_ready" and self.content_ref is not None:
            raise ValueError("unfinished generated pages cannot carry content")
        return self


class FullDeckGenerationDirective(StrictModel):
    directive_id: str = Field(
        default_factory=lambda: "directive_" + uuid4().hex,
        pattern=r"^directive_[a-f0-9]{32}$",
    )
    session_id: str = Field(pattern=r"^fullsession_[a-f0-9]{32}$")
    content: str = Field(min_length=1, max_length=4000)
    apply_from_batch_index: int = Field(ge=1, le=1000)
    created_at: str = Field(default_factory=utc_now)
    first_applied_at: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("generation directive cannot be blank")
        return normalized


class FullDeckGenerationPackage(FullDeckPackage):
    package_id: str = Field(
        default_factory=lambda: "fullgenpkg_" + uuid4().hex,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    session_id: str = Field(pattern=r"^fullsession_[a-f0-9]{32}$")
    batch_index: int = Field(ge=1, le=1000)
    kind: Literal["segment", "preview"]
    created_at: str = Field(default_factory=utc_now)


class FullDeckRevision(StrictModel):
    full_deck_id: str = Field(pattern=r"^deck_[a-f0-9]{24}$")
    revision: int = Field(ge=1)
    revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    parent_revision_hash: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    feedback: str | None = Field(default=None, max_length=4000)
    status: Literal["draft", "pending_approval", "approved", "stale"] = "draft"
    plan: FullDeckPlan
    package: FullDeckPackage | None = None
    created_at: str = Field(default_factory=utc_now)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_revision(self) -> "FullDeckRevision":
        if self.status in {"pending_approval", "approved"}:
            if self.package is None or any(page.status != "ready" for page in self.plan.pages):
                raise ValueError("publishable full-deck revisions require a complete package")
        identity = self.content_identity(
            full_deck_id=self.full_deck_id,
            revision=self.revision,
            parent=self.parent_revision_hash,
            feedback=self.feedback,
            plan=self.plan,
            package=self.package,
            provenance=self.provenance,
        )
        serialized = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if self.revision_hash != digest(f"ppt_full\n{serialized}"):
            raise ValueError("full-deck revision hash does not match its immutable content")
        return self

    @staticmethod
    def content_identity(
        *,
        full_deck_id: str,
        revision: int,
        parent: str | None,
        feedback: str | None,
        plan: FullDeckPlan,
        package: FullDeckPackage | None,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "full_deck_id": full_deck_id,
            "revision": revision,
            "parent_revision_hash": parent,
            "feedback": feedback,
            "pages": [page.model_dump(mode="json") for page in plan.pages],
            "package_hash": package.package_hash if package else None,
            "provenance": provenance,
        }

    @classmethod
    def create(
        cls,
        *,
        full_deck_id: str,
        revision: int,
        parent: str | None,
        feedback: str | None,
        plan: FullDeckPlan,
        package: FullDeckPackage | None = None,
        status: Literal["draft", "pending_approval", "approved", "stale"] = "draft",
        provenance: dict[str, Any] | None = None,
    ) -> "FullDeckRevision":
        provenance_value = provenance or {}
        identity = cls.content_identity(
            full_deck_id=full_deck_id,
            revision=revision,
            parent=parent,
            feedback=feedback,
            plan=plan,
            package=package,
            provenance=provenance_value,
        )
        serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            full_deck_id=full_deck_id,
            revision=revision,
            revision_hash=digest(f"ppt_full\n{serialized}"),
            parent_revision_hash=parent,
            feedback=feedback,
            status=status,
            plan=plan,
            package=package,
            provenance=provenance_value,
        )


class FullDeckRevisionRef(StrictModel):
    revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    status: Literal["draft", "pending_approval", "approved", "stale"]


class FullDeck(StrictModel):
    full_deck_id: str = Field(pattern=r"^deck_[a-f0-9]{24}$")
    approved_sample_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    outline_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    current_revision_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    revision_refs: list[FullDeckRevisionRef] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_current_revision(self) -> "FullDeck":
        hashes = [item.revision_hash for item in self.revision_refs]
        if len(hashes) != len(set(hashes)):
            raise ValueError("full-deck revision references must be unique")
        if self.current_revision_hash not in hashes:
            raise ValueError("current full-deck revision must be referenced")
        return self


class SampleRevision(StrictModel):
    sample_id: str = "sample_ppt"
    revision: int
    revision_hash: str
    parent_revision_hash: str | None = None
    pages: list[SamplePage] = Field(default_factory=list, max_length=6)
    package: HtmlPptPackage | None = None
    feedback: str | None = Field(default=None, max_length=4000)
    status: Literal["pending_approval", "approved", "stale"] = "pending_approval"
    created_at: str = Field(default_factory=utc_now)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_artifact(self) -> "SampleRevision":
        if not self.pages and self.package is None:
            raise ValueError("sample revision must contain pages or an HTML-PPT package")
        return self

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
            revision_hash=digest(f"ppt_sample\n{revision}\n{parent or 'root'}\n{serialized}"),
            parent_revision_hash=parent,
            pages=pages,
            feedback=feedback,
            provenance=provenance or {},
        )

    @classmethod
    def create_package(
        cls,
        package: HtmlPptPackage,
        *,
        revision: int,
        parent: str | None,
        feedback: str | None,
        provenance: dict[str, Any] | None = None,
    ) -> "SampleRevision":
        slide_mapping = json.dumps(
            [slide.model_dump() for slide in package.slides],
            ensure_ascii=False,
            sort_keys=True,
        )
        return cls(
            revision=revision,
            revision_hash=digest(
                f"ppt_package\n{revision}\n{parent or 'root'}\n"
                f"{package.package_hash or package.content_hash()}\n{slide_mapping}"
            ),
            parent_revision_hash=parent,
            package=package,
            feedback=feedback,
            provenance=provenance or {},
        )
