"""WO39 — Oct 16 (2026-10-16): three shares, the brown Alo bralette never posted, the Sam Edelman refused twice on
Vinted. The same item is never listed twice (an earlier twin retired, or the sites where it is live skipped), a size
reply applies to its own card only, a refused form is said with its error and never posted again by itself, and the 💤
line names what waits for the owner — "All done" only when nothing does. A fake bot; no network, no browser."""
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import approve, crosslist, daily, pipeline
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.schema import Ev
from thrift_agent.telegram import Bot

CHAT, OWNER, OPS = 100, 7, 555
ROOT = Path(__file__).resolve().parents[1]
GREY = "Alo Yoga Heather Gray Sports Bra Crisscross Back"
BROWN = "Alo Yoga Sports Bralette Mauve Taupe Elastic Logo Band"
OLD_BROWN = "Alo Yoga Mauve Taupe Sports Bra Logo Elastic Band"


class FakeBot(Bot):
    def __init__(self, chat=CHAT):
        super().__init__("TOK", chat, {OWNER})
        self.calls, self.next_id = [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True

    def texts(self):
        return [p.get("text") or p.get("caption") for m, p in self.calls if m in ("sendMessage", "sendPhoto")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    s = Settings(data)
    s.ensure_dirs()
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(crosslist.notify, "say", said.append)
    return s, DB(s.path("db")), said


def _item(db, tmp_path, facts, title, status="awaiting_price", twin=None, size=None, name="item"):
    bid = db.add_batch(f"share-{name}", 3)
    db.set_batch(bid, status="split")
    d = tmp_path / name
    (d / "photos").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (30, 40), "grey").save(d / "cover.jpg")
    iid = db.add_item(bid, 1, str(d))
    question = (f"looks like item {twin} ({OLD_BROWN}) — same item? Reply 'same item' to drop it; a price lists it "
                f"(WO37: a warning, never a hold)") if twin else None
    db.set_item(iid, status=status, facts=facts(item_type="sports bra", category="Intimates & Sleepwear",
                                                 subcategory="Sports Bras", size_printed=Ev(),
                                                 size_us=facts().size_us if size else Ev()).model_dump(),
                price={"target": 25, "list_price": 25, "source": "brand", "by_marketplace": {"poshmark": 25},
                       "basis": "brand"},
                renders={mp: {"marketplace": mp, "title": title, "description": "Bralette.", "size": size,
                              "size_value": size, "price": 25} for mp in ("poshmark", "depop", "vinted")},
                gate={"decision": "needs_info" if twin else "publish", "reasons": [question] if twin else [],
                      "questions": [question] if twin else [], "notes": [],
                      "info": [] if size else ["size: the best reading, none read"]})
    return iid


def _rows(db, iid):
    return {r["marketplace"]: (r["status"], r["error"]) for r in db.listings_for(iid)}


# ---------------------------------------------------------------- the same item is never listed twice

def test_an_old_twin_that_isnt_live_is_retired_when_the_new_card_is_priced(env, tmp_path, facts):
    s, db, said = env
    old = _item(db, tmp_path, facts, OLD_BROWN, status="ready", name="old")          # Oct 13: priced, waits for a size
    db.set_item(old, owner_price=25)
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(old, mp, status="skipped", error="skipped: no size on the item")
    db.add_outbox(str(CHAT), 415, "item", old, text=OLD_BROWN)
    new = _item(db, tmp_path, facts, BROWN, twin=old, name="new")                    # Oct 16: "looks like item …"
    assert pipeline.set_price(s, db, new, 25) == "ready"
    assert db.item(old)["status"] == "dropped"
    assert {mp: st for mp, (st, _) in _rows(db, old).items()} == {"poshmark": "skipped", "depop": "skipped",
                                                                   "vinted": "skipped"}
    assert db.conn.execute("SELECT resolved_at FROM outbox WHERE ref=?", (old,)).fetchone()[0]   # its card closed
    assert [x for x in said if x.startswith("♻️")] == [
        f"♻️ {BROWN}: the same item was shared before (“{OLD_BROWN}”) — that earlier card is retired, this one is "
        f"the one listed"]
    with pytest.raises(ValueError, match="shared again"):                            # a late 'size S' to the old card
        pipeline.set_size(s, db, old, "S")
    with pytest.raises(ValueError, match="shared again"):
        pipeline.set_price(s, db, old, 25)
    pipeline.settle_twin(s, db, new)                                                 # once: nothing said twice
    assert len([x for x in said if x.startswith("♻️")]) == 1


def test_an_old_twin_live_on_a_site_keeps_the_new_one_off_that_site_only(env, tmp_path, facts):
    s, db, said = env
    old = _item(db, tmp_path, facts, OLD_BROWN, status="posted", size="S", name="old")
    db.set_item(old, owner_price=25)
    db.upsert_listing(old, "poshmark", status="posted", url="https://poshmark.com/listing/x-6ad2")
    db.upsert_listing(old, "depop", status="posted", url="https://www.depop.com/products/x/")
    db.upsert_listing(old, "vinted", status="skipped", error="skipped: no size on the item")
    new = _item(db, tmp_path, facts, BROWN, twin=old, name="new")
    pipeline.set_price(s, db, new, 25)
    assert db.item(old)["status"] == "posted"                                        # never touched
    assert _rows(db, new) == {"poshmark": ("skipped", f"skipped: already live as {old}"),
                              "depop": ("skipped", f"skipped: already live as {old}")}
    assert [x for x in said if x.startswith("♻️")] == [
        f"♻️ {BROWN}: the same item is already live on Poshmark and Depop (“{OLD_BROWN}”) — not listed there again"]
    # her size later puts the sites it skipped for want of a size back in line — never the ones its twin holds
    db.upsert_listing(new, "vinted", status="skipped", error="skipped: no size on the item")
    pipeline.set_size(s, db, new, "S")
    assert _rows(db, new) == {"poshmark": ("skipped", f"skipped: already live as {old}"),
                              "depop": ("skipped", f"skipped: already live as {old}"), "vinted": ("queued", None)}


def test_a_card_with_no_twin_is_priced_as_before(env, tmp_path, facts):
    s, db, said = env
    iid = _item(db, tmp_path, facts, BROWN, size="S")
    assert pipeline.set_price(s, db, iid, 25) == "ready"
    assert _rows(db, iid) == {} and not said


# ---------------------------------------------------------------- a size reply applies to its own card only

def _reply(db, s, bot, text, to=None):
    msg = {"message_id": 900 + len(bot.calls), "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text}
    if to is not None:
        msg["reply_to_message"] = {"message_id": to}
    token = approve._REPLYING.set(True)
    try:
        return approve._route_message(s, db, bot, msg)
    finally:
        approve._REPLYING.reset(token)


def test_a_size_reply_sets_the_size_of_the_card_it_answers_and_no_other(env, tmp_path, facts, monkeypatch):
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    grey = _item(db, tmp_path, facts, GREY, name="grey")
    brown = _item(db, tmp_path, facts, BROWN, name="brown")
    for iid, mid in ((grey, 451), (brown, 454)):
        db.add_outbox(str(CHAT), mid, "item", iid, text="card")
        pipeline.set_price(s, db, iid, 25)
    _reply(db, s, bot, "size S", to=451)                                     # her reply to the GREY card
    assert db.item(grey)["owner_size"] == "S" and db.item(brown)["owner_size"] is None
    out = _reply(db, s, bot, "size S")                                       # typed, replying to nothing: never guessed
    assert db.item(brown)["owner_size"] is None and "reply to its card" in out
    assert bot.texts()[-1] == "Which item? Reply 'size …' to its card (swipe the card to reply)"


def test_a_price_on_a_card_with_no_size_queues_it_with_the_agents_size(env, tmp_path, facts, monkeypatch):
    """WO39/WO43 said "tap a size to list it" and the item waited; WO58 (the owner: nothing waits for a size): the
    card's ⭐ is its size at once, the item is queued, the size rows stay for her tap."""
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    brown = _item(db, tmp_path, facts, BROWN, name="brown")
    db.add_outbox(str(CHAT), 454, "item", brown, text="card")
    _reply(db, s, bot, "25", to=454)
    edits = [p for m, p in bot.calls if m in ("editMessageCaption", "editMessageMedia", "editMessageReplyMarkup")]
    assert edits and "queued" in str(edits[-1]) and "tap a size to list it" not in str(edits[-1])
    it = db.item(brown)
    assert it["agent_size"] and it["status"] == "ready"
    assert "tap another on its card to change it" in bot.texts()[-1]


# ---------------------------------------------------------------- a refused form

def test_a_field_error_is_tried_again_by_itself_another_way_then_one_line_and_the_alarm(env, tmp_path, facts,
                                                                                         monkeypatch):
    """WO54 §2 (the owner: every failure is debugged, fallen back on and tried again by itself): a form refused with a
    field error goes back in line by itself 30 minutes later, another way — a fresh page, a stricter title, the site's
    own data looked at first; refused again, ONE private ⛔ line (true: tried, refused again, the code must change) and
    ONE 🚨; then it is tried once more by itself only after a code update (WO39/WO53 said "never posted again by
    itself"). Nothing asks; her own 'retry' still works."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    s, db, said = env
    ops = FakeBot(OPS)
    monkeypatch.setattr(approve, "ops_bot_for", lambda *_: ops)
    monkeypatch.setattr(crosslist.notify, "ops_photo", lambda *a, **k: True)
    monkeypatch.setattr(crosslist, "code_version", lambda: "v1")
    title = "Sam Edelman Daniella Snake Print Ankle Sandals size 8.5"
    iid = _item(db, tmp_path, facts, title, status="ready", size="8.5", name="sam")
    db.set_item(iid, owner_price=25)
    db.upsert_listing(iid, "vinted", status="posting", attempts=1)
    out = SimpleNamespace(status="failed", url=None, clicked=False, guesses=[], screenshot=None, note=None,
                          error="Vinted refused the form: Brand: Fill in brand to continue (its requests after "
                                "the click: /api/v2/item_upload/items 400)")
    crosslist.record(s, db, iid, "vinted", title, out, {})
    assert ops.texts() == [] and not any(m.startswith(("🚨", "⛔")) for m in said)   # the first time: quietly
    assert db.listing(iid, "vinted")["status"] == "failed"
    now = datetime.now(timezone.utc)
    assert crosslist.retry_failed(s, db, at=now + timedelta(minutes=20)) == []        # not before its 30 minutes
    assert crosslist.retry_failed(s, db, at=now + timedelta(minutes=31)) == [(iid, "vinted")]
    assert db.listing(iid, "vinted")["status"] == "queued"
    assert crosslist.unconf_state(db, iid, "vinted")["stage"] == "retry"            # the site's own data looked at first
    assert crosslist.strict_level(db, iid, "vinted") == 1                          # … and a stricter title

    crosslist.set_unconf(db, iid, "vinted", stage="reposted")                      # (run_cross, right before the post)
    db.upsert_listing(iid, "vinted", status="posting")
    crosslist.record(s, db, iid, "vinted", title, out, {})                         # refused again: now it is said
    assert ops.texts() == [f"⛔ {title}: Vinted refused the form — Brand: Fill in brand to continue (its requests "
                           "after the click: /api/v2/item_upload/items 400). It isn't on Vinted (its own data agrees). "
                           "Tried again by itself another way and refused again: the code must change for it (the 🚨 "
                           "has the request); it is tried once more by itself after the next code update."]
    assert "retry" not in ops.texts()[0].lower().replace("tried", "")          # nothing asked
    assert len([m for m in said if m.startswith("🚨") and "refused the form" in m]) == 1   # the fix request, once
    assert tuple(db.conn.execute("SELECT kind, ref FROM outbox WHERE chat_id=?", (str(OPS),)).fetchone()) == \
        ("refused", f"{iid}:vinted")
    db.kv_set(crosslist.WINDOW_SEEN, "yesterday")
    crosslist.new_window(s, db)                                            # the same code: NOT back in line by itself
    assert db.listing(iid, "vinted")["status"] == "failed"
    assert daily.RETRY_WAIT not in daily.owner_waits(s, db)                  # WO53: never waiting for a 'retry'

    monkeypatch.setattr(crosslist, "code_version", lambda: "v2")             # a deploy changed the code
    assert crosslist.retry_failed(s, db) == [(iid, "vinted")]                # tried once more by itself
    assert db.listing(iid, "vinted")["status"] == "queued"
    db.upsert_listing(iid, "vinted", status="posting")
    crosslist.set_unconf(db, iid, "vinted", stage="reposted")
    crosslist.record(s, db, iid, "vinted", title, out, {})                   # still refused: not said twice
    assert len(ops.texts()) == 1
    assert crosslist.retry_failed(s, db) == []                               # once per version, never in a loop

    msg = {"message_id": 70, "chat": {"id": OPS}, "from": {"id": OWNER}, "text": "retry",
           "reply_to_message": {"message_id": ops.next_id}}
    approve._route_ops(s, db, {"message": msg})                               # her own 'retry' still works
    assert db.listing(iid, "vinted")["status"] == "queued"


# ---------------------------------------------------------------- the 💤 line

def test_the_done_line_names_what_waits_for_her_and_all_done_only_when_nothing_does():
    assert crosslist.done_line([], {daily.SIZE_WAIT: ["Alo Yoga Sports Bralette Mauve Taupe"]}) == \
        "💤 Done for now — waiting for a size: Alo Yoga Sports Bralette Mauve Taupe."
    assert crosslist.done_line(["vinted"], {daily.SIZE_WAIT: ["A"], daily.RETRY_WAIT: ["B"]}) == \
        "💤 Done for now — waiting for a size: A; waiting for your 'retry': B. Vinted catches up by itself."
    assert crosslist.done_line([], {daily.SIZE_WAIT: [], daily.RETRY_WAIT: []}) == "💤 All done — nothing is waiting."
    assert crosslist.done_line(None, {daily.SIZE_WAIT: ["A"]}) is None                # work left: no line


def test_an_item_waiting_for_a_size_is_named_and_all_done_never_shows(env, tmp_path, facts):
    s, db, _ = env
    brown = _item(db, tmp_path, facts, BROWN, status="ready", name="brown")
    db.set_item(brown, owner_price=25)
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(brown, mp, status="skipped", error="skipped: no size on the item")
    assert daily.owner_waits(s, db)[daily.SIZE_WAIT] == ["Alo Yoga Sports Bralette Mauve Taupe"]
    w = daily.work(s, db)
    text = daily.status_text(w, daily.PosterNow(True, True), daily.timings(s, db), True, None, "08:00",
                             daily._now())
    assert "All done" not in text and "waiting for a size: Alo Yoga Sports Bralette Mauve Taupe" in text
    if daily.done_state(s, db) is not None:
        assert daily.done_text(s, db) == "💤 Done for now — waiting for a size: Alo Yoga Sports Bralette Mauve Taupe."
    pipeline.set_size(s, db, brown, "S")                                    # her reply: no longer waiting
    assert daily.owner_waits(s, db)[daily.SIZE_WAIT] == []


def test_the_old_all_done_wording_is_gone_from_the_code():
    old = "safe to close the Mac."
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "thrift_agent").rglob("*.py")
            if f"All done — {old}" in p.read_text(encoding="utf-8")]
    assert hits == []


# ---------------------------------------------------------------- follow-ups (the owner, 2026-10-16)

@pytest.mark.parametrize("error,silent", [
    ("Vinted refused the form: no error shown on the form (its requests after the click: /api/v2/x 500)", True),
    ("Vinted refused the form: it kept the sell form (not a listing page: https://www.vinted.com/items/new)", True),
    ("Vinted refused the form: Something went wrong. Please try again.", True),
    ("Vinted refused the form: Brand: Fill in brand to continue", False),
    ("Vinted refused the form: Something went wrong | Title: marked invalid", False),
    ("Vinted refused the form: Fill in brand to continue | Something went wrong", False),
    ("unconfirmed publish: after the click no listing page", False),
])
def test_a_refusal_with_no_field_error_is_silent(error, silent):
    assert crosslist.silent_refusal(error) is silent


def _render_ready(db, iid):
    renders = {mp: {"marketplace": mp, "title": "Sam Edelman Daniella Sandals size 8.5", "description": "Sandals.",
                    "brand": "Sam Edelman", "department": "Women", "category": "Shoes", "subcategory": "Heels",
                    "size": "8.5", "colors": ["Tan"], "condition": "like_new", "price": 25, "photos": [],
                    "sku": iid} for mp in ("poshmark", "depop", "vinted")}
    db.set_item(iid, status="ready", owner_price=25, renders=renders)


def test_a_silent_refusal_is_checked_after_10_minutes_and_posted_once_more_by_itself(env, tmp_path, facts,
                                                                                         monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from thrift_agent.post import runner
    s, db, said = env
    ops = FakeBot(OPS)
    monkeypatch.setattr(approve, "ops_bot_for", lambda *_: ops)
    monkeypatch.setattr(crosslist.notify, "ops_photo", lambda *a, **k: True)
    monkeypatch.setattr(runner.notify, "say", said.append)
    iid = _item(db, tmp_path, facts, "Sam Edelman", status="ready", size="8.5", name="sam")
    _render_ready(db, iid)
    db.upsert_listing(iid, "vinted", status="posting", attempts=1)
    silent = SimpleNamespace(status="failed", url=None, clicked=False, guesses=[], screenshot=None, note=None,
                             error="Vinted refused the form: no error shown on the form")
    crosslist.record(s, db, iid, "vinted", "Sam Edelman", silent, {})
    assert ops.texts() == []                                                 # nothing asked yet
    assert crosslist.rechecks_due(db, "vinted") == []                        # 10 minutes first
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    assert crosslist.rechecks_due(db, "vinted", now=later) == [iid]
    crosslist.set_unconf(db, iid, "vinted", due=datetime.now(timezone.utc).isoformat())

    class Absent:
        held = set()

        async def find_live(self, ctx, r, since, created=None):
            return None, {"listings": 40, "how": "Vinted's newest items, the exact title"}

    from thrift_agent import postcheck
    clicked = postcheck.clicked_of(crosslist.unconf_state(db, iid, "vinted"), db.listing(iid, "vinted"))
    for minutes in (11, 25):                                                 # WO53 rule 3: absent, but no proof yet
        monkeypatch.setattr(postcheck, "now", lambda m=minutes: clicked + timedelta(minutes=m))
        crosslist.set_unconf(db, iid, "vinted", due=postcheck.iso(clicked + timedelta(minutes=minutes)))
        assert asyncio.run(runner.recheck_unconfirmed(s, db, {"vinted": Absent()}, "vinted"))
        assert db.listing(iid, "vinted")["status"] == "failed"
    monkeypatch.setattr(postcheck, "now", lambda: clicked + timedelta(minutes=35))   # 35 min after, 10+ after 25
    crosslist.set_unconf(db, iid, "vinted", due=postcheck.iso(clicked + timedelta(minutes=35)))
    assert asyncio.run(runner.recheck_unconfirmed(s, db, {"vinted": Absent()}, "vinted"))
    assert db.listing(iid, "vinted")["status"] == "queued"                    # posted once more, by itself
    assert crosslist.unconf_state(db, iid, "vinted")["stage"] == "retry"      # ... after one last look
    crosslist.set_unconf(db, iid, "vinted", stage="reposted")                 # (run_cross, right before the post)
    db.upsert_listing(iid, "vinted", status="posting")
    crosslist.record(s, db, iid, "vinted", "Sam Edelman", silent, {})        # refused again: now it is said
    assert len(ops.texts()) == 1 and ops.texts()[0].startswith("⛔ Sam Edelman: Vinted refused the form")
    assert "retry" not in ops.texts()[0]                                     # WO53: never asked
    assert crosslist.unconf_state(db, iid, "vinted") is None
    assert crosslist.rechecks_due(db, "vinted", now=later) == []             # never a third post by itself


def test_an_item_waiting_for_a_size_gets_one_message_and_a_reply_to_it_sets_the_size(env, tmp_path, facts,
                                                                                         monkeypatch):
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    brown = _item(db, tmp_path, facts, BROWN, status="ready", name="brown")
    db.set_item(brown, owner_price=25)
    # WO58: nothing is asked any more — an item that waited for a size before the rule gets the agent's own size and
    # goes back in line; a reply to an old "Waiting for a size" message still sets hers
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(brown, mp, status="skipped", error="skipped: no size on the item")
    assert approve.ask_sizes(s, db) == 0 and bot.texts() == []
    assert db.item(brown)["agent_size"]
    assert {r["status"] for r in db.listings_for(brown)} == {"queued"}         # every site back in line
    db.add_outbox(str(CHAT), 777, "size", brown, text="Waiting for a size: …")  # one sent before WO58
    _reply(db, s, bot, "size S", to=777)
    assert db.item(brown)["owner_size"] == "S"


def test_same_item_on_a_card_whose_twin_was_retired_keeps_it(env, tmp_path, facts, monkeypatch):
    """Live 2026-10-17: 'same item' replied to the new card three minutes after its earlier twin was retired — both
    were dropped and the bralette was listed nowhere. Now the reply is answered and the item stays."""
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    old = _item(db, tmp_path, facts, OLD_BROWN, status="ready", name="old")
    db.set_item(old, owner_price=25)
    new = _item(db, tmp_path, facts, BROWN, twin=old, name="new")
    db.add_outbox(str(CHAT), 454, "item", new, text="card")
    pipeline.set_price(s, db, new, 25)
    assert db.item(old)["status"] == "dropped"
    _reply(db, s, bot, "same item", to=454)
    assert db.item(new)["status"] == "ready"
    assert bot.texts()[-1] == pipeline.TWIN_KEPT
