from pathlib import Path

from hal.models import ContentBlock, Message
from hal.sessions import Metadata, SessionStore


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
