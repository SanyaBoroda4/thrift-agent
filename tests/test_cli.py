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


def test_poster_on_prod_without_a_telegram_token_refuses_to_start(monkeypatch, settings_override):
    """The Mac's real situation before .env is filled in: prod + telegram.enabled, no token. This is the wanted
    behaviour of `thrift poster` there, and it must be an explicit opt-in here, never the machine's own config."""
    settings_override(machine_role="prod", telegram={"enabled": True})
    monkeypatch.setattr(cli, "_db", lambda: pytest.fail("must refuse before touching the DB"))
    r = CliRunner().invoke(cli.app, ["poster", "--once"])
    assert r.exit_code != 0 and isinstance(r.exception, RuntimeError) and "TELEGRAM" in str(r.exception)


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


# ---------- WO4: price command, worker loop with Telegram, telegram setup/test ----------

class _FakeBot:
    chat_id = "-100123"

    def __init__(self, updates=None):
        self.updates, self.sent = updates or [], []

    def get_updates(self, offset, timeout):
        return self.updates

    def send_message(self, text, buttons=None, reply_to=None):
        self.sent.append(text)
        return 42


def test_price_command_sets_the_owner_price(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.pipeline, "set_price", lambda s, db, iid, amount: calls.append((iid, amount)) or "ready")
    monkeypatch.setattr(cli, "_db", lambda: object())
    r = CliRunner().invoke(cli.app, ["price", "i_1", "85"])
    assert r.exit_code == 0 and calls == [("i_1", 85)] and "$85" in r.output and "ready" in r.output


def test_worker_iteration_polls_telegram_when_configured(monkeypatch, tmp_path):
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))
    seen = []
    monkeypatch.setattr(cli, "_safe_tick", lambda s, db: seen.append("tick"))
    monkeypatch.setattr(cli.approve, "poll_once", lambda s, db, bot, timeout: seen.append(("poll", timeout)) or 0)
    monkeypatch.setattr(cli.approve, "resend_pending", lambda s, db, force=False: seen.append("resend") or [])
    monkeypatch.setattr(cli.time, "sleep", lambda n: seen.append(("sleep", n)))
    state = {"last_resend": 0}                                        # long ago: the hourly resend check is due
    cli._worker_iteration(s, db, _FakeBot(), interval=15, state=state)
    assert seen == ["tick", ("poll", 15), "resend"] and state["last_resend"] > 0
    seen.clear()
    cli._worker_iteration(s, db, _FakeBot(), interval=15, state=state)
    assert seen == ["tick", ("poll", 15)]                             # not due again yet
    seen.clear()
    cli._worker_iteration(s, db, None, interval=7, state=state)      # Telegram off: plain sleep, no polling
    assert seen == ["tick", ("sleep", 7)]


def test_worker_poll_errors_do_not_kill_the_worker(monkeypatch, tmp_path):
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))

    def boom(s, db, bot, timeout):
        raise ConnectionError("telegram down")
    monkeypatch.setattr(cli.approve, "poll_once", boom)
    monkeypatch.setattr(cli.time, "sleep", lambda n: None)
    assert cli._safe_poll(s, db, _FakeBot(), 15) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='error'").fetchone()[0] == 1


def test_telegram_setup_lists_chat_and_user_ids(monkeypatch):
    import thrift_agent.telegram as tg
    updates = [{"update_id": 1, "message": {"chat": {"id": -100123, "title": "Posh closet"},
                                            "from": {"id": 555, "username": "owner"}, "text": "/start"}},
               {"update_id": 2, "callback_query": {"id": "c", "from": {"id": 777, "first_name": "Helper"},
                                                   "message": {"chat": {"id": -100123, "title": "Posh closet"}}}}]
    monkeypatch.setattr(tg, "Bot", lambda token, chat, users: _FakeBot(updates))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    r = CliRunner().invoke(cli.app, ["telegram", "setup"])
    assert r.exit_code == 0, r.output
    assert "-100123" in r.output and "555" in r.output and "777" in r.output and "TELEGRAM_ALLOWED_USER_IDS" in r.output


def test_telegram_setup_needs_a_token():
    r = CliRunner().invoke(cli.app, ["telegram", "setup"])
    assert r.exit_code != 0 and "TELEGRAM_BOT_TOKEN" in r.output


def test_telegram_test_sends_when_configured(monkeypatch):
    r = CliRunner().invoke(cli.app, ["telegram", "test"])
    assert r.exit_code != 0 and "not configured" in r.output          # dev defaults: Telegram off
    bot = _FakeBot()
    monkeypatch.setattr(cli.approve, "bot_for", lambda s: bot)
    r = CliRunner().invoke(cli.app, ["telegram", "test"])
    assert r.exit_code == 0 and bot.sent and "sent" in r.output
