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

from thrift_agent import approve, notify
from thrift_agent.brain import copy as copywriter, sizes, taxonomy
from thrift_agent.brain.extract import extract, strip_screenshot_evidence
from thrift_agent.brain.gate import GateResult, evaluate
from thrift_agent.brain.price import price
from thrift_agent.brain.verify import fit_style_tags, lint, verify
from thrift_agent.config import Settings, load_yaml
from thrift_agent.db import DB, loads, now
from thrift_agent.ingest import prep, segment as seg
from thrift_agent.schema import ITEM_ALONE, CopyOut, Ev, Facts, PriceResult, Render

MAX_SEGMENT_PHOTOS = 90        # the Messages API takes at most 100 image blocks per request; keep headroom
ANSWERABLE = ("needs_info", "ready", "failed", "new", "awaiting_price", "needs_owner",   # a note resets these to 'new'
              "awaiting_condition")
REQUEUEABLE = ("failed", "dryrun")                      # post statuses `thrift requeue` may send back to the queue
PARKED = "needs owner: "                                # last_error of a row the poster parked with a question
PRICEABLE = ("awaiting_price", "needs_info", "ready", "needs_owner")   # item statuses an owner price may be set on
MANIFEST = "photos.json"                                # per item: which photos are the seller's own vs retail screenshots
NOT_A_DUPLICATE = re.compile(r"different item|not a duplicate", re.I)   # owner's reply that clears the re-share hold
SAME_ITEM = re.compile(r"\bsame item\b|\bdrop it\b", re.I)               # owner's reply that drops a re-shared item
NWT_WORD = re.compile(r"\bNWT\b|new with tags", re.I)                     # the owner saying so is the only other NWT proof

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
    cfg = s["segmentation"]
    # Breaks in shooting, relative to this roll (own photos only: screenshots are placed by content).
    breaks = seg.pauses(kept_times, kept_kinds, cfg.get("pause_min_seconds", 30), cfg.get("pause_factor", 4))
    distances, changes, report = {}, set(), {}
    if len(kept) == 1:
        groups, reasons, summaries, unassigned = [[0]], [], ["single photo"], []
    else:
        out = seg.segment(list(zip(kept, kept_times)), s["models"]["segment"], s["images"]["thumb_long_edge"],
                          kinds=kept_kinds, breaks=breaks, fallback_px=s["images"].get("thumb_fallback_long_edge"),
                          max_bytes=int(cfg.get("max_request_mb", 20) * 1_000_000), report=report)
        groups = [g.photos for g in out.groups]
        summaries = [g.summary for g in out.groups]
        unassigned = list(out.unassigned)                 # screenshots the model could not match to an item
        reasons = seg.check(out, len(kept), cfg["min_confidence"], kinds=kept_kinds)
        distances, changes = seg.visual_changes(kept, kept_kinds, cfg.get("visual_change_min", 0.45),
                                                cfg.get("visual_change_factor", 2.5))
        # a pause AND a colour change inside an item, neither between two — only where the model was unsure (WO20)
        reasons += seg.timing_check(groups, kept_kinds, breaks, changes, [g.confidence for g in out.groups],
                                    cfg["min_confidence"])

    seg.contact_sheet(kept, groups, work / "contact_sheet.png", kinds=kept_kinds, breaks=breaks)   # approve.send_batch
    db.set_batch(bid, segmentation={"groups": groups, "summaries": summaries, "photos": [str(p) for p in kept],
                                    "kinds": kept_kinds, "unassigned": unassigned,
                                    "dropped": [str(p) for p in dropped], "note": note,
                                    # what the code saw, kept to tune the thresholds on real rolls
                                    "pauses": [[i, round(sec)] for i, sec in sorted(breaks.items())],
                                    "distances": [[i, d] for i, d in sorted(distances.items())],
                                    "changes": sorted(changes), "preview_px": report.get("preview_px")},
                 reasons=reasons)

    if reasons or s["segmentation"]["always_confirm"]:
        db.set_batch(bid, status="needs_confirm")
        approve.pump(s, db)                               # the contact sheet, when it is next (one question at a time)
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


def duplicate_check(s: Settings, db: DB, iid: str, cover_src: Path, note: str | None) -> tuple[str, str | None]:
    """phash of the cover photo (prep.cover_hash: padded to a square, as the covers were before they became 3:4, so
    older items compare like with like), plus a needs-info reason when an item from the lookback window has a
    near-identical cover (the same photos shared twice = the same garment listed twice). The seller clears it by
    answering "different item"."""
    h = imagehash.hex_to_hash(prep.cover_hash(cover_src))
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
            return str(h), (f"looks like item {row['id']} ({title}) — same item? Held until you reply "
                            f"'different item' (list it) or 'same item' (drop it); a price alone does not release it")
    return str(h), None


def owner_priced(pr: PriceResult, amount: int, marketplaces: list[str]) -> PriceResult:
    """The owner's approved price replaces the suggestion for every marketplace (source 'owner')."""
    return PriceResult(target=pr.target, list_price=amount, source="owner", basis=f"owner price ${amount}",
                       by_marketplace={mp: amount for mp in marketplaces}, original_price=pr.original_price)


def settle_nwt(facts: Facts, note: str | None) -> tuple[Facts, list[str]]:
    """NWT is never confirmed by a price alone. Proof is the owner's own photo of the attached hang tag, or the
    owner saying "NWT" in a note (then the note is the evidence). Without either, the item is listed as like new
    and the owner is told how to say otherwise. Returns (facts, notes for the approval message)."""
    if facts.condition != "NWT" or facts.hang_tag_photo is not None:
        return facts, []
    if note and NWT_WORD.search(note):
        ev = facts.condition_evidence.model_copy(update={"source": "note", "confidence": 1.0,
                                                        "value": facts.condition_evidence.value or "owner: NWT"})
        return facts.model_copy(update={"condition_evidence": ev}), []
    return (facts.model_copy(update={"condition": "like_new"}), [NWT_QUESTION])


NWT_QUESTION = "Listed as Like New: no photo of an attached hang tag — reply 'NWT' if the tag is attached"
GRADES_UP = ("good", "excellent", "like_new")      # torn between two of these: the higher (the owner's rule, WO17)
WELL_WORN = "looked well-worn — listed as Good; check before approving"


def settle_condition(facts: Facts) -> tuple[Facts, list[str]]:
    """The owner's condition rules (WO17). Torn between like new and good (or excellent): like new — Poshmark has no
    "very good", and Like New is the owner's choice; between excellent and good: excellent (which Poshmark lists as
    Like New too). Never Fair: a fair reading — or a good one the model thought might be fair — is listed as Good, and
    the owner is told to check. NWT is settle_nwt()'s and stays strict. Returns (facts, notes for the approval)."""
    cond, alt = facts.condition, facts.condition_alternative
    if cond in GRADES_UP and alt in GRADES_UP and GRADES_UP.index(alt) > GRADES_UP.index(cond):
        return facts.model_copy(update={"condition": alt}), []
    if cond == "fair" or (cond == "good" and alt == "fair"):
        return facts.model_copy(update={"condition": "good"}), [WELL_WORN]
    return facts, []


UNWORN_DOUBT = (0.30, 0.80)       # how sure "unworn: yes" may be and still be a question (owner rule, WO18)
NEW_SIDE = ("NWT", "NWOT", "like_new")
# The owner's answer to "Brand new or worn?" (buttons and `thrift condition`): Like New there means brand new without
# tags, i.e. NWOT (Poshmark's Like New, the copy's "New without tags."); worn is Good (never Fair, WO17).
OWNER_CONDITIONS = {"nwt": "NWT", "like_new": "NWOT", "good": "good"}
OWNER_CONDITION_LABELS = {"NWT": "new with tags", "NWOT": "new without tags (Like New)", "good": "worn (Good)"}
CONDITIONABLE = ("awaiting_condition", "awaiting_price", "needs_info", "ready", "new", "failed", "needs_owner")


def unworn_yes(ev: Ev) -> float | None:
    """How sure the model is that the pair is unworn (0..1), from unworn's value and confidence; None = no reading."""
    value = (ev.value or "").strip().lower()
    if value in ("yes", "true", "unworn"):
        return ev.confidence
    if value in ("no", "false", "worn"):
        return 1 - ev.confidence
    return None


def shoe_condition_doubt(facts: Facts) -> bool:
    """Ask the owner "brand new or worn?" — shoes only, any department, and only in doubt (owner rule, WO18): the model
    is between 30% and 80% sure the pair is unworn, or its two grades straddle new and used (one of NWT / NWOT /
    like_new, the other used). A clear new (box, tags, sole stickers) or a clear worn (sole wear, footbed imprints,
    creasing) is never asked. Judged on the model's own reading, before settle_nwt / settle_condition."""
    if facts.category.strip().lower() != "shoes":
        return False
    p = unworn_yes(facts.unworn)
    if p is not None and UNWORN_DOUBT[0] <= p <= UNWORN_DOUBT[1]:
        return True
    alt = facts.condition_alternative
    return alt is not None and (facts.condition in NEW_SIDE) != (alt in NEW_SIDE)


def apply_owner_condition(facts: Facts, condition: str) -> Facts:
    """The owner's answer is the evidence (source owner): NWT needs no hang-tag photo then (invariant 2)."""
    ev = Ev(value=f"owner: {OWNER_CONDITION_LABELS.get(condition, condition)}", photos=[], source="owner",
            confidence=1.0)
    return facts.model_copy(update={"condition": condition, "condition_alternative": None, "condition_evidence": ev})


KIDS_SURE = 0.70                  # how sure of Girls/Boys the model must be to go without asking (WO20)
KIDS_CHOICES = {"girls": "girls", "girl": "girls", "boys": "boys", "boy": "boys"}


def kids_question(facts: Facts) -> bool:
    """Poshmark files a kids size under Girls or Boys: the model's reading is used silently when it is at least 0.70
    sure of one of them; below that (or unisex, or unread) the owner is asked — [Girls] [Boys], queued like the other
    questions (WO20). Only for a kids item with a size (the form's size list is what needs it)."""
    if facts.department != "Kids" or not facts.size_us.value:
        return False
    return not (facts.kids_gender in ("girls", "boys") and facts.kids_gender_confidence >= KIDS_SURE)


def process_item(s: Settings, db: DB, iid: str) -> None:
    """extract -> price -> copy -> verify -> lint -> gate -> renders. The item then waits for the owner's price
    (awaiting_price, ONE Telegram message) unless the owner already priced it and nothing is unresolved."""
    it = db.item(iid)
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    kinds = photo_kinds_of(d, photos)
    retail = {i for i, k in enumerate(kinds) if k == "retail"}
    facts = extract(photos, it["note"], s["models"]["extract"], s["images"]["llm_long_edge"], kinds=kinds)
    facts = strip_screenshot_evidence(facts, retail)     # a screenshot is never evidence for condition, size or flaws
    if facts.condition_evidence.source == "owner":       # only the owner's answer is source owner, never the model
        facts.condition_evidence.source = "photo"
    # Shoes, in doubt between brand new and worn: the owner is asked before the price (WO18) — on the model's own
    # reading, before the settles below merge its two grades. Once answered, the answer is the condition.
    ask_condition = (not it["owner_condition"] and not it["owner_price"] and shoe_condition_doubt(facts))
    facts, nwt_questions = settle_nwt(facts, it["note"])   # NWT needs a tag photo or the owner's word; else like new
    facts, notes = settle_condition(facts)               # doubt -> like new; never Fair (Good, and a warning)
    if it["owner_condition"]:                            # the owner's word settles it all
        facts, notes, nwt_questions = apply_owner_condition(facts, it["owner_condition"]), [], []
    if it["owner_kids_gender"]:                          # the owner's [Girls]/[Boys]: never asked again
        facts = facts.model_copy(update={"kids_gender": it["owner_kids_gender"], "kids_gender_confidence": 1.0})
    facts, fit_notes, fit_questions = taxonomy.fit(facts)   # Poshmark's own category names (Kids Tops -> Shirts & Tops)
    ask_kids = kids_question(facts)                      # Girls or Boys, below 0.70 sure: a question before the price
    shown = listing_photos(s, facts, len(photos), kinds)  # flaw photos in, never the cover: the condition rule
    _, cover_note = choose_cover(facts, len(photos), kinds)
    # The card shows only what needs the owner (WO20): these warnings and the allowed questions; the rest (an unsure
    # grade, a subcategory left out) is kept in the item's record as info.
    notes += flaw_notes(facts, shown) + ([cover_note] if cover_note else [])
    info = list(fit_notes)
    pr = price(facts, load_yaml("brand_tiers.yaml"), s["pricing"], it["note"])
    enabled = [mp for mp, m in s["marketplaces"].items() if m.get("enabled")]
    if it["owner_price"]:
        pr = owner_priced(pr, int(it["owner_price"]), enabled)   # approved earlier; a reprocessing never asks again
    draft = copywriter.write(facts, s["models"]["copy"], s["copy"])
    audit = verify(facts, draft, s["models"]["verify"])
    final = copywriter.clean(CopyOut(
        poshmark_title=audit.poshmark_title, poshmark_description=audit.poshmark_description,
        poshmark_style_tags=draft.poshmark_style_tags, depop_description=audit.depop_description,
        depop_hashtags=draft.depop_hashtags))
    final = copywriter.condition_rule(final, facts)      # wear is shown in the photos, never put in words
    final.poshmark_description = copywriter.ensure_retail_line(final.poshmark_description, facts)
    final.poshmark_style_tags = fit_style_tags(final.poshmark_style_tags, facts)   # Poshmark's curated tags only
    problems = lint(facts, final, shown)
    if not audit.unsupported:
        # The gate only sees the verifier's self-reported count. A verifier that rewrites the text but reports
        # nothing would otherwise publish an LLM-rewritten listing that nobody reviewed.
        problems += [f"verifier rewrote {f} without reporting a claim" for f in copywriter.changed_fields(draft, audit)]
    gate = evaluate(facts, pr, problems, len(audit.unsupported), s["gate"], s["pricing"])

    questions = fit_questions + gate.questions + nwt_questions
    if fit_questions:                                    # a department/category Poshmark doesn't have: like "Other"
        gate = GateResult("needs_info", fit_questions + gate.reasons, gate.notes, gate.questions)

    renders = build_renders(s, iid, d, photos, facts, final, pr, kinds)
    cover_src = photos[photo_order(facts, len(photos), kinds)[0]]
    cover_hash, twin = duplicate_check(s, db, iid, cover_src, it["note"])
    if twin:
        gate = GateResult("needs_info", [twin] + gate.reasons, gate.notes, gate.questions)
        questions = [twin] + questions
    # `hold` marks a question a price alone must not settle: set_price() keeps a held item waiting until the owner
    # answers it ("different item" lists it, "same item" drops it). `notes` are told to the owner but need no answer.
    gate_doc = {"decision": gate.decision, "reasons": gate.reasons, "questions": questions, "notes": notes,
                "info": info + gate.notes, "hold": "reshare" if twin else None, "ask_kids": ask_kids}
    (d / "item.json").write_text(json.dumps({
        "facts": facts.model_dump(), "price": pr.model_dump(),
        "renders": {k: v.model_dump() for k, v in renders.items()},
        "gate": gate_doc,
        "unsupported_removed": [u.model_dump() for u in audit.unsupported],
    }, indent=2), encoding="utf-8")
    # Nothing publishes without the owner's price. Open questions (an unreadable brand or size, a possible
    # re-share) ride along in the same message; an item the owner already priced comes back only while something
    # is still unresolved.
    unresolved = gate.decision == "needs_info"
    latest = db.item(iid)
    if latest["updated_at"] != it["updated_at"] and latest["status"] == "new":
        # An answer arrived while the item was being processed (the worker's Telegram thread: a note, a condition; or
        # `thrift redo`): this result is already out of date. The item stays 'new' and is processed again with it.
        db.log(iid, "item_processed_stale", {"would_be": gate.decision})
        return
    # Whatever was asked about this item before is out of date now (a new card, a new question): closed, so the
    # queue sends the new one rather than waiting on the old (WO20).
    db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref=? AND resolved_at IS NULL", (now(), iid))
    status = ("awaiting_condition" if ask_condition
              else "awaiting_price" if unresolved or not it["owner_price"] or ask_kids else "ready")
    db.set_item(iid, status=status, facts=facts.model_dump(), price=pr.model_dump(),
                renders={k: v.model_dump() for k, v in renders.items()}, gate=gate_doc, cover_hash=cover_hash)
    db.log(iid, "item_processed", {"decision": gate.decision, "reasons": gate.reasons, "status": status})

    if status in ("awaiting_condition", "awaiting_price"):
        approve.pump(s, db)                              # ONE open question at a time: sent now if it is next
    elif gate.decision == "draft":
        first = next(iter(renders.values()), None)
        title = first.title if first else final.poshmark_title
        notify.say(f"Queued as draft ({iid}): {title} \u2014 ${pr.list_price}\n- " + "\n- ".join(gate.reasons))


def requeue(s: Settings, db: DB, iid: str, marketplace: str | None = None) -> list[str]:
    """Send failed / dry-run post rows back to the queue. Only rows with NO listing URL: a row that reached the
    site (a URL, or 'posting'/'posted'/'drafted') is reconciled against the closet by hand, never re-posted
    (invariant 4). Returns the marketplaces requeued; raises ValueError with the reason otherwise.

    An item the poster parked in needs_owner (its question came before anything was submitted, so the row is
    'queued' with a "needs owner:" error) goes back to 'ready' as it is — no reprocessing — and its pending
    Telegram question is closed: the way to retry after the poster's code changed. `thrift answer` is the way to
    retry with an answer."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    parked = it["status"] == "needs_owner"
    rows = [r for r in db.posts_for(iid) if marketplace is None or r["marketplace"] == marketplace]
    if not rows:
        raise ValueError(f"item {iid} has no {marketplace or ''} post rows — nothing to requeue".replace("  ", " "))
    for r in rows:
        waiting = parked and r["status"] == "queued" and (r["last_error"] or "").startswith(PARKED)
        if r["status"] not in REQUEUEABLE and not waiting:
            raise ValueError(f"{r['marketplace']}: status is {r['status']} — only failed/dryrun rows (or a poster "
                             "question) can be requeued")
        if r["url"]:
            raise ValueError(f"{r['marketplace']}: has a listing URL ({r['url']}) — it reached the site; "
                             "check the closet and fix it by hand")
        if (r["last_error"] or "").startswith("unconfirmed publish: "):
            raise ValueError(f"{r['marketplace']}: List This Item was pressed and no listing address was found — it "
                             f"may be live; check the closet, then `thrift mark-posted {iid} {r['marketplace']} <url>`")
    if it["status"] not in ("ready", "drafted", "needs_owner"):
        raise ValueError(f"item {iid} is {it['status']}, not ready — fix the item first (thrift answer)")
    with db.tx():
        for r in rows:
            db.upsert_post(iid, r["marketplace"], status="queued", last_error=None)
        if it["status"] != "ready":
            db.set_item(iid, status="ready")
        if parked:
            db.outbox_resolve("owner_q", iid)            # the question is moot: never re-sent after a sleep
        db.log(iid, "requeued", {"marketplaces": [r["marketplace"] for r in rows], "from": it["status"]})
    return [r["marketplace"] for r in rows]


def set_price(s: Settings, db: DB, iid: str, amount: int) -> str:
    """The owner's price for an item (Telegram reply/button or `thrift price`). Persisted as items.owner_price so a
    later reprocessing (a note, a needs_owner answer) keeps it and does not ask again. An item that was only
    waiting for the price becomes ready; anything else keeps its status. Returns the resulting status."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] == "awaiting_condition":
        raise ValueError(f"item {iid} first needs its condition: brand new or worn? (the buttons, or `thrift condition "
                         f"{iid} nwt|like_new|good`)")
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
    gate = loads(it["gate"]) or {}
    # A re-share question is never settled by the price alone; nor is Girls/Boys (its own question, asked first).
    waiting_only_for_the_price = it["status"] == "awaiting_price" and not gate.get("hold") and not gate.get("ask_kids")
    status = "ready" if waiting_only_for_the_price else it["status"]
    with db.tx():
        db.set_item(iid, owner_price=amount, price=pr, renders=renders, status=status)
        db.log(iid, "price_set", {"amount": amount, "status": status})
    return status


def set_kids_gender(s: Settings, db: DB, iid: str, choice: str) -> str:
    """The owner's [Girls] / [Boys] for a kids item (WO20): which of Poshmark's size lists the size is picked from.
    Stored as items.owner_kids_gender (a reprocessing keeps it) and written into the facts and the renders at once —
    nothing else depends on it, so no reprocessing. An item that was only waiting for this and already has its price
    becomes ready. Returns the resulting status."""
    gender = KIDS_CHOICES.get((choice or "").strip().lower())
    if gender is None:
        raise ValueError(f"kids gender must be girls or boys, got {choice!r}")
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] not in PRICEABLE:
        raise ValueError(f"item {iid} is {it['status']} — Girls/Boys can't be changed now")
    facts = loads(it["facts"]) or {}
    facts.update(kids_gender=gender, kids_gender_confidence=1.0)
    renders = loads(it["renders"]) or {}
    for r in renders.values():
        if "kids_gender" in r:
            r["kids_gender"] = gender
    gate = {**(loads(it["gate"]) or {}), "ask_kids": False}
    waiting_only_for_this = (it["status"] == "awaiting_price" and it["owner_price"] and not gate.get("hold")
                             and gate.get("decision") != "needs_info")
    status = "ready" if waiting_only_for_this else it["status"]
    with db.tx():
        db.set_item(iid, owner_kids_gender=gender, facts=facts, renders=renders, gate=gate, status=status)
        db.log(iid, "kids_gender_set", {"kids_gender": gender, "status": status})
    return status


def defer_item(s: Settings, db: DB, iid: str) -> None:
    """[Later] on a price card: the item goes to the end of the owner's queue (WO20), behind everything else."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] not in ("awaiting_price", "needs_info"):
        raise ValueError(f"item {iid} is {it['status']} — no price card to put off")
    db.set_item(iid, deferred_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    db.log(iid, "deferred", {"status": it["status"]})


REDO_KEEP = ("posting", "posted", "drafted")      # item and post statuses that reached the site: never rebuilt


def unconfirmed_publish(post) -> bool:
    """A publish that may have gone live although its address was never found (thrift mark-posted settles it)."""
    return (post["last_error"] or "").startswith("unconfirmed publish: ")


def redo_batch(s: Settings, db: DB, bid: str) -> tuple[list[str], list[str]]:
    """Rebuild a split batch's items that never reached the site (WO20): each goes back to 'new' with its photos and
    its notes, from the grouping already confirmed (no new contact sheet), and is processed again — new cover, new
    price, a new card in the queue. Its suggested and approved price, its dry-run post rows and its Telegram messages
    are dropped; the owner's answers about the item itself (condition, Girls/Boys) are kept. An item that is posting,
    posted, drafted, or has a post that reached the site or may have (a URL, an unconfirmed publish) is left alone,
    and so is one the owner dropped as a re-share. Returns (rebuilt, kept): kept as "<item> (<why>)"."""
    b = db.batch(bid)
    if b is None:
        raise ValueError(f"unknown batch {bid}")
    items = db.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY seq", (bid,)).fetchall()
    if not items:
        raise ValueError(f"batch {bid} is {b['status']} with no items — nothing to redo")
    rebuilt, kept = [], []
    for it in items:
        posts = db.conn.execute("SELECT * FROM posts WHERE item_id=?", (it["id"],)).fetchall()
        reached = [f"{p['marketplace']}: unconfirmed publish" if unconfirmed_publish(p)
                   else f"{p['marketplace']} {p['status']}"
                   for p in posts if p["status"] in REDO_KEEP or p["url"] or unconfirmed_publish(p)]
        if it["status"] in REDO_KEEP or it["status"] == "dropped" or reached:
            kept.append(f"{it['id']} ({', '.join(reached) or it['status']})")
            continue
        with db.tx():
            db.conn.execute("DELETE FROM posts WHERE item_id=?", (it["id"],))
            db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref=? AND resolved_at IS NULL",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), it["id"]))
            db.set_item(it["id"], status="new", facts=None, price=None, renders=None, gate=None, owner_price=None,
                        deferred_at=None, cover_hash=None)
            db.log(it["id"], "redo", {"batch": bid, "from": it["status"]})
        rebuilt.append(it["id"])
    if not rebuilt:
        raise ValueError(f"batch {bid}: every item reached the site ({', '.join(kept)}) — nothing to redo")
    return rebuilt, kept


def requeue_batch(s: Settings, db: DB, bid: str) -> str:
    """A batch that failed (an API error, a model change gone wrong) goes back to 'new': the worker processes it again
    on its next tick, from the same share folder. Only a failed batch, and only while its share folder is still there
    (a failed batch is never archived). Returns 'new'."""
    b = db.batch(bid)
    if b is None:
        raise ValueError(f"unknown batch {bid}")
    if b["status"] != "failed":
        raise ValueError(f"batch {bid} is {b['status']} — only a failed batch can be requeued")
    if not Path(b["src_dir"]).is_dir():
        raise ValueError(f"batch {bid}: its share folder is gone ({b['src_dir']}) — share the photos again")
    with db.tx():
        db.set_batch(bid, status="new", reasons=None)
        db.log(bid, "requeued", {"from": "failed"})
    return "new"


def last_error(db: DB, ref: str) -> str | None:
    """The last line of the latest error logged for a batch or item (the exception, e.g. "BadRequestError: ...")."""
    row = db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='error' ORDER BY ts DESC, rowid DESC "
                          "LIMIT 1", (ref,)).fetchone()
    if row is None or not row["detail"]:
        return None
    detail = json.loads(row["detail"]) if row["detail"].startswith('"') else row["detail"]
    lines = [line.strip() for line in str(detail).splitlines() if line.strip()]
    return lines[-1] if lines else None


def owner_choice(choice: str) -> str | None:
    """"NWT" / "like new" / "like-new" / "Good" -> the OWNER_CONDITIONS key, or None."""
    key = (choice or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in OWNER_CONDITIONS else None


def set_condition(s: Settings, db: DB, iid: str, choice: str) -> str:
    """The owner's answer to "Brand new or worn?" (a button, a reply, or `thrift condition`): nwt | like_new | good.
    Stored as items.owner_condition (a reprocessing keeps it and never asks again) and the item goes through the
    pipeline again, so the price, the copy and the listing follow the answer; then the price card comes. Returns
    'new' (reprocessing)."""
    if (key := owner_choice(choice)) is None:
        raise ValueError(f"condition must be one of {', '.join(OWNER_CONDITIONS)}, got {choice!r}")
    condition = OWNER_CONDITIONS[key]
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] not in CONDITIONABLE:
        raise ValueError(f"item {iid} is {it['status']} — its condition can't be changed now")
    with db.tx():
        # As with a note: earlier dry-runs and queue entries are forgotten, real post history stays.
        db.conn.execute("DELETE FROM posts WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
        db.set_item(iid, owner_condition=condition, status="new")
        db.log(iid, "condition_set", {"condition": condition, "from": it["status"]})
    return "new"


def answer(s: Settings, db: DB, iid: str, note: str) -> str:
    """Seller note for one item: merge it in and send the item through the pipeline again. Returns the new status:
    'new' (reprocessing), or 'dropped' when the owner confirms a held re-share is the same item."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] in ("posted", "posting"):
        raise ValueError(f"item {iid} is already listed/posting — edit it on the marketplace")
    if it["status"] not in ANSWERABLE:
        raise ValueError(f"item {iid} is {it['status']} — a note can't reopen it")
    merged = f"{it['note']}; {note}" if it["note"] else note
    if (loads(it["gate"]) or {}).get("hold") == "reshare" and SAME_ITEM.search(note) and not NOT_A_DUPLICATE.search(note):
        with db.tx():
            db.set_item(iid, note=merged, status="dropped")     # the same garment is already listed: never list it twice
            db.log(iid, "dropped", {"note": note})
        return "dropped"
    with db.tx():
        # Forget earlier dry-runs / queue entries, or next_job would skip the corrected listing (dryrun + dry).
        # posting/posted/drafted/failed rows stay: those are history the poster must never repeat blindly.
        db.conn.execute("DELETE FROM posts WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
        db.set_item(iid, note=merged, status="new")
        db.log(iid, "answered", {"note": note})
    return "new"


def flaw_photos(facts: Facts, n: int) -> set[int]:
    """The photos that show a flaw: they go in the listing, never as its cover (the owner's condition rule)."""
    return {i for f in facts.flaws for i in f.photos if 0 <= i < n}


NO_FRONT_COVER = "cover: no front flat-lay photo"


def choose_cover(facts: Facts, n: int, kinds: list[str] | None = None) -> tuple[int | None, str | None]:
    """The cover (owner rule, WO20): the item alone, its front, flat lay or on a hanger — by the model's photo_roles,
    checked here: only a photo of the item alone (front, else side, else back; the model's own pick first among
    equals), never worn / a label / a tag / a flaw / a box / a screenshot. (photo, note for the card): the note says
    "cover: no front flat-lay photo" when no front shot exists; (None, …) leaves the cover to photo_order's older
    rules — no roles at all (older facts), or no photo of the item alone."""
    kinds = kinds or ["own"] * n
    roles = {r.photo: r.role for r in facts.photo_roles if 0 <= r.photo < n}
    if not roles:
        return None, None
    flawed = flaw_photos(facts, n)
    preferred = list(dict.fromkeys(i for i in [facts.cover_photo, *facts.photo_order, *range(n)] if 0 <= i < n))
    alone = [i for i in preferred if kinds[i] != "retail" and i not in flawed]
    for role in ITEM_ALONE:                                   # front, then side, then back
        if (pick := next((i for i in alone if roles.get(i) == role), None)) is not None:
            return pick, None if role == "front" else NO_FRONT_COVER
    return None, NO_FRONT_COVER


def photo_order(facts: Facts, n: int, kinds: list[str] | None = None) -> list[int]:
    """The listing's photo order: the cover first (choose_cover), then the model's order, every photo once; retail
    screenshots last and never the cover; a photo that shows a flaw never the cover either (the first clean own photo
    is, when there is one)."""
    kinds = kinds or ["own"] * n
    order = [i for i in facts.photo_order if 0 <= i < n]
    if 0 <= facts.cover_photo < n:
        order = [facts.cover_photo] + order
    order = list(dict.fromkeys(order + list(range(n))))         # cover first, then the model's order, no repeats
    own = [i for i in order if kinds[i] != "retail"]
    order = (own + [i for i in order if kinds[i] == "retail"]) if own else order   # screenshots last, never the cover
    flawed = flaw_photos(facts, n)
    clean = [i for i in own if i not in flawed]
    cover, _ = choose_cover(facts, n, kinds)
    if cover is None and order and order[0] in flawed and clean:
        cover = clean[0]
    if cover is not None and order[0] != cover:
        order = [cover] + [i for i in order if i != cover]
    return order


def fit_photos(order: list[int], keep: set[int], limit: int) -> list[int]:
    """At most `limit` photos in `order`, the cover first and every photo in `keep` (the flaw photos) among them:
    when there are too many, the last photos that are neither the cover nor kept make room."""
    out = list(order)
    while len(out) > limit:
        drop = next((i for i in reversed(out[1:]) if i not in keep), None)
        if drop is None:
            break
        out.remove(drop)
    return out[:limit]


def listing_photos(s: Settings, facts: Facts, n: int, kinds: list[str] | None = None) -> list[int]:
    """The photos (indices, cover first) every enabled marketplace's listing shows at least: the order cut to the
    smallest photo limit, flaw photos kept. What lint checks the flaw photos against."""
    limits = [m.get("max_photos", n) for m in s["marketplaces"].values() if m.get("enabled")] or [n]
    return fit_photos(photo_order(facts, n, kinds), flaw_photos(facts, n), min(limits))


def flaw_notes(facts: Facts, photos: list[int]) -> list[str]:
    """For the approval message: a flaw no photo shows isn't in the listing at all, and a cover that shows a flaw
    means every own photo does. Told, never asked."""
    notes = [f"flaw without a photo, so the listing doesn't show it: {f.description}" for f in facts.flaws
             if not f.photos]
    if photos and photos[0] in flaw_photos(facts, max(photos) + 1):
        notes.append("every photo shows a flaw, so the cover does too")
    return notes


def build_renders(s: Settings, iid: str, d: Path, photos: list[Path], facts: Facts, c: CopyOut,
                  pr: PriceResult, kinds: list[str] | None = None) -> dict[str, Render]:
    order = photo_order(facts, len(photos), kinds)
    flawed = flaw_photos(facts, len(photos))
    width, height = prep.cover_dims(s["images"]["cover_size"])
    cover = prep.portrait_cover(photos[order[0]], d / "cover.jpg", width, height)   # 3:4: Poshmark's cover frame

    def paths(limit: int) -> list[str]:                # this marketplace's photos: flaw photos are never cut
        shown = fit_photos(order, flawed, limit)
        return [str(cover)] + [str(photos[i]) for i in shown[1:]]

    common = dict(brand=facts.brand.value, department=facts.department, category=facts.category,
                  subcategory=facts.subcategory, size=sizes.size_label(facts), colors=list(facts.colors),
                  kids_gender=facts.kids_gender if facts.department == "Kids" else None,
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
            photos=paths(mcfg["max_photos"]),
            **common,
        )
    return out
