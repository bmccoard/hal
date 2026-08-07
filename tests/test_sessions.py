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


def test_default_store_reads_legacy_sessions_and_saves_to_hal(
    tmp_path: Path, monkeypatch,
) -> None:
    home = tmp_path / "home"
    legacy_store = SessionStore(home / ".neo" / "sessions")
    legacy = legacy_store.create(Metadata(cwd=str(tmp_path), model="old", provider="fake"))
    legacy.messages.append(Message("user", [ContentBlock("text", text="Legacy session")]))
    legacy_store.save(legacy)
    monkeypatch.setattr("hal.sessions.Path.home", lambda: home)

    store = SessionStore()
    loaded = store.load(legacy.metadata.id)
    store.save(loaded)

    assert loaded.messages[0].content[0].text == "Legacy session"
    assert (home / ".hal" / "sessions" / f"{legacy.metadata.id}.json").is_file()
    assert [item.id for item in store.list()].count(legacy.metadata.id) == 1


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
