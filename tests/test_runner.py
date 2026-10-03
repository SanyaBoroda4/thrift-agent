"""The poster loop with the browser and Telegram stubbed: job selection, recording, the circuit breaker, and stop."""
import asyncio
from pathlib import Path

import pytest

from thrift_agent import approve, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post import runner
from thrift_agent.post.base import AccountBlocked, NeedsOwner, Outcome, PosterError
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.schema import Render

RENDER = Render(marketplace="poshmark", title="Tory Burch Red Flats size 7.5", description="Red flats.", brand="Tory Burch",
                department="Women", category="Shoes", subcategory=None, size="7.5", colors=["Red"], condition="excellent",
                price=85, photos=[], sku="i_1")


def _settings(tmp_path, role="dev", autopublish=False, dry_run=True, max_fail=3, username="closet", depop=False,
              confirmed=None) -> Settings:
    """Built from scratch, not from config/settings.yaml: the tests must not depend on the checked-in file."""
    paths = {k: str(tmp_path / k) for k in ("inbox", "work", "archive", "failed", "chrome_profile", "control")}
    s = Settings({
        "machine_role": role,
        "paths": {**paths, "db": str(tmp_path / "state.db")},
        # A test that turns dry_run off means "live" unless it says otherwise: the second key goes with it.
        "poster": {"dry_run": dry_run, "max_consecutive_failures": max_fail,
                   "autopublish_confirmed": (not dry_run) if confirmed is None else confirmed},
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
        self.results, self.calls, self.stages = list(results), [], []

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        self.calls.append((r.sku, mode, dry_run))
        self.stages.append(stage)
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


def test_run_passes_the_dry_run_stage_and_reports_the_note(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    note = "review page recorded in i_1-review.json; style tags Poshmark doesn't offer, left out: sparkly"
    poster = StubPoster(Outcome("dryrun", note=note))
    _run(monkeypatch, s, db, poster, once=True, stage="review")
    assert poster.stages == ["review"]
    assert any(m.startswith("🧪 dry-run poshmark (review): ") and m.endswith(note) for m in said)
    detail = loads(db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='post_dryrun'", (iid,)).fetchone()[0])
    assert detail["note"] == note

    s.data["poster"]["dry_run_stage"] = "review"                 # the config's stage when the CLI gives none
    assert runner.dry_run_stage(s) == "review" and runner.dry_run_stage(s, "FORM") == "form"


def test_run_refuses_an_unknown_stage_before_opening_the_browser(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path)
    s.data["poster"]["dry_run_stage"] = "publish"
    db = DB(s.path("db"))
    _ready_item(db)
    poster = StubPoster(Outcome("dryrun"))
    opened = []

    async def spying_open_browser(profile_dir, timezone_id):
        opened.append(profile_dir)
        return FakePW(), FakeCtx()

    monkeypatch.setattr(runner, "open_browser", spying_open_browser)
    with pytest.raises(ValueError, match="dry_run_stage must be one of form, review"):
        _run(monkeypatch, s, db, poster, once=True)
    assert opened == [] and poster.calls == []


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


def test_run_warns_when_a_dry_run_left_a_draft(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    left = "a draft was left behind (Drafts 0 → 1)"
    poster = StubPoster(Outcome("dryrun", note=left, draft_left=left))
    _run(monkeypatch, s, db, poster, once=True)
    warning = [m for m in said if m.startswith("⚠️ poshmark: a draft was left behind")]
    assert warning == [f"⚠️ poshmark: {left} by the dry-run of {iid}. Delete it in the closet's Drafts; the "
                       "leave step (Cancel → Discard Changes) needs a look."]
    detail = loads(db.conn.execute("SELECT detail FROM events WHERE kind='post_dryrun'").fetchone()[0])
    assert detail["draft_left"] == left


def test_run_warns_about_a_left_draft_after_a_question_too(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _ready_item(db)
    question = NeedsOwner("which brand?")
    question.draft_left = "a draft was left behind (Drafts 2 → 3)"
    monkeypatch.setattr(approve, "ask_owner", lambda *a: None)
    _run(monkeypatch, s, db, StubPoster(question), once=True)
    assert any("a draft was left behind (Drafts 2 → 3) by the dry-run of" in m for m in said)


# ---------------------------------------------------------------- WO15: the supervised first publish

def _approved(db: DB, price: int = 85) -> str:
    iid = _ready_item(db)
    db.set_item(iid, owner_price=price)
    return iid


def _first(monkeypatch, s, db, poster, iid, confirm="confirm"):
    monkeypatch.setattr(runner, "posters", lambda s_: {"poshmark": poster})
    return asyncio.run(runner.publish_first(s, db, iid, confirm=confirm))


def test_publish_first_runs_on_the_mac_only(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="dev")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x"))
    with pytest.raises(RuntimeError, match="runs on the Mac only"):
        _first(monkeypatch, s, db, poster, iid)
    assert poster.calls == [] and db.post(iid, "poshmark") is None


@pytest.mark.parametrize("setup,why", [
    (lambda db, iid: db.set_item(iid, owner_price=None), "no owner-approved price"),
    (lambda db, iid: db.set_item(iid, owner_price=80), "no owner-approved price"),            # 85 on the listing
    (lambda db, iid: db.set_item(iid, status="awaiting_price"), "is awaiting_price, not ready"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="posting"), "it reached the site"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="failed", url="https://poshmark.com/listing/x"),
     "it reached the site"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="failed",
                                    last_error=runner.UNCONFIRMED + "no address"), "may have gone live"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="failed", last_error="Mismatch"),
     r"thrift requeue i_\w+` first"),
])
def test_publish_first_refuses_what_it_must_not_publish(tmp_path, monkeypatch, harness, setup, why):
    s = _settings(tmp_path, role="prod", dry_run=True)
    db = DB(s.path("db"))
    iid = _approved(db)
    setup(db, iid)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x"))
    with pytest.raises(ValueError, match=why):
        _first(monkeypatch, s, db, poster, iid)
    assert poster.calls == []


def test_publish_first_respects_the_shipping_hold_and_the_hours(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x"))
    s.flag("HOLD_UNSHIPPED").touch()
    with pytest.raises(ValueError, match="publish held"):
        _first(monkeypatch, s, db, poster, iid)
    s.flag("HOLD_UNSHIPPED").unlink()
    monkeypatch.setattr(runner, "can_post", lambda s_, hour, day: (False, "outside posting hours"))
    with pytest.raises(ValueError, match="not now: outside posting hours"):
        _first(monkeypatch, s, db, poster, iid)
    assert poster.calls == [] and db.post(iid, "poshmark") is None


def test_publish_first_publishes_once_and_records_the_listing(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod", dry_run=True)                # poster.dry_run is ignored for this call
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/naturino-6ad", clicked=True))
    out = _first(monkeypatch, s, db, poster, iid, confirm="the LIST prompt")
    assert out.status == "posted" and poster.calls == [("i_1", "publish", False)]
    assert poster.confirm == "the LIST prompt"
    row = db.post(iid, "poshmark")
    assert (row["status"], row["mode"], row["url"]) == ("posted", "publish", "https://poshmark.com/listing/naturino-6ad")
    assert db.item(iid)["status"] == "posted" and any(m.startswith("✅ posted on poshmark") for m in said)


def test_publish_first_cancelled_at_the_prompt_goes_back_to_the_queue(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("cancelled", note="not published: LIST wasn't typed"))
    assert _first(monkeypatch, s, db, poster, iid).status == "cancelled"
    row = db.post(iid, "poshmark")
    assert (row["status"], row["url"], row["posted_at"]) == ("queued", None, None)
    assert db.item(iid)["status"] == "ready"


def test_a_publish_that_clicked_but_found_no_listing_is_never_requeued(tmp_path, monkeypatch, harness):
    from thrift_agent import pipeline
    said, _ = harness
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("failed", error="PosterError: after List This Item no listing address", clicked=True))
    _first(monkeypatch, s, db, poster, iid)
    row = db.post(iid, "poshmark")
    assert row["status"] == "failed" and row["last_error"].startswith(runner.UNCONFIRMED) and row["url"] is None
    assert any("unconfirmed publish: PosterError: after List This Item" in m for m in said)    # the Telegram ping
    with pytest.raises(ValueError, match="may be live"):
        pipeline.requeue(s, db, iid)                                       # invariant 4: reconcile by hand
    with pytest.raises(ValueError, match="may have gone live"):
        _first(monkeypatch, s, db, poster, iid)


def test_the_list_prompt_takes_only_list(monkeypatch):
    for typed, ok in (("LIST", True), (" LIST ", True), ("list", False), ("", False), ("yes", False)):
        monkeypatch.setattr("builtins.input", lambda prompt, typed=typed: typed)
        assert asyncio.run(runner.terminal_confirm(RENDER, "panel")) is ok, typed


def test_publish_first_with_chrome_still_held_by_the_poster_service_claims_nothing(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)

    async def profile_in_use(profile_dir, timezone_id):
        raise RuntimeError("ProcessSingleton: the profile directory is already in use")
    monkeypatch.setattr(runner, "open_browser", profile_in_use)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x"))
    with pytest.raises(RuntimeError, match="services.sh stop"):
        _first(monkeypatch, s, db, poster, iid)
    assert poster.calls == [] and db.post(iid, "poshmark") is None          # never left in 'posting'


def test_publish_first_leaves_the_item_ready_while_another_marketplace_still_waits(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x", clicked=True))
    monkeypatch.setattr(runner, "posters", lambda s_: {"poshmark": poster, "depop": StubPoster(Outcome("posted"))})
    asyncio.run(runner.publish_first(s, db, iid, confirm="confirm"))
    assert db.post(iid, "poshmark")["status"] == "posted" and db.item(iid)["status"] == "ready"


# ---------------------------------------------------------------- WO16: two keys, and mark-posted

@pytest.mark.parametrize("dry_run,confirmed,live,why", [
    (False, False, False, "poster.autopublish_confirmed is off (poster.dry_run alone doesn't publish)"),
    (True, True, False, "poster.dry_run is on"),
    (False, True, True, None),
])
def test_the_loop_publishes_only_with_dry_run_off_and_autopublish_confirmed(tmp_path, monkeypatch, harness, dry_run,
                                                                            confirmed, live, why):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=dry_run, confirmed=confirmed)
    db = DB(s.path("db"))
    _ready_item(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x") if live else Outcome("dryrun"))
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == [("i_1", "publish", not live)]
    started = next(m for m in said if m.startswith("Poster started"))
    assert ("LIVE" in started) is live and (why is None or why in started), started


LIVE_URL = "https://poshmark.com/listing/Tory-Burch-Red-Flats-size-75-6ac111490000000000000a01"


class PageCtx(FakeCtx):
    async def new_page(self):
        return object()          # keep_evidence swallows what a stand-in page can't do


def _seeing(shows=True) -> PoshmarkPoster:
    """The real address check; the live page stubbed: it shows the item, or it doesn't."""
    p = PoshmarkPoster("closet")

    async def verify_live(page, url, r):
        p.seen = (url, r.title, r.price)
        if not shows:
            raise PosterError(f"live page at {url} doesn't show the title")
    p.verify_live = verify_live
    return p


def _unconfirmed(db: DB, iid: str) -> None:
    db.upsert_post(iid, "poshmark", status="failed", mode="publish",
                   last_error=runner.UNCONFIRMED + "PosterError: after List This Item no listing address")


def _mark(monkeypatch, s, db, iid, url=LIVE_URL, poster=None):
    poster = poster or _seeing()

    async def open_browser(profile_dir, timezone_id):
        return FakePW(), PageCtx()
    monkeypatch.setattr(runner, "posters", lambda s_: {"poshmark": poster})
    monkeypatch.setattr(runner, "open_browser", open_browser)
    return asyncio.run(runner.mark_posted(s, db, iid, "poshmark", url)), poster


def test_mark_posted_records_a_listing_found_by_hand(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    _unconfirmed(db, iid)
    before = db.post(iid, "poshmark")
    address, poster = _mark(monkeypatch, s, db, iid, url=LIVE_URL + "?utm_source=share")
    assert address == LIVE_URL and poster.seen == (LIVE_URL, RENDER.title, 85)
    row = db.post(iid, "poshmark")
    assert (row["status"], row["url"], row["last_error"]) == ("posted", LIVE_URL, None)
    assert row["posted_at"] == before["updated_at"]                     # when it went live, as near as known
    assert db.item(iid)["status"] == "posted"
    assert f"✅ confirmed live on poshmark: {RENDER.title} — $85\n{LIVE_URL}" in said
    assert "post_confirmed" in [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE ref=?", (iid,))]
    assert list((s.path("failed") / "shots").glob(f"{iid}-poshmark-*-confirm.json"))   # the evidence of the check


@pytest.mark.parametrize("setup,url,why", [
    (lambda db, iid: None, LIVE_URL, r"only a post in 'unconfirmed publish' can be marked posted \(i_\w+ has no post\)"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="failed", last_error="Mismatch: title"), LIVE_URL,
     "only a post in 'unconfirmed publish'"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="posted", url=LIVE_URL), LIVE_URL,
     "has status posted with https://poshmark.com/listing/"),
    (lambda db, iid: db.upsert_post(iid, "poshmark", status="dryrun"), LIVE_URL, "has status dryrun"),
    (_unconfirmed, "https://poshmark.com/closet/someone", "not a poshmark listing address"),
    (_unconfirmed, "https://poshmark.com/listing/x-6ac11149", "not a poshmark listing address"),
])
def test_mark_posted_refuses_anything_else(tmp_path, monkeypatch, harness, setup, url, why):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    setup(db, iid)
    before = dict(db.post(iid, "poshmark") or {})
    with pytest.raises(ValueError, match=why):
        _mark(monkeypatch, s, db, iid, url=url)
    assert dict(db.post(iid, "poshmark") or {}) == before                  # nothing changed


def test_mark_posted_refuses_an_address_another_item_holds_or_a_page_without_the_item(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid, other = _approved(db), _ready_item(db, seq=2)
    _unconfirmed(db, iid)
    db.upsert_post(other, "poshmark", status="posted", url=LIVE_URL)
    with pytest.raises(ValueError, match=f"already recorded for item {other}"):
        _mark(monkeypatch, s, db, iid)
    db.upsert_post(other, "poshmark", url="https://poshmark.com/listing/y-6ac111490000000000000a02")
    with pytest.raises(ValueError, match="doesn't show this item .*nothing changed"):
        _mark(monkeypatch, s, db, iid, poster=_seeing(shows=False))
    row = db.post(iid, "poshmark")
    assert row["status"] == "failed" and row["last_error"].startswith(runner.UNCONFIRMED) and row["url"] is None
    assert not any("confirmed live" in m for m in said)


def test_mark_posted_runs_on_the_mac_only(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="dev")
    db = DB(s.path("db"))
    iid = _approved(db)
    _unconfirmed(db, iid)
    with pytest.raises(RuntimeError, match="runs on the Mac only"):
        _mark(monkeypatch, s, db, iid)


def test_publish_first_refuses_a_listing_written_before_the_condition_rule(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    old = RENDER.model_copy(update={"description": "Red flats. Excellent used condition, light wear on soles."})
    db.set_item(iid, renders={"poshmark": old.model_dump()})
    poster = StubPoster(Outcome("posted", url=LIVE_URL))
    with pytest.raises(ValueError, match=r"written before the condition rule \(excellent, light wear\): reprocess it "
                                         rf"first — thrift answer {iid} \"recheck\""):
        _first(monkeypatch, s, db, poster, iid)
    assert poster.calls == [] and db.post(iid, "poshmark") is None
    assert runner.condition_rule_breaks(RENDER) == []
