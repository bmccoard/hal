from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import NoReturn

from .cancellation import CancelledError, CancellationToken, cancellation_or_default
from .models import ContentBlock, Message, Request, Usage
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


EventHandler = Callable[[Event], None]

_KNOWN_STOP_REASONS = {
    "", "end_turn", "stop_sequence", "tool_use", "max_tokens", "length",
    "refusal", "pause_turn", "model_context_window_exceeded",
}


class Agent:
    """Provider-neutral model/tool loop with a serial, structurally valid transcript."""

    def __init__(self, provider: Provider, model: str, system: str, tools: Registry,
                 messages: list[Message] | None = None, usage: Usage | None = None,
                 max_turns: int = 500, on_event: EventHandler | None = None) -> None:
        self.provider = provider
        self.model = model
        self.system = system
        self.tools = tools
        self.messages = list(messages or [])
        self.usage = usage or Usage()
        self.max_turns = max_turns
        self.on_event = on_event or (lambda _event: None)
        self._send_started = 0.0

    def send(self, text: str, display_text: str = "",
             cancellation: CancellationToken | None = None) -> str:
        if not text.strip():
            raise AgentError("message is empty")
        cancellation = cancellation_or_default(cancellation)
        self._send_started = time.monotonic()
        self.messages.append(Message("user", [ContentBlock("text", text=text)], display_text=display_text))
        final_parts: list[str] = []
        for _ in range(self.max_turns):
            try:
                cancellation.raise_if_cancelled()
                response = self.provider.complete(Request(
                    model=self.model, system=self.system, messages=list(self.messages),
                    tools=self.tools.specs,
                ), cancellation)
                cancellation.raise_if_cancelled()
            except Exception as exc:
                self._emit(Event(EventKind.ERROR, error=exc))
                raise
            self.usage.add(response.usage)
            assistant_blocks = response.content
            for block in assistant_blocks:
                if not block.text:
                    continue
                if block.type == "text":
                    final_parts.append(block.text)
                    self._emit(Event(EventKind.ASSISTANT_TEXT, text=block.text))
                elif block.type == "commentary":
                    self._emit(Event(EventKind.ASSISTANT_COMMENTARY, text=block.text))
            partial = "\n".join(final_parts).strip()
            calls = [block for block in assistant_blocks if block.type == "tool_use"]

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
                self.messages.append(Message("assistant", assistant_blocks))
                if response.stop_reason in {"", "end_turn", "stop_sequence"}:
                    self._emit(Event(EventKind.DONE))
                    return partial
                if response.stop_reason in {"max_tokens", "length"}:
                    self._fail(MaxOutputTokensError(
                        "provider response was truncated at the token limit", partial
                    ))
                # pause_turn explicitly asks for another provider response. A
                # tool_use stop without calls is also replayed, matching HAL's
                # provider-neutral loop instead of silently treating it as done.
                continue

            # Do not append the assistant tool request until every call has a
            # matching result. If execution is interrupted by a BaseException,
            # neither side is committed and the stored transcript stays valid.
            results: list[ContentBlock] = []
            cancelled: CancelledError | None = None
            for call in calls:
                self._emit(Event(
                    EventKind.TOOL_CALL, name=call.name, args=dict(call.input),
                    tool_use_id=call.id,
                ))
                tool_started = time.monotonic()
                error = cancelled is not None
                if cancelled is not None:
                    output = "skipped because the agent turn was cancelled"
                else:
                    try:
                        cancellation.raise_if_cancelled()
                        output = self.tools.run(call.name, call.input, cancellation)
                    except CancelledError as exc:
                        cancelled = exc
                        error = True
                        output = str(exc)
                    except Exception as exc:
                        error = True
                        output = str(exc)
                output = bound_output(output)
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
        partial = "\n".join(final_parts).strip()
        error = MaxTurnsError(
            f"agent exceeded maximum of {self.max_turns} provider turns", partial
        )
        self._emit(Event(
            EventKind.MAX_TURNS_REACHED, max_turns=self.max_turns,
            error=error, partial_text=partial,
        ))
        raise error

    def _emit(self, event: Event) -> None:
        if self._send_started:
            event.elapsed_ms = int((time.monotonic() - self._send_started) * 1000)
        self.on_event(event)

    def _append_safe_assistant(self, blocks: list[ContentBlock]) -> None:
        safe = [block for block in blocks if block.type != "tool_use"]
        if safe:
            self.messages.append(Message("assistant", safe))

    def _fail(self, error: AgentError) -> NoReturn:
        self._emit(Event(
            EventKind.ERROR, text=str(error), error=error,
            partial_text=error.partial_text,
        ))
        raise error
