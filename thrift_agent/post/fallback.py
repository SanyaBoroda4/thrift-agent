"""The poster's best guesses when a form menu doesn't offer exactly what the listing says (WO27: the poster never stops
to ask; every guess is reported in the "Posted ✓" message). Pure functions: the adapter reads what the menu offers and
asks here which to pick."""
from __future__ import annotations

import re

_LETTERS = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "4XL", "5XL", "6XL"]
_SPELL = {"2XL": "XXL", "3XL": "XXXL", "XXXXL": "4XL"}
_LETTER = re.compile(r"(X{0,3}S|M|L|X{1,3}L|[2-6]XL)([PT])?", re.I)


def _system(size: str) -> tuple[str, float] | None:
    """(the size's system, its place in it): a number with its words around it — "Waist 32" is ("waist #", 32), "7.5
    (Toddler Girl)" is ("# (toddler girl)", 7.5), "8P" is ("#p", 8) — or a letter size with its suffix ("MP" is
    ("letters p", 4)). None for anything else ("One Size")."""
    s = re.sub(r"\s+", " ", (size or "").strip())
    if m := _LETTER.fullmatch(s.replace(" ", "")):            # first: "2XL" is a letter size, not a 2
        letters = _SPELL.get(m.group(1).upper(), m.group(1).upper())
        return f"letters {(m.group(2) or '').lower()}".strip(), float(_LETTERS.index(letters))
    if m := re.search(r"\d+(?:\.\d+)?", s):
        return (s[:m.start()] + "#" + s[m.end():]).lower(), float(m.group())
    return None


MAX_STEP = 1.0           # a guess is one size away at most: 8.5 -> 9, XL -> L, Waist 33 -> 34; never 15 -> 12


def nearest_size(want: str, offered: list[str]) -> str | None:
    """The offered size nearest to `want` in the same system ("8" or "9" for "8.5": the larger on a tie; "L" for "XL"
    when the menu stops at L), at most one size away; None when no offered size is that close in its system — never a
    size of another system (a women's 8 is not a kids' 8 (Toddler Girl))."""
    mine = _system(want)
    if mine is None:
        return None
    same = [(abs(n - mine[1]), -n, o) for o in offered if (sys := _system(o)) and sys[0] == mine[0] for n in [sys[1]]]
    best = min(same) if same else None
    return best[2] if best and best[0] <= MAX_STEP else None


LETTERS = ("XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL")
DUAL = re.compile(r"^\s*(?:one\s*size\s*\(\s*)?(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2})\s*(?:/|-|–|—|to)\s*"
                  r"(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2})\s*\)?\s*$", re.I)
ONE_SIZE_ITEMS = re.compile(r"\b(robes?|scarf|scarves|bags?|totes?|clutch|purse|wallets?|hats?|beanies?|caps?|bandanas?|"
                            r"shawls?|wraps?|ponchos?|gloves?|mittens?|sunglasses|keychains?|jewelry|necklaces?|"
                            r"bracelets?|earrings?|headbands?|belts?)\b", re.I)


def dual_parts(size: str | None) -> tuple[str, str] | None:
    """WO50: a label's two sizes — "S/M", "M/L", "XS/S", "0/2", "7/8", "8-10", "One Size (S–L)" — as (smaller, bigger)
    in the label's own spelling; None for one size."""
    m = DUAL.match(str(size or ""))
    if not m:
        return None
    a, b = m[1].upper(), m[2].upper()
    if a.isdigit() != b.isdigit():
        return None
    if a.isdigit():
        return (a, b) if int(a) <= int(b) else (b, a)
    return (a, b) if LETTERS.index(a) <= LETTERS.index(b) else (b, a)


def bigger_of(size: str | None) -> str | None:
    """The bigger size of a dual label ("S/M" → "M", "8-10" → "10"), else None."""
    parts = dual_parts(size)
    return parts[1] if parts else None


def menu_size(want: str, offered: list[str], item_type: str | None = None) -> tuple[str, str] | None:
    """WO50: the menu value for `want`, in the owner's order — (the value, how) or None when no value is reasonable:
    1. the exact value ("exact"); 2. the bigger size of a dual label ("S/M" → "M": "bigger of S/M"); 3. the nearest size
    in the same system, one step at most ("nearest"); 4. One Size — only for an item that really is one-size (robes,
    scarves, bags, hats…: "one size"). Never a size of another system."""
    keyed = {_key(o): o for o in offered if o}
    if want and _key(want) in keyed:
        return keyed[_key(want)], "exact"
    if (big := bigger_of(want)) is not None:
        if _key(big) in keyed:
            return keyed[_key(big)], f"the bigger of {want}"
        if (near := nearest_size(big, offered)) is not None:
            return near, f"near the bigger of {want}"
    if want and (near := nearest_size(want, offered)) is not None:
        return near, "nearest"
    if item_type and ONE_SIZE_ITEMS.search(item_type):
        for o in offered:
            if re.fullmatch(r"\s*(?:one\s*size|os|o/s)\s*", o or "", re.I):
                return o, "one size"
    return None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", "&", str(text).lower()))


def matching(want: str, offered: list[str]) -> str | None:
    """WO36 (the owner: pick only an option whose text matches the wanted value): the offered name that IS `want`
    (case, spacing and punctuation ignored; "and" = "&"); else the ONE name that is `want` followed by more words
    ("Jumpsuits" → "Jumpsuits & Rompers"), else the ONE name `want` is a whole " & " part of ("Rompers" → "Jumpsuits
    & Rompers"). Never a shorter name and never a merely similar one (WO27's difflib guess is gone)."""
    keyed = {_key(o): o for o in offered if o and _key(o)}
    k = _key(want)
    if not k:
        return None
    if k in keyed:
        return keyed[k]

    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+|&", re.sub(r"\band\b", "&", str(text).lower()))
    w = words(want)
    longer = [o for o in keyed.values() if len(words(o)) > len(w) and words(o)[:len(w)] == w]
    if len(longer) == 1:
        return longer[0]
    part = [o for o in keyed.values() if any(_key(p) == k for p in re.split(r"\s*(?:&|\band\b)\s*", str(o), flags=re.I))]
    return part[0] if len(part) == 1 else None
