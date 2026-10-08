"""The database (WO33): one small wrapper so the SQL is written once and runs on SQLite (the tests, a local run) and on
Postgres (Azure).

- `Database("sqlite:///<path>")`, `Database("sqlite:///:memory:")` or `Database(":memory:")`: the stdlib's sqlite3.
- `Database("postgresql://…")` (or postgres://): psycopg 3, imported only then (the tests never need it), rows as
  dicts, TLS required — `sslmode=require` is added when the URL names none (or a weaker one: disable / allow /
  prefer), `verify-ca` / `verify-full` are kept. Prepared statements are off (`prepare_threshold=None`) so a
  transaction-mode pooler (PgBouncer) works too.

SQL uses `?` placeholders; for Postgres they become `%s` (and a literal % is doubled). Timestamps are ISO-8601 UTC
text (util.iso), JSON is TEXT. Outside `tx()` every statement commits on its own; `tx()` is one transaction (a nested
one is a savepoint) that commits when its block ends and rolls back when it raises.

One connection per Database, shared by the Function's threads: a lock serialises its use, a transaction holding it
from BEGIN to COMMIT, so two requests never interleave their statements. A Postgres connection found closed or broken
is opened again before the next statement. The URL (and its password) is never logged or shown."""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

POSTGRES_SCHEMES = ("postgresql://", "postgres://")
WEAK_SSLMODES = ("disable", "allow", "prefer")
PG_OPTIONS = {"connect_timeout": 10, "application_name": "thrift-api", "keepalives": 1, "keepalives_idle": 30,
              "keepalives_interval": 10, "keepalives_count": 3}


def postgres_sql(sql: str) -> str:
    """The `?` placeholders as psycopg's `%s`, a literal % doubled so psycopg doesn't take it for one."""
    return sql.replace("%", "%%").replace("?", "%s")


def require_tls(url: str) -> str:
    """The Postgres URL with sslmode=require unless it already asks for TLS (require, verify-ca, verify-full)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k == "sslmode" and v.lower() in WEAK_SSLMODES)]
    if not any(k == "sslmode" for k, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def sqlite_path(url: str) -> str:
    """The file of a sqlite URL ("sqlite:///var/x.db" → "var/x.db", "sqlite:////abs/x.db" → "/abs/x.db"); memory for
    ":memory:", "sqlite://" and "sqlite:///:memory:"."""
    if url in (":memory:", "sqlite://", "sqlite:///"):
        return ":memory:"
    if not url.startswith("sqlite:///"):
        raise ValueError("a SQLite URL is sqlite:///<path> (or :memory:)")
    return url[len("sqlite:///"):] or ":memory:"


def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {column[0]: value for column, value in zip(cursor.description, row)}


class Database:
    """See the module. `kind` is "sqlite" or "postgres"."""

    def __init__(self, url: str):
        url = (url or "").strip()
        self._lock = threading.RLock()
        self._depth = 0
        if url == ":memory:" or url.startswith("sqlite:"):
            self.kind = "sqlite"
            self._conn = sqlite3.connect(sqlite_path(url), check_same_thread=False, isolation_level=None)
            self._conn.row_factory = _dict_row
            self._conn.execute("PRAGMA busy_timeout = 5000")
        elif url.startswith(POSTGRES_SCHEMES):
            self.kind = "postgres"
            self._url = require_tls(url)
            self._conn = self._pg_connect()
        else:
            raise ValueError("the database URL must start with sqlite:/// or postgresql://")

    def __repr__(self) -> str:
        return f"<Database {self.kind}>"            # never the URL: it carries the password

    # --- the statements

    def execute(self, sql: str, params: Sequence = ()) -> int:
        """Runs one statement; the number of rows it changed."""
        with self._lock:
            return self._run(sql, params).rowcount

    def query(self, sql: str, params: Sequence = ()) -> list[dict]:
        with self._lock:
            cursor = self._run(sql, params)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []

    def one(self, sql: str, params: Sequence = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def tx(self) -> Iterator[Database]:
        """One transaction; nested, a savepoint inside the outer one."""
        with self._lock:
            if self._depth:
                name = f"sp_{self._depth}"
                self._run(f"SAVEPOINT {name}", ())
                self._depth += 1
                try:
                    yield self
                except BaseException:
                    self._depth -= 1
                    self._run(f"ROLLBACK TO SAVEPOINT {name}", ())
                    self._run(f"RELEASE SAVEPOINT {name}", ())
                    raise
                self._depth -= 1
                self._run(f"RELEASE SAVEPOINT {name}", ())
                return
            self._revive()
            self._run("BEGIN", ())
            self._depth = 1
            try:
                yield self
            except BaseException:
                self._depth = 0
                self._quiet_rollback()
                raise
            self._depth = 0
            try:
                self._run("COMMIT", ())
            except BaseException:
                self._quiet_rollback()          # SQLite keeps a transaction whose COMMIT failed: end it
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — closing is best effort
                pass

    # --- inside

    def _run(self, sql: str, params: Sequence):
        if self.kind == "postgres":
            if self._depth == 0:
                self._revive()
            return self._conn.execute(postgres_sql(sql), tuple(params)) if params else self._conn.execute(sql)
        return self._conn.execute(sql, tuple(params))

    def _quiet_rollback(self) -> None:
        try:
            self._run("ROLLBACK", ())
        except Exception as e:  # noqa: BLE001 — the original error is the one that matters
            log.warning("rollback failed: %s", type(e).__name__)

    def _pg_connect(self):
        import psycopg                               # lazily: only a Postgres deployment needs it
        from psycopg.rows import dict_row
        return psycopg.connect(self._url, autocommit=True, row_factory=dict_row, prepare_threshold=None, **PG_OPTIONS)

    def _revive(self) -> None:
        """A Postgres connection the server or the network dropped is opened again (never inside a transaction)."""
        if self.kind != "postgres" or self._depth:
            return
        if getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False):
            log.warning("database connection lost: reconnecting")
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = self._pg_connect()
