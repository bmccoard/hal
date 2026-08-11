from pathlib import Path

from hal.models import ContentBlock, Message
import pytest

from hal.sessions import Metadata, SessionStore, short_session_id


def test_session_round_trip_and_search(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(Metadata(cwd=str(tmp_path), model="test", provider="fake"))
    session.messages.append(Message("user", [ContentBlock("text", text="Find the parser bug")]))
    store.save(session)
    loaded = store.load(session.metadata.id)
    assert loaded.metadata.title == "Find the parser bug"
    assert store.search("PARSER")[0][0].id == session.metadata.id

def test_short_session_selectors_are_stable_and_resolve_unique_prefixes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.create(Metadata(id="sess_ae5f63c2dd8b4abd", cwd=str(tmp_path)))
    store.create(Metadata(id="sess_cdfc4daea3897dd4", cwd=str(tmp_path)))

    assert short_session_id(first.metadata.id) == "ae5f63c2"
    assert store.resolve_id("ae5f") == first.metadata.id
    assert store.load("ae5f63c2").metadata.id == first.metadata.id


def test_ambiguous_short_session_selector_requests_more_characters(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create(Metadata(id="sess_abcd111111111111", cwd=str(tmp_path)))
    store.create(Metadata(id="sess_abcd222222222222", cwd=str(tmp_path)))

    with pytest.raises(ValueError, match="ambiguous session selector"):
        store.resolve_id("abcd")
