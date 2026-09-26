"""Size labels for the copy and the marketplace form.

A kids shoe size carries its system: "EU 24 / US Toddler 7.5". A bare "7.5" on a kids sneaker reads as a women's 7.5
and comes back as "not as described". Kids clothing sizes (4T, 6X, 10-12) are not grouped and stay as they are."""
from __future__ import annotations

import re

from thrift_agent.schema import Facts

# Poshmark's kids shoe groups: C sizes up to 10 are Toddler, C sizes above are Little Kid, Y sizes are Big Kid.
SEGMENTS = ("Toddler", "Little Kid", "Big Kid")

_US = re.compile(r"^(\d{1,2}(?:\.\d)?)\s*([CY])?$", re.I)     # "7.5", "12C", "4 y"
_LETTER = re.compile(r"\d\s*([CY])\b", re.I)                   # the C/Y after a number on the label ("15 CM" is not one)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def kids_shoe(facts: Facts) -> bool:
    """Only kids shoes use the Toddler / Little Kid / Big Kid groups."""
    return facts.department == "Kids" and facts.category.strip().lower() == "shoes"


def _trim(n: str) -> str:
    return n[:-2] if n.endswith(".0") else n


def _eu(facts: Facts) -> str | None:
    """The EU size as a number: size_eu when known, else the first number of 16 or more on the printed label (a kids
    US size never reaches 16, an EU size is never under it)."""
    for text, least in ((facts.size_eu.value, 0.0), (facts.size_printed.value, 16.0)):
        for n in _NUMBER.findall(text or ""):
            if float(n) >= least:
                return _trim(n)
    return None


def _segment(n: float, letter: str | None, eu: float | None) -> str:
    if letter == "C":
        return "Toddler" if n <= 10 else "Little Kid"
    if letter == "Y":
        return "Big Kid"
    if eu is not None:
        return "Toddler" if eu <= 27 else "Little Kid" if eu <= 33 else "Big Kid"
    return "Toddler" if n <= 10 else "Little Kid" if n <= 13.5 else "Big Kid"


def size_label(facts: Facts) -> str | None:
    """The size as the copy and the listing form should state it.

    Anything but a kids shoe: size_us as it is. A kids shoe: "EU <eu> / US <segment> <n>" — the EU part only when the
    label or size_eu gives it, the segment from the C/Y letter (on size_us or the printed label), else from the EU size,
    else from the US number alone. A kids value that is not a shoe size ("M") is returned as it is."""
    us = facts.size_us.value
    if us is None or not kids_shoe(facts):
        return us
    m = _US.match(us.strip())
    if not m:
        return us
    n, letter = _trim(m.group(1)), (m.group(2) or "").upper() or None
    if letter is None and (on_label := _LETTER.search(facts.size_printed.value or "")):
        letter = on_label.group(1).upper()
    eu = _eu(facts)
    label = f"US {_segment(float(n), letter, float(eu) if eu else None)} {n}"
    return f"EU {eu} / {label}" if eu else label
