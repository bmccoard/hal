from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from .models import ContentBlock, Message, Request, Usage
from .providers import Provider
from .tools import MAX_RESULT, Registry


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


EventHandler = Callable[[str, dict[str, object]], None]

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
        self.on_event = on_event or (lambda _kind, _data: None)

    def send(self, text: str, display_text: str = "") -> str:
        if not text.strip():
            raise AgentError("message is empty")
        self.messages.append(Message("user", [ContentBlock("text", text=text)], display_text=display_text))
        final_parts: list[str] = []
        for _ in range(self.max_turns):
            response = self.provider.complete(Request(
                model=self.model, system=self.system, messages=list(self.messages),
                tools=self.tools.specs,
            ))
            self.usage.add(response.usage)
            assistant_blocks = response.content
            response_text = "".join(
                block.text for block in assistant_blocks
                if block.type == "text" and block.text
            )
            if response_text:
                final_parts.append(response_text)
                self.on_event("assistant", {"text": response_text})
            partial = "\n".join(final_parts).strip()
            calls = [block for block in assistant_blocks if block.type == "tool_use"]

            if response.stop_reason not in _KNOWN_STOP_REASONS:
                self._append_safe_assistant(assistant_blocks)
                self._fail(UnexpectedStopReasonError(
                    f"unexpected provider stop reason: {response.stop_reason!r}", partial
                ))

            if response.stop_reason == "refusal":
                self._append_safe_assistant(assistant_blocks)
                self.on_event("done", {})
                return partial

            if response.stop_reason == "model_context_window_exceeded":
                self._append_safe_assistant(assistant_blocks)
                self._fail(ContextWindowExceededError(
                    "provider context window was exceeded", partial
                ))

            if not calls:
                self.messages.append(Message("assistant", assistant_blocks))
                if response.stop_reason in {"", "end_turn", "stop_sequence"}:
                    self.on_event("done", {})
                    return partial
                if response.stop_reason in {"max_tokens", "length"}:
                    self._fail(MaxOutputTokensError(
                        "provider response was truncated at the token limit", partial
                    ))
                # pause_turn explicitly asks for another provider response. A
                # tool_use stop without calls is also replayed, matching Neo's
                # provider-neutral loop instead of silently treating it as done.
                continue

            # Do not append the assistant tool request until every call has a
            # matching result. If execution is interrupted by a BaseException,
            # neither side is committed and the stored transcript stays valid.
            results: list[ContentBlock] = []
            for call in calls:
                self.on_event("tool_call", {"name": call.name, "input": call.input})
                error = False
                try:
                    output = self.tools.run(call.name, call.input)
                except Exception as exc:
                    error = True
                    output = str(exc)
                output = output.encode("utf-8", "replace")[:MAX_RESULT].decode("utf-8", "replace")
                results.append(ContentBlock("tool_result", tool_use_id=call.id, content=output, is_error=error))
                self.on_event("tool_result", {"name": call.name, "content": output, "is_error": error})
            self.messages.append(Message("assistant", assistant_blocks))
            self.messages.append(Message("user", results))
        partial = "\n".join(final_parts).strip()
        error = MaxTurnsError(
            f"agent exceeded maximum of {self.max_turns} provider turns", partial
        )
        self.on_event("max_turns_reached", {"max_turns": self.max_turns, "error": error})
        raise error

    def _append_safe_assistant(self, blocks: list[ContentBlock]) -> None:
        safe = [block for block in blocks if block.type != "tool_use"]
        if safe:
            self.messages.append(Message("assistant", safe))

    def _fail(self, error: AgentError) -> NoReturn:
        self.on_event("error", {
            "error": error,
            "message": str(error),
            "partial_text": error.partial_text,
        })
        raise error
