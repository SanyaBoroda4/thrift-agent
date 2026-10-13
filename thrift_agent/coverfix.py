"""WO37 §2 — a live listing's cover fixed on each site's edit page (the owner, 2026-10-12: "put the right photo first
and change nothing else"; "if a site's edit page can't do that reliably, list those listings and ask the owner before
any delete-and-repost").

- Depop and Vinted: the right photo moved first in the edit page's photo grid (the extension's "cover" job: the grid's
  own keyboard reordering, ext/content/common.js T.coverFirst); the new order checked — that photo first, every other
  one in its order — before Save, pressed once; then the listing page's first photo must be that photo.
- Poshmark: its edit page has no order to change (its cover dialog only replaces, recorded 2026-10-12): the cover is
  replaced by the right photo's 3:4 cover, built as the listing's own (prep.portrait_cover), and that photo's tile gets
  the old cover's own photo — the listing keeps the same photos. Update once; the public listing's cover must change.
- A dry run moves / replaces on the page and never saves. The poster must be stopped (the bridge's port, Poshmark's
  Chrome profile); Poshmark's part runs in the owner's GUI session (`bash deploy/gui_once.sh cover-fix …`).
The right photo: the owner's (`--photo N`, her Nth photo of the item, from 1), else the item's cover by the WO37 rule
(its labelled check; asked now when there is none — one model call)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from thrift_agent import crosslist, notify, pipeline
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import prep
from thrift_agent.schema import Facts

SITES = ("poshmark", "depop", "vinted")


@dataclass
class Plan:
    mp: str
    url: str
    photos: list[str]            # the listing's photos as posted, in order (the first is the cover file)
    position: int | None         # where the right photo is among them (None: not in this listing)


def right_photo(s: Settings, db: DB, iid: str, photo: int | None) -> tuple[int, int, str]:
    """(the item's photo index, its upright turn, why): the owner's `--photo N` (from 1), else the WO37 rule on the
    item's labelled check — the stored one, else asked now (one model call)."""
    it = db.item(iid)
    photos, kinds = pipeline._photos_of(it)
    check = pipeline._stored_check(it)
    turn = {v.photo: v.upright for v in (check.views if check else [])}
    if photo is not None:
        if not 1 <= photo <= len(photos):
            raise ValueError(f"this item has photos 1 to {len(photos)} — there is no photo {photo}")
        return photo - 1, turn.get(photo - 1, 0), "the owner's choice"
    facts = Facts.model_validate(loads(it["facts"]))
    if not pipeline.labelled(check):
        check = pipeline.front_view(s, db, iid, photos, facts, kinds)
    pick, upright, note = pipeline.choose_cover(facts, len(photos), kinds, check)
    if pick is None:
        raise ValueError("no photo could be the cover")
    return pick, upright, note or "the whole item from the front (WO37)"


def plans(db: DB, iid: str, right: Path, mps: list[str] | None = None) -> list[Plan]:
    """Each live listing of the item (posted, with an address), its photos as posted and where `right` is among
    them: Poshmark's from the listing's render, Depop's and Vinted's from the fields their form got."""
    it = db.item(iid)
    renders = loads(it["renders"]) or {}
    out = []
    for row in db.conn.execute("SELECT * FROM listings WHERE item_id=? AND status='posted' AND url IS NOT NULL",
                               (iid,)).fetchall():
        mp = row["marketplace"]
        if mps and mp not in mps:
            continue
        if mp == "poshmark":
            photos = list((renders.get("poshmark") or {}).get("photos") or [])
        else:
            photos = list((loads(row["fields_json"]) or {}).get("photos") or [])
        position = next((i for i, p in enumerate(photos) if Path(p) == right), None)
        out.append(Plan(mp, row["url"], photos, position))
    return sorted(out, key=lambda p: SITES.index(p.mp) if p.mp in SITES else 9)


async def fix(s: Settings, db: DB, iid: str, mps: list[str] | None = None, photo: int | None = None,
              dry: bool = False) -> list[str]:
    """The item's live covers fixed (or, `dry`, only tried on the edit pages). Returns one line per site; a real run
    also tells the private chat once for the item."""
    from thrift_agent.post import takedown
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    title = pipeline._title_of(it)
    pick, upright, why = right_photo(s, db, iid, photo)
    d = Path(it["dir"])
    right = d / "photos" / f"{pick:02d}.jpg"
    old = Facts.model_validate(loads(it["facts"])).cover_photo
    lines, todo = [], []
    for plan in plans(db, iid, right, mps):
        site = crosslist.LABEL.get(plan.mp, plan.mp)
        if plan.position is None:
            lines.append(f"{site}: photo {pick + 1} isn't among the listing's photos — left as it is (ask the owner)")
        elif plan.position == 0 or (plan.mp == "poshmark" and old == pick):
            lines.append(f"{site}: photo {pick + 1} is the cover already")
        else:
            todo.append(plan)
    if not todo:
        return lines or ["no live listing to fix"]
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ps, bridge, pw, ctx = await takedown._open(s, [p.mp for p in todo])
    done = []
    try:
        for plan in todo:
            site = crosslist.LABEL.get(plan.mp, plan.mp)
            try:
                if plan.mp == "poshmark":
                    width, height = prep.cover_dims(s["images"]["cover_size"])
                    cover = prep.portrait_cover(right, d / f"cover-fix-{pick:02d}.jpg", width, height, rotate=upright)
                    swap = (plan.position, prep.portrait_cover(d / "photos" / f"{old:02d}.jpg",
                                                               d / f"cover-fix-swap-{old:02d}.jpg", width, height)
                            ) if old is not None else None     # 3:4 too: the photo dialog's frame takes it whole
                    out = await ps["poshmark"].set_cover(ctx, plan.url, cover, swap, dry=dry, shots=shots)
                else:
                    ps[plan.mp].shot = shots / f"{iid}-{plan.mp}-{stamp}-cover.png"
                    out = await ps[plan.mp].set_cover(plan.url, plan.position, len(plan.photos), dry=dry)
            except Exception as e:  # noqa: BLE001 — one site's failure is said; the others go on
                out = {"ok": False, "saved": False, "error": f"{type(e).__name__}: {e}"[:300]}
            db.log(iid, "cover_fix", {"mp": plan.mp, "photo": pick, "position": plan.position, "dry": dry,
                                      **{k: out.get(k) for k in ("ok", "saved", "error", "shot")}})
            if out.get("ok"):
                done.append(site)
                lines.append(f"{site}: {'moved first on the edit page (dry run: not saved)' if dry else 'fixed'} — "
                             f"photo {pick + 1}")
            else:
                lines.append(f"{site}: not fixed — {out.get('error')}"
                             + (" (it WAS saved: look at the listing)" if out.get("saved") else ""))
    finally:
        await takedown._close(bridge, pw, ctx)
    if not dry:
        notify.say(f"🖼 Cover of {title}: photo {pick + 1} ({why}) — " + "; ".join(lines))
    return lines
