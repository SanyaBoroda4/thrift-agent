"""The share ledger (WO45 §3): every share ends in exactly one visible outcome.

Live (Oct 12–18): 4 of 23 shares never became a card and 11 came short or late, and nothing anywhere said so — each
step trusted what arrived and no record tied a share to its end. Now every share is one row in `shares`, keyed by the
share id (the Shortcut's folder name, the same for the iCloud folder and the direct upload), with the time of each
step — received → complete → grouped → carded — and its outcome: `carded` (a card per item), `repeat`, `empty`,
`same_share` (the second copy of a share that arrived twice), `failed` with its reason, or (WO56 §6) `held` (its upload
didn't finish: never posted, she is asked to share again) and `replaced` (she did). Each step is recorded before
the next starts; the rows go to thrift-api with the rest of the sync, where a watchdog that doesn't depend on the Mac
tells the owner about a complete share with no outcome (api/thrift_api/timers.shares_watch) and the dashboard shows
the day's shares.

A share id: `2026-10-18_121154` (the Shortcut's date and time; §2 adds `_<random>`); an archive folder's name is
`20261018_<share id>`, a second archive of the same share `…_2`, WO37's copies `<share id>-late` — all one share."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent.db import DB, now

OUTCOMES = ("carded", "repeat", "empty", "same_share", "failed", "joined",
            "held", "replaced")      # WO56 §6: an upload that didn't finish (held, never posted) · then shared again
_ARCHIVE = re.compile(r"^\d{8}_(?P<id>.+?)(?:_\d{1,3})?$")      # 20261018_<id>, 20261018_<id>_2


def share_id_of(folder: str | Path) -> str:
    """The share id behind a folder name: the archive's date prefix and copy suffix and WO37's "-late" taken off."""
    name = Path(str(folder)).name
    m = _ARCHIVE.match(name)
    if m:
        name = m["id"]
    name = re.sub(r" \d{1,2}$", "", name)        # WO46: iCloud's second folder of one name ("<id> 2") is that share
    return name.removesuffix("-icloud").removesuffix("-late")


def seen(db: DB, sid: str, source: str = "icloud", expected: int | None = None, received: int | None = None,
         test: bool | None = None, batch_id: str | None = None) -> None:
    """The share's row, made or brought up to date on every look at it (never a step back: counts only grow,
    `received_at` is the first look that saw a photo)."""
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    stamp = now()
    if row is None:
        db.conn.execute(
            "INSERT INTO shares (id, source, expected, received, received_at, test, batch_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, source, expected, received or 0, stamp if received else None, int(bool(test)), batch_id, stamp))
        return
    fields: dict = {"updated_at": stamp}
    if source and row["source"] and source != row["source"]:
        fields["source"] = "both"
    elif source and not row["source"]:
        fields["source"] = source
    if expected is not None and (row["expected"] is None or expected > int(row["expected"] or 0)):
        fields["expected"] = expected
    if received is not None and received > int(row["received"] or 0):
        fields["received"] = received
        if not row["received_at"]:
            fields["received_at"] = stamp
    if test is not None and not row["test"] and test:
        fields["test"] = 1
    if batch_id and not row["batch_id"]:
        fields["batch_id"] = batch_id
    _update(db, sid, fields)


def step(db: DB, sid: str, name: str, batch_id: str | None = None) -> None:
    """One step done — complete | grouped | carded — stamped once (the first time only), so a restart that repeats a
    step keeps the first time."""
    assert name in ("complete", "grouped", "carded"), name
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    if row is None:
        seen(db, sid, batch_id=batch_id)
        row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    fields: dict = {}
    if not row[f"{name}_at"]:
        fields[f"{name}_at"] = now()
    if batch_id and not row["batch_id"]:
        fields["batch_id"] = batch_id
    if fields:
        _update(db, sid, {**fields, "updated_at": now()})


def outcome(db: DB, sid: str, what: str, reason: str | None = None, batch_id: str | None = None) -> None:
    """The share's end: carded | repeat | empty | same_share | failed | joined | held | replaced. A share carded once
    stays carded (a later failure of one item is that item's, told on its own); any other outcome may be replaced by a
    later one (empty → carded when the photos came after all; held → replaced when she shared them again)."""
    assert what in OUTCOMES, what
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    if row is None:
        seen(db, sid, batch_id=batch_id)
        row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    if row["outcome"] == "carded" and what != "carded":
        return
    fields = {"outcome": what, "reason": reason, "outcome_at": now(), "updated_at": now()}
    if batch_id and not row["batch_id"]:
        fields["batch_id"] = batch_id
    if what == "carded" and not row["carded_at"]:
        fields["carded_at"] = now()
    _update(db, sid, fields)


def of_batch(db: DB, bid: str):
    """The share a batch came from (by the batch's row, else its folder's name)."""
    row = db.conn.execute("SELECT * FROM shares WHERE batch_id=?", (bid,)).fetchone()
    if row is not None:
        return row
    b = db.batch(bid)
    if b is None:
        return None
    return db.conn.execute("SELECT * FROM shares WHERE id=?", (share_id_of(b["src_dir"]),)).fetchone()


def batch_carded(db: DB, bid: str) -> None:
    """Called when an item's card goes out (approve.send_item): the share's `carded_at` at the first card; the
    outcome `carded` once every item of the batch has had its card or needs none (dropped, or answered)."""
    row = of_batch(db, bid)
    if row is None:
        return
    step(db, row["id"], "carded", batch_id=bid)
    items = db.conn.execute("SELECT id, status FROM items WHERE batch_id=?", (bid,)).fetchall()
    if not items:
        return
    for it in items:
        if it["status"] in ("dropped", "failed"):
            continue
        sent = db.conn.execute("SELECT 1 FROM outbox WHERE kind='item' AND ref=? LIMIT 1", (it["id"],)).fetchone()
        if sent is None and it["status"] in ("new", "awaiting_price", "awaiting_condition", "needs_info"):
            return                                        # a card still to come
    outcome(db, row["id"], "carded", batch_id=bid)


def batch_failed(db: DB, bid: str, reason: str) -> None:
    """A batch that failed (a traceback, no usable photos): its share's outcome, unless carded already."""
    row = of_batch(db, bid)
    if row is not None:
        outcome(db, row["id"], "failed", reason[:300], batch_id=bid)


def doc(db: DB, sid: str) -> dict | None:
    """The row as /sync takes it."""
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    keys = ("id", "source", "expected", "received", "received_at", "complete_at", "grouped_at", "carded_at", "outcome",
            "outcome_at", "reason", "batch_id", "test", "updated_at", *QUEUE_FIELDS)
    return {k: row[k] for k in keys if k in row.keys()}


# ---------------------------------------------------------------- what an open share waits for (WO51 §6)
#
# Live 2026-11-01 (1:15 PM): "⏳ Share 12:39 PM (70 photos) stuck at carded since 12:44" — the share wasn't stuck: its
# 12 items were made and carded one at a time, and the next card waited for her answer to the open one (the queue is
# one message at a time, WO20). The API's watchdog only saw "no outcome yet". Now each open share carries what it waits
# for, worked out here from the queue itself and kept up to date by the worker (`refresh_open`, every minute): the
# state, the words, and when it last moved — the API's watchdog fires only when nothing moves and says what it waits for.

QUEUE_FIELDS = ("items", "cards", "state", "waiting", "moved_at")
STATES = ("work", "her", "send", "orphan", "done")   # processing · her answer · a card to send · an item lost · done
REFRESH_EVERY = 60                                   # seconds between two looks at the open shares (the worker's loop)
REFRESHED = "shares_refreshed_at"                    # kv: when the open shares were last looked at
DONE = ("ready", "posting", "posted", "drafted", "sold", "needs_owner")   # an item past its card
WHAT = {"item": "price", "condition": "answer (brand new or worn?)", "kids": "answer (Girls or Boys?)",
        "category": "answer (which category?)", "batch": "reply to the photo sheet",
        "regroup": "reply to the photo sheet"}


def refresh_open(db: DB, force: bool = False, at: float | None = None) -> list[str]:
    """Every open share's (complete, no outcome) queue fields brought up to date — written only when they changed, so
    the sync carries a change and nothing else; a share whose every item has had its card gets its outcome. At most
    every REFRESH_EVERY seconds (`force`: now). Returns the shares changed."""
    t = time.time() if at is None else at
    if not force and t - float(db.kv_get(REFRESHED) or 0) < REFRESH_EVERY:
        return []
    db.kv_set(REFRESHED, str(t))
    changed = []
    for row in db.conn.execute("SELECT * FROM shares WHERE complete_at IS NOT NULL AND outcome IS NULL").fetchall():
        fields = queue_state(db, row)
        if fields["state"] == "done" and row["batch_id"]:
            outcome(db, row["id"], "carded", batch_id=row["batch_id"])     # every item has had its card
        new = {k: v for k, v in fields.items() if row[k] != v}
        if new:
            _update(db, row["id"], {**new, "updated_at": now()})
            changed.append(row["id"])
    return changed


def queue_state(db: DB, row) -> dict:
    """What the share waits for now, from its items and the card queue: {"items", "cards", "state", "waiting",
    "moved_at"}. `waiting` is the object of "waiting for …" in the owner's words: "the grouping of its photos", "its
    items to be processed (3 of 12 done)", 'her price on "Edikted Crochet Mini Dress" — 8 more cards behind it', "the
    next card to go out (8 queued, none open for her)", 'a card for "…" — it has none and no place in the card queue
    (failed)'. `moved_at`: the share's last movement — a step, an item processed or answered, a card sent or answered
    (and, while it waits for her or for a card, any card's; while items are being processed, the worker's progress)."""
    from thrift_agent import alerts, approve
    steps = [row[k] for k in ("received_at", "complete_at", "grouped_at", "carded_at") if row[k]]
    bid = row["batch_id"]
    if not bid or not row["grouped_at"]:
        return {"items": None, "cards": None, "state": "work", "waiting": "the grouping of its photos",
                "moved_at": _latest(steps + [_progress(db, alerts.PROGRESS)])}
    items = [r for r in db.conn.execute("SELECT id, status, updated_at FROM items WHERE batch_id=? ORDER BY seq",
                                        (bid,)).fetchall() if r["status"] != "dropped"]
    ids = [r["id"] for r in items]
    marks = ",".join("?" * len(ids)) or "''"
    carded = {r[0] for r in db.conn.execute(f"SELECT DISTINCT ref FROM outbox WHERE kind='item' AND ref IN ({marks})",
                                            ids)}
    own = steps + [r["updated_at"] for r in items] + [
        x for r in db.conn.execute(f"SELECT sent_at, resolved_at FROM outbox WHERE ref IN ({marks})", ids)
        for x in (r["sent_at"], r["resolved_at"]) if x]
    lane = "test" if row["test"] else "real"
    batch = db.batch(bid)
    queued = approve.queue(db, lane)
    in_queue = {ref for _, ref in queued}
    done = [r for r in items if r["status"] in DONE or (r["status"] in approve.OWNER_WAITING and r["id"] in carded)]
    working = [r for r in items if r["status"] == "new"]
    waiting = [r for r in items if r["status"] in approve.OWNER_WAITING and r["id"] not in carded]
    sorted_ids = {r["id"] for r in done + working + waiting}
    lost = [r for r in items if r["id"] not in sorted_ids]                 # failed, or a status no step takes up
    if batch is not None and batch["status"] != "regroup":
        lost += [r for r in working + waiting if r["id"] not in in_queue]   # never asked: no place in the queue
    counts = {"items": len(items), "cards": len(done)}
    if not items:
        return {**counts, "state": "work", "waiting": "its items to be made", "moved_at": _latest(steps)}
    if batch is not None and batch["status"] == "regroup":
        return {**counts, "state": "her", "waiting": "her reply to the photo sheet ([Wrong photos])",
                "moved_at": _latest(own + [_queue_moved(db, lane)])}
    if lost:
        r = lost[0]
        return {**counts, "state": "orphan",
                "waiting": f'a card for "{_name(db, r["id"])}" — it has none and no place in the card queue '
                           f'({r["status"]})', "moved_at": r["updated_at"]}
    if working:
        return {**counts, "state": "work",
                "waiting": f"its items to be processed ({len(items) - len(working)} of {len(items)} done)",
                "moved_at": _latest(own + [_progress(db, alerts.PROGRESS)])}
    if waiting:
        moved = _latest(own + [_queue_moved(db, lane)])
        opened = approve.open_message(db, settle=False, lane=lane)
        if opened is None:
            return {**counts, "state": "send",
                    "waiting": f"the next card to go out ({len(waiting)} queued, none open for her)", "moved_at": moved}
        behind = sum(1 for e in queued if e != (opened["kind"], opened["ref"]))
        title = "the photo sheet" if opened["kind"] in ("batch", "regroup") else _name(db, opened["ref"])
        return {**counts, "state": "her",
                "waiting": f'her {WHAT.get(opened["kind"], "answer")} on "{title}" — {behind} more card'
                           f'{"s" if behind != 1 else ""} behind it', "moved_at": moved}
    return {**counts, "state": "done", "waiting": None, "moved_at": _latest(own)}


def _latest(stamps: list) -> str | None:
    """The latest of some ISO stamps (None and unreadable ones aside), in the ledger's own form."""
    best = None
    for s in stamps:
        try:
            t = datetime.fromisoformat(str(s))
        except (TypeError, ValueError):
            continue
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        best = t if best is None or t > best else best
    return best.astimezone(timezone.utc).isoformat(timespec="seconds") if best else None


def _progress(db: DB, key: str) -> str | None:
    """The worker's last progress (alerts' kv: {"at": …})."""
    try:
        return (json.loads(db.kv_get(key) or "{}") or {}).get("at")
    except (TypeError, ValueError):
        return None


def _queue_moved(db: DB, lane: str) -> str | None:
    """The last time the card queue of `lane` moved: a card sent or answered."""
    from thrift_agent import approve
    marks = ",".join("?" * len(approve.QUEUE_KINDS))
    rows = db.conn.execute(f"SELECT kind, ref, sent_at, resolved_at FROM outbox WHERE kind IN ({marks}) "
                           f"ORDER BY rowid DESC LIMIT 50", approve.QUEUE_KINDS).fetchall()
    return _latest([x for r in rows if approve.lane_of(db, r["kind"], r["ref"]) == lane
                    for x in (r["sent_at"], r["resolved_at"]) if x])


def _name(db: DB, iid: str) -> str:
    """The item's short name, as its ✅ card says it (brand + item, no size or colour)."""
    from thrift_agent.brain import copy as copywriter
    from thrift_agent.db import loads
    from thrift_agent.schema import Facts
    it = db.item(iid)
    if it is None:
        return iid
    render = (loads(it["renders"]) or {}).get("poshmark") or {}
    try:
        facts = Facts.model_validate(loads(it["facts"])) if it["facts"] else None
    except Exception:  # noqa: BLE001 — the title alone
        facts = None
    return copywriter.short_name(render.get("short_name"), render.get("title") or iid, facts)


def today(db: DB, day: str | None = None) -> list:
    """The day's shares (local day of their first look), newest first — `thrift shares`."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    return db.conn.execute("SELECT * FROM shares WHERE id LIKE ? OR substr(updated_at, 1, 10)=? ORDER BY id DESC",
                           (f"{day}%", day)).fetchall()


def lines(db: DB, rows=None) -> list[str]:
    out = []
    for r in rows if rows is not None else today(db):
        steps = " → ".join(f"{k} {_hm(r[f'{k}_at'])}" for k in ("received", "complete", "grouped", "carded")
                           if r[f"{k}_at"])
        count = f"{r['received'] or 0}" + (f" of {r['expected']}" if r["expected"] else "")
        out.append(f"{r['id']} ({r['source'] or '?'}; {count} photos): {steps or 'nothing yet'}"
                   + (f" → {r['outcome']}" if r["outcome"] else " (no outcome yet)")
                   + (f": {r['reason']}" if r["reason"] else "") + (" · test" if r["test"] else ""))
    return out


def _hm(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return "?"


def _update(db: DB, sid: str, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    db.conn.execute(f"UPDATE shares SET {cols} WHERE id=?", (*fields.values(), sid))


def stamp_of(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
