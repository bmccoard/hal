from pathlib import Path

from neo.models import ContentBlock, Message
from neo.sessions import Metadata, SessionStore


def test_session_round_trip_and_search(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(Metadata(cwd=str(tmp_path), model="test", provider="fake"))
    session.messages.append(Message("user", [ContentBlock("text", text="Find the parser bug")]))
    store.save(session)
    loaded = store.load(session.metadata.id)
    assert loaded.metadata.title == "Find the parser bug"
    assert store.search("PARSER")[0][0].id == session.metadata.id

