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
        if directory is not None:
            self.directory = directory
            self.legacy_directory: Path | None = None
        else:
            self.directory = Path.home() / ".hal" / "sessions"
            self.legacy_directory = Path.home() / ".neo" / "sessions"

    def create(self, metadata: Metadata) -> Session:
        now = _now(); metadata.id = metadata.id or f"sess_{secrets.token_hex(8)}"
        metadata.created_at = metadata.created_at or now; metadata.updated_at = metadata.updated_at or now
        session = Session(metadata); self.save(session); return session

    def load(self, selector: str) -> Session:
        selector = selector.strip()
        if not selector or "/" in selector or "\\" in selector:
            raise ValueError(f"invalid session selector {selector!r}")
        session_id = selector
        path = self._find_session_path(session_id)
        if path is None:
            session_id = self.resolve_id(selector)
            path = self._find_session_path(session_id)
        if path is None: raise FileNotFoundError(f"session not found: {selector}")
        data = json.loads(path.read_text(encoding="utf-8")); meta = data.get("metadata") or {}
        return Session(Metadata(**{k: v for k, v in meta.items() if k in Metadata.__dataclass_fields__}), [Message.from_dict(x) for x in data.get("messages", [])], Usage(**data.get("usage", {})))

    def resolve_id(self, selector: str) -> str:
        """Resolve a full ID or a unique prefix without relying on list order."""
        value = selector.strip()
        if not value or "/" in value or "\\" in value:
            raise ValueError(f"invalid session selector {selector!r}")
        short = value.removeprefix("sess_")
        if len(short) < 4:
            raise ValueError("session selector must contain at least 4 ID characters")
        matches = [
            item.id for item in self.list()
            if item.id == value or item.id.removeprefix("sess_").startswith(short)
        ]
        if not matches:
            raise FileNotFoundError(f"session not found: {selector}")
        if len(matches) > 1:
            choices = ", ".join(short_session_id(item) for item in matches[:5])
            raise ValueError(f"ambiguous session selector {selector!r}; use more characters ({choices})")
        return matches[0]

    def save(self, session: Session) -> None:
        meta = session.metadata; meta.id = meta.id or f"sess_{secrets.token_hex(8)}"; meta.created_at = meta.created_at or _now(); meta.updated_at = _now()
        if not meta.title: meta.title = title_from_messages(session.messages)
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"metadata": asdict(meta), "messages": [x.to_dict() for x in session.messages], "usage": asdict(session.usage)}
        self._atomic(self.directory / f"{meta.id}.json", payload)
        index = self._read_index(self.directory); items = index.setdefault("sessions", [])
        for idx, item in enumerate(items):
            if item.get("id") == meta.id: items[idx] = asdict(meta); break
        else: items.append(asdict(meta))
        self._atomic(self.directory / "index.json", index)

    def list(self) -> list[Metadata]:
        by_id: dict[str, Metadata] = {}
        directories = ([self.legacy_directory] if self.legacy_directory is not None else []) + [self.directory]
        for directory in directories:
            for item in self._read_index(directory).get("sessions", []):
                metadata = Metadata(**{
                    k: v for k, v in item.items() if k in Metadata.__dataclass_fields__
                })
                by_id[metadata.id] = metadata
        return sorted(by_id.values(), key=lambda x: x.updated_at, reverse=True)

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

    def _read_index(self, directory: Path | None = None) -> dict:
        path = (directory or self.directory) / "index.json"
        if not path.is_file(): return {"sessions": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def _find_session_path(self, session_id: str) -> Path | None:
        path = self.directory / f"{session_id}.json"
        if path.is_file():
            return path
        if self.legacy_directory is not None:
            legacy_path = self.legacy_directory / f"{session_id}.json"
            if legacy_path.is_file():
                return legacy_path
        return None

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


def short_session_id(session_id: str, length: int = 8) -> str:
    """Return the stable, human-sized portion used as a session selector."""
    return session_id.removeprefix("sess_")[:length]


def transcript_text(messages: list[Message]) -> str:
    parts = []
    for message in messages:
        if message.role == "user" and message.display_text: parts.append(message.display_text)
        else: parts.extend(x.text for x in message.content if x.type == "text")
        parts.extend(x.content for x in message.content if x.type == "tool_result")
    return "\n".join(parts)
