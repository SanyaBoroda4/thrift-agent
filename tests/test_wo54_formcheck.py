"""WO54 §1.6 (live 2026-11-09): form checks never compete with real work. At 8:37 the "08:30" check ran the canary on
Depop and Vinted while real items were queued there (priced at 8:23, and from 8:38 on); at 8:40:25 both ended "the form
wasn't done in 3 min (nothing was submitted)" — two FALSE 🚨 — while the same sites posted the real items fine
8:42–8:47. Now: a site with real work is proven by it (the check skipped; a real listing counts as its pass); a check
that couldn't judge the form beside real work is no failure; never in the quiet hours; once at night (02:00), plus the
poster's 2:00 PM start; a site with no work and a real failure is degraded and alarmed as before."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import yaml

from test_crosslist_flow import _item                               # a ready item, the owner's price
from test_wo52_formcheck import _sold_item                          # the canary's source
from thrift_agent import formcheck, health, notify
from thrift_agent.config import ROOT, Settings
from thrift_agent.db import DB
from thrift_agent.post import runner
from thrift_agent.post.base import Outcome

TIMEOUT = {"depop": "Depop: the form wasn't done in 3 min (nothing was submitted)",       # 13:40:25 UTC, the
           "vinted": "Vinted: the form wasn't done in 3 min (nothing was submitted)"}     # morning's form_check events


def at(y, mo, d, h, mi=0, sec=0):
    return datetime(y, mo, d, h, mi, sec, tzinfo=UTC)


T0 = at(2026, 11, 9, 13, 37, 20)        # 8:37:20 EST: the canary jobs began (i_canary_bd8fed-<site>-20261109-083720.json)


def _settings(tmp_path, slots=None) -> Settings:
    fc = {"enabled": True, "anywhere": True, **({"slots": slots} if slots else {})}
    return Settings({"machine_role": "dev", "formcheck": fc,
                     "paths": {"db": str(tmp_path / "var" / "state.db"), "failed": str(tmp_path / "var" / "failed"),
                               "work": str(tmp_path / "work"), "control": str(tmp_path / "control")},
                     "schedule": {"timezone": "America/New_York", "hours": ["00:00", "23:59"], "per_hour_max": 60,
                                  "daily_cap": 25, "gap_seconds": [0, 0]},
                     "marketplaces": {"poshmark": {"enabled": True, "autopublish": True},
                                      "depop": {"enabled": True, "daily_cap": 25},
                                      "vinted": {"enabled": True, "daily_cap": 25}}})


@pytest.fixture
def s(tmp_path):
    return _settings(tmp_path, slots=["08:30", "14:30"])           # the morning's slots (WO52)


@pytest.fixture
def db(s):
    s.path("db").parent.mkdir(parents=True, exist_ok=True)
    return DB(s.path("db"))


@pytest.fixture
def said(monkeypatch):
    """The private chat and the group; the real alarm module (formcheck.failed → alarm.form_check_failed → the 🚨)."""
    out = SimpleNamespace(ops=[], group=[])
    monkeypatch.setattr(notify, "say", out.ops.append)
    monkeypatch.setattr(notify, "group", out.group.append)
    monkeypatch.setattr(notify, "photo", lambda path, caption: out.ops.append(caption))
    return out


def _alarms(said):
    return [m for m in said.ops if m.startswith("🚨")]


def _skips(db):
    return [json.loads(d) for (d,) in db.conn.execute("SELECT detail FROM events WHERE kind='form_check_skipped' "
                                                      "ORDER BY rowid")]


def _pause_poshmark(db):
    health.pause(db, "poshmark", "site: PosterError: after picking '12 Months' the size menu neither showed Done nor "
                 "closed with that size on the form (the size field shows '12M')", health.SITE, wait=4 * 3600)


def _cross_check(monkeypatch, outcome, during=None):
    """runner.run_cross for the canary's dry run: `during(db)` runs while it fills (the real items priced meanwhile)."""
    seen = []

    async def fake(s_, db_, ps, ctx, iid, mp, *, dry, request, quiet, **kw):
        seen.append((iid, mp, dry, request))
        if during is not None:
            during(db_)
        return outcome

    monkeypatch.setattr(runner, "run_cross", fake)
    return seen


def test_this_mornings_8_37_checks_never_run_the_canary_the_sites_real_listings_prove_them(s, db, said):
    """The evidence: Poshmark paused at 8:03:51; Depop live with i_261109_600947 at 8:24:16, Vinted with
    i_261109_482186 at 8:23:57; c47338 queued on both since 8:23:25, 600947 back in line on Vinted (8:24:11, the lid).
    At 8:37:20 Depop's and Vinted's checks are proven by those listings — no canary, no 🚨; Poshmark's (paused: no real
    work going) runs, as it did and passed at 8:37:09."""
    _pause_poshmark(db)
    c47338, i600947, i482186 = _item(db, 1), _item(db, 2), _item(db, 3)
    db.upsert_listing(i482186, "vinted", status="posted", url="https://www.vinted.com/items/10734799007",
                      posted_at="2026-11-09T13:23:57+00:00")
    db.upsert_listing(i600947, "depop", status="posted", url="https://www.depop.com/products/x-mick-mack-1d1b/",
                      posted_at="2026-11-09T13:24:16+00:00")
    for iid, mp in ((c47338, "depop"), (c47338, "vinted"), (i600947, "vinted")):
        db.upsert_listing(iid, mp, status="queued")
    assert formcheck.due(s, db, "depop", T0) is None and formcheck.due(s, db, "vinted", T0) is None
    for site, item in (("depop", i600947), ("vinted", i482186)):
        st = formcheck.state(db, site)
        assert st["ok"] and not st["degraded"] and st["proven_by"] == item and st["today"]["passed"] == ["08:30"]
    assert formcheck.due(s, db, "depop", at(2026, 11, 9, 13, 40, 25)) is None        # taken: not due again
    assert formcheck.due(s, db, "poshmark", T0) == "the 08:30 check"
    assert _alarms(said) == [] and _skips(db) == []


def test_the_8_37_checks_beside_the_queued_rows_alone_are_skipped_with_no_alarm(s, db, said, monkeypatch):
    """The same 8:37, had nothing gone live in the hour before: c47338 queued on Depop and Vinted, 600947 back in line
    on Vinted — both checks skipped (proven by the items going up), said once each, no canary filled, no 🚨."""
    filled = _cross_check(monkeypatch, Outcome("failed", error=TIMEOUT["depop"]))
    c47338, i600947 = _item(db, 1), _item(db, 2)
    for iid, mp in ((c47338, "depop"), (c47338, "vinted"), (i600947, "vinted")):
        db.upsert_listing(iid, mp, status="queued")
    for turn in range(3):                                               # 8:37:20, 8:37:40, 8:38:00
        t = at(2026, 11, 9, 13, 37, 20 + 20 * turn) if turn < 2 else at(2026, 11, 9, 13, 38)
        assert formcheck.due(s, db, "depop", t) is None and formcheck.due(s, db, "vinted", t) is None
    assert [(x["site"], x["why"]) for x in _skips(db)] == [("depop", "1 real item queued there"),
                                                            ("vinted", "2 real items queued there")]
    assert filled == [] and _alarms(said) == []
    assert not formcheck.degraded(db, "depop") and not formcheck.degraded(db, "vinted")


def test_the_2pm_start_check_is_skipped_while_real_items_queue_and_their_first_landing_is_its_pass(s, db, said):
    """§5: "A form check while a site is posting → skipped; no 🚨". The poster starts at 2:00 PM with the morning's
    items waiting: each busy site's check is skipped (said once, not every turn); the first real listing there passes
    it; a site with nothing to post still gets its canary."""
    items = [_item(db, n) for n in (1, 2, 3)]
    for iid in items:
        db.upsert_listing(iid, "vinted", status="queued")
    db.upsert_listing(items[0], "depop", status="posting")              # Depop: one at its go-ahead right now
    start = at(2026, 11, 9, 19, 0, 30)                                  # 2:00:30 PM EST
    formcheck.request(db, ["poshmark", "depop", "vinted"], "the poster started", at=start)
    for turn in range(4):                                               # every worker turn asks again
        t = at(2026, 11, 9, 19, 1, 10 * turn)
        assert formcheck.due(s, db, "depop", t) is None and formcheck.due(s, db, "vinted", t) is None
    assert [(x["site"], x["why"], x["check"]) for x in _skips(db)] == [
        ("depop", "1 real listing being posted there", "the poster started"),
        ("vinted", "3 real items queued there", "the poster started")]
    assert formcheck.due(s, db, "poshmark", start) is None              # Poshmark's queue: the same three items
    assert [(x["site"], x["why"]) for x in _skips(db)][2:] == [("poshmark", "3 real items queued there")]
    assert _alarms(said) == []
    assert not formcheck.degraded(db, "vinted") and "skipped while real items post" in "\n".join(formcheck.summary(db))
    # 2:01:40 PM: the first of them live on Vinted — the check's pass
    db.upsert_listing(items[0], "vinted", status="posted", url="https://www.vinted.com/items/1",
                      posted_at="2026-11-09T19:01:40+00:00")
    assert formcheck.due(s, db, "vinted", at(2026, 11, 9, 19, 2)) is None
    st = formcheck.state(db, "vinted")
    assert st["ok"] and st["proven_by"] == items[0] and st["why"] == "the poster started" and not st["skipped"]
    assert st["today"]["passed"] == ["14:02"]                           # a request's pass: named by its own time
    assert "vinted" not in json.loads(db.kv_get(formcheck.REQUESTS))
    # a site with nothing to post: its canary, as before
    formcheck.request(db, "vinted", "its browser was reopened", at=at(2026, 11, 9, 20, 0))
    for iid in items:
        db.upsert_listing(iid, "vinted", status="posted", url=f"https://www.vinted.com/items/{iid}",
                          posted_at="2026-11-09T19:30:00+00:00")
    assert formcheck.due(s, db, "vinted", at(2026, 11, 9, 20, 0, 20)) == "its browser was reopened"


def test_a_check_that_timed_out_while_the_site_had_jobs_is_no_failure_and_asked_again(s, db, said, monkeypatch):
    """8:37:20 → 8:40:25 again: the canary's Depop job "wasn't done in 3 min" while real items were priced and queued
    on Depop (8:38:20 on) — no verdict on the form: no degraded state, no 🚨; asked again — skipped while those items
    go, passed by the first of them."""
    _sold_item(s, db)
    s.data["formcheck"]["slots"] = ["08:30"]
    priced = []

    def the_owner_prices(db_):                                          # 8:38:20 … 8:42:35: 16 items priced
        for n in range(16):
            iid = _item(db_, 10 + n)
            db_.upsert_listing(iid, "depop", status="queued")
            priced.append(iid)
    seen = _cross_check(monkeypatch, Outcome("failed", error=TIMEOUT["depop"]), during=the_owner_prices)
    assert formcheck.due(s, db, "depop", T0) == "the 08:30 check"
    st = asyncio.run(formcheck.run_cross(s, db, {"depop": SimpleNamespace(last_structure=None)}, "depop",
                                         "the 08:30 check", at=T0))
    assert seen and seen[0][2:] == (True, True)                         # a dry run of the canary, as before
    assert not st.get("degraded") and not formcheck.degraded(db, "depop") and _alarms(said) == []
    assert [x["why"] for x in _skips(db)] == ["busy: 16 real items queued there"]
    assert formcheck.due(s, db, "depop", at(2026, 11, 9, 13, 41)) is None          # asked again: skipped while busy
    assert len(_skips(db)) == 1                                                     # … said once
    db.upsert_listing(priced[0], "depop", status="posted", url="https://www.depop.com/products/x-1/",
                      posted_at="2026-11-09T13:42:42+00:00")
    assert formcheck.due(s, db, "depop", at(2026, 11, 9, 13, 43)) is None
    st = formcheck.state(db, "depop")                                   # the 08:30 slot: passed by a real listing
    assert st["ok"] and st["proven_by"] == priced[0] and st["today"] == {"day": "2026-11-09", "passed": ["08:30"],
                                                                         "failed": []}
    assert not [e for (e,) in db.conn.execute("SELECT detail FROM events WHERE kind='form_check'")
                if not json.loads(e)["ok"]]                             # never recorded as a failure


def test_poshmarks_check_beside_its_queue_is_no_failure_either(s, db, said):
    _sold_item(s, db)
    iid = _item(db, 5)                                                  # ready and priced: in Poshmark's queue

    class Posh:
        capture_structure = False
        last_structure = None

        async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
            raise RuntimeError("TargetClosedError: Target page, context or browser has been closed")

    st = asyncio.run(formcheck.run_poshmark(s, db, Posh(), None, "the poster started", at=T0))
    assert not st.get("degraded") and _alarms(said) == []
    assert _skips(db)[0]["why"] == "busy: 1 real item queued there"
    assert json.loads(db.kv_get(formcheck.REQUESTS)) == {"poshmark": "the poster started"}   # asked again
    assert db.listing(iid, "poshmark") is None                          # the real item untouched


def test_a_site_with_no_work_and_a_real_failure_is_degraded_and_alarmed_as_before(s, db, said, monkeypatch):
    """No real work on Vinted: its check's failures count as before — the first degrades it (nothing waiting: no 🚨),
    the second in a row raises the 🚨; a timeout with nothing else going is a failure too."""
    _sold_item(s, db)
    ps = {"vinted": SimpleNamespace(last_structure=None)}
    _cross_check(monkeypatch, Outcome("failed", error=TIMEOUT["vinted"]))
    st = asyncio.run(formcheck.run_cross(s, db, ps, "vinted", "the 08:30 check", at=T0))
    assert st["degraded"] and st["fails"] == 1 and _alarms(said) == [] and _skips(db) == []
    _cross_check(monkeypatch, Outcome("failed", error="Mismatch: brand shown Zara (plan List without brand)"))
    st = asyncio.run(formcheck.run_cross(s, db, ps, "vinted", "the 14:30 check", at=at(2026, 11, 9, 19, 31)))
    assert st["degraded"] and st["fails"] == 2
    alarms = _alarms(said)
    assert len(alarms) == 1 and alarms[0].startswith("🚨 Vinted posting is blocked since ")
    # and a real failure beside real work still counts: only "no verdict" is excused
    iid = _item(db, 7)
    db.upsert_listing(iid, "vinted", status="queued")
    formcheck.request(db, "vinted", "asked by hand")
    st = asyncio.run(formcheck.run_cross(s, db, ps, "vinted", formcheck.HAND, at=at(2026, 11, 9, 20, 0)))
    assert st["fails"] == 3 and _skips(db) == []


def test_a_check_asked_by_hand_runs_beside_real_work_and_its_timeout_is_no_failure(s, db, said, monkeypatch):
    _sold_item(s, db)
    iid = _item(db, 8)
    db.upsert_listing(iid, "depop", status="queued")
    formcheck.request(db, "depop", formcheck.HAND, at=T0)
    assert formcheck.due(s, db, "depop", T0) == formcheck.HAND           # `thrift canary --check`: never skipped
    _cross_check(monkeypatch, Outcome("failed", error=TIMEOUT["depop"]))
    st = asyncio.run(formcheck.run_cross(s, db, {"depop": SimpleNamespace(last_structure=None)}, "depop",
                                         formcheck.HAND, at=T0))
    assert not st.get("degraded") and _alarms(said) == []
    assert json.loads(db.kv_get(formcheck.REQUESTS) or "{}") == {}       # not asked again by itself (it would loop)


@pytest.mark.quiet_on
def test_no_checks_in_the_quiet_hours(tmp_path, said):
    """§3: never in the quiet hours (8:30 AM – 2:00 PM) — nothing new from 8:20; the 2:00 PM start's check after."""
    s = _settings(tmp_path)
    s.path("db").parent.mkdir(parents=True, exist_ok=True)
    db = DB(s.path("db"))
    formcheck.request(db, "vinted", "the poster started", at=at(2026, 11, 10, 13, 0))
    assert formcheck.due(s, db, "vinted", at(2026, 11, 10, 13, 19, 59)) == "the poster started"   # 8:19:59 EST
    for h, m in ((13, 20), (13, 30), (16, 0), (18, 59)):                 # 8:20, 8:30, 11:00, 1:59 PM EST
        assert formcheck.due(s, db, "vinted", at(2026, 11, 10, h, m)) is None
    assert formcheck.due(s, db, "vinted", at(2026, 11, 10, 19, 0)) == "the poster started"          # 2:00 PM


def test_once_at_night_dst_safe_and_a_night_the_mac_slept_through_is_let_go(tmp_path, said):
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert cfg["formcheck"]["slots"] == ["02:00"] and formcheck.SLOTS == ("02:00",)
    s = _settings(tmp_path)                                             # the default: 02:00
    s.path("db").parent.mkdir(parents=True, exist_ok=True)
    db = DB(s.path("db"))
    assert formcheck.due(s, db, "depop", at(2026, 11, 10, 7, 5)) == "the 02:00 check"            # 2:05 AM EST
    assert formcheck.due(s, db, "depop", at(2026, 11, 10, 10, 59)) == "the 02:00 check"          # 5:59 AM: still
    assert formcheck.due(s, db, "depop", at(2026, 11, 10, 11, 1)) is None                        # 6:01 AM: let go
    formcheck._taken(db, "depop", "the 02:00 check", at(2026, 11, 10, 7, 5))
    assert formcheck.due(s, db, "depop", at(2026, 11, 10, 19, 0)) is None                        # the afternoon: no slot
    assert formcheck.due(s, db, "depop", at(2026, 11, 11, 7, 1)) == "the 02:00 check"            # the next night
    # DST: the fall-back night (Nov 1, 2026: 2:00 EST is 07:00 UTC) and the spring-forward one (Mar 8, 2026: 2:00 is
    # skipped — the slot is 3:00 EDT, 07:00 UTC)
    assert formcheck.last_slot(s, at(2026, 11, 1, 7, 0)).strftime("%m-%d %H:%M %Z") == "11-01 02:00 EST"
    assert formcheck.last_slot(s, at(2026, 11, 1, 6, 30)).strftime("%m-%d") == "10-31"          # 1:30 EST: not yet
    assert formcheck.last_slot(s, at(2026, 3, 8, 7, 0)).astimezone(UTC).strftime("%H:%M") == "07:00"
    assert formcheck.last_slot(s, at(2026, 3, 8, 6, 59)).strftime("%m-%d") == "03-07"
    s.data["formcheck"]["slots"] = [120]                                # an unquoted YAML 2:00 (minutes)
    assert formcheck.last_slot(s, at(2026, 11, 10, 7, 5)).strftime("%H:%M") == "02:00"


def test_a_failed_check_is_lifted_by_a_real_listing_since(s, db, said, monkeypatch):
    """The Mac at 2:00 PM today: Depop "degraded" by the 8:37 timeout (fails 1, today's failed ["08:30"]) although its
    real items went up from 8:42. The first turn after the deploy sees them: a pass, the no-verdict failure off the
    day's list, one private line."""
    monkeypatch.setattr(formcheck, "_module", lambda name: None)       # the 🚨 itself: tested above
    formcheck._taken(db, "depop", "the 08:30 check", T0)
    formcheck.record(s, db, "depop", Outcome("failed", error=TIMEOUT["depop"]), "the 08:30 check",
                     at=at(2026, 11, 9, 13, 40, 25))
    assert formcheck.degraded(db, "depop") and formcheck.state(db, "depop")["today"]["failed"] == ["08:30"]
    iid = _item(db, 1)
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/x-856d/",
                      posted_at="2026-11-09T13:42:42+00:00")
    formcheck.due(s, db, "depop", at(2026, 11, 9, 19, 0, 30))
    st = formcheck.state(db, "depop")
    assert not st["degraded"] and st["ok"] and st["proven_by"] == iid
    assert st["today"] == {"day": "2026-11-09", "passed": ["14:00"], "failed": []}    # the false 8:30 ✗ gone
    assert [m for m in said.ops if m.startswith("✅")] == ["✅ Depop's form works again — a real listing went live there."]
    formcheck.due(s, db, "depop", at(2026, 11, 9, 19, 1))
    assert len([m for m in said.ops if m.startswith("✅")]) == 1          # once
