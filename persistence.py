"""Architecture 14 — persistence abstraction.

App code keeps calling `app.connect()`; this module decides the backend:
  - SQLite (default; development and tests) via a local file or `REACHOUT_DB`,
  - PostgreSQL when a `*_DATABASE_URL` is configured (staging/production).

Both dialects are exposed through the same tiny adapter surface (`execute`, `executemany`,
`executescript`, `commit`, `row_factory`-style dict rows, context manager). SQLite-only SQL
patterns (AUTOINCREMENT, `INSERT OR IGNORE`, `PRAGMA`) are normalized for PostgreSQL so existing
business functions keep working wherever practical.
"""

import os
import re
import sqlite3


class PersistenceError(RuntimeError):
    """Raised when a configured backend cannot be used (fail closed)."""


# Which env URL an environment must use. Staging must NEVER point at the production DB.
ENV_DATABASE_URL = {
    "production": "PRODUCTION_DATABASE_URL",
    "staging": "STAGING_DATABASE_URL",
    "development": "REACHOUT_DB",
}


def environment_name():
    return os.environ.get("REACHOUT_ENV", "development").strip().lower() or "development"


def database_url(*, environment=None):
    """The URL/DSN for an environment, or None for the SQLite dev default."""
    env = (environment or environment_name())
    return os.environ.get(ENV_DATABASE_URL.get(env, "REACHOUT_DB")) or None


def _strip_driver(dsn):
    return re.sub(r"^postgres(?:ql)?(\+\w+)?://", "", dsn or "")


def staging_points_at_production():
    """True when staging is misconfigured to use the production database (must be refused)."""
    staging = os.environ.get("STAGING_DATABASE_URL", "").strip()
    production = os.environ.get("PRODUCTION_DATABASE_URL", "").strip()
    return bool(staging) and staging == production


def assert_environment_isolation():
    if environment_name() == "staging" and staging_points_at_production():
        raise PersistenceError("staging must not share the production database")
    if environment_name() == "production" and not os.environ.get("PRODUCTION_DATABASE_URL"):
        raise PersistenceError("production requires PRODUCTION_DATABASE_URL")


# ---------- adapters ----------

class SQLiteConnection:
    """Thin sqlite3 wrapper. `?` placeholders and dict rows are the common contract."""

    dialect = "sqlite"

    def __init__(self, path):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._raw = sqlite3.connect(path, timeout=15)
        self._raw.row_factory = sqlite3.Row
        self._raw.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, params=()):
        return self._raw.execute(sql, params)

    def executemany(self, sql, seq):
        return self._raw.executemany(sql, seq)

    def executescript(self, script):
        return self._raw.executescript(script)

    def iterdump(self):
        return self._raw.iterdump()

    @property
    def raw(self):
        return self._raw

    def commit(self):
        return self._raw.commit()

    def close(self):
        return self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False


def _translate_for_postgres(sql):
    """Best-effort SQLite -> PostgreSQL normalization for the SQL patterns this app uses."""
    out = sql
    out = re.sub(r"\bINSERT OR IGNORE INTO\b", "INSERT INTO", out, flags=re.I)
    out = re.sub(r"\bINSERT OR REPLACE INTO\b", "INSERT INTO", out, flags=re.I)
    # SQLite `?` placeholders -> psycopg `%s`.
    out = out.replace("?", "%s")
    return out


class PostgresConnection:
    """psycopg (v3) or psycopg2 adapter. Only used when a URL is configured and the driver exists."""

    dialect = "postgres"

    def __init__(self, url):
        self._driver = _import_psycopg()
        dsn = _strip_driver(url)
        self._raw = self._driver.connect(dsn)
        self._raw.autocommit = False
        try:
            self._cursor = self._raw.cursor()
            self._cursor.execute("SET statement_timeout = '30s'")
        except Exception:  # noqa: BLE001 - statement_timeout is best-effort
            pass

    def execute(self, sql, params=()):
        cursor = self._raw.cursor()
        cursor.execute(_translate_for_postgres(sql), tuple(params or ()))
        return _PostgresCursor(cursor)

    def executemany(self, sql, seq):
        cursor = self._raw.cursor()
        cursor.executemany(_translate_for_postgres(sql), list(seq))
        return _PostgresCursor(cursor)

    def executescript(self, script):
        cursor = self._raw.cursor()
        for statement in [s.strip() for s in script.split(";") if s.strip()]:
            cursor.execute(_translate_for_postgres(statement))
        return _PostgresCursor(cursor)

    def commit(self):
        return self._raw.commit()

    def close(self):
        return self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False


class _PostgresCursor:
    """Shim so callers that read `row["col"]`, `.fetchone()`, `.fetchall()`, `.rowcount` work."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        return _as_row(self._cursor, row) if row is not None else None

    def fetchall(self):
        return [_as_row(self._cursor, row) for row in self._cursor.fetchall()]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __iter__(self):
        for row in self._cursor.fetchall():
            yield _as_row(self._cursor, row)


def _as_row(cursor, row):
    if isinstance(row, dict):
        return row
    columns = [desc[0] for desc in (cursor.description or [])]
    return {name: value for name, value in zip(columns, row)}


def _import_psycopg():
    try:
        import psycopg  # psycopg v3
        return psycopg
    except ImportError:
        pass
    try:
        import psycopg2 as psycopg  # psycopg2 fallback
        return psycopg
    except ImportError as error:
        raise PersistenceError(
            "PostgreSQL is configured but no driver is installed; pip install 'psycopg[binary]'"
        ) from error


def open_connection(*, sqlite_path=None, environment=None):
    """Open the correct backend for the environment. Fails closed on misconfiguration."""
    env = (environment or environment_name())
    url = database_url(environment=env)
    if url and url.startswith(("postgres://", "postgresql://", "postgresql+", "postgres+")):
        assert_environment_isolation()
        return PostgresConnection(url)
    if env in ("staging", "production") and not url:
        raise PersistenceError(f"{env} requires a database URL")
    return SQLiteConnection(sqlite_path or os.environ.get("REACHOUT_DB") or "reachout.sqlite3")
