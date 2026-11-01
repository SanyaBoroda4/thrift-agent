"""WO52 §3: the twice-daily form check — a canary item's form filled on every site at 08:30 and 14:30 local (and
after a start or a reopened browser), never published; its draft count and the form's structure compared; a failed
check degrades the site and calls the alarm on the second failure (or at once with items waiting)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from thrift_agent import crosslist, formcheck, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.post import runner
from thrift_agent.post.base import Outcome


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.fixture
def s(tmp_path):
    return Settings({"machine_role": "dev", "formcheck": {"enabled": True, "anywhere": True},
                     "paths": {"db": str(tmp_path / "var" / "state.db"), "failed": str(tmp_path / "var" / "failed"),
                               "work": str(tmp_path / "work"), "control": str(tmp_path / "control")},
                     "schedule": {"timezone": "America/New_York"}})


@pytest.fixture
def db(s):
    s.path("db").parent.mkdir(parents=True, exist_ok=True)
    return DB(s.path("db"))


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    monkeypatch.setattr(formcheck, "_module", lambda name: None)   # WO52 §4/§5's modules: their own tests
    return out


def _sold_item(s, db, iid="i_261012_0abc12", status="sold"):
    d = s.path("work") / "b_261012_x" / "item_01"
    (d / "photos").mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        (d / "photos" / f"{n:02d}.jpg").write_bytes(b"jpg")
    (d / "cover.jpg").write_bytes(b"jpg")
    db.conn.execute("INSERT OR IGNORE INTO batches (id, src_dir, status, created_at, updated_at) "
                    "VALUES ('b_261012_x', 'x', 'split', '2026-10-12', '2026-10-12')")
    render = {"marketplace": "poshmark", "title": "Zara Knit Top Black size S", "description": "A top.", "department": "Women",
              "category": "Tops", "subcategory": None, "size": "S", "condition": "like_new", "brand": "Zara",
              "colors": ["Black"], "price": 30, "photos": [str(d / "cover.jpg"), str(d / "photos" / "02.jpg")],
              "sku": iid}
    db.conn.execute("INSERT INTO items (id, batch_id, seq, status, dir, facts, renders, owner_price, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (iid, "b_261012_x", 1, "posted", str(d), json.dumps({"item_type": "knit top"}),
                     json.dumps({"poshmark": render}), 30, "2026-10-12T10:00:00", "2026-10-12T10:00:00"))
    db.upsert_listing(iid, "poshmark", status=status, url="https://poshmark.com/listing/x-" + "a" * 24)
    return iid


# ---------------------------------------------------------------- when

def test_the_slots_are_8_30_and_2_30_local_on_both_sides_of_the_dst_change(s):
    assert formcheck.last_slot(s, at(2026, 10, 31, 12, 31)).strftime("%H:%M %Z") == "08:30 EDT"
    assert formcheck.last_slot(s, at(2026, 11, 1, 13, 31)).strftime("%H:%M %Z") == "08:30 EST"   # 08:31 EST
    assert formcheck.last_slot(s, at(2026, 11, 1, 13, 29)).strftime("%d %H:%M") == "31 14:30"     # 08:29 EST
    assert formcheck.last_slot(s, at(2026, 11, 1, 19, 31)).strftime("%d %H:%M") == "01 14:30"     # 14:31 EST


def test_due_once_per_slot_and_on_request(s, db):
    t = at(2026, 11, 2, 13, 40)                                         # 08:40 EST
    assert formcheck.due(s, db, "vinted", t) == "the 08:30 check"
    formcheck._taken(db, "vinted", "the 08:30 check", t)
    assert formcheck.due(s, db, "vinted", at(2026, 11, 2, 19, 0)) is None      # 14:00: not yet the next
    assert formcheck.due(s, db, "vinted", at(2026, 11, 2, 19, 31)) == "the 14:30 check"
    formcheck.request(db, ["vinted", "depop"], "the poster started")
    assert formcheck.due(s, db, "vinted", t) == "the poster started"
    formcheck._taken(db, "vinted", "the poster started", t)
    assert formcheck.due(s, db, "depop", t) == "the poster started"


def test_off_unless_enabled(tmp_path):
    s = Settings({"machine_role": "prod", "paths": {"db": str(tmp_path / "s.db")}})
    assert formcheck.due(s, DB(s.path("db")), "poshmark") is None


# ---------------------------------------------------------------- the canary

def test_the_canary_is_a_copy_in_no_queue_never_synced(s, db):
    src = _sold_item(s, db)
    iid = formcheck.make_canary(s, db)
    it = db.item(iid)
    assert it["status"] == "canary" and it["test"] == 1 and it["batch_id"] == "b_canary"
    assert db.batch("b_canary")["status"] == "canary"
    assert db.listings_for(iid) == []
    dest = s.path("db").parent / "canary" / iid
    assert sorted(p.name for p in (dest / "photos").iterdir()) == ["01.jpg", "02.jpg"] and (dest / "cover.jpg").is_file()
    render = json.loads(it["renders"])["poshmark"]
    assert render["sku"] == iid and all(str(dest) in p for p in render["photos"])
    assert src in it["note"]
    assert db.conn.execute("SELECT COUNT(*) FROM api_dirty WHERE key=?", (iid,)).fetchone()[0] == 0
    # never posted: the poster's queue, the cross queue and the card queue don't see it
    assert runner.next_job(s, db, ["poshmark"], dry=False) is None
    assert crosslist.pending(s, db) == []
    # made again: the old one retired
    second = formcheck.make_canary(s, db, src)
    assert formcheck.canary(db)["id"] == second and db.item(iid)["status"] == "retired_canary"


def test_no_source_no_canary(s, db, said):
    assert formcheck.make_canary(s, db) is None


# ---------------------------------------------------------------- the structure

VINTED_OLD = [{"label": "Brand", "kind": "input:text", "key": "brand-select-dropdown-input", "required": True},
              {"label": "Size", "kind": "input:text", "key": "category-size-single-grid-input", "required": True}]
VINTED_NEW = [{"label": "Brand", "kind": "button", "key": "brand-select-dropdown-input", "required": True},
              {"label": "Size", "kind": "input:text", "key": "category-size-single-grid-input", "required": True},
              {"label": "Package size", "kind": "radio", "key": "package", "required": False}]


def test_a_changed_form_is_said_once_even_when_the_dry_run_passed(s, db, said):
    assert formcheck.compare(s, db, "vinted", VINTED_OLD, True) == []        # the first: the baseline
    assert said == []
    changes = formcheck.compare(s, db, "vinted", VINTED_NEW, True)
    assert "Brand is now a button" in changes and "new field Package size" in changes
    assert said == ["🔎 Vinted's form changed: Brand is now a button; new field Package size."]
    formcheck.compare(s, db, "vinted", VINTED_NEW, True)                     # the new one is the baseline now
    assert len(said) == 1


def test_a_failed_check_keeps_the_last_good_structure(s, db, said):
    formcheck.compare(s, db, "vinted", VINTED_OLD, True)
    formcheck.compare(s, db, "vinted", VINTED_NEW, False)
    stored = json.loads(formcheck._baseline(s, "vinted").read_text(encoding="utf-8"))["fields"]
    assert stored["brand"]["kind"] == "input:text"
    assert said[-1].endswith("Its form check failed too.")


# ---------------------------------------------------------------- the result

def test_passing_check_is_ok_and_a_draft_left_is_a_failure(s, db, said):
    st = formcheck.record(s, db, "poshmark", Outcome("dryrun"), "the 08:30 check")
    assert st["ok"] and not st["degraded"] and st["draft"] == "none left"
    st = formcheck.record(s, db, "poshmark", Outcome("dryrun", draft_left="a draft was left behind (Drafts 0 → 1)"),
                          "the 14:30 check")
    assert not st["ok"] and st["degraded"] and "draft was left" in st["error"]
    assert formcheck.degraded(db, "poshmark")
    assert formcheck.record(s, db, "depop", Outcome("dryrun"), "x")["draft"] == "not checkable"


def test_the_alarm_comes_on_the_second_failure_in_a_row(s, db, said):
    formcheck.record(s, db, "vinted", Outcome("failed", error="Vinted brand: no search box"), "the 08:30 check")
    assert not [x for x in said if x.startswith("🚨")]
    formcheck.record(s, db, "vinted", Outcome("failed", error="Vinted brand: no search box"), "the 14:30 check")
    alarms = [x for x in said if x.startswith("🚨")]
    assert len(alarms) == 1 and "2× in a row" in alarms[0]
    formcheck.record(s, db, "vinted", Outcome("failed", error="Vinted brand: no search box"), "again")
    assert len([x for x in said if x.startswith("🚨")]) == 1                # one an episode
    formcheck.record(s, db, "vinted", Outcome("dryrun"), "fixed")
    assert said[-1] == "✅ Vinted's form check passed again." and not formcheck.degraded(db, "vinted")


def test_the_alarm_comes_at_once_with_items_waiting(s, db, said, monkeypatch):
    monkeypatch.setattr(formcheck, "waiting", lambda db_, site: 9)
    formcheck.record(s, db, "depop", Outcome("failed", error="Depop shipping: not on the page"), "the 08:30 check")
    assert [x for x in said if x.startswith("🚨")] == [
        "🚨 Depop's form check failed (1× in a row, 9 items waiting): Depop shipping: not on the page"]


def test_the_alarm_module_is_called_when_there_is_one(s, db, said, monkeypatch):
    import types
    calls = []
    fake = types.SimpleNamespace(form_check_failed=lambda *a: calls.append(a))
    monkeypatch.setattr(formcheck, "_module", lambda name: fake if name == "alarm" else None)
    monkeypatch.setattr(formcheck, "waiting", lambda db_, site: 2)
    formcheck.record(s, db, "vinted", Outcome("failed", error="x", screenshot="shot.png"), "y")
    assert calls and calls[0][1:] == ("vinted", "x", "shot.png", 2)


# ---------------------------------------------------------------- the runs

class FakePosh:
    capture_structure = False
    last_structure = None

    def __init__(self, out):
        self.out, self.calls = out, []

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        self.calls.append((r.sku, mode, dry_run, stage, self.capture_structure))
        self.last_structure = VINTED_OLD
        return self.out


def test_poshmarks_check_is_a_dry_run_of_the_canary_never_a_publish(s, db, said):
    _sold_item(s, db)
    poster = FakePosh(Outcome("dryrun"))
    st = asyncio.run(formcheck.run_poshmark(s, db, poster, None, "the poster started"))
    iid = formcheck.canary(db)["id"]
    assert poster.calls == [(iid, "publish", True, "form", True)]          # dry_run True: nothing is ever pressed
    assert st["ok"] and poster.capture_structure is False
    assert db.listings_for(iid) == []
    assert formcheck._baseline(s, "poshmark").is_file()
    assert formcheck.due(s, db, "poshmark") is None


def test_a_cross_check_goes_through_the_request_dry_run(s, db, said, monkeypatch):
    _sold_item(s, db)
    seen = []

    async def fake_run_cross(s_, db_, ps, ctx, iid, mp, *, dry, request, quiet, **kw):
        seen.append((iid, mp, dry, request, quiet))
        ps[mp].last_structure = VINTED_OLD
        return Outcome("dryrun")

    monkeypatch.setattr(runner, "run_cross", fake_run_cross)
    ps = {"vinted": type("P", (), {"last_structure": None})()}
    st = asyncio.run(formcheck.run_cross(s, db, ps, "vinted", "the 08:30 check"))
    assert seen == [(formcheck.canary(db)["id"], "vinted", True, True, True)] and st["ok"]


def test_no_canary_is_said_once(s, db, said):
    poster = FakePosh(Outcome("dryrun"))
    assert asyncio.run(formcheck.run_poshmark(s, db, poster, None, "x")) is None
    assert asyncio.run(formcheck.run_poshmark(s, db, poster, None, "x")) is None
    assert len(said) == 1 and "no canary" in said[0] and poster.calls == []


def test_the_heartbeat_carries_the_checks(s, db, said):
    formcheck.record(s, db, "poshmark", Outcome("dryrun"), "x", at=at(2026, 11, 2, 13, 35))
    from thrift_agent import cli
    beat = cli.beat_info(db)["formcheck"]
    assert beat["poshmark"]["ok"] is True and beat["poshmark"]["slot"] == "08:30"


def test_the_days_slots_are_kept_for_the_evening_line(s, db, said):
    formcheck.record(s, db, "vinted", Outcome("dryrun"), "x", at=at(2026, 11, 2, 13, 35))   # 08:35 EST
    formcheck.record(s, db, "vinted", Outcome("dryrun"), "x", at=at(2026, 11, 2, 19, 35))   # 14:35 EST
    assert formcheck.beat(db)["vinted"]["today"] == {"day": "2026-11-02", "passed": ["08:30", "14:30"], "failed": []}


def test_a_due_check_runs_on_poshmarks_turn_before_any_listing(s, db, said, monkeypatch):
    from thrift_agent.post import parallel
    ran = []

    async def fake_run(s_, db_, poster, ctx, why):
        ran.append(why)

    monkeypatch.setattr(formcheck, "run_poshmark", fake_run)
    monkeypatch.setattr(runner, "serve_requests", lambda *a: asyncio.sleep(0))
    monkeypatch.setattr(runner, "_paused", lambda db_: None)
    formcheck.request(db, "poshmark", "its browser was reopened")

    class St:
        def set(self, *a):
            pass

        def idle(self, *a):
            pass

    worked = asyncio.run(parallel._poshmark_turn(s, db, {"poshmark": object()}, None, False, "form", St(), None,
                                                 {"n": 0}, ["poshmark"], None))
    assert worked and ran == ["its browser was reopened"]
