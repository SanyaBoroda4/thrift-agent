"""The poster's best guesses when a form menu doesn't offer exactly what the listing says (WO27: the poster never stops
to ask; every guess is reported in the "Posted ✓" message). Pure functions: the adapter reads what the menu offers and
asks here which to pick."""
from __future__ import annotations

import difflib
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


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", "&", str(text).lower()))


def closest(want: str, offered: list[str], cutoff: float) -> str | None:
    """The offered name closest to `want` (case, spacing and punctuation ignored; "and" = "&"): a name containing it
    or contained in it first ("Jumpsuits" -> "Jumpsuits & Rompers"), else the most similar at `cutoff` or above."""
    keyed = {_key(o): o for o in offered if o and _key(o)}
    k = _key(want)
    if not k:
        return None
    if k in keyed:
        return keyed[k]
    if inside := sorted((o for ko, o in keyed.items() if k in ko or ko in k), key=lambda o: abs(len(_key(o)) - len(k))):
        return inside[0]
    near = difflib.get_close_matches(k, list(keyed), n=1, cutoff=cutoff)
    return keyed[near[0]] if near else None
