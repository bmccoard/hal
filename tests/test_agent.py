import itertools
from pathlib import Path
import threading

import pytest

from hal.agent import (
    Agent,
    BudgetExhaustedError,
    ContextWindowExceededError,
    Event,
    EventKind,
    MaxOutputTokensError,
    NoProgressError,
    RepeatedMalformedToolCallError,
    RepeatedToolCallError,
    ProviderProtocolError,
    ProviderResponseError,
    MaxTurnsError,
    UnexpectedStopReasonError,
)
from hal.cancellation import CancelledError, CancellationToken
from hal.harness import Capability, RunBudgets, RunStatus, resolve_capability
from hal.journal import RunJournalStore
from hal.models import ContentBlock, Response, StreamDelta, ToolSpec, Usage
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


class StreamingProvider:
    name = "streaming"

    def complete(self, request, cancellation=None):
        raise AssertionError("buffered completion should not be used")

    def stream(self, request, on_delta, cancellation=None):
        on_delta(StreamDelta("text", "Hel"))
        on_delta(StreamDelta("text", "lo"))
        return Response([ContentBlock("text", text="Hello")])


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


class ParallelTool(Tool):
    parallel_safe = True

    def __init__(self, name: str, barrier: threading.Barrier) -> None:
        self.name = name
        self.barrier = barrier

    @property
    def spec(self):
        return ToolSpec(self.name, self.name, {"type": "object"})

    def run(self, arguments, cancellation=None):
        self.barrier.wait(timeout=1)
        return f"{self.name}-done"


class FastParallelTool(Tool):
    parallel_safe = True

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def spec(self):
        return ToolSpec(self.name, self.name, {"type": "object"})

    def run(self, arguments, cancellation=None):
        return f"{self.name}-done"


class SubagentTool(Tool):
    def __init__(self, child_provider) -> None:
        self.parent = None
        self.child_provider = child_provider
        self.child_outcome = None

    @property
    def spec(self):
        return ToolSpec("delegate", "delegate", {"type": "object"})

    def run(self, arguments, cancellation=None):
        text, self.child_outcome = self.parent.run_subagent(
            "inspect", resolve_capability("inspect"),
            RunBudgets(
                provider_calls=3, tool_calls=2, elapsed_seconds=None,
            ),
            provider=self.child_provider,
        )
        return text


def test_agent_executes_tool_and_keeps_matching_result(tmp_path: Path) -> None:
    provider = FakeProvider()
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))
    target = tmp_path / "created.txt"
    assert agent.send(str(target)) == "done"
    assert target.read_text(encoding="utf-8") == "hello"
    assert agent.messages[1].content[0].id == "call-1"
    assert agent.messages[2].content[0].tool_use_id == "call-1"


def test_agent_forwards_stream_deltas_without_reemitting_final_text() -> None:
    events = []
    agent = Agent(StreamingProvider(), "model", "system", Registry([]), on_event=events.append)

    assert agent.send("hello") == "Hello"

    text = [event.text for event in events if event.kind == EventKind.ASSISTANT_TEXT]
    assert text == ["Hel", "lo"]
    assert events[-2].kind == EventKind.DONE
    assert events[-1].kind == EventKind.RUN_FINISHED


def test_agent_can_isolate_a_turn_without_discarding_saved_history() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="first")]),
        Response([ContentBlock("text", text="second")]),
    ])
    agent = Agent(provider, "model", "system", Registry([]))

    assert agent.send("one") == "first"
    assert agent.send("two", include_history=False) == "second"

    assert len(provider.requests[0].messages) == 1
    assert len(provider.requests[1].messages) == 1
    assert provider.requests[1].messages[0].content[0].text == "two"
    assert len(agent.messages) == 4


def test_tool_turn_is_committed_only_after_all_results_exist(tmp_path: Path) -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="interrupt", input={}),
    ], "tool_use")])
    agent = Agent(provider, "model", "system", Registry([InterruptTool()]))

    with pytest.raises(KeyboardInterrupt):
        agent.send("start")

    assert len(agent.messages) == 3
    assert agent.messages[1].content[0].id == "call-1"
    assert agent.messages[2].content[0].tool_use_id == "call-1"
    assert agent.messages[2].content[0].is_error is True


def test_interrupted_tool_batch_preserves_completed_and_skipped_results() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="noop", input={}),
        ContentBlock("tool_use", id="call-2", name="interrupt", input={}),
        ContentBlock("tool_use", id="call-3", name="noop", input={}),
    ], "tool_use")])
    agent = Agent(provider, "model", "system", Registry([NoopTool(), InterruptTool()]))

    with pytest.raises(KeyboardInterrupt):
        agent.send("start")

    results = agent.messages[-1].content
    assert [result.tool_use_id for result in results] == ["call-1", "call-2", "call-3"]
    assert results[0].content == "ok"
    assert results[0].is_error is False
    assert results[1].is_error is True
    assert "KeyboardInterrupt" in results[1].content
    assert "skipped" in results[2].content


class FailingStreamingProvider:
    name = "failing-stream"

    def complete(self, request, cancellation=None):
        raise AssertionError("buffered completion should not be used")

    def stream(self, request, on_delta, cancellation=None):
        on_delta(StreamDelta("commentary", "Checking. "))
        on_delta(StreamDelta("text", "Partial answer"))
        raise RuntimeError("connection lost")


def test_stream_failure_preserves_visible_partial_output() -> None:
    events: list[Event] = []
    agent = Agent(
        FailingStreamingProvider(), "model", "system", Registry([]),
        on_event=events.append,
    )

    with pytest.raises(ProviderResponseError, match="connection lost") as raised:
        agent.send("start")

    assert raised.value.partial_text == "Partial answer"
    assert [block.type for block in agent.messages[-1].content] == ["commentary", "text"]
    assert events[-2].partial_text == "Partial answer"


@pytest.mark.parametrize("calls, message", [
    ([ContentBlock("tool_use", id="", name="noop", input={})], "without an id"),
    ([ContentBlock("tool_use", id="same", name="noop", input={}),
      ContentBlock("tool_use", id="same", name="noop", input={})], "duplicate"),
    ([ContentBlock("tool_use", id="call-1", name="", input={})], "without a name"),
])
def test_invalid_tool_call_identity_is_rejected_before_execution(calls, message) -> None:
    provider = ScriptedProvider([Response(calls, "tool_use")])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(ProviderProtocolError, match=message):
        agent.send("start")

    assert len(agent.messages) == 1


def test_tool_use_stop_without_call_is_protocol_error() -> None:
    provider = ScriptedProvider([Response([], "tool_use")])
    agent = Agent(provider, "model", "system", Registry([]))

    with pytest.raises(ProviderProtocolError, match="without returning a tool call"):
        agent.send("start")

    assert len(provider.requests) == 1


def test_event_handler_failure_does_not_abort_turn() -> None:
    def broken_handler(_event):
        raise RuntimeError("display failed")

    agent = Agent(
        ScriptedProvider([Response([ContentBlock("text", text="Done.")])]),
        "model", "system", Registry([]), on_event=broken_handler,
    )

    assert agent.send("start") == "Done."
    assert len(agent.event_errors) == 4


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


def test_tool_batch_transcript_invariant_across_failure_permutations() -> None:
    """Every announced call gets exactly one ordered result for mixed failures."""
    for names in itertools.product(("noop", "fail", "missing"), repeat=3):
        calls = [
            ContentBlock(
                "tool_use", id=f"call-{index}", name=name,
                input={"value": index},
            )
            for index, name in enumerate(names, 1)
        ]
        provider = ScriptedProvider([
            Response(calls, "tool_use"),
            Response([ContentBlock("text", text="Done.")], "end_turn"),
        ])
        agent = Agent(
            provider, "model", "system", Registry([NoopTool(), FailTool()]),
        )

        assert agent.send("start") == "Done."

        results = agent.messages[-2].content
        assert [item.tool_use_id for item in results] == [
            "call-1", "call-2", "call-3",
        ]
        assert len(results) == len(calls)
        assert all(item.type == "tool_result" for item in results)
        for name, item in zip(names, results):
            assert item.is_error is (name != "noop")


def test_parallel_safe_batch_executes_concurrently_and_commits_in_order() -> None:
    barrier = threading.Barrier(2)
    provider = ScriptedProvider([
        Response([
            ContentBlock("tool_use", id="call-1", name="read_a", input={}),
            ContentBlock("tool_use", id="call-2", name="read_b", input={}),
        ], "tool_use"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system",
        Registry([
            ParallelTool("read_a", barrier), ParallelTool("read_b", barrier),
        ]),
        on_event=events.append,
    )

    assert agent.send("start") == "Done."

    results = agent.messages[2].content
    assert [item.tool_use_id for item in results] == ["call-1", "call-2"]
    assert [item.content for item in results] == ["read_a-done", "read_b-done"]
    result_events = [event for event in events if event.kind == EventKind.TOOL_RESULT]
    assert [event.tool_use_id for event in result_events] == ["call-1", "call-2"]


def test_approval_gated_parallel_safe_tools_remain_serial() -> None:
    barrier = threading.Barrier(2)
    tools = [ParallelTool("read_a", barrier), ParallelTool("read_b", barrier)]
    registry = Registry(
        tools, approvals=["read_a", "read_b"], confirm=lambda _prompt: False,
    )
    assert registry.is_parallel_safe("read_a") is False
    assert registry.is_parallel_safe("read_b") is False


def test_mixed_batch_parallelizes_adjacent_safe_group_before_serial_barrier() -> None:
    barrier = threading.Barrier(2)
    provider = ScriptedProvider([
        Response([
            ContentBlock("tool_use", id="call-1", name="read_a", input={}),
            ContentBlock("tool_use", id="call-2", name="read_b", input={}),
            ContentBlock("tool_use", id="call-3", name="noop", input={}),
        ], "tool_use"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system",
        Registry([
            ParallelTool("read_a", barrier), ParallelTool("read_b", barrier),
            NoopTool(),
        ]),
        on_event=events.append,
    )

    assert agent.send("start") == "Done."

    results = agent.messages[2].content
    assert [item.tool_use_id for item in results] == [
        "call-1", "call-2", "call-3",
    ]
    activity = [
        (event.kind, event.tool_use_id)
        for event in events
        if event.kind in {EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
    ]
    assert activity == [
        (EventKind.TOOL_CALL, "call-1"),
        (EventKind.TOOL_CALL, "call-2"),
        (EventKind.TOOL_RESULT, "call-1"),
        (EventKind.TOOL_RESULT, "call-2"),
        (EventKind.TOOL_CALL, "call-3"),
        (EventKind.TOOL_RESULT, "call-3"),
    ]


def test_mixed_parallel_group_budget_exhaustion_commits_all_results() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="read_a", input={}),
        ContentBlock("tool_use", id="call-2", name="read_b", input={}),
        ContentBlock("tool_use", id="call-3", name="noop", input={}),
    ], "tool_use")])
    agent = Agent(
        provider, "model", "system",
        Registry([
            FastParallelTool("read_a"), FastParallelTool("read_b"), NoopTool(),
        ]),
    )

    with pytest.raises(BudgetExhaustedError):
        agent.send("start", budgets=RunBudgets(
            provider_calls=None, tool_calls=1, elapsed_seconds=None,
        ))

    results = agent.messages[-1].content
    assert [item.tool_use_id for item in results] == [
        "call-1", "call-2", "call-3",
    ]
    assert results[0].content == "read_a-done"
    assert all(item.is_error for item in results[1:])
    assert agent.run_counters.tool_calls == 1


def test_subagent_inherits_narrower_policy_budget_and_run_attribution(
    tmp_path: Path,
) -> None:
    child_provider = ScriptedProvider([
        Response([ContentBlock("text", text="Child result")], "end_turn"),
    ])
    delegate = SubagentTool(child_provider)
    parent_provider = ScriptedProvider([
        Response([
            ContentBlock("tool_use", id="call-1", name="delegate", input={}),
        ], "tool_use"),
        Response([ContentBlock("text", text="Parent done")], "end_turn"),
    ])
    events: list[Event] = []
    journals = RunJournalStore(tmp_path / "runs")
    agent = Agent(
        parent_provider, "model", "system", Registry([delegate]),
        capability=resolve_capability("change"), on_event=events.append,
        budgets=RunBudgets(
            provider_calls=5, tool_calls=5, elapsed_seconds=None,
        ),
        journal_store=journals, workspace=tmp_path,
    )
    delegate.parent = agent

    assert agent.send("start") == "Parent done"

    assert delegate.child_outcome is not None
    assert agent.last_outcome is not None
    assert delegate.child_outcome.capability == "inspect"
    assert delegate.child_outcome.parent_run_id == agent.last_outcome.run_id
    assert delegate.child_outcome.counters.provider_calls == 1
    assert agent.run_counters.provider_calls == 3
    assert agent.run_counters.tool_calls == 1
    assert agent.last_outcome.child_outcomes == [delegate.child_outcome]
    child_events = [event for event in events if event.parent_run_id]
    assert child_events
    assert {event.run_id for event in child_events} == {
        delegate.child_outcome.run_id,
    }
    assert {event.parent_run_id for event in child_events} == {
        agent.last_outcome.run_id,
    }
    child_journal = journals.load(delegate.child_outcome.run_id)
    assert child_journal is not None
    assert child_journal["parent_run_id"] == agent.last_outcome.run_id


def test_subagent_requires_active_parent_and_strictly_narrower_capability() -> None:
    agent = Agent(
        ScriptedProvider([]), "model", "system", Registry([]),
        capability=resolve_capability("change"),
    )
    with pytest.raises(RuntimeError, match="active parent"):
        agent.run_subagent(
            "child", resolve_capability("inspect"), RunBudgets(),
        )


def test_invalid_tool_arguments_are_returned_without_execution() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id="call-1", name="noop", input={},
            argument_error="noop was not executed: invalid JSON arguments",
        )], "tool_use"),
        Response([ContentBlock("text", text="Recovered.")], "end_turn"),
    ])
    tool = NoopTool()
    tool.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("malformed call must not execute")
    )
    agent = Agent(provider, "model", "system", Registry([tool]))

    assert agent.send("start") == "Recovered."
    result = agent.messages[2].content[0]
    assert result.tool_use_id == "call-1"
    assert result.is_error is True
    assert "was not executed" in result.content


def test_three_malformed_calls_stop_turn_with_valid_transcript() -> None:
    malformed = lambda index: Response([ContentBlock(
        "tool_use", id=f"call-{index}", name="noop",
        argument_error="noop was not executed: invalid JSON arguments",
    )], "tool_use")
    provider = ScriptedProvider([malformed(1), malformed(2), malformed(3)])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(RepeatedMalformedToolCallError, match="tool 'noop'"):
        agent.send("start")

    assert len(provider.requests) == 3
    assert len(agent.messages) == 7
    assert agent.messages[-1].content[0].is_error is True
    assert "stopped this turn after three malformed calls" in agent.messages[-1].content[0].content


def test_three_identical_successful_calls_stop_turn() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id=f"call-{index}", name="noop", input={},
        )], "tool_use")
        for index in range(3)
    ])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(RepeatedToolCallError, match="tool call 'noop'"):
        agent.send("start")

    assert len(provider.requests) == 3
    assert "three identical calls" in agent.messages[-1].content[0].content


def test_three_repetitions_of_short_tool_cycle_stop_turn() -> None:
    responses = []
    for index, arguments in enumerate((
        {"value": "a"}, {"value": "b"},
        {"value": "a"}, {"value": "b"},
        {"value": "a"}, {"value": "b"},
    ), 1):
        responses.append(Response([ContentBlock(
            "tool_use", id=f"call-{index}", name="noop", input=arguments,
        )], "tool_use"))
    provider = ScriptedProvider(responses)
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(RepeatedToolCallError, match="cycle of length 2"):
        agent.send("start")

    assert len(provider.requests) == 6
    assert "sequence of 2 tool calls repeated three times" in agent.messages[-1].content[0].content


def test_tool_signature_canonicalizes_nested_argument_key_order() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id=f"call-{index}", name="noop", input=arguments,
        )], "tool_use")
        for index, arguments in enumerate((
            {"outer": {"a": 1, "b": 2}},
            {"outer": {"b": 2, "a": 1}},
            {"outer": {"a": 1, "b": 2}},
        ), 1)
    ])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(RepeatedToolCallError, match="identical"):
        agent.send("start")


def test_allowed_tools_limit_schema_and_execution() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id="call-1", name="noop", input={},
        )], "tool_use"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    assert agent.send("start", allowed_tools=set()) == "Done."
    assert provider.requests[0].tools == []
    result = agent.messages[2].content[0]
    assert result.is_error is True
    assert "not available in this workflow phase" in result.content


def test_denied_tools_are_hidden_and_cannot_execute() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id="call-1", name="noop", input={},
        )], "tool_use"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    assert agent.send("start", denied_tools={"noop"}) == "Done."
    assert provider.requests[0].tools == []
    result = agent.messages[2].content[0]
    assert result.is_error is True
    assert "not available in this workflow phase" in result.content


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


def test_three_empty_pause_turns_stop_as_no_progress() -> None:
    provider = ScriptedProvider([Response([], "pause_turn") for _ in range(3)])
    agent = Agent(provider, "model", "system", Registry([]))

    with pytest.raises(NoProgressError, match="three empty pause turns"):
        agent.send("start")

    assert len(provider.requests) == 3
    assert len(agent.messages) == 1


def test_nonempty_pause_turn_resets_no_progress_count() -> None:
    provider = ScriptedProvider([
        Response([], "pause_turn"),
        Response([], "pause_turn"),
        Response([ContentBlock("text", text="Working.")], "pause_turn"),
        Response([], "pause_turn"),
        Response([], "pause_turn"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    agent = Agent(provider, "model", "system", Registry([]))

    assert agent.send("start") == "Working.\nDone."


def test_max_tokens_returns_typed_error_with_partial_text() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Truncated answer."),
    ], "max_tokens")])
    agent = Agent(
        provider, "model", "system", Registry([]),
        max_output_continuations=0,
    )

    with pytest.raises(MaxOutputTokensError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "Truncated answer."
    assert len(provider.requests) == 1


def test_clean_text_truncation_continues_within_bound() -> None:
    provider = ScriptedProvider([
        Response([
            ContentBlock("raw", raw={
                "type": "reasoning", "id": "reason-1", "status": "completed",
            }),
            ContentBlock("text", text="First half."),
        ], "max_tokens"),
        Response([ContentBlock("text", text="Second half.")], "end_turn"),
    ])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system", Registry([]), on_event=events.append,
        max_output_tokens=16_384, max_output_continuations=2,
        reasoning_effort="xhigh",
    )

    assert agent.send("start") == "First half.\nSecond half."
    assert len(provider.requests) == 2
    assert all(request.max_tokens == 16_384 for request in provider.requests)
    assert all(request.reasoning_effort == "xhigh" for request in provider.requests)
    continuation = provider.requests[1].messages[-1]
    assert continuation.role == "user"
    assert continuation.display_text == "[harness output continuation]"
    assert "Do not repeat" in continuation.content[0].text
    continuation_events = [
        event for event in events if event.kind == EventKind.OUTPUT_CONTINUATION
    ]
    assert [event.args for event in continuation_events] == [
        {"attempt": 1, "limit": 2},
    ]


def test_clean_text_truncation_stops_after_continuation_limit() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text=f"Part {number}.")], "length")
        for number in range(1, 4)
    ])
    agent = Agent(
        provider, "model", "system", Registry([]),
        max_output_continuations=2,
    )

    with pytest.raises(MaxOutputTokensError) as raised:
        agent.send("start")

    assert raised.value.partial_text == "Part 1.\nPart 2.\nPart 3."
    assert len(provider.requests) == 3


def test_output_continuation_obeys_provider_call_budget() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="First half.")], "max_tokens"),
    ])
    agent = Agent(
        provider, "model", "system", Registry([]),
        budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ),
        max_output_continuations=2,
    )

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start")

    assert raised.value.reason_code == "budget_provider_calls_exhausted"
    assert raised.value.partial_text == "First half."
    assert len(provider.requests) == 1


@pytest.mark.parametrize("content", [
    [],
    [ContentBlock("text", text="   ")],
    [ContentBlock("commentary", text="unfinished reasoning")],
    [
        ContentBlock("text", text="Partial answer."),
        ContentBlock("raw", raw={"type": "unknown_structure"}),
    ],
])
def test_non_clean_truncation_is_not_automatically_continued(content) -> None:
    provider = ScriptedProvider([Response(content, "max_tokens")])
    agent = Agent(
        provider, "model", "system", Registry([]),
        max_output_continuations=2,
    )

    with pytest.raises(MaxOutputTokensError):
        agent.send("start")

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
        EventKind.RUN_STARTED,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT_COMMENTARY,
        EventKind.ASSISTANT_TEXT,
        EventKind.DONE,
        EventKind.RUN_FINISHED,
    ]
    assert events[1].tool_use_id == events[2].tool_use_id == "call-7"
    assert events[1].name == events[2].name == "noop"
    assert events[2].duration_ms >= 0
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
    assert events[-2].kind == EventKind.ERROR


def test_stop_sequence_is_a_normal_terminal_response() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Stopped."),
    ], "stop_sequence")])
    agent = Agent(provider, "model", "system", Registry([]))

    assert agent.send("start") == "Stopped."
    assert len(provider.requests) == 1


def test_provider_call_budget_stops_before_another_request() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="Working.")], "pause_turn"),
        Response([ContentBlock("text", text="Must not be requested.")]),
    ])
    events: list[Event] = []
    agent = Agent(provider, "model", "system", Registry([]), on_event=events.append)

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start", budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ))

    assert raised.value.reason_code == "budget_provider_calls_exhausted"
    assert len(provider.requests) == 1
    assert agent.run_counters.provider_calls == 1
    assert agent.last_outcome is not None
    assert agent.last_outcome.status == RunStatus.BUDGET_EXHAUSTED
    assert agent.last_outcome.reason == "budget_provider_calls_exhausted"
    assert agent.last_outcome.final_text == "Working."
    assert any(event.kind == EventKind.BUDGET_UPDATED for event in events)
    assert events[-2].kind == EventKind.ERROR
    assert events[-2].reason == "budget_provider_calls_exhausted"


def test_per_send_budget_cannot_weaken_configured_budget() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="Working.")], "pause_turn"),
        Response([ContentBlock("text", text="Must not be requested.")]),
    ])
    agent = Agent(
        provider, "model", "system", Registry([]),
        budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ),
    )

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start", budgets=RunBudgets(
            provider_calls=5, tool_calls=None, elapsed_seconds=None,
        ))

    assert raised.value.reason_code == "budget_provider_calls_exhausted"
    assert len(provider.requests) == 1


def test_capability_budget_cannot_weaken_configured_or_per_send_budget() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock("text", text="Working.")], "pause_turn"),
        Response([ContentBlock("text", text="Must not be requested.")]),
    ])
    capability = Capability(
        "bounded", "bounded", budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ),
    )
    agent = Agent(provider, "model", "system", Registry([]))

    with pytest.raises(BudgetExhaustedError):
        agent.send(
            "start", capability=capability,
            budgets=RunBudgets(
                provider_calls=5, tool_calls=None, elapsed_seconds=None,
            ),
        )

    assert len(provider.requests) == 1


def test_tool_call_budget_commits_ordered_results_before_stopping() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="noop", input={"value": 1}),
        ContentBlock("tool_use", id="call-2", name="noop", input={"value": 2}),
        ContentBlock("tool_use", id="call-3", name="noop", input={"value": 3}),
    ], "tool_use")])
    agent = Agent(provider, "model", "system", Registry([NoopTool()]))

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start", budgets=RunBudgets(
            provider_calls=None, tool_calls=1, elapsed_seconds=None,
        ))

    assert raised.value.reason_code == "budget_tool_calls_exhausted"
    assert agent.run_counters.tool_calls == 1
    results = agent.messages[-1].content
    assert [result.tool_use_id for result in results] == ["call-1", "call-2", "call-3"]
    assert results[0].content == "ok"
    assert results[0].is_error is False
    assert "not executed" in results[1].content
    assert "skipped" in results[2].content
    assert all(result.is_error for result in results[1:])


def test_output_token_budget_blocks_announced_tool_execution() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("tool_use", id="call-1", name="noop", input={}),
    ], "tool_use", Usage(output_tokens=5))])
    tool = NoopTool()
    tool.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("token budget must stop tool execution")
    )
    agent = Agent(provider, "model", "system", Registry([tool]))

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start", budgets=RunBudgets(
            provider_calls=None, tool_calls=None, elapsed_seconds=None,
            output_tokens=5,
        ))

    assert raised.value.reason_code == "budget_output_tokens_exhausted"
    assert agent.run_counters.usage.output_tokens == 5
    assert agent.run_counters.tool_calls == 0
    assert agent.messages[-1].content[0].is_error is True


def test_elapsed_budget_blocks_work_after_completed_provider_call(monkeypatch) -> None:
    clock = [10.0]

    class SlowProvider:
        name = "slow"

        def complete(self, request, cancellation=None):
            clock[0] = 12.0
            return Response([
                ContentBlock("tool_use", id="call-1", name="noop", input={}),
            ], "tool_use")

    monkeypatch.setattr("hal.agent.time.monotonic", lambda: clock[0])
    agent = Agent(SlowProvider(), "model", "system", Registry([NoopTool()]))

    with pytest.raises(BudgetExhaustedError) as raised:
        agent.send("start", budgets=RunBudgets(
            provider_calls=None, tool_calls=None, elapsed_seconds=1,
        ))

    assert raised.value.reason_code == "budget_elapsed_seconds_exhausted"
    assert agent.run_counters.elapsed_seconds == 2
    assert agent.run_counters.tool_calls == 0
    assert agent.messages[-1].content[0].is_error is True


def test_successful_budgeted_run_records_outcome_and_run_usage() -> None:
    provider = ScriptedProvider([Response(
        [ContentBlock("text", text="Done.")], "end_turn",
        Usage(input_tokens=3, output_tokens=2),
    )])
    events: list[Event] = []
    agent = Agent(provider, "model", "system", Registry([]), on_event=events.append)

    assert agent.send("start", budgets=RunBudgets()) == "Done."

    assert agent.last_outcome is not None
    assert agent.last_outcome.status == RunStatus.SUCCEEDED
    assert agent.last_outcome.reason == "completed"
    assert agent.last_outcome.final_text == "Done."
    assert agent.last_outcome.counters.provider_calls == 1
    assert agent.last_outcome.counters.usage.input_tokens == 3
    budget_events = [event for event in events if event.kind == EventKind.BUDGET_UPDATED]
    assert budget_events[-1].input_tokens == 3
    assert budget_events[-1].output_tokens == 2


def test_run_lifecycle_events_have_id_status_and_monotonic_sequence() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Done."),
    ], "end_turn")])
    events: list[Event] = []
    agent = Agent(provider, "model", "system", Registry([]), on_event=events.append)

    assert agent.send("start") == "Done."

    assert events[0].kind == EventKind.RUN_STARTED
    assert events[0].status == "running"
    assert events[-1].kind == EventKind.RUN_FINISHED
    assert events[-1].status == "succeeded"
    assert events[-1].reason == "completed"
    assert agent.last_outcome is not None
    assert {event.run_id for event in events} == {agent.last_outcome.run_id}
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


def test_capability_filters_schema_and_execution_and_labels_outcome() -> None:
    provider = ScriptedProvider([
        Response([ContentBlock(
            "tool_use", id="call-1", name="noop", input={},
        )], "tool_use"),
        Response([ContentBlock("text", text="Done.")], "end_turn"),
    ])
    events: list[Event] = []
    agent = Agent(
        provider, "model", "system", Registry([NoopTool()]),
        on_event=events.append,
    )

    assert agent.send("start", capability=resolve_capability("inspect")) == "Done."

    assert provider.requests[0].tools == []
    assert agent.messages[2].content[0].is_error is True
    assert agent.last_outcome is not None
    assert agent.last_outcome.capability == "inspect"
    assert all(event.capability == "inspect" for event in events)


def test_default_and_per_send_capabilities_are_intersected() -> None:
    provider = ScriptedProvider([Response([
        ContentBlock("text", text="Done."),
    ], "end_turn")])
    agent = Agent(
        provider, "model", "system", Registry([NoopTool()]),
        capability=Capability(
            "default", "default", allowed_tools=frozenset({"noop", "other"}),
        ),
    )

    assert agent.send(
        "start",
        capability=Capability(
            "turn", "turn", allowed_tools=frozenset({"other"}),
        ),
    ) == "Done."

    assert provider.requests[0].tools == []
    assert agent.last_outcome is not None
    assert agent.last_outcome.capability == "default+turn"
