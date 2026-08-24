from pathlib import Path
from shutil import copy2

import pytest
import yaml

import configs.runtime as runtime_module
from configs.runtime import ManagedRuntime, ModelBinding, RuntimeConfigUpdate, RuntimePolicy


def test_default_runtime_is_valid() -> None:
    runtime = ManagedRuntime(Path(__file__).parents[1])

    assert runtime.policy.stream_model_output is False
    assert len(runtime.models.state_bindings) == 3
    assert "api_key_env" not in runtime.public_context()["model_bindings"][0]


def test_runtime_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        RuntimePolicy.model_validate({"unexpected": True})


def test_runtime_rejects_inverted_read_budget() -> None:
    with pytest.raises(ValueError):
        RuntimePolicy(max_read_chars_per_call=200, max_read_chars_per_job=100)


def test_model_binding_rejects_credentials_in_parameters() -> None:
    with pytest.raises(ValueError, match="credential fields"):
        ModelBinding(
            state="intake_clarify",
            provider="openai",
            model="gpt-test",
            parameters={"headers": {"authorization": "secret"}},
        )


def editable_runtime(tmp_path: Path) -> ManagedRuntime:
    root = Path(__file__).parents[1]
    copy2(root / "runtime.yaml", tmp_path / "runtime.yaml")
    copy2(root / "model_config.yaml", tmp_path / "model_config.yaml")
    (tmp_path / "skills").mkdir()
    return ManagedRuntime(tmp_path)


def update_payload(runtime: ManagedRuntime) -> RuntimeConfigUpdate:
    context = runtime.public_context()
    bindings = context["model_bindings"]
    bindings[1] = {**bindings[1], "model": "narrative-preview-v2", "parameters": {"temperature": 0.2}}
    return RuntimeConfigUpdate.model_validate({
        "model_config_id": "ppt-agent-test-editable",
        "model_bindings": bindings,
        "policy": {**context["policy"], "max_auto_questions": 4},
    })


def test_runtime_update_persists_and_reloads_safe_fields(tmp_path: Path) -> None:
    runtime = editable_runtime(tmp_path)

    updated = runtime.apply_update(update_payload(runtime))

    assert updated.models.binding_for("narrative_structure").model == "narrative-preview-v2"
    assert updated.models.binding_for("narrative_structure").parameters == {"temperature": 0.2}
    assert updated.models.binding_for("narrative_structure").api_key_env == "OPENAI_API_KEY"
    assert updated.policy.max_auto_questions == 4
    assert updated.model_hash != runtime.model_hash
    assert yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))["max_auto_questions"] == 4


def test_runtime_update_rolls_back_both_files_on_write_failure(tmp_path: Path, monkeypatch) -> None:
    runtime = editable_runtime(tmp_path)
    previous_runtime = runtime.policy_path.read_bytes()
    previous_models = runtime.model_config_path.read_bytes()
    original_atomic = runtime_module._atomic_bytes
    failed = False

    def fail_model_write_once(path: Path, content: bytes) -> None:
        nonlocal failed
        if path == runtime.model_config_path and not failed and b"narrative-preview-v2" in content:
            failed = True
            raise OSError("simulated model config write failure")
        original_atomic(path, content)

    monkeypatch.setattr(runtime_module, "_atomic_bytes", fail_model_write_once)

    with pytest.raises(OSError, match="simulated"):
        runtime.apply_update(update_payload(runtime))

    assert runtime.policy_path.read_bytes() == previous_runtime
    assert runtime.model_config_path.read_bytes() == previous_models
