from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SENSITIVE_PARAMETER_KEYS = {"api_key", "apikey", "authorization", "access_token", "secret", "password", "cookie"}


def _safe_parameters(value: dict[str, Any]) -> dict[str, Any]:
    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lower() in SENSITIVE_PARAMETER_KEYS:
                    raise ValueError("model parameters cannot contain credential fields")
                check(nested)
        elif isinstance(item, list):
            for nested in item:
                check(nested)

    check(value)
    return value


def _http_url_or_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    return value


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

    _validate_parameters = field_validator("parameters")(_safe_parameters)
    _validate_base_url = field_validator("base_url")(_http_url_or_none)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_config_id: str = Field(min_length=1)
    state_bindings: list[ModelBinding]

    @model_validator(mode="after")
    def validate_state_bindings(self) -> "ModelConfig":
        expected = {"intake_clarify", "narrative_structure", "slide_outline"}
        actual = [item.state for item in self.state_bindings]
        if len(actual) != len(set(actual)):
            raise ValueError("state bindings must be unique")
        if set(actual) != expected:
            raise ValueError("model config must bind intake_clarify, narrative_structure and slide_outline")
        return self

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


class EditableModelBinding(BaseModel):
    """Safe model settings exposed to the browser; credential values stay in env."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["intake_clarify", "narrative_structure", "slide_outline"]
    model_role: Literal["reasoning_llm"] = "reasoning_llm"
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback_model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=1000)

    _validate_parameters = field_validator("parameters")(_safe_parameters)
    _validate_base_url = field_validator("base_url")(_http_url_or_none)


class EditableRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_auto_questions: int = Field(ge=0, le=8)
    clarification_total_budget: int = Field(ge=0, le=30)
    question_preference: Literal["proactive", "minimal", "none"]
    model_timeout_seconds: float = Field(ge=1, le=600)
    max_tool_rounds: int = Field(ge=0, le=20)
    max_read_chars_per_call: int = Field(ge=100, le=100_000)
    max_read_chars_per_job: int = Field(ge=100, le=500_000)

    @model_validator(mode="after")
    def validate_read_budget(self) -> "EditableRuntimePolicy":
        if self.max_read_chars_per_call > self.max_read_chars_per_job:
            raise ValueError("max_read_chars_per_call cannot exceed the per-job budget")
        return self


class RuntimeConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_config_id: str = Field(min_length=1, max_length=160)
    model_bindings: list[EditableModelBinding]
    policy: EditableRuntimePolicy

    @model_validator(mode="after")
    def validate_state_bindings(self) -> "RuntimeConfigUpdate":
        expected = {"intake_clarify", "narrative_structure", "slide_outline"}
        actual = [item.state for item in self.model_bindings]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("settings must include one binding for every phase-one state")
        return self


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _yaml_bytes(payload: dict[str, Any]) -> bytes:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")


class ManagedRuntime:
    def __init__(self, app_root: Path):
        self.app_root = app_root.resolve()
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
            bindings.append(safe)
        return {
            "model_config_id": self.models.model_config_id,
            "model_bindings": bindings,
            "policy": {
                name: getattr(self.policy, name)
                for name in EditableRuntimePolicy.model_fields
            },
            "runtime_hash": self.runtime_hash,
            "model_hash": self.model_hash,
            "read_permission": "skills/**/*.md (read-only)",
            "editable": True,
        }

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.public_context(), ensure_ascii=False))

    def apply_update(self, update: RuntimeConfigUpdate) -> "ManagedRuntime":
        """Validate, atomically persist and reload browser-editable runtime settings."""

        current_bindings = {item.state: item for item in self.models.state_bindings}
        model_config = ModelConfig(
            model_config_id=update.model_config_id,
            state_bindings=[
                ModelBinding(
                    **item.model_dump(),
                    api_key_env=current_bindings[item.state].api_key_env,
                )
                for item in update.model_bindings
            ],
        )
        policy = self.policy.model_copy(update=update.policy.model_dump())
        # Revalidate the merged policy so cross-field constraints are applied.
        policy = RuntimePolicy.model_validate(policy.model_dump())

        previous_runtime = self.policy_path.read_bytes()
        previous_models = self.model_config_path.read_bytes()
        try:
            _atomic_bytes(self.policy_path, _yaml_bytes(policy.model_dump(mode="json")))
            _atomic_bytes(self.model_config_path, _yaml_bytes(model_config.model_dump(mode="json")))
            return ManagedRuntime(self.app_root)
        except Exception:
            _atomic_bytes(self.policy_path, previous_runtime)
            _atomic_bytes(self.model_config_path, previous_models)
            raise
