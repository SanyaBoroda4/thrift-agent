"""WO52 follow-up (the owner, 2026-11-01): a Vinted listing that went up with no brand is DELETED and posted again with
its brand — Vinted refuses the brand change on a live listing ("Create new listing to change to luxury brand"). Only
for a listing nobody has shown interest in: no likes, no conversations (offers happen in conversations), not reserved.

    thrift vinted-repost <item> --check      likes, views, conversations — read only (the running poster reads them)
    thrift vinted-repost <item> --practice   the delete dialog opened and Cancelled — its buttons recorded (once)
    thrift vinted-repost <item> --go         checked again; then delete → gone → the row back in line

The running poster serves the requests (`serve`, from runner.serve_cross_requests). After the delete the Vinted row is
`queued` with no address; the normal posting lists it again — the same photos, title and price, its brand now on the
catalog's names (the parent of a sub-line too). When it is posted (`after_posted`, from crosslist.record), the listing
keeps its ORIGINAL posted_at — the price-drop clock never restarts — the new posting time and the old address go into
its fields (`reposted_at`, `reposted_from`), the ✅ card is edited to the new address, and ONE private line says so.
kv `vinted_reposts` (the requests), `vinted_delete_window` (the practice run's dialog), `vinted_repost:<item>` (the
listing before the delete)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from thrift_agent import notify
from thrift_agent.db import DB, loads

REQUESTS = "vinted_reposts"
WINDOW = "vinted_delete_window"
BEFORE = "vinted_repost:"
CANCEL_WORDS = ("cancel", "no", "keep", "keep listing", "go back", "back", "close")


def ask(db: DB, iid: str, step: str) -> None:
    reqs = loads(db.kv_get(REQUESTS)) or []
    reqs.append({"item": iid, "step": step})
    db.kv_set(REQUESTS, json.dumps(reqs))


def _title(db: DB, iid: str) -> str:
    it = db.item(iid)
    return (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or iid) if it is not None else iid


def interested(info: dict) -> str | None:
    """Why the listing must stay (someone showed interest, or it couldn't be told) — None: free to delete."""
    if info.get("gone"):
        return "it isn't in Vinted's data any more"
    if info.get("reserved"):
        return "it is reserved"
    for what in ("likes", "conversations"):
        n = info.get(what)
        if n is None:
            return f"its {what} couldn't be read ({info.get('inbox_error') or 'not given'})"
        if int(n) > 0:
            return f"{n} {what[:-1] if int(n) == 1 else what}"
    return None


def confirm_text(db: DB) -> str | None:
    """The dialog's delete button, as the practice run recorded it (the one button that isn't a cancel)."""
    win = loads(db.kv_get(WINDOW)) or {}
    others = [b for b in win.get("buttons") or [] if b and b.strip().lower() not in CANCEL_WORDS]
    return others[0] if len(others) == 1 else None


async def serve(db: DB, ps: dict) -> list[str]:
    """The requests, by the running poster's Vinted driver. Returns the items served."""
    with db.tx():
        reqs = loads(db.kv_get(REQUESTS)) or []
        if reqs:
            db.kv_set(REQUESTS, "[]")
    poster = ps.get("vinted")
    done = []
    for req in reqs:
        iid, step = req.get("item"), req.get("step")
        row = db.listing(iid, "vinted")
        title = _title(db, iid)
        if poster is None or row is None or not row["url"] or (row["status"] != "posted" and step != "check"):
            notify.say(f"♻️ Vinted repost of {title}: nothing to do (not live on Vinted, or Vinted off)")
            continue
        try:
            if step == "check":
                info = await poster.vinted_interest(row["url"])
                db.log(iid, "vinted_interest", info)
                stay = interested(info)
                notify.say(f"🔎 Vinted · {title}: {info.get('likes')} likes, {info.get('views')} views, "
                           f"{info.get('conversations')} conversations — " + (f"stays ({stay})" if stay else "free to repost"))
            elif step == "practice":
                out = await poster.vinted_practice_delete(row["url"])
                db.kv_set(WINDOW, json.dumps(out.get("window") or {}))
                db.log(iid, "vinted_delete_practice", out)
                notify.say(f"🧪 Vinted delete dialog recorded ({title}): buttons {out['window'].get('buttons')} — "
                           f"Cancel pressed, nothing deleted")
            elif step == "go":
                await go(db, poster, iid, row, title)
            elif step == "probe":                       # the delete job with "cancel" as its button: reaches the
                ok = await poster.vinted_delete_probe(row["url"])   # listing page, presses nothing
                db.log(iid, "vinted_delete_probe", {"ok": ok})
                notify.say(f"🧪 Vinted delete probe ({title}): " + ("the listing page is reached — nothing pressed"
                                                                   if ok else "the job did NOT reach the listing page"))
        except Exception as e:  # noqa: BLE001 — never the poster's end; nothing is retried by itself
            db.log(iid, "vinted_repost_failed", {"step": step, "error": f"{type(e).__name__}: {e}"[:300]})
            notify.say(f"❌ Vinted repost ({step}) of {title}: {type(e).__name__}: {e}")
        done.append(iid)
    return done


async def go(db: DB, poster, iid: str, row, title: str) -> bool:
    """Checked again, then deleted and put back in line. True when the row was put back."""
    text = confirm_text(db)
    if not text:
        notify.say(f"♻️ Vinted repost of {title}: no recorded delete dialog yet — run --practice first")
        return False
    info = await poster.vinted_interest(row["url"])
    db.log(iid, "vinted_interest", info)
    if (stay := interested(info)) is not None:
        notify.say(f"♻️ Vinted · {title} stays as it is: {stay}")
        return False
    before = {"url": row["url"], "listing_id": row["listing_id"], "posted_at": row["posted_at"],
              "price": row["price"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    db.log(iid, "vinted_repost_delete", {"url": row["url"], "confirm": text})
    await poster.vinted_delete(row["url"], text)      # raises unless the listing is gone from Vinted's data
    # kept only now (live 2026-11-01: kept before a delete that never happened, the sweep took the OLD listing for
    # the new one); then the row back in line, in one go
    db.kv_set(BEFORE + iid, json.dumps(before))
    db.upsert_listing(iid, "vinted", status="queued", url=None, listing_id=None, attempts=0, error=None)
    db.log(iid, "vinted_repost_queued", before)
    return True


def after_posted(s, db: DB, iid: str, mp: str) -> bool:
    """From crosslist.record when a Vinted row is posted: a repost's listing keeps its original posted_at (the price-drop
    clock), its fields name the new time and the old address, the ✅ card is edited, ONE private line. True if it was."""
    if mp != "vinted":
        return False
    before = loads(db.kv_get(BEFORE + iid))
    if not before:
        return False
    row = db.listing(iid, "vinted")
    if row is None or row["status"] != "posted" or not row["url"] or row["url"] == before.get("url"):
        return False                                   # not posted again yet (the old listing is never the new one)
    fields = loads(row["fields_json"]) or {}
    fields.update({"reposted_at": row["posted_at"], "reposted_from": before.get("url")})
    db.upsert_listing(iid, "vinted", posted_at=before.get("posted_at") or row["posted_at"], fields_json=fields)
    db.kv_set(BEFORE + iid, "")
    db.log(iid, "vinted_reposted", {"old": before.get("url"), "new": row["url"], "brand": fields.get("brand"),
                                    "posted_at_kept": before.get("posted_at")})
    from thrift_agent import crosslist
    try:
        crosslist.refresh_card(s, db, iid)
    except Exception as e:  # noqa: BLE001 — the card is cosmetic; the record is what counts
        db.log(iid, "card_edit", {"error": f"{type(e).__name__}: {e}"[:200]})
    notify.say(f"♻️ {_title(db, iid)} is on Vinted again with its brand {fields.get('brand')}: {row['url']} "
               f"(was {before.get('url')}); listed since {str(before.get('posted_at'))[:10]} for its price drops.")
    return True
