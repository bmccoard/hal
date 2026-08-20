"""Safe, bounded SQLite inspection tools built into HAL."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .cancellation import CancellationToken, cancellation_or_default
from .models import ToolSpec
from .tools import Tool, ToolEffect


_DENIED_ACTIONS = frozenset(
    value for value in (
        getattr(sqlite3, name, None)
        for name in (
            "SQLITE_ALTER_TABLE", "SQLITE_ANALYZE", "SQLITE_ATTACH",
            "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE", "SQLITE_DELETE", "SQLITE_DETACH",
            "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE",
            "SQLITE_INSERT", "SQLITE_PRAGMA", "SQLITE_REINDEX", "SQLITE_TRANSACTION",
            "SQLITE_UPDATE",
        )
    ) if value is not None
)


@dataclass(frozen=True, slots=True)
class SQLiteConnection:
    name: str
    path: Path
    extensions: tuple[Path, ...]
    max_rows: int
    timeout_ms: int


def _positive_int(value: Any, path: str, default: int, maximum: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"{path} must be an integer between 1 and {maximum}")
    return value


def _connections(settings: dict[str, Any], cwd: Path) -> dict[str, SQLiteConnection]:
    unknown = set(settings) - {"connections", "max_rows", "timeout_ms"}
    if unknown:
        raise ValueError(f"unknown database setting(s): {', '.join(sorted(unknown))}")
    raw_connections = settings.get("connections")
    if not isinstance(raw_connections, dict) or not raw_connections:
        raise ValueError("database.connections must be a non-empty mapping")
    default_rows = _positive_int(settings.get("max_rows"), "database.max_rows", 200, 10_000)
    default_timeout = _positive_int(
        settings.get("timeout_ms"), "database.timeout_ms", 5_000, 300_000,
    )
    parsed: dict[str, SQLiteConnection] = {}
    for raw_name, raw in raw_connections.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw, dict):
            raise ValueError("each database connection must have a non-empty name and mapping")
        unknown_connection = set(raw) - {
            "driver", "path", "read_only", "sqlite_extensions", "max_rows", "timeout_ms",
        }
        if unknown_connection:
            raise ValueError(
                f"unknown database.connections.{name} setting(s): "
                f"{', '.join(sorted(unknown_connection))}"
            )
        if raw.get("driver", "sqlite") != "sqlite":
            raise ValueError(f"database connection {name!r}: only driver 'sqlite' is supported")
        if raw.get("read_only", True) is not True:
            raise ValueError(f"database connection {name!r}: read_only must be true")
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"database connection {name!r}: path must be a non-empty string")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"database connection {name!r}: file does not exist: {path}")
        raw_extensions = raw.get("sqlite_extensions", [])
        if not isinstance(raw_extensions, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_extensions
        ):
            raise ValueError(
                f"database connection {name!r}: sqlite_extensions must be a list of paths"
            )
        extensions: list[Path] = []
        for item in raw_extensions:
            extension = Path(item).expanduser()
            if not extension.is_absolute():
                extension = cwd / extension
            extension = extension.resolve()
            if not extension.is_file():
                raise ValueError(
                    f"database connection {name!r}: SQLite extension does not exist: {extension}"
                )
            extensions.append(extension)
        parsed[name] = SQLiteConnection(
            name, path, tuple(extensions),
            _positive_int(
                raw.get("max_rows"), f"database.connections.{name}.max_rows",
                default_rows, 10_000,
            ),
            _positive_int(
                raw.get("timeout_ms"), f"database.connections.{name}.timeout_ms",
                default_timeout, 300_000,
            ),
        )
    return parsed


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    return value


class SQLiteTools:
    def __init__(self, connections: dict[str, SQLiteConnection]) -> None:
        self.connections = connections

    def selected(self, arguments: dict[str, Any]) -> SQLiteConnection:
        name = arguments.get("connection")
        if not isinstance(name, str) or name not in self.connections:
            available = ", ".join(sorted(self.connections))
            raise ValueError(f"connection must name a configured database; available: {available}")
        return self.connections[name]

    def connect(
        self, config: SQLiteConnection, cancellation: CancellationToken | None,
        schema_access: bool = False,
    ) -> sqlite3.Connection:
        token = cancellation_or_default(cancellation)
        token.raise_if_cancelled()
        uri = f"file:{quote(str(config.path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=config.timeout_ms / 1000)
        try:
            for extension in config.extensions:
                connection.enable_load_extension(True)
                try:
                    connection.load_extension(str(extension))
                finally:
                    connection.enable_load_extension(False)
            connection.execute("PRAGMA query_only = ON")
            deadline = time.monotonic() + config.timeout_ms / 1000

            def progress() -> int:
                try:
                    token.raise_if_cancelled()
                except RuntimeError:
                    return 1
                return int(time.monotonic() >= deadline)

            def authorize(
                action: int, one: str | None, _two: str | None,
                _database: str | None, _trigger: str | None,
            ) -> int:
                if (
                    schema_access and action == getattr(sqlite3, "SQLITE_PRAGMA", -1)
                    and one in {"table_info", "index_list"}
                ):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK

            connection.set_progress_handler(progress, 1_000)
            connection.set_authorizer(authorize)
            return connection
        except BaseException:
            connection.close()
            raise


def _connection_property(connections: dict[str, SQLiteConnection]) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": sorted(connections),
        "description": "Configured database connection name.",
    }


class SchemaTool(Tool):
    parallel_safe = True
    effect = ToolEffect.READ_ONLY

    def __init__(self, backend: SQLiteTools) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "db_schema",
            "Inspect tables, views, columns, and indexes in a configured read-only SQLite database.",
            {
                "type": "object",
                "properties": {"connection": _connection_property(self.backend.connections)},
                "required": ["connection"],
            },
        )

    def run(
        self, arguments: dict[str, Any], cancellation: CancellationToken | None = None,
    ) -> str:
        config = self.backend.selected(arguments)
        with closing(self.backend.connect(config, cancellation, schema_access=True)) as connection:
            objects = connection.execute(
                "SELECT name, type, sql FROM sqlite_schema "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            result = []
            for name, kind, sql in objects:
                columns = [
                    {"name": row[1], "type": row[2], "not_null": bool(row[3]),
                     "default": row[4], "primary_key": bool(row[5])}
                    for row in connection.execute("SELECT * FROM pragma_table_info(?)", (name,))
                ]
                indexes = [
                    {"name": row[1], "unique": bool(row[2]), "origin": row[3],
                     "partial": bool(row[4])}
                    for row in connection.execute("SELECT * FROM pragma_index_list(?)", (name,))
                ]
                result.append({
                    "name": name, "type": kind, "columns": columns,
                    "indexes": indexes, "sql": sql,
                })
            return json.dumps({"connection": config.name, "objects": result}, default=str)


class QueryTool(Tool):
    parallel_safe = True
    effect = ToolEffect.READ_ONLY

    def __init__(self, backend: SQLiteTools) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "db_query",
            "Run one read-only SQLite query with optional bound parameters. Results are row- and size-bounded.",
            {
                "type": "object",
                "properties": {
                    "connection": _connection_property(self.backend.connections),
                    "sql": {"type": "string", "description": "One SQLite read-only SQL statement."},
                    "parameters": {
                        "description": "Named object or positional array of JSON scalar bind values.",
                        "oneOf": [{"type": "object"}, {"type": "array"}],
                    },
                },
                "required": ["connection", "sql"],
            },
        )

    def run(
        self, arguments: dict[str, Any], cancellation: CancellationToken | None = None,
    ) -> str:
        config = self.backend.selected(arguments)
        sql = arguments.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be a non-empty string")
        parameters = arguments.get("parameters", {})
        if not isinstance(parameters, (dict, list)):
            raise ValueError("parameters must be an object or array")
        with closing(self.backend.connect(config, cancellation)) as connection:
            cursor = connection.execute(sql, parameters)
            if cursor.description is None:
                raise PermissionError("db_query accepts only statements that return rows")
            columns = [item[0] for item in cursor.description]
            fetched = cursor.fetchmany(config.max_rows + 1)
            truncated = len(fetched) > config.max_rows
            rows = [[_json_value(value) for value in row] for row in fetched[:config.max_rows]]
            return json.dumps({
                "connection": config.name, "columns": columns, "rows": rows,
                "row_count": len(rows), "truncated": truncated,
            }, default=str)


class ExplainTool(QueryTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "db_explain",
            "Show SQLite's query plan for one read-only query without returning its data.",
            {
                "type": "object",
                "properties": {
                    "connection": _connection_property(self.backend.connections),
                    "sql": {"type": "string", "description": "One SQLite read-only SQL query."},
                    "parameters": {
                        "description": "Named object or positional array of JSON scalar bind values.",
                        "oneOf": [{"type": "object"}, {"type": "array"}],
                    },
                },
                "required": ["connection", "sql"],
            },
        )

    def run(
        self, arguments: dict[str, Any], cancellation: CancellationToken | None = None,
    ) -> str:
        updated = dict(arguments)
        sql = updated.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be a non-empty string")
        updated["sql"] = f"EXPLAIN QUERY PLAN {sql}"
        return super().run(updated, cancellation)


def database_tools(settings: dict[str, Any], cwd: Path) -> list[Tool]:
    """Build the database tools for trusted, configured SQLite connections."""
    backend = SQLiteTools(_connections(settings, cwd.resolve()))
    return [SchemaTool(backend), QueryTool(backend), ExplainTool(backend)]
