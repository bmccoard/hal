# HAL Database Extension

This separately installed extension gives HAL read-only access to named SQLite
databases. It provides `db_schema`, `db_query`, and `db_explain`. Database paths
are selected in trusted configuration rather than supplied by the model.

## Install and enable

Install HAL and this extension into the same environment:

```bash
python -m pip install -e "../hal[dev]"
python -m pip install -e "../hal/example/database[dev]"
```

Add the extension and at least one named connection to `hal.yaml`:

```yaml
extensions:
  - database

extension_config:
  database:
    max_rows: 200
    timeout_ms: 5000
    connections:
      work:
        driver: sqlite
        path: /approved/data/work.db
        read_only: true
        # Optional trusted native extensions, loaded before query authorization.
        # sqlite_extensions:
        #   - /approved/extensions/vec0.so
```

Relative database and extension paths resolve from HAL's current working
directory. Every configured file must already exist when HAL starts.

For a database-only capability, allow the three tools explicitly and disable
shell access:

```yaml
harness:
  default_capability: database-read
  capabilities:
    database-read:
      description: Inspect configured SQLite databases without changing them
      allowed_tools: [db_schema, db_query, db_explain]
      denied_tools: [bash]
```

## Safety boundary

Connections use SQLite URI `mode=ro`, `PRAGMA query_only`, and an authorizer that
denies writes, schema changes, transactions, attachment, and model-issued pragmas.
Queries have configured time and row limits. SQLite native extensions are executable
code; configure only trusted absolute paths. Extension loading is disabled again
before any model-generated SQL runs.

Import, export, mutation, and PostgreSQL support are intentionally outside this
initial extension. Keep controlled data movement in user-authored scripts.
