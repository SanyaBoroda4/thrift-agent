"""The Telegram approval flow, fully mocked: FakeBot records what would be sent; the DB and pipeline are real
(except confirm, which needs photos on disk and is recorded instead)."""
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from thrift_agent import approve, pipeline
from thrift_agent.approve import (BATCH_HINT, ITEM_HINT, bot_for, handle_update, parse_reply, poll_once, resend_pending,
                                  send_batch, send_item)
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.schema import Ev
from thrift_agent.telegram import Bot

CHAT, OWNER, STRANGER = 100, 7, 8


class FakeBot(Bot):
    """Bot with call() replaced: records (method, params), hands out message ids and the queued updates."""
    def __init__(self, chat_id=CHAT, users=(OWNER,)):
        super().__init__("TOK", chat_id, set(users))
        self.calls, self.updates, self.next_id = [], [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p.get("text") or p.get("caption") for m, p in self.calls if m in ("sendMessage", "sendPhoto")]


def _settings(tmp_path, **telegram):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True, **telegram}
    s = Settings(data)
    s.ensure_dirs()
    return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings with telegram enabled, a DB, and a FakeBot that bot_for() returns."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    return s, db, bot


PRICE = {"target": 70, "list_price": 85, "source": "brand", "by_marketplace": {"poshmark": 85},
         "basis": "brand tier: Tory Burch flats"}
RENDERS = {"poshmark": {"marketplace": "poshmark", "title": "Tory Burch Suede Ballet Flats", "size": "7.5", "price": 85}}


def _item(db, tmp_path, facts, status="awaiting_price", price=PRICE, renders=RENDERS, gate=None, cover=True, **fkw):
    bid = db.add_batch(str(tmp_path / "share" / status), 5)
    db.set_batch(bid, status="split")                              # a confirmed batch: its items exist only then
    d = tmp_path / "items" / status
    (d / "photos").mkdir(parents=True, exist_ok=True)
    if cover:
        Image.new("RGB", (40, 40), "red").save(d / "cover.jpg")
    iid = db.add_item(bid, 1, str(d))
    f = facts(flaws=[{"description": "scuff on left toe", "photos": [4]}, {"description": "heel wear", "photos": [4]}],
              condition="good", retail_price={"value": "198", "photos": [2], "source": "photo", "confidence": 0.9},
              **fkw).model_dump()
    db.set_item(iid, status=status, facts=f, price=price, renders=renders,
                gate=gate or {"decision": "publish", "reasons": []})
    return iid


def _batch(s, db, reasons=None):
    bid = db.add_batch(str(s.path("inbox") / "share"), 3)
    db.set_batch(bid, status="needs_confirm", reasons=reasons or [], segmentation={
        "groups": [[0, 1], [2]], "summaries": ["red suede flats", "blue tee"], "photos": ["a", "b", "c"],
        "kinds": ["own", "own", "own"], "unassigned": [], "dropped": [], "note": None})
    return bid


def _reply(text, reply_to, chat=CHAT, user=OWNER, mid=50):
    return {"update_id": 1, "message": {"message_id": mid, "chat": {"id": chat}, "from": {"id": user}, "text": text,
                                        "reply_to_message": {"message_id": reply_to}}}


def _callback(data, chat=CHAT, user=OWNER, mid=11):
    return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": user}, "data": data,
                                               "message": {"message_id": mid, "chat": {"id": chat}}}}


def _outbox(db, iid_or_bid):
    return db.conn.execute("SELECT * FROM outbox WHERE ref=? ORDER BY message_id", (iid_or_bid,)).fetchall()


# ---------- parse_reply ----------

@pytest.mark.parametrize("text, expected", [
    ("85", (85, None)),
    ("$85", (85, None)),
    ("$ 85", (85, None)),
    ("85.00", (85, None)),
    ("85 dollars", (85, None)),
    ("85 usd", (85, None)),
    ("price 45", (45, None)),
    ("list: 30", (30, None)),
    ("size 8, 45", (45, "size 8")),
    ("45, size 8", (45, "size 8")),
    ("size 8", (None, "size 8")),
    ("size 7.5M, 50", (50, "size 7.5M")),
    ("8 us, 40", (40, "8 us")),
    ("kids 5y, 12", (12, "kids 5y")),
    ("size 8-9", (None, "size 8-9")),
    ("brand vince, NWT, 60", (60, "brand vince, NWT")),
    ("NWT", (None, "NWT")),
    ("", (None, None)),
    ("   ", (None, None)),
    ("0", (None, "0")),
])
def test_parse_reply(text, expected):
    assert parse_reply(text) == expected


# ---------- bot_for ----------

def test_bot_for_needs_enabled_and_both_env_vars(tmp_path, monkeypatch):
    assert bot_for(_settings(tmp_path, enabled=False)) is None
    s = _settings(tmp_path)
    assert bot_for(s) is None                                     # conftest scrubbed the secrets
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    assert bot_for(s) is None
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "7, 8,x")
    b = bot_for(s)
    assert isinstance(b, Bot) and b.token == "TOK" and b.chat_id == "-100999" and b.allowed_users == {7, 8}


def test_bot_for_warns_once_when_nobody_is_allowed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "100")
    monkeypatch.setattr(approve, "_warned_no_users", False)
    s = _settings(tmp_path)
    b = bot_for(s)
    assert b is not None and b.allowed_users == set()
    bot_for(s)
    assert capsys.readouterr().err.count("TELEGRAM_ALLOWED_USER_IDS") == 1


# ---------- outgoing ----------

def test_send_item_caption_buttons_and_outbox(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    (method, p), = bot.calls
    assert method == "sendPhoto" and p["photo"].name == "cover.jpg"
    cap = p["caption"]
    # WO20: cover, title, size, condition in Poshmark's words, the price on the buttons; no flaw count, no basis
    assert cap == "Tory Burch Suede Ballet Flats\nSize 7.5\nCondition: Good"
    assert p["reply_markup"] == {"inline_keyboard": [
        [{"text": "\u2705 $85", "callback_data": f"approve:{iid}:85"}],
        [{"text": f"${n}", "callback_data": f"approve:{iid}:{n}"} for n in (75, 80, 90, 95)],
        [{"text": "Later", "callback_data": f"later:{iid}"}, {"text": "Change", "callback_data": f"change:{iid}"},
         {"text": "Wrong photos", "callback_data": f"regroup:{iid}"}]]}
    (row,) = _outbox(db, iid)
    assert (row["chat_id"], row["message_id"], row["kind"], row["resolved_at"]) == ("100", 11, "item", None)
    assert row["text"] == cap


def test_send_item_shows_only_the_allowed_questions_and_the_warnings(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_info", cover=False,
                price={**PRICE, "list_price": 40, "source": "category_default", "basis": "category default"},
                renders={"poshmark": {"title": "Flats", "size": None, "price": 40}},
                gate={"decision": "needs_info", "reasons": ["brand unclear (None, 0.40)", "lint: title too long"],
                      "questions": ["Brand? Couldn't read it \u2014 reply 'brand \u2026'"],
                      "notes": ["cover: no front flat-lay photo"],
                      "info": ["model unsure of the condition (0.60): listed as good"]})
    send_item(s, db, iid)
    (method, p), = bot.calls
    assert method == "sendMessage"                                # no cover.jpg -> text message
    assert p["text"] == ("Flats\nSize 7.5\nCondition: Good\n\u26a0\ufe0f cover: no front flat-lay photo\n"
                         "\u2753 Brand? Couldn't read it \u2014 reply 'brand \u2026'\n"
                         "Reply to this card with the answer (a price too if you like), e.g. 'size 8, 45'")
    assert "lint" not in p["text"] and "unsure" not in p["text"] and "no price history" not in p["text"]
    assert [b["text"] for b in p["reply_markup"]["inline_keyboard"][1]] == ["$30", "$35", "$45", "$50"]


def test_send_item_processed_before_wo20_keeps_its_recorded_questions(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_info",
                gate={"decision": "needs_info", "reasons": ["brand unclear (None, 0.40)", "size unclear"]})
    send_item(s, db, iid)
    assert "Open questions:\n- brand unclear (None, 0.40)\n- size unclear" in bot.texts()[-1]


def test_send_item_without_price_has_no_price_buttons(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, price={**PRICE, "list_price": None, "source": "none", "basis": ""})
    send_item(s, db, iid)
    p = bot.sent("sendPhoto")[0]
    assert p["caption"].endswith("No price yet: type one")
    assert p["reply_markup"] == {"inline_keyboard": [[{"text": "Later", "callback_data": f"later:{iid}"},
                                                      {"text": "Change", "callback_data": f"change:{iid}"},
                                                      {"text": "Wrong photos", "callback_data": f"regroup:{iid}"}]]}


def test_send_item_without_bot_prints(tmp_path, facts, capsys):
    s = _settings(tmp_path, enabled=False)
    db = DB(s.path("db"))
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    out = capsys.readouterr().out
    assert "[notify]" in out and "Tory Burch Suede Ballet Flats" in out and "cover.jpg" in out
    assert _outbox(db, iid) == []


def test_send_batch_caption_and_outbox(env):
    s, db, bot = env
    bid = _batch(s, db, reasons=["photo 2 in no group"])
    sheet = s.path("work") / bid / "contact_sheet.png"
    sheet.parent.mkdir(parents=True)
    Image.new("RGB", (30, 30), "white").save(sheet)
    send_batch(s, db, bid)
    (method, p), = bot.calls
    assert method == "sendPhoto" and p["photo"] == sheet
    assert p["caption"] == (f"Batch {bid}: 3 photos -> 2 items\nitem 1: red suede flats - photos [0, 1]\n"
                            f"item 2: blue tee - photos [2]\nCheck: photo 2 in no group\n{BATCH_HINT}")
    (row,) = _outbox(db, bid)
    assert (row["message_id"], row["kind"], row["resolved_at"]) == (11, "batch", None)


def test_send_batch_without_sheet_sends_text(env):
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)
    assert [m for m, _ in bot.calls] == ["sendMessage"] and "Check:" not in bot.texts()[0]
    assert _outbox(db, bid)[0]["kind"] == "batch"


def test_the_poster_asks_nothing_an_old_parked_item_is_not_queued(env, tmp_path, facts):
    """WO27: the poster never stops to ask, so its question kind is gone from the queue; an item a poster from before
    parked in needs_owner waits for `thrift requeue`, it is never asked about again."""
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_owner")
    db.set_item(iid, owner_question="Which Poshmark size?")
    assert not hasattr(approve, "ask_owner") and "owner_q" not in approve.QUEUE_KINDS
    approve.pump(s, db)
    assert bot.texts() == [] and _outbox(db, iid) == [] and iid not in approve.queue(db)


# ---------- handle_update ----------

def test_approve_callback_sets_price_and_resolves(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    out = handle_update(s, db, bot, _callback(f"approve:{iid}:85"))
    assert out == f"price {iid}: $85 (ready)"
    it = db.item(iid)
    assert it["status"] == "ready" and it["owner_price"] == 85
    assert loads(it["price"])["source"] == "owner" and loads(it["renders"])["poshmark"]["price"] == 85
    assert all(r["resolved_at"] for r in _outbox(db, iid))
    assert bot.sent("answerCallbackQuery") == [{"callback_query_id": "cb1", "text": "$85"}]
    assert bot.sent("sendMessage")[-1]["reply_to_message_id"] == 11 and bot.texts()[-1] == "\u2713 $85 \u2014 all done"


def test_a_nearby_price_button_sets_that_price(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    assert handle_update(s, db, bot, _callback(f"approve:{iid}:75")) == f"price {iid}: $75 (ready)"
    assert db.item(iid)["owner_price"] == 75


def test_approve_callback_on_a_moved_on_item_replies_with_the_error(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="posted")
    out = handle_update(s, db, bot, _callback(f"approve:{iid}:85"))
    assert out.startswith(f"price {iid}: rejected")
    assert "can't be changed" in bot.texts()[-1] and db.item(iid)["owner_price"] is None


def test_change_callback_records_a_new_outbox_row(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)                                                     # message 11
    assert handle_update(s, db, bot, _callback(f"change:{iid}", mid=11)) == f"change {iid}: asked for the price"
    p = bot.sent("sendMessage")[-1]
    assert p["text"] == f"Reply to this message with the price for {iid} (or just type it)."
    assert p["reply_to_message_id"] == 11
    rows = _outbox(db, iid)
    assert [(r["message_id"], r["kind"], r["resolved_at"]) for r in rows] == [(11, "item", None), (12, "item", None)]
    assert bot.sent("answerCallbackQuery") == [{"callback_query_id": "cb1", "text": None}]
    # a price replied to the new message lands on the item
    handle_update(s, db, bot, _reply("45", reply_to=12))
    assert db.item(iid)["owner_price"] == 45 and all(r["resolved_at"] for r in _outbox(db, iid))


def test_reply_number_sets_price(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    assert handle_update(s, db, bot, _reply("45", reply_to=11)) == f"price {iid}: $45 (ready)"
    it = db.item(iid)
    assert it["status"] == "ready" and it["owner_price"] == 45 and loads(it["price"])["list_price"] == 45
    assert _outbox(db, iid)[0]["resolved_at"]
    assert bot.texts()[-1] == "\u2713 $45 \u2014 all done" and bot.sent("sendMessage")[-1]["reply_to_message_id"] == 50


def test_reply_answer_and_price_sets_price_then_reprocesses(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_info", gate={"decision": "needs_info", "reasons": ["size unclear"]})
    send_item(s, db, iid)
    out = handle_update(s, db, bot, _reply("size 8, 45", reply_to=11))
    assert out == f"item {iid}: $45; noted 'size 8', reprocessing"
    it = db.item(iid)
    assert it["status"] == "new" and it["owner_price"] == 45 and it["note"] == "size 8"
    assert _outbox(db, iid)[0]["resolved_at"]
    assert bot.texts()[-1] == "\u2713 $45, noted 'size 8', reprocessing"


def test_reply_without_price_or_note_gets_the_hint(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    assert handle_update(s, db, bot, _reply("", reply_to=11)) == f"item {iid}: empty reply"
    assert bot.texts()[-1] == ITEM_HINT and _outbox(db, iid)[0]["resolved_at"] is None


def test_reply_price_error_is_sent_back_not_raised(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="posted")
    db.add_outbox(CHAT, 11, "item", iid)
    out = handle_update(s, db, bot, _reply("45", reply_to=11))
    assert out.startswith(f"price {iid}: rejected")
    assert "can't be changed" in bot.texts()[-1]
    assert _outbox(db, iid)[0]["resolved_at"]               # a posted item waits for nothing: no longer the open one


def test_reply_ok_to_batch_calls_confirm(env, monkeypatch):
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)
    seen = []

    def confirm(_s, _db, b, cmd):                                  # what pipeline.confirm leaves behind: the items
        seen.append((b, cmd))
        _db.set_batch(b, status="split")
        for seq in (1, 2):
            _db.add_item(b, seq, f"item_{seq}")
    monkeypatch.setattr(pipeline, "confirm", confirm)
    assert handle_update(s, db, bot, _reply("ok", reply_to=11)) == f"batch {bid}: confirmed 'ok'"
    assert seen == [(bid, "ok")] and _outbox(db, bid)[0]["resolved_at"]
    assert bot.texts()[-1] == "\u2713 2 items \u2014 the cards follow one at a time"     # being processed: no card yet


def test_bad_correction_is_sent_back_and_batch_stays_pending(env, monkeypatch):
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)

    def bad(_s, _db, b, cmd):
        raise ValueError(f"can't drop [9]: photos are 0..2 ({cmd})")
    monkeypatch.setattr(pipeline, "confirm", bad)
    assert handle_update(s, db, bot, _reply("drop 9", reply_to=11)).startswith(f"batch {bid}: rejected 'drop 9'")
    p = bot.sent("sendMessage")[-1]
    assert p["text"].startswith("can't drop [9]") and p["reply_to_message_id"] == 50
    assert _outbox(db, bid)[0]["resolved_at"] is None
    assert handle_update(s, db, bot, _reply("", reply_to=11)) == f"batch {bid}: empty reply" and bot.texts()[-1] == BATCH_HINT


def _asked_before(db, iid, question, mid=11):
    """A poster question sent before WO27, still open in the chat."""
    db.set_item(iid, owner_question=question)
    db.add_outbox(CHAT, mid, "owner_q", iid, text=question)


def test_a_reply_to_an_old_poster_question_still_answers_the_item(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_owner")
    _asked_before(db, iid, "Which size?")
    assert handle_update(s, db, bot, _reply("size 8", reply_to=11)) == f"owner_q {iid}: answered 'size 8'"
    it = db.item(iid)
    assert it["status"] == "new" and it["note"] == "size 8" and _outbox(db, iid)[0]["resolved_at"]
    assert "got it" in bot.texts()[-1]


def test_a_plain_reply_to_an_old_brand_question_is_the_brand(env, tmp_path, facts):
    """WO27 1e (live: "J. Crew", without the word "brand", was taken as a note and the item came back stuck)."""
    s, db, bot = env
    listing = {**RENDERS["poshmark"], "title": "J.Crew Suede Ballet Flats", "brand": "J.Crew", "tags": [],
               "description": "J.Crew ballet flats.\nGently pre-loved, please see photos for condition."}
    iid = _item(db, tmp_path, facts, status="needs_owner", renders={"poshmark": listing},
                brand=Ev(value="J.Crew", photos=[1], source="photo", confidence=0.9))
    db.set_item(iid, owner_price=85)
    db.upsert_post(iid, "poshmark", status="queued", last_error="needs owner: Poshmark's brand list has no match")
    _asked_before(db, iid, "Poshmark's brand list has no match for 'J.Crew' (it offers: J. Crew, J. Crew Factory). "
                           "Which brand should I pick? (reply e.g. 'brand Vince')")
    assert handle_update(s, db, bot, _reply("J. Crew", reply_to=11)).startswith(f"brand {iid}: 'J. Crew'")
    it = db.item(iid)
    assert it["owner_brand"] == "J. Crew" and loads(it["facts"])["brand"]["value"] == "J. Crew"
    assert it["status"] == "ready" and it["note"] is None                     # back in line as it was, no reprocessing
    assert loads(it["renders"])["poshmark"]["brand"] == "J. Crew" and _outbox(db, iid)[0]["resolved_at"]
    assert bot.texts()[-1].startswith("✓ brand: J. Crew")


@pytest.mark.parametrize("text,brand", [("J. Crew", "J. Crew"), ("brand J. Crew", "J. Crew"), ("brand: Vince", "Vince"),
                                        ("size 8", None), ("NWT", None), ("same item", None), ("40", None),
                                        ("8.5", None), ("M", None), ("cover 2", None)])
def test_a_reply_to_a_card_asking_the_brand_is_the_brand(text, brand):
    gate = {"questions": ["Brand: read as “J.Crew”, not sure — reply 'brand …' if it's wrong"]}
    assert approve.brand_reply(text, gate) == brand
    assert approve.brand_reply("J. Crew", {"questions": []}) is None          # no brand asked: a note, as before


def test_unauthorized_and_unrelated_updates_are_ignored(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    n = len(bot.calls)
    assert handle_update(s, db, bot, _reply("45", reply_to=11, chat=999)) == "ignored: unauthorized"
    assert handle_update(s, db, bot, _reply("45", reply_to=11, user=STRANGER)) == "ignored: unauthorized"
    assert handle_update(s, db, bot, _callback(f"approve:{iid}:85", user=STRANGER)) == "ignored: unauthorized"
    chat = {"update_id": 3, "message": {"message_id": 5, "chat": {"id": CHAT}, "from": {"id": OWNER},
                                        "text": "see you at 5"}}
    assert handle_update(s, db, bot, chat) == "ignored: not a reply to the bot"     # the owners' own chat
    assert handle_update(s, db, bot, _reply("45", reply_to=999)) == "ignored: not a reply to the bot"
    assert handle_update(s, db, bot, _callback("nonsense")) == "ignored: unknown callback 'nonsense'"
    assert len(bot.calls) == n + 1                                # only the unknown button got an answerCallbackQuery
    assert db.item(iid)["status"] == "awaiting_price" and db.item(iid)["owner_price"] is None


# ---------- poll_once ----------

def test_poll_once_persists_offset_after_each_update_and_resumes(env, monkeypatch):
    s, db, bot = env
    seen = []

    def spy(_s, _db, _bot, u):
        seen.append((u["update_id"], _db.kv_get("telegram_offset")))
        return "spied"
    monkeypatch.setattr(approve, "handle_update", spy)
    bot.updates = [_reply("x", reply_to=1) | {"update_id": 5}, _reply("y", reply_to=1) | {"update_id": 6}]
    assert poll_once(s, db, bot, timeout=3) == 2
    assert bot.sent("getUpdates") == [{"offset": None, "timeout": 3, "allowed_updates": ["message", "callback_query"]}]
    assert seen == [(5, None), (6, "5")] and db.kv_get("telegram_offset") == "6"
    assert [r["detail"] for r in db.conn.execute("SELECT detail FROM events WHERE kind='telegram'")] == ['"spied"'] * 2

    fresh = FakeBot()                                             # "restart": new process, same DB
    assert poll_once(s, db, fresh, timeout=3) == 0
    assert fresh.sent("getUpdates")[0]["offset"] == 7


def test_poll_once_logs_a_crashing_update_and_moves_on(env, monkeypatch):
    s, db, bot = env

    def boom(*_a):
        raise RuntimeError("selector broke")
    monkeypatch.setattr(approve, "handle_update", boom)
    bot.updates = [_reply("x", reply_to=1) | {"update_id": 9}]
    assert poll_once(s, db, bot, timeout=1) == 1
    assert db.kv_get("telegram_offset") == "9"
    assert "selector broke" in bot.texts()[-1]
    kinds = [r["kind"] for r in db.conn.execute("SELECT kind FROM events")]
    assert "telegram_error" in kinds


# ---------- resend_pending ----------

def _age(db, message_id, hours):
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE outbox SET sent_at=? WHERE message_id=?", (old, message_id))


def test_resend_pending_resends_only_the_open_message(env, tmp_path, facts):
    """WO20: the flood of messages from before the queue (or any older copy) is closed; only the newest message whose
    item still waits is open, and only it is sent again."""
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)                                                        # 11
    waiting = _item(db, tmp_path, facts)
    send_item(s, db, waiting)                                                     # 12
    done = _item(db, tmp_path, facts, status="ready")
    db.add_outbox(CHAT, 99, "item", done)                                         # owner already priced it elsewhere
    fresh = _item(db, tmp_path, facts, status="needs_info")
    send_item(s, db, fresh)                                                       # 13: the newest that still waits
    for mid in (11, 12, 99, 13):
        _age(db, mid, hours=7)
    bot.calls.clear()

    assert resend_pending(s, db) == [fresh]
    assert [m for m, _ in bot.calls] == ["sendPhoto"] and bot.texts()[0].startswith("Tory Burch Suede Ballet Flats")
    assert [(r["kind"], r["ref"]) for r in db.outbox_pending()] == [("item", fresh)]   # the rest are closed
    assert [r["message_id"] for r in _outbox(db, fresh)] == [13, 14]              # the old copy stays, closed
    bot.calls.clear()
    assert resend_pending(s, db) == [] and bot.calls == []                        # the open one is recent now
    _age(db, 14, hours=7)
    assert resend_pending(s, db) == [fresh]                                       # old again: sent again


def test_resend_pending_uses_the_configured_timeout_and_clock(env, monkeypatch):
    s, db, bot = env
    s.data["telegram"]["resend_after_hours"] = 1
    bid = _batch(s, db)
    send_batch(s, db, bid)
    assert resend_pending(s, db) == []
    monkeypatch.setattr(approve, "_now", lambda: datetime.now(timezone.utc) + timedelta(hours=2))
    assert resend_pending(s, db) == [bid]


def test_resend_pending_without_bot_does_nothing(tmp_path):
    s = _settings(tmp_path, enabled=False)
    db = DB(s.path("db"))
    bid = _batch(s, db)
    db.add_outbox(CHAT, 11, "batch", bid)
    _age(db, 11, hours=9)
    assert resend_pending(s, db) == []
    assert _outbox(db, bid)[0]["resolved_at"] is None


def test_held_reshare_reply_says_so_and_same_item_drops(env, tmp_path, facts, monkeypatch):
    """WO5: a price on a held item is recorded but the hold stays; 'same item' drops it (wording comes from pipeline)."""
    s, db, bot = env
    iid = _item(db, tmp_path, facts, gate={"decision": "needs_info", "reasons": ["looks like item i_x"], "hold": "reshare"})
    send_item(s, db, iid)
    out = handle_update(s, db, bot, _reply("45", reply_to=11))
    assert "still held" in bot.texts()[-1] and "awaiting_price" in out
    it = db.item(iid)
    assert it["status"] == "awaiting_price" and it["owner_price"] == 45
    out = handle_update(s, db, bot, _reply("same item", reply_to=11))
    assert "dropped" in out and "dropped" in bot.texts()[-1] and db.item(iid)["status"] == "dropped"


def test_item_caption_shows_notes(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, gate={"decision": "publish", "reasons": [],
                                          "notes": ["model saw NWT but no attached hang tag: listed as like new"]})
    send_item(s, db, iid)
    assert "Note: model saw NWT but no attached hang tag" in bot.texts()[-1]



# ---------------------------------------------------------------- WO18: "Brand new or worn?"

def _waiting_shoe(db, tmp_path, facts):
    return _item(db, tmp_path, facts, status="awaiting_condition")


def test_ask_condition_sends_the_cover_the_question_and_three_buttons(env, tmp_path, facts):
    s, db, bot = env
    iid = _waiting_shoe(db, tmp_path, facts)
    approve.ask_condition(s, db, iid)
    [photo] = bot.sent("sendPhoto")
    assert photo["caption"].endswith("Brand new or worn? (couldn't tell from the photos)")
    assert [b["text"] for b in photo["reply_markup"]["inline_keyboard"][0]] == ["NWT", "Like New", "Good"]
    assert [b["callback_data"] for b in photo["reply_markup"]["inline_keyboard"][0]] == [
        f"cond:{iid}:nwt", f"cond:{iid}:like_new", f"cond:{iid}:good"]
    assert [r["kind"] for r in db.outbox_pending()] == ["condition"]


@pytest.mark.parametrize("choice,condition", [("nwt", "NWT"), ("like_new", "NWOT"), ("good", "good")])
def test_a_tap_sets_the_condition_and_reprocesses(env, tmp_path, facts, choice, condition):
    s, db, bot = env
    iid = _waiting_shoe(db, tmp_path, facts)
    approve.ask_condition(s, db, iid)
    result = handle_update(s, db, bot, _callback(f"cond:{iid}:{choice}"))
    it = db.item(iid)
    assert (it["status"], it["owner_condition"]) == ("new", condition), result
    assert db.outbox_pending() == []                                                   # the question is settled
    assert bot.sent("answerCallbackQuery")[-1]["text"] == approve.CONDITION_TAPPED[choice]
    assert "repricing" in bot.texts()[-1]


def test_a_tap_from_someone_else_is_ignored(env, tmp_path, facts):
    s, db, bot = env
    iid = _waiting_shoe(db, tmp_path, facts)
    approve.ask_condition(s, db, iid)
    assert handle_update(s, db, bot, _callback(f"cond:{iid}:nwt", user=STRANGER)) == "ignored: unauthorized"
    assert handle_update(s, db, bot, _callback(f"cond:{iid}:nwt", chat=999)) == "ignored: unauthorized"
    it = db.item(iid)
    assert (it["status"], it["owner_condition"]) == ("awaiting_condition", None)
    assert [r["kind"] for r in db.outbox_pending()] == ["condition"]                    # still waiting


def test_a_typed_answer_works_too(env, tmp_path, facts):
    s, db, bot = env
    iid = _waiting_shoe(db, tmp_path, facts)
    approve.ask_condition(s, db, iid)
    question = bot.next_id
    handle_update(s, db, bot, _reply("hmm", reply_to=question))
    assert bot.texts()[-1] == approve.CONDITION_HINT and db.item(iid)["status"] == "awaiting_condition"
    handle_update(s, db, bot, _reply("brand new, no tags", reply_to=question))
    assert db.item(iid)["owner_condition"] == "NWOT"
    assert [approve.parse_condition(x) for x in ("NWT", "with tags", "like new", "worn", "used", "?")] == [
        "nwt", "nwt", "like_new", "good", "good", None]


def test_a_pending_question_is_sent_again_after_a_restart_and_dropped_once_answered(env, tmp_path, facts):
    s, db, bot = env
    iid = _waiting_shoe(db, tmp_path, facts)
    approve.ask_condition(s, db, iid)
    _age(db, bot.next_id, hours=9)                                                       # the Mac slept overnight
    assert resend_pending(s, db) == [iid]                                               # the worker starts again
    assert len(bot.sent("sendPhoto")) == 2 and len(db.outbox_pending()) == 1           # the new message is the one
    db.set_item(iid, status="awaiting_price")                                            # answered elsewhere
    assert resend_pending(s, db) == []                                                  # nothing to send again...
    assert [(r["kind"], r["ref"]) for r in db.outbox_pending()] == [("item", iid)]       # ...the next one goes out


def test_the_whole_flow_tap_then_the_price_card_with_the_new_price(env, tmp_path, facts, monkeypatch):
    """A shoe in doubt: the question, a tap on NWT, the reprocessing, then the normal price card — priced as new."""
    from thrift_agent.schema import CopyOut, Ev, VerifyOut
    s, db, bot = env
    model = dict(category="Shoes", condition="good", condition_alternative="like_new",
                 unworn=Ev(value="yes", photos=[2], source="photo", confidence=0.6), photo_order=[0, 1, 2])
    copy = dict(poshmark_title="Tory Burch Red Ballet Flats size 7.5", poshmark_description="Red flats.",
                depop_description="red tory burch flats")

    def ask(model_name, system, content, out, tool, description, **kw):    # the model, stubbed: no API call
        if out.__name__ == "Facts":
            return facts(**model)
        if out is CopyOut:
            return CopyOut(**copy, poshmark_style_tags=[], depop_hashtags=["toryburch", "flats"])
        if out is VerifyOut:
            return VerifyOut(**copy)
        raise AssertionError(out)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    d = tmp_path / "shoe"
    for i, c in enumerate(["red", "green", "blue"]):
        (d / "photos").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (60, 80), c).save(d / "photos" / f"{i:02d}.jpg")
    bid = db.add_batch("share", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, 1, str(d))
    pipeline.process_item(s, db, iid)
    assert db.item(iid)["status"] == "awaiting_condition"
    assert not any("\u2705 $" in str(p.get("reply_markup")) for p in bot.sent("sendPhoto"))   # no price card yet
    guess = loads(db.item(iid)["price"])["list_price"]
    handle_update(s, db, bot, _callback(f"cond:{iid}:nwt"))
    pipeline.process_item(s, db, iid)                                                    # what the worker does next
    card = bot.sent("sendPhoto")[-1]
    price = loads(db.item(iid)["price"])["list_price"]
    assert price > guess and f"\u2705 ${price}" in str(card["reply_markup"])
    assert "Condition: NWT (your answer)" in card["caption"]
