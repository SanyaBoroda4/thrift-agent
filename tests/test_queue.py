"""WO20: one Telegram message at a time. The queue is read from the DB; FakeBot records what would be sent; the
pipeline is real (confirm is replaced where it would need photos on disk). No network."""
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from thrift_agent import approve, pipeline
from thrift_agent.approve import handle_update, price_options, resend_pending
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.telegram import Bot

CHAT, OWNER = 100, 7
EARLY, LATE = "2026-10-03T12:00:00+00:00", "2026-10-03T13:00:00+00:00"


class FakeBot(Bot):
    """Bot with call() replaced: records (method, params) and hands out message ids."""
    def __init__(self):
        super().__init__("TOK", CHAT, {OWNER})
        self.calls, self.next_id = [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True

    def texts(self):
        return [p.get("text") or p.get("caption") for m, p in self.calls if m in ("sendMessage", "sendPhoto")]

    def questions(self):
        """The messages with buttons or a contact sheet: what the queue sent (not the one-line confirmations)."""
        return [(p.get("text") or p.get("caption")).splitlines()[0] for m, p in self.calls
                if m in ("sendMessage", "sendPhoto") and (p.get("reply_markup") or "photo" in p or
                                                          (p.get("text") or "").startswith("Batch "))]


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    return s, db, bot


def _batch(db, created_at, status="split", name=None):
    bid = db.add_batch(name or f"share-{db.conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0]}", 3)
    db.conn.execute("UPDATE batches SET status=?, created_at=? WHERE id=?", (status, created_at, bid))
    if status == "needs_confirm":
        db.set_batch(bid, segmentation={"groups": [[0, 1], [2]], "summaries": ["tee", "flats"], "photos": ["a", "b", "c"]})
    return bid


def _item(db, tmp_path, facts, bid, seq, status="awaiting_price", title=None, price=40, gate=None, **fkw):
    d = tmp_path / "items" / f"{bid}-{seq}"
    (d / "photos").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (30, 40), "red").save(d / "cover.jpg")
    iid = db.add_item(bid, seq, str(d))
    title = title or f"Item {seq} of {bid[-6:]}"
    db.set_item(iid, status=status, facts=facts(**fkw).model_dump(),
                price={"target": 30, "list_price": price, "source": "brand", "by_marketplace": {"poshmark": price},
                       "basis": "brand"},
                renders={"poshmark": {"marketplace": "poshmark", "title": title, "size": "7.5", "price": price}},
                gate={"decision": "publish", "reasons": [], "questions": [], "notes": [], **(gate or {})})
    return iid


def _callback(data, mid=11):
    return {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": OWNER}, "data": data,
                                               "message": {"message_id": mid, "chat": {"id": CHAT}}}}


def _typed(text, mid=60):
    return {"update_id": 3, "message": {"message_id": mid, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text}}


def _reply(text, reply_to, mid=61):
    return {"update_id": 4, "message": {"message_id": mid, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text,
                                        "reply_to_message": {"message_id": reply_to}}}


def _open(db):
    row = approve.open_message(db)
    return (row["kind"], row["ref"]) if row else None


# ---------- the order, and one open message at a time ----------

def test_one_message_at_a_time_in_the_queues_order_across_two_batches(env, tmp_path, facts, monkeypatch):
    s, db, bot = env
    a = _batch(db, EARLY)                                                  # the older batch, already split
    a1 = _item(db, tmp_path, facts, a, 1, "awaiting_condition", title="Shoe A1")
    a2 = _item(db, tmp_path, facts, a, 2, title="Kids tee A2", gate={"ask_kids": True})
    a3 = _item(db, tmp_path, facts, a, 3, title="Dress A3")
    b = _batch(db, EARLY, status="needs_confirm")                          # same second as a: still after all of a
    c = _batch(db, LATE)
    c1 = _item(db, tmp_path, facts, c, 1, title="Bag C1")
    assert approve.queue(db) == [("condition", a1), ("kids", a2), ("item", a3), ("batch", b), ("item", c1)]

    assert approve.pump(s, db) == f"condition {a1}"
    assert approve.pump(s, db) is None and len(bot.texts()) == 1           # one open: nothing else goes out
    assert _open(db) == ("condition", a1)

    handle_update(s, db, bot, _callback(f"cond:{a1}:like_new"))            # Like New -> a1 is repriced first...
    assert bot.texts()[-1].startswith("✓ Like New") and "its card comes next" in bot.texts()[-1]
    assert approve.queue(db)[0] == (approve.NEW_ITEM, a1) and _open(db) is None
    assert approve.pump(s, db) is None                                     # ...and the queue holds for it
    db.set_item(a1, status="awaiting_price")                               # the worker is done with it
    assert approve.pump(s, db) == f"item {a1}"

    handle_update(s, db, bot, _callback(f"approve:{a1}:45"))
    assert bot.texts()[-2] == "✓ $45 — 3 of 4 left"                # then the next question at once:
    assert bot.texts()[-1].startswith("Kids tee A2\nGirls or Boys?")
    handle_update(s, db, bot, _callback(f"kids:{a2}:girls"))
    assert bot.texts()[-2] == "✓ Girls" and bot.texts()[-1].startswith("Kids tee A2\nSize")   # its card
    handle_update(s, db, bot, _callback(f"approve:{a2}:40"))
    handle_update(s, db, bot, _callback(f"approve:{a3}:40"))
    assert bot.texts()[-2] == "✓ $40 — 1 of 4 left" and bot.texts()[-1].startswith(f"Batch {b}")

    def confirm(_s, _db, bid, cmd):                                        # what confirm leaves: b's two new items
        _db.set_batch(bid, status="split")
        for seq in (1, 2):
            _db.add_item(bid, seq, str(tmp_path / f"b{seq}"))
    monkeypatch.setattr(pipeline, "confirm", confirm)
    handle_update(s, db, bot, _reply("ok", reply_to=bot.next_id))
    assert bot.texts()[-1] == "✓ 2 items — the cards follow one at a time"
    assert approve.queue(db)[:2] == [(approve.NEW_ITEM, i) for i in _items_of(db, b)]
    assert approve.pump(s, db) is None                                     # c1 never jumps ahead of b's items
    assert [kind for kind, _ in approve.queue(db)][-1] == "item" and approve.queue(db)[-1][1] == c1
    assert bot.questions() == ["Shoe A1", "Shoe A1", "Kids tee A2", "Kids tee A2", "Dress A3", f"Batch {b}: 3 photos -> 2 items"]


def _items_of(db, bid):
    return [r[0] for r in db.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY seq", (bid,))]


def test_the_worker_processes_in_the_queues_order(env, tmp_path, facts, monkeypatch):
    """The next card is the one ready first: the older batch's items before the newer batch, and an item sent back by
    the owner's answer before the rest."""
    from thrift_agent import cli
    s, db, bot = env
    late = _batch(db, LATE)
    l1 = _item(db, tmp_path, facts, late, 1, "new")
    early = _batch(db, EARLY)
    e2 = _item(db, tmp_path, facts, early, 2, "new")
    e1 = _item(db, tmp_path, facts, early, 1, "new")
    order = []

    def process(_s, _db, iid):
        order.append(iid)
        _db.set_item(iid, status="awaiting_price")
        if iid == e2:                                                      # meanwhile the owner sends e1 back
            _db.set_item(e1, status="new")
    monkeypatch.setattr(cli.pipeline, "process_item", process)
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda _s: [])
    cli._tick(s, db)
    assert order == [e1, e2, l1]                                           # e1 once per tick: again on the next one
    cli._tick(s, db)
    assert order == [e1, e2, l1, e1]


# ---------- a restart ----------

def test_a_restart_sends_again_only_the_open_message(env, tmp_path, facts):
    s, db, bot = env
    a = _batch(db, EARLY)
    a1 = _item(db, tmp_path, facts, a, 1, title="Tee A1")
    _item(db, tmp_path, facts, a, 2, title="Tee A2")
    assert approve.pump(s, db) == f"item {a1}"
    bot.calls.clear()
    assert resend_pending(s, db, force=True) == [a1]                       # the worker starts again
    assert bot.questions() == ["Tee A1"]                                   # only the open card, not A2
    assert approve.pump(s, db) is None                                     # A2 still waits for the answer
    handle_update(s, db, bot, _callback(f"approve:{a1}:40", mid=bot.next_id))
    assert bot.questions()[-1] == "Tee A2"


def test_after_a_restart_with_nothing_open_the_queue_goes_on(env, tmp_path, facts):
    s, db, bot = env
    a1 = _item(db, tmp_path, facts, _batch(db, EARLY), 1, title="Tee A1")
    assert resend_pending(s, db, force=True) == []                         # nothing to send again...
    assert bot.questions() == ["Tee A1"] and _open(db) == ("item", a1)    # ...the first message goes out


def test_the_flood_from_before_the_queue_is_closed(env, tmp_path, facts):
    """Every card of the live test arrived at once (before WO20): only the newest still-waiting one counts as open."""
    s, db, bot = env
    a = _batch(db, EARLY)
    items = [_item(db, tmp_path, facts, a, k) for k in (1, 2, 3)]
    for k, iid in enumerate(items):
        db.add_outbox(CHAT, 500 + k, "item", iid)
    assert _open(db) == ("item", items[2])
    assert [r["ref"] for r in db.outbox_pending()] == [items[2]]
    assert approve.pump(s, db) is None and bot.calls == []


# ---------- [Later] ----------

def test_later_moves_the_card_behind_everything_queued_so_far(env, tmp_path, facts):
    s, db, bot = env
    a = _batch(db, EARLY)
    a1 = _item(db, tmp_path, facts, a, 1, title="Tee A1")
    a2 = _item(db, tmp_path, facts, a, 2, title="Tee A2")
    assert approve.pump(s, db) == f"item {a1}"
    assert handle_update(s, db, bot, _callback(f"later:{a1}")) == f"later {a1}"
    assert ("answerCallbackQuery", {"callback_query_id": "cb", "text": "Later: moved to the end"}) in bot.calls
    assert bot.questions()[-1] == "Tee A2" and db.item(a1)["status"] == "awaiting_price"
    newer = _batch(db, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))
    n1 = _item(db, tmp_path, facts, newer, 1, title="Bag N1")              # shared after the tap on Later
    assert approve.queue(db) == [("item", a2), ("item", a1), ("item", n1)]
    handle_update(s, db, bot, _callback(f"approve:{a2}:40"))
    assert bot.questions()[-1] == "Tee A1"
    handle_update(s, db, bot, _callback(f"approve:{a1}:40"))
    assert bot.questions()[-1] == "Bag N1"
    assert handle_update(s, db, bot, _callback(f"later:{a1}")).startswith(f"later {a1}: rejected")   # priced already


# ---------- a number typed without a reply ----------

def test_a_number_typed_without_a_reply_prices_the_open_card(env, tmp_path, facts):
    s, db, bot = env
    a = _batch(db, EARLY)
    a1 = _item(db, tmp_path, facts, a, 1, title="Tee A1")
    a2 = _item(db, tmp_path, facts, a, 2, title="Tee A2")
    approve.pump(s, db)
    assert handle_update(s, db, bot, _typed("28")) == f"price {a1}: $28 (ready)"
    assert db.item(a1)["owner_price"] == 28 and bot.texts()[-2] == "✓ $28 — 1 of 2 left"
    assert bot.questions()[-1] == "Tee A2"                                 # the next card at once
    assert handle_update(s, db, bot, _typed("hi, are you there?")) == "ignored: not a reply to the bot"
    assert handle_update(s, db, bot, _typed("5")) == "typed $5: under the floor, not taken"
    assert "under the $20 floor" in bot.texts()[-1] and db.item(a2)["owner_price"] is None
    assert handle_update(s, db, bot, _typed("$35.00")) == f"price {a2}: $35 (ready)"
    assert bot.texts()[-1] == "✓ $35 — all done"


def test_a_typed_number_is_not_a_price_while_another_question_is_open(env, tmp_path, facts):
    s, db, bot = env
    b = _batch(db, EARLY, status="needs_confirm")
    approve.pump(s, db)
    assert _open(db) == ("batch", b)
    assert handle_update(s, db, bot, _typed("28")) == "typed $28: no price card open"
    assert bot.texts()[-1] == "No price card is open — answer the question above first"


@pytest.mark.parametrize("text,amount", [("28", 28), ("$28", 28), ("28.00", 28), ("28 dollars", 28), (" $ 45 ", 45),
                                         ("28.5", 29), ("size 8", None), ("8, 28", None), ("28 or 30", None),
                                         ("", None), ("0", None)])
def test_plain_price(text, amount):
    assert approve.plain_price(text) == amount


# ---------- one-tap prices ----------

@pytest.mark.parametrize("price,floor,expected", [
    (85, 20, [75, 80, 90, 95]),                     # the owner's example: X-10, X-5, X+5, X+10
    (45, 20, [35, 40, 50, 55]),
    (28, 20, [20, 25, 35, 40]),                     # an odd price: round neighbours
    (25, 20, [20, 30, 35, 40]),                     # 15 would be under the floor: one more above instead
    (20, 20, [25, 30, 35, 40]),                     # at the floor: all above
    (35, 30, [30, 40, 45, 50]),                     # a seller's "floor 30"
    (150, 20, [130, 140, 160, 170]),                # $10 apart from $100
    (300, 20, [250, 275, 325, 350]),                # $25 apart from $250
])
def test_price_buttons_are_round_and_never_under_the_floor(price, floor, expected):
    options = price_options(price, floor, 5)
    assert options == expected
    assert len(options) == 4 and price not in options and all(p >= floor and p % 5 == 0 for p in options)


def test_the_card_has_the_suggestion_four_neighbours_later_and_change(env, tmp_path, facts):
    s, db, bot = env
    a1 = _item(db, tmp_path, facts, _batch(db, EARLY), 1, price=45)
    approve.pump(s, db)
    rows = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [["✅ $45"], ["$35", "$40", "$50", "$55"], ["Later", "Change"]]
    assert {b["callback_data"] for b in rows[1]} == {f"approve:{a1}:{p}" for p in (35, 40, 50, 55)}


# ---------- the card in Poshmark's words ----------

@pytest.mark.parametrize("condition,label", [("NWT", "NWT"), ("NWOT", "Like New"), ("like_new", "Like New"),
                                             ("excellent", "Like New"), ("good", "Good"), ("fair", "Good")])
def test_the_card_says_poshmarks_condition(env, tmp_path, facts, condition, label):
    s, db, bot = env
    _item(db, tmp_path, facts, _batch(db, EARLY), 1, title="Flats", condition=condition)
    approve.pump(s, db)
    assert bot.texts()[-1] == f"Flats\nSize 7.5\nCondition: {label}"


def test_the_card_shows_the_kids_size_as_poshmarks_menu_does(env, tmp_path, facts):
    s, db, bot = env
    bid = _batch(db, EARLY)
    iid = _item(db, tmp_path, facts, bid, 1, title="Naturino Sneakers", department="Kids", kids_gender="girls",
                kids_gender_confidence=0.9)
    render = {"marketplace": "poshmark", "title": "Naturino Sneakers", "description": "x", "price": 40,
              "department": "Kids", "category": "Shoes", "subcategory": "Sneakers", "size": "EU 24 / US Toddler 7.5",
              "condition": "good", "brand": "Naturino", "kids_gender": "girls", "colors": ["Pink"],
              "photos": ["cover.jpg"], "sku": "i_x"}
    db.set_item(iid, renders={"poshmark": render})
    approve.pump(s, db)
    assert bot.texts()[-1].splitlines()[1] == "Size 7.5 (Toddler Girl)"
