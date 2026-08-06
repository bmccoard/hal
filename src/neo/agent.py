from __future__ import annotations

from collections.abc import Callable

from .models import ContentBlock, Message, Request, Usage
from .providers import Provider
from .tools import MAX_RESULT, Registry


class AgentError(RuntimeError):
    pass


EventHandler = Callable[[str, dict[str, object]], None]


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
                model=self.model, system=self.system, messages=self.messages,
                tools=self.tools.specs,
            ))
            self.usage.add(response.usage)
            assistant_blocks = response.content
            for block in assistant_blocks:
                if block.type == "text" and block.text:
                    final_parts.append(block.text)
                    self.on_event("assistant", {"text": block.text})
            calls = [block for block in assistant_blocks if block.type == "tool_use"]
            self.messages.append(Message("assistant", assistant_blocks))
            if not calls:
                if response.stop_reason in {"max_tokens", "length"}:
                    raise AgentError("provider response was truncated at the token limit")
                self.on_event("done", {})
                return "".join(final_parts).strip()
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
            self.messages.append(Message("user", results))
        raise AgentError(f"agent exceeded maximum of {self.max_turns} provider turns")
