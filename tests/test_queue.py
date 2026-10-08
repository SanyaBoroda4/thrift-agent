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

    def marks(self):
        """The answered cards (WO29): the "✓ …" label of each editMessageReplyMarkup."""
        return [p["reply_markup"]["inline_keyboard"][0][0]["text"] for m, p in self.calls
                if m == "editMessageReplyMarkup"]

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
    assert bot.marks()[-1] == "✓ Like New (brand new, no tags)" and len(bot.texts()) == 1   # quietly (WO29)
    assert approve.queue(db)[0] == (approve.NEW_ITEM, a1) and _open(db) is None
    assert approve.pump(s, db) is None                                     # ...and the queue holds for it
    db.set_item(a1, status="awaiting_price")                               # the worker is done with it
    assert approve.pump(s, db) == f"item {a1}"

    handle_update(s, db, bot, _callback(f"approve:{a1}:45"))
    assert bot.marks()[-1] == "✓ $45 — queued"                     # the card marked, then the next question at once:
    assert bot.texts()[-1].startswith("Kids tee A2\nGirls or Boys?")
    handle_update(s, db, bot, _callback(f"kids:{a2}:girls"))
    assert bot.marks()[-1] == "✓ Girls" and bot.texts()[-1].startswith("Kids tee A2\nSize")   # its card
    handle_update(s, db, bot, _callback(f"approve:{a2}:40"))
    handle_update(s, db, bot, _callback(f"approve:{a3}:40"))
    assert bot.marks()[-1] == "✓ $40 — queued" and bot.texts()[-1].startswith(f"Batch {b}")
    assert not any(t.startswith("✓") for t in bot.texts())               # never a "✓ … left" message (WO29)

    def confirm(_s, _db, bid, cmd):                                        # what confirm leaves: b's two new items
        _db.set_batch(bid, status="split")
        for seq in (1, 2):
            _db.add_item(bid, seq, str(tmp_path / f"b{seq}"))
    monkeypatch.setattr(pipeline, "confirm", confirm)
    handle_update(s, db, bot, _reply("ok", reply_to=bot.next_id))
    assert bot.marks()[-1] == "✓ 2 items — the cards follow"
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
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda _s, _db=None: [])
    cli._tick(s, db)
    assert order == [e1, e2, l1]                                           # e1 once per tick: again on the next one
    cli._tick(s, db)
    assert order == [e1, e2, l1, e1]


# ---------- a restart ----------

def test_a_restart_sends_again_only_the_open_message_and_only_once_it_is_old(env, tmp_path, facts):
    """WO24, live: every deploy restarts the worker, and each restart sent the open card again. A restart repeats only
    a card that waited past telegram.resend_after_hours (the Mac slept) — and only that one."""
    s, db, bot = env
    a = _batch(db, EARLY)
    a1 = _item(db, tmp_path, facts, a, 1, title="Tee A1")
    _item(db, tmp_path, facts, a, 2, title="Tee A2")
    assert approve.pump(s, db) == f"item {a1}"
    bot.calls.clear()
    assert resend_pending(s, db) == [] and bot.calls == []                 # a deploy right after: nothing repeated
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE outbox SET sent_at=?", (old,))
    assert resend_pending(s, db) == [a1]                                   # the worker starts after a long sleep
    assert bot.questions() == ["Tee A1"]                                   # only the open card, not A2
    assert approve.pump(s, db) is None                                     # A2 still waits for the answer
    handle_update(s, db, bot, _callback(f"approve:{a1}:40", mid=bot.next_id))
    assert bot.questions()[-1] == "Tee A2"


def test_after_a_restart_with_nothing_open_the_queue_goes_on(env, tmp_path, facts):
    s, db, bot = env
    a1 = _item(db, tmp_path, facts, _batch(db, EARLY), 1, title="Tee A1")
    assert resend_pending(s, db) == []                                     # nothing to send again...
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
    assert db.item(a1)["owner_price"] == 28 and bot.marks() == ["✓ $28 — queued"]
    assert bot.questions()[-1] == "Tee A2"                                 # the next card at once
    assert handle_update(s, db, bot, _typed("hi, are you there?")) == "ignored: not a reply to the bot"
    assert handle_update(s, db, bot, _typed("5")) == "typed $5: under the floor, not taken"
    assert "under the $20 floor" in bot.texts()[-1] and db.item(a2)["owner_price"] is None
    assert handle_update(s, db, bot, _typed("$35.00")) == f"price {a2}: $35 (ready)"
    assert bot.marks()[-1] == "✓ $35 — queued" and not bot.texts()[-1].startswith("✓")


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

@pytest.mark.parametrize("price,rows", [
    (40, [["$30", "⭐$40", "$50", "$60"], ["$70", "$80", "$90", "$100"]]),        # the owner's example (WO32b)
    (35, [["$25", "⭐$35", "$45", "$55"], ["$65", "$75", "$85", "$95"]]),         # steps start from the suggestion
    (10, [["⭐$10", "$20", "$30", "$40"], ["$50", "$60", "$70", "$80"]]),         # $0 below: no low button, 7 up
    (150, [["$140", "⭐$150", "$160", "$170"], ["$180", "$190", "$200", "$210"]]),
    (15, [["$5", "⭐$15", "$25", "$35"], ["$45", "$55", "$65", "$75"]]),          # $5 is not under $5: kept
    (14, [["⭐$14", "$24", "$34", "$44"], ["$54", "$64", "$74", "$84"]]),         # $4 would be: no low button
])
def test_eight_price_buttons_ten_dollars_apart_mostly_up(price, rows):
    texts = [[b["text"] for b in row] for row in approve.item_buttons("i_x", price)[:2]]
    assert texts == rows
    options = price_options(price)
    assert len(options) == 8 and price in options and all(p >= 5 for p in options)
    first = [b["callback_data"] for b in approve.item_buttons("i_x", price)[0]]
    assert first == [f"approve:i_x:{p}" for p in options[:4]]


def test_the_card_has_eight_prices_later_change_and_wrong_photos(env, tmp_path, facts):
    s, db, bot = env
    a1 = _item(db, tmp_path, facts, _batch(db, EARLY), 1, price=45)
    approve.pump(s, db)
    rows = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [["$35", "⭐$45", "$55", "$65"], ["$75", "$85", "$95", "$105"],
                                                          ["Later", "Change", "Wrong photos"]]
    assert {b["callback_data"] for b in rows[0] + rows[1]} == {f"approve:{a1}:{p}" for p in range(35, 106, 10)}


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


# ---------- [Wrong photos] (WO20b) ----------

COLOURS = ["red", "green", "blue", "navy", "orange", "purple", "gray", "white"]


def _split_batch(s, db, tmp_path, facts, groups, n):
    """A batch accepted as `groups` (n photos on disk, as process_batch leaves them), its items waiting for a price."""
    bid = db.add_batch(str(tmp_path / "share-regroup"), n)
    all_dir = s.path("work") / bid / "all"
    all_dir.mkdir(parents=True)
    photos = []
    for i in range(n):
        Image.new("RGB", (60, 80), COLOURS[i]).save(all_dir / f"{i:03d}.jpg")
        photos.append(str(all_dir / f"{i:03d}.jpg"))
    db.conn.execute("UPDATE batches SET created_at=? WHERE id=?", (EARLY, bid))
    db.set_batch(bid, status="needs_confirm", segmentation={"groups": groups, "summaries": ["x"] * len(groups),
                                                            "photos": photos, "kinds": ["own"] * n, "pauses": [],
                                                            "auto_accepted": True})
    pipeline.split(s, db, bid, groups)
    iids = [r[0] for r in db.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY seq", (bid,))]
    for k, iid in enumerate(iids, 1):
        db.set_item(iid, status="awaiting_price", facts=facts().model_dump(),
                    price={"target": 30, "list_price": 40, "source": "brand", "by_marketplace": {"poshmark": 40},
                           "basis": "brand"},
                    renders={"poshmark": {"marketplace": "poshmark", "title": f"Item {k}", "size": "7.5", "price": 40}},
                    gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
    return bid, iids


def _photos_of(db, iid):
    return pipeline.item_group(db.item(iid))


def test_wrong_photos_reopens_the_batch_and_rebuilds_only_what_changed(env, tmp_path, facts):
    s, db, bot = env
    bid, (i1, i2, i3) = _split_batch(s, db, tmp_path, facts, [[0, 1, 2], [3, 4], [5]], 6)
    db.set_item(i3, owner_price=30, status="ready")                       # priced already
    assert approve.pump(s, db) == f"item {i1}"
    assert handle_update(s, db, bot, _callback(f"regroup:{i1}")) == f"regroup {i1}: batch {bid} reopened"
    assert ("answerCallbackQuery", {"callback_query_id": "cb", "text": "The batch's photos follow"}) in bot.calls
    sheet = bot.calls[-1][1]                                               # the one open message now
    assert sheet["caption"].startswith(f"Wrong photos? Batch {bid}: 6 photos -> 3 items, as they are now")
    assert "item 2: Item 2 - photos [3, 4]" in sheet["caption"] and sheet["photo"].name == pipeline.REGROUP_SHEET
    assert db.batch(bid)["status"] == "regroup" and approve.queue(db) == [("regroup", bid)]   # its items wait
    assert _open(db) == ("regroup", bid)

    handle_update(s, db, bot, _reply("2>2", reply_to=bot.next_id))        # photo 2 belongs to item 2
    assert bot.marks()[-1] == "✓ 2 items rebuilt — the cards follow"
    assert _photos_of(db, i1) == [0, 1] and _photos_of(db, i2) == [2, 3, 4] and _photos_of(db, i3) == [5]
    assert [db.item(i)["status"] for i in (i1, i2, i3)] == ["new", "new", "ready"]
    assert db.item(i3)["owner_price"] == 30 and db.item(i1)["owner_price"] is None    # unchanged keeps everything
    assert db.batch(bid)["status"] == "split" and approve.queue(db) == [(approve.NEW_ITEM, i1), (approve.NEW_ITEM, i2)]


def test_a_photo_fix_can_split_merge_and_leave_nothing_changed(env, tmp_path, facts):
    s, db, bot = env
    bid, (i1, i2, i3) = _split_batch(s, db, tmp_path, facts, [[0, 1], [2, 3, 4], [5]], 6)
    pipeline.start_regroup(s, db, i2)
    assert pipeline.regroup(s, db, bid, "ok") == {"kept": [i1, i2, i3], "rebuilt": [], "created": [], "removed": []}
    assert [db.item(i)["status"] for i in (i1, i2, i3)] == ["awaiting_price"] * 3      # nothing changed: as it was

    pipeline.start_regroup(s, db, i2)
    out = pipeline.regroup(s, db, bid, "split 4")                         # photo 4 is an item of its own
    assert out["kept"] == [i1, i3] and out["rebuilt"] == [i2] and len(out["created"]) == 1
    new = out["created"][0]
    assert _photos_of(db, i2) == [2, 3] and _photos_of(db, new) == [4]
    assert [db.item(i)["seq"] for i in (i1, i2, new, i3)] == [1, 2, 3, 4]  # in photo order

    pipeline.start_regroup(s, db, i1)
    pipeline.confirm(s, db, bid, "merge 2 3")                              # thrift confirm is the CLI twin
    assert db.item(new) is None and _photos_of(db, i2) == [2, 3, 4]       # merged into the item it overlaps most


def test_wrong_photos_never_touches_an_item_on_the_marketplace(env, tmp_path, facts):
    s, db, bot = env
    bid, (i1, i2, i3) = _split_batch(s, db, tmp_path, facts, [[0, 1, 2], [3, 4], [5]], 6)
    db.set_item(i3, status="posted")
    db.upsert_listing(i3, "poshmark", status="posted", url="https://poshmark.com/listing/x-0000000000000000000000a1")
    out = handle_update(s, db, bot, _callback(f"regroup:{i3}"))
    assert out.startswith(f"regroup {i3}: rejected")
    assert any("already on the marketplace (poshmark posted)" in m for m in bot.texts())
    assert db.batch(bid)["status"] == "split"

    pipeline.start_regroup(s, db, i1)                                     # the others can still be fixed...
    with pytest.raises(ValueError, match="item 3 .* is already on the marketplace"):
        pipeline.regroup(s, db, bid, "merge 2 3")                         # ...but never the listed one
    assert db.batch(bid)["status"] == "regroup" and _photos_of(db, i3) == [5]
    assert pipeline.regroup(s, db, bid, "2>2")["rebuilt"] == [i1, i2]
    assert db.item(i3)["status"] == "posted"


def test_the_poster_waits_while_a_batch_is_being_fixed(env, tmp_path, facts):
    from thrift_agent.post import runner
    s, db, bot = env
    bid, (i1, i2) = _split_batch(s, db, tmp_path, facts, [[0, 1], [2]], 3)
    render = {"marketplace": "poshmark", "title": "Item 2", "description": "x", "price": 40, "department": "Women",
              "category": "Shoes", "subcategory": None, "size": "7.5", "condition": "good", "brand": "Tory Burch",
              "colors": ["Red"], "photos": ["cover.jpg"], "sku": i2}
    db.set_item(i2, status="ready", renders={"poshmark": render}, owner_price=40)
    assert runner.next_job(s, db, ["poshmark"], dry=True)[0] == i2
    pipeline.start_regroup(s, db, i1)
    assert runner.next_job(s, db, ["poshmark"], dry=True) is None
