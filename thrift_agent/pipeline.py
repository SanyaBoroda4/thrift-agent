"""Orchestration: inbox folder → batch → items → facts/price/copy/gate → ready for the poster."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from thrift_agent import notify
from thrift_agent.brain import copy as copywriter
from thrift_agent.brain.extract import extract
from thrift_agent.brain.gate import evaluate
from thrift_agent.brain.price import price
from thrift_agent.brain.verify import lint, verify
from thrift_agent.config import Settings, load_yaml
from thrift_agent.db import DB, loads
from thrift_agent.ingest import prep, segment as seg
from thrift_agent.schema import CopyOut, Facts, PriceResult, Render

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
    if not photos:
        return None
    bid = db.add_batch(str(folder), len(photos))
    if bid:
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
    norm = [prep.normalize(p, work / "all" / f"{i:03d}.jpg", s["images"]["work_long_edge"]) for i, p in enumerate(raw)]
    norm_times = [times[p.name] for p in raw]
    kept, dropped = prep.drop_near_duplicates(norm, norm_times, s["images"]["dedupe_hamming"])
    kept_times = [times[raw[int(p.stem)].name] for p in kept]

    note_file = src / "notes.txt"
    note = note_file.read_text(encoding="utf-8").strip() if note_file.exists() else None

    if len(kept) == 1:
        groups, reasons, summaries = [[0]], [], ["single photo"]
    else:
        out = seg.segment(list(zip(kept, kept_times)), s["models"]["segment"], s["images"]["thumb_long_edge"])
        groups = [g.photos for g in out.groups]
        summaries = [g.summary for g in out.groups]
        reasons = seg.check(out, len(kept), s["segmentation"]["min_confidence"])

    sheet = seg.contact_sheet(kept, groups, work / "contact_sheet.png")
    db.set_batch(bid, segmentation={"groups": groups, "summaries": summaries, "photos": [str(p) for p in kept],
                                    "dropped": [str(p) for p in dropped], "note": note},
                 reasons=reasons)

    if reasons or s["segmentation"]["always_confirm"]:
        db.set_batch(bid, status="needs_confirm")
        lines = [f"item {k}: {summaries[k - 1]} — photos {g}" for k, g in enumerate(groups, 1)]
        why = ("\n⚠️ " + "; ".join(reasons)) if reasons else ""
        notify.photo(sheet, f"Batch {bid}: {len(kept)} photos → {len(groups)} items\n" + "\n".join(lines) + why +
                     f"\nReply: thrift confirm {bid} ok   (or 12>2, split 7, merge 2 3)")
        return
    split(s, db, bid, groups)


def confirm(s: Settings, db: DB, bid: str, cmd: str) -> None:
    b = db.batch(bid)
    if b is None or b["status"] != "needs_confirm":
        raise ValueError(f"batch {bid} isn't waiting for confirmation")
    groups = seg.apply_correction(loads(b["segmentation"])["groups"], cmd)
    split(s, db, bid, groups)


def split(s: Settings, db: DB, bid: str, groups: list[list[int]]) -> None:
    b = db.batch(bid)
    segd = loads(b["segmentation"])
    photos = [Path(p) for p in segd["photos"]]
    note = segd.get("note") if len(groups) == 1 else None      # a batch note is only unambiguous for one item
    for k, g in enumerate(groups, 1):
        d = s.path("work") / bid / f"item_{k:02d}"
        (d / "photos").mkdir(parents=True, exist_ok=True)
        for j, idx in enumerate(g):
            shutil.copy2(photos[idx], d / "photos" / f"{j:02d}.jpg")
        iid = db.add_item(bid, k, str(d), note)
        db.log(iid, "item_created", {"batch": bid, "photos": g})
    db.set_batch(bid, status="split")
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


def process_item(s: Settings, db: DB, iid: str) -> None:
    it = db.item(iid)
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    facts = extract(photos, it["note"], s["models"]["extract"], s["images"]["llm_long_edge"])
    pr = price(facts, load_yaml("brand_tiers.yaml"), s["pricing"], it["note"])
    draft = copywriter.write(facts, s["models"]["copy"], s["copy"])
    audit = verify(facts, draft, s["models"]["verify"])
    final = copywriter.clean(CopyOut(
        poshmark_title=audit.poshmark_title, poshmark_description=audit.poshmark_description,
        poshmark_style_tags=draft.poshmark_style_tags, depop_description=audit.depop_description,
        depop_hashtags=draft.depop_hashtags))
    problems = lint(facts, final)
    gate = evaluate(facts, pr, problems, len(audit.unsupported), s["gate"], s["pricing"])

    renders = build_renders(s, iid, d, photos, facts, final, pr)
    (d / "item.json").write_text(json.dumps({
        "facts": facts.model_dump(), "price": pr.model_dump(),
        "renders": {k: v.model_dump() for k, v in renders.items()},
        "gate": {"decision": gate.decision, "reasons": gate.reasons},
        "unsupported_removed": [u.model_dump() for u in audit.unsupported],
    }, indent=2), encoding="utf-8")
    status = "needs_info" if gate.decision == "needs_info" else "ready"
    db.set_item(iid, status=status, facts=facts.model_dump(), price=pr.model_dump(),
                renders={k: v.model_dump() for k, v in renders.items()},
                gate={"decision": gate.decision, "reasons": gate.reasons})
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


def answer(s: Settings, db: DB, iid: str, note: str) -> None:
    it = db.item(iid)
    merged = f"{it['note']}; {note}" if it["note"] else note
    db.set_item(iid, note=merged, status="new")


def build_renders(s: Settings, iid: str, d: Path, photos: list[Path], facts: Facts, c: CopyOut,
                  pr: PriceResult) -> dict[str, Render]:
    n = len(photos)
    order = [i for i in facts.photo_order if 0 <= i < n]
    if 0 <= facts.cover_photo < n:
        order = [facts.cover_photo] + order
    order = list(dict.fromkeys(order + list(range(n))))         # cover first, then the model's order, no repeats
    cover = prep.square_cover(photos[order[0]], d / "cover.jpg", s["images"]["cover_size"])
    ordered = [str(cover)] + [str(photos[i]) for i in order[1:]]

    common = dict(brand=facts.brand.value, department=facts.department, category=facts.category,
                  subcategory=facts.subcategory, size=facts.size_us.value, colors=list(facts.colors),
                  condition=facts.condition, sku=iid)
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
