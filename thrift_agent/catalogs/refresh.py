"""Keeping the catalogs current (WO30 §8): `thrift catalogs refresh [--depop] [--vinted]`, and once a week in the
daily window (`crosslist.refresh_days`), the poster re-reads the same read-only APIs from the Mac's logged-in Chrome
and rewrites data/<mp>_catalog.json in the same format — only when the new file validates — with the diff (new,
removed or renamed categories and options) in the ops chat. A table row whose target has gone gets a ❗ line; that
row then simply isn't used (catalogs.categories falls back to the model with the catalog's enum).

What is re-read: Vinted's category tree and colours (a leaf the old file knows keeps its fields; a new leaf gets its
fields from the attributes API, else it is left out and named in the diff); Depop's size APIs (kept as raw evidence —
its category menu is the form's own and is not re-read here). Every raw answer is kept in data/catalog_raw/."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thrift_agent import catalogs, notify
from thrift_agent.catalogs import categories
from thrift_agent.db import DB, loads

REQUESTS = "catalog_refresh_requests"      # kv: [marketplace]: asked for by `thrift catalogs refresh`
LAST = "catalog_refreshed"                 # kv: {marketplace: when it was last re-read}
RAW_DIR = catalogs.DATA_DIR / "catalog_raw"
MAX_NEW = 40                 # more new leaves than this: the tree wasn't read as recorded — nothing is written
MAX_ATTRIBUTE_READS = 20     # attributes API calls for new leaves per refresh (read-only, but paced)
_FETCH_JS = """async ([u, method, body, csrf]) => {
  try {
    const h = {'Accept': 'application/json'};
    if (body) h['Content-Type'] = 'application/json';
    if (csrf) h['X-CSRF-Token'] = csrf;
    const r = await fetch(u, {method: method || 'GET', credentials: 'include', headers: h,
                              body: body ? JSON.stringify(body) : undefined});
    if (!r.ok) return {error: r.status};
    return await r.json();
  } catch (e) { return {error: String(e)}; }
}"""


def request(db: DB, mps: list[str]) -> None:
    with db.tx():
        db.kv_set(REQUESTS, json.dumps(sorted(set((loads(db.kv_get(REQUESTS)) or []) + list(mps)))))


def take_requests(db: DB) -> list[str]:
    with db.tx():
        mps = loads(db.kv_get(REQUESTS)) or []
        if mps:
            db.kv_set(REQUESTS, "[]")
    return mps


def due(s, db: DB, mp: str, now: datetime | None = None) -> bool:
    """A week (crosslist.refresh_days) since the last refresh. The first time it is asked, the clock starts now: the
    shipped catalogs are fresh, so the first automatic refresh is a week later (`thrift catalogs refresh` any time)."""
    stamps = loads(db.kv_get(LAST)) or {}
    now = now or datetime.now(timezone.utc)
    if mp not in stamps:
        stamps[mp] = now.isoformat(timespec="seconds")
        db.kv_set(LAST, json.dumps(stamps))
        return False
    days = float(s.get("crosslist.refresh_days") or 7)
    return now - datetime.fromisoformat(stamps[mp]) >= timedelta(days=days)


def _leaves(nodes: list, trail: tuple[str, ...] = ()) -> dict[int, str]:
    """{id: "Women > Clothing > Skirts"} of the tree's leaves (Vinted: {"catalogs": [{"id", "title", "catalogs": …}]})."""
    out: dict[int, str] = {}
    for n in nodes or []:
        if not isinstance(n, dict) or "id" not in n:
            continue
        path = (*trail, str(n.get("title") or "").strip())
        kids = n.get("catalogs") or n.get("children") or []
        if kids:
            out.update(_leaves(kids, path))
        else:
            out[int(n["id"])] = " > ".join(path)
    return out


def diff_paths(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """The changes between two {id or key: path} maps, as lines for the ops chat."""
    lines = []
    for k in sorted(set(new) - set(old), key=str):
        lines.append(f"+ {new[k]}")
    for k in sorted(set(old) - set(new), key=str):
        lines.append(f"- {old[k]}")
    for k in sorted(set(old) & set(new), key=str):
        if old[k] != new[k]:
            lines.append(f"~ {old[k]} → {new[k]}")
    return lines


def missing_targets(mp: str) -> list[str]:
    """The table rows whose target the current catalog no longer has (❗ lines: those rows go to the model)."""
    out = []
    for (dept, cat, sub), (depop_path, spec) in categories.TABLE.items():
        if mp == "depop" and depop_path and f"{dept} > {depop_path}" not in catalogs.depop_catalog().categories:
            out.append(f"{dept} > {cat}{' > ' + sub if sub else ''} → Depop {dept} > {depop_path}")
        if mp == "vinted" and spec is not None:
            for g in (("girls", "boys") if dept == "Kids" else (None,)):
                path = categories._vinted_path(dept, spec, g)
                if path and catalogs.vinted_catalog().leaf(path) is None:
                    out.append(f"{dept} > {cat}{' > ' + sub if sub else ''} → Vinted {path}")
    return out


async def _fetch(page, url: str, method: str = "GET", body=None, csrf: str | None = None):
    return await page.evaluate(_FETCH_JS, [url, method, body, csrf])


def _keep_raw(mp: str, name: str, data) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{mp}-{name}-{datetime.now():%Y%m%d}.json").write_text(json.dumps(data, indent=1, default=str)[:5_000_000],
                                                                      encoding="utf-8")


async def refresh_vinted(ctx) -> tuple[dict | None, list[str]]:
    """(the new catalog dict or None, the lines for the ops chat)."""
    old_cat = catalogs.vinted_catalog()
    old = json.loads(catalogs.path("vinted").read_text(encoding="utf-8"))
    page = await ctx.new_page()
    try:
        await page.goto("https://www.vinted.com/items/new")
        await page.wait_for_load_state("domcontentloaded")
        csrf = await page.evaluate("() => document.querySelector('meta[name=\"csrf-token\"]')?.content || null")
        tree = await _fetch(page, "/api/v2/item_upload/catalogs")
        colors = await _fetch(page, "/api/v2/item_upload/colors")
        _keep_raw("vinted", "catalogs", tree)
        _keep_raw("vinted", "colors", colors)
        if not isinstance(tree, dict) or tree.get("error") or not tree.get("catalogs"):
            why = (tree or {}).get("error") if isinstance(tree, dict) else type(tree).__name__
            return None, [f"vinted: the category tree didn't read ({why})"]
        leaves = {cid: p for cid, p in _leaves(tree["catalogs"]).items() if p.split(" > ")[0] in ("Women", "Men", "Kids")}
        new_ids = [cid for cid in leaves if str(cid) not in old["categories"]]
        if len(new_ids) > MAX_NEW or len(leaves) < len(old["categories"]) // 2:
            return None, [f"vinted: {len(leaves)} leaves read, {len(new_ids)} new — not the recorded tree, nothing "
                          f"written (the raw answer is in data/catalog_raw/)"]
        new_cats, unread = {}, []
        for cid, path in leaves.items():
            if str(cid) in old["categories"]:
                new_cats[str(cid)] = {**old["categories"][str(cid)], "path": path}
                continue
            if len(unread) < MAX_ATTRIBUTE_READS:
                attrs = await _fetch(page, "/api/v2/item_upload/attributes", "POST",
                                     {"attributes": [{"code": "category", "value": [cid]}]}, csrf)
                _keep_raw("vinted", f"attributes-{cid}", attrs)
            unread.append(path)                     # a new leaf: its fields need the recorded transform (kept raw)
        lines = diff_paths({k: c.path for k, c in old_cat.categories.items()}, {k: v["path"] for k, v in new_cats.items()})
        lines += [f"+ {p} (new — fields not read, left out until recorded)" for p in unread]
        new = {**old, "categories": new_cats, "_refreshed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if isinstance(colors, dict) and isinstance(colors.get("colors"), list) and colors["colors"]:
            fresh = [{"id": c["id"], "title": c["title"], "hex": c.get("hex")} for c in colors["colors"]
                     if isinstance(c, dict) and "id" in c and "title" in c]
            if fresh:
                lines += diff_paths({c.id: c.title for c in old_cat.colors}, {c["id"]: c["title"] for c in fresh})
                new["colors"] = fresh
        return new, lines
    finally:
        await page.close()


async def refresh_depop(ctx) -> tuple[dict | None, list[str]]:
    """Depop's size APIs, kept raw; the catalog file is left as it is (its categories are the form's own menu)."""
    page = await ctx.new_page()
    try:
        await page.goto("https://www.depop.com/products/create/")
        await page.wait_for_load_state("domcontentloaded")
        lines = []
        apis = (catalogs.depop_catalog().model_extra or {}).get("source_apis") or {}
        for name, url in apis.items():
            data = await _fetch(page, url.split(" ", 1)[-1])
            _keep_raw("depop", name, data)
            lines.append(f"depop {name}: {'read' if not (isinstance(data, dict) and data.get('error')) else data}")
        return None, lines
    finally:
        await page.close()


async def run(s, db: DB, ctx, mps: list[str]) -> list[str]:
    """Re-read `mps`; write a catalog only when the new one validates; tell the ops chat what changed."""
    report = []
    for mp in mps:
        try:
            new, lines = await (refresh_vinted(ctx) if mp == "vinted" else refresh_depop(ctx))
        except Exception as e:  # noqa: BLE001 — a refresh never stops the poster
            new, lines = None, [f"{mp}: refresh failed ({type(e).__name__}: {e})"]
        if new is not None:
            tmp = catalogs.path(mp).with_suffix(".new.json")
            tmp.write_text(json.dumps(new, indent=1, ensure_ascii=False), encoding="utf-8")
            try:
                catalogs.load(mp, tmp)
                tmp.replace(catalogs.path(mp))
            except catalogs.CatalogError as e:
                lines.append(f"{mp}: the new catalog doesn't validate, the old one stays ({e})")
                tmp.unlink(missing_ok=True)
        gone = missing_targets(mp)
        lines += [f"❗ {g} — that row goes to the model now" for g in gone]
        stamps = loads(db.kv_get(LAST)) or {}
        stamps[mp] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.kv_set(LAST, json.dumps(stamps))
        db.log(None, "catalog_refreshed", {"mp": mp, "changes": len(lines)})
        report += [f"{mp}: {len(lines)} change(s)" if lines else f"{mp}: no change"] + lines[:40]
    notify.say("📚 catalogs refreshed\n" + "\n".join(report))
    return report


async def run_standalone(s, db: DB, mps: list[str]) -> list[str]:
    from thrift_agent.post.base import open_browser
    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    try:
        return await run(s, db, ctx, mps)
    finally:
        await ctx.close()
        await pw.stop()
