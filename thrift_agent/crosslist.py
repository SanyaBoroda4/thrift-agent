"""Cross-listing on Depop and Vinted (WO30): once an item is live on Poshmark, the same item at the same approved price
goes up on Depop, then Vinted — no extra questions, every dropdown value from the saved catalogs (thrift_agent.catalogs).

- A `listings` row per marketplace: queued when Poshmark posts (or by `thrift crosslist`), then the poster takes it,
  right after the item's Poshmark listing (5–15 s apart, `crosslist.gap_seconds`, WO32b), within the hours and
  `marketplaces.<m>.daily_cap` (25 a day each).
- The same two keys as Poshmark (poster.dry_run off + poster.autopublish_confirmed on) AND
  `marketplaces.<m>.autopublish`; otherwise a dry run: the whole form filled, the screenshot and the mapped fields to the
  ops chat, the tab closed without publishing.
- A logged-out / CAPTCHA / verification wall stops that marketplace for the window (one plain line in the group); a
  failure before the publish button is retried next window (3 attempts, then skipped with an ops note); a publish the
  Mac was interrupted in is looked for in that site's shop (WO28), never published again blind.
- The group hears ONE line per item when its marketplaces are done: "Posted ✓ <title> — $X · Poshmark <url> · Depop
  <url> · Vinted <url>" (+ " — check: …"); a marketplace that failed or was skipped is left out (the details: ops)."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent import catalogs, daily, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, listing_id_from, loads

CROSS = catalogs.CROSS
ORDER = ("poshmark", *CROSS)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
BLOCKS = "crosslist_blocked"           # kv: {mp: {"window", "reason", "since"}}: stopped for this window
WINDOW_SEEN = "crosslist_window"       # kv: the daily window the poster last saw
STREAK = "crosslist_failures"          # kv: {mp: failures in a row}: N in a row stop that marketplace for the window
EXT_LINK = "ext_link"                  # kv: {"connected", "at"}: the poster's view of the Thrift Chrome extension (WO32)
MAX_ATTEMPTS = 3                       # a failure before the publish button: retried next window, up to this many
UNCONFIRMED = "unconfirmed publish: "
SKIPPED = "skipped: "
# The group's one plain line when a marketplace stops for the window, by what the site showed (WO32).
ALERTS = {"login": "{site} needs you to log in on the Mac.",
          "captcha": "{site} shows a CAPTCHA — solve it in its Chrome window on the Mac.",
          "verify": "{site} asks for a check — open it on the Mac.",
          "block": "{site} turned the Mac away for now — I'll try again next time the Mac is open."}
DRIVERS = {"depop": ("extension", "playwright", "api"), "vinted": ("extension", "playwright")}
ALL_DONE_LINE = "💤 All done — you can close the Mac."          # WO34: the group card's last line
DOT = {"poshmark": "🟣", "depop": "🔴", "vinted": "🟢"}
CARRIER = "posted_done_carrier"         # kv: the item whose group card carries the 💤 line now (WO34)
LATER = ("queued", "posting", "dryrun", "failed")   # a site still to come: "⏳ Vinted — later"


def enabled(s: Settings) -> list[str]:
    """The cross-list marketplaces that are on and whose catalog loads (a broken catalog turns only that one off)."""
    ok = catalogs.check()
    return [mp for mp in CROSS if s.get(f"marketplaces.{mp}.enabled", False) and ok.get(mp) is None]


def driver(s: Settings, mp: str) -> str:
    """How a marketplace is posted to (WO32): "extension" — the Thrift Chrome's extension, the seller's own Chrome (the
    default); "playwright" — WO30's poster profile (Vinted and Depop turn it away); "api" — Depop's Selling API (a
    stub until its key arrives)."""
    d = str(s.get(f"marketplaces.{mp}.driver") or "extension").strip().lower()
    if d not in DRIVERS.get(mp, ("playwright",)):
        raise ValueError(f"marketplaces.{mp}.driver must be one of {', '.join(DRIVERS.get(mp, ()))}, not {d!r}")
    return d


def alert_kind(reason: str, page: str | None = None) -> str:
    """What the site showed, for the owner's line: the poster's own word for it, else read from the reason."""
    if page in ALERTS:
        return page
    low = reason.lower()
    if "captcha" in low:
        return "captcha"
    if "verif" in low or "check" in low:
        return "verify"
    if "turned" in low or "block" in low or "403" in low or "not authorized" in low:
        return "block"
    return "login"


def reachable(s: Settings, db: DB) -> list[str]:
    """The enabled marketplaces the poster can work on now: one on the extension driver only while the poster sees the
    extension connected (WO32) — for the daily window's "still to publish", which mustn't wait on a missing Chrome."""
    link = loads(db.kv_get(EXT_LINK)) or {}
    return [mp for mp in enabled(s) if driver(s, mp) == "playwright"
            or (driver(s, mp) == "extension" and link.get("connected"))]


def live(s: Settings, mp: str) -> bool:
    """Publishes for real: on the Mac, both poster keys AND the marketplace's own autopublish (WO30 §5)."""
    return bool(s.is_prod and not s.get("poster.dry_run", True) and s.get("poster.autopublish_confirmed", False)
                and s.get(f"marketplaces.{mp}.autopublish", False))


def daily_cap(s: Settings, mp: str) -> int:
    return int(s.get(f"marketplaces.{mp}.daily_cap") or s["schedule"]["daily_cap"])


def gap(s: Settings) -> float:
    lo, hi = s.get("crosslist.gap_seconds") or [5, 15]
    return random.uniform(float(lo), float(hi))


def unconfirmed_ref(iid: str, mp: str) -> str:
    """The outbox ref of an "unconfirmed publish" message: the item, plus the marketplace when it isn't Poshmark."""
    return iid if mp == "poshmark" else f"{iid}:{mp}"


def split_ref(ref: str) -> tuple[str, str]:
    iid, _, mp = ref.partition(":")
    return iid, (mp or "poshmark")


# ---------------------------------------------------------------- the queue

def queue(s: Settings, db: DB, iid: str, mps: list[str] | None = None, why: str = "poshmark posted") -> list[str]:
    """One 'queued' row per cross-list marketplace the item has none on, at the owner's approved price. Returns them."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    out = []
    for mp in mps if mps is not None else enabled(s):
        if db.listing(iid, mp) is None:
            db.upsert_listing(iid, mp, status="queued", price=it["owner_price"])
            out.append(mp)
    if out:
        db.log(iid, "crosslist_queued", {"mps": out, "why": why})
    return out


def window(db: DB) -> str:
    """The current daily window (WO28: the worker begins one at a start or a wake with the lid open)."""
    return db.kv_get(daily.SESSION_KEY) or datetime.now().strftime("%Y-%m-%d")


def new_window(s: Settings, db: DB) -> bool:
    """Between listings: a window the poster hasn't seen lifts the blocks and puts the failures that never reached the
    publish button back in line (MAX_ATTEMPTS each; the last one becomes 'skipped', with an ops note)."""
    w = window(db)
    if db.kv_get(WINDOW_SEEN) == w:
        return False
    db.kv_set(WINDOW_SEEN, w)
    db.kv_set(BLOCKS, "{}")
    db.kv_set(STREAK, "{}")
    for row in db.conn.execute(f"SELECT * FROM listings WHERE status='failed' AND url IS NULL AND marketplace IN "
                               f"({','.join('?' * len(CROSS))})", CROSS).fetchall():
        if (row["error"] or "").startswith(UNCONFIRMED):
            continue                                    # it may be live: the owner settles it (invariant 4)
        iid, mp = row["item_id"], row["marketplace"]
        if row["attempts"] < MAX_ATTEMPTS:
            db.upsert_listing(iid, mp, status="queued")
            db.log(iid, "crosslist_retry", {"mp": mp, "attempts": row["attempts"]})
        else:
            db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + f"{MAX_ATTEMPTS} attempts: {row['error']}")
            notify.say(f"⏭ {LABEL[mp]} skipped for {iid} after {MAX_ATTEMPTS} attempts: {row['error']}")
            from thrift_agent.post import runner
            runner.ask_skipped(db, iid, mp, _title_of(db, iid), f"{MAX_ATTEMPTS} tries didn't go through")
    return True


def blocked(db: DB, mp: str) -> str | None:
    b = (loads(db.kv_get(BLOCKS)) or {}).get(mp)
    return b["reason"] if b and b.get("window") == window(db) else None


def block(db: DB, mp: str, reason: str, page: str | None = None) -> None:
    """Stop `mp` for this window (logged out, CAPTCHA, a verification wall, a block page, failures in a row): ONE plain
    line in the group for an account problem — what to do, by what the site showed (`page`) — the reason in the ops
    chat; Poshmark and the other marketplace go on."""
    blocks = loads(db.kv_get(BLOCKS)) or {}
    if (blocks.get(mp) or {}).get("window") == window(db):
        return
    blocks[mp] = {"window": window(db), "reason": reason, "since": datetime.now(timezone.utc).isoformat()}
    db.kv_set(BLOCKS, json.dumps(blocks))
    db.log(None, "crosslist_blocked", {"mp": mp, "reason": reason, "page": page})
    if not reason.startswith("failures:"):
        notify.group(ALERTS[alert_kind(reason, page)].format(site=LABEL[mp]))
    notify.say(f"⛔ {LABEL[mp]} stopped for this window: {reason}")


def failure(db: DB, mp: str, failed: bool, limit: int = 3) -> None:
    """The per-marketplace circuit breaker: `limit` failures in a row stop that marketplace for the window."""
    streak = loads(db.kv_get(STREAK)) or {}
    streak[mp] = streak.get(mp, 0) + 1 if failed else 0
    db.kv_set(STREAK, json.dumps(streak))
    if streak[mp] >= limit:
        block(db, mp, f"failures: {streak[mp]} in a row")


def capped(s: Settings, db: DB, mp: str) -> bool:
    from thrift_agent.scheduler import windows
    _, midnight = windows(tz=s["schedule"]["timezone"])
    return db.listed_since(midnight, mp) >= daily_cap(s, mp)


def pending(s: Settings, db: DB, mps: list[str] | None = None) -> list[tuple[str, str]]:
    """(item, marketplace) the poster takes now, oldest Poshmark listing first: a 'queued' row — and a 'dryrun' one
    once the marketplace publishes for real — on an enabled marketplace that isn't stopped for the window or at its
    daily cap."""
    out = []
    for mp in mps if mps is not None else enabled(s):
        if blocked(db, mp) or capped(s, db, mp):
            continue
        states = ("queued", "dryrun") if live(s, mp) else ("queued",)
        rows = db.conn.execute(
            f"SELECT l.item_id FROM listings l LEFT JOIN listings p ON p.item_id=l.item_id AND p.marketplace='poshmark' "
            f"WHERE l.marketplace=? AND l.status IN ({','.join('?' * len(states))}) "
            f"ORDER BY COALESCE(p.posted_at, l.updated_at), l.rowid", (mp, *states)).fetchall()   # ties: queue order
        out += [(r["item_id"], mp) for r in rows]
    return out


def next_job(s: Settings, db: DB, prefer: str | None = None, mps: list[str] | None = None) -> tuple[str, str] | None:
    """The next cross-list job: the item just listed first (Poshmark → Depop → Vinted, then the next item), else the
    oldest waiting one. `mps`: only these marketplaces (WO32: the ones whose poster can take a job now)."""
    jobs = pending(s, db, mps)
    if not jobs:
        return None
    if prefer is not None:
        mine = [j for j in jobs if j[0] == prefer]
        if mine:
            return min(mine, key=lambda j: ORDER.index(j[1]))
    first = jobs[0][0]
    return min((j for j in jobs if j[0] == first), key=lambda j: ORDER.index(j[1]))


# ---------------------------------------------------------------- recording an outcome

def fields_summary(fields: dict) -> str:
    keys = ("category", "category_path", "size", "condition", "colors", "materials", "skirt_length", "attributes",
            "source", "age", "package_size", "package_sizes", "brand", "price")
    lines = [f"{k}: {fields[k]}" for k in keys if fields.get(k) not in (None, [], {}, "")]
    lines.append(f"photos: {len(fields.get('photos') or [])}")
    return "\n".join(lines)


def record(s: Settings, db: DB, iid: str, mp: str, title: str, out, fields: dict, seconds: float | None = None,
           slept: bool = False, lines: list[str] | None = None) -> None:
    """The listing row, the event, and the messages for one Depop/Vinted Outcome. Nothing here goes to the group but an
    unconfirmed publish's question (the "Posted ✓" line comes from announce()). `lines`: the extension job's progress
    ("photos 6/6 · category ✓ · … · filled in 21.3 s", WO32b), in a dry run's ops message."""
    from thrift_agent.post import runner           # the poster's own wording for an unconfirmed publish
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = out.error
    if out.status == "failed" and out.clicked and not out.url:
        error = UNCONFIRMED + (error or "")
    if out.status == "skipped":
        error = SKIPPED + (error or "")
    status = {"cancelled": "queued"}.get(out.status, out.status)
    guesses = [*fields.get("guesses", []), *out.guesses]
    db.upsert_listing(iid, mp, status=status, url=out.url, listing_id=listing_id_from(mp, out.url),
                      error=error if out.status != "cancelled" else out.note,
                      posted_at=stamp if out.status in ("posted", "dryrun") else None,
                      fields_json={**fields, "guesses": guesses, "note": out.note})
    db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": error, "shot": out.screenshot,
                                       "note": out.note, "clicked": out.clicked, "guesses": guesses,
                                       "seconds": round(seconds, 1) if seconds else None, "slept": slept or None,
                                       "progress": list(lines) if lines else None})
    shot = Path(out.screenshot or "")
    note = f"\n{out.note}" if out.note else ""
    progress = f"\n{' · '.join(lines)}" if lines else ""
    if out.status == "failed" and out.clicked and not out.url:
        runner.ask_unconfirmed(db, iid, mp, runner.unconfirmed_text(title, slept, mp))
    elif out.status == "failed":
        row = db.listing(iid, mp)
        again = "retried next window" if row and row["attempts"] < MAX_ATTEMPTS else "no more attempts: skipped next window"
        notify.ops_photo(shot, f"❌ {LABEL[mp]} failed ({iid}): {title}\n{error}{note}\n({again})")
    elif out.status == "skipped":
        notify.ops_photo(shot, f"⏭ {LABEL[mp]} skipped ({iid}): {title}\n{out.error}{note}")
        from thrift_agent.post import runner
        runner.ask_skipped(db, iid, mp, title, f"{LABEL[mp]}'s form doesn't take one of its details")
    elif out.status == "dryrun":
        notify.ops_photo(shot, f"🧪 dry-run {LABEL[mp]} ({iid}): {title} — ${fields.get('price')}\n"
                               f"{fields_summary(fields)}{progress}{note}")
    elif out.status == "posted" and out.note:
        notify.ops_photo(shot, f"{LABEL[mp]} posted ({iid}) {out.url}: {out.note}")


def skip_unmappable(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """A required field the catalog can't take from the facts: this marketplace is skipped for this item (the others
    go on), with the reason in the ops chat."""
    db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)
    db.log(iid, "post_skipped", {"mp": mp, "error": why, "mapping": True})
    notify.say(f"⏭ {LABEL[mp]} skipped ({iid}): {title}\ncan't map: {why}")


# ---------------------------------------------------------------- the group's one line per item

def announced(db: DB, iid: str) -> set[str]:
    done: set[str] = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='posted_announced'", (iid,)):
        done |= set((loads(detail) or {}).get("mps") or [])
    return done


def done_line(state: list[str] | None) -> str | None:
    """The card's last line (WO34): "💤 All done — you can close the Mac." when nothing is left on any site; "💤 Done for
    now — you can close the Mac. Vinted catches up next time." when a stopped site still has listings; None while
    there is work."""
    if state is None:
        return None
    if not state:
        return ALL_DONE_LINE
    names = [LABEL.get(mp, mp) for mp in state]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"💤 Done for now — you can close the Mac. {joined} catch{'es' if len(names) == 1 else ''} up next time."


def card_text(s: Settings, db: DB, iid: str, last: str | None = None) -> str:
    """The group's card for an item (WO34, the owner: separate lines, little text, some emoji): "✅ <short name> ·
    $<price>", then each site on its own line — its name the link (HTML), "⏳ Vinted — later" while it hasn't posted —
    with an empty line between, and the 💤 line when it is the window's last. Nothing technical."""
    import html

    from thrift_agent.brain import copy as copywriter
    from thrift_agent.schema import Facts
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    try:
        facts = Facts.model_validate(loads(it["facts"])) if it and it["facts"] else None
    except Exception:  # noqa: BLE001 — the name falls back to the title alone
        facts = None
    name = copywriter.short_name(render.get("short_name"), render.get("title") or iid, facts)
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    price = (it["owner_price"] if it else None) or render.get("price") or \
        next((r["price"] for r in rows.values() if r["price"]), "")
    lines = [f"✅ {html.escape(name)} · ${price}"]
    sites = [*(["poshmark"] if s is None or s.get("marketplaces.poshmark.enabled", True) else []),
             *(enabled(s) if s is not None else [m for m in ORDER if m != "poshmark" and m in rows])]
    for mp in sites:
        row = rows.get(mp)
        if row is None:
            continue
        if row["status"] == "posted" and row["url"]:
            lines.append(f'{DOT.get(mp, "•")} <a href="{html.escape(row["url"], quote=True)}">{LABEL.get(mp, mp)}</a>')
        elif row["status"] in LATER and not (row["status"] == "failed" and row["error"]
                                             and row["error"].startswith(UNCONFIRMED)) \
                and not (row["status"] == "dryrun" and not (s is not None and live(s, mp))):
            lines.append(f"⏳ {LABEL.get(mp, mp)} — later")     # a dry run on a site that won't publish: left out
    if last:
        lines.append(last)
    return "\n\n".join(lines)


def _card(s: Settings, db: DB, iid: str, text: str, edit: bool) -> None:
    """The card out (`edit`: the same message changed — never a new one), HTML, previews off; on the dev machine
    (no bot) printed."""
    from thrift_agent import approve
    bot = approve.bot_for(s) if s is not None else None
    row = db.conn.execute("SELECT * FROM outbox WHERE kind='posted' AND ref=? ORDER BY rowid DESC LIMIT 1",
                          (iid,)).fetchone()
    if bot is None:
        notify.group(text)
        if row is None:
            db.add_outbox("dev", 0, "posted", iid, text=text)
            db.outbox_resolve("posted", iid)
        return
    if edit and row is not None and str(row["chat_id"]) == str(bot.chat_id) and int(row["message_id"]):
        try:
            bot.edit_message(int(row["message_id"]), text, parse_mode="HTML")
            db.conn.execute("UPDATE outbox SET text=? WHERE rowid=(SELECT rowid FROM outbox WHERE kind='posted' "
                            "AND ref=? ORDER BY rowid DESC LIMIT 1)", (text, iid))
        except Exception as e:  # noqa: BLE001 — "message is not modified" and the like: the card stays as it is
            db.log(iid, "card_edit", {"error": str(e)[:200]})
        return
    mid = bot.send_message(text, parse_mode="HTML")
    db.add_outbox(bot.chat_id, mid, "posted", iid, text=text)
    db.outbox_resolve("posted", iid)


def _say_checks(db: DB, iid: str, mps: list[str]) -> None:
    """The posters' notes (a brand left empty, a size taken as the nearest, …) to the ops chat, each once per item —
    never to the group (WO34, the owner: nothing technical in the group; the MNG card had a Depop note twice)."""
    said = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='checks_said'", (iid,)):
        said |= set((loads(detail) or {}).get("notes") or [])
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    notes = []
    for mp in mps:
        f = loads((rows.get(mp) or {})["fields_json"]) if rows.get(mp) and rows[mp]["fields_json"] else {}
        for c in [*(f.get("guesses") or []), *(f"copy: {x}" for x in (f.get("checks") or []))]:
            note = f"{LABEL.get(mp, mp)}: {c}"
            if note not in said and note not in notes:
                notes.append(note)
    if notes:
        it = db.item(iid)
        title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
        db.log(iid, "checks_said", {"notes": notes})
        notify.say(f"Check — {title}:\n" + "\n".join(f"- {n}" for n in notes))


def _carry_done(s: Settings, db: DB, iid: str | None) -> None:
    """The 💤 line rides on the newest card (WO34): `iid` takes it (None: it stays where it is) and the card that had
    it is edited without it; the carrier's line follows the state (Done for now -> All done once Vinted caught up)."""
    from thrift_agent import daily
    before = db.kv_get(CARRIER)
    if iid is not None and before and before != iid:
        _card(s, db, before, card_text(s, db, before), edit=True)
    carrier = iid or before
    if carrier:
        db.kv_set(CARRIER, carrier)
        if iid is None:                                  # only the line may have changed: the carrier's card again
            _card(s, db, carrier, card_text(s, db, carrier, done_line(daily.done_state(s, db))), edit=True)


def posted_line(s: Settings, db: DB, iid: str, mps: list[str], done: bool = False) -> str:
    """ "Posted ✓ <title> — $X · Poshmark <url> · Depop <url> · Vinted <url>" (+ " — check: …"; + "✓ All done …")."""
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    price = (rows.get(mps[0]) or {})["price"] if mps and rows.get(mps[0]) and rows[mps[0]]["price"] else \
        (it["owner_price"] if it else None) or render.get("price")
    checks = []
    for mp in mps:
        f = loads(rows[mp]["fields_json"]) or {}
        for c in [*(f.get("guesses") or []), *(f"copy: {x}" for x in (f.get("checks") or []))]:
            checks.append(c if mp == "poshmark" else f"{LABEL[mp]}: {c}")
    parts = " · ".join(f"{LABEL[mp]} {rows[mp]['url'] or ''}".rstrip() for mp in mps)
    text = f"Posted ✓ {render.get('title') or iid} — ${price} · {parts}"
    text += f" — check: {'; '.join(checks)}" if checks else ""
    return text + (f"\n{ALL_DONE_LINE}" if done else "")


def unconfirmed(db: DB, iid: str) -> list[str]:
    """The item's marketplaces where a publish may be live but its address wasn't found (the owner's ⚠️ question)."""
    return [r["marketplace"] for r in db.listings_for(iid)
            if r["status"] == "failed" and not r["url"] and (r["error"] or "").startswith(UNCONFIRMED)]


def added_line(db: DB, iid: str, mps: list[str]) -> str:
    """The ops chat's "Added: <title> · Depop <url>" for sites added to an item already announced (WO32b)."""
    it = db.item(iid)
    title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    parts = " · ".join(f"{LABEL[mp]} {rows[mp]['url'] or ''}".rstrip() for mp in mps)
    return f"Added: {title} · {parts}"


SETTLED = ("posted", "failed", "skipped", "drafted", "dryrun", "delisted", "sold")
AWAY_WAIT = 600.0      # a site whose extension is away holds an item's line this long after its first live listing


def settled(s: Settings, db: DB, iid: str, now: datetime | None = None) -> bool:
    """Has every enabled site of the item had its say (WO33 A3)? Posted, failed (an unconfirmed publish too: its ⚠️
    question waits for the owner), skipped, drafted or dry-run — or queued on a site stopped for the window or at its
    daily cap, or on one whose extension has been away for 10 minutes since the item's first live listing (a moment's
    disconnect doesn't send the line without it). A site with no row yet, or one in progress, is not settled."""
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    mps = [*(["poshmark"] if s.get("marketplaces.poshmark.enabled", False) else []), *enabled(s)]
    first = min((r["posted_at"] for r in rows.values() if r["status"] == "posted" and r["posted_at"]), default=None)
    now = now or datetime.now(timezone.utc)
    waited = first is not None and (now - datetime.fromisoformat(first)).total_seconds() >= AWAY_WAIT
    for mp in mps:
        row = rows.get(mp)
        if row is None:
            return False
        if row["status"] in SETTLED:
            continue
        if row["status"] == "queued" and mp != "poshmark" and (blocked(db, mp) or capped(s, db, mp) or
                                                               (mp not in reachable(s, db) and waited)):
            continue
        return False
    return True


def waiting_lines(db: DB) -> list[str]:
    """Items with a live listing whose group line hasn't gone out yet (the poster sweeps them: a site that was away)."""
    return [r[0] for r in db.conn.execute(
        "SELECT DISTINCT l.item_id FROM listings l WHERE l.status='posted' AND NOT EXISTS "
        "(SELECT 1 FROM events e WHERE e.ref=l.item_id AND e.kind='posted_announced')").fetchall()]


def _line_due(s: Settings, db: DB, iid: str) -> bool:
    """Another item's first line is due now (settled, not yet sent): "✓ All done" goes on the window's last line, so
    not on this one (WO33, a loaded CI runner: two items finished every site before either line went out, and both
    lines said "All done")."""
    return any(other != iid and settled(s, db, other) for other in waiting_lines(db))


def announce(s: Settings | None, db: DB, iid: str) -> str | None:
    """The group hears about an item ONCE (WO32b): the first time its listings go out — once all its sites are settled
    (WO33 A3: posted, failed, skipped, or waiting on the owner's answer to the ⚠️ question) — one "Posted ✓" line with
    every site that confirmed, Poshmark · Depop · Vinted. Sites added to an item already announced (a supervised
    publish, the backfill, a retry, a 'posted <url>' reply) go to the ops chat only: "Added: <title> · Depop <url>".
    Returns the text sent (None: nothing new)."""
    seen = announced(db, iid)
    mps = [r["marketplace"] for r in db.listings_for(iid) if r["status"] == "posted" and r["marketplace"] not in seen]
    if not mps:
        return None
    has_card = db.conn.execute("SELECT 1 FROM outbox WHERE kind='posted' AND ref=? LIMIT 1", (iid,)).fetchone()
    if seen and has_card and s is not None:            # WO34: its card is out — the new link in place, no new message
        carrier = db.kv_get(CARRIER) == iid
        text = card_text(s, db, iid, done_line(daily.done_state(s, db)) if carrier else None)
        _card(s, db, iid, text, edit=True)
        db.log(iid, "posted_announced", {"mps": mps, "to": "edit"})
        _say_checks(db, iid, mps)
        if not carrier:
            _carry_done(s, db, None)
        return text
    if seen:                                           # announced before WO34 (the one-line message): the ops chat
        text = added_line(db, iid, mps)
        db.log(iid, "posted_announced", {"mps": mps, "to": "ops"})
        notify.say(text)
        return text
    if s is not None and not settled(s, db, iid):
        return None
    state = daily.done_state(s, db) if s is not None and not _line_due(s, db, iid) else None
    text = card_text(s, db, iid, done_line(state))
    db.log(iid, "posted_announced", {"mps": mps})
    _card(s, db, iid, text, edit=False)
    _say_checks(db, iid, mps)
    if state is not None:
        _carry_done(s, db, iid)
    return text


# ---------------------------------------------------------------- backfill: the existing closet

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 "
      "Safari/537.36")


def poshmark_live(url: str) -> bool | None:
    """Is this Poshmark listing still for sale? Its public page: listingDetails.inventory.status "available" → True;
    any other status, or a page that's gone (404 / 410) → False; a page that can't be read → None (unknown)."""
    return poshmark_page(url)[0]


_TLS = None


def _tls():
    """The TLS settings for Poshmark's public pages: a classic key exchange (P-256). Its CloudFront refuses OpenSSL
    3.5's default handshake — the post-quantum key share python.org's Python 3.14 on the Mac offers: every public page
    answered 403 "Request blocked" there (WO33, live), while the same request with P-256 — what OpenSSL 3.0 on the PC
    offers by default — is answered as it should be (404 for a deleted listing, 200 for a live one)."""
    global _TLS
    if _TLS is None:
        import ssl

        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.set_ecdh_curve("prime256v1")
        _TLS = ctx
    return _TLS


def poshmark_page(url: str) -> tuple[bool | None, int | None]:
    """(still for sale — as poshmark_live —, its price today: listingDetails.price_amount, whole dollars, or None)."""
    import httpx

    from thrift_agent.harvest import parse_state
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=True,
                      verify=_tls())
    except httpx.HTTPError:
        return None, None
    if r.status_code in (404, 410):
        return False, None
    if r.status_code != 200:
        return None, None
    try:
        details = parse_state(r.text).get("$_listing_details", {}).get("listingDetails", {})
    except ValueError:
        return None, None
    status = ((details.get("inventory") or {}).get("status") or "").lower()
    try:
        price = round(float((details.get("price_amount") or {}).get("val")))
    except (TypeError, ValueError):
        price = None
    return (None if not status else status == "available"), (price or None)


def backfill(s: Settings, db: DB, mps: list[str], check: bool = True, dry: bool = False,
             live=None, page=None) -> list[tuple[str, str]]:
    """`thrift crosslist --backfill`: every item live on Poshmark that isn't on these marketplaces yet, oldest first,
    queued — after its Poshmark page says it is still for sale (sold, removed or unreadable: left out). The poster
    then takes them within each marketplace's daily cap. `dry`: only says what it would queue.
    At each item's price on Poshmark TODAY (WO33, the owner: some were changed there by hand): a price that differs
    from ours becomes the item's price — the one Depop and Vinted get — and the differences go to the ops chat in one
    message. (`live`: a for-sale check without a price, the tests'.)"""
    read = page or ((lambda url: (live(url), None)) if live is not None else poshmark_page)
    out, changed = [], []
    rows = db.conn.execute("SELECT item_id, url FROM listings WHERE marketplace='poshmark' AND status='posted' "
                           "AND url IS NOT NULL ORDER BY posted_at, item_id").fetchall()
    for row in rows:
        iid = row["item_id"]
        missing = [mp for mp in mps if db.listing(iid, mp) is None]
        if not missing:
            continue
        note = ""
        if check:
            state, price = read(row["url"])
            if state is None:
                out.append((iid, "couldn't read its Poshmark page: left out"))
                continue
            if not state:
                out.append((iid, "no longer for sale on Poshmark: left out"))
                continue
            ours = int((db.item(iid) or {})["owner_price"] or 0)
            if price and price != ours:
                note = f" — Poshmark price ${price} (ours ${ours})" if ours else f" — Poshmark price ${price}"
                changed.append((iid, ours, price))
                if not dry:
                    db.set_item(iid, owner_price=price)
                    db.upsert_listing(iid, "poshmark", price=price)
                    db.log(iid, "price_from_poshmark", {"was": ours or None, "now": price})
        if dry:
            out.append((iid, f"would queue {', '.join(missing)}{note}"))
        else:
            queue(s, db, iid, missing, why="backfill")
            out.append((iid, f"queued {', '.join(missing)}{note}"))
    if changed and not dry:
        lines = [f"• {_title_of(db, iid)}: ${ours} → ${price}" if ours else f"• {_title_of(db, iid)}: ${price}"
                 for iid, ours, price in changed]
        text = (f"💲 Backfill at today's Poshmark prices — {len(changed)} differ from our records (now the price on "
                f"Depop and Vinted too):\n" + "\n".join(lines))
        notify.say(text[:3900])
    return out


def _title_of(db: DB, iid: str) -> str:
    it = db.item(iid)
    return ((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or iid if it else iid
