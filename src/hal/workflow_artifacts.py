"""Content-addressed storage for typed workflow artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import tempfile


DEFAULT_INLINE_LIMIT = 16 * 1024
DEFAULT_ARTIFACT_LIMIT = 64 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


@dataclass(frozen=True, slots=True)
class WorkflowArtifact:
    type: str
    producer: str
    digest: str
    size: int
    media_type: str
    location: str | None = None
    inline: bytes | None = None

    def __post_init__(self) -> None:
        if not self.type or not _IDENTIFIER.fullmatch(self.producer):
            raise ValueError("artifact type and producer must be valid identifiers")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("artifact digest must be lowercase SHA-256")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("artifact size must be a non-negative integer")
        if not self.media_type.strip():
            raise ValueError("artifact media type must not be empty")
        if (self.location is None) == (self.inline is None):
            raise ValueError("artifact must have exactly one storage location or inline value")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactHandle:
    """A bounded downstream reference that does not expose artifact content or paths."""

    artifact: WorkflowArtifact

    def summary(self) -> dict[str, str | int]:
        return {
            "type": self.artifact.type,
            "producer": self.artifact.producer,
            "digest": self.artifact.digest,
            "size": self.artifact.size,
            "media_type": self.artifact.media_type,
        }


class WorkflowArtifactStore:
    def __init__(
        self,
        directory: Path,
        *,
        inline_limit: int = DEFAULT_INLINE_LIMIT,
        artifact_limit: int = DEFAULT_ARTIFACT_LIMIT,
    ) -> None:
        self.directory = directory.resolve()
        if inline_limit < 0 or artifact_limit <= 0 or inline_limit > artifact_limit:
            raise ValueError("artifact limits must satisfy 0 <= inline <= artifact")
        self.inline_limit = inline_limit
        self.artifact_limit = artifact_limit

    def put(
        self,
        content: bytes | str,
        *,
        type: str,
        producer: str,
        media_type: str = "application/octet-stream",
        inline: bool = False,
    ) -> WorkflowArtifact:
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(data) > self.artifact_limit:
            raise ValueError(f"artifact exceeds {self.artifact_limit} bytes")
        digest = sha256(data).hexdigest()
        if inline:
            if len(data) > self.inline_limit:
                raise ValueError(f"inline artifact exceeds {self.inline_limit} bytes")
            return WorkflowArtifact(type, producer, digest, len(data), media_type, inline=data)
        relative = f"objects/{digest[:2]}/{digest}"
        target = self._resolve_location(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._verify_file(target, digest, len(data))
        else:
            self._atomic_write(target, data)
        return WorkflowArtifact(
            type, producer, digest, len(data), media_type, location=relative,
        )

    def read(self, artifact: WorkflowArtifact) -> bytes:
        self.validate(artifact)
        if artifact.inline is not None:
            return artifact.inline
        assert artifact.location is not None
        return self._resolve_location(artifact.location).read_bytes()

    def validate(self, artifact: WorkflowArtifact) -> None:
        if artifact.size > self.artifact_limit:
            raise ValueError("artifact metadata exceeds configured size limit")
        if artifact.inline is not None:
            if artifact.size > self.inline_limit or len(artifact.inline) > self.inline_limit:
                raise ValueError("inline artifact exceeds configured limit")
            self._verify_bytes(artifact.inline, artifact.digest, artifact.size)
            return
        assert artifact.location is not None
        target = self._resolve_location(artifact.location)
        if not target.is_file():
            raise FileNotFoundError(f"artifact content is missing: {artifact.location}")
        self._verify_file(target, artifact.digest, artifact.size)

    def _resolve_location(self, location: str) -> Path:
        relative = PurePosixPath(location)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("objects",):
            raise ValueError("artifact location must stay inside the object store")
        target = (self.directory / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self.directory)
        except ValueError as exc:
            raise ValueError("artifact location escaped the object store") from exc
        return target

    @staticmethod
    def _verify_bytes(data: bytes, digest: str, size: int) -> None:
        if len(data) != size:
            raise ValueError("artifact size does not match metadata")
        if sha256(data).hexdigest() != digest:
            raise ValueError("artifact digest does not match content")

    def _verify_file(self, path: Path, digest: str, size: int) -> None:
        if path.stat().st_size != size:
            raise ValueError("artifact size does not match stored content")
        hasher = sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError("artifact digest does not match stored content")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
