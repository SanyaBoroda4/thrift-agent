"""WO43: edits to live listings, made by the running poster — a listing's department and category (live 2026-10-17:
the On Cloudsurfer Max sneakers went up under Women › Shoes › Sneakers on all three sites; they are men's).

One row per item and site in `listing_edits` (kind `category`; pending → saving → done | unconfirmed | failed |
skipped; `dry` rows fill the edit page and never save). Each site worker takes its due row on an idle turn, before a
price drop (`parallel.price_drop`): Poshmark through `PoshmarkPoster.set_category` (the edit page's category menu —
department, category, subcategory — and the size menu for the new department, read back, Update → the Share Listing
panel's List This Item once (WO40), then the public page's own JSON until it shows the new department); Depop and
Vinted through the extension's "fields" job (`ExtensionPoster.set_fields`: the category chosen under the new
department's heading and the size, read back, Save once, then the edit page read again for what stuck). One Save per
site; a failure is tried again an hour later, three in all, then ONE private line. The item's own record (facts,
renders, the listings' mapped fields) follows the owner's decision the moment it is queued.
`thrift recategorize <item> "Men > Shoes > Sneakers" [--dry-run | --now]`."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import crosslist, notify
from thrift_agent.brain import sizes, taxonomy
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads, new_id
from thrift_agent.schema import Facts, Render

KINDS = ("category",)
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=1)
OPEN = ("pending", "saving", "unconfirmed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _title(db: DB, iid: str) -> str:
    it = db.item(iid)
    posh = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    return posh.get("title") or iid


def path_text(path: dict) -> str:
    return " › ".join(x for x in (path.get("department"), path.get("category"), path.get("subcategory")) if x)


# ---------------------------------------------------------------- the rows

def queue(db: DB, iid: str, kind: str, payloads: dict[str, dict], dry: bool = False) -> list[str]:
    """One row per site; a site with an open row of the same kind gets none (never two edits in flight)."""
    out = []
    for site, payload in payloads.items():
        if db.conn.execute("SELECT 1 FROM listing_edits WHERE item_id=? AND marketplace=? AND kind=? AND status IN "
                           "('pending', 'saving', 'unconfirmed')", (iid, site, kind)).fetchone():
            continue
        rid = new_id("e")
        now = _iso(_now())
        db.conn.execute("INSERT INTO listing_edits (id, item_id, marketplace, kind, payload, status, attempts, dry, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, iid, site, kind, json.dumps(payload), "pending", 0, int(dry), now, now))
        out.append(rid)
    db.conn.commit()
    return out


def due(db: DB, site: str, now: datetime | None = None) -> list:
    """The site's rows to work on: pending ones (a failed attempt waits RETRY_AFTER), and unconfirmed ones — saved
    once, the site not yet showing it — after RETRY_AFTER too, and those are only READ (`run_one`), never saved again
    blind (live 2026-10-18: Vinted's refusal was followed by a second Save within the minute)."""
    now = now or _now()
    rows = db.conn.execute("SELECT * FROM listing_edits WHERE marketplace=? AND status IN ('pending', 'unconfirmed') "
                           "ORDER BY created_at", (site,)).fetchall()
    return [r for r in rows if _retry_ok(r, now)]


def _retry_ok(row, now: datetime) -> bool:
    if not int(row["attempts"] or 0):
        return True
    try:
        return now - datetime.fromisoformat(row["updated_at"]) >= RETRY_AFTER
    except ValueError:
        return True


def _update(db: DB, rid: str, **values) -> None:
    values["updated_at"] = _iso(_now())
    cols = ", ".join(f"{k}=?" for k in values)
    db.conn.execute(f"UPDATE listing_edits SET {cols} WHERE id=?", (*values.values(), rid))
    db.conn.commit()


def _fresh(db: DB, rid: str):
    return db.conn.execute("SELECT * FROM listing_edits WHERE id=?", (rid,)).fetchone()


def _failed(db: DB, row, why: str, title: str) -> None:
    attempts = int(row["attempts"] or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        _update(db, row["id"], status="failed", attempts=attempts, error=why, done_at=_iso(_now()))
        notify.say(f"⚠️ Couldn't change {title} on {crosslist.LABEL.get(row['marketplace'], row['marketplace'])} "
                   f"after {attempts} tries: {why}. Please change it by hand.")
    else:
        _update(db, row["id"], status="pending", attempts=attempts, error=why)
    db.log(row["item_id"], "listing_edit_failed", {"site": row["marketplace"], "why": why, "attempts": attempts})


def _skip(db: DB, row, why: str) -> None:
    _update(db, row["id"], status="skipped", error=why, done_at=_iso(_now()))
    db.log(row["item_id"], "listing_edit_skipped", {"site": row["marketplace"], "why": why})


def _done(db: DB, row, note: str, evidence: str | None = None) -> None:
    _update(db, row["id"], status="done", error=None, evidence=evidence, done_at=_iso(_now()))
    db.log(row["item_id"], "listing_edited", {"site": row["marketplace"], "kind": row["kind"], "note": note})


# ---------------------------------------------------------------- a department / category change

def recategorize(s: Settings, db: DB, iid: str, path: dict, dry: bool = False) -> dict:
    """The owner's department / category for a listed item: the item's record follows at once (facts, renders — the
    size on the new department's menu —, the sites' mapped fields), and one edit row per site the item is posted on.
    {"placed", "sites", "skipped": {site: why}, "rows"}."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    facts = Facts.model_validate(loads(it["facts"]))
    placed = taxonomy.place(facts, path.get("department"), path.get("category") or facts.category,
                            path.get("subcategory"))
    if placed is None:
        raise ValueError(f"Poshmark has no category {path_text(path)!r}")
    facts2 = facts.model_copy(update={"department": placed["department"], "category": placed["category"],
                                      "subcategory": placed["subcategory"], "category_confidence": 1.0,
                                      "category_alternatives": [], "department_confidence": 1.0})
    tab, value = sizes.poshmark_size(facts2) or (None, None)
    renders = loads(it["renders"]) or {}
    renders2 = {mp: {**r, "department": placed["department"], "category": placed["category"],
                     "subcategory": placed["subcategory"], "size_tab": tab, "size_value": value}
                for mp, r in renders.items()}
    posted = {r["marketplace"]: r for r in db.listings_for(iid) if r["status"] == "posted" and r["url"]}
    payloads, skipped = {}, {}
    if "poshmark" in posted:
        payloads["poshmark"] = {"render": renders2.get("poshmark") or next(iter(renders2.values())),
                                "want": {**placed, "size": value or (renders2.get("poshmark") or {}).get("size")}}
    cross = [mp for mp in ("depop", "vinted") if mp in posted]
    if cross:
        try:
            from thrift_agent.catalogs import depop as depop_cat, vinted as vinted_cat
            from thrift_agent.catalogs.common import ItemView, MappingError
            posh = renders2.get("poshmark") or next(iter(renders2.values()))
            view = ItemView(iid=iid, facts=facts2, render=Render.model_validate(posh),
                            price=int(it["owner_price"] or posh.get("price") or 0))
        except Exception as e:  # noqa: BLE001
            for mp in cross:
                skipped[mp] = f"{type(e).__name__}: {e}"
            cross = []
        for mp in cross:
            try:
                # the fields a category change empties on the edit page come along (live 2026-10-18: Vinted cleared
                # the condition and the package size with the category; Depop's edit page can come up blank)
                if mp == "depop":
                    f = depop_cat.map_depop(view)
                    payloads[mp] = {"fields": {"category": f.category, "size": f.size, "condition": f.condition},
                                    "want": {"category": f.category, "size": f.size}, "path": placed}
                else:
                    f = vinted_cat.map_vinted(view)
                    payloads[mp] = {"fields": {"category_id": f.category_id, "category_path": f.category_path,
                                               "size": f.size, "condition": f.condition,
                                               "package_sizes": list(f.package_sizes)},
                                    "want": {"category_path": f.category_path, "size": f.size}, "path": placed}
            except MappingError as e:
                skipped[mp] = str(e)
            except Exception as e:  # noqa: BLE001
                skipped[mp] = f"{type(e).__name__}: {e}"
    if not dry:
        with db.tx():
            db.set_item(iid, facts=facts2.model_dump(), renders=renders2, owner_category=placed)
            db.log(iid, "recategorized", {"path": placed, "sites": list(payloads), "skipped": skipped})
    rows = queue(db, iid, "category", payloads, dry=dry)
    return {"placed": placed, "sites": list(payloads), "skipped": skipped, "rows": rows}


def may_edit(s: Settings, db: DB, site: str, dry_poster: bool = False) -> bool:
    """The poster LIVE, never with PAUSE, the lid closed or the site stopped for the window — in any hour (an edit the
    owner asked for is made now, like a take-down; it lists nothing new)."""
    from thrift_agent import power
    from thrift_agent.post import runner
    if dry_poster or s.flag_set("PAUSE") or runner._paused(db):
        return False
    return not (power.lid_closed() is True or crosslist.blocked(db, site))


async def run_one(s: Settings, db: DB, site: str, poster, ctx, sites: list[str], dry_poster: bool = False) -> bool:
    """The site's next due edit, made by its worker on an idle turn (`may_edit`). True when a row was worked on."""
    rows = due(db, site)
    if not rows:
        return False
    row = rows[0]
    if not may_edit(s, db, site, dry_poster):
        return False
    iid, title = row["item_id"], _title(db, row["item_id"])
    payload = loads(row["payload"]) or {}
    listing = db.listing(iid, site)
    if listing is None or listing["status"] != "posted" or not listing["url"]:
        _skip(db, row, "not live on this site")
        return True
    url = listing["url"]
    dry = bool(row["dry"])
    label = crosslist.LABEL.get(site, site)
    what = path_text(payload.get("path") or payload.get("want") or {}) or row["kind"]
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    from thrift_agent.post.base import AccountBlocked
    if row["status"] == "unconfirmed":                 # saved once already: the site is READ, never saved again blind
        try:
            shown = await _shows(site, poster, url, payload, iid, shots)
        except AccountBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            shown = False
            notify.say(f"❌ {label}: couldn't read {title} after the change: {type(e).__name__}: {e}")
        if shown:
            _done(db, row, f"{what}: the site shows it now", row["evidence"])
            _mirror(db, iid, site, payload)
            notify.say(f"✓ {title} is now {what} on {label}")
        else:
            _failed(db, row, "saved, but the site doesn't show the change", title)
        return True

    def go() -> bool:
        _update(db, row["id"], status="saving")
        return True

    try:
        if site == "poshmark":
            out = await poster.set_category(ctx, url, payload, dry=dry, shots=shots, go=go)
        else:
            out = await poster.set_fields(url, payload.get("fields") or {}, dry=dry, shots=shots, go=go, sku=iid)
    except AccountBlocked:
        if _fresh(db, row["id"])["status"] == "saving":
            _update(db, row["id"], status="unconfirmed")
        raise
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "saved": _fresh(db, row["id"])["status"] == "saving", "error": f"{type(e).__name__}: {e}"}
    evidence = str(out.get("shot") or "")
    if dry:
        _update(db, row["id"], status="done" if out.get("ok") else "failed", done_at=_iso(_now()),
                error=out.get("error"), evidence=evidence)
        notify.say(f"Category dry run on {label} — {title} → {what}: "
                   + ("filled in on the edit page, not saved" if out.get("ok") else f"not filled: {out.get('error')}")
                   + (f" · {Path(evidence).name}" if evidence else "")
                   + (f" · seen: {out.get('seen')}" if out.get("seen") else ""))
        return True
    if not out.get("saved"):
        _failed(db, _fresh(db, row["id"]), out.get("error") or "not saved", title)
        return True
    if out.get("ok"):
        _done(db, row, f"{what}: {out.get('note') or 'the site shows it'}", evidence)
        _mirror(db, iid, site, payload)
        notify.say(f"✓ {title} is now {what} on {label}")
        return True
    _update(db, row["id"], status="unconfirmed", evidence=evidence, attempts=int(row["attempts"] or 0) + 1,
            error=out.get("error") or "saved; the site doesn't show it yet")
    if int(row["attempts"] or 0) + 1 >= MAX_ATTEMPTS:
        _failed(db, _fresh(db, row["id"]), out.get("error") or "saved, but the site doesn't show the change", title)
    return True


async def _shows(site: str, poster, url: str, payload: dict, iid: str, shots: Path) -> bool:
    """Does the site show the change now? Poshmark: its public listing's JSON; Depop / Vinted: the edit page read
    (a read-only "fields" job)."""
    want = payload.get("want") or {}
    if site == "poshmark":
        import asyncio
        live = await asyncio.to_thread(crosslist.poshmark_listing, url)
        return bool(live and (live.get("department") or "").lower() == str(want.get("department") or "").lower()
                    and (live.get("category") or "").lower() == str(want.get("category") or "").lower())
    out = await poster.read_fields(url, payload.get("fields") or {}, shots, iid)
    return bool(out.get("ok"))


def _mirror(db: DB, iid: str, site: str, payload: dict) -> None:
    """The listing's mapped fields follow the change made on the site."""
    listing = db.listing(iid, site)
    if listing is None:
        return
    fields = loads(listing["fields_json"]) or {}
    if site == "poshmark":
        fields.update({k: v for k, v in (payload.get("want") or {}).items() if k != "size"})
    else:
        fields.update(payload.get("fields") or {})
    db.upsert_listing(iid, site, fields_json=fields)


def listing_text(db: DB) -> list[str]:
    out = []
    for r in db.conn.execute("SELECT * FROM listing_edits ORDER BY created_at DESC LIMIT 30"):
        out.append(f"{r['item_id']} {r['marketplace']} {r['kind']}: {r['status']}"
                   + (f" — {r['error']}" if r["error"] else "") + (" (dry)" if r["dry"] else ""))
    return out
