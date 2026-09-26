import pytest
from typer.testing import CliRunner

from thrift_agent import cli
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB


def test_worker_tick_survives_inbox_errors(monkeypatch, tmp_path):
    def boom(s):
        raise OSError("iCloud evicted a file mid-scan")
    monkeypatch.setattr(cli.pipeline, "ready_folders", boom)
    db = DB(tmp_path / "state.db")
    assert cli._safe_tick(settings(), db) == 0                    # no exception: the service keeps running
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='error'").fetchone()[0] == 1


def _settings(tmp_path, role):
    base = settings().data
    data = {**base, "machine_role": role, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    return Settings(data)


@pytest.mark.parametrize("command", [["confirm", "b_1", "ok"], ["answer", "i_1", "size 8"]])
def test_confirm_and_answer_leave_processing_to_the_worker_on_prod(monkeypatch, tmp_path, command):
    """On the Mac `thrift run` ticks the same DB every 15 s; a second ticker could grab the same 'new' row."""
    monkeypatch.setattr(cli, "settings", lambda: _settings(tmp_path, "prod"))
    ticks, called = [], []
    monkeypatch.setattr(cli, "_tick", lambda s, db: ticks.append(1))
    monkeypatch.setattr(cli.pipeline, "confirm", lambda s, db, bid, cmd: called.append(("confirm", bid, cmd)))
    monkeypatch.setattr(cli.pipeline, "answer", lambda s, db, iid, note: called.append(("answer", iid, note)))
    r = CliRunner().invoke(cli.app, command)
    assert r.exit_code == 0, r.output
    assert called == [(command[0], command[1], command[2])] and ticks == []
    assert "worker" in r.output


def test_confirm_processes_right_away_on_dev(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "settings", lambda: _settings(tmp_path, "dev"))
    ticks = []
    monkeypatch.setattr(cli, "_tick", lambda s, db: ticks.append(1))
    monkeypatch.setattr(cli.pipeline, "confirm", lambda s, db, bid, cmd: None)
    r = CliRunner().invoke(cli.app, ["confirm", "b_1", "ok"])
    assert r.exit_code == 0, r.output
    assert ticks == [1]


def test_worker_refuses_to_start_on_prod_without_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "settings", lambda: _settings(tmp_path, "prod"))

    def check(s):
        raise RuntimeError("telegram.enabled but TELEGRAM_BOT_TOKEN is missing")
    monkeypatch.setattr(cli.notify, "check", check, raising=False)
    monkeypatch.setattr(cli, "_safe_tick", lambda s, db: pytest.fail("the worker must not start"))
    r = CliRunner().invoke(cli.app, ["run"])
    assert r.exit_code != 0 and isinstance(r.exception, RuntimeError)


def test_requeue_command_reports_marketplaces(monkeypatch):
    from typer.testing import CliRunner
    calls = []
    monkeypatch.setattr(cli.pipeline, "requeue", lambda s, db, iid, mp=None: calls.append((iid, mp)) or ["poshmark"])
    monkeypatch.setattr(cli, "_db", lambda: object())
    result = CliRunner().invoke(cli.app, ["requeue", "i_1", "poshmark"])
    assert result.exit_code == 0 and calls == [("i_1", "poshmark")] and "poshmark" in result.output


def test_poster_passes_allow_dev_browser(monkeypatch):
    from typer.testing import CliRunner
    import thrift_agent.post.runner as runner
    seen = {}

    async def fake_run(s, db, once=False, force_dry=False, allow_dev_browser=False):
        seen.update(once=once, force_dry=force_dry, allow_dev_browser=allow_dev_browser)
    monkeypatch.setattr(runner, "run", fake_run)
    monkeypatch.setattr(cli, "_db", lambda: object())
    assert CliRunner().invoke(cli.app, ["poster", "--once", "--allow-dev-browser"]).exit_code == 0
    assert seen == {"once": True, "force_dry": False, "allow_dev_browser": True}
    CliRunner().invoke(cli.app, ["poster"])
    assert seen["allow_dev_browser"] is False
