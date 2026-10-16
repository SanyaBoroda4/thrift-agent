"""Orchestration: inbox folder → batch → items → facts/price/copy/gate → ready for the poster."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import imagehash

from thrift_agent import approve, brands, notify
from thrift_agent.brain import copy as copywriter, cover as cover_brain, labels, premium, sizes, taxonomy
from thrift_agent.brain.extract import extract, strip_screenshot_evidence
from thrift_agent.brain.gate import OTHER_CATEGORY_QUESTION, SIZE_NOTE, GateResult, evaluate
from thrift_agent.brain.price import price
from thrift_agent.brain.verify import lint, verify
from thrift_agent.config import Settings, load_yaml
from thrift_agent.db import DB, loads, now
from thrift_agent.ingest import prep, segment as seg
from thrift_agent.schema import ITEM_ALONE, CopyOut, Ev, Facts, FrontOut, PieceSize, Premium, PriceResult, Render

MAX_SEGMENT_PHOTOS = 90        # the Messages API takes at most 100 image blocks per request; keep headroom
ANSWERABLE = ("needs_info", "ready", "failed", "new", "awaiting_price", "needs_owner",   # a note resets these to 'new'
              "awaiting_condition")
REQUEUEABLE = ("failed", "dryrun", "skipped")           # listing statuses `thrift requeue` may send back to the queue
PARKED = "needs owner: "                                # error of a row the poster parked with a question
DROPPED_SAID = "This one was shared again (or dropped) — it won't be listed; its newest card is the one to answer"
PRICEABLE = ("awaiting_price", "needs_info", "ready", "needs_owner")   # item statuses an owner price may be set on
MANIFEST = "photos.json"                                # per item: which photos are the seller's own vs retail screenshots
NOT_A_DUPLICATE = re.compile(r"different item|not a duplicate", re.I)   # owner's reply that clears the re-share hold
SAME_ITEM = re.compile(r"\bsame item\b|\bdrop it\b", re.I)               # owner's reply that drops a re-shared item
NWT_WORD = re.compile(r"\bNWT\b|new with tags", re.I)                     # the owner saying so is the only other NWT proof

# ---------- inbox ----------


ICLOUD_WAIT = "icloud_waiting"     # kv: {share folder: {"since", "told"}} — shares whose files are still in iCloud
ICLOUD_TELL_AFTER = timedelta(minutes=5)
ICLOUD_MESSAGE = "Waiting for iCloud to finish downloading the photos…"


def ready_folders(s: Settings, db: DB | None = None) -> list[Path]:
    """Folders the Shortcut finished writing: marker present, every file really downloaded, quiet for a bit.

    A share whose files are still in iCloud only (the old .Name.icloud placeholders, or current macOS's dataless
    files) is asked for (`brctl download`, the folder and each such file) and waits; with `db`, ONE line goes out if
    that lasts more than ICLOUD_TELL_AFTER (WO28 §4), per share."""
    inbox, marker, settle = s.path("inbox"), s["inbox"]["done_marker"], s["inbox"]["settle_seconds"]
    if not inbox.exists():
        return []
    out, waiting = [], {}
    for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        pending = prep.icloud_placeholders(d)
        if pending:
            if platform.system() == "Darwin":
                for target in [d, *(p for p in pending if not p.name.endswith(".icloud"))]:
                    subprocess.run(["brctl", "download", str(target)], check=False, capture_output=True)
            waiting[str(d)] = len(pending)
            continue
        marks = [f for f in d.iterdir() if f.name == marker or f.stem == marker]   # "_done" or "_done.txt"
        if not marks:
            continue
        if share_complete(s, d, marks[0], settle):
            out.append(d)
    if db is not None:
        _icloud_wait(db, waiting)
    return out


EMPTY_SHARE_WAIT = 30 * 60        # WO37: a share whose photos haven't come yet — waited for this long before "no photos"


def arrived(p: Path) -> float:
    """When the file came onto this Mac: its inode change time, never its modification time — that one comes from the
    phone (live 2026-10-12: photos dated Oct 9, so a share always looked "quiet for a while")."""
    st = p.stat()
    return max(st.st_ctime, getattr(st, "st_birthtime", 0) or 0)


def expected_photos(marker: Path) -> int | None:
    """The number of photos the Shortcut says it saved, when its marker carries one ("done 4", "4"); else None."""
    try:
        m = re.search(r"\d+", marker.read_text(encoding="utf-8", errors="ignore")[:64])
    except OSError:
        return None
    return int(m.group()) if m and 0 < int(m.group()) < 500 else None


def share_complete(s: Settings, d: Path, marker: Path, settle: float) -> bool:
    """Is this share whole? WO37 (live 2026-10-12: the iPhone's `_done` came before its photos — two shares taken with
    no photos, two with only their small label shots): nothing has come into the folder for `settle` seconds (arrival
    on this Mac, not the photos' dates), the marker itself is at least `inbox.min_wait_seconds` old, and — when the
    marker carries a count — that many photos are here. A share with no photo at all waits up to 30 minutes."""
    now = time.time()
    files = list(d.iterdir())
    photos = [f for f in files if prep.is_photo(f)]
    newest = max((arrived(f) for f in files), default=0)          # each file's arrival: a new one is fresh
    since_marker = now - arrived(marker)
    if now - newest < settle or since_marker < float(s.get("inbox.min_wait_seconds", 90) or 0):
        return False
    want = expected_photos(marker)
    if want is not None and len(photos) < want:
        return since_marker >= EMPTY_SHARE_WAIT          # the count never came: take what is here, said in register
    return bool(photos) or since_marker >= EMPTY_SHARE_WAIT


def late_photos(s: Settings, db: DB) -> list[str]:
    """WO37: photos that came into a share after it was taken (live 2026-10-12: two alo bras' shares failed as "no
    usable photos", and the leggings' and the vest's full shots came after their label shots). Looks at the archived
    shares of the last two days, once per new file:
    - a share failed for having no photo now has some: it goes back into the inbox and is taken as a new share;
    - a share taken whole whose single item isn't on any site yet: the item is noted for the owner (ops) — a reply
      'redo' rebuilds it with every photo;
    - otherwise one line to the private chat: which photos came late for which item.
    Returns what was done (for the log)."""
    done, seen = [], set(json.loads(db.kv_get("late_photos_seen") or "[]"))
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    for b in db.conn.execute("SELECT * FROM batches WHERE created_at >= ?", (since,)).fetchall():
        dest = None
        for e in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='archived'", (b["id"],)):
            dest = (loads(e["detail"]) or {}).get("dest")
        if not dest or not Path(dest).is_dir():
            continue
        try:
            photos = sorted(p for p in Path(dest).iterdir() if prep.is_photo(p))
        except OSError:
            continue
        taken = registered_names(db, b["id"])
        if taken is None:                                # from before WO37: the first to arrive were taken — as many
            work = s.path("work") / b["id"] / "all"      # as its preparation read (more than registered, at times)
            n = len(list(work.glob("*.jpg"))) if work.is_dir() else 0
            taken = {p.name for p in sorted(photos, key=arrived)[:n or int(b["n_photos"] or 0)]}
        new = [p for p in photos if p.name not in taken and f"{b['id']}:{p.name}" not in seen]
        seen.update(f"{b['id']}:{p.name}" for p in new)
        if not new:
            continue
        items = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (b["id"],)).fetchall()
        if b["status"] == "failed" and not int(b["n_photos"] or 0):
            back = s.path("inbox") / f"{Path(dest).name.split('_', 1)[-1]}-late"
            try:
                shutil.move(dest, str(back))
            except OSError as e:
                db.log(b["id"], "late_photos_failed", str(e))
                continue
            db.log(b["id"], "late_photos", {"photos": len(photos), "to": back.name, "how": "taken again as a new share"})
            notify.say(f"📸 {len(photos)} photos came late into a share that had none — taken again now ({back.name})")
            done.append(f"{b['id']}: retaken")
            continue
        names = ", ".join(p.name for p in new[:6])
        title = (_title_of(items[0]) if len(items) == 1 else f"batch {b['id']}")
        db.log(b["id"], "late_photos", {"photos": [p.name for p in new], "items": [it["id"] for it in items]})
        notify.say(f"📸 {len(new)} photos of {title} came after it was taken ({names}) — the listing doesn't have them")
        done.append(f"{b['id']}: {len(new)} late")
    db.kv_set("late_photos_seen", json.dumps(sorted(seen)))
    return done


def registered_names(db: DB, bid: str) -> set[str] | None:
    """The photo files a share's items were made from (WO37): the ones its preparation read (`batch_prepped` — live,
    the Zadig boots: registered with 5, prepared with 7), else the ones it was registered with; None for a share from
    before they were recorded."""
    for kind in ("batch_prepped", "batch_registered"):
        for e in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                                 (bid, kind)):
            names = (loads(e["detail"]) or {}).get("names")
            if isinstance(names, list):
                return set(names)
    return set() if (db.batch(bid) or {"n_photos": 1})["n_photos"] == 0 else None


def _title_of(it) -> str:
    posh = (loads(it["renders"]) or {}).get("poshmark") or {}
    return posh.get("title") or it["id"]


def _icloud_wait(db: DB, waiting: dict[str, int]) -> None:
    """Remember since when each share waits for iCloud; tell the owner once per share after ICLOUD_TELL_AFTER."""
    known = loads(db.kv_get(ICLOUD_WAIT)) or {}
    stamp = datetime.now(timezone.utc)
    state = {}
    for folder, n in waiting.items():
        st = dict(known.get(folder) or {"since": stamp.isoformat(timespec="seconds"), "told": False})
        if not st["told"] and stamp - datetime.fromisoformat(st["since"]) >= ICLOUD_TELL_AFTER:
            notify.say(ICLOUD_MESSAGE)
            db.log(None, "icloud_waiting", {"folder": Path(folder).name, "files": n, "since": st["since"]})
            st["told"] = True
        state[folder] = st
    if state != known:
        db.kv_set(ICLOUD_WAIT, json.dumps(state))


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
    db.log(bid, "batch_registered", {"folder": folder.name, "photos": len(photos),
                                     "names": sorted(p.name for p in photos)})   # WO37: what came late is told apart
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
    db.log(bid, "batch_prepped", {"names": sorted(p.name for p in raw)})   # WO37: the photos it really took
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
    screenshots: list[int] = []
    if len(kept) == 1:
        groups, reasons, summaries, unassigned = [[0]], [], ["single photo"], []
    else:
        out = seg.segment(list(zip(kept, kept_times)), s["models"]["segment"], s["images"]["thumb_long_edge"],
                          kinds=kept_kinds, breaks=breaks, fallback_px=s["images"].get("thumb_fallback_long_edge"),
                          max_bytes=int(cfg.get("max_request_mb", 20) * 1_000_000), report=report)
        groups = [g.photos for g in out.groups]
        summaries = [g.summary for g in out.groups]
        unassigned = list(out.unassigned)                 # screenshots the model could not match to an item
        screenshots = list(out.screenshots)
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

    if cfg.get("auto_confirm", True):
        # The owner never confirms the grouping (owner decision, WO20b): it is taken as her "ok" at once and no contact
        # sheet goes out. The doubts stay in batches.reasons (thrift status). [Wrong photos] on a price card reopens it.
        if (accepted := accept_grouping(groups, len(kept), kept_kinds, unassigned, screenshots)) is not None:
            groups, left_out, notes = accepted
            segd = {**(loads(db.batch(bid)["segmentation"]) or {}), "auto_accepted": True}
            db.set_batch(bid, segmentation=segd, reasons=reasons + notes)
            db.log(bid, "grouping_accepted", {"reasons": reasons, "left_out": left_out})
            split(s, db, bid, groups, dropped=left_out)
            return
        # Not a partition (a photo in no item, or in two): taking it as it is would lose or double a photo, so the
        # contact sheet asks, as before.
    if reasons or cfg["always_confirm"] or cfg.get("auto_confirm", True):
        db.set_batch(bid, status="needs_confirm")
        approve.pump(s, db)                               # the contact sheet, when it is next (one question at a time)
        return
    split(s, db, bid, groups)


def accept_grouping(groups: list[list[int]], n: int, kinds: list[str], unassigned: list[int] | tuple[int, ...] = (),
                    screenshots: list[int] | tuple[int, ...] = ()) -> tuple[list[list[int]], list[int], list[str]] | None:
    """The model's grouping as the owner's "ok" (WO20b): (groups, left out, notes), or None when it can't be taken as
    it is. A retail screenshot that matches no item (the model's `unassigned`, or a group of nothing but screenshots)
    is left out — it isn't a photo of any item, so nothing is lost. Empty groups are dropped. Anything else must be a
    partition of the photos: a photo of the owner's in no item, or in two, is never guessed (None: the contact sheet
    asks)."""
    retail = {i for i, k in enumerate(kinds) if k == "retail"} | set(screenshots)
    groups = [list(g) for g in groups if g]
    screens_only = [g for g in groups if all(i in retail for i in g)]
    left_out = sorted(set(unassigned) | {i for g in screens_only for i in g})
    groups = [g for g in groups if g not in screens_only]
    if not groups or partition_problems(groups, n, left_out):
        return None
    return groups, left_out, [f"screenshot {i} matches no item: left out" for i in left_out]


def confirm(s: Settings, db: DB, bid: str, cmd: str) -> None:
    b = db.batch(bid)
    if b is not None and b["status"] == "regroup":         # [Wrong photos]: the fix of a batch already split
        regroup(s, db, bid, cmd)
        return
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
        fill_item_dir(d, g, photos, kinds)
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
                   f"{len(groups)} items ({', '.join(iids)}). Reply to the right item's card with it")
    archive_share(s, db, bid, Path(b["src_dir"]))


def fill_item_dir(d: Path, group: list[int], photos: list[Path], kinds: list[str]) -> None:
    """An item's photos/ and its manifest from the batch's photos: the owner's own first (capture order), retail
    screenshots last. Whatever was there before is replaced (a failed attempt, or the photos before a fix)."""
    if (d / "photos").exists():
        shutil.rmtree(d / "photos")
    (d / "photos").mkdir(parents=True)
    ordered = [i for i in group if kinds[i] != "retail"] + [i for i in group if kinds[i] == "retail"]
    for j, idx in enumerate(ordered):
        shutil.copy2(photos[idx], d / "photos" / f"{j:02d}.jpg")
    (d / MANIFEST).write_text(json.dumps([{"file": f"{j:02d}.jpg", "kind": kinds[idx], "src": idx}
                                          for j, idx in enumerate(ordered)], indent=1), encoding="utf-8")


def item_group(it) -> list[int]:
    """The batch photos an item was made of (its manifest's `src`), in capture order."""
    manifest = Path(it["dir"]) / MANIFEST
    entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    return sorted(e["src"] for e in entries if "src" in e)


def reached_site(db: DB, it) -> list[str]:
    """Why an item counts as on the marketplace — posting, posted, drafted, a post with a URL, an unconfirmed
    publish — in words ("poshmark posted"); empty when it never got there."""
    posts = db.conn.execute("SELECT * FROM listings WHERE item_id=?", (it["id"],)).fetchall()
    why = [f"{p['marketplace']}: unconfirmed publish" if unconfirmed_publish(p) else f"{p['marketplace']} {p['status']}"
           for p in posts if p["status"] in REDO_KEEP or p["url"] or unconfirmed_publish(p)]
    return why or ([it["status"]] if it["status"] in REDO_KEEP else [])


REGROUP_SHEET = "regroup_sheet.png"


def start_regroup(s: Settings, db: DB, iid: str) -> str:
    """[Wrong photos] on an item's price card (WO20b): its batch's grouping is reopened. The batch goes to 'regroup',
    its contact sheet as it is now (the items' photos) is drawn, and it becomes the one open message in the queue,
    answered with the usual 12>2 / split 7 / merge 2 3 / drop 7 (or ok: nothing changes). Until then none of the
    batch's items is asked about, processed or posted. Never for an item that is on the marketplace. Returns the
    batch id."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if why := reached_site(db, it):
        raise ValueError(f"item {iid} is already on the marketplace ({', '.join(why)}) — its photos can't change")
    b = db.batch(it["batch_id"])
    if b is None or b["status"] != "split":
        raise ValueError(f"batch {it['batch_id']} is {b['status'] if b else 'gone'} — its photos can't be regrouped now")
    segd = loads(b["segmentation"]) or {}
    photos = [Path(p) for p in segd.get("photos") or []]
    items = db.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY seq", (b["id"],)).fetchall()
    if not photos or not all(p.exists() for p in photos):
        raise ValueError(f"batch {b['id']}: its photos are no longer on disk — regroup by hand")
    breaks = {int(i): float(sec) for i, sec in segd.get("pauses") or []}
    seg.contact_sheet(photos, [item_group(i) for i in items], s.path("work") / b["id"] / REGROUP_SHEET,
                      kinds=segd.get("kinds"), breaks=breaks)
    with db.tx():
        db.set_batch(b["id"], status="regroup")
        db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref IN (SELECT id FROM items WHERE batch_id=?) "
                        "AND resolved_at IS NULL", (now(), b["id"]))
        db.log(b["id"], "regroup_asked", {"item": iid})
    return b["id"]


def regroup(s: Settings, db: DB, bid: str, cmd: str) -> dict[str, list[str]]:
    """The owner's fix for a reopened batch (WO20b): the correction is applied to the items' photos as they are now.
    An item whose photos are unchanged keeps everything (its price, its place); an item whose photos changed is
    rebuilt like `thrift redo` (same id, new photos, processed again; the owner's condition and Girls/Boys answers
    kept, the price asked again); a new group becomes a new item; an item whose photos all went elsewhere is removed.
    Changed items pair with the old item they share the most photos with. Refused, with nothing changed, when it
    would change an item that is on the marketplace. "ok" changes nothing. Returns {"kept", "rebuilt", "created",
    "removed"}: item ids."""
    b = db.batch(bid)
    if b is None or b["status"] != "regroup":
        raise ValueError(f"batch {bid} isn't waiting for a photo fix")
    segd = loads(b["segmentation"]) or {}
    photos = [Path(p) for p in segd["photos"]]
    n = len(photos)
    kinds = segd.get("kinds") or ["own"] * n
    items = db.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY seq", (bid,)).fetchall()
    current = [item_group(it) for it in items]
    left_out = sorted(set(range(n)) - {i for g in current for i in g})
    rest, drops = seg.parse_drops(cmd)
    if bad := [i for i in drops if not 0 <= i < n]:
        raise ValueError(f"can't drop {bad}: photos are 0..{n - 1}")
    groups = [sorted(g) for g in seg.apply_correction([[i for i in g if i not in drops] for g in current], rest, n=n)]
    dropped = sorted((set(left_out) - {i for g in groups for i in g}) | set(drops))
    if problems := partition_problems(groups, n, dropped):
        raise ValueError(f"batch {bid}: " + "; ".join(problems))

    old = {frozenset(g): it for g, it in zip(current, items)}
    kept = {k: old[frozenset(g)] for k, g in enumerate(groups) if frozenset(g) in old}
    free = [(it, set(g)) for it, g in zip(items, current) if it["id"] not in {i["id"] for i in kept.values()}]
    rebuilt: dict[int, object] = {}
    for k, g in enumerate(groups):                       # a changed group: the old item it overlaps most, if any
        if k in kept:
            continue
        best = max(free, key=lambda f: (len(f[1] & set(g)), -f[0]["seq"]), default=None)
        if best is not None and best[1] & set(g):
            rebuilt[k] = best[0]
            free.remove(best)
    removed = [it for it, _ in free]
    for it in [*rebuilt.values(), *removed]:
        if why := reached_site(db, it):
            raise ValueError(f"item {it['seq']} ({it['id']}) is already on the marketplace ({', '.join(why)}) — its "
                             "photos can't change; fix only the others")

    round_no = int(segd.get("regroups") or 0) + 1
    new_dirs = {}
    for k, g in enumerate(groups):
        if k in rebuilt:
            fill_item_dir(Path(rebuilt[k]["dir"]), g, photos, kinds)
        elif k not in kept:
            new_dirs[k] = s.path("work") / bid / f"item_{k + 1:02d}_fix{round_no}"
            fill_item_dir(new_dirs[k], g, photos, kinds)
    out = {"kept": [], "rebuilt": [], "created": [], "removed": []}
    with db.tx():
        for it in removed:
            db.conn.execute("DELETE FROM listings WHERE item_id=?", (it["id"],))
            db.conn.execute("DELETE FROM items WHERE id=?", (it["id"],))
            db.log(it["id"], "regroup_removed", {"batch": bid})
            out["removed"].append(it["id"])
        for k, g in enumerate(groups):
            if k in kept:
                db.set_item(kept[k]["id"], seq=k + 1)
                out["kept"].append(kept[k]["id"])
            elif k in rebuilt:
                iid = rebuilt[k]["id"]
                db.conn.execute("DELETE FROM listings WHERE item_id=?", (iid,))
                db.set_item(iid, seq=k + 1, status="new", facts=None, price=None, renders=None, gate=None,
                            owner_price=None, deferred_at=None, cover_hash=None)
                db.log(iid, "regroup_rebuilt", {"batch": bid, "photos": g})
                out["rebuilt"].append(iid)
            else:
                iid = db.add_item(bid, k + 1, str(new_dirs[k]))
                db.log(iid, "item_created", {"batch": bid, "photos": g, "regroup": round_no})
                out["created"].append(iid)
        changed = {i for ids in (out["rebuilt"], out["removed"]) for i in ids}
        if changed:
            db.conn.execute(f"UPDATE outbox SET resolved_at=? WHERE ref IN ({','.join('?' * len(changed))}) "
                            "AND resolved_at IS NULL", (now(), *changed))
        db.set_batch(bid, status="split", segmentation={**segd, "regroups": round_no})
        db.log(bid, "regrouped", {"cmd": cmd, **out})
    return out


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
            return str(h), (f"looks like item {row['id']} ({title}) — same item? Reply 'same item' to drop it; "
                            f"a price lists it (WO37: a warning, never a hold)")
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


CATEGORY_SURE = 0.70              # how sure of its category the model must be to go without asking (WO25)
NO_BRAND = "none"                 # items.owner_brand: the owner said the item has no brand (WO25)
NO_BRAND_WORDS = re.compile(r"\b(?:no[\s-]*brand|unbranded|un-branded|no[\s-]*label|without (?:a )?brand)\b", re.I)


def category_question(facts: Facts, fit_questions: list[str], owner_category: str | None) -> list[dict]:
    """The options of "Which category?" (WO25): 1-3 real Poshmark paths (taxonomy.category_options), offered only when
    the model is under 0.70 sure of its category, or gave one Poshmark doesn't have (or "Other"); [] when nothing is
    asked — always once the owner answered."""
    if owner_category:
        return []
    unsure = facts.category_confidence is not None and facts.category_confidence < CATEGORY_SURE
    other = facts.category.strip().lower() in ("", "other") or (facts.subcategory or "").strip().lower() == "other"
    return taxonomy.category_options(facts) if unsure or other or fit_questions else []


WAIST = re.compile(r"(?:W\s*)?(2[3-9]|3[0-4])", re.I)
WAIST_CATEGORIES = ("jeans", "pants & jumpsuits", "shorts")


def settle_waist(facts: Facts) -> Facts:
    """WO37 (live: FRAME jeans labelled "24" went up as "size 00", the model's conversion): jeans, pants and shorts
    whose label prints a waist (23-34) are listed in it — Poshmark's menus have the waists, buyers search them — unless
    the owner gave the size."""
    printed = (facts.size_printed.value or "").strip()
    m = WAIST.fullmatch(printed)
    if (not m or (facts.category or "").strip().lower() not in WAIST_CATEGORIES or facts.size_us.source == "owner"
            or (facts.size_us.value or "").strip() == m[1]):
        return facts
    ev = Ev(value=m[1], photos=facts.size_printed.photos, source="photo", confidence=facts.size_printed.confidence)
    return facts.model_copy(update={"size_us": ev})


LOAFERS = re.compile(r"\b(loafers?|moccasins?|ballet flats?|flats|penny|driving shoes?)\b", re.I)
LOAFER_HOME = {"Women": "Flats & Loafers", "Men": "Loafers & Slip-Ons"}


def settle_shoe_kind(facts: Facts, owner_category: str | None) -> Facts:
    """WO37 (live: Tory Burch block-heel loafers went up under Heels, Depop "Pumps"): shoes the item type calls loafers,
    moccasins or flats are listed under Poshmark's Flats & Loafers (Men: Loafers & Slip-Ons), whatever their heel —
    unless the owner chose the category."""
    home = LOAFER_HOME.get(facts.department or "")
    if (owner_category or not home or facts.category != "Shoes" or facts.subcategory not in ("Heels", "Pumps")
            or not LOAFERS.search(facts.item_type or "")):
        return facts
    return facts.model_copy(update={"subcategory": home})


def settle_open_questions(facts: Facts, fit_questions: list[str], owner_category: str | None) -> tuple[Facts, list[str]]:
    """WO37 (the owner): nothing but the price waits — what used to be a question is decided here and shown on the card,
    and a reply corrects it. Girls or Boys for a kids size: the model's reading, else Girls (Poshmark's kids clothing
    default). The category: the model's own when Poshmark has it, else the first real path that fits (a set: its
    bottom, settle_set). Returns the facts and what was decided (the item's record)."""
    said = []
    if kids_question(facts):
        gender = facts.kids_gender if facts.kids_gender in ("girls", "boys") else "girls"
        facts = facts.model_copy(update={"kids_gender": gender})
        said.append(f"kids: {gender} (not sure from the photos) — a reply 'boys' / 'girls' changes it")
    options = category_question(facts, fit_questions, owner_category)
    if options:
        own = taxonomy.place(facts, facts.department, facts.category, facts.subcategory)
        path = own or options[0]
        facts = facts.model_copy(update={"department": path.get("department") or facts.department,
                                         "category": path["category"], "subcategory": path.get("subcategory")})
        said.append(f"category: {taxonomy.path_label(path, facts.department)} (not sure from the photos) — a reply "
                    "'category …' changes it")
    return facts, said


def category_unsure(facts: Facts) -> list[str]:
    """The gate's reason when the model's own doubt is what asks "Which category?"."""
    sure = facts.category_confidence
    return [f"category unsure ({sure:.2f})"] if sure is not None and sure < CATEGORY_SURE else []


def size_question(s: Settings, facts: Facts, choice: tuple[str, str] | None) -> str | None:
    """WO33, the owner's rule: never a question about size — always None now (a size off Poshmark's menus is a note,
    size_menu_note; the poster takes the nearest value, WO27). Kept for the callers' sake."""
    return None


def size_menu_note(facts: Facts, choice: tuple[str, str] | None) -> str | None:
    """A size read that is on none of the category's size menus (WO25): no question (WO33) — the card shows it and the
    poster takes the nearest value of the menu; a reply 'size …' changes it."""
    menus = taxonomy.size_menus(facts.department, facts.category, facts.subcategory)
    value = facts.size_us.value
    if choice or not menus or not value:
        return None
    return (f"{SIZE_NOTE} {value} — not on Poshmark's {facts.department} {facts.category} size list "
            f"({', '.join(menus)}): the nearest is used")


def owner_answers(it) -> list[str]:
    """The owner's answers the extraction must follow, as lines of its seller note (WO25): the category picked on
    "Which category?", "no brand"."""
    lines = []
    if it["owner_category"] and (path := loads(it["owner_category"])):
        lines.append(f"Category (the owner's answer): {taxonomy.path_label(path)}")
    if it["owner_brand"] == NO_BRAND:
        lines.append("Brand: none — the owner says the item has no brand")
    elif it["owner_brand"]:
        lines.append(f"Brand (the owner's answer): {it['owner_brand']}")
    if pieces := owner_sizes(it):
        lines.append("Sizes of the set's pieces (the owner's answer): "
                     + ", ".join(f"{p.piece} {p.size}" for p in pieces))
    if size := owner_size(it):
        lines.append(f"Size (the owner's answer): {size}")
    return lines


def owner_size(it) -> str | None:
    """The size the owner gave for the item (items.owner_size, WO33: a reply 'size S'), else None."""
    return (it["owner_size"] if "owner_size" in it.keys() else None) or None


def owner_sizes(it) -> list[PieceSize]:
    """The set's pieces and sizes the owner gave (items.owner_sizes, WO33), else none."""
    raw = it["owner_sizes"] if "owner_sizes" in it.keys() else None
    return [PieceSize(piece=p["piece"], size=p["size"]) for p in (loads(raw) or []) if p.get("piece") and p.get("size")]


def apply_owner_answers(it, facts: Facts) -> Facts:
    """The owner's category and "no brand" over the model's reading (WO25): source owner, never asked again."""
    update: dict = {}
    if it["owner_category"] and (path := loads(it["owner_category"])):
        update.update(department=path["department"], category=path["category"], subcategory=path.get("subcategory"),
                      category_confidence=1.0, category_alternatives=[])
    if it["owner_brand"] == NO_BRAND:
        update["brand"] = Ev(value=None, photos=[], source="owner", confidence=1.0)
    elif it["owner_brand"]:                              # the owner's spelling (WO27): "J. Crew"
        update["brand"] = Ev(value=it["owner_brand"], photos=facts.brand.photos, source="owner", confidence=1.0)
    if pieces := owner_sizes(it):                        # the set's pieces, the owner's sizes (WO33)
        update["piece_sizes"] = pieces
        update["size_us"] = Ev(value=sizes.bigger_size([p.size for p in pieces]), photos=facts.size_us.photos,
                               source="owner", confidence=1.0)
    if size := owner_size(it):                           # her size for the item (WO33: a reply 'size S'): it wins
        update["size_us"] = Ev(value=size, photos=facts.size_us.photos, source="owner", confidence=1.0)
    return facts.model_copy(update=update) if update else facts


_PIECE_IN_VALUE = re.compile(r"([A-Za-z0-9./]+)\s*\(([A-Za-z][\w' -]{1,30})\)")


def _pieces_in(value: str | None) -> list[PieceSize]:
    """The pieces a size reading names itself — live: "S (cardigan), XS (pants)" — else none."""
    found = _PIECE_IN_VALUE.findall(value or "")
    return [PieceSize(piece=piece.strip(), size=size) for size, piece in found] if len(found) >= 2 else []


def settle_set_size(facts: Facts) -> Facts:
    """A set whose pieces carry different sizes is listed under the bigger one (WO33, the owner's rule) — the size
    field and the title — with no size question (source derived, 0.95); the description says which piece is which
    (copy.ensure_piece_sizes). The owner's own size stands. Nothing for one garment or one shared size."""
    pieces = [p for p in facts.piece_sizes if p.size.strip()] or _pieces_in(facts.size_us.value)
    if len(pieces) < 2 or facts.size_us.source == "owner":
        return facts
    size = sizes.bigger_size([p.size for p in pieces])
    if size is None:
        return facts
    photos = sorted({i for p in pieces for i in p.photos} | set(facts.size_us.photos))
    return facts.model_copy(update={"size_us": Ev(value=size, photos=photos, source="derived", confidence=0.95),
                                    "piece_sizes": pieces})


# A set of two garments goes under its bottom (WO27, the owner's rule made code): pants -> Pants & Jumpsuits, skirt ->
# Skirts > Skirt Sets, shorts -> Shorts; Kids -> Matching Sets. Never for pajamas, swimwear or lingerie sets.
_SET_TOPS = r"top|tee|t-shirt|shirt|blouse|cami|camisole|tank|bralette|bandeau|corset|bustier|crop|sweater|cardigan|" \
            r"jacket|blazer|hoodie|sweatshirt|vest"
_SET_NOT = re.compile(r"\b(?:pajamas?|pyjamas?|sleep\w*|bikini|swim\w*|lingerie|bra|underwear|robe)\b", re.I)
_SET_BOTTOMS = (("skirts?|skorts?", "Skirts", "Skirt Sets"), ("shorts", "Shorts", None),
                ("pants|trousers|jeans|joggers|leggings|sweatpants|culottes|palazzos?", "Pants & Jumpsuits", None))
_PANTS_SUBS = (("wide", "Wide Leg"), ("flared?|flares|bootcut|boot[- ]cut|bell", "Boot Cut & Flare"),
               ("joggers?|track|sweatpants", "Track Pants & Joggers"), ("leggings?", "Leggings"),
               ("straight", "Straight Leg"), ("skinny", "Skinny"), ("cropped|ankle", "Ankle & Cropped"),
               ("trousers?", "Trousers"))


def settle_set(facts: Facts, owner_category: str | None = None) -> Facts:
    """A two-piece set's category by its bottom, in code, so no "Which category?" comes for a set (WO27): a top and
    pants -> Pants & Jumpsuits (the subcategory the pants' words name: flared -> Boot Cut & Flare, wide -> Wide Leg),
    a top and a skirt -> Skirts > Skirt Sets, a top and shorts -> Shorts; Kids -> Matching Sets. The owner's category
    wins; pajama, swim and lingerie sets are left to the model."""
    text = facts.item_type or ""
    two = (facts.set_pieces or 0) >= 2 or (re.search(r"\bset\b", text, re.I)
                                           and re.search(rf"\b(?:{_SET_TOPS})s?\b", text, re.I))
    if owner_category or not two or _SET_NOT.search(text):
        return facts
    if facts.department == "Kids":
        category, sub = "Matching Sets", None
    else:
        hit = next(((c, s) for pat, c, s in _SET_BOTTOMS if re.search(rf"\b(?:{pat})\b", text, re.I)), None)
        if hit is None:
            return facts
        category, sub = hit
        if category != "Skirts":
            subs = ((taxonomy.load()["departments"].get(facts.department) or {}).get("categories") or {}).get(category)
            sub = facts.subcategory if facts.category == category and facts.subcategory in (subs or []) else \
                next((s for pat, s in _PANTS_SUBS if re.search(rf"\b(?:{pat})\b", text, re.I)), None) \
                if category == "Pants & Jumpsuits" else None
    return facts.model_copy(update={"category": category, "subcategory": sub, "category_confidence": 1.0,
                                    "category_alternatives": [], "set_pieces": facts.set_pieces or 2})


def known_brands(tiers: dict | None = None) -> list[str]:
    """The brand names the price table knows (its brands and their aliases): lint flags one in a listing whose facts
    have no brand (WO25: the description never invents a brand)."""
    tiers = load_yaml("brand_tiers.yaml") if tiers is None else tiers
    return sorted({*(tiers.get("brands") or {}), *(tiers.get("aliases") or {})}, key=len, reverse=True)


def process_item(s: Settings, db: DB, iid: str) -> dict:
    """extract -> price -> copy -> verify -> lint -> gate -> renders. The item then waits for the owner's price
    (awaiting_price, ONE Telegram message) unless the owner already priced it and nothing is unresolved.

    An item that isn't 'new' (`thrift reprocess`, WO25) is processed in place: it keeps waiting meanwhile, so the card
    the owner has stays open, and that card is sent again only when what it shows changed (as `thrift recover`); an
    answer that lands meanwhile wins (ValueError, nothing written). Returns {"status", "card"}."""
    it = db.item(iid)
    in_place = it["status"] != "new"
    d = Path(it["dir"])
    was = (approve.card(iid, it), _digest(d / "cover.jpg")) if in_place else None
    photos = sorted((d / "photos").glob("*.jpg"))
    kinds = photo_kinds_of(d, photos)
    retail = {i for i, k in enumerate(kinds) if k == "retail"}
    note = "; ".join(x for x in (it["note"], *owner_answers(it)) if x) or None
    facts = extract(photos, note, s["models"]["extract"], s["images"]["llm_long_edge"], kinds=kinds)
    facts = strip_screenshot_evidence(facts, retail)     # a screenshot is never evidence for condition, size or flaws
    if facts.condition_evidence.source == "owner":       # only the owner's answer is source owner, never the model
        facts.condition_evidence.source = "photo"
    if facts.brand.source == "owner":
        facts.brand.source = "note"
    facts = facts.model_copy(update={"premium": None})   # the close label read below sets it, never the extraction
    spelled = brands.for_settings(s).alias(facts.brand.value)    # Poshmark's spelling (WO27): "J.Crew" -> "J. Crew" —
    if spelled and brands.spelling_of(facts.brand.value, spelled):  # another spelling only: a short form stays the
        facts = facts.model_copy(update={"brand": facts.brand.model_copy(update={"value": spelled})})   # label's (WO36)
    facts = apply_owner_answers(it, facts)               # the owner's category, brand or "no brand"
    # Shoes, in doubt between brand new and worn: the owner is asked before the price (WO18) — on the model's own
    # reading, before the settles below merge its two grades. Once answered, the answer is the condition.
    # WO37: never "Brand new or worn?" — NWT only with tags (settle_nwt); in doubt Like New, never Fair (settle_condition)
    facts, nwt_questions = settle_nwt(facts, it["note"])   # NWT needs a tag photo or the owner's word; else like new
    facts, notes = settle_condition(facts)               # doubt -> like new; never Fair (Good, and a warning)
    if it["owner_condition"]:                            # the owner's word settles it all
        facts, notes, nwt_questions = apply_owner_condition(facts, it["owner_condition"]), [], []
    if it["owner_kids_gender"]:                          # the owner's [Girls]/[Boys]: never asked again
        facts = facts.model_copy(update={"kids_gender": it["owner_kids_gender"], "kids_gender_confidence": 1.0})
    facts = settle_set(facts, it["owner_category"])      # a set goes under its bottom, no question (WO27)
    facts = settle_set_size(facts)                       # pieces of different sizes: the bigger one (WO33)
    facts = settle_waist(facts)                          # a waist on the label is the size, not its conversion (WO37)
    facts, fit_notes, fit_questions = taxonomy.fit(facts)   # Poshmark's own category names (Kids Tops -> Shirts & Tops)
    facts = settle_shoe_kind(facts, it["owner_category"])  # loafers are Flats & Loafers, never Heels (WO37)
    facts = settle_kids_size(s, facts, photos)            # a kids label's cm or age -> Poshmark's size, no question
    facts, decided = settle_open_questions(facts, fit_questions, it["owner_category"])   # WO37: decided, not asked
    if any(x.startswith("category:") for x in decided):
        fit_questions = []                               # the category was decided: nothing left to ask about it
    ask_kids, options = False, []
    kept = _stored_check(it)
    kept = kept if labelled(kept) and {v.photo for v in kept.views} >= {i for i in range(len(photos))
                                                                       if kinds[i] != "retail"} else None
    check = kept or front_view(s, db, iid, photos, facts, kinds)   # every photo labelled (WO37), once per item
    cover, upright, cover_note = choose_cover(facts, len(photos), kinds, check, it["owner_cover"])
    if cover is not None:
        before = loads(it["facts"]) or {} if it["facts"] else {}
        if kept is not None and before.get("cover_photo") == cover and before.get("cover_upright") is not None:
            upright = before["cover_upright"]            # the same photo, turned upright before: kept
        else:
            upright = upright_view(s, db, iid, photos[cover], upright)   # turned upright: four pictures, pick one
        facts = facts.model_copy(update={"cover_photo": cover, "cover_upright": upright})
    if labelled(check):
        facts = facts.model_copy(update={"photo_order": labelled_order(check, len(photos))})
    facts = premium.merge(facts, label_view(s, db, iid, photos, facts, kinds), len(photos))   # the labels (WO26)
    shown = listing_photos(s, facts, len(photos), kinds)  # every flaw photo in the listing
    # The card shows only what needs the owner (WO20): these warnings and the allowed questions; the rest (an unsure
    # grade, a subcategory left out) is kept in the item's record as info.
    notes += flaw_notes(facts, shown) + ([cover_note] if cover_note else [])
    info = list(fit_notes)
    tiers, pcfg = load_yaml("brand_tiers.yaml"), premium.config()
    pr = price(facts, tiers, s["pricing"], it["note"], premium.price_factor(facts, pcfg))
    enabled = [mp for mp, m in s["marketplaces"].items() if m.get("enabled")]
    if it["owner_price"]:
        pr = owner_priced(pr, int(it["owner_price"]), enabled)   # approved earlier; a reprocessing never asks again
    draft = copywriter.write(facts, s["models"]["copy"], s["copy"])
    audit = verify(facts, draft, s["models"]["verify"])
    final = copywriter.clean(CopyOut(
        poshmark_title=audit.poshmark_title, poshmark_description=audit.poshmark_description,
        poshmark_style_tags=[], depop_description=audit.depop_description,
        depop_hashtags=draft.depop_hashtags, short_name=draft.short_name, brand=draft.brand,
        brand_label=draft.brand_label))
    shop, _label = shop_brand(facts, final.brand, brands.for_settings(s))   # WO36: "Alo Yoga" for an "alo" label
    tfacts = title_facts(facts, shop)                    # the title and its checks name the brand as shops list it
    final = shop_named(final, facts.brand.value, shop)   # the copy too: "MNG Knit Cardigan" → "Mango Knit Cardigan"
    final = copywriter.condition_rule(final, facts)      # wear is shown in the photos, never put in words
    final.poshmark_title = copywriter.ensure_set_title(final.poshmark_title, facts)   # "… 2-Piece Set size M"
    final.poshmark_title = (it["owner_title"] or                 # the owner's own title stays as it is (WO27)
                            premium.title_with_feature(copywriter.ensure_title_size(
                                copywriter.fix_title(final.poshmark_title, tfacts), tfacts),
                                tfacts, pcfg))   # fixed (WO33), the listing's size; brand first, the feature
    final.poshmark_description = premium.ensure_feature_lines(final.poshmark_description, facts, pcfg)
    final.depop_description = premium.ensure_feature_lines(final.depop_description, facts, pcfg)
    final.poshmark_description = copywriter.ensure_retail_line(final.poshmark_description, facts)
    final.poshmark_description = copywriter.ensure_label_size(final.poshmark_description, facts)   # "104 cm / 4 ans"
    final.depop_description = copywriter.ensure_label_size(final.depop_description, facts)
    final.poshmark_description = copywriter.ensure_piece_sizes(final.poshmark_description, facts)  # "Pants: size XS."
    final.depop_description = copywriter.ensure_piece_sizes(final.depop_description, facts)
    final.poshmark_style_tags = []                       # none, ever (the owner, 2026-10-12: "Coord Sets" deleted)
    problems = lint(tfacts, final, shown, known_brands(tiers))
    if not audit.unsupported:
        # The gate only sees the verifier's self-reported count. A verifier that rewrites the text but reports
        # nothing would otherwise publish an LLM-rewritten listing that nobody reviewed.
        problems += [f"verifier rewrote {f} without reporting a claim" for f in copywriter.changed_fields(draft, audit)]
    gate = evaluate(facts, pr, problems, len(audit.unsupported), s["gate"], s["pricing"],
                    sized=not sizes.one_size(facts.department, facts.category, facts.subcategory))

    questions = fit_questions + gate.questions + nwt_questions
    asked = list(fit_questions)                          # a department/category Poshmark doesn't have: like "Other"
    if menu_note := size_menu_note(facts, sizes.poshmark_size(facts)):
        info.append(menu_note)                           # off Poshmark's menus: shown, never asked (WO33)
    if options:                                          # asked with buttons, its own message: not on the card too
        questions = [q for q in questions if q not in fit_questions and q != OTHER_CATEGORY_QUESTION]
        asked += category_unsure(facts)
    if asked:
        gate = GateResult("needs_info", asked + gate.reasons, gate.notes, gate.questions)

    renders = build_renders(s, iid, d, photos, facts, final, pr, kinds)
    cover_src = photos[photo_order(facts, len(photos), kinds)[0]]
    cover_hash, twin = duplicate_check(s, db, iid, cover_src, it["note"])
    if twin:
        gate = GateResult("needs_info", [twin] + gate.reasons, gate.notes, gate.questions)
        questions = [twin] + questions
    # `hold` marks a question a price alone must not settle: set_price() keeps a held item waiting until the owner
    # answers it ("different item" lists it, "same item" drops it). `notes` are told to the owner but need no answer.
    gate_doc = {"decision": gate.decision, "reasons": gate.reasons, "questions": questions, "notes": notes,
                "info": info + gate.notes + decided, "hold": None, "ask_kids": ask_kids,   # WO37: only the price waits
                "ask_category": options}
    (d / "item.json").write_text(json.dumps({
        "facts": facts.model_dump(), "price": pr.model_dump(),
        "renders": {k: v.model_dump() for k, v in renders.items()},
        "gate": gate_doc,
        "unsupported_removed": [u.model_dump() for u in audit.unsupported],
    }, indent=2), encoding="utf-8")
    # Nothing publishes without the owner's price. Open questions (an unreadable brand or size, a possible
    # re-share) ride along in the same message; an item the owner already priced comes back only while something
    # is still unresolved.
    status = "awaiting_price" if not it["owner_price"] else "ready"   # WO37: the price is the only thing that waits
    row = {k: v.model_dump() for k, v in renders.items()}
    new_card = approve.card(iid, {**dict(it), "status": status, "facts": json.dumps(facts.model_dump()),
                                  "price": json.dumps(pr.model_dump()), "renders": json.dumps(row),
                                  "gate": json.dumps(gate_doc)})
    changed = not in_place or (new_card, _digest(d / "cover.jpg")) != was
    with db.tx():
        latest = db.item(iid)
        stale = latest["updated_at"] != it["updated_at"] and (in_place or latest["status"] == "new")
        if stale:
            # An answer arrived while the item was being processed (the worker's Telegram thread: a note, a condition;
            # or `thrift redo`): this result is already out of date. A 'new' item stays new and is processed again
            # with it; one reprocessed in place keeps the answer.
            db.log(iid, "item_processed_stale", {"would_be": gate.decision})
        else:
            waiting = _open_cards(db, iid)
            if changed:
                # Whatever was asked about this item before is out of date now (a new card, a new question): closed,
                # so the queue sends the new one rather than waiting on the old (WO20). Reprocessed in place and
                # unchanged, the owner's card stays as it is (WO25).
                db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref=? AND resolved_at IS NULL", (now(), iid))
            db.set_item(iid, status=status, facts=facts.model_dump(), price=pr.model_dump(), renders=row,
                        gate=gate_doc, cover_hash=cover_hash, views=check.model_dump() if check else None)
            db.log(iid, "item_processed", {"decision": gate.decision, "reasons": gate.reasons, "status": status,
                                           "cover": facts.cover_photo, "upright": facts.cover_upright,
                                           **({"card": "changed" if changed else "unchanged"} if in_place else {})})
            if cover_note == NO_FULL_COVER:              # WO37: one line to the private chat, never a question
                title = (row.get("poshmark") or {}).get("title") or _title_of(db.item(iid))
                notify.say(f"No full photo of {title} — a full front shot next time")
    if stale:
        if in_place:
            raise ValueError(f"item {iid} changed while it was being reprocessed (an answer came in) — run it again")
        return {"status": "new", "card": None}

    if status in ("awaiting_condition", "awaiting_price"):
        approve.pump(s, db)                              # ONE open question at a time: sent now if it is next
    elif gate.decision == "draft":
        first = next(iter(renders.values()), None)
        title = first.title if first else final.poshmark_title
        notify.say(f"Queued as draft ({iid}): {title} — ${pr.list_price}\n- " + "\n- ".join(gate.reasons))
    card = None if new_card is None else ("sent again" if changed and waiting else "changed" if changed
                                          else "unchanged")
    return {"status": status, "card": card if in_place else "new"}


def _open_cards(db: DB, iid: str) -> int:
    """How many of the item's Telegram questions are still open (the queue's kinds)."""
    marks = ",".join("?" * len(approve.QUEUE_KINDS))
    return db.conn.execute(f"SELECT COUNT(*) FROM outbox WHERE ref=? AND resolved_at IS NULL AND kind IN ({marks})",
                           (iid, *approve.QUEUE_KINDS)).fetchone()[0]


def settle_kids_size(s: Settings, facts: Facts, photos: list[Path] | None = None) -> Facts:
    """Kids clothing (WO23): a label that gives the child's height or age is Poshmark's size by a fixed table
    (sizes.kids_clothing_size: 104 cm -> 4T, 116 cm -> 6, "4 ans" -> 4T …) — settled, never a question. When the
    model read only a bare number ("4") and would be unsure, the label photos are read again, once, for the units."""
    if facts.department != "Kids" or facts.category.strip().lower() == "shoes":
        return facts
    printed = facts.size_printed.value
    size = sizes.kids_clothing_size(printed)
    if (size is None and photos and facts.size_printed.photos and facts.size_us.confidence < s["gate"]["min_confidence"]
            ["size"]):
        label = [photos[i] for i in facts.size_printed.photos if 0 <= i < len(photos)]
        try:
            again = cover_brain.read_size_label(label, s["models"].get("cover") or s["models"]["extract"],
                                                int(s["images"].get("cover_check_long_edge", 1024)))
        except Exception:  # noqa: BLE001 - a second look is a bonus: without it the size is simply asked
            again = None
        if again and (size := sizes.kids_clothing_size(again)) is not None:
            printed = again
    if size is None:
        return facts
    ev = Ev(value=size, photos=facts.size_printed.photos, source="derived", confidence=0.95)
    label = facts.size_printed.model_copy(update={"value": printed})
    return facts.model_copy(update={"size_us": ev, "size_printed": label})


def front_view(s: Settings, db: DB, iid: str, photos: list[Path], facts: Facts, kinds: list[str]) -> FrontOut | None:
    """The front check (brain/cover.py) on the item's photos of the item alone; None without such photos, or when
    the call fails (the cover then follows the extraction's roles, and the failure is logged)."""
    own = [i for i in range(len(photos)) if kinds[i] != "retail"]
    if not own:
        return None
    worn = {r.photo for r in facts.photo_roles if r.role == "worn" and 0 <= r.photo < len(photos)}
    try:     # WO37: every own photo labelled, in shooting order — the extraction's roles can be wrong
        return cover_brain.front_check([(i, photos[i]) for i in own if i not in worn],
                                       s["models"].get("cover") or s["models"]["extract"],
                                       int(s["images"].get("cover_check_long_edge", 1024)),
                                       worn=[(i, photos[i]) for i in own if i in worn])
    except Exception as e:  # noqa: BLE001
        db.log(iid, "front_check_failed", f"{type(e).__name__}: {e}")
        return None


def label_view(s: Settings, db: DB, iid: str, photos: list[Path], facts: Facts, kinds: list[str]) -> Premium | None:
    """The item's labels read closely (brain/labels.py, WO26): its label and tag photos at images.label_long_edge, its
    detail photos at the usual size. An empty read when it has no label photo (nothing to read); None when the call
    fails (logged; recover reads them again)."""
    roles = {r.photo: r.role for r in facts.photo_roles if 0 <= r.photo < len(photos)}
    own = [i for i in range(len(photos)) if kinds[i] != "retail"]
    tags = [(i, photos[i]) for i in own if roles.get(i) in ("label", "tag")]
    if not tags:
        return Premium()
    details = [(i, photos[i]) for i in own if roles.get(i) == "detail"][:3]
    try:
        return labels.read_labels(tags, details, s["models"].get("labels") or s["models"]["extract"],
                                  int(s["images"].get("label_long_edge", 2048)), int(s["images"]["llm_long_edge"]))
    except Exception as e:  # noqa: BLE001
        db.log(iid, "label_read_failed", f"{type(e).__name__}: {e}")
        return None


def upright_view(s: Settings, db: DB, iid: str, photo: Path, fallback: int) -> int:
    """The turn that puts the cover upright, from the four-turn check (brain/cover.py); `fallback` (the front check's
    reading) when that call fails — logged."""
    try:
        return cover_brain.upright_check(photo, s["models"].get("upright") or s["models"].get("cover")
                                         or s["models"]["extract"])
    except Exception as e:  # noqa: BLE001
        db.log(iid, "upright_check_failed", f"{type(e).__name__}: {e}")
        return fallback


def relist(s: Settings, it, facts: Facts, renders: dict) -> dict:
    """The renders with the listing's photos, cover and form fields recomputed from `facts` — no model call: the
    cover file (turned upright), the photo order, category / subcategory / size (with Poshmark's menu value, WO25), the
    brand and the label line; a line break the model wrote as backslash + n is made a line break (WO24); the premium
    details the labels gave (WO26): the title's strongest feature, the description's feature lines, "Original retail
    $…" and the Original Price. Tags and prices stay as they are; the title changes only by its feature."""
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    kinds = photo_kinds_of(d, photos)
    order = photo_order(facts, len(photos), kinds)
    flawed = flaw_photos(facts, len(photos))
    width, height = prep.cover_dims(s["images"]["cover_size"])
    turn = facts.cover_upright if order[0] == facts.cover_photo else 0
    cover = prep.portrait_cover(photos[order[0]], d / "cover.jpg", width, height, rotate=turn)
    tab, value = sizes.poshmark_size(facts) or (None, None)
    pcfg = premium.config()
    old = Facts.model_validate(loads(it["facts"])) if it["facts"] else None
    aliases = brands.for_settings(s)
    out = {}
    for mp, r in renders.items():
        same_label = brands.key(r.get("brand_label") or r.get("brand")) == brands.key(facts.brand.value)
        shop, label = shop_brand(facts, r.get("brand") if same_label else None, aliases)   # kept (WO36)
        tfacts = title_facts(facts, shop)
        limit = int((s["marketplaces"].get(mp) or {}).get("max_photos", len(photos)))
        shown = fit_photos(order, flawed, limit)
        text = premium.ensure_feature_lines(copywriter.unescape_breaks(
            named_as(r.get("description") or "", facts.brand.value, shop)), facts, pcfg,
                                            old=old)
        if mp == "poshmark":
            text = copywriter.ensure_retail_line(text, facts)
        out[mp] = {**r, "photos": [str(cover)] + [str(photos[i]) for i in shown[1:]], "category": facts.category,
                   "subcategory": facts.subcategory, "size": sizes.size_label(facts), "size_tab": tab,
                   "size_value": value, "brand": shop, "brand_label": label, "tags": [] if mp == "poshmark" else
                   r.get("tags") or [],
                   "title": it["owner_title"] or premium.title_with_feature(
                       copywriter.ensure_title_size(named_as(r.get("title") or "", facts.brand.value, shop), tfacts),
                       tfacts, pcfg),
                   "original_price": _dollars(facts.retail_price.value) or r.get("original_price"),
                   "description": copywriter.ensure_piece_sizes(copywriter.ensure_label_size(text, facts), facts)}
    return out


def set_cover(s: Settings, db: DB, iid: str, n: int) -> str:
    """The owner's "cover N" (WO23): photo n of the item (its photos in shooting order, from 0 here; her reply counts
    from 1, WO37 — approve.cover_index) becomes the cover — kept
    through any later reprocessing (items.owner_cover). The listing is rebuilt at once, no model call. Never for an
    item that is on the marketplace. Returns the item's status."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if why := reached_site(db, it):
        raise ValueError(f"item {iid} is already on the marketplace ({', '.join(why)}) — change its cover there")
    if not it["facts"] or not it["renders"]:
        raise ValueError(f"item {iid} is {it['status']} — it has no listing yet")
    count = len(list((Path(it["dir"]) / "photos").glob("*.jpg")))
    if not 0 <= n < count:
        raise ValueError(f"this item has photos 1 to {count} — there is no photo {n + 1}")
    facts = Facts.model_validate(loads(it["facts"]))
    views = FrontOut.model_validate(loads(it["views"])) if it["views"] else None
    _, upright, _ = choose_cover(facts, count, None, views, owner=n)
    photos = sorted((Path(it["dir"]) / "photos").glob("*.jpg"))
    upright = upright_view(s, db, iid, photos[n], upright)              # the owner's photo, turned upright too
    facts = facts.model_copy(update={"cover_photo": n, "cover_upright": upright})
    gate = loads(it["gate"]) or {}
    gate["notes"] = [x for x in gate.get("notes") or [] if x not in (NO_FRONT_COVER, NO_FULL_COVER)]
    with db.tx():
        db.set_item(iid, owner_cover=n, facts=facts.model_dump(), renders=relist(s, it, facts, loads(it["renders"])),
                    gate=gate)
        db.log(iid, "cover_set", {"photo": n})
    return it["status"]


RECOVERABLE = ("awaiting_condition", "awaiting_price", "needs_info", "ready", "needs_owner")


def _listed_cover(it, photos: list[Path]) -> int | str:
    """The photo the listing's cover was made of, as rendered: the one photo missing after cover.jpg when every
    photo fits; "?" when that can't be told."""
    renders = loads(it["renders"]) or {}
    listed = {Path(p).name for r in renders.values() for p in (r.get("photos") or [])[1:]}
    missing = [i for i, p in enumerate(photos) if p.name not in listed]
    return missing[0] if len(missing) == 1 else "?"


def _stored_check(it) -> FrontOut | None:
    """The front check stored with the item (items.views), or None — none stored (processed before WO23) or one that
    no longer reads."""
    try:
        return FrontOut.model_validate(loads(it["views"])) if it["views"] else None
    except ValueError:
        return None


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def recover_item(s: Settings, db: DB, iid: str, recheck: bool = False, relabel: bool = False) -> dict:
    """WO23: recompute ONLY the cover (the front check, upright), the photo order, the category and the size of an
    item that is not on the marketplace — the price, the owner's approved price, condition and Girls/Boys answers and
    the copy stay. Nothing settled is asked again: a question that the new category or size settles goes; an item that
    then waits only for a price it already has is ready.

    WO24: the front check stored with the item is kept, and so is the cover's turn while the cover is the same photo —
    run twice, recover changes nothing (a second look could swap a front for a look-alike back: live, a skirt's cover
    went 0, 2, 0, 2 over four runs); `recheck` asks both checks again. The item's open card is sent again only when
    what it shows changed (its text, its buttons' price, its cover picture); otherwise it stays as the owner has it.
    Returns what it found ({"cover", "role", "upright", "card", ...})."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if why := reached_site(db, it):
        raise ValueError(f"item {iid} is on the marketplace ({', '.join(why)}) — left as it is")
    if it["status"] not in RECOVERABLE or not it["facts"] or not it["renders"]:
        raise ValueError(f"item {iid} is {it['status']} — nothing to recover")
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    kinds = photo_kinds_of(d, photos)
    facts = Facts.model_validate(loads(it["facts"]))
    before = {"cover": _listed_cover(it, photos), "category": facts.category, "subcategory": facts.subcategory,
              "size": facts.size_us.value}
    was = (approve.card(iid, it), _digest(d / "cover.jpg"))
    facts, _, _ = taxonomy.fit(settle_waist(settle_set_size(settle_set(apply_owner_answers(it, facts),
                                                                       it["owner_category"]))))
    facts = settle_shoe_kind(facts, it["owner_category"])
    facts = settle_kids_size(s, facts, photos)
    stored = None if recheck else _stored_check(it)
    check = stored or front_view(s, db, iid, photos, facts, kinds)
    cover, upright, cover_note = choose_cover(facts, len(photos), kinds, check, it["owner_cover"])
    if cover is not None:
        if stored is None or cover != facts.cover_photo:
            upright = upright_view(s, db, iid, photos[cover], upright)
        else:
            upright = facts.cover_upright                    # the same photo, turned upright before: kept
        facts = facts.model_copy(update={"cover_photo": cover, "cover_upright": upright})
    if labelled(check):
        facts = facts.model_copy(update={"photo_order": labelled_order(check, len(photos))})
    if facts.premium is None or recheck or relabel:      # the labels, read closely once (WO26); --relabel again
        facts = premium.merge(facts, label_view(s, db, iid, photos, facts, kinds), len(photos))
    renders = relist(s, it, facts, loads(it["renders"]))
    if not it["owner_price"]:                            # a suggestion, not the owner's price: the premium factor too
        pr = price(facts, load_yaml("brand_tiers.yaml"), s["pricing"], it["note"],
                   premium.price_factor(facts, premium.config()))
        renders = {mp: {**r, "price": pr.by_marketplace.get(mp, pr.list_price or r.get("price"))}
                   for mp, r in renders.items()}
        it = {**dict(it), "price": json.dumps(pr.model_dump())}
    notes = [x for x in (loads(it["gate"]) or {}).get("notes") or []
             if x != NO_FRONT_COVER and not x.startswith("every photo shows a flaw")]
    doc, status = _regate(s, it, facts, renders, photos, kinds, notes + ([cover_note] if cover_note else []))
    questions = doc["questions"]
    new_card = approve.card(iid, {**dict(it), "status": status, "facts": json.dumps(facts.model_dump()),
                                  "renders": json.dumps(renders), "gate": json.dumps(doc)})
    changed = new_card is not None and (new_card, _digest(d / "cover.jpg")) != was
    with db.tx():
        if db.item(iid)["updated_at"] != it["updated_at"]:
            # An answer came in while this ran (the worker's Telegram thread): writing now would put back what was
            # read before it. Nothing is changed; the next run starts from the answer.
            raise ValueError(f"item {iid} changed while it was being recovered (an answer came in) — run it again")
        db.set_item(iid, facts=facts.model_dump(), renders=renders, gate=doc, status=status, price=loads(it["price"]),
                    views=check.model_dump() if check else it["views"], cover_hash=prep.cover_hash(photos[facts.cover_photo])
                    if 0 <= facts.cover_photo < len(photos) else it["cover_hash"])
        waiting = _open_cards(db, iid)
        if changed and waiting:
            # the owner's card shows something else now: closed, so the queue sends the new one (one at a time)
            db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref=? AND resolved_at IS NULL", (now(), iid))
        card = (None if new_card is None else "sent again" if changed and waiting else "changed" if changed
                else "unchanged")
        after = {"cover": photo_order(facts, len(photos), kinds)[0], "category": facts.category,
                 "subcategory": facts.subcategory, "size": facts.size_us.value}
        db.log(iid, "recovered", {"before": before, "after": after, "upright": facts.cover_upright, "status": status,
                                  "card": card})
    roles = {r.photo: r.role for r in facts.photo_roles}
    views = {v.photo: v.view for v in (check.views if check else [])}
    return {"item": iid, "cover": after["cover"], "role": roles.get(after["cover"], "?"),
            "view": views.get(after["cover"], "-"), "upright": facts.cover_upright, "category": facts.category,
            "subcategory": facts.subcategory, "size": facts.size_us.value, "questions": questions, "status": status,
            "card": card, "before": before, "title": (renders.get("poshmark") or {}).get("title"),
            "features": premium.summary(facts, premium.config())}


def _regate(s: Settings, it, facts: Facts, renders: dict, photos: list[Path], kinds: list[str],
            notes: list[str]) -> tuple[dict, str]:
    """(gate doc, status) recomputed from the item's facts and listing, no model call — recover, "no brand", a category
    the owner confirmed (WO25): the copy linted again, the verifier's count kept, a re-share hold and the NWT question
    kept; "Which category?", Girls/Boys and a size Poshmark's menus don't have asked as process_item asks them. An item
    that then waits only for a price it already has is ready."""
    d = Path(it["dir"])
    _, fit_notes, fit_questions = taxonomy.fit(facts)
    pr = PriceResult.model_validate(loads(it["price"]))
    posh = renders.get("poshmark") or next(iter(renders.values()))
    depop = renders.get("depop") or posh
    copy = CopyOut(poshmark_title=posh["title"], poshmark_description=posh["description"],
                   poshmark_style_tags=posh.get("tags") or [], depop_description=depop["description"],
                   depop_hashtags=(depop.get("tags") or []) if "depop" in renders else ["x"] * 5)
    shown = listing_photos(s, facts, len(photos), kinds)
    record = json.loads((d / "item.json").read_text(encoding="utf-8")) if (d / "item.json").exists() else {}
    gate = evaluate(facts, pr, lint(facts, copy, shown, known_brands()), len(record.get("unsupported_removed") or []),
                    s["gate"], s["pricing"], sized=not sizes.one_size(facts.department, facts.category,
                                                                      facts.subcategory))
    doc = loads(it["gate"]) or {}
    twin = next((q for q in doc.get("questions") or [] if q.startswith("looks like item")), None)
    options = category_question(facts, fit_questions, it["owner_category"])
    asked = list(fit_questions)
    menu_note = size_menu_note(facts, sizes.poshmark_size(facts))
    questions = ([twin] if twin else []) + asked + gate.questions + \
        [q for q in doc.get("questions") or [] if q == NWT_QUESTION]
    if options:                                          # "Which category?" asks it, with buttons
        questions = [q for q in questions if q not in fit_questions and q != OTHER_CATEGORY_QUESTION]
    decision = "needs_info" if twin or asked or options or gate.decision == "needs_info" else gate.decision
    unsure = category_unsure(facts) if options else []
    doc.update(decision=decision, reasons=([twin] if twin else []) + asked + unsure + gate.reasons, questions=questions,
               notes=notes, info=list(fit_notes) + gate.notes + ([menu_note] if menu_note else []),
               ask_kids=False, ask_category=[], hold=None)       # WO37: nothing but the price waits
    status = it["status"]
    if status in ("awaiting_condition", "awaiting_price", "needs_info") and it["owner_price"]:
        status = "ready"                                     # WO37: the price it has is all that waited
    return doc, status


def _answerable(db: DB, iid: str, what: str):
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if why := reached_site(db, it):
        raise ValueError(f"item {iid} is already on the marketplace ({', '.join(why)}) — change its {what} there")
    if it["status"] == "dropped":
        raise ValueError(DROPPED_SAID)
    if it["status"] not in PRICEABLE or not it["facts"] or not it["renders"]:
        raise ValueError(f"item {iid} is {it['status']} — its {what} can't be changed now")
    return it


def _photos_of(it) -> tuple[list[Path], list[str]]:
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    return photos, photo_kinds_of(d, photos)


def set_no_brand(s: Settings, db: DB, iid: str) -> str:
    """The owner's [No brand] (a button on the brand question, a reply "no brand" / "unbranded", or `thrift answer
    <item> "no brand"`; WO25): the item has no brand. Kept as items.owner_brand through any reprocessing; Poshmark's
    brand field stays empty (the form marks Brand "Optional", seen on the live form 2026-09-30) and the copy names no
    brand. When the listing named one — the model's unsure reading — the item is reprocessed so the copy loses it;
    otherwise its brand question goes at once, no model call. Returns the item's status ('new' when reprocessing)."""
    it = _answerable(db, iid, "brand")
    facts = Facts.model_validate(loads(it["facts"]))
    if facts.brand.value:
        with db.tx():
            db.conn.execute("DELETE FROM listings WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
            db.set_item(iid, owner_brand=NO_BRAND, status="new")
            db.log(iid, "brand_set", {"brand": None, "from": it["status"], "reprocess": True})
        return "new"
    facts = facts.model_copy(update={"brand": Ev(value=None, photos=[], source="owner", confidence=1.0)})
    renders = {mp: {**r, "brand": None} for mp, r in (loads(it["renders"]) or {}).items()}
    photos, kinds = _photos_of(it)
    answered = {**dict(it), "owner_brand": NO_BRAND}
    doc, status = _regate(s, answered, facts, renders, photos, kinds, (loads(it["gate"]) or {}).get("notes") or [])
    with db.tx():
        db.set_item(iid, owner_brand=NO_BRAND, facts=facts.model_dump(), renders=renders, gate=doc, status=status)
        db.log(iid, "brand_set", {"brand": None, "status": status})
    return status


def _brand_in(old: str | None) -> str | None:
    """The pattern of a brand name in a text — any spacing or punctuation of it ("J.Crew", "J. Crew")."""
    parts = re.findall(r"[A-Za-z0-9]+", old or "")
    return (r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9\n]{0,3}".join(map(re.escape, parts)) + r"(?![A-Za-z0-9])"
            if parts else None)


def _rename(text: str, old: str | None, new: str) -> str:
    """`text` with the old brand name — any spacing or punctuation of it ("J.Crew", "J. Crew") — as the new one."""
    pattern = _brand_in(old)
    return re.sub(pattern, lambda m: new, text, flags=re.I) if pattern else text


def _renamed(text: str, names: list[str | None], new: str) -> str:
    """`text` with the first of `names` it holds as the new brand: the name the listing used ("Alo Yoga"), else the
    label's ("Alo") — one only, so "Alo Yoga" never becomes "Alo Yoga Yoga"."""
    for was in dict.fromkeys(n for n in names if n):
        if (pattern := _brand_in(was)) and re.search(pattern, text, re.I):
            return _rename(text, was, new)
    return text


def _close_changed_card(db: DB, iid: str, was) -> None:
    """The item's open card closed when what it shows changed (WO24's rule, as recover): the queue sends the new one."""
    if approve.card(iid, db.item(iid)) != was and _open_cards(db, iid):
        db.conn.execute("UPDATE outbox SET resolved_at=? WHERE ref=? AND resolved_at IS NULL", (now(), iid))


def set_brand(s: Settings, db: DB, iid: str, brand: str) -> str:
    """The owner's brand (WO27: a plain reply to the brand question — "J. Crew" — "brand J. Crew", or `thrift edit
    --brand`): kept as items.owner_brand through any reprocessing, source owner. No model call: the facts, the listing's
    brand field and its text follow — the old name replaced where the copy wrote it, the title in the owner's order (the
    brand first). Another spelling of the same name ("J.Crew" -> "J. Crew") is learned for next time. "no brand" is
    set_no_brand. Returns the item's status."""
    brand = re.sub(r"\s+", " ", brand or "").strip().strip("'\"")
    if not brand or NO_BRAND_WORDS.fullmatch(brand.strip(" .!")):
        return set_no_brand(s, db, iid)
    it = _answerable(db, iid, "brand")
    facts = Facts.model_validate(loads(it["facts"]))
    old = facts.brand.value
    before = loads(it["renders"]) or {}
    shown = (before.get("poshmark") or {}).get("brand") or old     # the name the listing used ("Alo")
    label = (before.get("poshmark") or {}).get("brand_label") or (old if facts.brand.source != "owner" else None)
    facts = facts.model_copy(update={"brand": Ev(value=brand, photos=facts.brand.photos, source="owner",
                                                 confidence=1.0)})
    if label:                                   # WO36: the label's text → her name, kept for the next item
        brands.for_settings(s).learn(label, brand, owner=True)
    pcfg = premium.config()
    renders = {}
    for mp, r in before.items():
        title, text = (_renamed(r.get(k) or "", [r.get("brand") or shown, old], brand) for k in ("title", "description"))
        renders[mp] = {**r, "brand": brand, "brand_label": label if label and brands.key(label) != brands.key(brand)
                       else None, "title": it["owner_title"] or premium.title_with_feature(title, facts, pcfg),
                       "description": text}
    photos, kinds = _photos_of(it)
    doc, status = _regate(s, {**dict(it), "owner_brand": brand}, facts, renders, photos, kinds,
                          (loads(it["gate"]) or {}).get("notes") or [])
    was = approve.card(iid, it)
    with db.tx():
        db.set_item(iid, owner_brand=brand, facts=facts.model_dump(), renders=renders, gate=doc, status=status)
        db.log(iid, "brand_set", {"brand": brand, "was": old, "status": status})
        _close_changed_card(db, iid, was)
    return status


def _resized(it, facts: Facts, renders: dict) -> dict:
    """The renders after a size change, no model call and no photo work (WO33): the size field with Poshmark's menu
    value, the title's size (the owner's own title kept), the pieces' line in the description."""
    tab, value = sizes.poshmark_size(facts) or (None, None)
    pcfg = premium.config()
    return {mp: {**r, "size": sizes.size_label(facts), "size_tab": tab, "size_value": value,
                 "title": it["owner_title"] or premium.title_with_feature(
                     copywriter.ensure_title_size(copywriter.fix_title(r.get("title") or "", facts), facts), facts,
                     pcfg),
                 "description": copywriter.ensure_piece_sizes(r.get("description") or "", facts)}
            for mp, r in renders.items()}


def set_size(s: Settings, db: DB, iid: str, size: str) -> str:
    """The owner's size for the item (WO33: a reply 'Size S', 'size s' or 'S' to its card): kept as items.owner_size
    through any reprocessing, source owner; the listing's size field and title follow; no model call, the price kept.
    A set's pieces keep their line. Returns the item's status."""
    size = re.sub(r"\s+", " ", size or "").strip()
    if not size:
        raise ValueError("which size? Reply 'size S'")
    it = _answerable(db, iid, "size")
    it2 = {**dict(it), "owner_size": size}
    facts = apply_owner_answers(it2, Facts.model_validate(loads(it["facts"])))
    renders = _resized(it2, facts, loads(it["renders"]) or {})
    photos, kinds = _photos_of(it)
    doc, status = _regate(s, it2, facts, renders, photos, kinds, (loads(it["gate"]) or {}).get("notes") or [])
    was = approve.card(iid, it)
    with db.tx():
        db.set_item(iid, owner_size=size, facts=facts.model_dump(), renders=renders, gate=doc, status=status)
        db.log(iid, "size_set", {"size": size, "status": status})
        _close_changed_card(db, iid, was)
        # WO37: the owner's size is "list it": every site that failed or skipped it before any publish click goes
        # back in line (live: the Clarks — Depop and Vinted skipped "no size", Poshmark's Next showed "Required");
        # an unconfirmed publish never (invariant 4)
        for row in db.conn.execute("SELECT marketplace FROM listings WHERE item_id=? AND status IN ('skipped', "
                                   "'failed') AND COALESCE(error, '') NOT LIKE 'unconfirmed%' AND COALESCE(error, '') "
                                   "NOT LIKE ?", (iid, f"skipped: {TWIN_LIVE}%")).fetchall():   # WO39: its twin
            db.upsert_listing(iid, row["marketplace"], status="queued", error=None)
    return status


def set_piece_sizes(s: Settings, db: DB, iid: str, pieces: list[tuple[str, str]]) -> str:
    """The owner's sizes for a set's pieces (WO33: `thrift edit --sizes "Cardigan=S,Pants=XS"`): kept as
    items.owner_sizes through any reprocessing; the listing under the bigger size — its size field and title — and the
    description's line "Cardigan: size S. Pants: size XS."; no model call, the price kept. Returns the status."""
    pieces = [(p.strip(), z.strip()) for p, z in pieces if p and p.strip() and z and z.strip()]
    if len(pieces) < 2:
        raise ValueError("give every piece with its size: Cardigan=S,Pants=XS")
    it = _answerable(db, iid, "sizes")
    stored = [{"piece": p, "size": z} for p, z in pieces]
    it2 = {**dict(it), "owner_sizes": json.dumps(stored)}
    facts = apply_owner_answers(it2, Facts.model_validate(loads(it["facts"])))
    renders = _resized(it2, facts, loads(it["renders"]) or {})
    photos, kinds = _photos_of(it)
    doc, status = _regate(s, it2, facts, renders, photos, kinds, (loads(it["gate"]) or {}).get("notes") or [])
    was = approve.card(iid, it)
    with db.tx():
        db.set_item(iid, owner_sizes=stored, facts=facts.model_dump(), renders=renders, gate=doc, status=status)
        db.log(iid, "sizes_set", {"pieces": stored, "size": facts.size_us.value, "status": status})
        _close_changed_card(db, iid, was)
    return status


def edit_listing(s: Settings, db: DB, iid: str, title: str | None = None, brand: str | None = None) -> str:
    """`thrift edit <item> --title … --brand …` (WO27): the owner's exact title and/or brand, kept through any
    reprocessing (items.owner_title, items.owner_brand); no model call, the price kept. Not for an item on the
    marketplace. Returns the item's status."""
    status = _answerable(db, iid, "listing")["status"]
    if brand is not None:
        status = set_brand(s, db, iid, brand)
    if title is not None:
        it = _answerable(db, iid, "title")
        title = re.sub(r"\s+", " ", title).strip()
        if not 0 < len(title) <= copywriter.TITLE_MAX:
            raise ValueError(f"a title is 1-{copywriter.TITLE_MAX} characters, not {len(title)}")
        facts = Facts.model_validate(loads(it["facts"]))
        renders = {mp: {**r, "title": title} for mp, r in (loads(it["renders"]) or {}).items()}
        photos, kinds = _photos_of(it)
        doc, status = _regate(s, {**dict(it), "owner_title": title}, facts, renders, photos, kinds,
                              (loads(it["gate"]) or {}).get("notes") or [])
        was = approve.card(iid, it)
        with db.tx():
            db.set_item(iid, owner_title=title, renders=renders, gate=doc, status=status)
            db.log(iid, "title_set", {"title": title, "status": status})
            _close_changed_card(db, iid, was)
    return status


def set_category(s: Settings, db: DB, iid: str, path: dict) -> str:
    """The owner's category (a button on "Which category?", a typed reply, or `thrift category`; WO25), kept as
    items.owner_category through any reprocessing. The model's own pick: settled at once, no model call. Another path:
    the item is reprocessed with it, so the price, the size menu and the copy follow what the owner says it is (shorts,
    not a skirt). Returns the item's status ('new' when reprocessing)."""
    it = _answerable(db, iid, "category")
    facts = Facts.model_validate(loads(it["facts"]))
    placed = taxonomy.place(facts, path.get("department"), path.get("category") or "", path.get("subcategory"))
    if placed is None:
        raise ValueError(f"Poshmark has no category {taxonomy.path_label(path)!r} — reply e.g. 'Skirts › Skirt Sets'")
    if placed != {"department": facts.department, "category": facts.category, "subcategory": facts.subcategory}:
        with db.tx():
            db.conn.execute("DELETE FROM listings WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
            db.set_item(iid, owner_category=placed, status="new")
            db.log(iid, "category_set", {"category": placed, "from": it["status"], "reprocess": True})
        return "new"
    facts = facts.model_copy(update={"category_confidence": 1.0, "category_alternatives": []})
    photos, kinds = _photos_of(it)
    answered = {**dict(it), "owner_category": json.dumps(placed)}
    doc, status = _regate(s, answered, facts, loads(it["renders"]), photos, kinds,
                          (loads(it["gate"]) or {}).get("notes") or [])
    with db.tx():
        db.set_item(iid, owner_category=placed, facts=facts.model_dump(), gate=doc, status=status)
        db.log(iid, "category_set", {"category": placed, "status": status})
    return status


REPROCESSABLE = ("awaiting_condition", "awaiting_price", "needs_info", "ready")


def reprocess(s: Settings, db: DB, iid: str) -> dict:
    """`thrift reprocess <item>` (WO25): an item that waits for the owner, or is ready, goes through the pipeline again
    in place — today's prompts and copy rules (a set's "2-Piece Set"), the owner's answers kept (price, condition,
    Girls/Boys, cover, category, no brand). It keeps waiting meanwhile; its card is sent again only when what it shows
    changed. Its dry-run post rows are forgotten, as after a note. Returns {"status", "card", "title", "category"}."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if why := reached_site(db, it):
        raise ValueError(f"item {iid} is on the marketplace ({', '.join(why)}) — left as it is")
    if it["status"] not in REPROCESSABLE:
        raise ValueError(f"item {iid} is {it['status']} — only an item waiting for the owner, or ready, is reprocessed")
    with db.tx():
        db.conn.execute("DELETE FROM listings WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
        db.log(iid, "reprocess", {"from": it["status"]})
    out = process_item(s, db, iid)
    after = db.item(iid)
    facts = loads(after["facts"]) or {}
    posh = (loads(after["renders"]) or {}).get("poshmark") or {}
    return {**out, "title": posh.get("title"), "features": premium.summary(Facts.model_validate(facts), premium.config()),
            "category": taxonomy.path_label({"category": facts.get("category"), "subcategory": facts.get("subcategory")})}


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
    rows = [r for r in db.listings_for(iid) if marketplace is None or r["marketplace"] == marketplace]
    if not rows:
        raise ValueError(f"item {iid} has no {marketplace or ''} post rows — nothing to requeue".replace("  ", " "))
    for r in rows:
        waiting = parked and r["status"] == "queued" and (r["error"] or "").startswith(PARKED)
        if r["status"] not in REQUEUEABLE and not waiting:
            raise ValueError(f"{r['marketplace']}: status is {r['status']} — only failed/dryrun rows (or a poster "
                             "question) can be requeued")
        if r["url"]:
            raise ValueError(f"{r['marketplace']}: has a listing URL ({r['url']}) — it reached the site; "
                             "check the closet and fix it by hand")
        if (r["error"] or "").startswith("unconfirmed publish: "):
            raise ValueError(f"{r['marketplace']}: List This Item was pressed and no listing address was found — it "
                             f"may be live; check the closet, then `thrift mark-posted {iid} {r['marketplace']} <url>` "
                             f"(it is there) or `thrift retry {iid}` (it is not)")
    cross_only = all(r["marketplace"] != "poshmark" for r in rows)     # Depop / Vinted rows (WO30)
    if cross_only and (db.listing(iid, "poshmark") or {"status": ""})["status"] != "posted" and \
            not (it["status"] == "ready" and it["owner_price"]):      # WO33: approved items post everywhere at once
        raise ValueError(f"item {iid} isn't live on Poshmark: Depop and Vinted follow Poshmark")
    if not cross_only and it["status"] not in ("ready", "drafted", "needs_owner"):
        raise ValueError(f"item {iid} is {it['status']}, not ready — reply to its card with the fix first")
    with db.tx():
        for r in rows:
            db.upsert_listing(iid, r["marketplace"], status="queued", error=None,
                              attempts=0 if r["marketplace"] != "poshmark" else r["attempts"])
        if not cross_only and it["status"] != "ready":
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
    if it["status"] == "dropped":
        raise ValueError(DROPPED_SAID)
    if it["status"] == "awaiting_condition":
        raise ValueError("first its condition: brand new or worn? — tap NWT, Like New or Good on its message")
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
    waiting_only_for_the_price = (it["status"] == "awaiting_price" and not gate.get("hold") and not gate.get("ask_kids")
                                  and not gate.get("ask_category"))
    status = "ready" if waiting_only_for_the_price else it["status"]
    with db.tx():
        db.set_item(iid, owner_price=amount, price=pr, renders=renders, status=status)
        db.log(iid, "price_set", {"amount": amount, "status": status})
    settle_twin(s, db, iid)
    return status


TWIN = re.compile(r"^looks like item (i_[0-9a-z_]+)")
TWIN_LIVE = "already live as "                  # WO39: a new item's row skipped because its twin is live there


def twin_of(it) -> str | None:
    """The earlier item a new card was flagged as ("looks like item X …", duplicate_check), or None."""
    for q in [*((loads(it["gate"]) or {}).get("questions") or []), *((loads(it["gate"]) or {}).get("reasons") or [])]:
        if m := TWIN.match(q or ""):
            return m[1]
    return None


def _title(it) -> str:
    return (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or (it["id"] if it else "")


def settle_twin(s: Settings, db: DB, iid: str) -> str | None:
    """WO39 (the owner: the same item is never listed twice). A card flagged "looks like item X" got its price — the
    owner's "list it" — so X and this item are one garment:
    - X not live anywhere: X is retired (status `dropped`, its rows still to come skipped, its card closed), so a
      later reply to X's card can't post it;
    - X live on a site (posted, being posted, or an unconfirmed publish that may be live): this item doesn't post on
      that site (its row skipped "already live as X").
    One private line either way; once per item (event `twin_settled`). Returns what was done, or None."""
    it = db.item(iid)
    if it is None or not it["owner_price"] or it["status"] == "dropped":
        return None
    tid = twin_of(it)
    tw = db.item(tid) if tid else None
    if tw is None or tw["status"] == "dropped" or tid == iid:
        return None
    if db.conn.execute("SELECT 1 FROM events WHERE ref=? AND kind='twin_settled'", (iid,)).fetchone():
        return None
    live = {r["marketplace"]: r for r in db.listings_for(tid)
            if r["status"] in ("posted", "posting") or unconfirmed_publish(r)}
    title, ttitle = _title(it), _title(tw)
    if not live and tw["status"] not in REDO_KEEP:
        with db.tx():
            db.set_item(tid, status="dropped")
            for r in db.listings_for(tid):
                if r["status"] in ("queued", "dryrun", "failed", "skipped") and not r["url"]:
                    db.upsert_listing(tid, r["marketplace"], status="skipped", error=f"skipped: shared again as {iid}")
            db.conn.execute("UPDATE outbox SET resolved_at=? WHERE (ref=? OR ref LIKE ?) AND resolved_at IS NULL",
                            (now(), tid, f"{tid}:%"))
            db.log(tid, "dropped", {"twin": iid, "why": "shared again; the new card got its price"})
            db.log(iid, "twin_settled", {"twin": tid, "retired": True})
        notify.say(f"♻️ {title}: the same item was shared before (“{ttitle}”) — that earlier card is retired, "
                   f"this one is the one listed")
        return f"retired {tid}"
    sites = sorted(live, key=lambda mp: ("poshmark", "depop", "vinted").index(mp) if mp in
                   ("poshmark", "depop", "vinted") else 9)
    with db.tx():
        for mp in sites:
            row = db.listing(iid, mp)
            if row is not None and (row["status"] in ("posted", "posting") or row["url"] or unconfirmed_publish(row)):
                continue
            db.upsert_listing(iid, mp, status="skipped", error=f"skipped: {TWIN_LIVE}{tid}")
        db.log(iid, "twin_settled", {"twin": tid, "live": sites})
    names = " and ".join(m.capitalize() for m in sites)
    notify.say(f"♻️ {title}: the same item is already live on {names} (“{ttitle}”) — not listed there again")
    return f"blocked on {', '.join(sites)}"


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
    return (post["error"] or "").startswith("unconfirmed publish: ")


POSTER_REQUESTS = "poster_requests"   # kv: the owner's "posted <url>", for the poster to check between listings


def check_unconfirmed(db: DB, iid: str, mp: str = "poshmark"):
    """The post row of an unconfirmed publish — the final click happened, no address was found (WO28 §3) — else
    ValueError saying what the row is."""
    if db.item(iid) is None:
        raise ValueError(f"unknown item {iid}")
    row = db.listing(iid, mp)
    if row is None or row["status"] != "failed" or row["url"] or not unconfirmed_publish(row):
        state = "no post" if row is None else f"status {row['status']}" + (f" with {row['url']}" if row["url"] else "")
        raise ValueError(f"{mp}: only a post in 'unconfirmed publish' can be marked posted or retried ({iid} has "
                         f"{state})")
    return row


def listing_address(mp: str, url: str) -> str | None:
    """The canonical address of a listing page of `mp` (WO30: Poshmark, Depop, Vinted), else None."""
    if mp == "poshmark":
        from thrift_agent.post.poshmark import listing_address as posh    # Playwright's module: only when needed
        return posh(url)
    if mp == "depop":
        from thrift_agent.post.depop import listing_address as depop
        return depop(url)
    if mp == "vinted":
        from thrift_agent.post.vinted import listing_address as vinted
        return vinted(url)
    return None


def request_posted(s: Settings, db: DB, iid: str, url: str, mp: str = "poshmark") -> str:
    """The owner's "posted <url>" (a reply to the ⚠️ message, or `thrift mark-posted` while the poster runs): what can
    be checked here is — the row is an unconfirmed publish, the address is a listing page no other item holds — and
    it is queued for the poster, which opens the page (the item's title and price) between listings and records it.
    Returns the canonical address."""
    check_unconfirmed(db, iid, mp)
    address = listing_address(mp, url)
    if address is None:
        raise ValueError(f"not a {mp} listing address: {url!r}" + (" (e.g. https://poshmark.com/listing/<title-words>-"
                                                                  "<24 hex id>)" if mp == "poshmark" else ""))
    if (other := db.conn.execute("SELECT item_id FROM listings WHERE url=? AND item_id != ?", (address, iid)).fetchone()):
        raise ValueError(f"{address} is already recorded for item {other['item_id']}")
    with db.tx():
        reqs = [r for r in (loads(db.kv_get(POSTER_REQUESTS)) or []) if (r.get("item"), r.get("mp")) != (iid, mp)]
        db.kv_set(POSTER_REQUESTS, json.dumps([*reqs, {"item": iid, "mp": mp, "url": address, "at": now()}]))
        db.log(iid, "posted_reported", {"mp": mp, "url": address})
    return address


def take_requests(db: DB, mps: list[str] | None = None) -> list[dict]:
    """The queued "posted <url>" requests, taken out — those of `mps` only, when given (WO33: each site's worker takes
    its own; the rest stay queued)."""
    with db.tx():
        reqs = loads(db.kv_get(POSTER_REQUESTS)) or []
        mine = [r for r in reqs if mps is None or (r.get("mp") or "poshmark") in mps]
        if mine:
            db.kv_set(POSTER_REQUESTS, json.dumps([r for r in reqs if r not in mine]))
    return mine


def retry_unconfirmed(s: Settings, db: DB, iid: str, mp: str = "poshmark") -> str:
    """The owner's "retry" (a reply to the ⚠️ message, or `thrift retry`; WO28 §3): they looked, and the listing is
    not on the marketplace — so it goes back in line and the poster lists it again. Only for an unconfirmed publish:
    the code never decides that by itself (invariant 4). Returns the item's status."""
    check_unconfirmed(db, iid, mp)
    from thrift_agent import crosslist
    with db.tx():
        db.upsert_listing(iid, mp, status="queued", error=None)
        if mp == "poshmark" and db.item(iid)["status"] != "ready":
            db.set_item(iid, status="ready")       # Depop / Vinted rows never move the item (WO30)
        db.outbox_resolve("unconfirmed", crosslist.unconfirmed_ref(iid, mp))
        db.log(iid, "post_retry", {"mp": mp, "by": "owner"})
    return db.item(iid)["status"]


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
    if b["status"] == "regroup":
        raise ValueError(f"batch {bid} is waiting for its photo fix ([Wrong photos]) — answer that first")
    rebuilt, kept = [], []
    for it in items:
        reached = reached_site(db, it)
        if reached or it["status"] == "dropped":
            kept.append(f"{it['id']} ({', '.join(reached) or it['status']})")
            continue
        with db.tx():
            db.conn.execute("DELETE FROM listings WHERE item_id=?", (it["id"],))
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


def correct_condition(s: Settings, db: DB, iid: str, choice: str) -> str:
    """WO37: the owner's condition, by a reply to the card (`condition nwt|like new|good`) or `thrift condition` — no
    model call and no new reading of the photos (live 2026-10-12: reprocessing after "Brand new or worn?" lost the
    Clarks' size and changed the Schutz's cover). The grade is the owner's (source owner: NWT needs no hang-tag photo),
    the descriptions take its condition line, the title its "New" or none; the price stays. Returns the status."""
    if (key := owner_choice(choice)) is None:
        raise ValueError(f"condition must be one of {', '.join(OWNER_CONDITIONS)}, got {choice!r}")
    condition = OWNER_CONDITIONS[key]
    it = _answerable(db, iid, "condition")
    facts = apply_owner_condition(Facts.model_validate(loads(it["facts"])), condition)
    before = loads(it["renders"]) or {}
    tfacts = title_facts(facts, (before.get("poshmark") or {}).get("brand"))
    pcfg = premium.config()
    renders = {}
    for mp, r in before.items():
        text = r.get("description") or ""
        c = copywriter.condition_rule(CopyOut(poshmark_title=r.get("title") or "-", poshmark_description=text or "-",
                                              depop_description=text or "-", depop_hashtags=[]), facts)
        title = it["owner_title"] or premium.title_with_feature(copywriter.fix_title(r.get("title") or "", tfacts),
                                                                 tfacts, pcfg)
        renders[mp] = {**r, "title": title, "condition": condition,
                       "description": c.depop_description if mp == "depop" else c.poshmark_description}
    photos, kinds = _photos_of(it)
    doc, status = _regate(s, {**dict(it), "owner_condition": condition}, facts, renders, photos, kinds,
                          (loads(it["gate"]) or {}).get("notes") or [])
    with db.tx():
        db.set_item(iid, owner_condition=condition, facts=facts.model_dump(), renders=renders, gate=doc, status=status)
        db.log(iid, "condition_set", {"condition": condition, "from": it["status"], "how": "corrected, no reprocessing"})
    return status


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
    if it["status"] in PRICEABLE and it["facts"] and it["renders"]:
        return correct_condition(s, db, iid, choice)      # WO37: a correction, no new reading of the photos
    with db.tx():
        # As with a note: earlier dry-runs and queue entries are forgotten, real post history stays.
        db.conn.execute("DELETE FROM listings WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
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
    if NO_BRAND_WORDS.fullmatch(note.strip(" .!")):     # "no brand" / "unbranded": the [No brand] button (WO25)
        return set_no_brand(s, db, iid)
    merged = f"{it['note']}; {note}" if it["note"] else note
    gate = loads(it["gate"]) or {}
    twin = gate.get("hold") == "reshare" or any(q.startswith("looks like item") for q in gate.get("questions") or [])
    if twin and SAME_ITEM.search(note) and not NOT_A_DUPLICATE.search(note):     # WO37: a warning, no longer a hold
        with db.tx():
            db.set_item(iid, note=merged, status="dropped")     # the same garment is already listed: never list it twice
            db.log(iid, "dropped", {"note": note})
        return "dropped"
    with db.tx():
        # Forget earlier dry-runs / queue entries, or next_job would skip the corrected listing (dryrun + dry).
        # posting/posted/drafted/failed rows stay: those are history the poster must never repeat blindly.
        db.conn.execute("DELETE FROM listings WHERE item_id=? AND status IN ('dryrun','queued')", (iid,))
        db.set_item(iid, note=merged, status="new")
        db.log(iid, "answered", {"note": note})
    return "new"


def flaw_photos(facts: Facts, n: int) -> set[int]:
    """The photos that show a flaw: they go in the listing, never as its cover (the owner's condition rule)."""
    return {i for f in facts.flaws for i in f.photos if 0 <= i < n}


NO_FRONT_COVER = "cover: no front flat-lay photo"


DESIGN = {"none": 0, "some": 1, "strong": 2}


def cover_candidates(facts: Facts, n: int, kinds: list[str] | None = None) -> list[int]:
    """The photos that may be the cover: the item alone (front, side or back by the model's photo_roles), never a
    screenshot, a worn / mirror photo, a label, a tag, a flaw close-up or a box; the model's own cover pick first, then
    its order. A full view cited for a flaw is NOT left out (WO23): the front stays the cover even when a small stain
    shows on it — the owner's rule, the first photo is the front — and the flaw is disclosed by that very photo."""
    kinds = kinds or ["own"] * n
    roles = {r.photo: r.role for r in facts.photo_roles if 0 <= r.photo < n}
    preferred = list(dict.fromkeys(i for i in [facts.cover_photo, *facts.photo_order, *range(n)] if 0 <= i < n))
    return [i for i in preferred if kinds[i] != "retail" and roles.get(i) in ITEM_ALONE]


NO_FULL_COVER = "cover: no photo shows the whole item — the one showing the most of it is used"
# WO37 (the owner): the cover shows the WHOLE item from the front — shoes the pair from the side or 3/4 front, clothing
# flat or on a hanger (worn only without one), a set both pieces — and is never one of these
NEVER_COVER = ("back", "top", "label", "detail", "inside", "sole", "flaw", "box", "other")
SHOE_COVER = ("three_quarter", "side", "front")
WORN_ALONE = ("front", "three_quarter", "side")                 # a bag, an accessory: any whole outside view
LATER = {"front": 1, "three_quarter": 1, "side": 2, "back": 3, "top": 4, "worn": 5, "detail": 6, "inside": 6,
         "sole": 6, "unclear": 6, "label": 7, "flaw": 8, "box": 9, "other": 9}   # WO37: back, side, worn, details,
                                                                                   # label, flaws


def shoes(facts: Facts) -> bool:
    return (facts.category or "").strip().lower() == "shoes"


def cover_rule(facts: Facts, check: FrontOut, n: int, kinds: list[str]) -> tuple[int | None, str | None]:
    """WO37, the owner's rule, from the check's labels (brain/cover.py): the WHOLE item, from the front, nothing cut off.
    Shoes: the pair from the side or 3/4 front (three_quarter, side, front), whole. Clothing and sets: a front flat lay
    or hanger shot, whole; a worn photo only when there is none. Bags and the rest: a whole outside view. Never a
    label, a close-up, a look into shoes, a sole, a flaw, a box, a hand-held shot from behind (view back) or a photo cut
    off at an edge. Several fit: the model's own pick, else the one filling more of the frame (the printed side first
    for clothing). None fits: the photo showing the most of the item (whole, not cut off, the most of the frame — never
    a label, a close-up, a sole, a look inside), with NO_FULL_COVER: one line to the private chat, never a question."""
    views = [v for v in check.views if 0 <= v.photo < n and kinds[v.photo] != "retail"]
    if not views:
        return None, None
    worn_ok = False
    if shoes(facts):
        allowed = SHOE_COVER
    elif facts.department in ("Women", "Men", "Kids") and (facts.category or "").strip().lower() not in (
            "bags", "jewelry", "accessories"):
        allowed, worn_ok = ("front",), True
    else:
        allowed = WORN_ALONE

    def good(v) -> bool:
        return v.whole and not v.cut_off

    def rank(v) -> tuple:
        return (v.photo == check.front, DESIGN.get(v.design, 0) if not shoes(facts) else 0, v.fill, -v.photo)
    fits = [v for v in views if v.view in allowed and good(v)]
    if not fits and worn_ok:
        fits = [v for v in views if v.view == "worn" and good(v)]
    if fits:
        return max(fits, key=rank).photo, None
    shown = [v for v in views if v.view not in ("label", "detail", "inside", "sole", "flaw", "box", "other")] or views
    best = max(shown, key=lambda v: (v.whole, not v.cut_off, v.view not in NEVER_COVER, v.fill, v.photo == check.front,
                                     -v.photo))
    return best.photo, NO_FULL_COVER


def choose_cover(facts: Facts, n: int, kinds: list[str] | None = None, check: FrontOut | None = None,
                 owner: int | None = None) -> tuple[int | None, int, str | None]:
    """(cover, clockwise turn that puts it upright, note for the card) — the owner's rules: the first photo of every
    listing is the item (WO23), the WHOLE item from the front (WO37, `cover_rule`).

    The owner's "cover N" wins. Else, with the check's labels (brain/cover.py, every photo — WO37): `cover_rule`.
    A check from before WO37 (no labels of whole / cut off) still follows them: its defaults read every candidate as
    whole. Without a check: the extraction's roles, front, else side (a shoe's profile is a fine cover), else back; the
    note "cover: no front flat-lay photo" only when no candidate could be the front. (None, 0, None): no photo roles at
    all — facts from before WO20 keep the model's pick."""
    kinds = kinds or ["own"] * n
    views = {v.photo: v for v in (check.views if check else []) if 0 <= v.photo < n}

    def turn(i: int) -> int:
        return views[i].upright if i in views else 0

    if owner is not None and 0 <= owner < n:
        return owner, turn(owner), None
    if labelled(check):
        pick, note = cover_rule(facts, check, n, kinds)      # a WO37 check: every photo labelled
        if pick is not None:
            return pick, turn(pick), note
    roles = {r.photo: r.role for r in facts.photo_roles if 0 <= r.photo < n}
    if not roles:
        return None, 0, None
    cands = cover_candidates(facts, n, kinds)
    if not cands:
        return None, 0, NO_FRONT_COVER

    def back(i: int) -> bool:
        return views[i].view == "back" if i in views else roles.get(i) == "back"

    def rank(i: int) -> tuple:
        is_front = views[i].view == "front" if i in views else roles.get(i) == "front"
        return is_front, DESIGN.get(views[i].design, 0) if i in views else 0, -cands.index(i)

    pick = check.front if check is not None and check.front in cands else None
    for role in ("front", "side", "back"):
        if pick is None:
            pick = next((i for i in cands if roles.get(i) == role), None)
    if back(pick) and (others := [i for i in cands if not back(i)]):
        pick = max(others, key=rank)                          # a back is never the cover while a front side exists
    designed = [i for i in cands if i in views and DESIGN.get(views[i].design, 0) > 0 and not back(i)]
    if pick in views and DESIGN.get(views[pick].design, 0) == 0 and designed:
        pick = max(designed, key=rank)                        # the printed side is the front (the owner's cue)
        return pick, turn(pick), None                         # ...never one the check itself calls the back
    return pick, turn(pick), NO_FRONT_COVER if back(pick) else None


def labelled(check: FrontOut | None) -> bool:
    """A check made by WO37's labelling (every photo: view, whole, cut off …), not WO23's front/back comparison."""
    return check is not None and any(v.view not in ("front", "back", "side", "unclear") or not v.whole or v.cut_off
                                     or v.fill != 0.5 or v.held for v in check.views)


def labelled_order(check: FrontOut, n: int) -> list[int]:
    """WO37: after the cover — the other whole front views, back, side, the look from above, worn, details, labels,
    flaws, box (each group in shooting order). Stored as facts.photo_order where the cover is chosen."""
    labels = {v.photo: v.view for v in check.views if 0 <= v.photo < n}
    return sorted(range(n), key=lambda i: (LATER.get(labels.get(i, "unclear"), 6), i))


def photo_order(facts: Facts, n: int, kinds: list[str] | None = None) -> list[int]:
    """The listing's photo order: the cover first — facts.cover_photo, which choose_cover settled (WO23) — then
    facts.photo_order (WO37: the check's labelled order — back, side, worn, details, labels, flaws), every photo once;
    retail screenshots last and never the cover. Facts from before WO20 (no photo roles) keep their older rule: a photo
    that shows a flaw is not the cover when a clean own photo exists."""
    kinds = kinds or ["own"] * n
    order = [i for i in facts.photo_order if 0 <= i < n]
    if 0 <= facts.cover_photo < n:
        order = [facts.cover_photo] + order
    order = list(dict.fromkeys(order + list(range(n))))         # cover first, then the order, no repeats
    own = [i for i in order if kinds[i] != "retail"]
    order = (own + [i for i in order if kinds[i] == "retail"]) if own else order   # screenshots last, never the cover
    if not facts.photo_roles:
        flawed = flaw_photos(facts, n)
        clean = [i for i in own if i not in flawed]
        if order and order[0] in flawed and clean:
            order = [clean[0]] + [i for i in order if i != clean[0]]
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
    """For the approval message: a flaw no photo shows isn't in the listing at all. Told, never asked. (The front may
    be the cover with a flaw on it, WO23: that photo discloses it.)"""
    return [f"flaw without a photo, so the listing doesn't show it: {f.description}" for f in facts.flaws
            if not f.photos]


def shop_brand(facts: Facts, named: str | None, aliases: brands.Aliases | None = None) -> tuple[str | None, str | None]:
    """(the brand as shops list it, the label's text when it differs) — WO36: "Alo Yoga" for an "alo" label. The
    owner's answer is the name as she gave it. Else the copy step's fuller name (`named`) when it is that brand — the
    label's words followed by more ("alo" → "Alo Yoga"), the alias file's name, or the label as its initials ("TNF" →
    The North Face) — else the alias file's name for the label, else the label's own text. A name that is none of
    these (another brand) is never used."""
    label = (facts.brand.value or "").strip() or None
    if label is None:
        return None, None
    if facts.brand.source == "owner":
        return label, None
    alias = aliases.alias(label) if aliases is not None else None
    named = (named or "").strip() or None

    def same_brand(name: str) -> bool:
        ws = brands.words(name)
        initials = {"".join(w[0] for w in ws), "".join(w[0] for w in ws if w not in ("the", "and", "of"))}
        return (brands.key(name) == brands.key(label) or brands.extends(label, name)
                or (alias is not None and brands.key(name) == brands.key(alias))
                or (len(brands.key(label)) >= 2 and brands.key(label) in initials))
    if named and same_brand(named) and brands.key(named) != brands.key(label):
        shop = named                                     # the copy step's fuller name ("Alo Yoga")
    else:
        shop = alias or label                            # the alias file's, else the label's (an older listing too)
    return shop, (label if brands.key(label) != brands.key(shop) else None)


def named_as(text: str, label: str | None, shop: str | None) -> str:
    """WO36: the brand named as shops list it in a text — the label's name ("MNG") as the shop's ("Mango") — unless the
    text names it so already ("Alo Yoga …" holds "Alo": left as it is, never "Alo Yoga Yoga")."""
    if not text or not label or not shop or brands.key(label) == brands.key(shop):
        return text
    if (pattern := _brand_in(shop)) and re.search(pattern, text, re.I):
        return text
    return _rename(text, label, shop)


def shop_named(c: CopyOut, label: str | None, shop: str | None) -> CopyOut:
    """The copy with the brand named as shops list it (named_as): the title, both descriptions and the short name."""
    if not label or not shop or brands.key(label) == brands.key(shop):
        return c
    return c.model_copy(update={k: named_as(getattr(c, k), label, shop)
                                for k in ("poshmark_title", "poshmark_description", "depop_description", "short_name")})


def title_facts(facts: Facts, shop: str | None) -> Facts:
    """The facts as the title sees them: the brand named as shops list it (WO36) — the stored facts keep the label's."""
    if not shop or brands.key(shop) == brands.key(facts.brand.value):
        return facts
    return facts.model_copy(update={"brand": facts.brand.model_copy(update={"value": shop})})


def build_renders(s: Settings, iid: str, d: Path, photos: list[Path], facts: Facts, c: CopyOut,
                  pr: PriceResult, kinds: list[str] | None = None) -> dict[str, Render]:
    order = photo_order(facts, len(photos), kinds)
    flawed = flaw_photos(facts, len(photos))
    width, height = prep.cover_dims(s["images"]["cover_size"])
    turn = facts.cover_upright if order[0] == facts.cover_photo else 0       # upright: a front laid sideways (WO23)
    cover = prep.portrait_cover(photos[order[0]], d / "cover.jpg", width, height, rotate=turn)   # 3:4, never cropped

    def paths(limit: int) -> list[str]:                # this marketplace's photos: flaw photos are never cut
        shown = fit_photos(order, flawed, limit)
        return [str(cover)] + [str(photos[i]) for i in shown[1:]]

    tab, value = sizes.poshmark_size(facts) or (None, None)      # exactly a value of the form's size menu (WO25)
    shop, label = shop_brand(facts, c.brand, brands.for_settings(s))   # WO36: the brand as shops list it, the label's
    common = dict(brand=shop, brand_label=label, department=facts.department, category=facts.category,
                  subcategory=facts.subcategory, size=sizes.size_label(facts), size_tab=tab, size_value=value,
                  colors=list(facts.colors),
                  kids_gender=facts.kids_gender if facts.department == "Kids" else None,
                  condition=facts.condition, sku=iid,
                  original_price=_dollars(facts.retail_price.value) or pr.original_price)   # screenshot beats note
    out = {}
    for mp, mcfg in s["marketplaces"].items():
        if not mcfg.get("enabled") or mp != "poshmark":
            continue                        # Depop and Vinted: mapped from the catalogs when cross-listed (WO30)
        is_posh = mp == "poshmark"
        out[mp] = Render(
            marketplace=mp,
            title=c.poshmark_title,
            description=c.poshmark_description if is_posh else c.depop_description,
            tags=[] if is_posh else c.depop_hashtags,         # no style tags on Poshmark (the owner, 2026-10-12)
            short_name=copywriter.short_name(c.short_name, c.poshmark_title, facts),   # WO34: the group's name
            price=pr.by_marketplace.get(mp, pr.list_price or 0),
            photos=paths(mcfg["max_photos"]),
            **common,
        )
    return out
