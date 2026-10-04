"""Size labels for the copy and the marketplace form.

A kids shoe size carries its system: "EU 24 / US Toddler 7.5". A bare "7.5" on a kids sneaker reads as a women's 7.5
and comes back as "not as described". Kids clothing sizes (4T, 6X, 10-12) are not grouped and stay as they are.

The groups are Poshmark's (its create-listing form, verified 2026-09-29), so the title, the description and the form
agree: Toddler up to 12C, Little Kid 12.5-13.5C and 1-3Y, Big Kid 3.5-7Y. The form files 0-7C on a Baby tab of its own;
the title and the description still say Toddler there, which is what buyers search."""
from __future__ import annotations

import re

from thrift_agent.brain import taxonomy
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


# ---- kids clothing: the label's height or age -> Poshmark's size (WO23) ----
# European kids labels give the child's height ("104 cm", "Gr. 104") and/or the age ("4 ans", "4A", "5-6 Y"). The
# sizes are the labels of Poshmark's Kids size menus as its public Kids size filter lists them (read 2026-10-04 for
# Shirts & Tops): Baby "Newborn", "0-3 Months", "3 Months" … "24 Months"; Girls and Boys "2T"-"5T", then "6", "7", "8",
# "10", "12", "14", "16" (no 9, 11, 13: those go up to the next size, so it fits). Every label is on the Baby / Girls /
# Boys menus of the form's own catalog (WO25, data/poshmark_catalog.json).
KIDS_BY_CM = ((50, "Newborn"), (56, "0-3 Months"), (62, "3 Months"), (68, "6 Months"), (74, "9 Months"),
              (80, "12 Months"), (86, "18 Months"), (92, "2T"), (98, "3T"), (104, "4T"), (110, "5T"), (116, "6"),
              (122, "7"), (128, "8"), (134, "10"), (140, "10"), (146, "12"), (152, "12"), (158, "14"), (164, "14"),
              (170, "16"), (176, "16"))
KIDS_BY_YEARS = {1: "12 Months", 2: "2T", 3: "3T", 4: "4T", 5: "5T", 6: "6", 7: "7", 8: "8", 9: "10", 10: "10",
                 11: "12", 12: "12", 13: "14", 14: "14", 15: "16", 16: "16"}
KIDS_BY_MONTHS = ((3, "3 Months"), (6, "6 Months"), (9, "9 Months"), (12, "12 Months"), (18, "18 Months"),
                  (24, "24 Months"))
_KCM = re.compile(r"(?<![\d.])(\d{2,3})\s*cm\b", re.I)
_KGR = re.compile(r"\b(?:gr\.?|größe|taille|talla)\s*(\d{2,3})\b", re.I)          # "Gr. 104"
_KBARE = re.compile(r"^\s*(\d{2,3})\s*$")                                          # "104" alone: a height
_KYEARS = re.compile(r"(?<![\d.])(\d{1,2})(?:\s*[-/]\s*(\d{1,2}))?\s*(?:ans?|years?|yrs?|jahre|años|anni|y|a|j)\b",
                     re.I)
_KMONTHS = re.compile(r"(?<![\d.])(\d{1,2})(?:\s*[-/]\s*(\d{1,2}))?\s*(?:mois|months?|mos?|m|monate|meses)\b", re.I)


def _by_cm(cm: int) -> str | None:
    if not KIDS_BY_CM[0][0] - 3 <= cm <= KIDS_BY_CM[-1][0] + 3:
        return None
    return min(KIDS_BY_CM, key=lambda row: (abs(row[0] - cm), -row[0]))[1]      # nearest step; a tie goes up


def kids_clothing_size(printed: str | None) -> str | None:
    """Poshmark's kids clothing size from a label that gives the height (cm) or the age (years, months); None when it
    gives neither — a bare "4" stays a question (4T or kids 4?). The height wins over the age when both are printed.
    An age range ("5-6 Y") takes its upper end."""
    text = (printed or "").strip()
    if not text:
        return None
    if m := (_KCM.search(text) or _KGR.search(text) or _KBARE.match(text)):
        if (size := _by_cm(int(m.group(1)))) is not None:
            return size
    if m := _KYEARS.search(text):
        return KIDS_BY_YEARS.get(int(m.group(2) or m.group(1)))
    if m := _KMONTHS.search(text):
        months = int(m.group(2) or m.group(1))
        return next((label for limit, label in KIDS_BY_MONTHS if months <= limit), None)
    return None


# ---- Poshmark's size menus (WO25): the size exactly as the form offers it ----
# The form's catalog (data/poshmark_catalog.json) gives every category's size menu per tab: Women Standard / Plus /
# Petite / Juniors / Maternity, Men Standard / Big & Tall, Kids Baby / Girls / Boys, shoes Standard (Kids shoes Baby /
# Girls / Boys). The size the poster selects is one value of one tab's menu, spelled as the menu spells it.
_ADULT_TABS = ("Standard", "Plus", "Petite", "Juniors", "Big & Tall")    # tried in this order; Maternity only when asked
_PETITE = re.compile(r"\bpetite\b|^\s*(?:\d{1,2}|XXS|XS|S|M|L|XL|XXL)\s*P\s*$", re.I)
_JUNIORS = re.compile(r"\bjuniors?\b|\bjrs?\b", re.I)
_MATERNITY = re.compile(r"\bmaternity\b", re.I)
_LETTERS = {"2XL": "XXL", "XXL": "2XL", "3XL": "XXXL", "XXXL": "3XL"}
_BRA = re.compile(r"(\d{2})[A-Z]+ \((\w+)\)")                          # "34E (DD)" is also written "34DD"
# A kids shoe size written as our label (sizes before WO10 too): "US Toddler 7.5", "Little Kid 13", "Big Kid 4Y".
_GROUP_LABEL = re.compile(r"\b(Toddler|Little Kid|Big Kid)\s+(\d{1,2}(?:\.5)?)\s*([CY])?\s*$", re.I)


def _menu_key(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _pick(candidates: list[str], menu: list[str]) -> str | None:
    """The menu entry one of the candidates is (case and spaces ignored), in the candidates' order, else None."""
    keys: dict[str, str] = {}
    for entry in menu:
        keys.setdefault(_menu_key(entry), entry)
        if m := _BRA.fullmatch(entry):
            keys.setdefault(_menu_key(m[1] + m[2]), entry)
    return next((keys[_menu_key(c)] for c in candidates if _menu_key(c) in keys), None)


def _spellings(value: str) -> list[str]:
    """The ways a size is written on Poshmark's menus, the value's own first: "3XL" / "XXXL", "32" / "Waist 32" (also
    from "W32" or "32x30"), "15.5" / "Neck 15.5", "8 1/2" and "8.5M" (a shoe width) -> "8.5"."""
    v = re.sub(r"(\d+)\s+1/2\b", lambda m: f"{m[1]}.5", re.sub(r"\s+", " ", value.strip()))
    up = v.upper().replace(" ", "")
    out = [v, up]
    if up in _LETTERS:
        out.append(_LETTERS[up])
    if m := re.fullmatch(r"W?(\d{2})(?:[X/]L?\d{2})?", up):
        out += [m[1], f"Waist {m[1]}"]
    if re.fullmatch(r"\d{2}(?:\.5)?", up):
        out.append(f"Neck {up}")
    if m := re.fullmatch(r"(\d{1,2}(?:\.5)?)(?:[MBWND]|WIDE|MEDIUM)", up):
        out.append(m[1])
    return list(dict.fromkeys(out))


def _petite(value: str) -> list[str]:
    """ "M Petite" / "M P" / "8" -> "MP" / "8P": the Petite menu's spelling."""
    base = re.sub(r"\s*(?:petite|P)\s*$", "", value.strip(), flags=re.I)
    return [f"{base.upper().replace(' ', '')}P", value]


def _kids_value(value: str) -> str:
    """ "4t" -> "4T", "3-6 m" -> "3-6 Months", "12M" -> "12 Months", "NB" -> "Newborn"; "M" stays a letter size."""
    v = value.strip()
    if m := re.fullmatch(r"(\d{1,2})\s*[-–/]\s*(\d{1,2})\s*(?:m|mo|mos|months?)", v, re.I):
        return f"{m[1]}-{m[2]} Months"
    if m := re.fullmatch(r"(\d{1,2})\s*(?:m|mo|mos|months?)", v, re.I):
        return f"{m[1]} Months"
    if re.fullmatch(r"nb|newborn", v, re.I):
        return "Newborn"
    return "Preemie" if re.fullmatch(r"preemie", v, re.I) else v.upper().replace(" ", "")


def _kids_shoe_choice(facts: Facts, gender_tab: str) -> tuple[str, str] | None:
    """A kids shoe on Poshmark's menus: 0-7C on the Baby tab as the bare number ("5", "6.5"); from 7.5C on the Girls /
    Boys tab as "7.5 (Toddler Girl)" — Toddler 7.5-12C, Little 12.5-13.5C and 1-3Y, Big 3.5-7Y."""
    value = (facts.size_us.value or "").strip()
    if label := _GROUP_LABEL.search(value):              # a size written as our label: "US Toddler 7.5", "Big Kid 4"
        n = _trim(label[2])
        letter = (label[3] or "").upper() or ("C" if label[1].lower() == "toddler" or
                                               (label[1].lower() == "little kid" and float(n) >= 10) else "Y")
        value = f"{n}{letter}"
    m = _US.match(value)
    if not m:
        return None
    n, letter = _trim(m.group(1)), (m.group(2) or "").upper() or None
    if letter is None and (on_label := _LETTER.search(facts.size_printed.value or "")):
        letter = on_label.group(1).upper()
    eu = _eu(facts)
    letter = _letter(float(n), letter, float(eu) if eu else None)
    if letter == "C" and float(n) <= 7:
        return "Baby", n
    group = "Toddler" if letter == "C" and float(n) <= 12 else "Little" if letter == "C" or float(n) <= 3 else "Big"
    return gender_tab, f"{n} ({group} {gender_tab[:-1]})"


def one_size(department: str, category: str, subcategory: str | None = None) -> bool:
    """Every size menu of this category is just "One Size" (bags, jewelry, most accessories, Home): no size to ask."""
    menus = taxonomy.size_menus(department, category, subcategory)
    return bool(menus) and all(sizes == ["One Size"] for sizes in menus.values())


def poshmark_size(facts: Facts) -> tuple[str, str] | None:
    """(tab, size) on Poshmark's size menus for this item, the size spelled exactly as the menu spells it (WO25), or
    None: no menus, no size, or a size on none of the category's menus (then the owner is asked).

    One-size categories: "One Size". Kids: shoes by their C/Y size (see _kids_shoe_choice); clothing on the Girls or
    Boys tab by kids_gender (unisex or unread: Girls), else the Baby tab ("3 Months", "Newborn"). Adults: the tab the
    label asks for first — Maternity (the item type or label says so), Petite ("8P", "M Petite"), Juniors ("Jrs") —
    then Standard, Plus, Petite, Juniors, Big & Tall in that order: "14", "1X" and "14W" are Plus, "XXL" is Plus for
    Women and Big & Tall for Men, a men's "32" is "Waist 32"."""
    menus = taxonomy.size_menus(facts.department, facts.category, facts.subcategory)
    if not menus:
        return None
    gender_tab = "Boys" if facts.kids_gender == "boys" else "Girls"
    if all(sizes == ["One Size"] for sizes in menus.values()):
        return (gender_tab if gender_tab in menus else next(iter(menus))), "One Size"
    value = (facts.size_us.value or "").strip()
    if not value:
        return None
    if facts.department == "Kids":
        if kids_shoe(facts):
            choice = _kids_shoe_choice(facts, gender_tab)
            return (choice[0], hit) if choice and choice[0] in menus and (hit := _pick([choice[1]], menus[choice[0]])) \
                else None
        for tab in (gender_tab, "Baby"):
            if tab in menus and (hit := _pick([_kids_value(value), value], menus[tab])):
                return tab, hit
        return None
    label = f"{facts.size_printed.value or ''} {value}"
    first = ("Maternity" if _MATERNITY.search(f"{facts.item_type} {label}") else "Petite" if _PETITE.search(label)
             else "Juniors" if _JUNIORS.search(label) else None)
    tabs = [t for t in _ADULT_TABS if t in menus]
    if first in menus:
        tabs = [first] + [t for t in tabs if t != first]
    for tab in tabs:
        if hit := _pick(_petite(value) if tab == "Petite" and first == "Petite" else _spellings(value), menus[tab]):
            return tab, hit
    return None
