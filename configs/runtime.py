from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_auto_questions: int = Field(default=3, ge=0, le=8)
    stream_model_output: Literal[False] = False
    clarification_total_budget: int = Field(default=10, ge=0, le=30)
    question_preference: Literal["proactive", "minimal", "none"] = "proactive"
    model_timeout_seconds: float = Field(default=180.0, ge=1, le=600)
    max_tool_rounds: int = Field(default=8, ge=0, le=20)
    max_read_chars_per_call: int = Field(default=20_000, ge=100, le=100_000)
    max_read_chars_per_job: int = Field(default=80_000, ge=100, le=500_000)
    skills_root: str = "skills"
    offline_mode: bool = False
    source_config_revision: str | None = None
    config_hash: str | None = None
    generated_at: str | None = None

    @model_validator(mode="after")
    def validate_read_budget(self) -> "RuntimePolicy":
        if self.max_read_chars_per_call > self.max_read_chars_per_job:
            raise ValueError("max_read_chars_per_call cannot exceed the per-job budget")
        return self


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["intake_clarify", "narrative_structure", "slide_outline"]
    model_role: Literal["reasoning_llm"] = "reasoning_llm"
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback_model: str | None = None
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_config_id: str = Field(min_length=1)
    state_bindings: list[ModelBinding]

    def binding_for(self, state: str) -> ModelBinding:
        matches = [item for item in self.state_bindings if item.state == state]
        if len(matches) != 1:
            raise ValueError(f"state {state!r} must have exactly one model binding")
        return matches[0]


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"configuration must be a mapping: {path}")
    return payload


class ManagedRuntime:
    def __init__(self, app_root: Path):
        runtime_path = Path(os.getenv("PPT_AGENT_RUNTIME_POLICY", app_root / "runtime.yaml")).resolve()
        model_path = Path(os.getenv("PPT_AGENT_MODEL_CONFIG", app_root / "model_config.yaml")).resolve()
        self.policy_path = runtime_path
        self.model_config_path = model_path
        self.policy = RuntimePolicy.model_validate(_read_yaml(runtime_path))
        self.models = ModelConfig.model_validate(_read_yaml(model_path))
        skills = Path(self.policy.skills_root)
        self.skills_root = (skills if skills.is_absolute() else app_root / skills).resolve()
        if not self.skills_root.is_dir():
            raise ValueError("skills_root does not exist")
        self.runtime_hash = self._hash(runtime_path)
        self.model_hash = self._hash(model_path)

    @staticmethod
    def _hash(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def public_context(self) -> dict[str, Any]:
        bindings = []
        for item in self.models.state_bindings:
            safe = item.model_dump(exclude={"api_key_env"})
            safe.pop("parameters", None)
            bindings.append(safe)
        return {
            "model_config_id": self.models.model_config_id,
            "model_bindings": bindings,
            "policy": self.policy.model_dump(exclude={"skills_root"}),
            "runtime_hash": self.runtime_hash,
            "model_hash": self.model_hash,
            "read_permission": "skills/**/*.md (read-only)",
        }

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.public_context(), ensure_ascii=False))
