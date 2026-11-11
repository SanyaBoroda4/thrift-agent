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
    s = _SPELL.get(s.upper(), s)                              # "XXXXL" is 4XL
    if m := _LETTER.fullmatch(s.replace(" ", "")):            # first: "2XL" is a letter size, not a 2
        letters = _SPELL.get(m.group(1).upper(), m.group(1).upper())
        return f"letters {(m.group(2) or '').lower()}".strip(), float(_LETTERS.index(letters))
    if m := re.search(r"\d+(?:\.\d+)?", s):
        return (s[:m.start()] + "#" + s[m.end():]).lower(), float(m.group())
    return None


MAX_STEP = 1.0           # a guess is one size away at most: 8.5 -> 9, XL -> L, Waist 33 -> 34; never 15 -> 12


def nearest_size(want: str, offered: list[str], step: float | None = MAX_STEP) -> str | None:
    """The offered size nearest to `want` in the same system ("8" or "9" for "8.5": the larger on a tie; "L" for "XL"
    when the menu stops at L), at most `step` sizes away (None: however far — WO58); None when no offered size is that
    close in its system — never a size of another system (a women's 8 is not a kids' 8 (Toddler Girl))."""
    mine = _system(want)
    if mine is None:
        return None
    same = [(abs(n - mine[1]), -n, o) for o in offered if (sys := _system(o)) and sys[0] == mine[0] for n in [sys[1]]]
    best = min(same) if same else None
    return best[2] if best and (step is None or best[0] <= step) else None


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
    if (bare := kids_bare(want)) is not None and _key(bare) in keyed:
        return keyed[_key(bare)], f"{want} as {bare}"     # WO55 (live 2026-11-09, the Birkenstock Milano: skipped)
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
    # WO58 (the owner, 2026-11-10: "absolutely nothing should be skipped"): past the close choices, still a value of
    # the menu — the nearest of the same system however far (the bigger between two), the same fit in the menu's
    # other system (a letter size on a numbers menu), the menu's neutral choice; a menu that has none of those is
    # left to the caller (another category whose menu holds the size — rule 2.5 — or its own last choice)
    for size in [x for x in (bigger_of(want), want) if x]:
        if (far := nearest_size(size, offered, step=None)) is not None:
            return far, "nearest on the menu"
    if want and (fit := same_fit(want, offered)) is not None:
        return fit, "the same fit"
    for o in offered:
        if NEUTRAL.fullmatch(o or ""):
            return o, "the menu's neutral choice"
    return None


NEUTRAL = re.compile(r"\s*(?:one\s*size(?:\s*fits\s*(?:all|most))?|os|o/s|osfa|other)\s*", re.I)
# how close a choice is to the size as written — the poster that can try several categories (eBay) keeps the closest
CLOSENESS = {"exact": 0, "as": 0, "the bigger of": 1, "near the bigger of": 2, "nearest": 2, "one size": 2,
             "nearest on the menu": 3, "the same fit": 3, "the menu's neutral choice": 4}


def closeness(how: str | None) -> int:
    """0 for the size as written … 4 for the menu's neutral choice; 5 for anything else (the caller's last choice)."""
    how = str(how or "")
    if how == "exact":
        return 0
    if " as " in how:
        return 0
    for name in sorted(CLOSENESS, key=len, reverse=True):
        if how.startswith(name):
            return CLOSENESS[name]
    return 5


# the same fit in another system: US women's numbers and letters (00-0 XXS, 2 XS, 4-6 S, 8-10 M, 12-14 L, 16-18 XL,
# 20-22 XXL, 24-26 XXXL — the sizing the catalogs' Vinted mapping uses)
_FIT_OF_NUMBER = {0: "XXS", 1: "XXS", 2: "XS", 4: "S", 6: "S", 8: "M", 10: "M", 12: "L", 14: "L", 16: "XL", 18: "XL",
                  20: "XXL", 22: "XXL", 24: "XXXL", 26: "XXXL"}


def _fit(size: str) -> float | None:
    """A size's place on the letters' run (XXS 1 … XXXL 8), from a letter size or a plain US women's number."""
    s = (size or "").strip().upper()
    s = _SPELL.get(s, s)
    if s in _LETTERS:
        return float(_LETTERS.index(s))
    if s == "00":
        return float(_LETTERS.index("XXS"))
    if re.fullmatch(r"\d{1,2}", s) and int(s) in _FIT_OF_NUMBER:
        return float(_LETTERS.index(_FIT_OF_NUMBER[int(s)]))
    return None


def same_fit(want: str, offered: list[str]) -> str | None:
    """The offered size of the other system nearest in fit ("M" → "8" or "10" on a numbers menu: the bigger; "10" → "M"
    on a letters menu), else None (shoes, waists, kids' sizes keep to their own system)."""
    mine = _fit(want)
    if mine is None:
        return None
    others = [(abs(f - mine), -f, o) for o in offered if (f := _fit(o)) is not None]
    best = min(others) if others else None
    return best[2] if best and best[0] <= MAX_STEP else None      # one size away at most: a 4XL is never a 4


def last_choice(offered: list[str]) -> str | None:
    """WO58: the size a menu still gives when nothing of it is near the item's (and it has no neutral choice): the
    middle of the menu — a real value of the site's, said in the item's check line; None for an empty menu."""
    values = [o for o in offered if o and o.strip()]
    return values[len(values) // 2] if values else None


KIDS_SHOE = re.compile(r"\s*(\d{1,2}(?:\.5)?)\s*(?:(c)|(y)|\(?\s*(?:kids?|children'?s?|toddler)\s*\)?)\s*", re.I)


def kids_bare(want: str | None) -> str | None:
    """WO55 (live 2026-11-09 2:28 PM: the Birkenstock Milano's "11.5 (kids)" was skipped on Poshmark's Kids › Shoes
    Girls tab although its menu offers "11.5" — the qualifier stopped the exact match): a kids' shoe size with its
    system mark — "11.5 (kids)", "11.5C", "4Y" — as the bare number Poshmark's Girls / Boys tabs show, only where it
    can't be another size: C (toddler / little kid) and a plain "kids" from 7.5 to 13.5 (those tabs' 1–7 are the
    youth sizes), Y (youth) from 1 to 7. Else None."""
    m = KIDS_SHOE.fullmatch(str(want or ""))
    if m is None:
        return None
    n = float(m.group(1))
    if m.group(3):                                     # Y: the youth / big kid run
        return m.group(1) if 1 <= n <= 7 else None
    return m.group(1) if 7.5 <= n <= 13.5 else None   # C or "kids": the toddler / little kid run


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


def closest_option(want: str, offered: list[str], words: str = "") -> tuple[str, str] | None:
    """WO58 (the owner: nothing is ever skipped — the category is the agent's decision): the offered option for a
    category or department the form lacks — `matching` first; else the option sharing the most words with the item's
    own (`words`: its type, subcategory, title — "Rompers" for a romper, singular or plural); else the form's
    "Other"; else its first option. (the option, how) — None only for an empty list."""
    values = [o for o in offered if o and o.strip()]
    if not values:
        return None
    if (hit := matching(want, values)) is not None:
        return hit, "matching"

    def stems(text: str) -> set[str]:
        return {singular(w) for w in re.findall(r"[a-z]+", str(text).lower())
                if len(w) > 2 and w not in ("and", "the", "for", "with", "size")}
    mine = stems(f"{want} {words}")
    scored = sorted(((len(stems(o) & mine), -n, o) for n, o in enumerate(values)), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2], "the item's own words"
    other = next((o for o in values if _key(o) == "other"), None)
    return (other, "the form's Other") if other else (values[0], "the first the form offers")


def singular(word: str) -> str:
    """A word's singular, roughly — enough to match "Dresses" with "dress", "Rompers" with "romper", "Accessories"
    with "accessory"; "dress", "shorts" (one garment) and short words as they are."""
    w = word.lower()
    if len(w) <= 3 or w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "shes", "ches", "xes", "zes")):
        return w[:-2]
    return w[:-1] if w.endswith("s") else w
