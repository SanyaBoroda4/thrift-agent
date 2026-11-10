"""WO53 — Nov 8 (2026-11-08): three items priced, nothing reported as posted. Poshmark took all three and named each
one in the address it landed on (/closet/<user>?created_listing_id=<id>), but its closet's first page didn't show them
for the 90 s the poster watched: the poster asked the owner to check Poshmark and reply 'posted <url>' or 'retry',
counted the three as failures in a row, paused Poshmark and said "nothing was published". Depop and Vinted had been
logged out since Nov 7, one group line then 31 hours of silence; a test run that met the login page ran again every
~5 s; a hidden reCAPTCHA helper frame on an ordinary Vinted page paused Vinted for a CAPTCHA nobody could see; the
status message counted a test item and asked for a 'retry'.

Now: the site's own data is the truth (the id Poshmark named → its listing page; the closet newest first); nobody is
asked to check a site; no second click without proof of absence (two absent reads 10+ min apart, the last 30+ min after
the click; unreadable is never absent); an unconfirmed publish never counts toward a pause; an item live on one site is
finished on the others the same night; every line true and whole. Fakes only: no network, no browser."""
from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from thrift_agent import alarm, crosslist, daily, health, notify, postcheck
from thrift_agent.db import DB
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import Outcome
from thrift_agent.schema import Render

from test_crosslist_flow import _settings, fields_for

ROOT = Path(__file__).resolve().parents[1]
POSH = "https://poshmark.com"
USER = "closet"
# Tonight's three listings (their ids carry their creation time: 10:50:38, 10:53:01, 10:55:20 PM EST).
DRESS = ("know one cares Floral Twist-Front Maxi Dress Cream & Pink size S", "6af1430e0ef143d93e2167c3",
         "know-one-cares-Floral-TwistFront-Maxi-Dress-Cream-Pink-size-S")
SHORTS = ("Southern Tide Channel Markers Chino Shorts Khaki size 4, New with Tags", "6af1439db9f4eafeb15ba811",
          "Southern-Tide-Channel-Markers-Chino-Shorts-Khaki-size-4-New-with-Tags")
PANTS = ("Southern Tide Chino Pants Khaki Straight Leg size 9, New with Tags", "6af14428105c58ace72bf5f2",
         "Southern-Tide-Chino-Pants-Khaki-Straight-Leg-size-9-New-with-Tags")
OLDER = [(f"Older Listing Number {n}", f"6ac2{n:04x}dc9435736119201c"[:24]) for n in range(48)]   # Oct 4 ids


def _t(lid: str) -> datetime:
    return datetime.fromtimestamp(int(lid[:8], 16), timezone.utc)


def _state_page(state: dict) -> str:
    return f"<html><head><script>window.__INITIAL_STATE__ = {json.dumps(state)};</script></head><body></body></html>"


class Poshmark:
    """Poshmark's public pages as plain HTTP answers: /listing/<id> (redirected to /listing/<slug>-<id>) and the closet
    newest first, from its page state — `closet` is the first page as it shows now (tonight: the 48 older listings,
    the new one not yet in it)."""

    def __init__(self, listings=(), closet=None, down=False):
        self.listings = {lid: {"title": t, "price": p, "slug": slug} for t, lid, slug, p in listings}
        self.closet = closet if closet is not None else []
        self.down, self.asked = down, []

    def __call__(self, url):
        self.asked.append(url)
        if self.down:
            return None, "", url
        path = url.split("poshmark.com", 1)[-1]
        if path.startswith("/listing/"):
            lid = path.rsplit("-", 1)[-1].rsplit("/", 1)[-1]
            x = self.listings.get(lid)
            if x is None:
                return 404, "Not Found", url
            details = {"id": lid, "title": x["title"], "price_amount": {"val": str(x["price"])},
                       "inventory": {"status": "available"}, "status": "published"}
            return 200, _state_page({"$_listing_details": {"listingDetails": details}}), \
                f"{POSH}/listing/{x['slug']}-{lid}"
        if path.startswith(f"/closet/{USER}"):
            assert "sort_by=added_desc" in path                       # newest first, never "Just Shared"
            rows = [{"id": lid, "title": t, "price": str(p), "price_amount": {"val": str(p)},
                     "created_at": _t(lid).isoformat(), "status": "published", "inventory": {"status": "available"},
                     "cover_shot": {"url": f"https://cdn.example/{lid}.jpg"}} for t, lid, p in self.closet]
            return 200, _state_page({"$_closet": {"listingsPostData": {"data": rows}}}), url
        return 404, "", url


# ---------------------------------------------------------------- §1: Poshmark's own data, wherever its page goes

def test_tonights_titles_match_as_poshmark_slugs_them():
    """'&', commas, 'Twist-Front' (the slug says TwistFront), a lowercase brand, ', New with Tags'."""
    for title, _, slug in (DRESS, SHORTS, PANTS):
        assert postcheck.title_key(title) == postcheck.title_key(slug.replace("-", " "))
        assert postcheck.posh_slug(title) == slug
    assert postcheck.title_key("Know One Cares Floral") == postcheck.title_key("know one cares Floral")
    assert postcheck.title_key("Dress Cream & Pink") == postcheck.title_key("Dress Cream Pink")


def test_tonight_the_named_id_finds_the_listing_the_closet_did_not_show():
    """The case of 2026-11-08: the closet's first page (48 older listings) never showed the new one; Poshmark had named
    it in its landing address. The closet alone says absent — the named id's own page says live."""
    title, lid, slug = DRESS
    clicked = _t(lid).timestamp() - 4
    site = Poshmark(listings=[(title, lid, slug, 20)], closet=[(t, i, 27) for t, i in OLDER])
    url, seen = postcheck.find_poshmark(title, 20, clicked, user=USER, get=site)
    assert url is None and seen["result"] == "absent"                     # what the old closet watch saw
    url, seen = postcheck.find_poshmark(title, 20, clicked, created=lid, user=USER, get=site)
    assert url == f"{POSH}/listing/{slug}-{lid}" and seen["result"] == "found"
    assert seen["how"] == "the listing id Poshmark named"
    assert site.asked[-1] == f"{POSH}/listing/{lid}"                        # the page by its id — no closet needed


def test_the_closet_newest_first_title_price_and_created_after_the_click():
    title, lid, slug = PANTS
    clicked = _t(lid).timestamp() - 4
    site = Poshmark(closet=[(title, lid, 20), *[(t, i, 27) for t, i in OLDER]])
    url, seen = postcheck.find_poshmark(title, 20, clicked, user=USER, get=site)
    assert url == f"{POSH}/listing/{slug}-{lid}", seen
    assert seen["how"].startswith("the closet: our title and price")
    assert postcheck.find_poshmark(title, 25, clicked, user=USER, get=site)[0] is None        # another price
    assert postcheck.find_poshmark(title, 20, clicked + 3600, user=USER, get=site)[0] is None  # made before the click


def test_two_new_listings_with_our_title_the_cover_decides_never_a_guess():
    title, lid, slug = SHORTS
    twin = "6af1439db9f4eafeb15ba812"
    clicked = _t(lid).timestamp() - 4
    site = Poshmark(closet=[(title, twin, 20), (title, lid, 20)])
    url, seen = postcheck.find_poshmark(title, 20, clicked, user=USER, get=site)
    assert url is None and seen["result"] == "unknown" and "never a guess" in seen["why"]   # never "absent" either
    url, seen = postcheck.find_poshmark(title, 20, clicked, user=USER, get=site, cover=lambda u: lid in u)
    assert url == f"{POSH}/listing/{slug}-{lid}" and "cover photo" in seen["how"]


def test_data_that_cannot_be_read_is_never_absent():
    title, lid, _ = DRESS
    url, seen = postcheck.find_poshmark(title, 20, _t(lid).timestamp(), created=lid, user=USER,
                                        get=Poshmark(down=True))
    assert url is None and seen["result"] == "unknown"


def test_an_address_another_item_holds_is_never_taken():
    title, lid, slug = DRESS
    site = Poshmark(listings=[(title, lid, slug, 20)])
    held = {f"{POSH}/listing/{slug}-{lid}"}
    assert postcheck.find_poshmark(title, 20, _t(lid).timestamp(), created=lid, user=USER, get=site,
                                   held=held)[0] is None


def test_a_brand_is_never_all_lowercase_in_a_title():
    """§0.6: tonight's dress went up as "know one cares Floral …" while Poshmark's own brand reads "Know One Cares"."""
    from thrift_agent import titles
    assert titles.display_brand("know one cares") == "Know One Cares"
    assert titles.display_brand("kate spade new york") == "Kate Spade New York"
    assert titles.display_brand("J.Crew") == "J.Crew" and titles.display_brand("Zara") == "Zara"   # left as written
    assert "starts in lower case" in titles.check(DRESS[0])
    fixed = titles.fix(DRESS[0], "know one cares")
    assert fixed == "Know One Cares Floral Twist-Front Maxi Dress Cream & Pink size S" and not titles.check(fixed)
    assert titles.display_in_text("A know one cares dress.", "know one cares") == "A Know One Cares dress."


def test_a_size_on_no_menu_goes_up_as_the_nearest_and_the_description_says_the_label(facts):
    """§0.6: the Southern Tide chinos were labelled 9 and listed as Boys "10" (no 9 on the menu) — the description
    never said 9. Now "Label size 9." is added; nothing when the menu has the size."""
    from thrift_agent import pipeline
    from thrift_agent.schema import Ev
    pants = facts(item_type="chino pants", department="Kids", category="Bottoms", subcategory="Casual",
                  kids_gender="boys", size_printed=Ev(value="9", photos=[1], source="photo", confidence=0.95),
                  size_us=Ev(value="9", photos=[1], source="photo", confidence=0.95))
    assert pipeline.menu_label_line("Khaki chinos.\nNew with tags.", pants) == \
        "Khaki chinos.\nNew with tags.\nLabel size 9."
    assert pipeline.menu_label_line("Khaki chinos.\nLabel size 9.", pants) == "Khaki chinos.\nLabel size 9."
    shorts = pants.model_copy(update={"size_us": Ev(value="4", photos=[1], source="photo", confidence=0.95)})
    assert pipeline.menu_label_line("Khaki shorts.", shorts) == "Khaki shorts."            # Boys 4 is on the menu


def test_a_line_added_to_a_description_goes_before_depops_hashtags():
    from thrift_agent import edits
    assert edits.add_line("Khaki chinos.\nNew with tags.", "Label size 9.") == \
        "Khaki chinos.\nNew with tags.\nLabel size 9."
    assert edits.add_line("Southern Tide Chino Pants\n\nKhaki chinos.\n#southerntide #chinos", "Label size 9.") == \
        "Southern Tide Chino Pants\n\nKhaki chinos.\nLabel size 9.\n#southerntide #chinos"


# ---------------------------------------------------------------- §2: verifying — the schedule and rule 3

def test_the_looks_after_the_click():
    c = datetime(2026, 11, 9, 3, 50, 38, tzinfo=timezone.utc)
    want = [10, 30, 60, 120, 180, 300, 600, 1200, 1800, 2400]
    at, got = c, []
    for _ in want:
        at = postcheck.next_due(c, at)
        got.append(int((at - c).total_seconds()))
    assert got == want


def test_absent_only_with_two_reads_ten_minutes_apart_the_last_thirty_after_the_click():
    c = datetime(2026, 11, 9, 3, 50, 0, tzinfo=timezone.utc)

    def r(minutes, result="absent"):
        return {"at": postcheck.iso(c + timedelta(minutes=minutes)), "result": result}
    assert not postcheck.absent_proven([r(2), r(5)], c)                         # too early, too close
    assert not postcheck.absent_proven([r(25), r(31)], c)                       # 6 min apart
    assert not postcheck.absent_proven([r(10), r(20)], c)                       # the last before 30 min
    assert postcheck.absent_proven([r(20), r(30)], c)
    assert not postcheck.absent_proven([r(20, "unknown"), r(30)], c)            # unreadable never counts
    assert postcheck.absent_proven([r(5), r(20, "unknown"), r(40)], c)


@pytest.fixture
def clock(monkeypatch):
    box = SimpleNamespace(now=datetime(2026, 11, 9, 3, 50, 38, tzinfo=timezone.utc))
    monkeypatch.setattr(postcheck, "now", lambda: box.now)
    return box


@pytest.fixture
def said(monkeypatch):
    out = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(notify, "group", lambda text: out.group.append(text))
    monkeypatch.setattr(notify, "say", lambda text: out.ops.append(text))
    monkeypatch.setattr(notify, "ops", lambda text, **k: out.ops.append(text) or True)
    monkeypatch.setattr(notify, "photo", lambda path, caption: out.ops.append(caption))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: out.ops.append(caption) or True)
    monkeypatch.setattr(crosslist, "announce", lambda s, db, iid: None)
    return out


def _posh_item(db, tmp_path, title=DRESS[0], n=1):
    bid = db.add_batch(f"share_{n}", 3)
    db.set_batch(bid, status="split")
    cover = tmp_path / f"cover{n}.jpg"
    Image.new("RGB", (30, 40), "pink").save(cover)
    iid = db.add_item(bid, n, str(tmp_path / f"item{n}"))
    r = Render(marketplace="poshmark", title=title, description="Dress.", tags=[], brand="Know One Cares",
               department="Women", category="Dresses", subcategory="Maxi", size="S", colors=["Cream"],
               condition="excellent", price=20, photos=[str(cover)], sku=iid)
    db.set_item(iid, status="ready", owner_price=20, gate={"decision": "publish", "reasons": []},
                renders={"poshmark": r.model_dump()})
    return iid, r


class Finder:
    """Poshmark's own data, scripted: each look the next answer — "found", "absent" or "unknown"."""
    name, driver = "poshmark", "playwright"

    def __init__(self, *answers, url=f"{POSH}/listing/{DRESS[2]}-{DRESS[1]}"):
        self.answers, self.url, self.looks, self.held = list(answers), url, [], set()

    async def find_live(self, ctx, r, since, created=None):
        self.looks.append((since, created))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if a == "found":
            return self.url, {"result": "found", "how": "the listing id Poshmark named"}
        if a == "unknown":
            return None, {"result": "unknown", "error": "no answer"}
        return None, {"result": "absent"}

    def listing_address(self, url):
        return url


def _unconfirmed(db, iid, r, created=DRESS[1]):
    out = Outcome("failed", clicked=True, error="PosterError: submitted — verifying: …", created_id=created,
                  clicked_at=_t(DRESS[1]).timestamp() - 4, landing="closet")
    runner.record_outcome(db, iid, "poshmark", r, out, ["poshmark", "depop", "vinted"])


def test_a_publish_not_confirmed_at_once_is_verifying_never_a_question(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    row = db.listing(iid, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(runner.UNCONFIRMED)
    st = crosslist.unconf_state(db, iid, "poshmark")
    assert st["stage"] == "wait" and st["created"] == DRESS[1] and st["round"] == 1
    assert not db.conn.execute("SELECT 1 FROM outbox WHERE kind='unconfirmed'").fetchone()
    assert not said.group and not any(re.search(r"reply|retry|check poshmark", m, re.I) for m in said.ops), said.ops
    assert "verifying" in crosslist.wait_line(db, iid, "poshmark", row)            # the ✅ card's own words


def _look(s, db, ps, clock, minutes, clicked):
    clock.now = clicked + timedelta(minutes=minutes)
    return asyncio.run(runner.recheck_unconfirmed(s, db, ps, "poshmark", None))


def test_found_by_its_own_data_it_is_posted_and_its_sites_queued(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    clicked = _t(DRESS[1]) - timedelta(seconds=4)
    finder = Finder("unknown", "found")
    ps = {"poshmark": finder}
    assert _look(s, db, ps, clock, 2, clicked)                                     # unreadable: nothing changes
    assert db.listing(iid, "poshmark")["status"] == "failed"
    assert _look(s, db, ps, clock, 3, clicked)
    row = db.listing(iid, "poshmark")
    assert (row["status"], row["url"]) == ("posted", finder.url)
    assert row["posted_at"] == postcheck.iso(clicked)                              # when it went live, not now
    assert finder.looks[-1][1] == DRESS[1]                                         # the named id was looked up
    assert {x["marketplace"] for x in db.listings_for(iid)} == {"poshmark", "depop", "vinted"}
    assert not said.group and not any("🚨" in m or "⛔" in m for m in said.ops)


def test_unreadable_for_twenty_minutes_no_pause_no_second_click_no_question(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    clicked = _t(DRESS[1]) - timedelta(seconds=4)
    ps = {"poshmark": Finder("unknown")}
    for minutes in (1, 2, 3, 5, 10, 20):
        _look(s, db, ps, clock, minutes, clicked)
    assert db.listing(iid, "poshmark")["status"] == "failed"                       # verifying, never queued again
    assert runner.next_job(s, db, ["poshmark"], False) is None                     # nothing to click
    assert crosslist.blocked(db, "poshmark") is None
    assert not said.group and not said.ops


def test_absent_twice_ten_minutes_apart_after_thirty_minutes_it_goes_up_once_more(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    clicked = _t(DRESS[1]) - timedelta(seconds=4)
    ps = {"poshmark": Finder("absent")}
    for minutes in (2, 3, 5, 10, 20):                     # absent, but no proof yet (the last before 30 min)
        _look(s, db, ps, clock, minutes, clicked)
        assert db.listing(iid, "poshmark")["status"] == "failed", minutes
    _look(s, db, ps, clock, 30, clicked)                    # 30 min after the click, 10+ after another absent read
    assert db.listing(iid, "poshmark")["status"] == "queued"
    assert crosslist.unconf_state(db, iid, "poshmark")["stage"] == "retry"
    assert runner.next_job(s, db, ["poshmark"], False)[0] == iid
    assert crosslist.blocked(db, "poshmark") is None                             # never a failure
    assert len([m for m in said.ops if m.startswith("↻ Poshmark")]) == 1


def test_still_unknown_two_hours_after_the_click_one_alarm(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    clicked = _t(DRESS[1]) - timedelta(seconds=4)
    ps = {"poshmark": Finder("unknown")}
    for minutes in range(10, 181, 10):
        _look(s, db, ps, clock, minutes, clicked)
    alarms = [m for m in said.ops if m.startswith("🚨")]
    assert len(alarms) == 1, said.ops
    assert alarms[0].startswith("🚨 Poshmark: a listing is still not confirmed") and "Paste this to Claude Code" in alarms[0]
    assert not re.search(r"reply '|check poshmark", alarms[0], re.I)
    assert db.listing(iid, "poshmark")["status"] == "failed"                       # never posted again


def test_an_old_unconfirmed_row_from_before_wo53_is_never_posted_again_on_a_guess(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    db.upsert_listing(iid, "poshmark", status="failed", error=runner.UNCONFIRMED + "PosterError: old")
    db.conn.execute("UPDATE listings SET updated_at='2026-10-20T10:00:00+00:00' WHERE item_id=?", (iid,))
    ps = {"poshmark": Finder("absent")}
    for minutes in (0, 15, 45, 90):
        _look(s, db, ps, clock, minutes, clock.now)
    assert db.listing(iid, "poshmark")["status"] == "failed" and not ps["poshmark"].looks   # never looked as "absent"
    assert crosslist.unconf_state(db, iid, "poshmark")["stage"] == "given_up"
    assert len([m for m in said.ops if m.startswith("🚨")]) == 1


def test_three_unconfirmed_in_a_row_never_pause_poshmark(tmp_path, monkeypatch, said):
    """Tonight's three: each List pressed, each unconfirmed — no pause, no ⛔, no question."""
    import test_parallel as tp
    monkeypatch.setattr(parallel, "IDLE", 0.05)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.05)
    monkeypatch.setattr(parallel, "next_gap", lambda schedule: 0.0)
    monkeypatch.setattr(parallel.crosslist, "listing_gap", lambda s, mp: 0.0)
    monkeypatch.setattr(runner, "start_thrift_chrome", lambda s: "not in a test")
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title="x"))))
    monkeypatch.setattr(runner, "_paused", lambda db: None)

    class Unconfirmed(tp.Timed):
        async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
            self.calls.append((r.sku, 0))
            return Outcome("failed", clicked=True, error="PosterError: submitted — verifying", created_id=None)

    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_posh_item(db, tmp_path, title=t, n=n)[0] for n, (t, _, _) in enumerate((DRESS, SHORTS, PANTS), 1)]
    ps = {"poshmark": Unconfirmed("poshmark", 0.0), "depop": tp.Timed("depop", 0.0),
          "vinted": tp.Timed("vinted", 0.0)}
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=tp.fake_open, until_idle=True))
    assert [db.listing(i, "poshmark")["status"] for i in items] == ["failed"] * 3
    assert crosslist.blocked(db, "poshmark") is None, health.state(db, "poshmark")
    assert not any(m.startswith("⛔") for m in said.ops) and not said.group
    assert len(ps["poshmark"].calls) == 3                                           # each pressed once, never again


# ---------------------------------------------------------------- rule 6: finished the same night

def test_an_item_live_on_poshmark_is_finished_on_depop_and_vinted_outside_the_hours(tmp_path, monkeypatch, said):
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    live, _ = _posh_item(db, tmp_path, n=1)
    waiting, _ = _posh_item(db, tmp_path, title=SHORTS[0], n=2)
    db.upsert_listing(live, "poshmark", status="posted", url=f"{POSH}/listing/{DRESS[2]}-{DRESS[1]}",
                      posted_at="2026-11-09T03:50:38+00:00")
    for iid in (live, waiting):
        crosslist.queue(s, db, iid, ["depop", "vinted"])
    monkeypatch.setattr(parallel, "can_post", lambda *a, **k: (False, "outside listing hours ['08:00', '23:00']"))
    assert crosslist.next_job(s, db, mps=["depop"], live_elsewhere=True) == (live, "depop")
    assert crosslist.next_job(s, db, mps=["depop"], live_elsewhere=False)[0] in (live, waiting)
    pending = crosslist.pending(s, db, ["depop", "vinted"], live_elsewhere_only=True)
    assert {iid for iid, _ in pending} == {live}
    assert runner.next_job(s, db, ["poshmark"], False, live_elsewhere=True) is None   # Poshmark: only if live elsewhere


# ---------------------------------------------------------------- §3: the lines tell the truth

def test_the_pause_line_tells_the_truth_and_is_never_cut_mid_word(tmp_path, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    reason = ("failures: 3 listings in a row failed (last: PosterError: after List This Item no listing address: the "
              "page went to https://poshmark.com/closet/closet; the new listing didn't show in the closet within 90 s")
    at = datetime(2026, 11, 9, 3, 58, 0, tzinfo=timezone.utc)
    health.pause(db, "poshmark", reason, "items", error=reason, at=at)
    line = said.ops[-1]
    assert line.startswith("⛔ Poshmark paused at ") and "Live today: 0." in line and "Waiting: 0." in line
    assert "Next try" in line and "nothing was published" not in line
    cut = health.plain("poshmark", reason, "items")
    assert cut.endswith("…") and len(cut) <= 160
    assert cut[:-1].split()[-1] in reason[len("failures: "):].split()          # a whole word before "…"
    assert "the new listing d." not in line                                     # tonight's line, cut mid-word


def test_cut_keeps_whole_words():
    text = "the new listing didn't show in the closet within 90 s (Poshmark named it 6af14428105c58ace72bf5f2)"
    out = health.cut(text, 40)
    assert out.endswith("…") and text.startswith(out[:-1].rstrip()) and len(out) <= 40
    assert out[:-1].split()[-1] in text.split()                                   # a whole word before "…"


def test_the_status_message_never_counts_a_test_item(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, _ = _posh_item(db, tmp_path)
    test_item, _ = _posh_item(db, tmp_path, title="Tan Item", n=2)
    db.set_item(test_item, test=1)
    assert runner.publishable(s, db, ["poshmark"]) == [(iid, "poshmark")]


def test_owner_waits_never_name_a_retry(tmp_path, clock, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r)
    db.add_outbox("1", 5, "unconfirmed", iid, text="⚠️ an old question")
    assert daily.RETRY_WAIT not in daily.owner_waits(s, db)


def test_an_account_page_with_items_waiting_raises_the_alarm_and_the_group_line_again(tmp_path, said):
    """WO56: a sign-out is a human step — said at once, whatever the hour (only a person settles it), in ONE private
    message ("reply show"), then reminded every 12 hours; never the group's line, never a 🚨 (live 11-07/08: one group
    line, then 31 hours of silence while three items waited)."""
    from thrift_agent import humansteps
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, _ = _posh_item(db, tmp_path)
    crosslist.queue(s, db, iid, ["depop"])
    t0 = datetime(2026, 11, 7, 21, 4, 56, tzinfo=timezone.utc)
    health.pause(db, "depop", "Depop: not logged in in the Thrift Chrome — login page", "account", page="login",
                 at=t0)
    assert alarm.tick(db, t0 + timedelta(minutes=1), hours_open=False) == ["depop"]
    assert said.ops[-1].startswith("🔐 Depop wants you to sign in") and "reply show" in said.ops[-1]
    assert said.group == [] and not any(m.startswith("🚨") for m in said.ops)
    assert alarm.tick(db, t0 + timedelta(minutes=25)) == []                      # not again
    assert humansteps.tick(db, t0 + timedelta(hours=11)) == []                   # not again within 12 hours
    assert humansteps.tick(db, t0 + timedelta(hours=13)) == ["depop"]            # 12 h later: once more, privately
    assert said.ops[-1].startswith("🔐 Reminder (") and said.group == []


def test_a_test_run_that_meets_the_login_page_waits_its_cool_down(tmp_path, monkeypatch, said):
    """Live 2026-11-08 10:50–10:59 PM: the paused sites' test runs met the login page and ran again every ~5 s."""
    from thrift_agent.post.base import AccountBlocked
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, _ = _posh_item(db, tmp_path)
    db.upsert_listing(iid, "poshmark", status="posted", url=f"{POSH}/listing/x-{DRESS[1]}")
    crosslist.queue(s, db, iid, ["depop"])
    health.pause(db, "depop", "Depop: not logged in in the Thrift Chrome — login page", "account", page="login",
                 at=datetime.now(timezone.utc) - timedelta(hours=1))
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title="x"), facts=None)))

    class LoggedOut:
        name, driver = "depop", "stub"

        async def post(self, *a, **k):
            raise AccountBlocked("Depop: not logged in in the Thrift Chrome — login page", page="login")

    assert health.test_due(db, "depop") == "auto"
    asyncio.run(parallel._cross_test(s, db, {"depop": LoggedOut()}, "depop", "auto"))
    assert health.test_due(db, "depop") is None                                  # the next one after the cool-down
    assert health.state(db, "depop")["tests"] == 1


def test_an_owners_resume_that_meets_a_failing_test_names_the_real_problem(tmp_path, said):
    """Live 2026-11-09 01:04: `thrift resume depop` → the test met the sell form without its shipping section — the
    pause still read "PAUSED (owner): paused by the owner". Now it is the test's problem, said once."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    health.owner_pause(db, "depop")
    health.owner_resume(db, "depop")
    detail = "Mismatch: package nothing (plan package (any of Depops; the plan Small))"
    health.tested(db, "depop", False, detail)
    p = health.state(db, "depop")
    assert p["kind"] == "site" and not p["owner"] and "package nothing" in p["reason"]
    assert [m for m in said.ops if m.startswith("⛔ Depop paused at ")]                    # said once
    health.tested(db, "depop", False, detail)
    assert len([m for m in said.ops if m.startswith("⛔ Depop paused at ")]) == 1
    assert "paused by the owner" not in health.summary(db)[0]


def test_a_new_landing_is_said_once(tmp_path, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    assert not postcheck.landed(db, "poshmark", "closet", say=said.ops.append)    # known since 2026-10-03
    assert postcheck.landed(db, "poshmark", "other:share", say=said.ops.append)
    assert not postcheck.landed(db, "poshmark", "other:share", say=said.ops.append)
    assert said.ops == ["🩹 Poshmark now lands on a new page (share) after List — handled."]
    assert postcheck.landing_kind("poshmark", f"{POSH}/closet/{USER}?created_listing_id={DRESS[1]}") == "closet"


def test_two_jobs_of_one_site_never_run_side_by_side(monkeypatch):
    """Live 2026-11-09 01:01: an asked-for dry run beside the site's own form check — the canary's form compared with
    the dress's plan (the poster object carries the job's fields and its go-ahead). Another site still runs beside."""
    running, overlap, order = set(), [], []

    async def fake(s, db, ps, ctx, iid, mp, **kw):
        if mp in running:
            overlap.append(iid)
        running.add(mp)
        order.append((iid, mp, "start"))
        await asyncio.sleep(0.05)
        running.discard(mp)
        order.append((iid, mp, "end"))

    monkeypatch.setattr(runner, "_run_cross", fake)

    async def go():
        await asyncio.gather(runner.run_cross(None, None, {}, None, "a", "vinted", dry=True),
                             runner.run_cross(None, None, {}, None, "b", "vinted", dry=True),
                             runner.run_cross(None, None, {}, None, "c", "depop", dry=True))
    asyncio.run(go())
    assert overlap == []
    vinted = [x for x in order if x[1] == "vinted"]
    assert [x[2] for x in vinted] == ["start", "end", "start", "end"]
    assert order.index(("c", "depop", "start")) < order.index(("a", "vinted", "end"))   # Depop beside Vinted


def test_a_brands_own_repeated_word_is_never_cut():
    """WO53 (tonight's review proposed "Lauren Ralph Striped Dress Shirt"): a brand's own name repeats no word; a brand
    word repeated after it still goes (WO51: "… Zara Moto Jacket")."""
    from thrift_agent import titles
    t = "Lauren Ralph Lauren Striped Dress Shirt Classic Fit Cotton size 15.5"
    assert titles.check(t) == [] and titles.fix(t, "Lauren Ralph Lauren") == t
    assert titles.fix("Zara TRF Collection Leather Zara Moto Jacket size S", "Zara") == \
        "Zara TRF Collection Leather Moto Jacket size S"


def test_thrift_text_sets_a_title_only_where_it_differs_and_adds_a_line(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from thrift_agent import cli, edits
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    db.upsert_listing(iid, "poshmark", status="posted", url=f"{POSH}/listing/{DRESS[2]}-{DRESS[1]}")
    new = "Know One Cares Floral Twist-Front Maxi Dress Cream & Pink size S"
    db.upsert_listing(iid, "vinted", status="posted", url="https://www.vinted.com/items/10730867983",
                      fields_json={"title": new, "description": "Dress.\nNew with tags."})
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    out = CliRunner().invoke(cli.app, ["text", iid, "--title", new, "--add-line", "Label size 9."],
                             terminal_width=200)
    assert out.exit_code == 0, out.output
    rows = {r_["marketplace"]: json.loads(r_["payload"]) for r_ in db.conn.execute(
        "SELECT marketplace, payload FROM listing_edits WHERE item_id=? AND kind='text'", (iid,))}
    assert rows["poshmark"]["title"] == new and rows["poshmark"]["description"].endswith("\nLabel size 9.")
    assert "title" not in rows["vinted"] and rows["vinted"]["description"] == "Dress.\nNew with tags.\nLabel size 9."
    assert edits.add_line("a\n#x #y", "b") == "a\nb\n#x #y"


def test_the_reconciler_records_tonights_unrecorded_listing_once(tmp_path, clock, said, monkeypatch):
    """§7: tonight's three, had nobody looked — live on Poshmark, our records "verifying": Poshmark's closet shows
    them; created after our click, our title, our price, our cover → recorded, ONE private line; never a listing made
    before our click, nor one for an item we never clicked for."""
    from thrift_agent import reconcile
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, r = _posh_item(db, tmp_path)
    _unconfirmed(db, iid, r, created=None)                                  # no id named: verifying
    queued, _ = _posh_item(db, tmp_path, title=SHORTS[0], n=2)
    db.upsert_listing(queued, "poshmark", status="queued")                 # never clicked: never adopted
    site = Poshmark(closet=[(DRESS[0], DRESS[1], 20), (SHORTS[0], SHORTS[1], 20),
                            (DRESS[0], "6ae7aa61d73373a5d34b8611", 20)])     # an older listing with our title
    clock.now = _t(DRESS[1]) + timedelta(minutes=30)
    monkeypatch.setattr(runner.crosslist, "queue", lambda *a, **k: [])
    out = asyncio.run(reconcile.run(s, db, {"poshmark": Finder("unknown")}, now=clock.now, get=site,
                                    cover_check=lambda ours, theirs: DRESS[1] in theirs))
    assert out["record"] == [(iid, f"{POSH}/listing/{DRESS[2]}-{DRESS[1]}")]
    assert db.listing(iid, "poshmark")["status"] == "posted"
    assert db.listing(queued, "poshmark")["status"] == "queued"
    assert [m for m in said.ops if m.startswith("🔗")] == [
        f"🔗 Poshmark: {DRESS[0]} was live but not recorded — recorded now: {POSH}/listing/{DRESS[2]}-{DRESS[1]}"]
    again = asyncio.run(reconcile.run(s, db, {"poshmark": Finder("unknown")}, now=clock.now, get=site,
                                      cover_check=lambda ours, theirs: True))
    assert again["record"] == [] and len([m for m in said.ops if m.startswith("🔗")]) == 1


def test_two_live_listings_for_one_item_are_said_once_and_nothing_deleted(tmp_path, clock, said):
    from thrift_agent import reconcile
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, _ = _posh_item(db, tmp_path)
    db.upsert_listing(iid, "poshmark", status="posted", url=f"{POSH}/listing/{DRESS[2]}-{DRESS[1]}",
                      listing_id=DRESS[1])
    twin = "6af1430e0ef143d93e2167c4"
    site = Poshmark(closet=[(DRESS[0], DRESS[1], 20), (DRESS[0], twin, 20)])
    for _ in range(2):
        asyncio.run(reconcile.run(s, db, {"poshmark": Finder("unknown")}, now=_t(DRESS[1]) + timedelta(hours=1),
                                  get=site, cover_check=lambda ours, theirs: True))
    lines = [m for m in said.ops if m.startswith("⚠️ Two live Poshmark listings")]
    assert len(lines) == 1 and twin in lines[0] and DRESS[1] in lines[0] and "nothing deleted" in lines[0]


# ---------------------------------------------------------------- grep: nobody is asked to check a site

PROMPTS = re.compile(r"reply '(?:posted|retry)|Reply '(?:posted|retry)|'posted <(?:url|link)>'|"
                     r"\bCheck (?:Poshmark|Depop|Vinted)\b|check the closet, then|asks for 'retry'", re.I)


def _strings(path: Path):
    """The string constants of a module, its docstrings and comments left out (they may tell the history)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def test_no_message_asks_the_owner_to_check_a_site_or_reply_posted_or_retry():
    hits = []
    for base in ("thrift_agent", "api/thrift_api"):
        for path in sorted((ROOT / base).rglob("*.py")):
            if path.name == "cli.py":                 # the owner's own commands' help texts
                continue
            for line, text in _strings(path):
                if PROMPTS.search(text):
                    hits.append(f"{path.relative_to(ROOT)}:{line}: {text[:100]!r}")
    assert not hits, "\n".join(hits)


def test_a_failed_private_pull_rolls_the_deploy_back():
    """Live 2026-11-09 01:44: `git -C private pull` failed in the nohup deploy ("fatal: failed to get: -25308", the
    keychain) after the code was pulled — mac_deploy.sh exited before its tests with the new code checked out, and the
    poster started on it. Now every failure before the tests goes back to the commit before the pull."""
    script = (ROOT / "deploy" / "mac_deploy.sh").read_text(encoding="utf-8")
    assert 'git -C private pull --ff-only ../.private.bundle main || back "' in script
    assert "git -C private pull --ff-only \\\n      || back \"" in script
    body = script[script.index("back() {"):script.index("if [ -d private/.git ]")]
    assert 'git reset --hard "$before"' in body and "exit 1" in body
