"""WO54 §4 — ONE-TIME move of the share archive out of iCloud Drive into the Mac's own disk, for Alex to run on the Mac by
hand (the owner approved it on 2026-11-09). The agent itself never moves an existing archive folder.

    cd ~/thrift-agent
    bash deploy/services.sh stop worker                      # the worker archives shares and reads these folders
    .venv/bin/python deploy/move_archive.py                  # the plan: what moves, what stays and why (nothing moved)
    .venv/bin/python deploy/move_archive.py --go             # move them
    bash deploy/services.sh start worker

From ~/Library/Mobile Documents/com~apple~CloudDocs/Posh/archive (`--from`) to the local archive (`--to`; default
`paths.archive` when it is already local, else ~/thrift/var/local.noindex/archive — a .noindex folder, which
Spotlight never indexes). Then the one settings line (config/settings.local.yaml, paths:) —
    archive: ~/thrift/var/local.noindex/archive
— and every new whole share goes there by itself (pipeline.archive_share); a share taken short still waits in iCloud's
Posh/archive, where its late photos land (pipeline.late_archive).

Each folder of the iCloud archive:
- files iCloud keeps only in the cloud are pulled first (`brctl download` and a first-byte read, up to `--wait` s);
  one that doesn't arrive → the folder is NOT moved, and listed with those files;
- a batch on it still in flight (new / needs_confirm / regroup) → not moved, listed;
- a share taken short in the last 2 days (its late photos may still land in that folder) → not moved, listed, unless
  `--all`;
- else copied file by file (dates kept), the copy checked against the folder — the same names, each the same size,
  the same count and bytes — and only then deleted from iCloud Drive. A copy that doesn't match is removed again and
  the folder stays where it was.
The records follow each moved folder: an `archived` event with its new place for each batch archived there
(late_photos and the cleanup read the last one) and the in-place late batches' src_dir. The database is backed up first
(state.db.bak-move-archive-<time>, SQLite's own backup). What it did: printed, in ~/thrift/logs/move_archive-<time>.json,
and the event `archive_moved`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from thrift_agent import pipeline  # noqa: E402
from thrift_agent.config import settings  # noqa: E402
from thrift_agent.db import DB, loads  # noqa: E402
from thrift_agent.ingest import prep  # noqa: E402

ICLOUD_ARCHIVE = "~/Library/Mobile Documents/com~apple~CloudDocs/Posh/archive"
LOCAL_ARCHIVE = "~/thrift/var/local.noindex/archive"
LATE_WINDOW = timedelta(days=2)                      # late_photos looks this far back
IN_FLIGHT = ("new", "needs_confirm", "regroup")


def cloud_only(d: Path) -> list[Path]:
    """The files in this folder iCloud keeps only in the cloud (dataless, or an old `.name.icloud` placeholder)."""
    return sorted(p for p in d.rglob("*") if p.name.endswith(".icloud") or (p.is_file() and prep.dataless(p)))


def pull(d: Path, wait: float, run=subprocess.run, sleep=time.sleep) -> list[Path]:
    """Ask iCloud for the folder's cloud-only files and wait up to `wait` seconds for them. Returns those still not
    here (an `.icloud` placeholder counts as here once its real file is)."""
    missing = cloud_only(d)
    if not missing or sys.platform != "darwin":
        return missing
    for target in [d, *(p for p in missing if not p.name.endswith(".icloud"))]:
        run(["brctl", "download", str(target)], check=False, capture_output=True)
    for p in missing:
        if not p.name.endswith(".icloud"):
            try:
                run(["head", "-c", "1", str(p)], check=False, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                pass
    end = time.monotonic() + wait
    while (missing := cloud_only(d)) and time.monotonic() < end:
        sleep(2)
    return missing


def _dests(db: DB) -> dict[str, str]:
    """{batch: the folder it was last archived to} (the `archived` events, the last one wins)."""
    out: dict[str, str] = {}
    for e in db.conn.execute("SELECT ref, detail FROM events WHERE kind='archived' ORDER BY ts"):
        d = (loads(e["detail"]) or {}).get("dest") if e["detail"] else None
        if e["ref"] and d:
            out[e["ref"]] = str(d)
    return out


def _made(batches, d: Path) -> datetime:
    times = []
    for b in batches:
        try:
            t = datetime.fromisoformat(str(b["created_at"]).replace("Z", "+00:00"))
            times.append(t if t.tzinfo else t.replace(tzinfo=timezone.utc))
        except (TypeError, ValueError):
            pass
    return min(times) if times else datetime.fromtimestamp(d.stat().st_mtime, timezone.utc)


def plan(s, db: DB, src_root: Path, dest_root: Path, every: bool = False,
         now: datetime | None = None) -> list[dict]:
    """[{"folder", "dest", "batches", "move", "why"}] for every folder of the iCloud archive. Nothing is touched."""
    now = now or datetime.now(timezone.utc)
    by_dest: dict[str, list[str]] = {}
    for bid, d in _dests(db).items():
        by_dest.setdefault(d, []).append(bid)
    rows = []
    for d in sorted(p for p in src_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        batches = [b for bid in by_dest.get(str(d), []) if (b := db.batch(bid)) is not None]
        batches += [b for b in db.conn.execute("SELECT * FROM batches WHERE src_dir=? OR src_dir LIKE ?",
                                               (str(d), f"{d}#%")).fetchall()
                    if all(b["id"] != x["id"] for x in batches)]
        dest = dest_root / d.name
        n = 1
        while dest.exists():
            n += 1
            dest = dest_root / f"{d.name}_{n}"
        row = {"folder": str(d), "dest": str(dest), "batches": [b["id"] for b in batches], "move": True,
               "why": "a share of ours" if batches else "no share of ours is recorded for it (moved all the same)"}
        if any(b["status"] in IN_FLIGHT for b in batches):
            row.update(move=False, why="a batch on it is still " + ", ".join(sorted({b["status"] for b in batches})))
        elif not every and batches and not pipeline.whole_share(s, d) and now - _made(batches, d) < LATE_WINDOW:
            row.update(move=False, why=f"taken short {(now - _made(batches, d)).total_seconds() / 3600:.0f} h ago: its "
                                       "late photos may still land here (run again in 2 days, or --all)")
        rows.append(row)
    return rows


def move(db: DB, row: dict, wait: float, pull_fn=pull) -> dict:
    """One folder: pulled, copied, checked, then deleted from iCloud Drive; the records follow. Returns what happened."""
    src, dest = Path(row["folder"]), Path(row["dest"])
    still = pull_fn(src, wait)
    if still:
        return {**row, "moved": False, "why": "files still only in iCloud (not downloaded)",
                "cloud_only": [str(p.relative_to(src)) for p in still]}
    try:
        files, size = pipeline.copy_verified(src, dest)
    except OSError as e:
        return {**row, "moved": False, "why": f"the copy didn't check out — left where it is ({type(e).__name__}: {e})"}
    for bid in row["batches"]:                     # the checked copy is the share's archive from here on
        b = db.batch(bid)
        if b is None:
            continue
        if str(b["src_dir"]).split("#")[0] == str(src):          # an in-place late batch: its folder is the new one
            db.conn.execute("UPDATE batches SET src_dir=? WHERE id=?",
                            (str(dest) + str(b["src_dir"])[len(str(src)):], bid))
        else:
            db.log(bid, "archived", {"dest": str(dest), "moved_from": str(src), "by": "deploy/move_archive.py"})
    try:
        shutil.rmtree(src)
    except OSError as e:
        return {**row, "moved": True, "files": files, "bytes": size,
                "why": f"copied and checked — the copy is its archive now; deleting it from iCloud failed "
                       f"({type(e).__name__}: {e}): what is left of {src} can go once iCloud lets go of it"}
    return {**row, "moved": True, "files": files, "bytes": size, "why": "copied, checked, deleted from iCloud Drive"}


def worker_running() -> bool:
    if sys.platform != "darwin":
        return False
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.thriftagent.worker"], capture_output=True,
                       text=True, timeout=10)
    return "state = running" in (r.stdout or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WO54 §4: the iCloud share archive moved to the Mac's own disk (once).")
    ap.add_argument("--go", action="store_true", help="move them (without it: the plan only, nothing moved)")
    ap.add_argument("--all", action="store_true", help="also the shares taken short in the last 2 days")
    ap.add_argument("--from", dest="src", default=None, help=f"the iCloud archive (default {ICLOUD_ARCHIVE})")
    ap.add_argument("--to", dest="dest", default=None, help=f"the local archive (default: paths.archive when it is "
                                                              f"local, else {LOCAL_ARCHIVE})")
    ap.add_argument("--wait", type=float, default=120.0, help="seconds to wait for a folder's cloud-only files")
    args = ap.parse_args(argv)
    s = settings()
    src_root = Path(os.path.expanduser(args.src or ICLOUD_ARCHIVE))
    configured = s.path("archive")
    dest_root = Path(os.path.expanduser(args.dest)) if args.dest else (
        configured if pipeline.ICLOUD_DRIVE not in str(configured) else Path(os.path.expanduser(LOCAL_ARCHIVE)))
    if pipeline.ICLOUD_DRIVE in str(dest_root):
        print(f"the destination {dest_root} is inside iCloud Drive — give --to a folder on the Mac's own disk")
        return 2
    if not src_root.is_dir():
        print(f"no iCloud archive at {src_root}: nothing to move")
        return 0
    db_path = s.path("db")
    db = DB(db_path)
    rows = plan(s, db, src_root, dest_root, every=args.all)
    for r in rows:
        print(f"{'MOVE' if r['move'] else 'stay'}  {Path(r['folder']).name}  ({', '.join(r['batches']) or 'no batch'})"
              f" — {r['why']}")
    todo = [r for r in rows if r["move"]]
    print(f"{len(todo)} to move, {len(rows) - len(todo)} staying: {src_root} → {dest_root}")
    if pipeline.ICLOUD_DRIVE in str(configured):
        print(f"then, in config/settings.local.yaml under paths:  archive: {LOCAL_ARCHIVE}")
    if not args.go or not todo:
        return 0
    if worker_running():
        print("the worker is running (it archives shares and reads these folders): bash deploy/services.sh stop "
              "worker, run this again, then start worker")
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-move-archive-{stamp}")
    with sqlite3.connect(str(backup)) as copy:
        db.conn.backup(copy)
    print(f"database backed up: {backup}")
    dest_root.mkdir(parents=True, exist_ok=True)
    done = []
    for r in todo:
        out = move(db, r, args.wait)
        done.append(out)
        what = (f"moved, {out['files']} files, {out['bytes']} bytes, checked" if out.get("moved") else "NOT moved")
        print(f"{Path(r['folder']).name}: {what} — {out['why']}"
              + (f" ({', '.join(out['cloud_only'][:5])})" if out.get("cloud_only") else ""))
    report = {"at": stamp, "from": str(src_root), "to": str(dest_root), "moved": sum(1 for x in done if x.get("moved")),
              "not_moved": [x for x in done if not x.get("moved")], "staying": [r for r in rows if not r["move"]],
              "folders": done}
    log = Path(os.path.expanduser("~/thrift/logs")) / f"move_archive-{stamp}.json"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"what it did: {log}")
    except OSError as e:
        print(f"(the report couldn't be written: {e})")
    db.log(None, "archive_moved", {"moved": report["moved"], "of": len(todo), "to": str(dest_root),
                                   "not_moved": [Path(x["folder"]).name for x in report["not_moved"]]})
    print(json.dumps({"moved": report["moved"], "of": len(todo), "not_moved": len(report["not_moved"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
