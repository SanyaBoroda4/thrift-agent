"""WO43: size buttons when no photo shows the size (the owner, 2026-10-17: shoes almost never do, and every shoe card
asked her to type `size …`). When the item takes a size and none was read, the card offers sizes like the price
buttons — eight in two rows of four (letters in one row), the agent's best guess marked ⭐ in the middle, "‹ smaller ·
bigger ›" underneath when there are more — right above the price rows. She taps one; the guess alone never goes on a
listing. The values are Poshmark's own menu for the category (what the poster selects from), kept to those Depop and
Vinted take too (`crosslist_ok`: the catalogs' mappers, never a made-up value — WO36); shoes of a brand that marks
sizes in EU show the EU size next to the US one. The guess: the shop's own history for the brand and category, then
the category's usual size."""
from __future__ import annotations

import json
from collections import Counter

from thrift_agent.brain import sizes, taxonomy
from thrift_agent.db import DB, loads
from thrift_agent.schema import Facts

ROW = 4
SHOWN = 8
LETTERS = ("XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL")
PAGE_KEY = "size_page"              # kv: {item: the window's first index} — "‹ smaller · bigger ›" taps
EU_BRANDS = {"birkenstock", "zara", "massimo dutti", "mango", "camper", "ecco", "geox", "dr. martens", "superga",
             "veja", "hunter", "bershka", "pull & bear", "stradivarius", "h&m", "cos", "arket", "paul green",
             "gabor", "rieker", "tamaris", "lloyd", "pikolinos", "fly london", "arche", "repetto"}
EU_WOMEN = {"5": "35", "5.5": "36", "6": "36.5", "6.5": "37", "7": "37.5", "7.5": "38", "8": "38.5", "8.5": "39",
            "9": "39.5", "9.5": "40", "10": "40.5", "10.5": "41", "11": "41.5", "11.5": "42", "12": "42.5"}
EU_MEN = {"6": "39", "6.5": "39.5", "7": "40", "7.5": "40.5", "8": "41", "8.5": "42", "9": "42.5", "9.5": "43",
          "10": "44", "10.5": "44.5", "11": "45", "11.5": "45.5", "12": "46", "13": "47", "14": "48"}
DEFAULTS = {("Women", "Shoes"): "8", ("Men", "Shoes"): "10", ("Kids", "Shoes"): "12 (Toddler Girl)",
            ("Women", "Jeans"): "27", ("Men", "Jeans"): "Waist 32", ("Men", "Pants"): "Waist 32",
            ("Women", "Intimates & Sleepwear"): "34B", ("Women", "Dresses"): "M", ("Women", "Pants & Jumpsuits"): "M"}


def _rank(v: str) -> float:
    r = sizes._size_rank(v)
    return r if r is not None else 1e9


def menu_for(facts: Facts) -> tuple[str | None, list[str]]:
    """(tab, the sizes Poshmark's menu offers for this item), in the menu's order; ([], None) for a category without
    menus or a one-size one."""
    menus = taxonomy.size_menus(facts.department, facts.category, facts.subcategory)
    if not menus or all(v == ["One Size"] for v in menus.values()):
        return None, []
    if facts.department == "Kids":
        tab = "Boys" if facts.kids_gender == "boys" else "Girls"
        tab = tab if tab in menus else next(iter(menus))
    else:
        tab = "Standard" if "Standard" in menus else next(iter(menus))
    return tab, [v for v in menus.get(tab) or [] if v != "One Size"]


def system_of(values: list[str], facts: Facts) -> str:
    if facts.category == "Shoes":
        return "shoes"
    if all(v.upper() in LETTERS for v in values):
        return "letters"
    if any(v.startswith("Waist") for v in values):
        return "waist"
    if facts.category == "Intimates & Sleepwear":
        return "bra"
    return "numbers"


def eu_label(value: str, facts: Facts) -> str | None:
    """"EU 38" for a shoe of a brand that marks sizes in EU, else None."""
    brand = (facts.brand.value or "").strip().lower()
    if facts.category != "Shoes" or brand not in EU_BRANDS:
        return None
    table = EU_MEN if facts.department == "Men" else EU_WOMEN
    eu = table.get(value)
    return f"EU {eu}" if eu else None


def history_guess(db: DB, facts: Facts, values: list[str]) -> str | None:
    """The size she has listed most for this brand and category, else for the category — only one of `values`."""
    brand = (facts.brand.value or "").strip().lower()
    rows = db.conn.execute("SELECT facts, renders FROM items WHERE status IN ('posted', 'ready', 'posting', 'sold') "
                           "ORDER BY created_at DESC LIMIT 400").fetchall()
    by_brand, by_cat = Counter(), Counter()
    for r in rows:
        f, posh = loads(r["facts"]) or {}, (loads(r["renders"]) or {}).get("poshmark") or {}
        if f.get("department") != facts.department or f.get("category") != facts.category:
            continue
        v = posh.get("size_value") or ((f.get("size_us") or {}).get("value") if isinstance(f.get("size_us"), dict)
                                       else None)
        if not v or v not in values:
            continue
        by_cat[v] += 1
        if brand and ((f.get("brand") or {}).get("value") or "").strip().lower() == brand:
            by_brand[v] += 1
    for c in (by_brand, by_cat):
        if c:
            return c.most_common(1)[0][0]
    return None


def guess(db: DB, facts: Facts, values: list[str]) -> str | None:
    """The ⭐: the shop's history, else the category's usual size, else the middle of the menu."""
    if not values:
        return None
    if (v := history_guess(db, facts, values)) is not None:
        return v
    want = DEFAULTS.get((facts.department, facts.category))
    if want in values:
        return want
    return values[len(values) // 2]


def crosslist_ok(facts: Facts, values: list[str]) -> list[str]:
    """The values Depop and Vinted take too (their catalogs' mappers; a catalog that can't be read keeps them all —
    the poster says what it couldn't map, never a made-up value)."""
    try:
        from thrift_agent import catalogs
        from thrift_agent.catalogs import categories
        from thrift_agent.catalogs.sizes import SizeIn, depop_size, vinted_size
        dcat, vcat = catalogs.depop_catalog(), catalogs.vinted_catalog()
        dpick = categories.resolve("depop", facts.department, facts.category, facts.subcategory, facts.item_type,
                                   kids_gender=facts.kids_gender, ask=lambda *a: None)
        vpick = categories.resolve("vinted", facts.department, facts.category, facts.subcategory, facts.item_type,
                                   kids_gender=facts.kids_gender, ask=lambda *a: None)
    except Exception:  # noqa: BLE001 — no catalog, no table row: the menu stands
        return values
    out = []
    for v in values:
        s_in = SizeIn(department=facts.department, category=facts.category, subcategory=facts.subcategory,
                      item_type=facts.item_type, value=v, tab=None, printed=None)
        ok = True
        try:
            if dpick is not None and (sid := dcat.size_set_id(dpick.value)) is not None:
                depop_size(s_in, dcat.size_set(sid), dcat.size_sets_US[sid].path)
            if vpick is not None:
                leaf = vcat.category(int(vpick.value))
                if (f := leaf.fields.get("size")) is not None and f.lib and f.required:
                    vinted_size(s_in, f.lib, vcat.library(f.lib))
        except Exception:  # noqa: BLE001 — that site has no such value
            ok = False
        if ok:
            out.append(v)
    return out or values


def offer(db: DB, it, facts: Facts | None = None) -> dict | None:
    """The card's size offer for an item with no size: {"values", "star", "system", "tab", "start", "shown", "eu"}
    or None (a one-size category, no menu)."""
    facts = facts or Facts.model_validate(loads(it["facts"]))
    if sizes.one_size(facts.department, facts.category, facts.subcategory):
        return None
    tab, values = menu_for(facts)
    if not values:
        return None
    values = crosslist_ok(facts, values)
    letters = [v for v in values if v.upper() in LETTERS]
    values = letters + [v for v in values if v.upper() not in LETTERS]       # a mixed menu: the letters first
    star = guess(db, facts, values)
    system = "letters" if letters and star in letters else system_of(values, facts)
    pages = loads(db.kv_get(PAGE_KEY)) or {}
    start = pages.get(it["id"])
    if start is None:
        start = 0 if system == "letters" or len(values) <= SHOWN else max(0, min(values.index(star) - 3,
                                                                                  len(values) - SHOWN))
    start = max(0, min(int(start), max(0, len(values) - SHOWN)))
    width = len(letters) if system == "letters" and start == 0 and len(letters) <= SHOWN else SHOWN
    return {"values": values, "star": star, "system": system, "tab": tab, "start": start,
            "shown": values[start:start + width], "eu": {v: eu_label(v, facts) for v in values}
            if facts.category == "Shoes" else {}}


def shift(db: DB, iid: str, o: dict, direction: int) -> dict:
    """"‹ smaller" / "bigger ›": the window moved by one row, kept in kv for the card's next render."""
    start = max(0, min(o["start"] + direction * ROW, max(0, len(o["values"]) - SHOWN)))
    pages = loads(db.kv_get(PAGE_KEY)) or {}
    pages[iid] = start
    db.kv_set(PAGE_KEY, json.dumps(pages))
    return {**o, "start": start, "shown": o["values"][start:start + SHOWN]}


def forget_page(db: DB, iid: str) -> None:
    pages = loads(db.kv_get(PAGE_KEY)) or {}
    if iid in pages:
        del pages[iid]
        db.kv_set(PAGE_KEY, json.dumps(pages))


def label(o: dict, v: str, chosen: str | None) -> str:
    base = v
    if eu := (o.get("eu") or {}).get(v):
        base = f"{v} · {eu}"
    if chosen is not None and v == chosen:
        return f"✓ {base}"
    return f"⭐{base}" if v == o["star"] else base


def buttons(iid: str, o: dict, chosen: str | None = None) -> list[list[dict]]:
    """The size rows: eight in two rows of four (letters: one row), ⭐ on the guess, ✓ on the one tapped; then
    "‹ smaller · bigger ›" when the menu has more than shown. Callbacks: size:<item>:<value>, sizes:<item>:-1|1."""
    shown = o["shown"]
    rows = []
    if o["system"] == "letters" and len(shown) <= SHOWN:
        rows.append([{"text": label(o, v, chosen), "callback_data": f"size:{iid}:{v}"} for v in shown])
    else:
        for k in range(0, len(shown), ROW):
            rows.append([{"text": label(o, v, chosen), "callback_data": f"size:{iid}:{v}"}
                         for v in shown[k:k + ROW]])
    if len(o["values"]) > len(shown):
        nav = []
        if o["start"] > 0:
            nav.append({"text": "‹ smaller", "callback_data": f"sizes:{iid}:-1"})
        if o["start"] + SHOWN < len(o["values"]):
            nav.append({"text": "bigger ›", "callback_data": f"sizes:{iid}:1"})
        if nav:
            rows.append(nav)
    return rows
