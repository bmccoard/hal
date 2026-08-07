from pathlib import Path

import pytest

from hal.agent import (
    Agent,
    ContextWindowExceededError,
    Event,
    EventKind,
    MaxOutputTokensError,
    MaxTurnsError,
    UnexpectedStopReasonError,
)
from hal.cancellation import CancelledError, CancellationToken
from hal.models import ContentBlock, Response, ToolSpec
from hal.tools import Registry, Tool, default_registry


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request, cancellation=None):
        self.calls += 1
        if self.calls == 1:
            return Response([ContentBlock("tool_use", id="call-1", name="write_file", input={"path": str(Path(request.messages[0].content[0].text)), "content": "hello"})], "tool_use")
        return Response([ContentBlock("text", text="done")])


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: list[Response]) -> None:
        self.responses = list(responses)
        self.requests = []

    def complete(self, request, cancellation=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected provider call")
        return self.responses.pop(0)


class InterruptTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("interrupt", "Interrupt execution.", {"type": "object"})

    def run(self, arguments, cancellation=None):
        raise KeyboardInterrupt


class FailTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("fail", "Fail execution.", {"type": "object"})

    def run(self, arguments, cancellation=None):
        raise RuntimeError("tool failed")


class NoopTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("noop", "Do nothing.", {"type": "object"})

    def run(self, arguments, cancellation=None):
        return "ok"


def test_agent_executes_tool_and_keeps_matching_result(tmp_path: Path) -> None:
    provider = FakeProvider()
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))
    target = tmp_path / "created.txt"
    assert agent.send(str(target)) == "done"
    assert target.read_text(encoding="utf-8") == "hello"
    assert agent.messages[1].content[0].id == "call-1"
    assert agent.messages[2].content[0].tool_use_id == "call-1"


def test_tool_turn_is_committed_only_after_all_results_exist(tmp_path: Path) -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="interrupt", input={}),
    ], "tool_use")])
    agent = Agent(provider, "model", "system", Registry([InterruptTool()]))

    with pytest.raises(KeyboardInterrupt):
        agent.send("start")

    assert len(agent.messages) == 1
    assert agent.messages[0].role == "user"


def test_unknown_failed_and_denied_calls_get_ordered_error_results() -> None:
    provider = ScriptedProvider([
        Response([
            ContentBlock("tool_use", id="call-1", name="missing", input={}),
            ContentBlock("tool_use", id="call-2", name="fail", input={}),
            ContentBlock("tool_use", id="call-3", name="noop", input={}),
        ], "tool_use"),
        Response([ContentBlock("text", text="Recovered.")], "end_turn"),
    ])
    registry = Registry(
        [FailTool(), NoopTool()], approvals=["noop"], confirm=lambda _prompt: False,
    )
    agent = Agent(provider, "model", "system", registry)

    assert agent.send("start") == "Recovered."
    results = agent.messages[2].content
    assert [result.tool_use_id for result in results] == ["call-1", "call-2", "call-3"]
    assert all(result.is_error for result in results)
    assert "unknown tool" in results[0].content
    assert "tool failed" in results[1].content
    assert "denied" in results[2].content


def test_unknown_stop_reason_keeps_safe_text_and_does_not_run_tool(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.txt"
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Partial answer."),
        ContentBlock("tool_use", id="call-1", name="write_file", input={
            "path": str(target), "content": "bad",
        }),
    ], "future_reason")])
    events = []
    agent = Agent(
        provider, "model", "system", default_registry(tmp_path, tmp_path),
        on_event=events.append,
    )

    with pytest.raises(UnexpectedStopReasonError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "Partial answer."
    assert not target.exists()
    assert [block.type for block in agent.messages[-1].content] == ["text"]
    assert any(event.kind == EventKind.ERROR for event in events)


def test_refusal_ends_without_executing_announced_tools(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.txt"
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="I cannot do that."),
        ContentBlock("tool_use", id="call-1", name="write_file", input={
            "path": str(target), "content": "bad",
        }),
    ], "refusal")])
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))

    assert agent.send("start") == "I cannot do that."
    assert not target.exists()
    assert [block.type for block in agent.messages[-1].content] == ["text"]
    assert len(provider.requests) == 1


def test_pause_turn_replays_assistant_text_and_continues() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="Still working.")], "pause_turn"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    agent = Agent(provider, "model", "system", Registry([]))

    assert agent.send("start") == "Still working.\nDone."
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].content[0].text == "Still working."


def test_max_tokens_returns_typed_error_with_partial_text() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Truncated answer."),
    ], "max_tokens")])
    agent = Agent(provider, "model", "system", Registry([]))

    with pytest.raises(MaxOutputTokensError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "Truncated answer."
    assert len(provider.requests) == 1


def test_context_window_error_keeps_only_safe_partial_text(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.txt"
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Partial answer."),
        ContentBlock("tool_use", id="call-1", name="write_file", input={
            "path": str(target), "content": "bad",
        }),
    ], "model_context_window_exceeded")])
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))

    with pytest.raises(ContextWindowExceededError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "Partial answer."
    assert not target.exists()
    assert [block.type for block in agent.messages[-1].content] == ["text"]


def test_max_tokens_with_tool_calls_executes_tools_before_continuing(tmp_path: Path) -> None:
    target = tmp_path / "created.txt"
    provider = ScriptedProvider([
        Response([ContentBlock("tool_use", id="call-1", name="write_file", input={
            "path": str(target), "content": "created",
        })], "max_tokens"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))

    assert agent.send("start") == "Done."
    assert target.read_text(encoding="utf-8") == "created"
    assert agent.messages[2].content[0].tool_use_id == "call-1"


def test_max_turns_returns_typed_error_with_accumulated_partial_text() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="First.")], "pause_turn"),
        Response([ContentBlock("text", text="Second.")], "pause_turn"),
    ])
    events = []
    agent = Agent(
        provider, "model", "system", Registry([]), max_turns=2,
        on_event=events.append,
    )

    with pytest.raises(MaxTurnsError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "First.\nSecond."
    assert any(event.kind == EventKind.MAX_TURNS_REACHED for event in events)


def test_structured_events_include_call_identity_commentary_and_timing() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("tool_use", id="call-7", name="noop", input={})], "tool_use"),
        Response([
            ContentBlock("commentary", text="Checked the result. "),
            ContentBlock("text", text="Done."),
        ], "end_turn"),
    ])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system", Registry([NoopTool()]),
        on_event=events.append,
    )

    assert agent.send("start") == "Done."
    assert [event.kind for event in events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT_COMMENTARY,
        EventKind.ASSISTANT_TEXT,
        EventKind.DONE,
    ]
    assert events[0].tool_use_id == events[1].tool_use_id == "call-7"
    assert events[0].name == events[1].name == "noop"
    assert events[1].duration_ms >= 0
    assert all(event.elapsed_ms >= 0 for event in events)


class CancelTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("cancel", "Cancel execution.", {"type": "object"})

    def run(self, arguments, cancellation=None):
        assert cancellation is not None
        cancellation.cancel("cancelled by test tool")
        cancellation.raise_if_cancelled()


def test_cancellation_commits_error_and_skipped_results_for_announced_calls() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="cancel", input={}),
        ContentBlock("tool_use", id="call-2", name="noop", input={}),
    ], "tool_use")])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system", Registry([CancelTool(), NoopTool()]),
        on_event=events.append,
    )

    with pytest.raises(CancelledError, match="cancelled by test tool"):
        agent.send("start", cancellation=CancellationToken())

    results = agent.messages[-1].content
    assert [result.tool_use_id for result in results] == ["call-1", "call-2"]
    assert all(result.is_error for result in results)
    assert results[0].content == "cancelled by test tool"
    assert "skipped" in results[1].content
    assert [event.tool_use_id for event in events if event.kind == EventKind.TOOL_RESULT] == [
        "call-1", "call-2",
    ]
    assert events[-1].kind == EventKind.ERROR


def test_stop_sequence_is_a_normal_terminal_response() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Stopped."),
    ], "stop_sequence")])
    agent = Agent(provider, "model", "system", Registry([]))

    assert agent.send("start") == "Stopped."
    assert len(provider.requests) == 1
