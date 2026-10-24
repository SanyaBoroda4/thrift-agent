"""WO47 (2026-10-19, the owner's photo storage policy): one copy of a photo, no duplicates, nothing kept for the sake
of it — the daily plan and run, what it never touches, the intake's deletion of a second copy, the low-disk line."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import cleanup, pipeline, sales
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB

NOW = datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=9)
FRESH = NOW - timedelta(days=2)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    said = []
    monkeypatch.setattr(cleanup.notify, "say", said.append)
    monkeypatch.setattr(cleanup.sales, "api", lambda: None)
    return s, db, said


def photo(p: Path, color="red", size=(60, 80)) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(p)
    return p


def batch(s, db, name: str, status: str, made: datetime, n: int = 2, reasons=None, test=False, items=1,
          work_copy=True, archived=True):
    """A share: its batch (made then), its archive folder with n photos, `items` items with (or without) their work
    copies, the `archived` event — as the intake leaves them."""
    src = s.path("inbox") / name
    bid = db.add_batch(str(src), n)
    db.set_batch(bid, status=status, reasons=reasons)
    if test:
        db.set_batch(bid, test=1)
    db.conn.execute("UPDATE batches SET created_at=? WHERE id=?", (iso(made), bid))
    dest = s.path("archive") / f"{made:%Y%m%d}_{name}"
    if archived:
        for i in range(n):
            photo(dest / f"IMG_{i + 1}.jpeg")
        (dest / "_done.txt").write_text(f"done {n}", encoding="utf-8")
        db.log(bid, "archived", {"dest": str(dest)})
    work = s.path("work") / bid
    for i in range(n):
        photo(work / "all" / f"{i:02d}.jpg")
    ids = []
    for k in range(items):
        d = work / f"item_{k + 1:02d}"
        iid = db.add_item(bid, k + 1, str(d))
        if work_copy:
            photo(d / "photos" / "00.jpg")
            photo(d / "cover.jpg")
        d.mkdir(parents=True, exist_ok=True)
        (d / "item.json").write_text("{}", encoding="utf-8")
        ids.append(iid)
    db.log(bid, "batch_registered", {"folder": name, "photos": n, "names": [f"IMG_{i + 1}.jpeg" for i in range(n)]})
    return bid, dest, ids


def kinds(removals):
    return sorted((r.kind, Path(r.path).name) for r in removals)


# ---------------------------------------------------------------- the plan

def test_the_plan_removes_what_the_policy_names_and_keeps_the_rest(env):
    s, db, said = env
    old_split, old_dest, [kept_item] = batch(s, db, "2026-10-10_100000_1111", "split", OLD)
    fresh_split, fresh_dest, _ = batch(s, db, "2026-10-17_100000_2222", "split", FRESH)
    no_copy, no_copy_dest, [orphan] = batch(s, db, "2026-10-09_100000_3333", "split", OLD, work_copy=False)
    old_failed, failed_dest, _ = batch(s, db, "2026-10-09_110000_4444", "failed", OLD, items=0,
                                       reasons=["repeat share: same photos as i_1"])
    test_b, test_dest, _ = batch(s, db, "2026-10-09_120000_5555", "split", OLD, test=True)
    in_flight, flight_dest, _ = batch(s, db, "2026-10-09_130000_6666", "new", OLD, items=0)
    aside = s.path("archive") / "20261019_2026-10-19_225345_9873-icloud"
    photo(aside / "IMG_2214.jpeg")
    unknown = s.path("archive") / "20261001_2026-10-01_090000_7777"
    photo(unknown / "IMG_1.jpeg")
    import os
    os.utime(unknown, (OLD.timestamp(), OLD.timestamp()))
    stray = s.path("archive") / "notes"
    stray.mkdir()
    second = s.path("inbox") / "2026-10-17_100000_2222 2"
    photo(second / "IMG_1.jpeg")
    late = s.path("inbox") / "2026-10-17_100000_2222-late"
    photo(late / "IMG_9.jpeg")
    staging_old = s.path("work") / "uploads" / "2026-10-05_100000_8888"
    photo(staging_old / "IMG_1.jpeg")
    os.utime(staging_old, (OLD.timestamp(), OLD.timestamp()))
    staging_new = s.path("work") / "uploads" / "2026-10-19_100000_9999"
    photo(staging_new / "IMG_1.jpeg")

    removals, notes = cleanup.plan(s, db, NOW)
    assert kinds(removals) == sorted([
        ("archive", old_dest.name), ("archive", failed_dest.name), ("archive", test_dest.name),
        ("duplicate", aside.name), ("duplicate", second.name), ("unknown", unknown.name),
        ("work_all", "all"), ("work_all", "all"), ("work_all", "all"),                       # the three split shares
        ("work_batch", old_failed), ("work_batch", test_b), ("staging", staging_old.name)])
    assert all(r.bytes > 0 for r in removals)
    assert any(orphan in n and "no work copy" in n for n in notes)
    assert any(flight_dest.name in n and "still new" in n for n in notes)
    assert all(fresh_dest.name not in Path(r.path).name for r in removals)          # within 7 days: kept
    assert all(late.name != Path(r.path).name and stray.name != Path(r.path).name for r in removals)
    why = {Path(r.path).name: r.why for r in removals}
    assert why[old_dest.name].startswith("share older than 7 days: 1 item; the items' photos are in the work folder")
    assert why[failed_dest.name] == "a repeat share; older than 7 days"
    assert why[test_dest.name] == "a test share; older than 7 days"
    assert "no share of ours" in why[unknown.name]
    # the in-flight batch's own all/ stays, the fresh split's all/ goes (registered names known)
    assert sum(1 for r in removals if r.kind == "work_all" and r.ref == in_flight) == 0
    assert sum(1 for r in removals if r.kind == "work_all" and r.ref == fresh_split) == 1


def test_the_run_removes_logs_and_says_one_line_and_a_dry_run_touches_nothing(env):
    s, db, said = env
    old_split, old_dest, _ = batch(s, db, "2026-10-10_100000_1111", "split", OLD)
    before = sorted(p.name for p in s.path("archive").iterdir())
    out = cleanup.run(s, db, dry=True, now=NOW)
    assert out["dry"] and out["removed"] == 2 and sorted(p.name for p in s.path("archive").iterdir()) == before
    assert (s.path("work") / old_split / "all").is_dir() and said == []
    assert db.kv_get(cleanup.LAST) is None
    out = cleanup.run(s, db, now=NOW)
    assert not out["dry"] and out["removed"] == 2 and out["failed"] == 0
    assert not old_dest.exists() and not (s.path("work") / old_split / "all").exists()
    assert (s.path("work") / old_split / "item_01" / "cover.jpg").is_file()           # the work copy: kept
    assert said == [cleanup.line(out["removals"], out["freed"])]
    assert said[0].startswith("🧹 Cleanup: freed ") and "1 share folder older than 7 days (archive), 1 prep copy." in said[0]
    ev = db.conn.execute("SELECT detail FROM events WHERE kind='cleanup' ORDER BY rowid").fetchall()
    assert len(ev) == 2 and json.loads(ev[1]["detail"])["removed"] == 2
    assert [r["path"] for r in json.loads(ev[1]["detail"])["removals"]] == [old_dest.name, "all"]
    assert json.loads(db.kv_get(cleanup.LAST))["freed"] == out["freed"]
    assert cleanup.run(s, db, now=NOW)["removed"] == 0 and len(said) == 1                # nothing left: silent


def test_sold_and_shipped_items_lose_their_photos(env, monkeypatch):
    s, db, said = env
    bid, dest, [sold, kept, queued] = batch(s, db, "2026-10-17_100000_2222", "split", FRESH, items=3)
    for iid in (sold, kept, queued):
        db.set_item(iid, status="posted")
        db.upsert_listing(iid, "poshmark", status="posted", url=f"https://poshmark.com/listing/x-{iid}")
    db.upsert_listing(sold, "poshmark", status="sold")
    db.upsert_listing(queued, "depop", status="queued")

    class Api:
        def get(self, path, params=None):
            assert (path, params) == ("/sales", {"state": "all"})
            return {"sales": [
                {"id": "s1", "item_id": sold, "marketplace": "poshmark", "status": "matched",
                 "shipped_at": "2026-10-18T10:00:00+00:00"},
                {"id": "s2", "item_id": queued, "marketplace": "vinted", "status": "done", "shipped_at": None,
                 "delivered_at": "2026-10-18"},
                {"id": "s3", "item_id": kept, "marketplace": "depop", "status": "cancelled", "shipped_at": "2026-10-18"},
                {"id": "s4", "item_id": "i_unknown", "marketplace": "depop", "status": "matched", "shipped_at": "2026-10-18"},
                {"id": "s5", "item_id": kept, "marketplace": "depop", "status": "matched", "shipped_at": None}]}
    removals, notes = cleanup.plan(s, db, NOW, api=Api())
    assert [(r.kind, r.ref) for r in removals if r.kind == "sold"] == [("sold", sold)]
    assert any(queued in n and "queued or posting" in n for n in notes)
    cleanup.run(s, db, now=NOW, api=Api())
    d = Path(db.item(sold)["dir"])
    assert not (d / "cover.jpg").exists() and list((d / "photos").glob("*")) == [] and (d / "item.json").is_file()
    assert (Path(db.item(kept)["dir"]) / "cover.jpg").is_file()
    assert "1 sold item's photos" in said[-1]
    assert dest.is_dir()                                                              # 2 days old: the archive stays
    monkeypatch.setattr(cleanup.sales, "api", lambda: Api())
    assert cleanup.shipped_items(db) == {sold: {"site": "poshmark", "when": "2026-10-18"},
                                         queued: {"site": "vinted", "when": "2026-10-18"},
                                         "i_unknown": {"site": "depop", "when": "2026-10-18"}}


def test_the_api_out_of_reach_means_no_sold_photos_go(env):
    s, db, said = env

    class Down:
        def get(self, path, params=None):
            raise sales.ApiError("GET /sales: ConnectError")
    assert cleanup.shipped_items(db, Down()) == {}
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='cleanup_api_down'").fetchone()[0] == 1


def test_a_removal_that_fails_is_logged_and_the_rest_goes_on(env, monkeypatch):
    s, db, said = env
    a, dest_a, _ = batch(s, db, "2026-10-10_100000_1111", "split", OLD)
    b, dest_b, _ = batch(s, db, "2026-10-10_110000_2222", "split", OLD)
    real = cleanup.shutil.rmtree

    def flaky(path, *args, **kw):
        if Path(path).name == dest_a.name:
            raise OSError(11, "Resource deadlock avoided")
        return real(path, *args, **kw)
    monkeypatch.setattr(cleanup.shutil, "rmtree", flaky)
    out = cleanup.run(s, db, now=NOW)
    assert out["failed"] == 1 and dest_a.is_dir() and not dest_b.exists()
    assert "Resource deadlock avoided" in out["failures"][0]["error"]


def test_due_once_a_day_and_the_tick_runs_it_on_the_mac_only(env, monkeypatch):
    s, db, said = env
    monkeypatch.setattr(cleanup, "ENABLED", True)                                       # off for every other test
    assert cleanup.due(db) and not cleanup.due(db)
    monkeypatch.setattr(cleanup, "disk_free", lambda _s: (100 * 1024 ** 3, 228 * 1024 ** 3))
    runs = []
    monkeypatch.setattr(cleanup, "run", lambda *a, **k: runs.append(1))
    prod = Settings({**s.data, "machine_role": "prod"})
    cleanup.tick(prod, db)
    assert runs == []                                                                   # claimed a moment ago
    db.kv_set(cleanup.DAILY, iso(NOW - timedelta(days=2)))
    cleanup.tick(s, db)
    assert runs == [] and cleanup.due(db)                                               # dev: the tick does nothing
    cleanup.tick(prod, db)
    assert runs == [] and not cleanup.due(db)                                           # the dev tick above claimed?
    db.kv_set(cleanup.DAILY, iso(NOW - timedelta(days=2)))
    cleanup.tick(prod, db)
    assert runs == [1]


def test_the_low_disk_line_once_per_episode(env, monkeypatch):
    s, db, said = env
    gb = 1024 ** 3
    free = {"n": 1.3 * gb}
    monkeypatch.setattr(cleanup, "disk_free", lambda _s: (free["n"], 228 * gb))
    t = time.time()
    assert cleanup.disk_watch(s, db, now=t).startswith("⚠️ The Mac has 1.3 GB free (under 20 GB).")
    assert "Thrift's own folders:" in said[0]
    assert cleanup.disk_watch(s, db, now=t + 1) is None                               # looked at every 10 min
    assert cleanup.disk_watch(s, db, now=t + 601) is None and len(said) == 1          # still low: silent
    free["n"] = 21 * gb
    assert cleanup.disk_watch(s, db, now=t + 1202) is None                            # above 20, under 22: silent
    free["n"] = 30 * gb
    assert cleanup.disk_watch(s, db, now=t + 1803) == "✓ The Mac has 30.0 GB free again."
    free["n"] = 10 * gb
    assert cleanup.disk_watch(s, db, now=t + 2404).startswith("⚠️")                   # a new episode
    assert [r[0] for r in db.conn.execute("SELECT kind FROM events WHERE kind IN ('disk_low', 'disk_ok') ORDER BY rowid")] \
        == ["disk_low", "disk_ok", "disk_low"]


def test_delete_copy_only_inside_the_inbox(env):
    s, db, said = env
    inside = s.path("inbox") / "2026-10-19_225345_9873"
    photo(inside / "IMG_1.jpeg")
    (inside / ".IMG_2.jpeg.icloud").write_bytes(b"x")
    outside = s.path("archive") / "20261019_2026-10-19_225345_9873"
    photo(outside / "IMG_1.jpeg")
    assert cleanup.delete_copy(s, db, "2026-10-19_225345_9873", inside, "the upload is whole") is True
    assert not inside.exists()
    ev = json.loads(db.conn.execute("SELECT detail FROM events WHERE kind='share_copy_deleted'").fetchone()[0])
    assert ev["photos_here"] == 1 and ev["cloud_only"] == 1 and ev["bytes"] > 0 and ev["why"] == "the upload is whole"
    assert cleanup.delete_copy(s, db, "2026-10-19_225345_9873", outside, "no") is False and outside.is_dir()
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='share_copy_kept'").fetchone()[0] == 1


def test_the_second_copy_at_intake_is_deleted_not_archived(env, monkeypatch):
    """pipeline.register: the iCloud folder of a share the upload brought first, after it was taken (WO45) — deleted."""
    s, db, said = env
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    first = s.path("inbox") / "2026-10-19_223807_6801"
    photo(first / "IMG_1.jpeg")
    (first / "_done.txt").write_text("done 1", encoding="utf-8")
    bid = pipeline.register(s, db, first)
    db.set_batch(bid, status="split")
    pipeline.archive_share(s, db, bid, first)
    again = s.path("inbox") / "2026-10-19_223807_6801"
    photo(again / "IMG_1.jpeg")
    assert pipeline.register(s, db, again) is None and not again.exists()
    assert len(list(s.path("archive").iterdir())) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='share_copy_deleted'").fetchone()[0] == 1
    assert said == []


def test_sizes_and_human(env):
    s, db, said = env
    bid, dest, _ = batch(s, db, "2026-10-17_100000_2222", "split", FRESH)
    z = cleanup.sizes(s)
    assert z["archive"] > 0 and z["work_items"] > 0 and z["work_all"] > 0 and z["inbox"] == 0 and z["db"] > 0
    assert cleanup.human(900) == "900 B" and cleanup.human(1536) == "2 KB" and cleanup.human(1.5 * 1024 ** 3) == "1.5 GB"


# ---------------------------------------------------------------- rule 8 (Alex, 2026-10-20): the evidence folder

def test_evidence_older_than_14_days_goes_except_an_items_with_a_problem_still_open(env):
    import os
    s, db, said = env
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    _, _, [settled, stuck, idle] = batch(s, db, "2026-10-01_100000_1111", "split", OLD, items=3)
    for iid in (settled, stuck, idle):
        db.set_item(iid, status="posted")
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(settled, mp, status="posted", url=f"https://x/{mp}/{settled}",
                          listing_id="6ad6d53a105c58b9598c5eff" if mp == "poshmark" else None)
        if mp == "vinted":
            db.upsert_listing(stuck, mp, status="failed", error="Mismatch: photos 0 of 8 on the form")
        else:
            db.upsert_listing(stuck, mp, status="posted", url=f"https://x/{mp}/{stuck}",
                              listing_id="6ad6d53a105c58b9598c5e00" if mp == "poshmark" else None)
    ancient, young = NOW - timedelta(days=20), NOW - timedelta(days=5)

    def shot(name: str, when: datetime) -> Path:
        p = shots / name
        p.write_bytes(b"x" * 100)
        os.utime(p, (when.timestamp(), when.timestamp()))
        return p
    gone = [shot(f"{settled}-vinted-20261001-120000.png", ancient), shot(f"{settled}-vinted-20261001-120000.json", ancient),
            shot("6ad6d53a105c58b9598c5eff-poshmark-20261001-120000-price-dry.png", ancient),   # its Poshmark listing id
            shot("orders-probe-20261001.html", ancient)]                                            # no item in its name
    kept_ = [shot(f"{stuck}-vinted-20261001-120000.png", ancient),                                 # a problem still open
             shot("6ad6d53a105c58b9598c5e00-poshmark-20261001-120000.png", ancient),               # the same item, by id
             shot(f"{settled}-depop-20261015-120000.png", young)]                                  # within 14 days
    assert cleanup.open_problems(db) == {stuck: "its vinted listing is failed"}
    removals, notes = cleanup.plan(s, db, NOW)
    ev = [r for r in removals if r.kind == "evidence"]
    assert sorted(r.ref or "" for r in ev) == ["", settled]
    assert sorted(Path(f).name for r in ev for f in r.files) == sorted(p.name for p in gone)
    assert all(r.bytes == 100 * len(r.files) for r in ev)
    assert any(n.startswith(f"{stuck}: 2 evidence files older than 14 days kept") and "vinted listing is failed" in n
               for n in notes), notes
    cleanup.run(s, db, now=NOW)
    assert all(not p.exists() for p in gone) and all(p.exists() for p in kept_)
    assert "4 evidence files older than 14 days (1 item)" in said[-1], said
    assert shots.is_dir()
    # settled skips keep nothing open; a listing edit, a price change, a take-down or an unconfirmed publish do
    db.upsert_listing(stuck, "vinted", status="skipped", error="skipped: shared again as i_x")
    assert cleanup.open_problems(db) == {}
    db.upsert_listing(stuck, "vinted", status="skipped", error="skipped: no size on the item")
    assert cleanup.open_problems(db) == {stuck: "its vinted listing is skipped"}
    db.upsert_listing(stuck, "vinted", status="posted", url="https://x/v/1")
    db.conn.execute("INSERT INTO takedowns (id, item_id, marketplace, listing_url, status, attempts, fetched_at) "
                    "VALUES ('t1', ?, 'depop', 'https://x', 'waiting', 1, '2026-10-19')", (idle,))
    db.kv_set("unconfirmed_state", json.dumps({f"{settled}:vinted": {"stage": "wait"}}))
    assert cleanup.open_problems(db) == {idle: "a take-down is waiting", settled: "a publish is unconfirmed"}
