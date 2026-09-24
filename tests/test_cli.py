from thrift_agent import cli
from thrift_agent.config import settings
from thrift_agent.db import DB


def test_worker_tick_survives_inbox_errors(monkeypatch, tmp_path):
    def boom(s):
        raise OSError("iCloud evicted a file mid-scan")
    monkeypatch.setattr(cli.pipeline, "ready_folders", boom)
    db = DB(tmp_path / "state.db")
    assert cli._safe_tick(settings(), db) == 0                    # no exception: the service keeps running
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='error'").fetchone()[0] == 1
