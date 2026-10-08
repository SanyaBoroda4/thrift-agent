"""thrift_api.db and .schema (WO33): one SQL for SQLite and Postgres — the placeholders, TLS required, transactions
(a nested one is a savepoint), the tables as specified, the migration run twice, `python -m thrift_api migrate`;
Postgres through a stand-in psycopg (nothing is installed or reached)."""
import os
import subprocess
import sys
import types

import pytest

from thrift_agent.config import ROOT
from thrift_api import __main__ as cli
from thrift_api.db import Database, postgres_sql, require_tls, sqlite_path
from thrift_api.schema import migrate

COLUMNS = {
    "items": ["id", "title", "brand", "size", "price", "created_at", "updated_at"],
    "listings": ["item_id", "marketplace", "status", "url", "listing_id", "sku", "price", "posted_at", "updated_at"],
    "email_events": ["message_id", "thread_id", "marketplace", "kind", "subject", "received_at", "parsed_json",
                     "raw_text", "status", "attempts", "created_at"],
    "sales": ["id", "marketplace", "item_id", "listing_id", "title_seen", "price", "order_id", "sold_at", "ship_by",
              "ship_by_source", "shipped_at", "delivered_at", "status", "message_id", "created_at"],
    "delist_tasks": ["id", "sale_id", "item_id", "marketplace", "listing_url", "status", "attempts", "last_error",
                     "evidence", "lease_until", "created_at", "done_at"],
    "reminders": ["sale_id", "kind", "sent_at"],
    "heartbeats": ["source", "last_seen", "info"],
    "samples": ["message_id", "marketplace", "subject", "received_at", "text"],
    "settings": ["key", "value"],
}


def test_sqlite_urls():
    assert sqlite_path(":memory:") == sqlite_path("sqlite://") == sqlite_path("sqlite:///:memory:") == ":memory:"
    assert sqlite_path("sqlite:///var/api.db") == "var/api.db"
    assert sqlite_path("sqlite:////tmp/api.db") == "/tmp/api.db"
    for bad in ("mysql://x/y", "", "sqlite:/x"):
        with pytest.raises(ValueError):
            Database(bad)


def test_statements_and_rows(db):
    assert db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("a", "1")) == 1
    assert db.query("SELECT key, value FROM settings WHERE key = ?", ("a",)) == [{"key": "a", "value": "1"}]
    assert db.one("SELECT value FROM settings WHERE key = ?", ("nothing",)) is None
    assert db.execute("UPDATE settings SET value = '2' WHERE key LIKE 'a%'") == 1
    assert db.query("UPDATE settings SET value = '3' WHERE key = 'a'") == []
    assert db.one("SELECT COUNT(*) AS n FROM settings WHERE key = 'a'") == {"n": 1}


def test_a_transaction_commits_or_rolls_back(db):
    with db.tx():
        db.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
    with pytest.raises(RuntimeError):
        with db.tx():
            db.execute("INSERT INTO settings (key, value) VALUES ('b', '1')")
            raise RuntimeError("boom")
    assert [r["key"] for r in db.query("SELECT key FROM settings WHERE key IN ('a', 'b')")] == ["a"]
    with db.tx():                                                      # the connection is usable afterwards
        db.execute("INSERT INTO settings (key, value) VALUES ('c', '1')")


def test_a_nested_transaction_is_a_savepoint(db):
    with db.tx():
        db.execute("INSERT INTO settings (key, value) VALUES ('outer', '1')")
        with pytest.raises(ValueError):
            with db.tx():
                db.execute("INSERT INTO settings (key, value) VALUES ('inner', '1')")
                raise ValueError("the inner part fails")
        with db.tx():
            db.execute("INSERT INTO settings (key, value) VALUES ('inner2', '1')")
    keys = sorted(r["key"] for r in db.query("SELECT key FROM settings WHERE key <> 'schema_version'"))
    assert keys == ["inner2", "outer"]


def test_a_file_database_keeps_its_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    first = Database(url)
    migrate(first)
    first.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    first.close()
    second = Database(url)
    try:
        assert second.one("SELECT value FROM settings WHERE key = 'k'") == {"value": "v"}
    finally:
        second.close()


def test_the_tables_as_specified_and_migrating_twice(db):
    migrate(db)
    migrate(db)
    for table, columns in COLUMNS.items():
        assert [row["name"] for row in db.query(f"PRAGMA table_info({table})")] == columns, table
    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(COLUMNS)
    indexes = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'index' "
                                               "AND name NOT LIKE 'sqlite_%'")}
    assert {"ix_sales_item", "ix_sales_order", "ix_delist_tasks_status", "ux_delist_tasks_sale_site"} <= indexes
    assert db.one("SELECT value FROM settings WHERE key = 'schema_version'") == {"value": "1"}


def test_postgres_placeholders():
    assert postgres_sql("SELECT * FROM t WHERE a = ? AND b LIKE '%x' AND c IN (?, ?)") == \
        "SELECT * FROM t WHERE a = %s AND b LIKE '%%x' AND c IN (%s, %s)"


@pytest.mark.parametrize("url, expected", [
    ("postgresql://u:p@db.example.com:5432/thrift", "postgresql://u:p@db.example.com:5432/thrift?sslmode=require"),
    ("postgresql://u:p@h/thrift?sslmode=disable", "postgresql://u:p@h/thrift?sslmode=require"),
    ("postgresql://u:p@h/thrift?sslmode=prefer&application_name=x", "postgresql://u:p@h/thrift?application_name=x&sslmode=require"),
    ("postgresql://u:p@h/thrift?sslmode=verify-full", "postgresql://u:p@h/thrift?sslmode=verify-full"),
    ("postgres://u:p@h/thrift?sslmode=require", "postgres://u:p@h/thrift?sslmode=require"),
])
def test_tls_is_required(url, expected):
    assert require_tls(url) == expected


class FakeCursor:
    description = None
    rowcount = 1

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, conninfo, **kwargs):
        self.conninfo, self.kwargs, self.statements = conninfo, kwargs, []
        self.closed = self.broken = False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return FakeCursor()

    def close(self):
        self.closed = True


@pytest.fixture
def fake_psycopg(monkeypatch):
    """psycopg as far as Database uses it: connect(...) and psycopg.rows.dict_row; the connections it made."""
    made = []
    psycopg, rows = types.ModuleType("psycopg"), types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    psycopg.rows = rows
    psycopg.connect = lambda conninfo, **kwargs: made.append(FakeConnection(conninfo, **kwargs)) or made[-1]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    return made, rows.dict_row


def test_postgres_through_psycopg(fake_psycopg):
    made, dict_row = fake_psycopg
    db = Database("postgresql://thrift:s3cret@db.example.com/thrift")
    [conn] = made
    assert conn.conninfo == "postgresql://thrift:s3cret@db.example.com/thrift?sslmode=require"
    assert (conn.kwargs["autocommit"], conn.kwargs["row_factory"], conn.kwargs["prepare_threshold"]) == (True, dict_row,
                                                                                                         None)
    assert repr(db) == "<Database postgres>"                           # never the URL and its password
    db.execute("UPDATE sales SET status = ? WHERE id = ? AND note LIKE '%x'", ("done", "s_1"))
    db.execute("SELECT 1")
    with db.tx():
        db.execute("DELETE FROM reminders WHERE sale_id = ?", ("s_1",))
        with db.tx():
            db.execute("SELECT 2")
    assert conn.statements == [
        ("UPDATE sales SET status = %s WHERE id = %s AND note LIKE '%%x'", ("done", "s_1")), ("SELECT 1", None),
        ("BEGIN", None), ("DELETE FROM reminders WHERE sale_id = %s", ("s_1",)),
        ("SAVEPOINT sp_1", None), ("SELECT 2", None), ("RELEASE SAVEPOINT sp_1", None), ("COMMIT", None)]
    migrate(db)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in conn.statements)


def test_a_lost_postgres_connection_is_opened_again(fake_psycopg):
    made, _ = fake_psycopg
    db = Database("postgresql://u:p@h/db")
    made[0].broken = True
    db.execute("SELECT 1")
    assert len(made) == 2 and made[0].closed and made[1].statements == [("SELECT 1", None)]


def test_python_m_thrift_api_migrate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    assert cli.main(["migrate"]) == 0 and cli.main(["migrate"]) == 0
    assert capsys.readouterr().out == "migrated (sqlite)\nmigrated (sqlite)\n"
    assert cli.main([]) == 2 and cli.main(["drop"]) == 2
    monkeypatch.delenv("DATABASE_URL")
    assert cli.main(["migrate"]) == 2


def test_python_m_thrift_api_migrate_as_a_command(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}"}
    done = subprocess.run([sys.executable, "-m", "thrift_api", "migrate"], cwd=ROOT / "api", env=env,
                          capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout.strip()) == (0, "migrated (sqlite)"), done.stderr
    assert (tmp_path / "cli.db").exists()
