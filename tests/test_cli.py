import os
import pytest
from typer.testing import CliRunner

from thrift_agent import cli
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB


def test_worker_tick_survives_inbox_errors(monkeypatch, tmp_path):
    def boom(s, db=None):
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

    async def fake_run(s, db, once=False, force_dry=False, allow_dev_browser=False, stage=None):
        seen.update(once=once, force_dry=force_dry, allow_dev_browser=allow_dev_browser, stage=stage)
    monkeypatch.setattr(runner, "run", fake_run)
    monkeypatch.setattr(cli, "_db", lambda: object())
    assert CliRunner().invoke(cli.app, ["poster", "--once", "--allow-dev-browser"]).exit_code == 0
    assert seen == {"once": True, "force_dry": False, "allow_dev_browser": True, "stage": "form"}
    CliRunner().invoke(cli.app, ["poster"])
    assert seen["allow_dev_browser"] is False


def test_poster_stage_option_overrides_the_config_and_is_checked(monkeypatch):
    from typer.testing import CliRunner
    import thrift_agent.post.runner as runner
    seen = {}

    async def fake_run(s, db, **kw):
        seen.update(kw)
    monkeypatch.setattr(runner, "run", fake_run)
    monkeypatch.setattr(cli, "_db", lambda: object())
    assert CliRunner().invoke(cli.app, ["poster", "--once", "--dry-run", "--stage", "Review"]).exit_code == 0
    assert seen["stage"] == "review"
    seen.clear()
    r = CliRunner().invoke(cli.app, ["poster", "--once", "--stage", "publish"])
    assert r.exit_code != 0 and seen == {}                          # refused before the browser is ever opened


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
    calls, pumped = [], []
    from types import SimpleNamespace
    monkeypatch.setattr(cli.pipeline, "set_price", lambda s, db, iid, amount: calls.append((iid, amount)) or "ready")
    monkeypatch.setattr(cli, "_db", lambda: SimpleNamespace(outbox_resolve=lambda kind, ref: None))
    monkeypatch.setattr(cli.approve, "pump", lambda s, db: pumped.append(1))
    r = CliRunner().invoke(cli.app, ["price", "i_1", "85"])
    assert r.exit_code == 0 and calls == [("i_1", 85)] and "$85" in r.output and "ready" in r.output
    assert pumped == [1]                                              # the next card goes out (one at a time)


def test_worker_and_telegram_iterations(monkeypatch, tmp_path):
    """WO20: the main thread processes and sends the next question; the Telegram thread polls (answers send the next
    one at once, while items are still being processed) and re-sends the open question once it is old."""
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))
    seen = []
    monkeypatch.setattr(cli, "_safe_tick", lambda s, db: seen.append("tick"))
    monkeypatch.setattr(cli.approve, "pump", lambda s, db: seen.append("pump"))
    monkeypatch.setattr(cli.approve, "poll_once", lambda s, db, bot, timeout: seen.append(("poll", timeout)) or 0)
    monkeypatch.setattr(cli.approve, "resend_pending", lambda s, db: seen.append("resend") or [])
    monkeypatch.setattr(cli.time, "sleep", lambda n: seen.append(("sleep", n)))
    cli._worker_iteration(s, db, interval=7)
    assert seen == ["tick", "pump", ("sleep", 7)]
    seen.clear()
    due = cli.time.monotonic() - cli.RESEND_CHECK_SECONDS - 1         # over an hour ago: the resend check is due
    state = {"last_resend": due}                                      # (not 0: a fresh CI runner's clock is small)
    s.data["telegram"] = {**s.data.get("telegram", {}), "poll_timeout": 15}      # a copy: settings() is shared
    cli._telegram_iteration(s, db, _FakeBot(), state)
    assert seen == [("poll", 15), "pump", "resend"] and state["last_resend"] > due
    seen.clear()
    cli._telegram_iteration(s, db, _FakeBot(), state)
    assert seen == [("poll", 15), "pump"]                             # not due again yet


def test_the_worker_exits_when_the_telegram_thread_dies(monkeypatch, tmp_path):
    """launchd (KeepAlive) restarts the worker: never a worker that processes but no longer hears the owner."""
    s = _settings(tmp_path, "dev")
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli.approve, "bot_for", lambda s: _FakeBot())
    monkeypatch.setattr(cli.approve, "resend_pending", lambda s, db: [])
    monkeypatch.setattr(cli, "_telegram_loop", lambda s, bot: None)          # returns at once: the thread is gone
    monkeypatch.setattr(cli, "_worker_iteration", lambda s, db, interval: cli.time.sleep(0.05))
    r = CliRunner().invoke(cli.app, ["run"])
    assert r.exit_code != 0 and "Telegram thread stopped" in str(r.exception or r.output)


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


# ---------- WO6: every command sees .env ----------

def test_cli_loads_dotenv_for_commands_that_never_call_settings(monkeypatch, tmp_path):
    """On the Mac `thrift telegram setup` said the token was missing although .env had it: it read os.environ before
    anything had loaded .env. The app callback now loads it for every command."""
    import thrift_agent.telegram as tg
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=123:from-dotenv\nTELEGRAM_CHAT_ID=-100123\n", encoding="utf-8")
    monkeypatch.setattr(cli.config, "ENV_FILE", env)
    assert "TELEGRAM_BOT_TOKEN" not in os.environ                       # unset in the process: only .env has it
    seen = {}

    def fake_bot(token, chat, users):
        seen.update(token=token, chat=chat)
        return _FakeBot([{"update_id": 1, "message": {"chat": {"id": -100123, "title": "g"},
                                                      "from": {"id": 555, "username": "owner"}, "text": "/start"}}])
    monkeypatch.setattr(tg, "Bot", fake_bot)
    r = CliRunner().invoke(cli.app, ["telegram", "setup"])
    assert r.exit_code == 0, r.output
    assert seen == {"token": "123:from-dotenv", "chat": "-100123"} and "555" in r.output


@pytest.mark.parametrize("pad", [0, 150, 197])     # 197 folded "nowhere" inside the 80-col rich panel (the Mac case)
def test_telegram_setup_error_names_the_env_file(monkeypatch, tmp_path, pad):
    """Assert on the exception, not the rich panel: it wraps long paths (macOS temp dirs) mid-word, borders and all."""
    import typer
    missing = tmp_path / ("p" * pad) / "nowhere" / ".env"
    monkeypatch.setattr(cli.config, "ENV_FILE", missing)
    r = CliRunner().invoke(cli.app, ["telegram", "setup"], standalone_mode=False)
    assert isinstance(r.exception, typer.BadParameter)
    msg = r.exception.message
    assert str(missing) in msg and "which does not exist" in msg
    assert str(cli.config.ROOT / ".env") not in msg                                # never the real .env path


def test_telegram_setup_error_panel_shows_the_whole_path(monkeypatch, tmp_path):
    import typer.rich_utils
    missing = tmp_path / ("p" * 197) / "nowhere" / ".env"
    monkeypatch.setattr(cli.config, "ENV_FILE", missing)
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 1000)                     # wide enough that nothing wraps
    r = CliRunner().invoke(cli.app, ["telegram", "setup"])
    assert r.exit_code != 0 and str(missing) in r.output and "which does not exist" in r.output


def test_exported_variables_win_over_dotenv(monkeypatch, tmp_path):
    import thrift_agent.telegram as tg
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(cli.config, "ENV_FILE", env)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-shell")
    seen = {}
    monkeypatch.setattr(tg, "Bot", lambda token, chat, users: seen.update(token=token) or _FakeBot([]))
    CliRunner().invoke(cli.app, ["telegram", "setup"])
    assert seen["token"] == "from-shell"


# ---------- WO6: CLI actions are echoed to the group and settle the pending Telegram message ----------

def test_price_answer_confirm_announce_to_the_group(monkeypatch, tmp_path):
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    said = []
    monkeypatch.setattr(cli.approve, "announce", lambda s, text: said.append(text) or True)
    monkeypatch.setattr(cli, "_tick", lambda s, db: None)
    iid = db.add_item(db.add_batch("share", 1), 1, str(tmp_path / "item"))
    db.set_item(iid, status="awaiting_price", price={"list_price": 30}, renders={"poshmark": {"price": 30}})
    db.add_outbox("-100", 11, "item", iid, text="approve?")
    db.add_outbox("-100", 12, "batch", "b_1", text="confirm?")
    monkeypatch.setattr(cli.pipeline, "confirm", lambda s, db, bid, cmd: None)

    assert CliRunner().invoke(cli.app, ["price", iid, "85"]).exit_code == 0
    assert db.item(iid)["status"] == "ready" and db.outbox_lookup("-100", 11)["resolved_at"]
    assert CliRunner().invoke(cli.app, ["answer", iid, "worn twice"]).exit_code == 0
    assert CliRunner().invoke(cli.app, ["confirm", "b_1", "ok"]).exit_code == 0
    assert db.outbox_lookup("-100", 12)["resolved_at"]
    assert [t.split(":")[0] for t in said] == [iid, iid, "batch b_1"]
    assert "$85" in said[0] and "worn twice" in said[1] and "(ok)" in said[2]


def test_poster_publish_first_runs_the_supervised_publish(monkeypatch):
    from typer.testing import CliRunner
    import thrift_agent.post.runner as runner
    from thrift_agent.post.base import Outcome
    seen = {}

    async def fake_publish_first(s, db, iid, mp="poshmark"):
        seen["iid"] = iid
        return Outcome("posted", url="https://poshmark.com/listing/x")

    async def no_loop(*a, **k):
        raise AssertionError("the poster loop must not run")
    monkeypatch.setattr(runner, "publish_first", fake_publish_first)
    monkeypatch.setattr(runner, "run", no_loop)
    monkeypatch.setattr(cli, "_db", lambda: object())
    r = CliRunner().invoke(cli.app, ["poster", "--publish-first", "i_261002_abc123"])
    assert r.exit_code == 0, r.output
    assert seen == {"iid": "i_261002_abc123"} and "posted: https://poshmark.com/listing/x" in r.output


def test_poster_publish_first_says_why_it_refused_and_exits_1(monkeypatch):
    from typer.testing import CliRunner
    import thrift_agent.post.runner as runner
    from thrift_agent.post.base import Outcome

    async def refused(s, db, iid, mp="poshmark"):
        raise ValueError(f"item {iid} has no owner-approved price (approve it in Telegram or `thrift price`)")

    async def failed(s, db, iid, mp="poshmark"):
        return Outcome("failed", error="unconfirmed publish: PosterError: after List This Item no listing address")
    monkeypatch.setattr(cli, "_db", lambda: object())
    for fake, says in ((refused, "has no owner-approved price"), (failed, "failed: unconfirmed publish")):
        monkeypatch.setattr(runner, "publish_first", fake)
        r = CliRunner().invoke(cli.app, ["poster", "--publish-first", "i_261002_abc123"])
        assert r.exit_code == 1 and says in " ".join(r.output.split()), r.output
        assert "Traceback" not in r.output


def test_mark_posted_reports_the_address_or_why_not(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    import thrift_agent.post.runner as runner
    url = "https://poshmark.com/listing/Naturino-Sneakers-Toddler-size-75-6ac111490000000000000a01"
    calls = []

    async def marked(s, db, iid, mp, address):
        calls.append((iid, mp, address))
        return address

    async def refused(s, db, iid, mp, address):
        raise ValueError(f"{mp}: only a post in 'unconfirmed publish' can be marked posted ({iid} has status posted)")
    db = DB(tmp_path / "state.db")                            # no poster heartbeat: it opens Chrome itself
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(runner, "mark_posted", marked)
    r = CliRunner().invoke(cli.app, ["mark-posted", "i_261002_abc123", "poshmark", url])
    assert r.exit_code == 0 and calls == [("i_261002_abc123", "poshmark", url)], r.output
    assert "posted i_261002_abc123 on poshmark" in " ".join(r.output.split())
    monkeypatch.setattr(runner, "mark_posted", refused)
    r = CliRunner().invoke(cli.app, ["mark-posted", "i_261002_abc123", "poshmark", url])
    assert r.exit_code == 1 and "not marked" in r.output and "Traceback" not in r.output



def test_condition_is_the_cli_twin_of_the_buttons(monkeypatch):
    from typer.testing import CliRunner
    from thrift_agent import approve, pipeline
    calls, said, resolved = [], [], []

    class Outbox:
        def outbox_resolve(self, kind, ref):
            resolved.append((kind, ref))

    def set_condition(s, db, iid, choice):
        if pipeline.owner_choice(choice) is None:
            raise ValueError(f"condition must be one of nwt, like_new, good, got {choice!r}")
        calls.append((iid, choice))
        return "new"
    monkeypatch.setattr(cli, "_db", lambda: Outbox())
    monkeypatch.setattr(pipeline, "set_condition", set_condition)
    monkeypatch.setattr(approve, "announce", lambda s, text: said.append(text) or True)
    monkeypatch.setattr(approve, "pump", lambda s, db: None)
    monkeypatch.setattr(cli, "_tick_unless_worker", lambda s, db: None)
    r = CliRunner().invoke(cli.app, ["condition", "i_261003_abc123", "like_new"])
    assert r.exit_code == 0 and calls == [("i_261003_abc123", "like_new")], r.output
    assert resolved == [("condition", "i_261003_abc123")] and "Like New (brand new, no tags)" in said[0]
    r = CliRunner().invoke(cli.app, ["condition", "i_261003_abc123", "fair"])
    assert r.exit_code == 1 and "not set" in r.output and "Traceback" not in r.output



def test_requeue_sends_a_failed_batch_back_and_status_lists_the_batches(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from thrift_agent.db import DB
    db = DB(tmp_path / "state.db")
    share = tmp_path / "inbox" / "2026-10-03_1500"
    share.mkdir(parents=True)
    failed = db.add_batch(str(share), 12)
    db.set_batch(failed, status="failed")
    db.log(failed, "error", "Traceback ...\nanthropic.BadRequestError: Error code: 400 - tool_choice not supported")
    waiting = db.add_batch(str(tmp_path / "inbox" / "other"), 5)
    db.set_batch(waiting, status="needs_confirm")
    doubted = db.add_batch(str(tmp_path / "inbox" / "third"), 9)            # accepted without the owner (WO20b)
    db.set_batch(doubted, status="split", segmentation={"auto_accepted": True},
                 reasons=["item 2: low confidence 0.60"])
    monkeypatch.setattr(cli, "_db", lambda: db)

    raw = CliRunner().invoke(cli.app, ["status"], terminal_width=200).output
    out = " ".join("".join(" " if "─" <= ch <= "╿" else ch for ch in raw).split())   # no table borders
    assert f"{failed} failed 12" in out and f"{waiting} needs_confirm 5" in out
    assert f"failed batch {failed} → thrift requeue {failed} anthropic.BadRequestError: Error code: 400" in out
    assert f"awaiting confirm {waiting} → thrift confirm {waiting} ok" in out
    assert f"grouping accepted with doubts {doubted}: item 2: low confidence 0.60" in out   # kept, never sent
    assert f"Telegram open: nothing; 1 in the queue, next: batch {waiting}" in out       # the owner's queue (WO20)

    r = CliRunner().invoke(cli.app, ["requeue", failed])
    assert r.exit_code == 0 and db.batch(failed)["status"] == "new", r.output
    assert "splits it again" in " ".join(r.output.split())
    r = CliRunner().invoke(cli.app, ["requeue", waiting])
    assert r.exit_code == 1 and "only a failed batch" in " ".join(r.output.split()) and "Traceback" not in r.output


def test_redo_rebuilds_and_lists_what_it_left_alone(monkeypatch, tmp_path):
    from thrift_agent import approve, pipeline
    calls, said, pumped = [], [], []
    monkeypatch.setattr(cli, "settings", lambda: _settings(tmp_path, "prod"))
    monkeypatch.setattr(pipeline, "redo_batch",
                        lambda s, db, bid: calls.append(bid) or (["i_1", "i_2"], ["i_3 (poshmark posted)"]))
    monkeypatch.setattr(approve, "announce", lambda s, text: said.append(text) or True)
    monkeypatch.setattr(approve, "pump", lambda s, db: pumped.append(1))
    r = CliRunner().invoke(cli.app, ["redo", "b_261003_x"])
    assert r.exit_code == 0 and calls == ["b_261003_x"], r.output
    assert "rebuilding 2 items of b_261003_x: i_1, i_2" in r.output and "left as it is i_3 (poshmark posted)" in r.output
    assert "the worker will pick it up" in r.output                       # prod: the worker processes, not the CLI
    assert said == ["batch b_261003_x: 2 items rebuilt from the CLI — new cards follow, one at a time"] and pumped

    def refuse(s, db, bid):
        raise ValueError("every item reached the site (i_3 (posted))")
    monkeypatch.setattr(pipeline, "redo_batch", refuse)
    r = CliRunner().invoke(cli.app, ["redo", "b_261003_x"])
    assert r.exit_code == 1 and "not rebuilt" in r.output and "Traceback" not in r.output


def test_kids_is_the_cli_twin_of_the_girls_boys_buttons(monkeypatch, tmp_path):
    from thrift_agent import approve, pipeline
    calls, resolved = [], []
    db = DB(tmp_path / "state.db")
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(db, "outbox_resolve", lambda kind, ref: resolved.append((kind, ref)))
    monkeypatch.setattr(pipeline, "set_kids_gender", lambda s, db, iid, choice: calls.append((iid, choice)) or "ready")
    monkeypatch.setattr(approve, "announce", lambda s, text: True)
    monkeypatch.setattr(approve, "pump", lambda s, db: None)
    r = CliRunner().invoke(cli.app, ["kids", "i_1", "Boys"])
    assert r.exit_code == 0 and calls == [("i_1", "Boys")] and resolved == [("kids", "i_1")] and "ready" in r.output


def test_the_worker_records_each_inbox_scan_and_status_shows_it(monkeypatch, tmp_path):
    """deploy/mac_deploy.sh waits for this to move after a restart: on the Mac a launchd worker can wait silently on
    macOS's iCloud Drive permission (WO21)."""
    from typer.testing import CliRunner
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda _s, _db=None: [])
    monkeypatch.setattr(cli, "_db", lambda: db)
    r = CliRunner().invoke(cli.app, ["status"], terminal_width=200)
    assert "worker inbox scan: never" in r.output
    cli._tick(s, db)
    stamp = db.kv_get(cli.SCAN_KEY)
    assert stamp and f"worker inbox scan: {stamp}" in CliRunner().invoke(cli.app, ["status"], terminal_width=200).output
