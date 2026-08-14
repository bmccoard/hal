from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import json
import time
from typing import NoReturn

from .cancellation import CancelledError, CancellationToken, cancellation_or_default
from .harness import (
    BudgetReason, Capability, RunBudgets, RunCounters, RunOutcome, RunStatus,
    compose_tool_policy,
)
from .models import ContentBlock, Message, Request, StreamDelta, Usage
from .providers import Provider
from .tools import Registry, bound_output


class AgentError(RuntimeError):
    """Base error for a turn that may have produced useful partial text."""

    def __init__(self, message: str, partial_text: str = "") -> None:
        super().__init__(message)
        self.partial_text = partial_text


class UnexpectedStopReasonError(AgentError):
    pass


class MaxOutputTokensError(AgentError):
    pass


class ContextWindowExceededError(AgentError):
    pass


class MaxTurnsError(AgentError):
    pass


class RepeatedMalformedToolCallError(AgentError):
    pass


class RepeatedToolCallError(AgentError):
    pass


class ProviderResponseError(AgentError):
    """A provider failed after it may have emitted useful streamed output."""


class ProviderProtocolError(AgentError):
    """A provider returned a response that cannot form a valid transcript."""


class NoProgressError(AgentError):
    """The provider repeatedly requested continuation without producing output."""


class BudgetExhaustedError(AgentError):
    """A harness run reached a hard limit before starting more work."""

    def __init__(self, reason: BudgetReason, limit: int | float,
                 partial_text: str = "") -> None:
        self.reason = reason
        self.limit = limit
        super().__init__(
            f"run exhausted {reason.value} budget ({limit})", partial_text,
        )

    @property
    def reason_code(self) -> str:
        return self.reason.code


class EventKind(str, Enum):
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_COMMENTARY = "assistant_commentary"
    PARALLEL_START = "parallel_start"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STEERING_APPLIED = "steering_applied"
    DONE = "done"
    ERROR = "error"
    MAX_TURNS_REACHED = "max_turns_reached"
    BUDGET_UPDATED = "budget_updated"


@dataclass(slots=True)
class Event:
    """Structured activity emitted by an agent turn.

    ``elapsed_ms`` is measured from the start of ``send``. ``duration_ms`` is
    populated for completed operations such as tool calls.
    """

    kind: EventKind
    text: str = ""
    name: str = ""
    args: dict[str, object] = field(default_factory=dict)
    tool_use_id: str = ""
    is_error: bool = False
    elapsed_ms: int = 0
    duration_ms: int = 0
    max_turns: int = 0
    error: BaseException | None = None
    partial_text: str = ""
    reason: str = ""
    provider_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    capability: str = ""


EventHandler = Callable[[Event], None]

_KNOWN_STOP_REASONS = {
    "", "end_turn", "stop_sequence", "tool_use", "max_tokens", "length",
    "refusal", "pause_turn", "model_context_window_exceeded",
}


class Agent:
    """Provider-neutral model/tool loop with a serial, structurally valid transcript."""

    def __init__(self, provider: Provider, model: str, system: str, tools: Registry,
                 messages: list[Message] | None = None, usage: Usage | None = None,
                 max_turns: int = 500, on_event: EventHandler | None = None,
                 budgets: RunBudgets | None = None,
                 capability: Capability | None = None) -> None:
        self.provider = provider
        self.model = model
        self.system = system
        self.tools = tools
        self.messages = list(messages or [])
        self.usage = usage or Usage()
        self.max_turns = max_turns
        self.on_event = on_event or (lambda _event: None)
        self.event_errors: list[Exception] = []
        self._send_started = 0.0
        self.budgets = budgets
        self.capability = capability
        self.run_counters = RunCounters()
        self.last_outcome: RunOutcome | None = None
        self._active_budgets: RunBudgets | None = None
        self._active_capability = ""

    def send(self, text: str, display_text: str = "",
             cancellation: CancellationToken | None = None,
             allowed_tools: set[str] | None = None,
             denied_tools: set[str] | None = None,
             protect_existing_files: bool = False,
             include_history: bool = True,
             budgets: RunBudgets | None = None,
             capability: Capability | None = None) -> str:
        """Run one user turn, optionally under hard harness budgets."""
        self._send_started = time.monotonic()
        self._active_budgets = budgets if budgets is not None else self.budgets
        policies = tuple(item for item in (self.capability, capability) if item is not None)
        policy = compose_tool_policy(
            *policies, allowed_tools=allowed_tools, denied_tools=denied_tools,
            protect_existing_files=protect_existing_files,
        )
        self._active_capability = "+".join(item.name for item in policies)
        self.run_counters = RunCounters()
        outcome = RunOutcome(
            capability=self._active_capability, counters=self.run_counters,
        )
        self.last_outcome = outcome
        try:
            result = self._run_turn(
                text, display_text, cancellation,
                None if policy.allowed_tools is None else set(policy.allowed_tools),
                set(policy.denied_tools), policy.protect_existing_files,
                include_history,
            )
        except CancelledError:
            outcome.status = RunStatus.CANCELLED
            outcome.reason = "cancelled"
            raise
        except BudgetExhaustedError as exc:
            outcome.status = RunStatus.BUDGET_EXHAUSTED
            outcome.reason = exc.reason_code
            outcome.final_text = exc.partial_text
            raise
        except BaseException as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = "agent_error" if isinstance(exc, AgentError) else "interrupted"
            outcome.final_text = getattr(exc, "partial_text", "")
            raise
        else:
            outcome.status = RunStatus.SUCCEEDED
            outcome.reason = "completed"
            outcome.final_text = result
            return result
        finally:
            self._update_elapsed()
            self._active_budgets = None
            self._active_capability = ""

    def _run_turn(self, text: str, display_text: str = "",
                  cancellation: CancellationToken | None = None,
                  allowed_tools: set[str] | None = None,
                  denied_tools: set[str] | None = None,
                  protect_existing_files: bool = False,
                  include_history: bool = True) -> str:
        if not text.strip():
            raise AgentError("message is empty")
        cancellation = cancellation_or_default(cancellation)
        turn_start = len(self.messages)
        self.messages.append(Message("user", [ContentBlock("text", text=text)], display_text=display_text))
        final_parts: list[str] = []
        malformed_counts: dict[str, int] = {}
        tool_signatures: list[tuple[str, str, bool, str]] = []
        no_progress_count = 0
        for _ in range(self.max_turns):
            streamed_blocks: list[ContentBlock] = []

            def emit_delta(delta: StreamDelta) -> None:
                if delta.text:
                    block_type = "commentary" if delta.kind == "commentary" else "text"
                    if streamed_blocks and streamed_blocks[-1].type == block_type:
                        streamed_blocks[-1].text += delta.text
                    else:
                        streamed_blocks.append(ContentBlock(block_type, text=delta.text))
                self._emit_delta(delta)

            try:
                cancellation.raise_if_cancelled()
                self._before_provider_call(partial="\n".join(final_parts).strip())
                request = Request(
                    model=self.model, system=self.system,
                    messages=list(self.messages if include_history else self.messages[turn_start:]),
                    tools=self.tools.specs_for(allowed_tools, denied_tools),
                )
                stream = getattr(self.provider, "stream", None)
                streamed = callable(stream) and getattr(self.provider, "streaming_enabled", True)
                if streamed:
                    response = stream(request, emit_delta, cancellation)
                else:
                    response = self.provider.complete(request, cancellation)
                cancellation.raise_if_cancelled()
            except BudgetExhaustedError as exc:
                self._fail(exc)
            except CancelledError as exc:
                self._append_streamed_partial(streamed_blocks)
                self._emit(Event(
                    EventKind.ERROR, text=str(exc), error=exc,
                    partial_text=self._partial_from(final_parts, streamed_blocks),
                ))
                raise
            except Exception as exc:
                partial = self._partial_from(final_parts, streamed_blocks)
                self._append_streamed_partial(streamed_blocks)
                error = ProviderResponseError(str(exc), partial)
                self._emit(Event(
                    EventKind.ERROR, text=str(error), error=error,
                    partial_text=partial,
                ))
                raise error from exc
            self.usage.add(response.usage)
            self.run_counters.usage.add(response.usage)
            self._emit_budget_update()
            assistant_blocks = response.content
            for block in assistant_blocks:
                if not block.text:
                    continue
                if block.type == "text":
                    final_parts.append(block.text)
                    if not streamed:
                        self._emit(Event(EventKind.ASSISTANT_TEXT, text=block.text))
                elif block.type == "commentary":
                    if not streamed:
                        self._emit(Event(EventKind.ASSISTANT_COMMENTARY, text=block.text))
            partial = "\n".join(final_parts).strip()
            calls = [block for block in assistant_blocks if block.type == "tool_use"]

            self._validate_response(response.stop_reason, calls, partial)
            meaningful = bool(calls) or any(
                block.text for block in assistant_blocks
                if block.type in {"text", "commentary"}
            )
            no_progress_count = 0 if meaningful else no_progress_count + 1
            if response.stop_reason == "pause_turn" and no_progress_count >= 3:
                self._fail(NoProgressError(
                    "provider returned three empty pause turns without making progress",
                    partial,
                ))

            if response.stop_reason not in _KNOWN_STOP_REASONS:
                self._append_safe_assistant(assistant_blocks)
                self._fail(UnexpectedStopReasonError(
                    f"unexpected provider stop reason: {response.stop_reason!r}", partial
                ))

            if response.stop_reason == "refusal":
                self._append_safe_assistant(assistant_blocks)
                self._emit(Event(EventKind.DONE))
                return partial

            if response.stop_reason == "model_context_window_exceeded":
                self._append_safe_assistant(assistant_blocks)
                self._fail(ContextWindowExceededError(
                    "provider context window was exceeded", partial
                ))

            if not calls:
                if assistant_blocks:
                    self.messages.append(Message("assistant", assistant_blocks))
                if response.stop_reason in {"", "end_turn", "stop_sequence"}:
                    self._emit(Event(EventKind.DONE))
                    return partial
                if response.stop_reason in {"max_tokens", "length"}:
                    self._fail(MaxOutputTokensError(
                        "provider response was truncated at the token limit", partial
                    ))
                # pause_turn explicitly asks for another provider response.
                continue

            # Commit one result for every announced call even when a tool raises a
            # BaseException. Earlier tools may already have changed external state,
            # so dropping the batch would make retries unsafe and history untruthful.
            results: list[ContentBlock] = []
            cancelled: CancelledError | None = None
            interrupted: BaseException | None = None
            repeated_malformed: RepeatedMalformedToolCallError | None = None
            repeated_tool: RepeatedToolCallError | None = None
            budget_exhausted: BudgetExhaustedError | None = None
            for call in calls:
                self._emit(Event(
                    EventKind.TOOL_CALL, name=call.name, args=dict(call.input),
                    tool_use_id=call.id,
                ))
                tool_started = time.monotonic()
                error = (
                    cancelled is not None or interrupted is not None
                    or budget_exhausted is not None
                )
                if interrupted is not None:
                    output = "skipped because an earlier tool interrupted the agent turn"
                elif cancelled is not None:
                    output = "skipped because the agent turn was cancelled"
                elif budget_exhausted is not None:
                    output = "skipped because the harness budget was exhausted"
                elif call.argument_error:
                    error = True
                    malformed_counts[call.name] = malformed_counts.get(call.name, 0) + 1
                    output = call.argument_error
                    if malformed_counts[call.name] >= 3:
                        output += (
                            " HAL stopped this turn after three malformed calls to "
                            f"{call.name!r}; use a different available tool or finish "
                            "the response without that tool."
                        )
                        repeated_malformed = RepeatedMalformedToolCallError(
                            f"repeated malformed arguments for tool {call.name!r}", partial,
                        )
                else:
                    try:
                        cancellation.raise_if_cancelled()
                        self._before_tool_call(partial)
                        output = self.tools.run(
                            call.name, call.input, cancellation,
                            allowed_tools, denied_tools, protect_existing_files,
                        )
                    except BudgetExhaustedError as exc:
                        budget_exhausted = exc
                        error = True
                        output = f"not executed: {exc}"
                    except CancelledError as exc:
                        cancelled = exc
                        error = True
                        output = str(exc)
                    except Exception as exc:
                        error = True
                        output = str(exc)
                    except BaseException as exc:
                        interrupted = exc
                        error = True
                        output = f"tool interrupted by {type(exc).__name__}"
                output = bound_output(output)
                signature = (
                    call.name,
                    self._canonical_arguments(call.input),
                    error,
                    output,
                )
                if not call.argument_error:
                    tool_signatures.append(signature)
                    cycle_length = self._repeated_cycle_length(tool_signatures)
                    if cycle_length:
                        if cycle_length == 1:
                            notice = (
                                " HAL stopped this turn after three identical "
                                f"calls to {call.name!r} with the same result."
                            )
                            message = f"repeated identical tool call {call.name!r}"
                        else:
                            notice = (
                                " HAL stopped this turn after the same sequence of "
                                f"{cycle_length} tool calls repeated three times."
                            )
                            message = (
                                "repeated tool-call cycle of length "
                                f"{cycle_length} ending at {call.name!r}"
                            )
                        output = bound_output(output + notice)
                        repeated_tool = RepeatedToolCallError(message, partial)
                results.append(ContentBlock("tool_result", tool_use_id=call.id, content=output, is_error=error))
                self._emit(Event(
                    EventKind.TOOL_RESULT, text=output, name=call.name,
                    tool_use_id=call.id, is_error=error,
                    duration_ms=int((time.monotonic() - tool_started) * 1000),
                ))
            self.messages.append(Message("assistant", assistant_blocks))
            self.messages.append(Message("user", results))
            if cancelled is not None:
                self._emit(Event(EventKind.ERROR, text=str(cancelled), error=cancelled))
                raise cancelled
            if interrupted is not None:
                self._emit(Event(
                    EventKind.ERROR, text=str(interrupted), error=interrupted,
                    partial_text=partial,
                ))
                raise interrupted
            if budget_exhausted is not None:
                self._fail(budget_exhausted)
            if repeated_malformed is not None:
                self._fail(repeated_malformed)
            if repeated_tool is not None:
                self._fail(repeated_tool)
        partial = "\n".join(final_parts).strip()
        error = MaxTurnsError(
            f"agent exceeded maximum of {self.max_turns} provider turns", partial
        )
        self._emit(Event(
            EventKind.MAX_TURNS_REACHED, max_turns=self.max_turns,
            error=error, partial_text=partial,
        ))
        raise error

    def _before_provider_call(self, partial: str) -> None:
        self._check_continuation_budgets(partial)
        budgets = self._active_budgets
        if budgets is not None and budgets.provider_calls is not None:
            if self.run_counters.provider_calls >= budgets.provider_calls:
                raise BudgetExhaustedError(
                    BudgetReason.PROVIDER_CALLS, budgets.provider_calls, partial,
                )
        self.run_counters.provider_calls += 1
        self._emit_budget_update()

    def _before_tool_call(self, partial: str) -> None:
        self._check_continuation_budgets(partial)
        budgets = self._active_budgets
        if budgets is not None and budgets.tool_calls is not None:
            if self.run_counters.tool_calls >= budgets.tool_calls:
                raise BudgetExhaustedError(
                    BudgetReason.TOOL_CALLS, budgets.tool_calls, partial,
                )
        self.run_counters.tool_calls += 1
        self._emit_budget_update()

    def _check_continuation_budgets(self, partial: str) -> None:
        budgets = self._active_budgets
        if budgets is None:
            return
        elapsed = self._update_elapsed()
        checks = (
            (BudgetReason.ELAPSED_SECONDS, elapsed, budgets.elapsed_seconds),
            (BudgetReason.INPUT_TOKENS, self.run_counters.usage.input_tokens, budgets.input_tokens),
            (BudgetReason.OUTPUT_TOKENS, self.run_counters.usage.output_tokens, budgets.output_tokens),
        )
        for reason, value, limit in checks:
            if limit is not None and value >= limit:
                raise BudgetExhaustedError(reason, limit, partial)

    def _update_elapsed(self) -> float:
        if self._send_started:
            self.run_counters.elapsed_seconds = max(
                0.0, time.monotonic() - self._send_started,
            )
        return self.run_counters.elapsed_seconds

    def _emit_budget_update(self) -> None:
        if self._active_budgets is None:
            return
        usage = self.run_counters.usage
        self._emit(Event(
            EventKind.BUDGET_UPDATED,
            provider_calls=self.run_counters.provider_calls,
            tool_calls=self.run_counters.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ))

    def _emit(self, event: Event) -> None:
        if not event.capability:
            event.capability = self._active_capability
        if self._send_started:
            event.elapsed_ms = int((time.monotonic() - self._send_started) * 1000)
        try:
            self.on_event(event)
        except Exception as exc:
            # Presentation and logging are observers. Their failure must not alter
            # tool execution or transcript commit semantics.
            self.event_errors.append(exc)

    def _emit_delta(self, delta: StreamDelta) -> None:
        kind = (
            EventKind.ASSISTANT_COMMENTARY
            if delta.kind == "commentary" else EventKind.ASSISTANT_TEXT
        )
        self._emit(Event(kind, text=delta.text))

    def _append_safe_assistant(self, blocks: list[ContentBlock]) -> None:
        safe = [block for block in blocks if block.type != "tool_use"]
        if safe:
            self.messages.append(Message("assistant", safe))

    def _append_streamed_partial(self, blocks: list[ContentBlock]) -> None:
        if blocks:
            self.messages.append(Message("assistant", blocks))

    @staticmethod
    def _partial_from(final_parts: list[str], streamed_blocks: list[ContentBlock]) -> str:
        parts = list(final_parts)
        parts.extend(
            block.text for block in streamed_blocks
            if block.type == "text" and block.text
        )
        return "\n".join(parts).strip()

    def _validate_response(self, stop_reason: str, calls: list[ContentBlock],
                           partial: str) -> None:
        if stop_reason == "tool_use" and not calls:
            self._fail(ProviderProtocolError(
                "provider stopped for tool use without returning a tool call", partial,
            ))
        seen: set[str] = set()
        for call in calls:
            if not call.id:
                self._fail(ProviderProtocolError(
                    "provider returned a tool call without an id", partial,
                ))
            if call.id in seen:
                self._fail(ProviderProtocolError(
                    f"provider returned duplicate tool call id {call.id!r}", partial,
                ))
            if not call.name:
                self._fail(ProviderProtocolError(
                    f"provider returned tool call {call.id!r} without a name", partial,
                ))
            seen.add(call.id)

    @staticmethod
    def _canonical_arguments(arguments: dict[str, object]) -> str:
        """Return a stable signature even for extension-provided non-JSON values."""
        return json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            default=lambda value: f"<{type(value).__module__}.{type(value).__qualname__}:{value!r}>",
        )

    @staticmethod
    def _repeated_cycle_length(
        signatures: list[tuple[str, str, bool, str]], max_cycle: int = 4,
    ) -> int:
        """Detect three consecutive copies of a short tool/result sequence."""
        for length in range(1, min(max_cycle, len(signatures) // 3) + 1):
            tail = signatures[-length:]
            if signatures[-2 * length:-length] == tail and signatures[-3 * length:-2 * length] == tail:
                return length
        return 0

    def _fail(self, error: AgentError) -> NoReturn:
        self._emit(Event(
            EventKind.ERROR, text=str(error), error=error,
            partial_text=error.partial_text,
            reason=(
                error.reason_code
                if isinstance(error, BudgetExhaustedError) else ""
            ),
        ))
        raise error
