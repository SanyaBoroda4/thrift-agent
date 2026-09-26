"""The poster loop with the browser and Telegram stubbed: job selection, recording, the circuit breaker, and stop."""
import asyncio
from pathlib import Path

import pytest

from thrift_agent import approve, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post import runner
from thrift_agent.post.base import AccountBlocked, NeedsOwner, Outcome
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.schema import Render

RENDER = Render(marketplace="poshmark", title="Tory Burch Red Flats size 7.5", description="Red flats.", brand="Tory Burch",
                department="Women", category="Shoes", subcategory=None, size="7.5", colors=["Red"], condition="excellent",
                price=85, photos=[], sku="i_1")


def _settings(tmp_path, role="dev", autopublish=False, dry_run=True, max_fail=3, username="closet", depop=False) -> Settings:
    """Built from scratch, not from config/settings.yaml: the tests must not depend on the checked-in file."""
    paths = {k: str(tmp_path / k) for k in ("inbox", "work", "archive", "failed", "chrome_profile", "control")}
    s = Settings({
        "machine_role": role,
        "paths": {**paths, "db": str(tmp_path / "state.db")},
        "poster": {"dry_run": dry_run, "max_consecutive_failures": max_fail},
        "schedule": {"timezone": "America/New_York", "hours": ["09:00", "21:00"], "per_hour_max": 10, "daily_cap": 25,
                     "gap_seconds": [150, 420]},
        "marketplaces": {
            "poshmark": {"enabled": True, "username": username, "autopublish": autopublish, "max_photos": 16},
            "depop": {"enabled": depop, "username": username, "autopublish": False},
        },
    })
    s.ensure_dirs()
    return s


def _ready_item(db: DB, seq: int = 1, decision: str = "publish") -> str:
    bid = db.add_batch(f"share_{seq}", 5)
    iid = db.add_item(bid, seq, f"work/{seq}")
    db.set_item(iid, status="ready", gate={"decision": decision, "reasons": []},
                renders={"poshmark": RENDER.model_dump()})
    return iid


# ---------------------------------------------------------------- posters

def test_posters_builds_one_per_enabled_marketplace(tmp_path):
    ps = runner.posters(_settings(tmp_path, depop=True))
    assert isinstance(ps["poshmark"], PoshmarkPoster) and isinstance(ps["depop"], DepopPoster)
    assert list(runner.posters(_settings(tmp_path, depop=False))) == ["poshmark"]


@pytest.mark.parametrize("mp", ["poshmark", "depop"])
@pytest.mark.parametrize("username", ["", "   ", None])
def test_posters_require_a_username_for_each_enabled_marketplace(tmp_path, mp, username):
    s = _settings(tmp_path, depop=True)
    s.data["marketplaces"][mp]["username"] = username
    with pytest.raises(ValueError, match=rf"marketplaces\.{mp}\.username is empty .* private/settings\.yaml"):
        runner.posters(s)
    s.data["marketplaces"][mp]["enabled"] = False                # a disabled marketplace needs no username
    assert mp not in runner.posters(s)


# ---------------------------------------------------------------- next_job

@pytest.mark.parametrize("status,dry,expect_job", [
    (None, False, True), ("queued", False, True), ("dryrun", False, True),
    ("dryrun", True, False),                                             # already dry-run once: don't repeat it
    ("posting", False, False), ("failed", False, False), ("posted", False, False), ("drafted", False, False),
])
def test_next_job_status_matrix(tmp_path, status, dry, expect_job):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    if status:
        db.upsert_post(iid, "poshmark", status=status)
    job = runner.next_job(s, db, ["poshmark"], dry)
    if expect_job:
        assert job is not None and job[0] == iid and job[1] == "poshmark" and job[2].title == RENDER.title
    else:
        assert job is None


def test_next_job_skips_marketplaces_without_a_render(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _ready_item(db)
    assert runner.next_job(s, db, ["depop"], False) is None


@pytest.mark.parametrize("role,autopublish,decision,mode", [
    ("dev", True, "publish", "draft"),        # the dev machine never publishes, whatever the config says
    ("prod", False, "publish", "draft"),
    ("prod", True, "draft", "draft"),
    ("prod", True, "publish", "publish"),
])
def test_next_job_mode(tmp_path, role, autopublish, decision, mode):
    s = _settings(tmp_path, role=role, autopublish=autopublish)
    db = DB(s.path("db"))
    _ready_item(db, decision=decision)
    assert runner.next_job(s, db, ["poshmark"], False)[3] == mode


def test_next_job_hold_skips_publish_but_still_returns_drafts(tmp_path):
    """HOLD_UNSHIPPED (allow_publish=False) holds only what would go live; the held item stays 'ready' behind it."""
    s = _settings(tmp_path, role="prod", autopublish=True)
    db = DB(s.path("db"))
    live = _ready_item(db, seq=1, decision="publish")
    draft = _ready_item(db, seq=2, decision="draft")
    assert runner.next_job(s, db, ["poshmark"], False)[0] == live               # no hold: the publish goes first
    job = runner.next_job(s, db, ["poshmark"], False, allow_publish=False)
    assert job is not None and (job[0], job[3]) == (draft, "draft")
    db.upsert_post(draft, "poshmark", status="drafted")
    assert runner.next_job(s, db, ["poshmark"], False, allow_publish=False) is None
    assert db.item(live)["status"] == "ready"


def test_next_job_hold_does_not_stop_a_dry_run(tmp_path):
    s = _settings(tmp_path, role="prod", autopublish=True)
    db = DB(s.path("db"))
    live = _ready_item(db, decision="publish")
    job = runner.next_job(s, db, ["poshmark"], True, allow_publish=False)
    assert job is not None and (job[0], job[3]) == (live, "publish")           # a dry-run never submits


# ---------------------------------------------------------------- run()

class FakePW:
    async def stop(self):
        pass


class FakeCtx:
    async def close(self):
        pass


class StubPoster:
    """Only what the loop calls. `results` is consumed one per post(); an Exception instance is raised."""
    name = "poshmark"

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    async def post(self, ctx, r, mode, dry_run, shots):
        self.calls.append((r.sku, mode, dry_run))
        res = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(res, Exception):
            raise res
        if res.screenshot is None:
            shots.mkdir(parents=True, exist_ok=True)
            res.screenshot = str(shots / f"{r.sku}.png")
            Path(res.screenshot).write_bytes(b"png")
        return res


@pytest.fixture
def harness(monkeypatch):
    """Browser, Telegram, pacing checks and the between-item sleep stubbed; returns the message log."""
    said: list[str] = []
    pauses: list[float] = []

    async def fake_open_browser(profile_dir, timezone_id):
        return FakePW(), FakeCtx()

    async def fake_pause(stop, seconds):
        pauses.append(seconds)
        if len(pauses) > 20:
            raise AssertionError("the loop never stopped")

    monkeypatch.setattr(runner, "open_browser", fake_open_browser)
    monkeypatch.setattr(runner, "can_post", lambda s, hour, day: (True, "ok"))
    monkeypatch.setattr(runner, "_pause", fake_pause)
    monkeypatch.setattr(notify, "say", lambda text: said.append(text))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said.append(caption))
    return said, pauses


def _run(monkeypatch, s, db, poster, **kw):
    monkeypatch.setattr(runner, "posters", lambda s: {"poshmark": poster})
    kw.setdefault("allow_dev_browser", True)                  # the (stubbed) browser on dev is the point of these tests
    asyncio.run(runner.run(s, db, **kw))


def test_run_refuses_to_open_the_browser_on_dev_without_the_flag(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path)                                   # role=dev
    db = DB(s.path("db"))
    _ready_item(db)
    poster = StubPoster(Outcome("dryrun"))
    opened: list[tuple] = []

    async def spying_open_browser(profile_dir, timezone_id):
        opened.append((profile_dir, timezone_id))
        return FakePW(), FakeCtx()

    monkeypatch.setattr(runner, "open_browser", spying_open_browser)
    with pytest.raises(RuntimeError, match="machine_role is 'dev'.*--allow-dev-browser.*stays a dry-run"):
        _run(monkeypatch, s, db, poster, once=True, allow_dev_browser=False)
    assert opened == [] and poster.calls == []

    _run(monkeypatch, s, db, poster, once=True, allow_dev_browser=True)
    assert len(opened) == 1 and poster.calls == [("i_1", "draft", True)]       # still a dry-run


def test_run_prod_needs_no_dev_browser_flag(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod", dry_run=True)
    db = DB(s.path("db"))
    _ready_item(db)
    poster = StubPoster(Outcome("dryrun"))
    monkeypatch.setattr(runner, "posters", lambda s: {"poshmark": poster})
    asyncio.run(runner.run(s, db, once=True))
    assert poster.calls == [("i_1", "draft", True)]


def test_run_records_a_posted_outcome(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/abc"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [("i_1", "publish", False)]
    row = db.post(iid, "poshmark")
    assert (row["status"], row["attempts"], row["mode"]) == ("posted", 1, "publish")
    assert row["url"] == "https://poshmark.com/listing/abc"
    assert row["posted_at"] and db.posted_since("2000-01-01T00:00:00+00:00") == 1
    assert db.item(iid)["status"] == "posted"
    assert any("posted on poshmark" in m for m in said) and not s.flag("PAUSE").exists()
    assert [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE ref=?", (iid,))] == ["post_posted"]


def test_run_records_a_drafted_outcome(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=False, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(Outcome("drafted", url="https://poshmark.com/listing/draft1"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [("i_1", "draft", False)]
    row = db.post(iid, "poshmark")
    assert (row["status"], row["mode"], row["url"]) == ("drafted", "draft", "https://poshmark.com/listing/draft1")
    assert row["posted_at"] and db.posted_since("2000-01-01T00:00:00+00:00") == 1
    assert db.item(iid)["status"] == "drafted"                # not 'posted': nothing is live yet
    assert any("drafted on poshmark" in m for m in said)


def test_run_hold_unshipped_holds_publish_and_says_so_when_idle(tmp_path, monkeypatch, harness, capsys):
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db, decision="publish")
    s.flag("HOLD_UNSHIPPED").touch()
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/abc"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [] and db.post(iid, "poshmark") is None and db.item(iid)["status"] == "ready"
    assert "nothing to do (unshipped orders — publish held (drafts and dry-runs still run))" in capsys.readouterr().out

    s.flag("HOLD_UNSHIPPED").unlink()                         # shipped: the same item now goes live
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == [("i_1", "publish", False)] and db.item(iid)["status"] == "posted"


def test_run_dry_run_is_stamped_and_counts_toward_pacing(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path)                                   # dev → dry-run whatever the config says
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(Outcome("dryrun"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [("i_1", "draft", True)]
    row = db.post(iid, "poshmark")
    assert row["status"] == "dryrun" and row["posted_at"]     # it hit the real form: the caps must see it
    assert db.posted_since("2000-01-01T00:00:00+00:00") == 1
    assert db.item(iid)["status"] == "ready"
    assert loads(db.conn.execute("SELECT detail FROM events WHERE kind='post_dryrun'").fetchone()[0])["mp"] == "poshmark"


def test_run_pauses_after_consecutive_failures(tmp_path, monkeypatch, harness):
    said, pauses = harness
    s = _settings(tmp_path, max_fail=3)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 6)]
    poster = StubPoster(Outcome("failed", error="Mismatch: form doesn't match plan: {'price': (85, '80')}"))
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 3
    assert [db.post(i, "poshmark")["status"] for i in ids[:3]] == ["failed"] * 3
    assert all(db.post(i, "poshmark") is None for i in ids[3:])
    assert all(db.item(i)["status"] == "ready" for i in ids)
    pause = s.flag("PAUSE").read_text(encoding="utf-8")
    assert "3 consecutive failures" in pause and "Mismatch" in pause
    assert sum("paused after 3 consecutive failures" in m for m in said) == 1
    assert len(pauses) == 2                                   # slept between items, not after the halt


def test_run_failure_counter_resets_on_success(tmp_path, monkeypatch, harness):
    _, pauses = harness
    s = _settings(tmp_path, max_fail=2)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 5)]
    poster = StubPoster(Outcome("failed", error="a"), Outcome("dryrun"), Outcome("failed", error="b"),
                        Outcome("dryrun"))
    stop_after = {"n": 0}

    async def stop_on_fourth_pause(stop, seconds):
        stop_after["n"] += 1
        if stop_after["n"] >= 4:
            stop.set()

    monkeypatch.setattr(runner, "_pause", stop_on_fourth_pause)
    _run(monkeypatch, s, db, poster, once=False)

    assert [db.post(i, "poshmark")["status"] for i in ids] == ["failed", "dryrun", "failed", "dryrun"]
    assert not s.flag("PAUSE").exists()                       # never two failures in a row


def test_run_unexpected_exception_requeues_and_pauses(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(RuntimeError("kaboom"))
    _run(monkeypatch, s, db, poster, once=True)

    row = db.post(iid, "poshmark")
    assert row["status"] == "queued" and "RuntimeError: kaboom" in row["last_error"] and row["attempts"] == 1
    assert "kaboom" in s.flag("PAUSE").read_text(encoding="utf-8")
    assert sum("Poster paused" in m for m in said) == 1
    assert db.item(iid)["status"] == "ready"


def test_run_account_blocked_requeues_and_pauses(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 3)]
    poster = StubPoster(AccountBlocked("not logged in to Poshmark in the poster profile"))
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 1                              # the whole poster stops, not just this item
    assert db.post(ids[0], "poshmark")["status"] == "queued" and db.post(ids[1], "poshmark") is None
    assert "not logged in" in s.flag("PAUSE").read_text(encoding="utf-8")
    assert sum("Poster paused" in m for m in said) == 1


QUESTION = "Poshmark's brand list has no match for 'Tory Burch'. Which brand should I pick? (reply e.g. 'brand Vince')"


def test_run_needs_owner_parks_the_item_and_asks_without_pausing(tmp_path, monkeypatch, harness):
    """The poster's separate question: item A waits in needs_owner, the owner is asked once, item B still posts."""
    said, pauses = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False, max_fail=1)
    db = DB(s.path("db"))
    a, b = _ready_item(db, seq=1), _ready_item(db, seq=2)
    poster = StubPoster(NeedsOwner(QUESTION), Outcome("posted", url="https://poshmark.com/listing/b"))
    asked: list[tuple] = []
    monkeypatch.setattr(approve, "ask_owner", lambda s_, db_, iid, q: asked.append((iid, q)))

    async def stop_on_second_pause(stop, seconds):
        pauses.append(seconds)
        if len(pauses) >= 2:
            stop.set()

    monkeypatch.setattr(runner, "_pause", stop_on_second_pause)
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 2                              # B was not held up by A's question
    row = db.post(a, "poshmark")
    assert (row["status"], row["attempts"], row["last_error"]) == ("queued", 1, f"needs owner: {QUESTION}")
    assert row["posted_at"] is None and db.item(a)["status"] == "needs_owner"
    assert asked == [(a, QUESTION)]
    assert db.post(b, "poshmark")["status"] == "posted" and db.item(b)["status"] == "posted"
    assert not s.flag("PAUSE").exists() and not any("paused" in m.lower() for m in said)   # max_fail=1: not a failure
    events = db.conn.execute("SELECT kind, detail FROM events WHERE ref=?", (a,)).fetchall()
    assert [(e["kind"], loads(e["detail"])) for e in events] == [("needs_owner", {"mp": "poshmark", "question": QUESTION})]
    assert len(pauses) == 2                                    # the usual gap after the question, then after B


def test_run_needs_owner_leaves_the_failure_counter_alone(tmp_path, monkeypatch, harness):
    """Neither a failure nor a success for the circuit breaker: failed, question, failed still trips max_fail=2."""
    s = _settings(tmp_path, max_fail=2)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 4)]
    poster = StubPoster(Outcome("failed", error="a"), NeedsOwner("which brand?"), Outcome("failed", error="b"))
    monkeypatch.setattr(approve, "ask_owner", lambda *a: None)
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 3
    assert [db.post(i, "poshmark")["status"] for i in ids] == ["failed", "queued", "failed"]
    assert [db.item(i)["status"] for i in ids] == ["ready", "needs_owner", "ready"]
    assert "2 consecutive failures: b" in s.flag("PAUSE").read_text(encoding="utf-8")


def test_run_once_returns_after_a_needs_owner_question(tmp_path, monkeypatch, harness):
    _, pauses = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 3)]
    poster = StubPoster(NeedsOwner("which brand?"), Outcome("dryrun"))
    monkeypatch.setattr(approve, "ask_owner", lambda *a: None)
    _run(monkeypatch, s, db, poster, once=True)

    assert len(poster.calls) == 1 and pauses == [] and db.item(ids[0])["status"] == "needs_owner"
    assert db.post(ids[1], "poshmark") is None and not s.flag("PAUSE").exists()


def test_run_skips_a_job_another_poster_claimed(tmp_path, monkeypatch, harness):
    """SELECT-then-UPDATE let two poster processes take the same item; claim_post is the lock."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 3)]
    poster = StubPoster(Outcome("dryrun"))
    real_claim = db.claim_post

    def racing_claim(iid, mp, mode):
        if iid == ids[0]:
            db.upsert_post(iid, mp, status="posting")          # the other process got there first
        return real_claim(iid, mp, mode)

    monkeypatch.setattr(db, "claim_post", racing_claim)
    _run(monkeypatch, s, db, poster, once=True)
    assert len(poster.calls) == 1                              # the claimed item was never opened by us
    assert db.post(ids[0], "poshmark")["status"] == "posting" and db.post(ids[1], "poshmark")["status"] == "dryrun"


def test_run_stop_finishes_the_current_item_then_exits(tmp_path, monkeypatch, harness):
    """SIGTERM sets `stop`; the loop must finish the item in hand and exit at the top of the next iteration."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 3)]
    poster = StubPoster(Outcome("dryrun"))

    async def stop_during_gap(stop, seconds):
        stop.set()

    monkeypatch.setattr(runner, "_pause", stop_during_gap)
    _run(monkeypatch, s, db, poster, once=False)
    assert len(poster.calls) == 1
    assert db.post(ids[0], "poshmark")["status"] == "dryrun" and db.post(ids[1], "poshmark") is None


def test_pause_helper_wakes_on_stop():
    async def go():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, stop.set)
        t0 = loop.time()
        await runner._pause(stop, 30)
        assert loop.time() - t0 < 5
        await runner._pause(stop, 0.01)                        # already set: returns at once
        stop.clear()
        await runner._pause(stop, 0.01)                        # times out quietly
    asyncio.run(go())
