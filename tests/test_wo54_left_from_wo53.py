"""WO54 §1.7–§1.8, built from the evidence of 2026-11-08/09 (var/arch: the Mac's state.db at 09:58, worker.log/err):
- the 127-photo share 2026-11-08_223855_3036: Azure's copy never thrown away for an iCloud copy still empty, a whole
  upload taken at once, no "📭 still on the iPhone" while Azure holds the photos or once the share is taken;
- the ledger quiet while iCloud is busy with a folder (share_ledger_failed ×4, 00:00–00:00:50);
- the heal's forbidden words (remove, delete, discard, deactivate, sign / log out, address, payment) and links;
- the stale "open problems": failed listing edits and dry runs are no problems, an edit or a drop that can't finish
  is closed (one private line), and nothing keeps evidence beyond the cap;
- a tap answered too late ("query is too old"): what it did stands, no error, no "Sorry" in the group;
- price_differs: our own drops are no hand change, a hand change is said once.
(The bridge's `tidied` event: tests/test_bridge.py; the page side of the heal: tests/js/content.test.mjs.)"""
from __future__ import annotations

import errno
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from thrift_agent import approve, cleanup, cli, heal, photocheck, pipeline, uploads
from thrift_agent.config import ROOT, Settings, settings
from thrift_agent.db import DB
from thrift_agent.telegram import Bot

SID = "2026-11-08_223855_3036"                      # the 127-photo share
APPEARED = datetime(2026, 11, 9, 3, 39, 6, tzinfo=timezone.utc)     # its iCloud folder on the Mac (22:39:06 EST)
NAMES = [f"IMG_{n}.jpg" for n in range(1182, 1301)] + [f"IMG_{n}.jpg" for n in range(1301, 1309)]   # 127


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    clock = {"now": APPEARED.timestamp(), "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), clock["now"]))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), clock["now"]))
    said, group = [], []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    monkeypatch.setattr(cli.uploads, "due", lambda db_: False)        # the uploads are fetched by the test itself
    return s, db, clock, said, group


class Azure:
    """thrift-api's /shares/pending as the Mac sees it, the share growing photo by photo like the live upload."""
    url, key, client, timeout = "https://api.test", "mac-key", None, 15.0

    def __init__(self):
        self.rows, self.posts, self.fetches = {}, [], []

    def upload(self, sid: str, n: int, count: int, ended: bool, started: datetime, last: datetime):
        photos = []
        for k, name in enumerate(NAMES[:n], 1):
            data = b"\xff\xd8\xff\xe0" + name.encode() * 20
            photos.append({"name": name, "n": k, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                           "content_type": "image/jpeg", "received_at": last.isoformat(),
                           "path": f"/shares/{sid}/photo/{name}", "_data": data})
        self.rows[sid] = {"share_id": sid, "count": count, "received": n, "ended": ended, "test": False,
                          "started_at": started.isoformat(), "ended_at": last.isoformat() if ended else None,
                          "last_received_at": last.isoformat(), "photos": photos}

    def get(self, path, params=None):
        assert path == "/shares/pending"
        return {"shares": [r for sid, r in self.rows.items() if sid not in self.fetched()]}

    def fetched(self):
        return {p.split("/")[2] for p, _ in self.posts if p.endswith("/fetched")}

    def post(self, path, body):
        self.posts.append((path, body))
        return {"ok": True}

    def get_bytes(self, path):
        self.fetches.append(path)
        sid, name = path.split("/")[2], path.rsplit("/", 1)[-1]
        return next(p["_data"] for p in self.rows[sid]["photos"] if p["name"] == name), "image/jpeg"


def kinds(db, kind):
    return [json.loads(r["detail"]) if r["detail"] and r["detail"][:1] in "{[" else r["detail"]
            for r in db.conn.execute("SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


def icloud_folder(s, clock, at: datetime, marker: str | None = "done 127"):
    d = s.path("inbox") / SID
    d.mkdir(parents=True, exist_ok=True)
    if marker:
        (d / "_done.txt").write_text(marker, encoding="utf-8")
    for f in [d, *d.iterdir()]:
        clock["born"][str(f)] = at.timestamp()
    return d


# ---------------------------------------------------------------- §1.7a the 127-photo share

def test_the_127_photo_share_is_taken_from_azure_and_no_one_hears_it_is_on_the_iphone(env):
    """Live: 22:49:08 the upload had 104 of 127 photos, still coming in, never ended — taken as "never ended" and
    settled on the iCloud copy (empty, iCloud still bringing it): Azure told "fetched", its photos gone; 23:08:59 the
    group's 📭 "still on the iPhone"; the iCloud copy whole at 00:02. Now Azure's copy is kept until the Mac has the
    share, the whole upload wins at once, and the iCloud copy coming back empty is the same share: no 📭."""
    s, db, clock, said, group = env
    api = Azure()
    started = APPEARED - timedelta(seconds=6)
    folder = icloud_folder(s, clock, APPEARED)
    at_2249 = datetime(2026, 11, 9, 3, 49, 8, tzinfo=timezone.utc)
    api.upload(SID, 104, 127, ended=False, started=started, last=at_2249 - timedelta(seconds=3))
    assert uploads.fetch_new(s, db, api, at_2249) == []                  # still receiving: not "never ended"
    assert api.posts == [] and kinds(db, "share_same_as_icloud") == [] and api.fetches == []
    assert uploads.holds(db, SID) and folder.is_dir()
    at_2251 = datetime(2026, 11, 9, 3, 51, 40, tzinfo=timezone.utc)
    api.upload(SID, 127, 127, ended=True, started=started, last=at_2251 - timedelta(seconds=10))
    assert uploads.fetch_new(s, db, api, at_2251) == [SID]                # whole: taken at once, from Azure
    taken = pipeline.uploads_dir(s) / SID
    assert len([p for p in taken.iterdir() if p.suffix == ".jpg"]) == 127
    assert not folder.exists()                                            # the iCloud copy deleted (WO47)
    assert kinds(db, "share_copy_deleted")[0]["why"].startswith("the upload is whole")
    assert api.posts == [(f"/shares/{SID}/fetched", {"names": NAMES})] and kinds(db, "share_same_as_icloud") == []
    clock["now"] = at_2251.timestamp() + 60
    assert cli._scan_inbox(s, db) == 1                                   # the upload's folder is the share
    [b] = db.batches("new")
    assert b["src_dir"] == str(taken) and b["n_photos"] == 127
    # iCloud puts its copy back (as it did with the 17-photo share at 00:00) — empty, its mark only — and 30 min on
    folder = icloud_folder(s, clock, at_2251 + timedelta(minutes=2))
    clock["now"] = (at_2251 + timedelta(minutes=40)).timestamp()
    assert cli._scan_inbox(s, db) == 0
    assert group == [] and not any("still on the iPhone" in x for x in said)    # never a 📭
    assert not folder.exists()                                            # the same share: deleted
    assert kinds(db, "batch_same_share")[-1]["empty"] is True
    assert db.kv_get(pipeline.WAITING_SAID) in (None, "{}")


def test_no_still_on_the_iphone_line_while_azure_holds_the_shares_photos(env):
    """The 📭 line is for photos the iPhone never sent: an empty iCloud folder 30 minutes old whose share Azure holds
    (not fetched yet — the API slow, the upload still going) is left alone; once Azure has nothing, the line comes."""
    s, db, clock, said, group = env
    folder = icloud_folder(s, clock, APPEARED)
    uploads.remember(db, [{"share_id": SID, "received": 104, "count": 127, "ended": False}], APPEARED)
    clock["now"] = (APPEARED + timedelta(minutes=30, seconds=1)).timestamp()
    assert pipeline.take_share(s, db, folder) is None
    assert cli._scan_inbox(s, db) == 0 and group == [] and folder.is_dir()
    uploads.remember(db, [], APPEARED + timedelta(minutes=31))           # Azure has nothing of it
    clock["now"] = (APPEARED + timedelta(minutes=31)).timestamp()
    assert cli._scan_inbox(s, db) == 0
    assert len(group) == 1 and group[0].startswith("📭 Photos from your 10:38 PM share are still on the iPhone")


def test_an_icloud_copy_is_never_taken_partial_over_an_upload_holding_more(env):
    """The copy with more photos in hand is the share: an iCloud copy with 3 of 127 at minute 31 waits while Azure
    holds 104 (still coming in); with Azure holding fewer, it is taken partial as before."""
    s, db, clock, said, group = env
    folder = icloud_folder(s, clock, APPEARED)
    for n in (1, 2, 3):
        Image.new("RGB", (30, 40), "red").save(folder / f"IMG_{1181 + n}.jpeg")
        clock["born"][str(folder / f"IMG_{1181 + n}.jpeg")] = APPEARED.timestamp()
    uploads.remember(db, [{"share_id": SID, "received": 104, "count": 127, "ended": False}], APPEARED)
    clock["now"] = (APPEARED + timedelta(minutes=31)).timestamp()
    assert pipeline.share_state(s, folder, pipeline.marker_of(folder, "_done"), 60) == "partial"
    assert pipeline.take_share(s, db, folder) is None and db.batches("new") == [] and said == []
    uploads.remember(db, [{"share_id": SID, "received": 2, "count": 127, "ended": False}], APPEARED)
    assert pipeline.take_share(s, db, folder)[0] == "partial"
    assert said and said[-1].startswith("📸 3 of 127 photos of your 10:38 PM share arrived")


def test_the_ledger_is_quiet_while_icloud_is_busy_with_a_folder(env, monkeypatch):
    """Live 00:00:00–00:00:50 (×4): the 17-photo share's iCloud copy, put back by iCloud, in "Resource deadlock
    avoided" — each look logged share_ledger_failed beside the quiet icloud_busy. Now: one icloud_busy, nothing else;
    a real failure (EACCES) is still logged."""
    s, db, clock, said, group = env
    folder = icloud_folder(s, clock, APPEARED, marker="done 17")
    real = pipeline.marker_of

    def busy(d, marker):
        if d == folder:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(d))
        return real(d, marker)
    monkeypatch.setattr(pipeline, "marker_of", busy)
    for _ in range(4):
        assert pipeline.ready_folders(s, db) == []
    assert kinds(db, "share_ledger_failed") == [] and len(kinds(db, "icloud_busy")) == 1
    monkeypatch.setattr(pipeline, "marker_of", lambda d, m: (_ for _ in ()).throw(OSError(errno.EACCES, "refused")))
    pipeline._share_seen(db, folder, "_done")
    assert len(kinds(db, "share_ledger_failed")) == 1


# ---------------------------------------------------------------- §1.7b the heal's forbidden words

HEAL_CANDS = [
    {"id": "e1", "tag": "input", "type": "text", "label": "Title"},
    {"id": "e2", "tag": "button", "type": "button", "text": "Remove"},             # Depop's ship-from address (WO53)
    {"id": "e3", "tag": "button", "label": "Remove Black", "text": "×"},
    {"id": "e4", "tag": "button", "text": "Change address"},
    {"id": "e5", "tag": "a", "text": "Payments"},
    {"id": "e6", "tag": "button", "text": "Payment methods"},
    {"id": "e7", "tag": "button", "text": "Deactivate account"},
    {"id": "e8", "tag": "a", "text": "Log Out"},
    {"id": "e9", "role": "menuitem", "tag": "div", "text": "Sign out"},
    {"id": "e10", "tag": "button", "text": "Logout"},
    {"id": "e11", "tag": "button", "text": "Discard changes"},
    {"id": "e12", "tag": "button", "text": "Deleting…"},
    {"id": "e13", "tag": "input", "type": "text", "label": "Address line 1"},     # a text box: presses nothing
    {"id": "e14", "tag": "button", "role": "combobox", "label": "Brand", "text": "Select a brand"},
    {"id": "e15", "tag": "button", "text": "Removable hood"},                     # not a "remove" word
]


def test_the_heal_never_offers_remove_delete_discard_deactivate_sign_out_address_or_payment():
    assert [c["id"] for c in heal.allowed(HEAL_CANDS)] == ["e1", "e13", "e14", "e15"]
    for bad in ("e2", "e3", "e4", "e7", "e8", "e9", "e11"):
        def ask(model, system, content, out, tool, description, max_tokens=4096, retries=3, bad=bad):
            return type("A", (), {"pick": bad})()
        assert heal.pick(ask, "element", "depop", "shippingMethods", "", HEAL_CANDS, "m") is None
    assert "removes" in heal.SYSTEM and "address" in heal.SYSTEM
    href = re.compile(heal.NEVER_HREF, re.I)
    for link in ("/logout", "https://www.vinted.com/member/general/logout", "/settings/shipping-address",
                 "/account/payments", "/sign-out", "/sign_out"):
        assert href.search(link), link
    assert not href.search("/products/create/") and not href.search("/items/new")
    source = (ROOT / "thrift_agent" / "post" / "poshmark.py").read_text(encoding="utf-8")
    assert "heal.SNAPSHOT_JS, [NEVER, heal.MAX_CANDIDATES, heal.NEVER_HREF]" in source   # Poshmark's page too
    assert "const [never, cap, neverHref] = args;" in heal.SNAPSHOT_JS
    common = (ROOT / "ext" / "content" / "common.js").read_text(encoding="utf-8")
    for word in ("remov(e|es|ed|ing)", "deactivat(e|es|ed|ing)", "sign\\s*-?\\s*out", "log\\s*-?\\s*out",
                 "addresse?s?", "payments?", "delet(e|es|ed|ing)", "discard"):
        assert word in common, word                                      # the page says the same words


# ---------------------------------------------------------------- §1.7d the stale "open problems"

def _item(s, db, n: int) -> str:
    bid = db.add_batch(str(s.path("inbox") / f"share-{n}"), 1)
    db.set_batch(bid, status="split")
    d = s.path("work") / bid / "item_01"
    d.mkdir(parents=True, exist_ok=True)
    iid = db.add_item(bid, 1, str(d))
    db.set_item(iid, status="posted", renders={"poshmark": {"title": f"Item {n}"}})
    return iid


def _edit(db, iid, site, kind, status, dry, made, error="", attempts=0, rid=None):
    rid = rid or f"e_{iid[-6:]}_{site}_{kind}_{status}"
    db.conn.execute("INSERT INTO listing_edits (id, item_id, marketplace, kind, payload, status, attempts, error, dry, "
                    "created_at, updated_at, done_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, iid, site, kind, "{}", status, attempts, error, dry, made, made,
                     made if status == "failed" else None))
    db.conn.commit()
    return rid


def _drop(db, iid, site, status, made, stage="auto_1", old=30.0, new=27.0, rid=None):
    rid = rid or f"pc_{iid}_{site}_{stage}"
    db.conn.execute("INSERT INTO price_changes (id, item_id, marketplace, stage, old_price, new_price, status, attempts, "
                    "dry, created_at, updated_at, done_at) VALUES (?,?,?,?,?,?,?,0,0,?,?,?)",
                    (rid, iid, site, stage, old, new, status, made, made, made if status == "done" else None))
    db.conn.commit()
    return rid


def test_failed_listing_edits_and_dry_runs_are_no_open_problems(env):
    """The evidence: ten listing edits of seven items, made and failed Oct 18–19 (five dry runs; the On sneakers'
    Vinted category refused by Vinted, done by a later edit; four photo edits "not the sell form" on items sold since);
    the five Oct-4 items' drops, done on Nov 9. They held 256 evidence files and the folder over its cap."""
    s, db, *_ = env
    oct18 = "2026-10-18T22:11:24+00:00"
    on_sneakers, tie_top, hoodie = _item(s, db, 1), _item(s, db, 2), _item(s, db, 3)
    for site in ("poshmark", "depop", "vinted"):
        db.upsert_listing(on_sneakers, site, status="posted", url=f"https://x/{site}/1")
        db.upsert_listing(tie_top, site, status="posted", url=f"https://x/{site}/2")
    db.upsert_listing(hoodie, "poshmark", status="sold", url="https://x/p/3")
    db.upsert_listing(hoodie, "depop", status="delisted", url="https://x/d/3")
    _edit(db, on_sneakers, "poshmark", "category", "failed", 1, "2026-10-18T03:45:03+00:00",
          "no category field on the edit page")
    _edit(db, on_sneakers, "vinted", "category", "failed", 0, "2026-10-18T04:26:29+00:00",
          "Vinted refused the change (Fill in brand to continue, …)", attempts=4)
    _edit(db, on_sneakers, "vinted", "category", "done", 0, "2026-10-18T04:41:37+00:00", rid="e_later_done")
    _edit(db, tie_top, "vinted", "photos", "failed", 1, oct18, "fill: the listing shows 5 photos, not the 2 recorded")
    _edit(db, hoodie, "depop", "photos", "failed", 0, oct18, "open: not the sell form: https://www.depop.com/…",
          attempts=3)
    for site in ("poshmark", "depop", "vinted"):
        _drop(db, tie_top, site, "done", "2026-11-08T16:28:04+00:00")
    assert cleanup.open_problems(db) == {}
    _edit(db, tie_top, "depop", "photos", "pending", 0, "2026-11-09T07:00:00+00:00")     # one on its way: open
    _drop(db, on_sneakers, "depop", "unconfirmed", "2026-11-09T13:01:00+00:00", stage="auto_2")
    assert cleanup.open_problems(db) == {tie_top: "a listing edit is pending", on_sneakers: "a price change is unconfirmed"}


def test_an_edit_or_a_drop_that_can_t_finish_is_closed_with_one_line(env):
    s, db, clock, said, group = env
    now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    live, sold = _item(s, db, 1), _item(s, db, 2)
    for site in ("poshmark", "depop", "vinted"):
        db.upsert_listing(live, site, status="posted", url=f"https://x/{site}/1")
    db.upsert_listing(sold, "vinted", status="sold", url="https://x/v/2")
    gone = _edit(db, sold, "vinted", "photos", "pending", 0, "2026-11-08T10:00:00+00:00")
    old = _edit(db, live, "depop", "photos", "pending", 0, "2026-10-30T10:00:00+00:00",
                "open: not the sell form", attempts=2)
    young = _edit(db, live, "vinted", "text", "pending", 0, "2026-11-08T10:00:00+00:00")
    dry = _edit(db, live, "poshmark", "category", "pending", 1, "2026-11-07T10:00:00+00:00")
    drop_gone = _drop(db, sold, "vinted", "pending", "2026-11-08T10:00:00+00:00")
    drop_old = _drop(db, live, "depop", "pending", "2026-10-31T10:00:00+00:00", old=55.0, new=49.0)
    lines = cleanup.close_stale(db, now)
    row = lambda t, rid: db.conn.execute(f"SELECT status, error FROM {t} WHERE id=?", (rid,)).fetchone()  # noqa: E731
    assert row("listing_edits", gone)["status"] == "skipped" and "its listing is sold" in row("listing_edits", gone)[1]
    assert row("listing_edits", old)["status"] == "failed" and "not done in 7 days" in row("listing_edits", old)[1]
    assert row("listing_edits", young)["status"] == "pending"            # a day old: still on its way
    assert row("listing_edits", dry)["status"] == "skipped"
    assert row("price_changes", drop_gone)["status"] == "skipped"
    assert row("price_changes", drop_old)["status"] == "skipped"
    assert lines == said and len(said) == 2                               # one line per row closed for its age
    assert said[0].startswith("⚠️ The photos change of Item 1 on Depop wasn't done in 7 days — closed (open: not the")
    assert said[1].startswith("⚠️ The price drop of Item 1 on Depop ($55 → $49) wasn't done in 7 days")
    assert cleanup.open_problems(db) == {live: "a listing edit is pending"}
    assert len(kinds(db, "listing_edit_closed")) == 3 and len(kinds(db, "price_drop_closed")) == 2
    assert cleanup.close_stale(db, now) == []                            # closed once
    # a row the poster touched meanwhile is never overwritten
    racing = _edit(db, live, "depop", "brand", "pending", 0, "2026-10-30T10:00:00+00:00", rid="e_racing")
    stale = db.conn.execute("SELECT * FROM listing_edits WHERE id=?", (racing,)).fetchone()
    db.conn.execute("UPDATE listing_edits SET status='saving', updated_at='2026-11-09T14:59:00+00:00' WHERE id=?",
                    (racing,))
    assert cleanup._close(db, "listing_edits", stale, "failed", "x") is False
    assert row("listing_edits", racing)["status"] == "saving"


def test_the_daily_run_closes_stale_problems_and_the_cap_holds_with_open_ones(env, monkeypatch):
    import os
    s, db, clock, said, group = env
    monkeypatch.setattr(cleanup.notify, "say", said.append)
    monkeypatch.setattr(cleanup.sales, "api", lambda: None)
    now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    stuck, done = _item(s, db, 1), _item(s, db, 2)
    for site in ("poshmark", "depop", "vinted"):
        db.upsert_listing(done, site, status="posted", url=f"https://x/{site}/2")
    db.upsert_listing(stuck, "vinted", status="failed", error="Mismatch: brand shown тщ икфтв")    # open (today's)
    _edit(db, done, "depop", "photos", "failed", 0, "2026-10-18T23:40:53+00:00", "open: not the sell form",
          attempts=3)                                                    # failed for good: no problem
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    def shot(name: str, days: float, size: int) -> None:
        p = shots / name
        p.write_bytes(b"x" * size)
        t = (now - timedelta(days=days)).timestamp()
        os.utime(p, (t, t))
    for k in range(6):
        shot(f"{done}-depop-202610{18 + k:02d}-120000.html.gz", 20 - k, 100)   # the failed edit's: older than 14 days
    for k in range(5):
        shot(f"{stuck}-vinted-20261109-0{k}0000.png", 0.2 - k * 0.01, 100)     # today's open problem
    old_cap = cleanup.EVIDENCE_CAP
    cleanup.EVIDENCE_CAP = 300
    try:
        out = cleanup.run(s, db, now=now, say=False)
    finally:
        cleanup.EVIDENCE_CAP = old_cap
    left = sorted(p.name for p in shots.iterdir())
    assert all(stuck in n for n in left) and len(left) == 3               # the cap holds: the newest three stay
    assert sum(p.stat().st_size for p in shots.iterdir()) <= 300
    assert any("removed though its vinted listing is failed" in n for n in out["notes"])


# ---------------------------------------------------------------- §1.8 a tap answered too late

class LateBot(Bot):
    """Telegram after the Mac slept: every answerCallbackQuery comes back "query is too old"."""
    def __init__(self):
        super().__init__("TOK", "100", {7})
        self.calls, self.updates, self.next_id = [], [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "answerCallbackQuery":
            raise RuntimeError("telegram answerCallbackQuery failed: Bad Request: query is too old and response "
                               "timeout expired or query ID is invalid")
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True


def test_a_price_tapped_while_the_mac_slept_is_set_without_an_error_or_a_sorry(env, monkeypatch, facts):
    """Live 08:36:46–47 (i_261109_fee7ed): two price taps made while the lid was closed came in on the wake; each was
    set (price_set $30, then $20), then answerCallbackQuery failed "query is too old" — telegram_error, "❌ telegram
    update" privately and "Sorry, that didn't go through — please try again." in the group, twice. Now only the toast
    is lost."""
    s, db, clock, said, group = env
    s.data["telegram"] = {**s.data.get("telegram", {}), "enabled": True}
    bot = LateBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    monkeypatch.setattr(approve.notify, "say", said.append)
    bid = db.add_batch(str(s.path("inbox") / "share"), 5)
    db.set_batch(bid, status="split")
    d = s.path("work") / bid / "item_01"
    d.mkdir(parents=True)
    Image.new("RGB", (40, 40), "red").save(d / "cover.jpg")
    iid = db.add_item(bid, 1, str(d))
    db.set_item(iid, status="awaiting_price", facts=facts().model_dump(),
                price={"target": 25, "list_price": 30, "source": "brand", "by_marketplace": {"poshmark": 30}},
                renders={"poshmark": {"marketplace": "poshmark", "title": "Teddy Bear Knit Sweater", "price": 30}},
                gate={"decision": "publish", "reasons": []})
    approve.send_item(s, db, iid)
    card = bot.next_id
    tap = lambda uid, amount: {"update_id": uid, "callback_query": {  # noqa: E731
        "id": f"cb{uid}", "from": {"id": 7}, "data": f"approve:{iid}:{amount}",
        "message": {"message_id": card, "chat": {"id": 100}}}}
    bot.updates = [tap(1, 30), tap(2, 20)]
    assert approve.poll_once(s, db, bot, 0) == 2
    assert db.item(iid)["owner_price"] == 20 and db.item(iid)["status"] == "ready"   # both applied, in her order
    assert kinds(db, "telegram_error") == []
    assert not any("didn't go through" in (p.get("text") or "") for m, p in bot.calls if m == "sendMessage")
    assert not any(x.startswith("❌ telegram update") for x in said)
    assert [m for m, p in bot.calls].count("answerCallbackQuery") == 2                  # tried, quietly lost
    assert Bot("TOK", "100", {7}).answer_callback.__doc__ and LateBot().answer_callback("cb") is False


# ---------------------------------------------------------------- §1.8 price_differs said once, never for our drops

def test_price_differs_is_said_once_per_hand_change_and_never_for_our_own_drop(env):
    """Live 2026-11-09: 79 price_differs for 21 listings — 26 our own drops (Poshmark 30 → 27 done Nov 8, compared
    with the posted 30), 53 fourteen hand changes read again every 2 h (e.g. the On sneakers 135 → 60, 5 times)."""
    s, db, *_ = env
    dropped, sneakers = _item(s, db, 1), _item(s, db, 2)
    db.upsert_listing(dropped, "poshmark", status="posted", url="https://x/p/1", price=30)
    _drop(db, dropped, "poshmark", "done", "2026-11-08T16:28:04+00:00", old=30.0, new=27.0)
    db.upsert_listing(sneakers, "poshmark", status="posted", url="https://x/p/2", price=135)
    for _ in range(4):                                                    # Poshmark every 2 hours
        photocheck.apply(s, db, db.listing(dropped, "poshmark"), {"photos": None, "price": 27})
        note = photocheck.apply(s, db, db.listing(sneakers, "poshmark"), {"photos": None, "price": "$60.00"})
    assert "price 60 on the site vs 135 ours" in note
    rows = kinds(db, "price_differs")
    assert rows == [{"site": "poshmark", "site_price": 60.0, "ours": 135.0}]  # once; our drop: never
    photocheck.apply(s, db, db.listing(sneakers, "poshmark"), {"photos": None, "price": 55})
    assert len(kinds(db, "price_differs")) == 2                          # a new change: said
    assert photocheck.our_price(db, db.listing(dropped, "poshmark")) == 27.0
