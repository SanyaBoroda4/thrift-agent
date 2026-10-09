"""The poster loop with the browser and Telegram stubbed: job selection, recording, the circuit breaker, and stop."""
import asyncio
import json
from pathlib import Path

import pytest

from cards import ALL_DONE, card
from thrift_agent import daily, notify, pipeline, power
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post import runner
from thrift_agent.post.base import AccountBlocked, Outcome, PosterError
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.depop_api import DepopApiPoster
from thrift_agent.post.ext_driver import ExtensionPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.schema import Render

NAME = "Tory Burch Red Flats"                    # WO34: the card's short name
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


def _ready_item(db: DB, seq: int = 1, decision: str = "publish", price: int | None = 85,
                reasons: list[str] | None = None) -> str:
    """A ready item as the pipeline leaves one: the owner approved the listing's price ($85) unless `price` says
    otherwise (None: never approved)."""
    bid = db.add_batch(f"share_{seq}", 5)
    db.set_batch(bid, status="split")                         # its items exist: the share was split
    iid = db.add_item(bid, seq, f"work/{seq}")
    db.set_item(iid, status="ready", gate={"decision": decision, "reasons": reasons or []},
                renders={"poshmark": RENDER.model_dump()}, owner_price=price)
    return iid


# ---------------------------------------------------------------- posters

def test_posters_builds_one_per_enabled_marketplace(tmp_path):
    """WO32: Depop and Vinted go through the extension by default; WO30's Playwright poster on request."""
    ps = runner.posters(_settings(tmp_path, depop=True))
    assert isinstance(ps["poshmark"], PoshmarkPoster) and isinstance(ps["depop"], ExtensionPoster)
    assert ps["depop"].driver == "extension" and ps["depop"].name == "depop"
    assert list(runner.posters(_settings(tmp_path, depop=False))) == ["poshmark"]
    s = _settings(tmp_path, depop=True)
    s.data["marketplaces"]["depop"]["driver"] = "playwright"
    assert isinstance(runner.posters(s)["depop"], DepopPoster)
    s.data["marketplaces"]["depop"]["driver"] = "api"
    assert isinstance(runner.posters(s)["depop"], DepopApiPoster)
    s.data["marketplaces"]["depop"]["driver"] = "selenium"
    with pytest.raises(ValueError, match="marketplaces.depop.driver must be one of extension, playwright, api"):
        runner.posters(s)


@pytest.mark.parametrize("username", ["", "   ", None])
def test_posters_require_a_username_for_poshmark(tmp_path, username):
    s = _settings(tmp_path, depop=True)
    s.data["marketplaces"]["poshmark"]["username"] = username
    with pytest.raises(ValueError, match=r"marketplaces\.poshmark\.username is empty .* private/settings\.yaml"):
        runner.posters(s)
    s.data["marketplaces"]["poshmark"]["enabled"] = False         # a disabled marketplace needs no username
    assert "poshmark" not in runner.posters(s)


def test_depop_and_vinted_need_no_shop_name(tmp_path):
    """WO30: their shop name only serves the shop check after an interrupted publish."""
    s = _settings(tmp_path, depop=True)
    s.data["marketplaces"]["depop"]["username"] = ""
    s.data["marketplaces"]["vinted"] = {"enabled": True}
    assert set(runner.posters(s)) == {"poshmark", "depop", "vinted"}


# ---------------------------------------------------------------- next_job

@pytest.mark.parametrize("status,dry,expect_job", [
    (None, False, True), ("queued", False, True), ("dryrun", False, True),
    ("dryrun", True, False),                                             # already dry-run once: don't repeat it
    ("posting", False, False), ("failed", False, False), ("posted", False, False), ("drafted", False, False),
])
def test_next_job_status_matrix(tmp_path, status, dry, expect_job):
    s = _settings(tmp_path, role="prod", autopublish=True)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    if status:
        db.upsert_listing(iid, "poshmark", status=status)
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


@pytest.mark.parametrize("role,autopublish,decision,dry,mode", [
    ("dev", True, "publish", True, "draft"),        # the dev machine never publishes, whatever the config says
    ("prod", False, "publish", True, "draft"),
    ("prod", True, "draft", True, "publish"),       # WO33: the gate holds nothing — a dry run of the publish
    ("prod", True, "publish", True, "publish"),
    ("prod", True, "publish", False, "publish"),
    ("prod", True, "draft", False, "publish"),      # WO33: live, a 'draft' gate publishes too (never held)
    ("prod", False, "publish", False, None),
])
def test_next_job_mode(tmp_path, role, autopublish, decision, dry, mode):
    s = _settings(tmp_path, role=role, autopublish=autopublish)
    db = DB(s.path("db"))
    _ready_item(db, decision=decision)
    job = runner.next_job(s, db, ["poshmark"], dry)
    assert (job[3] if job else None) == mode


@pytest.mark.parametrize("price", [None, 0, 80])                     # never approved; approved at another price
def test_next_job_takes_only_the_owners_approved_price(tmp_path, price):
    """WO27: nothing publishes without the approved price — the listing's price must be the one the owner approved."""
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    _ready_item(db, price=price)
    assert runner.next_job(s, db, ["poshmark"], False) is None and runner.next_job(s, db, ["poshmark"], True) is None
    assert runner.held(s, db, ["poshmark"]) == []                   # not held either: simply not approved


def test_next_job_hold_skips_publish_and_a_draft_gate_is_a_publish(tmp_path, monkeypatch):
    """HOLD_UNSHIPPED (allow_publish=False) holds what would go live; the items stay 'ready' behind it. WO33: a
    'draft' gate is a publish like any other (the copy check never holds), so the hold keeps it too."""
    monkeypatch.setattr(runner, "can_draft", lambda mp: True)
    s = _settings(tmp_path, role="prod", autopublish=True)
    db = DB(s.path("db"))
    live = _ready_item(db, seq=1, decision="publish")
    gated = _ready_item(db, seq=2, decision="draft")
    assert runner.next_job(s, db, ["poshmark"], False)[0] == live               # no hold: the oldest first
    db.upsert_listing(live, "poshmark", status="posted", url="https://poshmark.com/listing/x")
    assert runner.next_job(s, db, ["poshmark"], False)[0:4:3] == (gated, "publish")
    assert runner.next_job(s, db, ["poshmark"], False, allow_publish=False) is None
    assert db.item(gated)["status"] == "ready"


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


class Said(list):
    """Every message, in order; `.group` holds the ones sent to the GROUP (WO29: everything else is the ops chat)."""

    def __init__(self):
        super().__init__()
        self.group = []

    def to_group(self, text):
        self.append(text)
        self.group.append(text)


@pytest.fixture
def harness(monkeypatch):
    """Browser, Telegram, pacing checks and the between-item sleep stubbed; returns the message log."""
    said = Said()
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
    monkeypatch.setattr(notify, "group", said.to_group)
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: said.to_group(caption))
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
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["attempts"]) == ("posted", 1)
    assert row["url"] == "https://poshmark.com/listing/abc"
    assert row["posted_at"] and db.listed_since("2000-01-01T00:00:00+00:00") == 1
    assert db.item(iid)["status"] == "posted"
    assert said.group == [card(NAME, 85, [("poshmark", "https://poshmark.com/listing/abc")], ALL_DONE)]   # WO34
    assert not s.flag("PAUSE").exists()
    assert [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE ref=?", (iid,))] == [
        "post_posted", "posted_announced"]                    # its one "Posted ✓" line (WO30) was said


def test_the_posted_message_lists_the_posters_guesses(tmp_path, monkeypatch, harness):
    """WO27: the poster never asks; what it had to guess is listed so the owner can fix the live listing by hand."""
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    guesses = ["brand set to 'J. Crew' (from 'J.Crew')", "size set to '12' (from '12.5')"]
    _run(monkeypatch, s, db, StubPoster(Outcome("posted", url="https://poshmark.com/listing/abc", guesses=guesses)),
         once=True)
    assert said.group == [card(NAME, 85, [("poshmark", "https://poshmark.com/listing/abc")], ALL_DONE)]   # WO34:
    assert [m for m in said if m.startswith("Check —")] == [                       # the notes go to the ops chat, once
        f"Check — {RENDER.title}:\n- Poshmark: brand set to 'J. Crew' (from 'J.Crew')\n"
        "- Poshmark: size set to '12' (from '12.5')"]
    detail = loads(db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='post_posted'", (iid,)).fetchone()[0])
    assert detail["guesses"] == guesses


def test_run_records_a_drafted_outcome(tmp_path, monkeypatch, harness):
    """Once the poster can save a draft (draft_saved is still UNVERIFIED, so can_draft is patched here)."""
    monkeypatch.setattr(runner, "can_draft", lambda mp: True)
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=False, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(Outcome("drafted", url="https://poshmark.com/listing/draft1"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [("i_1", "draft", False)]
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["url"]) == ("drafted", "https://poshmark.com/listing/draft1")
    assert row["posted_at"] and db.listed_since("2000-01-01T00:00:00+00:00") == 1
    assert db.item(iid)["status"] == "drafted"                # not 'posted': nothing is live yet
    assert any("drafted on poshmark" in m for m in said)


def test_run_hold_unshipped_holds_publish_and_says_so_when_idle(tmp_path, monkeypatch, harness, capsys):
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db, decision="publish")
    s.flag("HOLD_UNSHIPPED").touch()
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/abc"))
    _run(monkeypatch, s, db, poster, once=True)

    assert poster.calls == [] and db.listing(iid, "poshmark") is None and db.item(iid)["status"] == "ready"
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
    row = db.listing(iid, "poshmark")
    assert row["status"] == "dryrun" and row["posted_at"]     # it hit the real form: the caps must see it
    assert db.listed_since("2000-01-01T00:00:00+00:00") == 1
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
    assert [db.listing(i, "poshmark")["status"] for i in ids[:3]] == ["failed"] * 3
    assert all(db.listing(i, "poshmark") is None for i in ids[3:])
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

    assert [db.listing(i, "poshmark")["status"] for i in ids] == ["failed", "dryrun", "failed", "dryrun"]
    assert not s.flag("PAUSE").exists()                       # never two failures in a row


def test_run_unexpected_exception_requeues_and_pauses(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(RuntimeError("kaboom"))
    _run(monkeypatch, s, db, poster, once=True)

    row = db.listing(iid, "poshmark")
    assert row["status"] == "queued" and "RuntimeError: kaboom" in row["error"] and row["attempts"] == 1
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
    assert db.listing(ids[0], "poshmark")["status"] == "queued" and db.listing(ids[1], "poshmark") is None
    assert "not logged in" in s.flag("PAUSE").read_text(encoding="utf-8")
    assert sum("Poster paused" in m for m in said) == 1


SKIP = "size '15' isn't on Poshmark's Women/Shoes (Standard) menu, nor one near it (the size is required)"


def test_a_skipped_item_saves_nothing_is_reported_and_the_loop_goes_on(tmp_path, monkeypatch, harness):
    """WO27: a required field the form can't take, even as a guess: nothing saved, the item reported and left for the
    owner (thrift requeue once fixed); never a question, never a pause — item B still posts."""
    said, pauses = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False, max_fail=1)
    db = DB(s.path("db"))
    a, b = _ready_item(db, seq=1), _ready_item(db, seq=2)
    poster = StubPoster(Outcome("skipped", error=SKIP), Outcome("posted", url="https://poshmark.com/listing/b"))

    async def stop_on_second_pause(stop, seconds):
        pauses.append(seconds)
        if len(pauses) >= 2:
            stop.set()

    monkeypatch.setattr(runner, "_pause", stop_on_second_pause)
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 2                              # B was not held up by A
    row = db.listing(a, "poshmark")
    assert (row["status"], row["error"], row["url"], row["posted_at"]) == ("skipped", runner.SKIPPED + SKIP,
                                                                                 None, None)
    assert db.item(a)["status"] == "ready" and db.listing(b, "poshmark")["status"] == "posted"
    assert any(m.startswith(f"⏭ skipped on poshmark ({a}): {RENDER.title}\n{SKIP}\n(a reply 'retry' to the ⏭ "
                            "message in the group tries again)") for m in said)
    assert f"⏭ {RENDER.title} wasn't listed on Poshmark: Poshmark's form doesn't take one of its details. Reply " \
           "'retry' to try it again." in said.group                  # WO33: a reply settles it, never a command
    assert not any("thrift " in m for m in [*said, *said.group])
    assert not s.flag("PAUSE").exists() and not any("paused" in m.lower() for m in said)   # max_fail=1: not a failure
    assert runner.next_job(s, db, ["poshmark"], False) is None          # never retried on its own


def test_a_skip_leaves_the_failure_counter_alone(tmp_path, monkeypatch, harness):
    """Neither a failure nor a success for the circuit breaker: failed, skipped, failed still trips max_fail=2."""
    s = _settings(tmp_path, max_fail=2)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 4)]
    poster = StubPoster(Outcome("failed", error="a"), Outcome("skipped", error=SKIP), Outcome("failed", error="b"))
    _run(monkeypatch, s, db, poster, once=False)

    assert len(poster.calls) == 3
    assert [db.listing(i, "poshmark")["error"] for i in ids] == ["a", runner.SKIPPED + SKIP, "b"]
    assert "2 consecutive failures: b" in s.flag("PAUSE").read_text(encoding="utf-8")


def test_a_draft_gated_item_publishes_never_held(tmp_path, monkeypatch, harness):
    """WO33, the owner's rule: the copy check never holds an item — an approved, ready item whose gate said "draft"
    (from before the rule) publishes like any other; no "⏸ Not published automatically", no command to run."""
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db, decision="draft", reasons=["material word without a label: wool"])
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x"))
    _run(monkeypatch, s, db, poster, once=True)
    assert [c[1:] for c in poster.calls] == [("publish", False)] and db.listing(iid, "poshmark")["status"] == "posted"
    assert not any(m.startswith("⏸") for m in [*said, *said.group])


def test_the_live_loop_end_to_end(tmp_path, monkeypatch, harness):
    """WO27 §4, the loop as the owner will switch it on (dry_run off, autopublish_confirmed on, poshmark autopublish
    on), with a stand-in browser: the oldest approved item first, one at a time, a human gap (gap_seconds) after each,
    "Posted ✓" with the guesses; an unapproved item untouched, a draft-gated one published too (WO33: never held);
    outside the hours nothing."""
    said, pauses = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    first, second = _ready_item(db, seq=1), _ready_item(db, seq=2)
    unpriced, gated = _ready_item(db, seq=3, price=None), _ready_item(db, seq=4, decision="draft", reasons=["x"])
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/1", guesses=["colour 'Teal' left out"]),
                        Outcome("posted", url="https://poshmark.com/listing/2"),
                        Outcome("posted", url="https://poshmark.com/listing/4"))
    gaps = []

    async def human_gap(stop, seconds):
        gaps.append(seconds)
        if len(gaps) >= 4:                                   # after the three items and one idle minute
            stop.set()

    monkeypatch.setattr(runner, "_pause", human_gap)
    monkeypatch.setattr(runner, "can_post", lambda s_, hour, day: (False, "outside posting hours"))
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == []                                # outside the hours: nothing at all
    monkeypatch.setattr(runner, "can_post", lambda s_, hour, day: (True, "ok"))
    _run(monkeypatch, s, db, poster, once=False)

    assert [c[1:] for c in poster.calls] == [("publish", False)] * 3
    assert [db.listing(i, "poshmark")["url"] for i in (first, second, gated)] == ["https://poshmark.com/listing/1",
                                                                                 "https://poshmark.com/listing/2",
                                                                                 "https://poshmark.com/listing/4"]
    assert all(150 <= g <= 4 * 420 for g in gaps[:3]) and gaps[3] == 60   # human gaps (now and then a longer
    #                                                                        break, next_gap), then the idle minute
    assert db.listing(unpriced, "poshmark") is None
    started = [m for m in said if m.startswith("Poster started")]
    assert started and all("LIVE" in m for m in started)
    posted = [m for m in said.group if m.startswith("✅")]                      # WO34: the cards
    assert posted == [card(NAME, 85, [("poshmark", "https://poshmark.com/listing/1")]),
                      card(NAME, 85, [("poshmark", "https://poshmark.com/listing/2")]),
                      card(NAME, 85, [("poshmark", "https://poshmark.com/listing/4")], ALL_DONE)]
    assert any(m.startswith("Check —") and "colour 'Teal' left out" in m for m in said)   # the guess: ops chat
    assert not any(m.startswith("⏸") for m in [*said, *said.group])


def test_run_skips_a_job_another_poster_claimed(tmp_path, monkeypatch, harness):
    """SELECT-then-UPDATE let two poster processes take the same item; claim_listing is the lock."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ids = [_ready_item(db, seq=i) for i in range(1, 3)]
    poster = StubPoster(Outcome("dryrun"))
    real_claim = db.claim_listing

    def racing_claim(iid, mp):
        if iid == ids[0]:
            db.upsert_listing(iid, mp, status="posting")          # the other process got there first
        return real_claim(iid, mp)

    monkeypatch.setattr(db, "claim_listing", racing_claim)
    _run(monkeypatch, s, db, poster, once=True)
    assert len(poster.calls) == 1                              # the claimed item was never opened by us
    assert db.listing(ids[0], "poshmark")["status"] == "posting" and db.listing(ids[1], "poshmark")["status"] == "dryrun"


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
    assert db.listing(ids[0], "poshmark")["status"] == "dryrun" and db.listing(ids[1], "poshmark") is None


def test_run_passes_the_dry_run_stage_and_reports_the_note(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    note = "review page recorded in i_1-review.json; style tags Poshmark doesn't offer, left out: sparkly"
    poster = StubPoster(Outcome("dryrun", note=note))
    s.data["poster"] = {**s.data["poster"], "notify_dry_runs": True}           # a copy: settings() is shared
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


def test_run_warns_about_a_left_draft_after_a_skip_too(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _ready_item(db)
    left = "a draft was left behind (Drafts 2 → 3)"
    _run(monkeypatch, s, db, StubPoster(Outcome("skipped", error=SKIP, draft_left=left)), once=True)
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
    assert poster.calls == [] and db.listing(iid, "poshmark") is None


@pytest.mark.parametrize("setup,why", [
    (lambda db, iid: db.set_item(iid, owner_price=None), "no owner-approved price"),
    (lambda db, iid: db.set_item(iid, owner_price=80), "no owner-approved price"),            # 85 on the listing
    (lambda db, iid: db.set_item(iid, status="awaiting_price"), "is awaiting_price, not ready"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="posting"), "it reached the site"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="failed", url="https://poshmark.com/listing/x"),
     "it reached the site"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="failed",
                                    error=runner.UNCONFIRMED + "no address"), "may have gone live"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="failed", error="Mismatch"),
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
    assert poster.calls == [] and db.listing(iid, "poshmark") is None


def test_publish_first_publishes_once_and_records_the_listing(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod", dry_run=True)                # poster.dry_run is ignored for this call
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/naturino-6ad", clicked=True))
    out = _first(monkeypatch, s, db, poster, iid, confirm="the LIST prompt")
    assert out.status == "posted" and poster.calls == [("i_1", "publish", False)]
    assert poster.confirm == "the LIST prompt"
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["url"]) == ("posted", "https://poshmark.com/listing/naturino-6ad")
    assert db.item(iid)["status"] == "posted" and any(m.startswith(f"✅ {NAME} · $85") for m in said)


def test_publish_first_cancelled_at_the_prompt_goes_back_to_the_queue(tmp_path, monkeypatch, harness):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("cancelled", note="not published: LIST wasn't typed"))
    assert _first(monkeypatch, s, db, poster, iid).status == "cancelled"
    row = db.listing(iid, "poshmark")
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
    row = db.listing(iid, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(runner.UNCONFIRMED) and row["url"] is None
    assert any(m.startswith(runner.unconfirmed_text(RENDER.title, slept=False)) for m in said)   # ONE reply-able
    assert not any(m.startswith("❌") for m in said)                                    # message (WO28), no ❌
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
    assert poster.calls == [] and db.listing(iid, "poshmark") is None          # never left in 'posting'


def test_publish_first_posts_the_item_and_queues_depop(tmp_path, monkeypatch, harness):
    """WO30: the item counts as posted once Poshmark is; Depop (and Vinted) are extra rows, queued to follow."""
    s = _settings(tmp_path, role="prod", depop=True)
    db = DB(s.path("db"))
    iid = _approved(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/x", clicked=True))
    monkeypatch.setattr(runner, "posters", lambda s_: {"poshmark": poster, "depop": StubPoster(Outcome("posted"))})
    asyncio.run(runner.publish_first(s, db, iid, confirm="confirm"))
    assert db.listing(iid, "poshmark")["status"] == "posted" and db.item(iid)["status"] == "posted"
    assert db.listing(iid, "depop")["status"] == "queued"


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
    db.upsert_listing(iid, "poshmark", status="failed",
                   error=runner.UNCONFIRMED + "PosterError: after List This Item no listing address")


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
    before = db.listing(iid, "poshmark")
    address, poster = _mark(monkeypatch, s, db, iid, url=LIVE_URL + "?utm_source=share")
    assert address == LIVE_URL and poster.seen == (LIVE_URL, RENDER.title, 85)
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["url"], row["error"]) == ("posted", LIVE_URL, None)
    assert row["posted_at"] == before["updated_at"]                     # when it went live, as near as known
    assert db.item(iid)["status"] == "posted"
    assert said.group == [card(NAME, 85, [("poshmark", LIVE_URL)], ALL_DONE)]
    assert f"✅ {iid} confirmed live on poshmark (the owner's link): {LIVE_URL}" in said      # the ops chat
    assert "post_confirmed" in [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE ref=?", (iid,))]
    assert list((s.path("failed") / "shots").glob(f"{iid}-poshmark-*-confirm.json"))   # the evidence of the check


@pytest.mark.parametrize("setup,url,why", [
    (lambda db, iid: None, LIVE_URL, r"only a post in 'unconfirmed publish' can be marked posted or retried "
                                     r"\(i_\w+ has no post\)"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="failed", error="Mismatch: title"), LIVE_URL,
     "only a post in 'unconfirmed publish'"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="posted", url=LIVE_URL), LIVE_URL,
     "has status posted with https://poshmark.com/listing/"),
    (lambda db, iid: db.upsert_listing(iid, "poshmark", status="dryrun"), LIVE_URL, "has status dryrun"),
    (_unconfirmed, "https://poshmark.com/closet/someone", "not a poshmark listing address"),
    (_unconfirmed, "https://poshmark.com/listing/x-6ac11149", "not a poshmark listing address"),
])
def test_mark_posted_refuses_anything_else(tmp_path, monkeypatch, harness, setup, url, why):
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    setup(db, iid)
    before = dict(db.listing(iid, "poshmark") or {})
    with pytest.raises(ValueError, match=why):
        _mark(monkeypatch, s, db, iid, url=url)
    assert dict(db.listing(iid, "poshmark") or {}) == before                  # nothing changed


def test_mark_posted_refuses_an_address_another_item_holds_or_a_page_without_the_item(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid, other = _approved(db), _ready_item(db, seq=2)
    _unconfirmed(db, iid)
    db.upsert_listing(other, "poshmark", status="posted", url=LIVE_URL)
    with pytest.raises(ValueError, match=f"already recorded for item {other}"):
        _mark(monkeypatch, s, db, iid)
    db.upsert_listing(other, "poshmark", url="https://poshmark.com/listing/y-6ac111490000000000000a02")
    with pytest.raises(ValueError, match="doesn't show this item .*nothing changed"):
        _mark(monkeypatch, s, db, iid, poster=_seeing(shows=False))
    row = db.listing(iid, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(runner.UNCONFIRMED) and row["url"] is None
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
    assert poster.calls == [] and db.listing(iid, "poshmark") is None
    assert runner.condition_rule_breaks(RENDER) == []


def test_a_dry_run_is_silent_in_the_owners_chat_by_default(tmp_path, monkeypatch, harness):
    """WO20: info-only messages are kept few. A dry-run that went fine needs nothing from the owner: no Telegram photo
    unless poster.notify_dry_runs (the evidence is in failed/shots/ either way)."""
    said, _ = harness
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _ready_item(db)
    _run(monkeypatch, s, db, StubPoster(Outcome("dryrun")), once=True)
    assert not any("dry-run poshmark" in m for m in said)


# ---------------------------------------------------------------- WO28: the daily window

class Held:
    """power.Awake stand-in: what the loop asked for."""
    log: list = []

    def __init__(self):
        Held.log = []

    def hold(self, on):
        Held.log.append(on)


class Finding(StubPoster):
    """A stub that can look in the closet (after a sleep): `found` is the address it sees, or None."""

    def __init__(self, *results, found=None):
        super().__init__(*results)
        self.found, self.looked = found, []

    async def find_live(self, ctx, r, since, created=None):
        self.looked.append((r.sku, created))
        return self.found, {"stub": True}


@pytest.fixture
def mac(monkeypatch):
    """The Mac's power readings, scripted: lid open, no battery reading, no sleep — unless a test says so."""
    state = {"lid": False, "battery": None, "slept": False}
    monkeypatch.setattr(runner.power, "lid_closed", lambda: state["lid"])
    monkeypatch.setattr(runner.power, "battery", lambda: state["battery"])
    monkeypatch.setattr(runner.power, "slept_since", lambda *a, **k: state["slept"])
    monkeypatch.setattr(runner.power, "last_wake", lambda: None)
    monkeypatch.setattr(runner.power, "Awake", Held)
    return state


def test_the_loop_keeps_the_mac_awake_while_it_publishes_and_lets_it_sleep_after(tmp_path, monkeypatch, harness, mac):
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    _ready_item(db, seq=1), _ready_item(db, seq=2)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/a"),
                        Outcome("posted", url="https://poshmark.com/listing/b"))
    pauses = []

    async def gap(stop, seconds):
        pauses.append(seconds)
        if len(pauses) >= 3:
            stop.set()

    monkeypatch.setattr(runner, "_pause", gap)
    _run(monkeypatch, s, db, poster, once=False)
    # held for listing 1, through the pause before listing 2 (one follows), for listing 2; released after it and when
    # there was nothing left to do
    assert Held.log[:3] == [True, True, True] and Held.log[-1] is False and False in Held.log[3:]
    state = json.loads(db.kv_get(daily.POSTER_STATE))
    assert state["stopped"] is True and state["live"] is True


def test_a_closed_lid_or_a_low_battery_starts_no_listing_and_it_resumes(tmp_path, monkeypatch, harness, mac):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = StubPoster(Outcome("posted", url="https://poshmark.com/listing/a"))
    mac["lid"] = True
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == [] and db.listing(iid, "poshmark") is None             # a maintenance wake: nothing started
    mac["lid"], mac["battery"] = False, power.Battery(12, False)
    _run(monkeypatch, s, db, poster, once=True)
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == [] and said.count(power.BATTERY_LOW) == 1            # ONE line, nothing lost
    mac["battery"] = power.Battery(17, False)
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == []                                                   # 17%: still waits for 20%
    mac["battery"] = power.Battery(17, True)                                    # plugged in
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.calls == [("i_1", "publish", False)] and db.item(iid)["status"] == "posted"


def test_a_publish_the_mac_slept_through_is_found_in_the_closet_and_posted(tmp_path, monkeypatch, harness, mac):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    url = "https://poshmark.com/listing/Tory-Burch-Red-Flats-size-75-6ac111490000000000000a01"
    poster = Finding(Outcome("failed", error="PosterError: after List This Item no listing address", clicked=True,
                             created_id="6ac111490000000000000a01"), found=url)
    mac["slept"] = True
    _run(monkeypatch, s, db, poster, once=True)
    assert poster.looked == [("i_1", "6ac111490000000000000a01")]                # created_listing_id first
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["url"], row["error"]) == ("posted", url, None)
    assert db.item(iid)["status"] == "posted"
    assert any(m.startswith(f"✅ {NAME}") and url in m for m in said)               # the normal card
    assert not any(m.startswith("⚠️") for m in said)


def test_a_publish_the_mac_slept_through_and_not_in_the_closet_asks_once(tmp_path, monkeypatch, harness, mac):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = Finding(Outcome("failed", error="PosterError: after List This Item no listing address", clicked=True))
    mac["slept"] = True
    _run(monkeypatch, s, db, poster, once=True)
    row = db.listing(iid, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(runner.UNCONFIRMED) and row["url"] is None
    ask = runner.unconfirmed_text(RENDER.title, slept=True)
    assert ask == (f"⚠️ {RENDER.title}: the Mac went to sleep while publishing and I can't see it in the closet. "
                   "Check Poshmark: if it's there, reply 'posted <url>'; if not, reply 'retry'.")
    assert [m for m in said if m.startswith("⚠️")] == [f"{ask}\n(thrift mark-posted {iid} poshmark <url> | thrift "
                                                         f"retry {iid})"]
    assert runner.next_job(s, db, ["poshmark"], False) is None                 # never retried by itself
    assert pipeline.retry_unconfirmed(s, db, iid) == "ready"                    # the owner's 'retry'
    assert db.listing(iid, "poshmark")["status"] == "queued" and runner.next_job(s, db, ["poshmark"], False)[0] == iid


def test_a_listing_the_mac_slept_through_before_its_final_click_simply_goes_again(tmp_path, monkeypatch, harness,
                                                                                  mac):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False, max_fail=1)
    db = DB(s.path("db"))
    iid = _ready_item(db)
    poster = Finding(Outcome("failed", error="TimeoutError: the size menu"),
                     Outcome("posted", url="https://poshmark.com/listing/a"))
    mac["slept"] = True
    _run(monkeypatch, s, db, poster, once=True)
    row = db.listing(iid, "poshmark")
    assert row["status"] == "queued" and row["error"].startswith(runner.SLEPT)    # nothing was submitted
    assert not any(m.startswith("❌") for m in said) and not s.flag("PAUSE").exists()  # not a failure (max_fail=1)
    mac["slept"] = False
    _run(monkeypatch, s, db, poster, once=True)
    assert db.listing(iid, "poshmark")["status"] == "posted" and len(poster.calls) == 2


def test_a_listing_still_posting_at_start_is_looked_for_in_the_closet(tmp_path, monkeypatch, harness, mac):
    said, _ = harness
    s = _settings(tmp_path, role="prod", autopublish=True, dry_run=False)
    db = DB(s.path("db"))
    found, lost = _ready_item(db, seq=1), _ready_item(db, seq=2)
    for iid in (found, lost):
        db.claim_listing(iid, "poshmark")                               # the Mac slept for good mid-listing
    url = "https://poshmark.com/listing/Tory-Burch-Red-Flats-size-75-6ac111490000000000000a02"

    class Closet(Finding):
        async def find_live(self, ctx, r, since, created=None):
            self.looked.append(r.sku)
            return (url, {}) if len(self.looked) == 1 else (None, {})

    poster = Closet(Outcome("dryrun"))
    asyncio.run(runner.reconcile_stale(s, db, {"poshmark": poster}, FakeCtx()))
    assert db.listing(found, "poshmark")["status"] == "posted" and db.listing(found, "poshmark")["url"] == url
    row = db.listing(lost, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(runner.UNCONFIRMED)   # never retried
    assert any(m.startswith("✅") for m in said) and sum(m.startswith("⚠️") for m in said) == 1


def test_the_owners_posted_url_is_checked_by_the_poster_between_listings(tmp_path, monkeypatch, harness):
    said, _ = harness
    s = _settings(tmp_path, role="prod")
    db = DB(s.path("db"))
    iid = _approved(db)
    _unconfirmed(db, iid)
    with pytest.raises(ValueError, match="not a poshmark listing address"):
        pipeline.request_posted(s, db, iid, "https://poshmark.com/closet/someone")
    assert pipeline.request_posted(s, db, iid, LIVE_URL + "?share=1") == LIVE_URL
    poster = _seeing()
    done = asyncio.run(runner.serve_requests(s, db, {"poshmark": poster}, PageCtx()))
    assert done == [iid] and db.listing(iid, "poshmark")["url"] == LIVE_URL and db.item(iid)["status"] == "posted"
    assert any(m.startswith(card(NAME, 85, [("poshmark", LIVE_URL)])) for m in said.group)
    assert pipeline.take_requests(db) == []                                     # taken once

    other = _ready_item(db, seq=2)
    db.set_item(other, owner_price=85)
    _unconfirmed(db, other)
    pipeline.request_posted(s, db, other, LIVE_URL.replace("0a01", "0a09"))
    asyncio.run(runner.serve_requests(s, db, {"poshmark": _seeing(shows=False)}, PageCtx()))
    assert db.listing(other, "poshmark")["status"] == "failed"                      # nothing changed
    assert any(m.startswith("⚠️") and "doesn't show this item" in m for m in said)   # the owner can reply again
