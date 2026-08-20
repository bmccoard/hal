from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hal.extensions import ExtensionContext
from hal_database import create_tools


def database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL);
            CREATE INDEX people_name ON people(name);
            INSERT INTO people(name, score) VALUES ('Ada', 9.5), ('Grace', 9.0), ('Linus', 8.5);
            CREATE VIEW high_scores AS SELECT name, score FROM people WHERE score >= 9;
        """)


def tools(tmp_path: Path, **settings):
    path = tmp_path / "work.db"
    database(path)
    config = {"connections": {"work": {"driver": "sqlite", "path": str(path), "read_only": True}}}
    config.update(settings)
    context = ExtensionContext("database", tmp_path, tmp_path, config)
    return {tool.spec.name: tool for tool in create_tools(context)}, path


def test_factory_and_schema(tmp_path: Path) -> None:
    available, _ = tools(tmp_path)
    assert set(available) == {"db_schema", "db_query", "db_explain"}
    result = json.loads(available["db_schema"].run({"connection": "work"}))
    people = next(item for item in result["objects"] if item["name"] == "people")
    assert [column["name"] for column in people["columns"]] == ["id", "name", "score"]
    assert people["indexes"][0]["name"] == "people_name"


def test_query_uses_parameters_and_bounds_rows(tmp_path: Path) -> None:
    available, _ = tools(tmp_path, max_rows=2)
    result = json.loads(available["db_query"].run({
        "connection": "work", "sql": "SELECT name FROM people WHERE score >= :score ORDER BY score DESC",
        "parameters": {"score": 8},
    }))
    assert result["columns"] == ["name"]
    assert result["rows"] == [["Ada"], ["Grace"]]
    assert result["truncated"] is True


@pytest.mark.parametrize("sql", [
    "DELETE FROM people", "UPDATE people SET score = 0", "CREATE TABLE bad (id INTEGER)",
    "ATTACH DATABASE ':memory:' AS other", "PRAGMA user_version = 2",
])
def test_query_rejects_mutation(tmp_path: Path, sql: str) -> None:
    available, path = tools(tmp_path)
    with pytest.raises((sqlite3.DatabaseError, PermissionError)):
        available["db_query"].run({"connection": "work", "sql": sql})
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM people").fetchone()[0] == 3


def test_query_rejects_multiple_statements(tmp_path: Path) -> None:
    available, _ = tools(tmp_path)
    with pytest.raises(sqlite3.ProgrammingError, match="one statement"):
        available["db_query"].run({"connection": "work", "sql": "SELECT 1; SELECT 2"})


def test_explain_returns_plan_without_data(tmp_path: Path) -> None:
    available, _ = tools(tmp_path)
    result = json.loads(available["db_explain"].run({
        "connection": "work", "sql": "SELECT * FROM people WHERE name = ?", "parameters": ["Ada"],
    }))
    assert result["columns"] == ["id", "parent", "notused", "detail"]
    assert "people_name" in result["rows"][0][3]


def test_configuration_fails_closed(tmp_path: Path) -> None:
    context = ExtensionContext("database", tmp_path, tmp_path, {
        "connections": {"work": {"path": "missing.db", "read_only": True}},
    })
    with pytest.raises(ValueError, match="does not exist"):
        create_tools(context)

    path = tmp_path / "work.db"
    database(path)
    context = ExtensionContext("database", tmp_path, tmp_path, {
        "connections": {"work": {"path": str(path), "read_only": False}},
    })
    with pytest.raises(ValueError, match="read_only must be true"):
        create_tools(context)
