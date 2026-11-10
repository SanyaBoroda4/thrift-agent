"""Shares from the API (WO45 §2), the Mac's side: the iPhone's Shortcut POSTs each photo of a share to thrift-api and
then the share's end (api/thrift_api/uploads.py); while it is awake it asks the API for new shares every 30 s,
downloads them and drops each into the inbox as a folder named by the share id — the same name the Shortcut gives the
iCloud copy — so the intake takes it like an iCloud folder (`_done.txt` with "done <count>"; `_source.txt` "azure";
`_test` "1" for a test share, the Shortcut's test copy). Live 2026-10-17: three shares' photos sat on the iPhone for
4½ hours and iCloud Drive never pushed them; the Mac saw an empty folder. iCloud stays the safety copy.

A share is taken when it is ended with every photo in (received >= count), or ended and short but quiet for two
minutes (a photo that never went: the iCloud copy completes the folder later, WO42's partial rule tells the owner), or
never ended but started ten minutes ago with photos and none for five minutes (the Shortcut died: what came — WO54:
never while its photos are still coming in). The photos are downloaded into
work/uploads/<share id> first (each written as a dot-file and renamed; a file already there with the right size is
kept) and verified by SHA-256 — a mismatch drops the file (event `upload_bad_hash`) and the share is tried again next
time — then the folder is moved whole into the inbox, so the intake never sees it half-written. A folder of that share
already in the inbox or the archive (the iCloud copy came first: `YYYYMMDD_<share id>` there) is never doubled: the
share is only marked fetched (event `share_same_as_icloud`).

WO54 (live 2026-11-08, the 127-photo share 2026-11-08_223855_3036): at 10:49 PM, ten minutes after its first photo
and still uploading (104 of 127 in, a photo every few seconds), the share counted as "never ended"; its iCloud folder
was in the inbox — empty, iCloud still bringing it — and was taken as "the share": the API was told "fetched" (its
blobs deleted, the 23 photos that came after never fetched), and at 11:08 PM the group heard "📭 … still on the
iPhone" while Azure had every photo; the iCloud copy was whole only at 12:02 AM. Now: a share still receiving photos
is never taken as "never ended" (STALE_QUIET); a WHOLE upload always wins at once over an iCloud copy not taken yet,
whole or not; a short upload is never settled on an iCloud copy that is neither taken nor whole — the copy with more
photos in hand is taken (the upload on a tie: the Mac's own disk), else the Mac waits and Azure keeps its copy
(`choose`). The last answer's shares are kept (kv `uploads_pending`): no "📭 still on the iPhone" for a share Azure
holds photos of (`holds`, pipeline.take_share).

WO56 §6 (the owner's new Shortcut: the upload FIRST; the iCloud save only when the phone has no internet; a short
upload says "Only X of N photos went through — please share them again." on the phone — docs/SHORTCUT_UPLOAD.md):
- a share whose upload is WHOLE (every photo its count names — the end call's count or the photos' own `of=`; the end
  itself may not have come) is taken at once and never waits for, or mentions, an iCloud copy;
- a share that ended SHORT, or never ended (the Shortcut stopped mid-upload), with NO iCloud copy is never taken short:
  once quiet (the same moment it was taken before — Azure's copy goes, the API's "hasn't taken it" watch stays quiet)
  its photos are downloaded into the held folder (`held_dir`, never the intake: never posted with missing photos), and
  HOLD_AFTER after its last photo — no iCloud copy by then, not shared again — the group hears ONE plain line, "📸 The
  photos shared at 7:42 PM didn't finish uploading — please share them again." (a human step: only her phone has the
  photos), and the ledger says `held`;
- when she shares again, the new share REPLACES the held one: a newer share carrying any of its photos (their SHA-256
  in Azure's listing, kv `uploads_shas`; or the photo memory knowing one with an item) drops it — ledger `replaced`
  (an older share's photos: `repeat`) — and the photo memory never skipped the new share's photos, since a held
  share's photos are never remembered (never grouped);
- an iCloud copy of it (the no-internet branch, or the old Shortcut's copy) is the share by its own rules, as before
  (whole, or partial after 30 minutes, `pipeline.take_share`); the held copy is then that share's other copy and goes.

How it is called (the worker's inbox scan — not wired here):

    if uploads.due(db):               # every POLL_EVERY seconds at most (a kv stamp, one for every caller)
        uploads.fetch_new(s, db)      # the new shares into the inbox; never raises (the API down: one `api_down`)
    uploads.held_tick(s, db)          # WO56 §6: each held share — replaced, its other copy taken, or its one line
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

from thrift_agent import cleanup, notify, sales
from thrift_agent.config import Settings
from thrift_agent.db import DB

POLL_EVERY = 30                          # seconds between looks at GET /shares/pending
LAST_POLL = "uploads_last_poll"          # kv: the last look (sales._claim's stamp)
QUIET_AFTER = 120                        # an ended share short of its count: taken once its last photo is this old
STALE_AFTER = 600                        # a share never ended: taken this long after it started, with what came …
STALE_QUIET = 300                        # … and only once no photo came for this long (WO54: a 127-photo upload was
#                                          taken at minute 10 with 104 photos while the rest were still coming in)
PENDING = "uploads_pending"              # kv: {share id: {"received", "count", "ended", "at"}} — the last answer's shares
HOLD_AFTER = 15 * 60                     # WO56 §6: a short / stopped upload with no iCloud copy, quiet this long since
#                                          its last photo: its one "share them again" line (it was held from the start)
HELD_KEEP = 7 * 24 * 3600                # … a held share never shared again: its folder goes after this (the ledger
#                                          keeps `held`; cleanup's KEEP_DAYS)
HELD_FILE = "_held.json"                 # held/<share id>/_held.json: what the upload was (count, received, its photos'
#                                          SHA-256 and memory keys, when it last moved, the line said or not)
HELD_LOOK = "uploads_held_look"          # kv: the last look at the held shares (every HELD_EVERY seconds at most)
HELD_EVERY = 30
SHAS = "uploads_shas"                    # kv: {share id: {"at", "started", "shas": [first 16 hex of each SHA-256]}} —
#                                          the photos of the shares Azure listed in the last SHAS_KEEP: a held share's
#                                          photos found in a newer share is that share replacing it
SHAS_KEEP = 2 * 24 * 3600
HELD_LINE = "📸 The photos shared at {when} didn't finish uploading — please share them again."
DOWNLOAD_TIMEOUT = 60.0                  # a 30 MB photo over the owner's Wi-Fi
STAGING = "uploads"                      # work/uploads/<share id>: the download, moved whole into the inbox
SOURCE_FILE, TEST_FILE, DONE_FILE = "_source.txt", "_test", "_done.txt"
SHARE_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(_[0-9a-z]{1,8})?")   # the Shortcut's date_time_random
PHOTO_PATH = "/shares/"                  # a photo's path from the listing must be the API's own


def due(db: DB) -> bool:
    """True once every POLL_EVERY seconds (the stamp taken in one transaction, as sales._claim)."""
    return sales._claim(db, LAST_POLL, POLL_EVERY)


def fetch_new(s: Settings, db: DB, client=None, now: datetime | None = None) -> list[str]:
    """The pending shares the rules above allow, into the inbox; the share ids taken (or settled as the iCloud copy).
    Never raises: the API out of reach is one `api_down` event and the next call tries again."""
    client = client if client is not None else sales.api()
    if client is None:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        answer = client.get("/shares/pending")
    except sales.ApiError as e:
        db.log(None, "api_down", {"path": "/shares/pending", "error": str(e)})
        return []
    done = []
    listed = [x for x in (answer.get("shares") or []) if isinstance(x, dict)] if isinstance(answer, dict) else []
    remember(db, listed, now)
    remember_shas(db, listed, now)                     # WO56 §6: a held share's photos in a newer share replace it
    for share in listed:
        sid = str(share.get("share_id") or "")
        if not SHARE_ID.fullmatch(sid):
            db.log(None, "upload_bad_id", {"share_id": sid[:80]})          # never a path the API made up
            continue
        if not takeable(share, now):
            continue
        try:
            if take(s, db, client, share, sid):
                done.append(sid)
        except sales.ApiError as e:
            db.log(sid, "api_down", {"path": "/shares", "error": str(e)})
            break
        except OSError as e:
            db.log(sid, "upload_error", {"share_id": sid, "error": f"{type(e).__name__}: {e}"[:300]})
    return done


def remember(db: DB, shares: list[dict], now: datetime) -> None:
    """WO54: the shares Azure holds now, as the last answer listed them (kv `uploads_pending`, replaced at every look
    that reached the API) — what `holds` reads before anyone says a share's photos are still on the iPhone."""
    out = {}
    for share in shares:
        sid = str(share.get("share_id") or "")
        if SHARE_ID.fullmatch(sid):
            out[sid] = {"received": int(share.get("received") or 0), "count": share.get("count"),
                        "ended": bool(share.get("ended")), "at": now.isoformat(timespec="seconds")}
    try:
        db.kv_set(PENDING, json.dumps(out))
    except Exception as e:  # noqa: BLE001 — a record: never the reason a share isn't fetched
        db.log(None, "uploads_pending_unsaved", f"{type(e).__name__}: {e}"[:200])


def holds(db: DB, sid: str, more_than: int = 0) -> bool:
    """WO54: Azure holds photos of this share — more than `more_than` —, not yet fetched (the last answer listed it):
    its photos are NOT on the iPhone, no "📭 still on the iPhone" for it (live 2026-11-08, 11:08 PM: said while Azure
    had all 127); and an iCloud copy with fewer photos is never taken "partial" over it (pipeline.take_share)."""
    try:
        known = json.loads(db.kv_get(PENDING) or "{}")
    except ValueError:
        return False
    row = known.get(sid) if isinstance(known, dict) else None
    return isinstance(row, dict) and int(row.get("received") or 0) > max(0, int(more_than))


def remember_shas(db: DB, shares: list[dict], now: datetime) -> None:
    """WO56 §6: the photos (SHA-256, its first 16 hex) of every share Azure lists, kept SHAS_KEEP — a held share whose
    photos show up in a newer share was shared again (`held_tick`), even when that share is taken before any look."""
    try:
        known = json.loads(db.kv_get(SHAS) or "{}")
    except ValueError:
        known = {}
    if not isinstance(known, dict):
        known = {}
    stamp = now.isoformat(timespec="seconds")
    changed = False
    for share in shares:
        sid = str(share.get("share_id") or "")
        shas = sorted({str(p.get("sha256") or "")[:16] for p in share.get("photos") or []
                       if isinstance(p, dict) and p.get("sha256")})
        if SHARE_ID.fullmatch(sid) and shas and (known.get(sid) or {}).get("shas") != shas:
            known[sid] = {"at": stamp, "started": share.get("started_at"), "shas": shas}
            changed = True
    cutoff = now.timestamp() - SHAS_KEEP
    old = [k for k, v in known.items() if (t := _time((v or {}).get("at"))) is None or t.timestamp() < cutoff]
    for k in old:
        known.pop(k, None)
    if changed or old:
        try:
            db.kv_set(SHAS, json.dumps(known))
        except Exception as e:  # noqa: BLE001 — a record: never the reason a share isn't fetched
            db.log(None, "uploads_shas_unsaved", f"{type(e).__name__}: {e}"[:200])


def whole(share: dict) -> bool:
    """The upload is whole: every photo the Shortcut counted is in — its count from the end call or, WO56, from the
    photos' own `of=` (a share whose every photo came is whole even if its end call never did)."""
    count, received = share.get("count"), int(share.get("received") or 0)
    return isinstance(count, int) and count > 0 and received >= count


def short(share: dict) -> bool:
    """WO56 §6: short of its count — ended with fewer photos than it named, or never ended (the Shortcut stopped
    mid-upload). An ended share with no count is taken with what came, as before."""
    if whole(share):
        return False
    return not share.get("ended") or isinstance(share.get("count"), int)


def takeable(share: dict, now: datetime) -> bool:
    """Something to do with the share now: whole (every photo in) — at once; ended and short but quiet QUIET_AFTER;
    never ended but STALE_AFTER old and quiet STALE_QUIET (WO54: a share still receiving photos is not one the Shortcut
    gave up on) — with at least one photo in every case. WO56 §6: a short or stopped one is then taken beside an iCloud
    copy by `choose`'s rules, or held (`take`)."""
    photos = share.get("photos") or []
    received = int(share.get("received") or 0)
    if not photos or received <= 0:
        return False
    if whole(share):
        return True
    count = share.get("count")
    last = _time(share.get("last_received_at"))
    if share.get("ended"):
        if not isinstance(count, int):
            return True
        return last is not None and (now - last).total_seconds() >= QUIET_AFTER
    started = _time(share.get("started_at"))
    if started is None or (now - started).total_seconds() < STALE_AFTER:
        return False
    return last is None or (now - last).total_seconds() >= STALE_QUIET


def take(s: Settings, db: DB, client, share: dict, sid: str) -> bool:
    """One share: the folder in the inbox (or the iCloud copy found), the API told. False when it must wait.

    WO46 (her Shortcut saves the iCloud copy FIRST, then uploads): the iCloud folder is usually on the Mac before the
    upload ends — often with its photos still cloud-only, or not all there (live 2026-10-17: the folder and its
    `_done` came at once, the photos never). WO54: which copy is the share is `choose`'s — a whole upload always wins
    over an iCloud copy not taken yet (the copy is deleted, WO47); a short upload never gives its photos up for an
    iCloud copy that is neither taken nor whole (False: it waits, Azure keeps it). A copy already taken is the share."""
    names = [n for n in (safe_name(p.get("name")) for p in share.get("photos") or [] if isinstance(p, dict)) if n]
    test = bool(share.get("test"))
    found = known_folder(s, sid)
    if found is None and short(share):
        return hold(s, db, client, share, sid, names)  # WO56 §6: no iCloud copy — never taken short: held
    pick = choose(s, db, found, share) if found is not None else UPLOAD
    if pick == WAIT:
        return False
    if pick == KNOWN:
        _settle_known(db, client, sid, found, names, test)
        return True
    staging = s.path("work") / STAGING / sid
    if not download(db, client, sid, share, staging):
        return False
    write_marks(staging, share)
    found = known_folder(s, sid)                      # the iCloud copy may have landed during the download
    pick = choose(s, db, found, share) if found is not None else UPLOAD
    if pick == WAIT:
        return False                                  # the staging stays: the next look downloads nothing twice
    if pick == KNOWN:
        shutil.rmtree(staging, ignore_errors=True)
        _settle_known(db, client, sid, found, names, test)
        return True
    deleted = None
    if found is not None and found.parent == s.path("inbox"):
        # WO47: no duplicates — the iCloud copy goes. WO49 (live 2026-10-24: iCloud held the copy 71 minutes in
        # "Resource deadlock avoided" and the whole upload waited behind it): the upload never waits for that — a copy
        # iCloud holds now is deleted when it lets go (`register` knows it by its share id: never a second batch;
        # WO54: nor a "📭 still on the iPhone" for it — pipeline.take_share)
        why = ("the upload is whole; the iCloud copy wasn't" if whole(share) else
               "the upload has more photos in hand than the iCloud copy")
        if cleanup.delete_copy(s, db, sid, found, f"{why} (WO47: no duplicates)"):
            deleted = found.name
        else:
            db.log(sid, "share_icloud_copy_left", {"share_id": sid, "folder": found.name})
    from thrift_agent import alerts, pipeline
    intake = pipeline.uploads_dir(s)                   # WO49: the Mac's own disk — iCloud can never hold the upload
    target = free_name(intake / sid)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(target))
    db.log(sid, "share_fetched", {"share_id": sid, "photos": names, "test": test, "folder": target.name,
                                  "intake": "local", **({"icloud_deleted": deleted} if deleted else {})})
    alerts.progress(db, f"upload {sid} fetched")
    client.post(f"/shares/{sid}/fetched", {"names": names})
    return True


def held_dir(s: Settings) -> Path:
    """WO56 §6: where a share that didn't finish uploading waits — beside the uploads' intake on the Mac's own disk,
    never in it (`pipeline.upload_folders` leaves it out): nothing here is ever grouped, carded or posted."""
    from thrift_agent import pipeline
    return pipeline.uploads_dir(s) / pipeline.UPLOADS_HELD


def held_copy(s: Settings, sid: str) -> Path | None:
    """The held folder of this share, if it is held."""
    folder = held_dir(s) / sid
    return folder if (folder / HELD_FILE).is_file() else None


def held_meta(folder: Path) -> dict | None:
    try:
        meta = json.loads((folder / HELD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _save_meta(folder: Path, meta: dict) -> None:
    part = folder / f".{HELD_FILE}.part"
    part.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(part, folder / HELD_FILE)


def hold(s: Settings, db: DB, client, share: dict, sid: str, names: list[str]) -> bool:
    """WO56 §6: a share that ended short or never ended, with no iCloud copy — never taken short (a card from 7 of 12
    photos would list an item with photos missing, and they never come later: the Shortcut is done). Its photos are
    downloaded (checked, as any upload) into held/<share id> with `_held.json` (what the upload was: count, received,
    its photos' SHA-256 and memory keys, when it last moved), the API told "fetched" — Azure's copy deleted, its
    "hasn't taken it" watch quiet, at the moment it was taken before — and the ledger knows it. Its one line comes
    from `held_tick` HOLD_AFTER after its last photo. Idempotent: a share held already is only told fetched again.
    False when it must be fetched again (a photo's download failed)."""
    from thrift_agent import shares
    target = held_dir(s) / sid
    count = share.get("count") if isinstance(share.get("count"), int) else None
    received = int(share.get("received") or 0)
    if held_meta(target) is None:
        staging = s.path("work") / STAGING / sid
        if not download(db, client, sid, share, staging):
            return False
        write_marks(staging, share)
        from thrift_agent import photomem
        from thrift_agent.ingest import prep  # HEIC opens once prep has registered its opener
        keys = {}
        for p in sorted(staging.iterdir()):
            if prep.is_photo(p):
                k = photomem.keys_of(p)
                keys[p.name] = {x: k.get(x) for x in ("sha", "capture", "phash", "colors")}
        last = max((t for t in (_time(share.get("last_received_at")), _time(share.get("ended_at")),
                                _time(share.get("started_at"))) if t is not None), default=None)
        _save_meta(staging, {"share_id": sid, "count": count, "received": received, "ended": bool(share.get("ended")),
                             "test": bool(share.get("test")), "started_at": share.get("started_at"),
                             "last_at": (last or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                             "held_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "shas": sorted({str(p.get("sha256") or "")[:16] for p in share.get("photos") or []
                                             if isinstance(p, dict) and p.get("sha256")}),
                             "keys": keys, "names": names, "said": None})
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)               # a half-made held folder of a crash before
        shutil.move(str(staging), str(target))
        db.log(sid, "share_held_fetched", {"share_id": sid, "count": count, "received": received,
                                           "ended": bool(share.get("ended")), "photos": names})
    try:
        shares.seen(db, sid, "azure", count, received, test=bool(share.get("test")) or None)
    except Exception as e:  # noqa: BLE001 — the ledger is a record
        db.log(sid, "share_ledger_failed", f"{type(e).__name__}: {e}")
    client.post(f"/shares/{sid}/fetched", {"names": names})
    return True


def _replaced_by(s: Settings, db: DB, sid: str, meta: dict) -> tuple[str, str] | None:
    """A share carrying any of this held share's photos: ("replaced" | "repeat", what it was), else None.
    - a NEWER share in Azure's listings (kv `uploads_shas`; share ids sort by their time) — she shared again; an older
      one there doesn't count (it may be a held share itself: the newest held one stays held);
    - the photo memory: a photo known with an item — made into it after this share started (shared again:
      "replaced") or before (these photos had their item already: "repeat")."""
    from thrift_agent import photomem, pipeline
    mine = set(meta.get("shas") or [])
    try:
        listed = json.loads(db.kv_get(SHAS) or "{}")
    except ValueError:
        listed = {}
    for other, row in sorted(listed.items() if isinstance(listed, dict) else []):
        if other > sid and isinstance(row, dict) and mine & set(row.get("shas") or []):
            return "replaced", f"shared again at {pipeline.share_time(Path(other))}"
    started = _time(meta.get("started_at"))
    for keys in (meta.get("keys") or {}).values():
        row = photomem.known(db, keys or {})
        if row is None:
            continue
        seen = _time(row["seen_at"])
        name = photomem.title_of(db, row["item_id"]) if row["item_id"] else "a newer share"
        if started is not None and seen is not None and seen < started:
            return "repeat", f"same photos as {name}"
        return "replaced", f"shared again ({name})"
    return None


def held_tick(s: Settings, db: DB, now: datetime | None = None, force: bool = False) -> list[str]:
    """WO56 §6, from the worker's look (every HELD_EVERY seconds at most): each held share —
    - taken from its other copy since (the iCloud copy, by its own rules): this copy goes (`share_held_dropped`);
    - shared again (`_replaced_by`): it goes, the ledger `replaced` (or `repeat`) — the new share is the item's;
    - an iCloud copy of it waiting in the inbox: that copy's rules decide (whole, or partial after 30 minutes), nothing
      said;
    - else HOLD_AFTER after its last photo: ONE plain line, "📸 The photos shared at 7:42 PM didn't finish uploading —
      please share them again." (a test share's to the private chat, 🧪), the ledger `held` with how many came;
    - HELD_KEEP after it was held: its folder goes (the ledger keeps `held`).
    Returns the lines said. Never raises (a held share that can't be read is left for the next look)."""
    now = now or datetime.now(timezone.utc)
    base = held_dir(s)
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    if not folders:
        return []
    if not force and now.timestamp() - float(db.kv_get(HELD_LOOK) or 0) < HELD_EVERY:
        return []
    db.kv_set(HELD_LOOK, str(now.timestamp()))
    from thrift_agent import pipeline, shares
    said = []
    for folder in folders:
        sid = folder.name
        meta = held_meta(folder)
        if meta is None:
            continue
        try:
            other = pipeline._taken_as(db, sid, folder)
            if other is not None:
                shutil.rmtree(folder, ignore_errors=True)
                db.log(sid, "share_held_dropped", {"share_id": sid, "why": "the share was taken from its other copy",
                                                   "batch": other})
                continue
            replaced = _replaced_by(s, db, sid, meta)
            if replaced is not None:
                outcome, why = replaced
                shutil.rmtree(folder, ignore_errors=True)
                shares.outcome(db, sid, outcome, f"{meta.get('received')} of {meta.get('count') or '?'} photos came; "
                                                 f"{why}")
                db.log(sid, "share_replaced", {"share_id": sid, "outcome": outcome, "why": why})
                continue
            if _icloud_copy_waits(s, sid):
                continue                                          # its own rules (pipeline.take_share)
            last = _time(meta.get("last_at")) or now
            if not meta.get("said") and (now - last).total_seconds() >= HOLD_AFTER:
                text = HELD_LINE.format(when=pipeline.share_time(folder))
                meta["said"] = now.isoformat(timespec="seconds")
                _save_meta(folder, meta)
                if meta.get("test"):
                    notify.say(f"🧪 {text}")
                else:
                    notify.group(text)
                have, want = meta.get("received"), meta.get("count")
                shares.outcome(db, sid, "held", f"{have} of {want or '?'} photos came — the upload stopped; held, "
                                               "never posted, until she shares them again")
                db.log(sid, "share_held", {"share_id": sid, "received": have, "count": want,
                                           "ended": meta.get("ended"), "said": "group" if not meta.get("test") else "ops"})
                said.append(text)
                continue
            held = _time(meta.get("held_at")) or now
            if (now - held).total_seconds() >= HELD_KEEP:
                shutil.rmtree(folder, ignore_errors=True)
                db.log(sid, "share_held_expired", {"share_id": sid, "held_at": meta.get("held_at")})
        except Exception as e:  # noqa: BLE001 — never the look's end
            db.log(sid, "share_held_failed", f"{type(e).__name__}: {e}"[:300])
    return said


def _icloud_copy_waits(s: Settings, sid: str) -> bool:
    """An iCloud copy of this share in the inbox (not taken yet): its own rules take it."""
    try:
        return any(p.is_dir() and shares_id(p.name) == sid for p in s.path("inbox").iterdir())
    except OSError:
        return True                                               # the inbox unreadable now: say nothing yet


def shares_id(name: str) -> str:
    from thrift_agent import shares
    return shares.share_id_of(name)


ASIDE = "-icloud"                       # WO46's archive name of an iCloud copy set aside (WO47 deletes it instead)
UPLOAD, KNOWN, WAIT = "upload", "known", "wait"     # `choose`: take the upload · the folder found is the share · not yet


def choose(s: Settings, db: DB, found: Path, share: dict) -> str:
    """Which copy of a share is THE share, the upload (takeable) or the folder found for it (WO46, WO54):
    - KNOWN: the folder is ours (an upload taken before), in the archive, or an iCloud copy already taken (a batch) —
      or the share was taken from another of its copies;
    - UPLOAD: the upload is whole — it wins at once over an iCloud copy not taken yet, whole or not (WO54: never a
      share held to a slower copy); or it is short and has at least as many photos in hand as the iCloud copy has
      on the Mac (the iCloud copy unreadable: none);
    - KNOWN too: a short upload beside an iCloud copy that is whole (marker, every file downloaded, the count covered);
    - WAIT: a short upload beside an iCloud copy with MORE photos in hand, neither taken nor whole — the iCloud copy's
      own rules take it (whole, or partial after 30 min), and only then is the upload settled; until then Azure keeps
      its copy (WO54: the API was told "fetched" for an iCloud copy that was empty, and Azure's photos were gone)."""
    if found.parent != s.path("inbox") or source_of(found) == "azure":
        return KNOWN
    if db.conn.execute("SELECT 1 FROM batches WHERE src_dir=?", (str(found),)).fetchone():
        return KNOWN                                  # taken already: it is the share
    from thrift_agent import pipeline, shares
    if pipeline._taken_as(db, str(share.get("share_id") or shares.share_id_of(found)), found) is not None:
        return KNOWN                                  # taken from another copy ("<id> 2", "-late"): never a 2nd batch
    if whole(share):
        return UPLOAD
    have = icloud_in_hand(s, found)
    if have is None:
        return UPLOAD                                 # iCloud still coordinating it: the upload's photos are here now
    photos, covered = have
    if covered:
        return KNOWN                                  # the iCloud copy is whole; the upload is short
    return UPLOAD if int(share.get("received") or 0) >= photos else WAIT


def icloud_behind(s: Settings, db: DB, found: Path, share: dict) -> bool:
    """WO46's question, kept for its callers: the upload is the copy to take (`choose`)."""
    return choose(s, db, found, share) == UPLOAD


def icloud_in_hand(s: Settings, found: Path) -> tuple[int, bool] | None:
    """The iCloud copy's photos on the Mac now (cloud-only ones not counted) and whether it is whole — the Shortcut's
    end mark there, nothing cloud-only, its count covered by the photos with their companions (WO42). None when macOS
    can't read it now (iCloud busy with it)."""
    from thrift_agent import pipeline
    from thrift_agent.ingest import prep
    try:
        pending = prep.icloud_placeholders(found)
        files = [f for f in found.iterdir() if not f.name.startswith(".")]
        marker = pipeline.marker_of(found, s["inbox"]["done_marker"])
        cloud = {p.name for p in pending}
        photos = [f for f in files if prep.is_photo(f) and f.name not in cloud]
        media = [f for f in files if f.is_file() and f.suffix.lower() in pipeline.MEDIA_EXT and f.name not in cloud]
        want = pipeline.expected_photos(marker) if marker is not None else None
    except OSError:
        return None
    covered = marker is not None and not pending and bool(photos) and (want is None or len(photos) + len(media) >= want)
    return len(photos), covered


def free_name(path: Path, sep: str = " ") -> Path:
    """`path`, else `path 2`, `path 3` … (iCloud's own way of naming a second folder), the first that doesn't exist."""
    out, n = path, 1
    while out.exists():
        n += 1
        out = path.with_name(f"{path.name}{sep}{n}")
    return out


def _settle_known(db: DB, client, sid: str, found: Path, names: list[str], test: bool) -> None:
    """The share's folder is here already: the iCloud copy (said once), or ours whose fetched call didn't go.
    WO45 follow-up: the iCloud copy of a TEST share carries no mark of its own — the API's word puts `_test` into the
    folder while it still waits in the inbox (the intake then makes a 🧪 card, never a real one); a copy taken
    before the API was asked is told to the owner (its card is a real one: 'same item' retires it)."""
    if source_of(found) != "azure":
        db.log(sid, "share_same_as_icloud", {"share_id": sid, "folder": found.name, "photos": names, "test": test})
        try:
            from thrift_agent import shares
            shares.seen(db, sid, "azure", received=len(names), test=test or None)     # the ledger: both copies came
        except Exception as e:  # noqa: BLE001 — the ledger is a record
            db.log(sid, "share_ledger_failed", f"{type(e).__name__}: {e}")
        if test:
            if found.parent.name != "archive" and not (found / "_test").exists():
                try:
                    (found / "_test").write_text("1", encoding="utf-8")
                    db.log(sid, "share_marked_test", {"share_id": sid, "folder": found.name})
                except OSError as e:
                    db.log(sid, "share_mark_failed", f"{type(e).__name__}: {e}")
            elif found.parent.name == "archive":
                # WO54 §2: what happened and what follows — no reply asked (nothing is listed without a price)
                notify.say(f"🧪 The iCloud copy of test share {sid} was taken before the API said it was a test — its "
                           "card is a real one in the group; nothing is listed unless it gets a price.")
    client.post(f"/shares/{sid}/fetched", {"names": names})


def known_folder(s: Settings, sid: str) -> Path | None:
    """A folder of this share in the inbox (`<share id>`, `<share id>-late` …) or the archive (`YYYYMMDD_<share id>`,
    `…_2`), else None."""
    from thrift_agent import pipeline
    local = pipeline.uploads_dir(s)                    # WO49: the uploads' own intake and archive, first
    late = pipeline.late_archive(s)                    # WO54 §4: a share taken short, archived inside iCloud Drive
    for base in (local, local / pipeline.UPLOADS_ARCHIVE, s.path("inbox"), s.path("archive"),
                 *([late] if late != s.path("archive") else [])):
        try:
            folders = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for folder in folders:
            if folder.name.endswith(ASIDE) or re.search(ASIDE + r"_\d+$", folder.name):
                continue                              # WO46: an iCloud copy set aside for the upload is not the share
            if folder.name.startswith(sid) or re.match(r"\d{8}_" + re.escape(sid), folder.name):
                return folder
    return None


def source_of(folder: Path) -> str:
    """"azure" for a folder this module made, else "icloud"."""
    try:
        return "azure" if (folder / SOURCE_FILE).is_file() and "azure" in (folder / SOURCE_FILE).read_text()[:20] \
            else "icloud"
    except OSError:
        return "icloud"


def download(db: DB, client, sid: str, share: dict, staging: Path) -> bool:
    """Every photo of the share into `staging`, byte for byte and checked; False when one must be fetched again."""
    staging.mkdir(parents=True, exist_ok=True)
    for photo in share.get("photos") or []:
        if not isinstance(photo, dict):
            continue
        name = safe_name(photo.get("name"))
        path = str(photo.get("path") or "")
        if not name or not path.startswith(PHOTO_PATH):
            db.log(sid, "upload_bad_name", {"share_id": sid, "name": str(photo.get("name"))[:80], "path": path[:120]})
            continue
        target = staging / name
        size = photo.get("size")
        if target.is_file() and (not isinstance(size, int) or target.stat().st_size == size):
            continue                                                    # already here, whole
        data, _ = get_bytes(client, path)
        want = str(photo.get("sha256") or "")
        if want and hashlib.sha256(data).hexdigest() != want:
            db.log(sid, "upload_bad_hash", {"share_id": sid, "name": name})
            target.unlink(missing_ok=True)
            return False
        part = staging / f".{name}.part"                                # a dot-file: never a photo to the intake
        part.write_bytes(data)
        os.replace(part, target)
    return True


def write_marks(folder: Path, share: dict) -> None:
    """The Shortcut's marks as the intake reads them: `_done.txt` "done <count>" (the share's count, else what came),
    `_source.txt` "azure", `_test` "1" for a test share."""
    photos = len(share.get("photos") or [])
    count = share.get("count") if isinstance(share.get("count"), int) else None
    (folder / DONE_FILE).write_text(f"done {count or int(share.get('received') or 0) or photos}", encoding="utf-8")
    (folder / SOURCE_FILE).write_text("azure", encoding="utf-8")
    if share.get("test"):
        (folder / TEST_FILE).write_text("1", encoding="utf-8")


def get_bytes(client, path: str) -> tuple[bytes, str]:
    """A photo's bytes and Content-Type from the API: the client's own `get_bytes` when it has one, else the request
    made here with its URL and key (sales.Api answers JSON only), a longer timeout for a 30 MB photo."""
    fetch = getattr(client, "get_bytes", None)
    if fetch is not None:
        return fetch(path)
    headers = {"x-functions-key": client.key}
    try:
        if getattr(client, "client", None) is not None:
            r = client.client.get(client.url + path, headers=headers)
        else:
            r = httpx.get(client.url + path, headers=headers, timeout=DOWNLOAD_TIMEOUT)
    except httpx.HTTPError as e:
        raise sales.ApiError(f"GET {path}: {type(e).__name__}") from None
    if r.status_code >= 300:
        raise sales.ApiError(f"GET {path}: HTTP {r.status_code}", r.status_code)
    return r.content, r.headers.get("content-type", "")


def safe_name(raw: object) -> str:
    """A photo's file name as the API listed it, with no path and never a dot-file (the inbox ignores those)."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._ ")
    return name[:80]


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
