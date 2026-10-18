"""WO45 §10: the replay harness — a saved share run through the whole intake (the photo memory, the grouping, the
items' extraction, cover, copy and gate) in a scratch workspace with a scratch database, never the live one, with
the model's answers recorded once and replayed after (brain/llm.Answers: CI and the Mac's pre-deploy run cost no
Opus call), and the result compared with what was expected.

A golden case is a folder: `shares/<share folder>…` (one or more share folders, taken in name order — the six Oct 17
folders are one case), optional `late/<share folder>/` (photos that come after the share was taken: added to its
archive folder and taken as late photos), `expected.yaml` and `answers.json`. `thrift replay <case> [--record]
[--card]` runs one; `thrift replay --golden` runs every case under private/tests/golden (never in the public repo:
the photos are the owner's); tests/test_golden.py runs them in pytest where that folder exists.

What the harness doesn't do: fill the sites' forms (a dry run of a live item is `thrift crosslist --dry-run <item>` on
the running poster; the scratch items aren't in the live database)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from thrift_agent import pipeline, shares
from thrift_agent.brain import llm
from thrift_agent.config import ROOT, Settings
from thrift_agent.db import DB, loads

GOLDEN = ROOT / "private" / "tests" / "golden"
ANSWERS = "answers.json"
EXPECTED = "expected.yaml"


def scratch(base: Settings, work: Path) -> Settings:
    """The live settings with every path under `work`, Telegram off and the dev role (nothing posts, nothing is
    said); the models and prompts as they are."""
    data = json.loads(json.dumps(base.data, default=str))
    data["paths"] = {k: str(work / k) for k in base.data["paths"]}
    data["paths"]["db"] = str(work / "state.db")
    data["telegram"] = {**(data.get("telegram") or {}), "enabled": False}
    data["machine_role"] = "dev"
    data.setdefault("poster", {})["dry_run"] = True
    s = Settings(data)
    s.ensure_dirs()
    return s


def run(base: Settings, case: Path, work: Path, record: bool = False, card=None) -> dict:
    """One golden case replayed in `work`: {"shares": [{id, batch, status, reasons, outcome}], "items": [...],
    "mismatches": [...], "answers": {"hits", "misses"}}; `card(iid, caption, cover)` is called for each card the
    owner would get (thrift replay --card: the private chat, marked 🧪)."""
    if work.exists():
        shutil.rmtree(work)
    s = scratch(base, work)
    db = DB(s.path("db"))
    folders = sorted(p for p in (case / "shares").iterdir() if p.is_dir()) if (case / "shares").is_dir() else [case]
    recorder = llm.Answers(case / ANSWERS, record=record)
    llm.RECORDER = recorder
    said: list[str] = []
    from thrift_agent import notify
    keep_say, keep_group = notify.say, notify.group
    notify.say = lambda text, **k: said.append(f"[private] {text}")       # type: ignore[assignment]
    notify.group = lambda text, **k: said.append(f"[group] {text}")       # type: ignore[assignment]
    out: dict = {"shares": [], "items": [], "said": said}
    try:
        for folder in folders:
            dest = s.path("inbox") / folder.name
            shutil.copytree(folder, dest)
            out["shares"].append(_take(s, db, dest))
        late = case / "late"
        if late.is_dir():
            for folder in sorted(p for p in late.iterdir() if p.is_dir()):
                arch = next((p for p in s.path("archive").iterdir() if p.name.endswith(folder.name)), None)
                if arch is None:
                    out["shares"].append({"id": folder.name, "status": "late photos: no archive folder to join"})
                    continue
                for p in folder.iterdir():
                    shutil.copy2(p, arch / p.name)
                for b in db.conn.execute("SELECT id FROM batches WHERE src_dir LIKE ?", (f"%{folder.name}",)):
                    pipeline.late_photos(s, db, force=[b["id"]])
                for b in db.batches("new"):
                    out["shares"].append(_process(s, db, b["id"], shares.share_id_of(pipeline.batch_dir(b))))
        for it in db.conn.execute("SELECT * FROM items ORDER BY created_at, seq").fetchall():
            out["items"].append(_item(db, it))
            if card is not None and it["status"] in ("awaiting_price", "needs_info", "ready"):
                from thrift_agent import approve
                caption, _price = approve.item_caption(it["id"], db.item(it["id"]))
                card(it["id"], caption, Path(it["dir"]) / "cover.jpg")
    finally:
        notify.say, notify.group = keep_say, keep_group
        llm.RECORDER = None
        db.conn.close()                                        # the scratch workspace can be removed (Windows locks)
    out["answers"] = {"hits": recorder.hits, "misses": recorder.misses}
    expected = yaml.safe_load((case / EXPECTED).read_text(encoding="utf-8")) if (case / EXPECTED).exists() else None
    out["mismatches"] = compare(out, expected) if expected else []
    return out


def _take(s: Settings, db: DB, dest: Path) -> dict:
    sid = shares.share_id_of(dest)
    bid = pipeline.register(s, db, dest)
    if bid is None:
        row = shares.of_batch(db, "") or db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
        return {"id": sid, "batch": None, "status": "not registered",
                "outcome": row["outcome"] if row is not None else None}
    return _process(s, db, bid, sid)


def _process(s: Settings, db: DB, bid: str, sid: str) -> dict:
    from thrift_agent import photomem
    pipeline.process_batch(s, db, bid)
    photomem.say_skipped(db)                                   # the worker's look says the repeats of the look
    for it in db.conn.execute("SELECT id FROM items WHERE batch_id=? AND status='new' ORDER BY seq", (bid,)).fetchall():
        pipeline.process_item(s, db, it["id"])
    b = db.batch(bid)
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    return {"id": sid, "batch": bid, "status": b["status"], "reasons": loads(b["reasons"]) or [],
            "outcome": row["outcome"] if row is not None else None,
            "items": db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid,)).fetchone()[0]}


def _item(db: DB, it) -> dict:
    facts = loads(it["facts"]) or {}
    posh = (loads(it["renders"]) or {}).get("poshmark") or {}
    d = Path(it["dir"])
    photos = sorted((d / "photos").glob("*.jpg"))
    roles = {r["photo"]: r["role"] for r in facts.get("photo_roles") or [] if isinstance(r, dict)}
    cover = facts.get("cover_photo")
    return {"id": it["id"], "batch": it["batch_id"], "status": it["status"], "photos": len(photos),
            "title": posh.get("title"), "department": facts.get("department"), "category": facts.get("category"),
            "subcategory": facts.get("subcategory"), "size": (facts.get("size_us") or {}).get("value"),
            "brand": (facts.get("brand") or {}).get("value"), "condition": facts.get("condition"),
            "cover": cover, "cover_role": roles.get(cover), "price": (loads(it["price"]) or {}).get("list_price"),
            "late": sorted(pipeline.late_indices(d)),
            "size_buttons": not (facts.get("size_us") or {}).get("value") and it["status"] != "dropped"}


def compare(out: dict, expected: dict) -> list[str]:
    """What differs from expected.yaml: `items: N` (items made, dropped ones aside), `outcomes: [carded, repeat, …]`
    (per share, in order), `each: [{photos, department, category, size, cover_role, title_contains, brand,
    size_buttons}]` (per item, in order; only the keys given are checked), `said_contains: [...]` (lines the owner
    would get)."""
    problems = []
    items = [i for i in out["items"] if i["status"] != "dropped"]
    if "items" in expected and len(items) != int(expected["items"]):
        problems.append(f"{len(items)} items, expected {expected['items']}")
    if "outcomes" in expected:
        got = [s.get("outcome") for s in out["shares"]]
        if got != list(expected["outcomes"]):
            problems.append(f"outcomes {got}, expected {expected['outcomes']}")
    for k, want in enumerate(expected.get("each") or []):
        if k >= len(items):
            problems.append(f"item {k + 1}: missing")
            continue
        got = items[k]
        for key, value in (want or {}).items():
            if key == "title_contains":
                if value.lower() not in (got.get("title") or "").lower():
                    problems.append(f"item {k + 1}: title {got.get('title')!r} lacks {value!r}")
            elif key == "photos_at_least":
                if int(got.get("photos") or 0) < int(value):
                    problems.append(f"item {k + 1}: {got.get('photos')} photos, expected at least {value}")
            elif str(got.get(key)) != str(value):
                problems.append(f"item {k + 1}: {key} {got.get(key)!r}, expected {value!r}")
    for text in expected.get("said_contains") or []:
        if not any(text in line for line in out["said"]):
            problems.append(f"nothing said containing {text!r}")
    return problems


def cases(root: Path = GOLDEN) -> list[Path]:
    """The golden cases: every folder under private/tests/golden with shares/ or photos in it."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and ((p / "shares").is_dir() or (p / EXPECTED).exists()))


def summary(out: dict) -> str:
    lines = ["shares: " + "; ".join(f"{s['id']} → {s.get('status')}" + (f" ({s['outcome']})" if s.get("outcome") else "")
                                     for s in out["shares"])]
    for i in out["items"]:
        lines.append(f"  {i['id']} {i['status']}: {i['photos']} photos · {i['department']} › {i['category']}"
                     f"{' › ' + i['subcategory'] if i.get('subcategory') else ''} · size {i['size'] or '—'}"
                     f"{' (buttons)' if i['size_buttons'] else ''} · cover {i['cover']} ({i['cover_role']}) · "
                     f"{i['title']}")
    lines.append(f"  answers: {out['answers']['hits']} replayed, {out['answers']['misses']} asked")
    lines += [f"  ✗ {m}" for m in out["mismatches"]] or ["  ✓ as expected"]
    return "\n".join(lines)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
