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

KINDS = ("category", "photos", "text")   # WO43: a category change; WO45 §6: photos added; WO50: a title / description
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


TEXT_SAID = "text_edit_said"                       # event: the text edits already told, by row id


def _say_done(db: DB, row, title: str, what: str, label: str, already: bool = False) -> None:
    """The owner hears a change once the site shows it. A text edit is told once per ITEM (WO50, the owner: one line
    per item), when its last site is done — "✓ <name>: new title on Poshmark, Depop and Vinted — “…”" — never once
    per site; a site that failed for good has said so itself."""
    if row["kind"] != "text":
        notify.say(f"✓ {title} {'already shows' if already else 'is now'} {what} on {label}")
        return
    iid = row["item_id"]
    if db.conn.execute("SELECT 1 FROM listing_edits WHERE item_id=? AND kind='text' AND dry=0 AND status IN "
                       "('pending','saving','unconfirmed') LIMIT 1", (iid,)).fetchone():
        return                                         # another site is still on its way: one line once all are done
    said: set[str] = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind=?", (iid, TEXT_SAID)):
        said |= set((loads(detail) or {}).get("rows") or [])
    done = [r for r in db.conn.execute("SELECT * FROM listing_edits WHERE item_id=? AND kind='text' AND dry=0 "
                                       "AND status='done' ORDER BY done_at", (iid,)) if r["id"] not in said]
    if not done:
        return
    order = ["poshmark", "depop", "vinted"]
    done.sort(key=lambda r: order.index(r["marketplace"]) if r["marketplace"] in order else len(order))
    labels = [crosslist.LABEL.get(r["marketplace"], r["marketplace"]) for r in done]
    where = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
    loaded = [loads(r["payload"]) or {} for r in done]
    new = next((p.get("title") or p.get("first_line") for p in loaded if p.get("title") or p.get("first_line")), None)
    desc = any(p.get("description") is not None and not p.get("first_line") for p in loaded)
    change = (f"new title on {where} — “{new}”" if new and not desc
              else f"new title and description on {where} — “{new}”" if new
              else f"new description on {where}")
    notify.say(f"✓ {crosslist.item_name(db, iid)}: {change}")
    db.log(iid, TEXT_SAID, {"rows": [r["id"] for r in done]})


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
    what = (f"{payload.get('add')} more photo{'s' if payload.get('add') != 1 else ''}" if row["kind"] == "photos"
            else f"titled “{payload.get('title') or payload.get('first_line')}”"
            if row["kind"] == "text" and (payload.get("title") or payload.get("first_line"))
            else "with its new description" if row["kind"] == "text"
            else path_text(payload.get("path") or payload.get("want") or {}) or row["kind"])
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    if row["kind"] == "photos" and not all(Path(f).is_file() for f in payload.get("files") or []):
        _skip(db, row, "the photos to add are gone from the Mac")
        return True
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
            _say_done(db, row, title, what, label)
        else:
            _failed(db, row, "saved, but the site doesn't show the change", title)
        return True

    def go() -> bool:
        _update(db, row["id"], status="saving")
        return True

    try:
        if row["kind"] == "text":                               # WO50: the title / description, nothing else
            if site == "poshmark":
                out = await poster.set_text(ctx, url, payload, dry=dry, shots=shots, go=go)
            else:
                out = await poster.set_text(url, payload, dry=dry, shots=shots, go=go, sku=iid,
                                            fields=loads(listing["fields_json"]) or {})
            if out.get("mismatch") is not None:
                _skip(db, row, f"the site shows another text than we posted (changed by hand?): {out['mismatch']}")
                notify.say(f"✏️ {title} on {label}: the listing shows another title than we posted — changed by hand? "
                           "Left as it is.")
                return True
        elif row["kind"] == "photos":
            if site == "poshmark":
                out = await poster.set_photos(ctx, url, payload, dry=dry, shots=shots, go=go)
            else:
                out = await poster.add_photos(url, payload.get("files") or [], dry=dry, shots=shots, go=go, sku=iid,
                                              had=int(payload.get("had") or 0))
        elif site == "poshmark":
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
        # the event log keeps what the page showed — the private chat may be out of reach (live 2026-10-24)
        db.log(iid, "listing_edit_dry", {"site": site, "kind": row["kind"], "ok": bool(out.get("ok")),
                                         "error": out.get("error"), "mismatch": out.get("mismatch"),
                                         "seen": out.get("seen"), "evidence": Path(evidence).name if evidence else None})
        kind = {"photos": "Photos", "text": "Title"}.get(row["kind"], "Category")
        notify.say(f"{kind} dry run on {label} — {title} → {what}: "
                   + ("filled in on the edit page, not saved" if out.get("ok") else f"not filled: {out.get('error')}")
                   + (f" · {Path(evidence).name}" if evidence else "")
                   + (f" · seen: {out.get('seen')}" if out.get("seen") else ""))
        return True
    if not out.get("saved"):
        _failed(db, _fresh(db, row["id"]), out.get("error") or "not saved", title)
        return True
    if out.get("ok"):
        _done(db, row, f"{what}: {out.get('note') or 'the site shows it'}", evidence)
        if out.get("after") is not None:                    # WO45: the count the edit page showed after Save
            payload = {**payload, "live": out["after"]}
        _mirror(db, iid, site, payload)
        _say_done(db, row, title, what, label, already=bool(out.get("already")))
        return True
    _update(db, row["id"], status="unconfirmed", evidence=evidence, attempts=int(row["attempts"] or 0) + 1,
            error=out.get("error") or "saved; the site doesn't show it yet")
    if int(row["attempts"] or 0) + 1 >= MAX_ATTEMPTS:
        _failed(db, _fresh(db, row["id"]), out.get("error") or "saved, but the site doesn't show the change", title)
    return True


async def _shows(site: str, poster, url: str, payload: dict, iid: str, shots: Path) -> bool:
    """Does the site show the change now? Poshmark: its public listing's JSON; Depop / Vinted: the edit page read
    (a read-only "fields" job, or the photo count for a "photos" edit)."""
    import asyncio
    want = payload.get("want") or {}
    if "title" in payload or "description" in payload:            # WO50: the title / description
        if site == "poshmark":
            live = await asyncio.to_thread(crosslist.poshmark_listing, url) or {}
            return bool(payload.get("title") is None or " ".join(str(live.get("title") or "").split())
                        == " ".join(str(payload["title"]).split()))
        from thrift_agent.post.ext_driver import _text_shows
        return _text_shows(await poster.read_text(url, shots, iid), payload)
    if "files" in payload:                                        # WO45 §6: the photo count
        need = int(payload.get("had") or 0) + int(payload.get("add") or 0)
        if site == "poshmark":
            live = await asyncio.to_thread(crosslist.poshmark_listing, url)
            return bool(live and live.get("photos") is not None and int(live["photos"]) >= need)
        out = await poster.read_photos(url, shots, iid)
        return bool(out.get("count") is not None and int(out["count"]) >= need)
    if site == "poshmark":
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
    if "files" in payload:                                        # WO45 §6: the photos the site now has
        had = int(payload.get("had") or 0)
        live = int(payload["live"]) if payload.get("live") is not None else had + len(payload["files"])
        if isinstance(fields.get("photos"), list):                # Depop's / Vinted's mapped list grows with them
            fields["photos"] = list(fields["photos"]) + list(payload["files"])
            fields["photos"] = fields["photos"][:max(live, len(fields["photos"]) - len(payload["files"]))]
        db.upsert_listing(iid, site, fields_json=fields, photos_live=live, photos_read_at=_iso(_now()))
        return
    if "title" in payload or "description" in payload:            # WO50: the text the site now shows
        if site == "poshmark":
            it = db.item(iid)
            renders = (loads(it["renders"]) or {}) if it is not None else {}
            posh = renders.get("poshmark") or {}
            if payload.get("title") is not None:
                posh["title"] = payload["title"]
            if payload.get("description") is not None:
                posh["description"] = payload["description"]
            if posh:
                renders["poshmark"] = posh
                db.set_item(iid, renders=renders)
        else:
            if payload.get("title") is not None and site == "vinted":
                fields["title"] = payload["title"]
            if payload.get("description") is not None:
                fields["description"] = payload["description"]
            db.upsert_listing(iid, site, fields_json=fields)
        return
    if site == "poshmark":
        fields.update({k: v for k, v in (payload.get("want") or {}).items() if k != "size"})
    else:
        fields.update(payload.get("fields") or {})
    db.upsert_listing(iid, site, fields_json=fields)


def queue_photos(s: Settings, db: DB, iid: str, site: str, files: list[str], had: int, dry: bool = False) -> list[str]:
    """WO45 §6: one "photos" edit for a live listing that shows fewer photos than its item has — `files` are the
    item's prepared photos it lacks (in the listing's order), `had` what the site shows now."""
    limit = int((s["marketplaces"].get(site) or {}).get("max_photos", 16))
    files = [f for f in files if Path(f).is_file()][:max(0, limit - had)]
    if not files:
        return []
    return queue(db, iid, "photos", {site: {"files": files, "had": had, "add": len(files), "limit": limit}}, dry=dry)


def queue_text(db: DB, iid: str, title: str | None = None, sites: list[str] | None = None,
               descriptions: dict[str, str] | None = None, dry: bool = False) -> list[str]:
    """WO50: one "text" edit per site the item is live on — the new title (Poshmark and Vinted: their title; Depop:
    its description's first line, the rest kept) and/or a new description per site (`descriptions`). Each carries the
    text the site should show now (`expect_title` / `expect_first_line`, and `expect_description` wherever a description
    is typed — on Depop always, its whole description is typed again): a listing she changed by hand is left as it is
    (`run_one`). Returns the rows made."""
    it = db.item(iid)
    posh = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it is not None else {}
    payloads: dict[str, dict] = {}
    for row in db.listings_for(iid):
        site = row["marketplace"]
        if row["status"] != "posted" or not row["url"] or (sites and site not in sites):
            continue
        fields = loads(row["fields_json"]) or {}
        desc = (descriptions or {}).get(site)
        if site == "poshmark":
            p = {"expect_title": posh.get("title")}
            if title:
                p["title"] = title
            if desc is not None:
                p["description"] = desc
                p["expect_description"] = posh.get("description")    # her own wording on the site is never overwritten
        elif site == "vinted":
            p = {"expect_title": fields.get("title")}
            if title:
                p["title"] = title
            if desc is not None:
                p["description"] = desc
                p["expect_description"] = fields.get("description")
        else:                                                     # Depop: the first line is the title
            body = desc if desc is not None else str(fields.get("description") or "")
            old_first = str(fields.get("description") or "").split("\n", 1)[0]
            first, sep, rest = body.partition("\n")
            p = {"expect_first_line": old_first, "description": f"{title}{sep}{rest}" if title else body,
                 "expect_description": str(fields.get("description") or "")}
            if title:
                p["first_line"] = title                           # the title, for the lines (Depop has no field)
        if p.get("title") is not None or p.get("description") is not None:
            payloads[site] = p
    return queue(db, iid, "text", payloads, dry=dry)


def listing_text(db: DB) -> list[str]:
    out = []
    for r in db.conn.execute("SELECT * FROM listing_edits ORDER BY created_at DESC LIMIT 30"):
        out.append(f"{r['item_id']} {r['marketplace']} {r['kind']}: {r['status']}"
                   + (f" — {r['error']}" if r["error"] else "") + (" (dry)" if r["dry"] else ""))
    return out
