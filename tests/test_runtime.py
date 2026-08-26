from pathlib import Path
from shutil import copy2

import pytest
import yaml

import configs.runtime as runtime_module
from configs.runtime import ManagedRuntime, ModelBinding, RuntimeConfigUpdate, RuntimePolicy


def test_default_runtime_is_valid() -> None:
    runtime = ManagedRuntime(Path(__file__).parents[1])

    assert runtime.policy.stream_model_output is False
    assert len(runtime.models.state_bindings) == 5
    assert runtime.policy.sample_page_count == 2
    assert runtime.policy.max_clarification_rounds == 3
    assert runtime.policy.max_tool_rounds == 20
    assert {binding.provider for binding in runtime.models.state_bindings} == {"ark"}
    assert {binding.model for binding in runtime.models.state_bindings} == {"deepseek-v4-flash-ga-260731"}
    assert {binding.api_key_env for binding in runtime.models.state_bindings} == {"ARK_API_KEY"}
    assert {binding.base_url for binding in runtime.models.state_bindings} == {
        "https://ark.cn-beijing.volces.com/api/v3"
    }
    assert runtime.models.binding_for("ppt_sample").parameters == {"max_tokens": 16_384}
    assert runtime.models.binding_for("ppt_full").parameters == {"max_tokens": 32_768}
    assert runtime.policy.full_deck_max_files == 384
    assert runtime.policy.full_deck_max_file_bytes == 10_485_760
    assert runtime.policy.full_deck_max_total_bytes == 209_715_200
    assert runtime.policy.full_deck_batched_generation_enabled is True
    assert runtime.policy.full_deck_reference_max_read_chars_per_call == 20_000
    assert runtime.policy.full_deck_reference_max_read_chars_per_batch == 80_000
    assert runtime.public_context()["features"] == {
        "full_deck_batched_generation_enabled": True,
    }
    assert runtime.public_context()["full_deck_limits"] == {
        "full_deck_reference_max_read_chars_per_call": 20_000,
        "full_deck_reference_max_read_chars_per_batch": 80_000,
        "full_deck_max_files": 384,
        "full_deck_max_file_bytes": 10_485_760,
        "full_deck_max_total_bytes": 209_715_200,
    }
    assert "api_key_env" not in runtime.public_context()["model_bindings"][0]


def test_runtime_rejects_unknown_fields() -> None:
    assert RuntimePolicy().full_deck_batched_generation_enabled is False
    with pytest.raises(ValueError):
        RuntimePolicy.model_validate({"unexpected": True})


def test_runtime_rejects_inverted_read_budget() -> None:
    with pytest.raises(ValueError):
        RuntimePolicy(max_read_chars_per_call=200, max_read_chars_per_job=100)

    with pytest.raises(ValueError, match="per-batch budget"):
        RuntimePolicy(
            full_deck_reference_max_read_chars_per_call=200,
            full_deck_reference_max_read_chars_per_batch=100,
        )


def test_runtime_allows_tool_round_limit_up_to_one_hundred() -> None:
    assert RuntimePolicy(max_tool_rounds=100).max_tool_rounds == 100
    with pytest.raises(ValueError):
        RuntimePolicy(max_tool_rounds=101)


def test_full_deck_package_limits_accept_new_bounds_and_reject_above() -> None:
    # 10MiB per file / 200MiB total are the deployed values; the schema keeps
    # headroom up to 16MiB / 256MiB and rejects anything beyond.
    assert RuntimePolicy(
        full_deck_max_file_bytes=10_485_760,
        full_deck_max_total_bytes=209_715_200,
    ).full_deck_max_file_bytes == 10_485_760

    assert RuntimePolicy(
        full_deck_max_file_bytes=16_777_216,
        full_deck_max_total_bytes=268_435_456,
    ).full_deck_max_total_bytes == 268_435_456

    with pytest.raises(ValueError):
        RuntimePolicy(full_deck_max_file_bytes=16_777_217)
    with pytest.raises(ValueError):
        RuntimePolicy(full_deck_max_total_bytes=268_435_457)


def test_model_binding_rejects_non_whitelisted_parameters() -> None:
    with pytest.raises(ValueError, match="unsupported model parameter fields"):
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
    assert updated.models.binding_for("narrative_structure").api_key_env == "ARK_API_KEY"
    assert updated.policy.max_auto_questions == 4
    assert updated.policy.full_deck_batched_generation_enabled is True
    assert updated.policy.full_deck_reference_max_read_chars_per_batch == 80_000
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
