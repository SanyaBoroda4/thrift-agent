"""Size labels for the copy and the marketplace form.

A kids shoe size carries its system: "EU 24 / US Toddler 7.5". A bare "7.5" on a kids sneaker reads as a women's 7.5
and comes back as "not as described". Kids clothing sizes (4T, 6X, 10-12) are not grouped and stay as they are.

The groups are Poshmark's (its create-listing form, verified 2026-09-29), so the title, the description and the form
agree: Toddler up to 12C, Little Kid 12.5-13.5C and 1-3Y, Big Kid 3.5-7Y. The form files 0-7C on a Baby tab of its own;
the title and the description still say Toddler there, which is what buyers search."""
from __future__ import annotations

import re

from thrift_agent.schema import Facts

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


def _letter(n: float, letter: str | None, eu: float | None) -> str:
    """C (baby to little kid) or Y (youth): the label's letter when it has one; else a number above 7 can only be a C
    size (Y sizes stop at 7); else the EU size decides (a C size up to 7 is EU 24 at most, 1Y is about EU 32); else C."""
    if letter:
        return letter
    if n > 7:
        return "C"
    if eu is not None:
        return "C" if eu <= 28 else "Y"
    return "C"


def _segment(n: float, letter: str | None, eu: float | None) -> str:
    """Poshmark's group: Toddler up to 12C, Little Kid 12.5-13.5C and 1-3Y, Big Kid 3.5Y and up."""
    if _letter(n, letter, eu) == "C":
        return "Toddler" if n <= 12 else "Little Kid"
    return "Little Kid" if n <= 3 else "Big Kid"


def kids_parts(facts: Facts) -> tuple[str | None, str, str] | None:
    """(eu, segment, n) for a kids shoe with a parseable US size, else None."""
    us = facts.size_us.value
    if us is None or not kids_shoe(facts):
        return None
    m = _US.match(us.strip())
    if not m:
        return None
    n, letter = _trim(m.group(1)), (m.group(2) or "").upper() or None
    if letter is None and (on_label := _LETTER.search(facts.size_printed.value or "")):
        letter = on_label.group(1).upper()
    eu = _eu(facts)
    return eu, _segment(float(n), letter, float(eu) if eu else None), n


def size_label(facts: Facts) -> str | None:
    """The full size for the description and the listing form.

    Anything but a kids shoe: size_us as it is. A kids shoe: "EU <eu> / US <segment> <n>" — the EU part only when the
    label or size_eu gives it; the segment is Poshmark's group for the number and its C/Y letter (the letter from
    size_us or the printed label, else worked out from the number or the EU size, see _letter). A kids value that is
    not a shoe size ("M") is returned as it is."""
    parts = kids_parts(facts)
    if parts is None:
        return facts.size_us.value
    eu, segment, n = parts
    label = f"US {segment} {n}"
    return f"EU {eu} / {label}" if eu else label


def title_size(facts: Facts) -> str | None:
    """The size phrase for the TITLE: the US size only, never EU. Adults "size 7.5"; kids shoes in Poshmark's groups,
    "Toddler size 7.5" (up to 12C, 0-7C included) / "Little Kid size 13" (12.5-13.5C, 1-3Y) / "Big Kid size 4" (3.5Y
    and up). The EU size belongs in the description (size_label)."""
    parts = kids_parts(facts)
    if parts is None:
        return f"size {facts.size_us.value}" if facts.size_us.value else None
    _, segment, n = parts
    return f"{segment} size {n}"
