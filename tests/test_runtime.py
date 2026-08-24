from pathlib import Path

import pytest

from configs.runtime import ManagedRuntime, RuntimePolicy


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
