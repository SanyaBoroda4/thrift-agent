"""Orchestration: inbox folder → batch → items → facts/price/copy/gate → ready for the poster."""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import imagehash
from PIL import Image

from thrift_agent import notify
from thrift_agent.brain import copy as copywriter
from thrift_agent.brain.extract import extract, strip_screenshot_evidence
from thrift_agent.brain.gate import GateResult, evaluate
from thrift_agent.brain.price import price
from thrift_agent.brain.verify import lint, verify
from thrift_agent.config import Settings, load_yaml
from thrift_agent.db import DB, loads
from thrift_agent.ingest import prep, segment as seg
from thrift_agent.schema import CopyOut, Facts, PriceResult, Render

MAX_SEGMENT_PHOTOS = 90        # the Messages API takes at most 100 image blocks per request; keep headroom
ANSWERABLE = ("needs_info", "ready", "failed", "new", "awaiting_price", "needs_owner")   # a note resets these to 'new'
REQUEUEABLE = ("failed", "dryrun")                      # post statuses `thrift requeue` may send back to the queue
PRICEABLE = ("awaiting_price", "needs_info", "ready", "needs_owner")   # item statuses an owner price may be set on
MANIFEST = "photos.json"                                # per item: which photos are the seller's own vs retail screenshots
NOT_A_DUPLICATE = re.compile(r"different item|not a duplicate", re.I)   # seller's reply that clears the re-share hold

# ---------- inbox ----------


def ready_folders(s: Settings) -> list[Path]:
    """Folders the Shortcut finished writing: marker present, nothing still downloading, quiet for a bit."""
    inbox, marker, settle = s.path("inbox"), s["inbox"]["done_marker"], s["inbox"]["settle_seconds"]
    if not inbox.exists():
        return []
    out = []
    for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        pending = prep.icloud_placeholders(d)
        if pending:
            if platform.system() == "Darwin":
                subprocess.run(["brctl", "download", str(d)], check=False, capture_output=True)
            continue
        if not any(f.name == marker or f.stem == marker for f in d.iterdir()):   # "_done" or "_done.txt"
            continue
        newest = max((f.stat().st_mtime for f in d.iterdir()), default=0)
        if time.time() - newest >= settle:
            out.append(d)
    return out


def register(s: Settings, db: DB, folder: Path) -> str | None:
    photos = [p for p in folder.iterdir() if prep.is_photo(p)]
    bid = db.add_batch(str(folder), len(photos))
    if bid is None:                                   # already known (src_dir is UNIQUE)
        return None
    if not photos:
        # A share of only .dng/.mov/_done must still be recorded, pinged and archived; otherwise it sits in the
        # inbox and is re-scanned every tick forever, and the seller never learns why nothing was listed.
        db.set_batch(bid, status="failed", reasons=["no usable photos"])
        db.log(bid, "batch_failed", {"folder": folder.name, "reason": "no usable photos"})
        notify.say(f"batch {folder.name}: no usable photos (jpg/jpeg/png/heic/heif/webp)")
        archive_share(s, db, bid, folder)
        return None
    db.log(bid, "batch_registered", {"folder": folder.name, "photos": len(photos)})
    return bid


# ---------- batch → items ----------


def process_batch(s: Settings, db: DB, bid: str) -> None:
    b = db.batch(bid)
    src = Path(b["src_dir"])
    work = s.path("work") / bid
    listed = prep.list_photos(src)
    if not listed:
        raise ValueError(f"no photos in {src} (moved away, or still downloading from iCloud?)")
    raw = [p for p, _ in listed]
    times = {p.name: t for p, t in listed}
    kinds_raw = prep.photo_kinds(raw)                     # from the originals: normalize() strips the EXIF
    norm = [prep.normalize(p, work / "all" / f"{i:03d}.jpg", s["images"]["work_long_edge"]) for i, p in enumerate(raw)]
    norm_times = [times[p.name] for p in raw]
    kept, dropped = prep.drop_near_duplicates(norm, norm_times, s["images"]["dedupe_hamming"])
    kept_times = [times[raw[int(p.stem)].name] for p in kept]
    kept_kinds = [kinds_raw[int(p.stem)] for p in kept]

    note_file = src / "notes.txt"
    note = note_file.read_text(encoding="utf-8").strip() if note_file.exists() else None

    if len(kept) > MAX_SEGMENT_PHOTOS:
        raise ValueError(f"batch has {len(kept)} photos after dedupe; the model takes at most {MAX_SEGMENT_PHOTOS} "
                         "— share it in smaller sets")
    if len(kept) == 1:
        groups, reasons, summaries, unassigned = [[0]], [], ["single photo"], []
    else:
        out = seg.segment(list(zip(kept, kept_times)), s["models"]["segment"], s["images"]["thumb_long_edge"],
                          kinds=kept_kinds)
        groups = [g.photos for g in out.groups]
        summaries = [g.summary for g in out.groups]
        unassigned = list(out.unassigned)                 # screenshots the model could not match to an item
        reasons = seg.check(out, len(kept), s["segmentation"]["min_confidence"], kinds=kept_kinds)

    sheet = seg.contact_sheet(kept, groups, work / "contact_sheet.png", kinds=kept_kinds)
    db.set_batch(bid, segmentation={"groups": groups, "summaries": summaries, "photos": [str(p) for p in kept],
                                    "kinds": kept_kinds, "unassigned": unassigned,
                                    "dropped": [str(p) for p in dropped], "note": note},
                 reasons=reasons)

    if reasons or s["segmentation"]["always_confirm"]:
        db.set_batch(bid, status="needs_confirm")
        lines = [f"item {k}: {summaries[k - 1]} — photos {g}" for k, g in enumerate(groups, 1)]
        why = ("\n⚠️ " + "; ".join(reasons)) if reasons else ""
        notify.photo(sheet, f"Batch {bid}: {len(kept)} photos → {len(groups)} items\n" + "\n".join(lines) + why +
                     f"\nReply: thrift confirm {bid} ok   (or 12>2, split 7, merge 2 3, drop 7)")
        return
    split(s, db, bid, groups)


def confirm(s: Settings, db: DB, bid: str, cmd: str) -> None:
    b = db.batch(bid)
    if b is None or b["status"] != "needs_confirm":
        raise ValueError(f"batch {bid} isn't waiting for confirmation")
    segd = loads(b["segmentation"])
    n = len(segd["photos"])
    rest, drops = seg.parse_drops(cmd)                    # "drop 7": a screenshot (or a bad shot) that belongs nowhere
    if bad := [i for i in drops if not 0 <= i < n]:
        raise ValueError(f"can't drop {bad}: photos are 0..{n - 1}")
    groups = [[i for i in g if i not in drops] for g in segd["groups"]]
    groups = seg.apply_correction(groups, rest, n=n)
    split(s, db, bid, groups, dropped=drops)


def partition_problems(groups: list[list[int]], n: int, dropped: list[int] | tuple[int, ...] = ()) -> list[str]:
    """Why `groups` is not a partition of range(n) minus `dropped`, in the seller's words (empty list = it is one)."""
    problems = []
    seen = [i for g in groups for i in g]
    if missing := sorted(set(range(n)) - set(seen) - set(dropped)):
        m = missing[0]
        problems.append(f"photos {missing} are in no item — reply e.g. '{m}>{max(len(groups), 1)}' "
                        f"(add photo {m} to item {max(len(groups), 1)}), 'split {m}' (its own item) or 'drop {m}'")
    if dupes := sorted({i for i in seen if seen.count(i) > 1}):
        problems.append(f"photos {dupes} appear twice")
    if unknown := sorted(set(seen) - set(range(n))):
        problems.append(f"photos {unknown} don't exist (photos are 0..{n - 1})")
    problems.extend(f"item {k} is empty" for k, g in enumerate(groups, 1) if not g)
    return problems


def split(s: Settings, db: DB, bid: str, groups: list[list[int]], dropped: list[int] | tuple[int, ...] = ()) -> None:
    """Turn a confirmed grouping into item rows. All-or-nothing: the disk copies happen first, then the rows,
    the log and the batch status commit in one transaction. A crash half-way through must not leave orphan
    'new' items that the worker lists while a re-confirm creates the same garment again.

    Inside each item the seller's own photos come first (capture order) and retail screenshots last; the
    item's photos.json manifest records which is which for extraction and rendering."""
    b = db.batch(bid)
    segd = loads(b["segmentation"])
    photos = [Path(p) for p in segd["photos"]]
    kinds = segd.get("kinds") or ["own"] * len(photos)
    if problems := partition_problems(groups, len(photos), dropped):
        raise ValueError(f"batch {bid}: " + "; ".join(problems))
    if db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid,)).fetchone()[0]:
        raise ValueError(f"batch {bid} already has items — it was split before")
    note = segd.get("note")
    item_note = note if len(groups) == 1 else None      # a batch note is only unambiguous for one item

    dirs = []
    for k, g in enumerate(groups, 1):
        d = s.path("work") / bid / f"item_{k:02d}"
        if (d / "photos").exists():                      # a previous, failed attempt: never keep its extra photos
            shutil.rmtree(d / "photos")
        (d / "photos").mkdir(parents=True)
        ordered = [i for i in g if kinds[i] != "retail"] + [i for i in g if kinds[i] == "retail"]
        for j, idx in enumerate(ordered):
            shutil.copy2(photos[idx], d / "photos" / f"{j:02d}.jpg")
        (d / MANIFEST).write_text(json.dumps([{"file": f"{j:02d}.jpg", "kind": kinds[idx], "src": idx}
                                              for j, idx in enumerate(ordered)], indent=1), encoding="utf-8")
        dirs.append(d)

    with db.tx():
        iids = [db.add_item(bid, k, str(d), item_note) for k, d in enumerate(dirs, 1)]
        for iid, g in zip(iids, groups):
            db.log(iid, "item_created", {"batch": bid, "photos": g})
        if dropped:
            db.log(bid, "photos_dropped", {"photos": list(dropped)})
        db.set_batch(bid, status="split")

    if note and len(groups) > 1:
        notify.say(f"⚠️ Batch {bid}: the note \"{note}\" was not applied — it can't be matched to one of the "
                   f"{len(groups)} items ({', '.join(iids)}). Re-apply it with: thrift answer <item> \"{note}\"")
    archive_share(s, db, bid, Path(b["src_dir"]))


def archive_share(s: Settings, db: DB, bid: str, src: Path) -> Path | None:
    """Move a finished share out of the inbox: this is what clears the phone's iCloud folder.

    Only folders inside paths.inbox are touched: `thrift process <any dir>` must never move the caller's folder.
    Never fatal: the items already exist in the DB, and a folder left behind is skipped by register() because
    src_dir is UNIQUE. On the Mac this is a cross-volume move (iCloud Drive to local disk), i.e. copy + delete."""
    inbox = s.path("inbox").resolve()
    if not src.exists() or inbox not in src.resolve().parents:
        return None
    base = s.path("archive") / f"{datetime.now():%Y%m%d}_{src.name}"
    dest, n = base, 1
    while dest.exists():                                         # never merge into an earlier archive folder
        n += 1
        dest = base.with_name(f"{base.name}_{n}")
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        db.log(bid, "archive_failed", {"src": str(src), "dest": str(dest), "error": str(e)})
        notify.say(f"\u26a0\ufe0f {bid}: could not archive {src.name} ({e}). The items are safe; move the folder by hand.")
        return None
    db.log(bid, "archived", {"dest": str(dest)})
    return dest


# ---------- item → ready ----------


def photo_kinds_of(d: Path, photos: list[Path]) -> list[str]:
    """'own' | 'retail' per photo, from the item's manifest (items split before the manifest existed are all own)."""
    manifest = d / MANIFEST
    if not manifest.exists():
        return ["own"] * len(photos)
    by_file = {e["file"]: e["kind"] for e in json.loads(manifest.read_text(encoding="utf-8"))}
    return [by_file.get(p.name, "own") for p in photos]


def _dollars(value: str | None) -> int | None:
    """'$128.00' / '128' / '1,250' -> 128 / 128 / 1250; None when there is no number."""
    if not value:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    return int(round(float(m.group()))) if m else None


def duplicate_check(s: Settings, db: DB, iid: str, cover: Path, note: str | None) -> tuple[str, str | None]:
    """phash of the cover, plus a needs-info reason when an item from the lookback window has a near-identical
    cover (the same photos shared twice = the same garment listed twice). The seller clears it by answering
    "different item"."""
    with Image.open(cover) as im:
        h = imagehash.phash(im)
    if note and NOT_A_DUPLICATE.search(note):
        return str(h), None
    cfg = s.get("duplicates") or {}
    days, max_distance = int(cfg.get("lookback_days", 60)), int(cfg.get("max_distance", 8))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    for row in db.recent_covers(since, exclude=iid):
        try:
            other = imagehash.hex_to_hash(row["cover_hash"])
        except ValueError:
            continue
        if h - other <= max_distance:
            title = next(iter((loads(row["renders"]) or {}).values()), {}).get("title", "")
            return str(h), (f"looks like item {row['id']} ({title}) — same item? "
                            f"Reply: thrift answer {iid} \"different item\" to list it anyway")
    return str(h), None


def process_item(s: Settings, db: DB, iid: str) -> None:
    it = db.item(iid)
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    kinds = photo_kinds_of(d, photos)
    retail = {i for i, k in enumerate(kinds) if k == "retail"}
    facts = extract(photos, it["note"], s["models"]["extract"], s["images"]["llm_long_edge"], kinds=kinds)
    facts = strip_screenshot_evidence(facts, retail)     # a screenshot is never evidence for condition, size or flaws
    pr = price(facts, load_yaml("brand_tiers.yaml"), s["pricing"], it["note"])
    draft = copywriter.write(facts, s["models"]["copy"], s["copy"])
    audit = verify(facts, draft, s["models"]["verify"])
    final = copywriter.clean(CopyOut(
        poshmark_title=audit.poshmark_title, poshmark_description=audit.poshmark_description,
        poshmark_style_tags=draft.poshmark_style_tags, depop_description=audit.depop_description,
        depop_hashtags=draft.depop_hashtags))
    final.poshmark_description = copywriter.ensure_retail_line(final.poshmark_description, facts)
    problems = lint(facts, final)
    if not audit.unsupported:
        # The gate only sees the verifier's self-reported count. A verifier that rewrites the text but reports
        # nothing would otherwise publish an LLM-rewritten listing that nobody reviewed.
        problems += [f"verifier rewrote {f} without reporting a claim" for f in copywriter.changed_fields(draft, audit)]
    gate = evaluate(facts, pr, problems, len(audit.unsupported), s["gate"], s["pricing"])

    renders = build_renders(s, iid, d, photos, facts, final, pr, kinds)
    cover_hash, twin = duplicate_check(s, db, iid, d / "cover.jpg", it["note"])
    if twin:
        gate = GateResult("needs_info", [twin] + gate.reasons)
    (d / "item.json").write_text(json.dumps({
        "facts": facts.model_dump(), "price": pr.model_dump(),
        "renders": {k: v.model_dump() for k, v in renders.items()},
        "gate": {"decision": gate.decision, "reasons": gate.reasons},
        "unsupported_removed": [u.model_dump() for u in audit.unsupported],
    }, indent=2), encoding="utf-8")
    status = "needs_info" if gate.decision == "needs_info" else "ready"
    db.set_item(iid, status=status, facts=facts.model_dump(), price=pr.model_dump(),
                renders={k: v.model_dump() for k, v in renders.items()},
                gate={"decision": gate.decision, "reasons": gate.reasons}, cover_hash=cover_hash)
    db.log(iid, "item_processed", {"decision": gate.decision, "reasons": gate.reasons})

    first = next(iter(renders.values()), None)                  # no marketplace enabled: still notify
    title = first.title if first else final.poshmark_title
    head = f"{title} \u2014 ${pr.list_price}" if pr.list_price else title
    if status == "needs_info":
        notify.photo(d / "photos" / "00.jpg",
                     f"Needs info ({iid}): {head}\n- " + "\n- ".join(gate.reasons) +
                     f"\nReply: thrift answer {iid} \"size 8, NWT\"")
    elif gate.decision == "draft":
        notify.say(f"Queued as draft ({iid}): {head}\n- " + "\n- ".join(gate.reasons))


def requeue(s: Settings, db: DB, iid: str, marketplace: str | None = None) -> list[str]:
    """Send failed / dry-run post rows back to the queue. Only rows with NO listing URL: a row that reached the
    site (a URL, or 'posting'/'posted'/'drafted') is reconciled against the closet by hand, never re-posted
    (invariant 4). Returns the marketplaces requeued; raises ValueError with the reason otherwise."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    rows = [r for r in db.posts_for(iid) if marketplace is None or r["marketplace"] == marketplace]
    if not rows:
        raise ValueError(f"item {iid} has no {marketplace or ''} post rows — nothing to requeue".replace("  ", " "))
    for r in rows:
        if r["status"] not in REQUEUEABLE:
            raise ValueError(f"{r['marketplace']}: status is {r['status']} — only failed/dryrun rows can be requeued")
        if r["url"]:
            raise ValueError(f"{r['marketplace']}: has a listing URL ({r['url']}) — it reached the site; "
                             "check the closet and fix it by hand")
    if it["status"] not in ("ready", "drafted"):
        raise ValueError(f"item {iid} is {it['status']}, not ready — fix the item first (thrift answer)")
    with db.tx():
        for r in rows:
            db.upsert_post(iid, r["marketplace"], status="queued", last_error=None)
        if it["status"] != "ready":
            db.set_item(iid, status="ready")
        db.log(iid, "requeued", {"marketplaces": [r["marketplace"] for r in rows]})
    return [r["marketplace"] for r in rows]


def set_price(s: Settings, db: DB, iid: str, amount: int) -> str:
    """The owner's price for an item (Telegram reply/button or `thrift price`). Persisted as items.owner_price so a
    later reprocessing (a note, a needs_owner answer) keeps it and does not ask again. An item that was only
    waiting for the price becomes ready; anything else keeps its status. Returns the resulting status."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] not in PRICEABLE:
        raise ValueError(f"item {iid} is {it['status']} — the price can't be changed now")
    amount = int(amount)
    if amount <= 0:
        raise ValueError(f"price must be a positive whole number, got {amount}")
    renders = loads(it["renders"]) or {}
    for r in renders.values():
        r["price"] = amount
    pr = loads(it["price"]) or {}
    pr.update(list_price=amount, source="owner", basis=f"owner price ${amount}",
              by_marketplace={mp: amount for mp in renders} or pr.get("by_marketplace", {}))
    status = "ready" if it["status"] == "awaiting_price" else it["status"]
    with db.tx():
        db.set_item(iid, owner_price=amount, price=pr, renders=renders, status=status)
        db.log(iid, "price_set", {"amount": amount, "status": status})
    return status


def answer(s: Settings, db: DB, iid: str, note: str) -> None:
    """Seller note for one item: merge it in and send the item through the pipeline again."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] in ("posted", "posting"):
        raise ValueError(f"item {iid} is already listed/posting — edit it on the marketplace")
    if it["status"] not in ANSWERABLE:
        raise ValueError(f"item {iid} is {it['status']} — a note can't reopen it")
    merged = f"{it['note']}; {note}" if it["note"] else note
    with db.tx():
        # Forget earlier dry-runs / queue entries, or next_job would skip the corrected listing (dryrun + dry).
        # posting/posted/drafted/failed rows stay: those are history the poster must never repeat blindly.
        db.conn.execute("DELETE FROM posts WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
        db.set_item(iid, note=merged, status="new")
        db.log(iid, "answered", {"note": note})


def build_renders(s: Settings, iid: str, d: Path, photos: list[Path], facts: Facts, c: CopyOut,
                  pr: PriceResult, kinds: list[str] | None = None) -> dict[str, Render]:
    n = len(photos)
    kinds = kinds or ["own"] * n
    order = [i for i in facts.photo_order if 0 <= i < n]
    if 0 <= facts.cover_photo < n:
        order = [facts.cover_photo] + order
    order = list(dict.fromkeys(order + list(range(n))))         # cover first, then the model's order, no repeats
    own = [i for i in order if kinds[i] != "retail"]
    order = (own + [i for i in order if kinds[i] == "retail"]) if own else order   # screenshots last, never the cover
    cover = prep.square_cover(photos[order[0]], d / "cover.jpg", s["images"]["cover_size"])
    ordered = [str(cover)] + [str(photos[i]) for i in order[1:]]

    common = dict(brand=facts.brand.value, department=facts.department, category=facts.category,
                  subcategory=facts.subcategory, size=facts.size_us.value, colors=list(facts.colors),
                  condition=facts.condition, sku=iid,
                  original_price=_dollars(facts.retail_price.value) or pr.original_price)   # screenshot beats note
    out = {}
    for mp, mcfg in s["marketplaces"].items():
        if not mcfg.get("enabled"):
            continue
        is_posh = mp == "poshmark"
        out[mp] = Render(
            marketplace=mp,
            title=c.poshmark_title,
            description=c.poshmark_description if is_posh else c.depop_description,
            tags=c.poshmark_style_tags if is_posh else c.depop_hashtags,
            price=pr.by_marketplace.get(mp, pr.list_price or 0),
            photos=ordered[: mcfg["max_photos"]],
            **common,
        )
    return out
