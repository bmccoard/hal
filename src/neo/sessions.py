from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import Message, Usage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Metadata:
    id: str = ""
    title: str = ""
    cwd: str = ""
    model: str = ""
    provider: str = ""
    openai_auth: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Session:
    metadata: Metadata
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class SessionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path.home() / ".neo" / "sessions"

    def create(self, metadata: Metadata) -> Session:
        now = _now(); metadata.id = metadata.id or f"sess_{secrets.token_hex(8)}"
        metadata.created_at = metadata.created_at or now; metadata.updated_at = metadata.updated_at or now
        session = Session(metadata); self.save(session); return session

    def load(self, session_id: str) -> Session:
        if not session_id.strip() or "/" in session_id or "\\" in session_id:
            raise ValueError(f"invalid session id {session_id!r}")
        path = self.directory / f"{session_id}.json"
        if not path.is_file(): raise FileNotFoundError(f"session not found: {session_id}")
        data = json.loads(path.read_text(encoding="utf-8")); meta = data.get("metadata") or {}
        return Session(Metadata(**{k: v for k, v in meta.items() if k in Metadata.__dataclass_fields__}), [Message.from_dict(x) for x in data.get("messages", [])], Usage(**data.get("usage", {})))

    def save(self, session: Session) -> None:
        meta = session.metadata; meta.id = meta.id or f"sess_{secrets.token_hex(8)}"; meta.created_at = meta.created_at or _now(); meta.updated_at = _now()
        if not meta.title: meta.title = title_from_messages(session.messages)
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"metadata": asdict(meta), "messages": [x.to_dict() for x in session.messages], "usage": asdict(session.usage)}
        self._atomic(self.directory / f"{meta.id}.json", payload)
        index = self._read_index(); items = index.setdefault("sessions", [])
        for idx, item in enumerate(items):
            if item.get("id") == meta.id: items[idx] = asdict(meta); break
        else: items.append(asdict(meta))
        self._atomic(self.directory / "index.json", index)

    def list(self) -> list[Metadata]:
        items = self._read_index().get("sessions", [])
        return sorted((Metadata(**{k: v for k, v in x.items() if k in Metadata.__dataclass_fields__}) for x in items), key=lambda x: x.updated_at, reverse=True)

    def search(self, query: str) -> list[tuple[Metadata, str]]:
        needle = query.strip().lower()
        if not needle: raise ValueError("search query is empty")
        results = []
        for meta in self.list():
            try: session = self.load(meta.id)
            except (OSError, ValueError, json.JSONDecodeError): continue
            text = transcript_text(session.messages); index = text.lower().find(needle)
            if index >= 0:
                start, end = max(0, index - 48), min(len(text), index + len(needle) + 48)
                excerpt = " ".join(text[start:end].split())
                results.append((session.metadata, ("..." if start else "") + excerpt + ("..." if end < len(text) else "")))
        return results

    def _read_index(self) -> dict:
        path = self.directory / "index.json"
        if not path.is_file(): return {"sessions": []}
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic(path: Path, value: dict) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600); os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name): os.unlink(temp_name)


def title_from_messages(messages: list[Message]) -> str:
    for message in messages:
        if message.role != "user": continue
        text = message.display_text or next((x.text for x in message.content if x.type == "text" and x.text.strip()), "")
        if text:
            clean = " ".join(text.split()); return clean if len(clean) <= 80 else clean[:79].rstrip() + "…"
    return ""


def transcript_text(messages: list[Message]) -> str:
    parts = []
    for message in messages:
        if message.role == "user" and message.display_text: parts.append(message.display_text)
        else: parts.extend(x.text for x in message.content if x.type == "text")
        parts.extend(x.content for x in message.content if x.type == "tool_result")
    return "\n".join(parts)
