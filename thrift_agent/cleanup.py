"""WO47 — the owner's photo storage policy (2026-10-19): one copy of a photo, no duplicates, nothing kept for the
sake of it. Where a share's photos live: her iCloud Drive inbox (`Posh/inbox/<share id>`, the Shortcut's copy),
thrift-api's blob container (the Shortcut's upload), the Mac's work folder (`work/<batch>/item_NN/photos`, the
normalized photos every listing, repost, cover fix and photo edit uses — THE copy kept) and `work/<batch>/all` (the
preparation's copy), and the archive (`Posh/archive/<date>_<share id>`, the share folder moved out of the inbox —
inside iCloud Drive too, so it counts against her iCloud storage and syncs to her phone).

The rules, and where each is done:
1. No duplicates, ever: the second copy of a share (the iCloud folder after the upload, or the other way round) is
   DELETED the moment it is recognised — never archived (`delete_copy`, from `uploads.take` / `pipeline.register` /
   `pipeline.repeat_share`); the daily run sweeps what slipped through (iCloud's "<id> 2" folders, a folder that
   came back, the "-icloud" folders set aside before this).
2. Azure: a share's blobs are deleted the moment the Mac says it has the share (`POST /shares/<id>/fetched`, in
   thrift-api — `uploads.mark_fetched`); the 30-day lifecycle rule stays as the backstop for a share never fetched.
3. Items for sale keep ONE copy: the work copy. The archive copy of a split share goes KEEP_DAYS after the share
   (the late-photo window is 2 days, `redo` / [Wrong photos] / `requeue` need the share folder — all within a week),
   once every item of it has its work copy; `work/<batch>/all` goes as soon as the share is split.
4. Sold items: their work photos go as soon as the sale shows shipped (thrift-api's sales: `shipped_at`, or the
   sale done) — the listing sites keep their own copies; a relist of a shipped item is never wanted.
5. Repeat, empty, failed and test shares: their archive folder and work folder go after KEEP_DAYS.
6. The photo memory (`photos`: fingerprints, no pictures) is untouched.
7. `run` once a day from the worker (`tick`, the first look at the inbox 24 h after the last run), `thrift cleanup`
   by hand (`--dry-run` to see the plan, `--sizes` to measure); everything removed is in the event `cleanup` and
   ONE private line; and ONE private line when the Mac's free space drops under LOW_GB (`disk_watch`).
8. The evidence (`failed/shots`: the posters' screenshots, page HTML and job traces) older than EVIDENCE_DAYS goes
   (Alex, 2026-10-20) — except an item's while a problem of it is still open (`open_problems`: a listing queued,
   posting, failed, or skipped for a reason that still waits; an item failed or parked; a price change, a listing
   edit or a take-down not done; an unconfirmed publish): that item's evidence stays whatever its age, said in the
   plan's notes. A file is tied to an item by its name (`<item id>-…`, or a Poshmark listing id of ours).
Never touched: the inbox folder of a share in flight, a work copy an item still needs, the database.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import notify, sales
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

KEEP_DAYS = 7                      # the archive copy of a share, a failed / repeat / empty / test share's folders
EVIDENCE_DAYS = 14                 # the evidence in failed/shots (Alex, 2026-10-20) — an open problem's stays
SETTLED_SKIPS = ("shared again", "already live", "sold", "not published automatically")   # skips that wait for no one
_ITEM_FILE = re.compile(r"^(i_\d{6}_[0-9a-f]{6})[-.]")      # "i_261019_09053e-vinted-20261020-080011.png"
_LISTING_FILE = re.compile(r"^([0-9a-f]{24})-")              # Poshmark's listing id: "6ad6d53a…-poshmark-…-price-dry.png"
LOW_GB = 20.0                      # ONE private line when the Mac's free space drops under this …
LOW_CLEAR_GB = 22.0                # … said again only after it was back above this
DAILY = "cleanup_at"               # kv: the daily run's stamp (sales._claim)
LAST = "cleanup_last"              # kv: the last run's summary (thrift status)
DISK_AT = "disk_checked_at"        # kv: the disk looked at every DISK_EVERY seconds
DISK_SAID = "disk_low_said"        # kv: the low-disk line is out (cleared above LOW_CLEAR_GB)
DISK_EVERY = 600
ENABLED = True                     # the worker's tick runs the watch and the policy; the test suite turns it off
ASIDE = "-icloud"                  # WO46's set-aside iCloud copies (a day's worth): duplicates
_ARCHIVED = re.compile(r"^\d{8}_")
_SECOND = re.compile(r"^(?P<base>.+?) (?P<n>\d{1,2})$")       # iCloud's second folder of one name: "<id> 2"
IN_FLIGHT = ("new", "needs_confirm", "regroup")
LOW_LINE = ("⚠️ The Mac has {free:.1f} GB free (under {low:.0f} GB). iCloud may stop downloading her photos and a "
            "share can fail — please free space on the Mac. Thrift's own folders: {ours}.")
BACK_LINE = "✓ The Mac has {free:.1f} GB free again."


@dataclass
class Removal:
    path: str
    kind: str                      # duplicate | archive | work_all | work_batch | staging | sold | unknown | evidence
    why: str
    bytes: int
    ref: str | None = None         # the batch, share or item it belongs to
    files: list[str] = field(default_factory=list)   # for `sold`: the files (the folder stays)


# ---------------------------------------------------------------- measuring

def folder_bytes(p: Path) -> int:
    """The bytes under `p` (a file's own size); what can't be read counts 0."""
    try:
        if p.is_file():
            return p.stat().st_size
        total = 0
        for q in p.rglob("*"):
            try:
                if q.is_file():
                    total += q.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return 0


def human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def disk_free(s: Settings) -> tuple[int, int]:
    """(free, total) bytes of the volume the work folder is on."""
    u = shutil.disk_usage(str(s.path("work")))
    return u.free, u.total


def sizes(s: Settings) -> dict[str, int]:
    """What thrift's folders take now: the archive and the inbox (iCloud Drive), the work copies, the prep copies,
    the staging, the evidence, the database."""
    work = s.path("work")
    out = {"archive": folder_bytes(s.path("archive")), "inbox": folder_bytes(s.path("inbox")),
           "work_items": 0, "work_all": 0, "staging": folder_bytes(work / "uploads"),
           "shots": folder_bytes(s.path("failed") / "shots"), "db": folder_bytes(s.path("db"))}
    try:
        for b in work.iterdir():
            if not b.is_dir() or b.name == "uploads":
                continue
            out["work_all"] += folder_bytes(b / "all")
            out["work_items"] += sum(folder_bytes(d) for d in b.iterdir() if d.is_dir() and d.name != "all")
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- the plan

def plan(s: Settings, db: DB, now: datetime | None = None, api=None) -> tuple[list[Removal], list[str]]:
    """What the policy removes now, and why each thing that looks removable is kept. Nothing is touched."""
    from thrift_agent import pipeline
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=KEEP_DAYS)
    removals: list[Removal] = []
    kept: list[str] = []
    archive, inbox, work = s.path("archive"), s.path("inbox"), s.path("work")
    dest_of = _archived_dests(db)
    shipped = shipped_items(db, api)

    # 4. sold and shipped: the work photos
    for iid, sale in shipped.items():
        it = db.item(iid)
        if it is None or int((it["test"] if "test" in it.keys() else 0) or 0):
            continue
        rows = db.listings_for(iid)
        if any(r["status"] in ("queued", "posting") for r in rows):
            kept.append(f"{iid}: sold, but a listing is still queued or posting — its photos stay")
            continue
        d = Path(it["dir"])
        files = [p for p in [*sorted((d / "photos").glob("*")), d / "cover.jpg"] if p.is_file()]
        if files:
            removals.append(Removal(str(d), "sold", f"sold on {sale['site']}, shipped {sale['when']}: its photos",
                                    sum(folder_bytes(p) for p in files), iid, [str(p) for p in files]))

    # 1. and 3./5. the archive
    try:
        folders = sorted(p for p in archive.iterdir() if p.is_dir())
    except OSError as e:
        kept.append(f"the archive can't be read now ({type(e).__name__})")
        folders = []
    for d in folders:
        if d.name.endswith(ASIDE) or re.search(re.escape(ASIDE) + r"[ _]\d+$", d.name):
            removals.append(Removal(str(d), "duplicate", "the iCloud copy of a share the upload brought (set aside "
                                    "before the policy)", folder_bytes(d), None))
            continue
        batches = _batches_of(db, d, dest_of)
        if not batches:
            if _ARCHIVED.match(d.name) and _mtime(d) < cutoff:
                removals.append(Removal(str(d), "unknown", f"no share of ours is recorded for it; older than "
                                        f"{KEEP_DAYS} days", folder_bytes(d), None))
            continue
        main = batches[0]
        made = _when(main["created_at"]) or _mtime(d)
        if made >= cutoff:
            continue
        if any(b["status"] in IN_FLIGHT for b in batches):
            kept.append(f"{d.name}: a batch on it is still {', '.join(b['status'] for b in batches)}")
            continue
        items = [i for b in batches for i in db.conn.execute("SELECT * FROM items WHERE batch_id=?",
                                                              (b["id"],)).fetchall()]
        missing = [i["id"] for i in items if i["status"] not in ("dropped", "failed") and i["id"] not in shipped
                   and not _work_copy(i)]
        if missing:
            kept.append(f"{d.name}: {', '.join(missing)} ha{'s' if len(missing) == 1 else 've'} no work copy — the "
                        "share folder is its only one")
            continue
        removals.append(Removal(str(d), "archive", _why_archive(main, items, shipped), folder_bytes(d), main["id"]))

    # 1. the inbox: iCloud's second folder of a share already taken, a folder that came back after it was taken
    try:
        folders = sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as e:
        kept.append(f"the inbox can't be read now ({type(e).__name__})")
        folders = []
    for d in folders:
        row = db.conn.execute("SELECT id, status FROM batches WHERE src_dir=?", (str(d),)).fetchone()
        if row is not None:
            if row["status"] in ("split", "failed", "joined") and dest_of.get(row["id"]):
                removals.append(Removal(str(d), "duplicate", "the share's folder came back after it was taken and "
                                        "archived", folder_bytes(d), row["id"]))
            continue
        m = _SECOND.match(d.name)
        if m:
            base = db.conn.execute("SELECT id, status FROM batches WHERE src_dir=?",
                                   (str(d.with_name(m["base"])),)).fetchone()
            if base is not None and base["status"] not in IN_FLIGHT:
                removals.append(Removal(str(d), "duplicate", "iCloud's second folder of a share already taken",
                                        folder_bytes(d), base["id"]))

    # 3./5. the work folder: the prep copy of a split share, a failed share's work, old staging
    try:
        wdirs = sorted(p for p in work.iterdir() if p.is_dir())
    except OSError as e:
        kept.append(f"the work folder can't be read now ({type(e).__name__})")
        wdirs = []
    for d in wdirs:
        if d.name == "uploads":
            for st in sorted(p for p in d.iterdir() if p.is_dir()):
                if _mtime(st) < cutoff:
                    removals.append(Removal(str(st), "staging", f"an upload staged more than {KEEP_DAYS} days ago",
                                            folder_bytes(st), st.name))
            continue
        b = db.batch(d.name)
        if b is None:
            if _mtime(d) < cutoff:
                removals.append(Removal(str(d), "work_batch", f"no batch of ours; older than {KEEP_DAYS} days",
                                        folder_bytes(d), d.name))
            continue
        if b["status"] in IN_FLIGHT:
            continue
        made = _when(b["created_at"]) or _mtime(d)
        test = int((b["test"] if "test" in b.keys() else 0) or 0)
        if test or b["status"] in ("failed", "joined"):
            live = db.conn.execute("SELECT 1 FROM items WHERE batch_id=? AND status NOT IN ('dropped', 'failed')",
                                   (b["id"],)).fetchone()                       # a test item is never listed
            if made < cutoff and (test or not live):
                removals.append(Removal(str(d), "work_batch", f"{_batch_kind(b)}; older than {KEEP_DAYS} days",
                                        folder_bytes(d), b["id"]))
                continue
        allp = d / "all"
        if allp.is_dir() and b["status"] in ("split", "joined", "failed") \
                and (pipeline.registered_names(db, b["id"]) is not None or made < cutoff):
            removals.append(Removal(str(allp), "work_all", "the preparation's copy of the share's photos (the items "
                                    "keep theirs)", folder_bytes(allp), b["id"]))

    # 8. the evidence: files older than EVIDENCE_DAYS, except an item's while a problem of it is still open
    removals += evidence_plan(s, db, now, kept)
    return removals, kept


def open_problems(db: DB) -> dict[str, str]:
    """{item id: why} for every item with a problem still open (Alex, 2026-10-20: its evidence stays whatever its
    age): a listing queued / posting / failed, or skipped for a reason that still waits (her 'retry', a size); an
    item failed or parked; a price change, a listing edit or a take-down not done; an unconfirmed publish."""
    out: dict[str, str] = {}

    def note(iid, why: str) -> None:
        if iid and iid not in out:
            out[str(iid)] = why
    for r in db.conn.execute("SELECT item_id, marketplace, status, error FROM listings WHERE status IN "
                             "('queued', 'posting', 'failed', 'skipped')"):
        if r["status"] == "skipped" and any(k in (r["error"] or "") for k in SETTLED_SKIPS):
            continue
        note(r["item_id"], f"its {r['marketplace']} listing is {r['status']}")
    for r in db.conn.execute("SELECT id, status FROM items WHERE status IN ('failed', 'needs_owner')"):
        note(r["id"], f"the item is {r['status']}")
    for table, label, states in (("price_changes", "a price change", "'pending', 'saving', 'unconfirmed', 'failed'"),
                                 ("listing_edits", "a listing edit", "'pending', 'saving', 'unconfirmed', 'failed'"),
                                 ("takedowns", "a take-down", "'pending', 'waiting', 'running', 'failed', 'manual'")):
        try:
            for r in db.conn.execute(f"SELECT item_id, status FROM {table} WHERE status IN ({states})"):
                note(r["item_id"], f"{label} is {r['status']}")
        except sqlite3.OperationalError:               # a table from before its work order
            continue
    for key in (loads(db.kv_get("unconfirmed_state")) or {}):
        note(str(key).partition(":")[0], "a publish is unconfirmed")
    return out


def evidence_plan(s: Settings, db: DB, now: datetime, kept: list[str]) -> list[Removal]:
    """Rule 8: the files in failed/shots older than EVIDENCE_DAYS, one Removal per item (its files), the files no item
    of ours is named in together under ref None; an item with an open problem keeps every file, said once in `kept`."""
    shots = s.path("failed") / "shots"
    cutoff = now - timedelta(days=EVIDENCE_DAYS)
    try:
        old = [p for p in shots.iterdir() if p.is_file() and _mtime(p) < cutoff]
    except OSError as e:
        kept.append(f"the evidence folder can't be read now ({type(e).__name__})")
        return []
    if not old:
        return []
    opened = open_problems(db)
    by_listing: dict[str, str] = {}
    for r in db.conn.execute("SELECT listing_id, item_id FROM listings WHERE listing_id IS NOT NULL"):
        by_listing[str(r["listing_id"])] = r["item_id"]
    groups: dict[str | None, list[Path]] = {}
    held: dict[str, int] = {}
    for p in sorted(old):
        ref = None
        if m := _ITEM_FILE.match(p.name):
            ref = m[1]
        elif m := _LISTING_FILE.match(p.name):
            ref = by_listing.get(m[1])
        if ref in opened:
            held[ref] = held.get(ref, 0) + 1
            continue
        groups.setdefault(ref, []).append(p)
    for ref, n in sorted(held.items()):
        kept.append(f"{ref}: {n} evidence file{'s' if n != 1 else ''} older than {EVIDENCE_DAYS} days kept — "
                    f"{opened[ref]}")
    out = []
    for ref, files in sorted(groups.items(), key=lambda kv: kv[0] or ""):
        why = f"evidence older than {EVIDENCE_DAYS} days" + ("" if ref else " (no item of ours in its name)")
        out.append(Removal(str(shots), "evidence", why, sum(folder_bytes(p) for p in files), ref,
                           [str(p) for p in files]))
    return out


def _why_archive(b, items, shipped: dict) -> str:
    if int((b["test"] if "test" in b.keys() else 0) or 0):
        return f"a test share; older than {KEEP_DAYS} days"
    if b["status"] in ("split", "joined"):
        live = [i["id"] for i in items if i["status"] not in ("dropped", "failed")]
        sold = [i for i in live if i in shipped]
        what = f"{len(live)} item{'s' if len(live) != 1 else ''}"
        if sold:
            what += f", {len(sold)} sold and shipped"
        return f"share older than {KEEP_DAYS} days: {what}; the items' photos are in the work folder (one copy kept)"
    return f"{_batch_kind(b)}; older than {KEEP_DAYS} days"


def _batch_kind(b) -> str:
    if int((b["test"] if "test" in b.keys() else 0) or 0):
        return "a test share"
    reasons = loads(b["reasons"]) or []
    first = str(reasons[0]) if reasons else ""
    if first.startswith("repeat share"):
        return "a repeat share"
    if first.startswith("no photos arrived") or first.startswith("no usable photos"):
        return "an empty share"
    if b["status"] == "joined":
        return "late photos that joined their items"
    return f"a failed share ({first[:60]})" if first else "a failed share"


def _work_copy(it) -> bool:
    d = Path(it["dir"])
    try:
        return (d / "cover.jpg").is_file() and any((d / "photos").glob("*.jpg"))
    except OSError:
        return False


def _archived_dests(db: DB) -> dict[str, str]:
    """{batch id: where its share folder was archived} from the `archived` events (the last one wins)."""
    out: dict[str, str] = {}
    for e in db.conn.execute("SELECT ref, detail FROM events WHERE kind='archived' ORDER BY ts"):
        dest = (loads(e["detail"]) or {}).get("dest") if e["detail"] else None
        if e["ref"] and dest:
            out[e["ref"]] = str(dest)
    return out


def _batches_of(db: DB, d: Path, dest_of: dict[str, str]) -> list:
    """The batches a share's archive folder belongs to: the one archived there, then the in-place late batches on it
    (src_dir the folder, or the folder plus "#hhmmss")."""
    rows = []
    for bid, dest in dest_of.items():
        if dest == str(d):
            b = db.batch(bid)
            if b is not None:
                rows.append(b)
    for b in db.conn.execute("SELECT * FROM batches WHERE src_dir=? OR src_dir LIKE ? ORDER BY created_at",
                             (str(d), f"{d}#%")).fetchall():
        if all(b["id"] != r["id"] for r in rows):
            rows.append(b)
    return rows


def shipped_items(db: DB, api=None) -> dict[str, dict]:
    """{item id: {"site", "when"}} for the sales thrift-api shows shipped (or done): the items whose photos go.
    Without the API, or with it out of reach: none (said in the plan's notes by the caller)."""
    client = api if api is not None else sales.api()
    if client is None:
        return {}
    try:
        answer = client.get("/sales", {"state": "all"})
    except sales.ApiError as e:
        db.log(None, "cleanup_api_down", str(e))
        return {}
    out: dict[str, dict] = {}
    for sale in (answer.get("sales") or []) if isinstance(answer, dict) else []:
        if not isinstance(sale, dict) or not sale.get("item_id"):
            continue
        if sale.get("status") in ("cancelled", "merged", "unmatched"):
            continue
        if not sale.get("shipped_at") and sale.get("status") != "done":
            continue
        when = str(sale.get("shipped_at") or sale.get("delivered_at") or sale.get("sold_at") or "")[:10]
        out.setdefault(str(sale["item_id"]), {"site": str(sale.get("marketplace") or "?"), "when": when})
    return out


# ---------------------------------------------------------------- doing it

def run(s: Settings, db: DB, dry: bool = False, now: datetime | None = None, api=None,
        say: bool = True) -> dict:
    """The plan carried out (or, `dry`, only reported): every removal in the event `cleanup`, ONE private line when
    something went, the summary in kv `cleanup_last`. Never raises."""
    now = now or datetime.now(timezone.utc)
    removals, kept = plan(s, db, now, api)
    inbox = s.path("inbox").resolve()
    removed, failed, freed = [], [], 0
    for r in removals:
        p = Path(r.path)
        if not dry:
            try:
                if r.kind in ("sold", "evidence"):      # the files named, the folder stays
                    for f in r.files:
                        Path(f).unlink(missing_ok=True)
                else:
                    _rmtree(p, inbox)
            except OSError as e:
                failed.append({**asdict(r), "error": f"{type(e).__name__}: {e}"[:200]})
                continue
        removed.append(r)
        freed += r.bytes
    summary = {"at": now.isoformat(timespec="seconds"), "dry": dry, "freed": freed, "removed": len(removed),
               "failed": len(failed), "kept": len(kept)}
    db.log(None, "cleanup", {**summary, "removals": [{**asdict(r), "path": Path(r.path).name, "files": len(r.files)}
                                                      for r in removed],
                             "failures": [{**f, "path": Path(f["path"]).name} for f in failed], "notes": kept})
    if not dry:
        db.kv_set(LAST, json.dumps(summary))
        if removed and say:
            notify.say(line(removed, freed))
    return {**summary, "removals": removed, "failures": failed, "notes": kept}


def line(removed: list[Removal], freed: int) -> str:
    """🧹 Cleanup: freed 1.6 GB — 9 share folders older than 7 days (archive), 2 duplicates, 1 sold item's photos,
    12 prep copies."""
    counts: dict[str, int] = {}
    for r in removed:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    words = {"archive": ("share folder older than {d} days (archive)", "share folders older than {d} days (archive)"),
             "duplicate": ("duplicate", "duplicates"), "sold": ("sold item's photos", "sold items' photos"),
             "work_all": ("prep copy", "prep copies"), "work_batch": ("old work folder", "old work folders"),
             "staging": ("old upload staging", "old upload stagings"),
             "unknown": ("unknown archive folder", "unknown archive folders")}
    parts = []
    for kind in ("archive", "duplicate", "sold", "work_all", "work_batch", "staging", "unknown"):
        n = counts.get(kind)
        if n:
            parts.append(f"{n} {words[kind][0 if n == 1 else 1].format(d=KEEP_DAYS)}")
    if counts.get("evidence"):
        files = sum(len(r.files) for r in removed if r.kind == "evidence")
        items = sum(1 for r in removed if r.kind == "evidence" and r.ref)
        parts.append(f"{files} evidence file{'s' if files != 1 else ''} older than {EVIDENCE_DAYS} days"
                     + (f" ({items} item{'s' if items != 1 else ''})" if items else ""))
    return f"🧹 Cleanup: freed {human(freed)} — " + ", ".join(parts) + "."


def _rmtree(p: Path, inbox: Path) -> None:
    """A folder gone, whole. A folder in the inbox is only ever one the plan named a duplicate (the share in flight
    is never in the plan)."""
    if p.is_file():
        p.unlink()
        return
    shutil.rmtree(p)


def delete_copy(s: Settings, db: DB, sid: str, folder: Path, why: str) -> bool:
    """WO47 rule 1, at intake: the second copy of a share, deleted from the iCloud inbox the moment it is recognised.
    Only a folder inside the inbox; iCloud still coordinating it (EDEADLK, EBUSY) → False, the caller tries again on
    its next look. Logged as `share_copy_deleted` (what was in it), never told."""
    inbox = s.path("inbox").resolve()
    try:
        if inbox not in folder.resolve().parents:
            db.log(sid, "share_copy_kept", {"folder": str(folder), "why": "outside the inbox"})
            return False
        from thrift_agent.ingest import prep
        try:
            here = sum(1 for f in folder.iterdir() if prep.is_photo(f))
            cloud = len(prep.icloud_placeholders(folder))
        except OSError:
            here, cloud = None, None
        size = folder_bytes(folder)
        shutil.rmtree(folder)
    except OSError as e:
        db.log(sid, "share_copy_delete_failed", {"folder": folder.name, "error": f"{type(e).__name__}: {e}"[:200]})
        return False
    db.log(sid, "share_copy_deleted", {"share_id": sid, "folder": folder.name, "why": why, "photos_here": here,
                                       "cloud_only": cloud, "bytes": size})
    return True


# ---------------------------------------------------------------- the worker's tick

def due(db: DB, force: bool = False) -> bool:
    return sales._claim(db, DAILY, 24 * 3600, force=force)


def tick(s: Settings, db: DB) -> None:
    """From the worker's look at the inbox: the disk every DISK_EVERY seconds, the policy once a day. Never raises.
    The Mac's only (the dev machine's worker is a test's: its disk and folders are not the policy's); off in the
    test suite (ENABLED, tests/conftest.py), whose prod-like tests run the worker's tick on a real disk."""
    if not ENABLED or not s.is_prod:
        return
    try:
        disk_watch(s, db)
    except Exception as e:  # noqa: BLE001
        db.log(None, "disk_watch_failed", f"{type(e).__name__}: {e}")
    if due(db):
        try:
            run(s, db)
        except Exception as e:  # noqa: BLE001
            db.log(None, "cleanup_failed", f"{type(e).__name__}: {e}")


def disk_watch(s: Settings, db: DB, now: float | None = None) -> str | None:
    """ONE private line when the free space drops under LOW_GB; one when it is back above LOW_CLEAR_GB. Returns the
    line sent, if any. Looked at every DISK_EVERY seconds at most."""
    now = time.time() if now is None else now
    if now - float(db.kv_get(DISK_AT) or 0) < DISK_EVERY:
        return None
    db.kv_set(DISK_AT, str(now))
    free, total = disk_free(s)
    free_gb = free / 1024 ** 3
    said = db.kv_get(DISK_SAID)
    if free_gb < LOW_GB and not said:
        z = sizes(s)
        ours = (f"archive {human(z['archive'])}, work {human(z['work_items'] + z['work_all'])}, evidence "
                f"{human(z['shots'])}")
        text = LOW_LINE.format(free=free_gb, low=LOW_GB, ours=ours)
        db.kv_set(DISK_SAID, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        db.log(None, "disk_low", {"free": free, "total": total, "sizes": z})
        notify.say(text)
        return text
    if free_gb >= LOW_CLEAR_GB and said:
        db.kv_set(DISK_SAID, "")
        db.log(None, "disk_ok", {"free": free, "total": total})
        text = BACK_LINE.format(free=free_gb)
        notify.say(text)
        return text
    return None


# ---------------------------------------------------------------- small helpers

def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _mtime(p: Path) -> datetime:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)
