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
    assert cap.startswith("Tory Burch Suede Ballet Flats\n")
    assert f"Item {iid}" in cap and "Size: 7.5 (printed 7.5M)" in cap
    assert "Condition: good, 2 flaws" in cap
    assert "Suggested: $85 (brand tier: Tory Burch flats)" in cap and "Retail $198" in cap
    assert "no price history" not in cap and "Open questions" not in cap
    assert cap.endswith("Reply with a number to change the price.")
    assert p["reply_markup"] == {"inline_keyboard": [[
        {"text": "Approve $85", "callback_data": f"approve:{iid}:85"},
        {"text": "Change", "callback_data": f"change:{iid}"}]]}
    (row,) = _outbox(db, iid)
    assert (row["chat_id"], row["message_id"], row["kind"], row["resolved_at"]) == ("100", 11, "item", None)
    assert row["text"] == cap


def test_send_item_needs_info_lists_questions_and_default_price_warning(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_info", cover=False,
                price={**PRICE, "list_price": 40, "source": "category_default", "basis": "category default"},
                renders={"poshmark": {"title": "Flats", "size": None, "price": 40}},
                gate={"decision": "needs_info", "reasons": ["brand unclear (None, 0.40)", "size unclear"]})
    send_item(s, db, iid)
    (method, p), = bot.calls
    assert method == "sendMessage"                                # no cover.jpg -> text message
    cap = p["text"]
    assert "Size: 7.5" in cap                                     # falls back to facts.size_us
    assert "no price history for Tory Burch" in cap
    assert "Open questions:\n- brand unclear (None, 0.40)\n- size unclear\n" in cap
    assert cap.endswith("Reply with the answers and the price, e.g. 'size 8, 45'")
    assert p["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve $40"


def test_send_item_without_price_has_no_approve_button(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, price={**PRICE, "list_price": None, "source": "none", "basis": ""})
    send_item(s, db, iid)
    p = bot.sent("sendPhoto")[0]
    assert "Suggested: no price" in p["caption"] and "no price history for Tory Burch" in p["caption"]
    assert p["reply_markup"] == {"inline_keyboard": [[{"text": "Change", "callback_data": f"change:{iid}"}]]}


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


def test_ask_owner_records_question(env):
    s, db, bot = env
    approve.ask_owner(s, db, "i_x", "Which Poshmark size?")
    assert bot.texts() == ["Question about i_x: Which Poshmark size?\nReply to this message."]
    (row,) = _outbox(db, "i_x")
    assert (row["kind"], row["text"]) == ("owner_q", "Which Poshmark size?")


# ---------- handle_update ----------

def test_approve_callback_sets_price_and_resolves(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    out = handle_update(s, db, bot, _callback(f"approve:{iid}:85"))
    assert out == f"approved {iid} at $85 (ready)"
    it = db.item(iid)
    assert it["status"] == "ready" and it["owner_price"] == 85
    assert loads(it["price"])["source"] == "owner" and loads(it["renders"])["poshmark"]["price"] == 85
    assert all(r["resolved_at"] for r in _outbox(db, iid))
    assert bot.sent("answerCallbackQuery") == [{"callback_query_id": "cb1", "text": "Approved $85"}]
    assert bot.sent("sendMessage")[-1]["reply_to_message_id"] == 11 and "$85" in bot.texts()[-1]


def test_approve_callback_on_a_moved_on_item_replies_with_the_error(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="posted")
    out = handle_update(s, db, bot, _callback(f"approve:{iid}:85"))
    assert out.startswith(f"approve {iid}: rejected")
    assert "can't be changed" in bot.texts()[-1] and db.item(iid)["owner_price"] is None


def test_change_callback_records_a_new_outbox_row(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)                                                     # message 11
    assert handle_update(s, db, bot, _callback(f"change:{iid}", mid=11)) == f"change {iid}: asked for the price"
    p = bot.sent("sendMessage")[-1]
    assert p["text"] == f"Reply to this message with the price for {iid}." and p["reply_to_message_id"] == 11
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
    assert handle_update(s, db, bot, _reply("45", reply_to=11)) == f"item {iid}: price $45 -> ready"
    it = db.item(iid)
    assert it["status"] == "ready" and it["owner_price"] == 45 and loads(it["price"])["list_price"] == 45
    assert _outbox(db, iid)[0]["resolved_at"]
    assert bot.texts()[-1] == f"{iid}: recorded price $45 -> ready" and bot.sent("sendMessage")[-1]["reply_to_message_id"] == 50


def test_reply_answer_and_price_sets_price_then_reprocesses(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_info", gate={"decision": "needs_info", "reasons": ["size unclear"]})
    send_item(s, db, iid)
    out = handle_update(s, db, bot, _reply("size 8, 45", reply_to=11))
    assert out == f"item {iid}: price $45 -> needs_info; note 'size 8' - reprocessing"
    it = db.item(iid)
    assert it["status"] == "new" and it["owner_price"] == 45 and it["note"] == "size 8"
    assert _outbox(db, iid)[0]["resolved_at"]
    assert "price $45" in bot.texts()[-1] and "'size 8'" in bot.texts()[-1]


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
    assert out.startswith(f"item {iid}: error:")
    assert "can't be changed" in bot.texts()[-1] and _outbox(db, iid)[0]["resolved_at"] is None


def test_reply_ok_to_batch_calls_confirm(env, monkeypatch):
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)
    seen = []
    monkeypatch.setattr(pipeline, "confirm", lambda _s, _db, b, cmd: seen.append((b, cmd)))
    assert handle_update(s, db, bot, _reply("ok", reply_to=11)) == f"batch {bid}: confirmed 'ok'"
    assert seen == [(bid, "ok")] and _outbox(db, bid)[0]["resolved_at"]
    assert bot.texts()[-1] == f"Batch {bid}: split ok (ok)"


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


def test_owner_question_reply_answers_the_item(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts, status="needs_owner")
    approve.ask_owner(s, db, iid, "Which size?")                                 # message 11
    assert handle_update(s, db, bot, _reply("size 8", reply_to=11)) == f"owner_q {iid}: answered 'size 8'"
    it = db.item(iid)
    assert it["status"] == "new" and it["note"] == "size 8" and _outbox(db, iid)[0]["resolved_at"]
    assert "got it" in bot.texts()[-1]


def test_unauthorized_and_unrelated_updates_are_ignored(env, tmp_path, facts):
    s, db, bot = env
    iid = _item(db, tmp_path, facts)
    send_item(s, db, iid)
    n = len(bot.calls)
    assert handle_update(s, db, bot, _reply("45", reply_to=11, chat=999)) == "ignored: unauthorized"
    assert handle_update(s, db, bot, _reply("45", reply_to=11, user=STRANGER)) == "ignored: unauthorized"
    assert handle_update(s, db, bot, _callback(f"approve:{iid}:85", user=STRANGER)) == "ignored: unauthorized"
    plain = {"update_id": 3, "message": {"message_id": 5, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": "45"}}
    assert handle_update(s, db, bot, plain) == "ignored: not a reply to the bot"
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


def test_resend_pending_resends_old_rows_and_resolves_moved_on_ones(env, tmp_path, facts):
    s, db, bot = env
    bid = _batch(s, db)
    send_batch(s, db, bid)                                                        # 11, will be old
    waiting = _item(db, tmp_path, facts)
    send_item(s, db, waiting)                                                     # 12, will be old
    fresh = _item(db, tmp_path, facts, status="needs_info")
    send_item(s, db, fresh)                                                       # 13, just sent
    done = _item(db, tmp_path, facts, status="ready")
    db.add_outbox(CHAT, 99, "item", done)                                         # owner already priced it elsewhere
    asked = _item(db, tmp_path, facts, status="needs_owner")
    approve.ask_owner(s, db, asked, "Which size?")                                # 14, will be old
    for mid in (11, 12, 99, 14):
        _age(db, mid, hours=7)
    bot.calls.clear()

    assert resend_pending(s, db) == [bid, waiting, asked]
    assert [m for m, _ in bot.calls] == ["sendMessage", "sendPhoto", "sendMessage"]
    assert bot.texts()[0].startswith(f"Batch {bid}:")
    assert bot.texts()[2] == f"Question about {asked}: Which size?\nReply to this message."
    assert [r["resolved_at"] is None for r in _outbox(db, done)] == [False]
    assert [r["message_id"] for r in _outbox(db, waiting)] == [12, 16]            # the old row stays, a new one is added
    assert _outbox(db, fresh)[0]["resolved_at"] is None and len(_outbox(db, fresh)) == 1

    bot.calls.clear()
    assert resend_pending(s, db) == []                                            # everything pending is now recent
    assert set(resend_pending(s, db, force=True)) == {bid, waiting, fresh, asked}


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
    assert resend_pending(s, db, force=True) == []
    assert _outbox(db, bid)[0]["resolved_at"] is None
