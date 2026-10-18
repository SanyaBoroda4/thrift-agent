"""The share ledger (WO45 §3): every share ends in exactly one visible outcome.

Live (Oct 12–18): 4 of 23 shares never became a card and 11 came short or late, and nothing anywhere said so — each
step trusted what arrived and no record tied a share to its end. Now every share is one row in `shares`, keyed by the
share id (the Shortcut's folder name, the same for the iCloud folder and the direct upload), with the time of each
step — received → complete → grouped → carded — and its outcome: `carded` (a card per item), `repeat`, `empty`,
`same_share` (the second copy of a share that arrived twice) or `failed` with its reason. Each step is recorded before
the next starts; the rows go to thrift-api with the rest of the sync, where a watchdog that doesn't depend on the Mac
tells the owner about a complete share with no outcome (api/thrift_api/timers.shares_watch) and the dashboard shows
the day's shares.

A share id: `2026-10-18_121154` (the Shortcut's date and time; §2 adds `_<random>`); an archive folder's name is
`20261018_<share id>`, a second archive of the same share `…_2`, WO37's copies `<share id>-late` — all one share."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent.db import DB, now

OUTCOMES = ("carded", "repeat", "empty", "same_share", "failed", "joined")
_ARCHIVE = re.compile(r"^\d{8}_(?P<id>.+?)(?:_\d{1,3})?$")      # 20261018_<id>, 20261018_<id>_2


def share_id_of(folder: str | Path) -> str:
    """The share id behind a folder name: the archive's date prefix and copy suffix and WO37's "-late" taken off."""
    name = Path(str(folder)).name
    m = _ARCHIVE.match(name)
    if m:
        name = m["id"]
    return name.removesuffix("-late")


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
    """The share's end: carded | repeat | empty | same_share | failed | joined. A share carded once stays carded (a
    later failure of one item is that item's, told on its own); any other outcome may be replaced by a later one
    (empty → carded when the photos came after all)."""
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
    return {k: row[k] for k in ("id", "source", "expected", "received", "received_at", "complete_at", "grouped_at",
                                "carded_at", "outcome", "outcome_at", "reason", "batch_id", "test", "updated_at")}


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
