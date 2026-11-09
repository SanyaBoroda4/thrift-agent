"""Which of our items a sale is (WO33), never a guess:

1. the SKU — our item id (i_261001_abc123), which the Depop listing carries — or the listing's own address / id
   against `listings` (any status: an exact id is unambiguous);
2. else the title on the same marketplace: lowercase letters-and-digits words, difflib ratio ≥ 0.9 against the items'
   titles, among that marketplace's posted listings first, then among its delisted / sold ones (a buyer who paid while
   the take-down was under way must still make a "Sold twice"); a title the email cut short ("…") is compared with
   the same length of ours;
3. else no item: unmatched. Two different items equally close is also unmatched."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .db import Database
from .emails import listing_id_from_url

THRESHOLD = 0.9
LIVE = ("posted",)
WAS_LIVE = ("delisting", "delisted", "sold")


def norm_title(text: object) -> str:
    """"Lacoste Tee — White, size M!" → "lacoste tee white size m"."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def similarity(seen: object, ours: object) -> float:
    """difflib's ratio of the two normalised titles; a `seen` title ending in an ellipsis is compared with as much of
    `ours` as it has."""
    a, b = norm_title(seen), norm_title(ours)
    if not a or not b:
        return 0.0
    if str(seen).rstrip().endswith(("…", "...")) and len(b) > len(a):
        b = b[:len(a)]
    return SequenceMatcher(None, a, b).ratio()


def best(title: object, candidates: list[tuple[str, object]]) -> tuple[str | None, bool]:
    """The one key whose text is closest to `title` at ≥ THRESHOLD: (key, False); (None, True) when two different keys
    are equally close; (None, False) when none is close enough."""
    scored = [(similarity(title, text), key) for key, text in candidates]
    good = [(score, key) for score, key in scored if score >= THRESHOLD]
    if not good:
        return None, False
    top = max(score for score, _ in good)
    keys = {key for score, key in good if top - score < 1e-9}
    return (keys.pop(), False) if len(keys) == 1 else (None, True)


def item_for_sku(db: Database, sku: str | None) -> str | None:
    if not sku:
        return None
    row = db.one("SELECT id FROM items WHERE id = ?", (sku,)) or \
        db.one("SELECT item_id AS id FROM listings WHERE item_id = ? OR sku = ? ORDER BY item_id LIMIT 1", (sku, sku))
    return row["id"] if row else None


def item_for_listing(db: Database, marketplace: str, listing_id: str | None = None,
                     listing_url: str | None = None) -> str | None:
    """The item whose listing on `marketplace` has this id (the stored id, or the id in the stored address)."""
    wanted = {str(x).lower() for x in (listing_id, listing_id_from_url(marketplace, listing_url)) if x}
    if not wanted:
        return None
    found = set()
    for row in db.query("SELECT item_id, listing_id, url FROM listings WHERE marketplace = ?", (marketplace,)):
        ids = {str(x).lower() for x in (row["listing_id"], listing_id_from_url(marketplace, row["url"])) if x}
        if ids & wanted:
            found.add(row["item_id"])
    return found.pop() if len(found) == 1 else None


def item_for_title(db: Database, marketplace: str, title: str | None) -> str | None:
    if not norm_title(title):
        return None
    for statuses in (LIVE, WAS_LIVE):
        marks = ", ".join("?" * len(statuses))
        rows = db.query("SELECT l.item_id, i.title FROM listings l JOIN items i ON i.id = l.item_id "
                        f"WHERE l.marketplace = ? AND l.status IN ({marks})", (marketplace, *statuses))
        item, ambiguous = best(title, [(row["item_id"], row["title"]) for row in rows])
        if item or ambiguous:
            return item
    return None


WORDS_SHARE = 0.6           # WO53 §6: the share of the sale's title words an item's title must hold (with its price)


def _words(text: object) -> set[str]:
    return {w for w in norm_title(text).split() if len(w) > 2 or w.isdigit()}


def item_for_words_and_price(db: Database, marketplace: str, title: str | None, price) -> str | None:
    """WO53 §6: our records checked by the agent itself, never the owner — the one item listed (or once listed) on
    `marketplace` whose title holds most of the sale's title words AND whose price is the sale's (whole dollars); two
    such → none (never a guess)."""
    want = _words(title)
    try:
        sold = round(float(price)) if price not in (None, "") else None
    except (TypeError, ValueError):
        sold = None
    if not want or sold is None:
        return None
    found = set()
    for row in db.query("SELECT l.item_id, l.price AS lprice, i.title, i.price AS iprice FROM listings l JOIN items i "
                        "ON i.id = l.item_id WHERE l.marketplace = ? AND l.status IN ('posted', 'delisting', 'delisted', "
                        "'sold')", (marketplace,)):
        theirs = _words(row["title"])
        prices = {round(float(p)) for p in (row["lprice"], row["iprice"]) if p not in (None, "")}
        if theirs and len(want & theirs) / len(want) >= WORDS_SHARE and sold in prices:
            found.add(row["item_id"])
    return found.pop() if len(found) == 1 else None


def match_sale(db: Database, marketplace: str, parsed: dict) -> tuple[str | None, str | None]:
    """(item id, how: "sku" | "listing" | "title" | "words and price") or (None, None) — see the module."""
    item = item_for_sku(db, parsed.get("sku"))
    if item:
        return item, "sku"
    item = item_for_listing(db, marketplace, parsed.get("listing_id"), parsed.get("listing_url"))
    if item:
        return item, "listing"
    item = item_for_title(db, marketplace, parsed.get("title"))
    if item:
        return item, "title"
    item = item_for_words_and_price(db, marketplace, parsed.get("title"), parsed.get("price"))
    return (item, "words and price") if item else (None, None)
