from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Role = Literal["user", "assistant", "tool"]
DeltaKind = Literal["text", "commentary"]
REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


@dataclass(slots=True)
class StreamDelta:
    """A provider-neutral incremental display update."""

    kind: DeltaKind
    text: str


@dataclass(slots=True)
class ContentBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    argument_error: str = ""
    tool_use_id: str = ""
    content: str = ""
    is_error: bool = False
    raw: Any = None
    source: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", False, None, {}, [])}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContentBlock":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in value.items() if k in allowed})


@dataclass(slots=True)
class Message:
    role: Role
    content: list[ContentBlock]
    display_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "content": [block.to_dict() for block in self.content],
        }
        if self.display_text:
            result["display_text"] = self.display_text
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Message":
        return cls(
            role=value.get("role", "user"),
            content=[ContentBlock.from_dict(x) for x in value.get("content", [])],
            display_text=value.get("display_text", ""),
        )


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens


@dataclass(slots=True)
class Request:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec]
    max_tokens: int = 8192
    reasoning_effort: str = ""


@dataclass(slots=True)
class Response:
    content: list[ContentBlock]
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
