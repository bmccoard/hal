from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from hal.workflow_artifacts import WorkflowArtifact, WorkflowArtifactStore


def test_artifact_store_writes_content_addressed_objects_atomically(tmp_path: Path) -> None:
    store = WorkflowArtifactStore(tmp_path / "artifacts")

    first = store.put(
        "hello", type="markdown", producer="plan", media_type="text/markdown",
    )
    second = store.put(
        b"hello", type="markdown", producer="plan", media_type="text/markdown",
    )

    assert first == second
    assert first.digest == sha256(b"hello").hexdigest()
    assert first.location == f"objects/{first.digest[:2]}/{first.digest}"
    assert store.read(first) == b"hello"
    assert len(tuple((tmp_path / "artifacts" / "objects").rglob(first.digest))) == 1


def test_inline_artifacts_are_bounded_and_digest_checked(tmp_path: Path) -> None:
    store = WorkflowArtifactStore(tmp_path, inline_limit=5, artifact_limit=10)
    artifact = store.put(b"hello", type="string", producer="node", inline=True)

    assert artifact.location is None
    assert store.read(artifact) == b"hello"
    with pytest.raises(ValueError, match="inline artifact exceeds"):
        store.put(b"123456", type="string", producer="node", inline=True)
    with pytest.raises(ValueError, match="digest"):
        store.validate(replace(artifact, inline=b"jello"))


def test_artifact_validation_rejects_missing_stale_and_escaped_content(tmp_path: Path) -> None:
    store = WorkflowArtifactStore(tmp_path / "store")
    artifact = store.put(b"original", type="diff", producer="review")
    assert artifact.location is not None
    path = store.directory / artifact.location

    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        store.read(artifact)
    path.unlink()
    with pytest.raises(FileNotFoundError, match="missing"):
        store.read(artifact)

    escaped = WorkflowArtifact(
        "diff", "review", sha256(b"x").hexdigest(), 1,
        "text/plain", location="objects/../../outside",
    )
    with pytest.raises(ValueError, match="inside"):
        store.validate(escaped)


def test_artifact_metadata_rejects_ambiguous_storage_and_oversize(tmp_path: Path) -> None:
    digest = sha256(b"x").hexdigest()
    with pytest.raises(ValueError, match="exactly one"):
        WorkflowArtifact("string", "node", digest, 1, "text/plain")
    with pytest.raises(ValueError, match="exactly one"):
        WorkflowArtifact(
            "string", "node", digest, 1, "text/plain",
            location="objects/x", inline=b"x",
        )
    store = WorkflowArtifactStore(tmp_path, inline_limit=4, artifact_limit=4)
    with pytest.raises(ValueError, match="exceeds"):
        store.put(b"12345", type="string", producer="node")
