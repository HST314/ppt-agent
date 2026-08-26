import json
from types import SimpleNamespace

import pytest

import model_router.client as client_module
from model_router.client import MaxToolRoundsExceeded, ModelGateway


class FakeMessage:
    def __init__(self, *, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=True):
        value = {"role": "assistant"}
        if self.content is not None:
            value["content"] = self.content
        if self.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return value


def tool_message(call_id: str) -> FakeMessage:
    return FakeMessage(tool_calls=[SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="read",
            arguments=json.dumps({"path": "snippet.md"}),
        ),
    )])


class FakeOpenAI:
    def __init__(self, responses, requests):
        def create(**kwargs):
            requests.append(kwargs)
            message = responses.pop(0)
            return SimpleNamespace(
                id=f"response_{len(requests)}",
                choices=[SimpleNamespace(message=message)],
                usage=None,
            )

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )


def real_gateway(mock_runtime, monkeypatch, responses):
    bindings = [
        item.model_copy(update={
            "provider": "test",
            "api_key_env": "TEST_MODEL_KEY",
        }) if item.state == "ppt_sample" else item
        for item in mock_runtime.models.state_bindings
    ]
    mock_runtime.models = mock_runtime.models.model_copy(
        update={"state_bindings": bindings},
    )
    mock_runtime.policy = mock_runtime.policy.model_copy(
        update={"max_tool_rounds": 2},
    )
    (mock_runtime.skills_root / "snippet.md").write_text("skill text", encoding="utf-8")
    monkeypatch.setenv("TEST_MODEL_KEY", "test-key")
    requests = []
    monkeypatch.setattr(
        client_module,
        "OpenAI",
        lambda **_kwargs: FakeOpenAI(responses, requests),
    )
    return ModelGateway(mock_runtime), requests


def test_gateway_counts_exact_tool_rounds_then_requests_one_tool_free_final(
    mock_runtime,
    monkeypatch,
) -> None:
    gateway, requests = real_gateway(
        mock_runtime,
        monkeypatch,
        [tool_message("call_1"), tool_message("call_2"), FakeMessage(content="{}")],
    )
    progress = []
    gateway.progress_callback = (
        lambda status, details, traces, messages: progress.append(
            (status, details, traces, messages)
        )
    )

    output, traces = gateway.generate("ppt_sample", "generate", json_mode=True)

    assert output == "{}"
    assert [request["tool_choice"] for request in requests] == ["auto", "auto", "none"]
    assert [item[1]["round"] for item in progress] == [1, 2]
    assert progress[-1][1]["skill_read_count"] == 2
    assert sum(item["type"] == "tool_call" for item in traces) == 2
    assert traces[-1]["tools_disabled"] is True


def test_gateway_limit_failure_retains_messages_and_traces_for_resume(
    mock_runtime,
    monkeypatch,
) -> None:
    gateway, requests = real_gateway(
        mock_runtime,
        monkeypatch,
        [tool_message("call_1"), tool_message("call_2"), tool_message("unexpected")],
    )

    with pytest.raises(MaxToolRoundsExceeded) as failure:
        gateway.generate("ppt_sample", "generate", json_mode=True)

    assert failure.value.public_code == "max_tool_rounds_exceeded"
    assert [request["tool_choice"] for request in requests] == ["auto", "auto", "none"]
    assert sum(item["type"] == "tool_call" for item in gateway.last_traces) == 2
    assert sum(message["role"] == "tool" for message in gateway.last_messages) == 2
