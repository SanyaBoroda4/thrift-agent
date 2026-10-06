"""The item's size on Depop's and Vinted's own size menus (WO30), from the size the owner approved for Poshmark (the
Poshmark menu value and tab, else the label's US size). Exactly a value of the menu, or MappingError — never an
invented size. A conversion between two sizes the menu has ("7.5C" → Vinted's "8 child": no 7.5 there) is a guess and
says so; "One size" / "Other" only when the item itself is one size."""
from __future__ import annotations

import re
from dataclasses import dataclass

from thrift_agent.catalogs import Option


class MappingError(ValueError):
    """A required field the catalog can't take from the facts: that marketplace is skipped for this item."""


@dataclass
class SizeIn:
    """What the item says about its size."""
    department: str
    category: str
    subcategory: str | None
    item_type: str
    value: str | None              # Poshmark's menu value ("Waist 32", "7.5 (Toddler Girl)", "M"), else the US size
    tab: str | None                # Poshmark's size tab ("Plus", "Petite", "Girls", "Baby"…)
    printed: str | None            # the label as printed ("W26", "4 / 104cm")
    kids_c_or_y: str | None = None  # kids shoes: "C" or "Y"

    @property
    def one_size(self) -> bool:
        return bool(self.value) and re.fullmatch(r"\s*(?:one[\s-]?size|os|o/s)\s*", self.value, re.I) is not None


# ---------------------------------------------------------------- parsing

_NUM = re.compile(r"^\d{1,2}(?:\.5)?$")
_LETTERS = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL", "7XL", "8XL"]
_LETTER_ALIASES = {"2XL": "XXL", "XXXL": "3XL", "XXXXL": "4XL", "SMALL": "S", "MEDIUM": "M", "LARGE": "L",
                   "X-SMALL": "XS", "X-LARGE": "XL", "XX-LARGE": "XXL", "EXTRA SMALL": "XS", "EXTRA LARGE": "XL"}
_PLUS_X = re.compile(r"^([0-6])X$", re.I)          # women's plus 0X..6X (kids "6X" is a kids size: never read here)


def letter(v: str) -> str | None:
    """"M", "Medium", "2XL", "XXXL", "M Petite", "PM" → the letter size ("XXL", "3XL", "M"), else None."""
    t = re.sub(r"\b(petite|tall|plus|regular|reg|jrs?|juniors?)\b|\(.*?\)", " ", v, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip().upper()
    t = re.sub(r"^P(XXS|XS|S|M|L|XL)$|^(XXS|XS|S|M|L|XL)P$", lambda m: m.group(1) or m.group(2), t)
    t = _LETTER_ALIASES.get(t, t)
    return t if t in _LETTERS else None


def number(v: str) -> str | None:
    """"8", "8P", "14W", "14 (Plus)", "8.5", "US 8.5" → "8", "14", "8.5"; else None."""
    t = re.sub(r"\(.*?\)", " ", v)
    t = re.sub(r"\b(us|size)\b", " ", t, flags=re.I).strip()
    m = re.fullmatch(r"(\d{1,2}(?:\.5)?)\s*(?:P|W|M|R|S|L)?", t, re.I)
    return m.group(1) if m else ("00" if t == "00" else None)


def waist(s: SizeIn) -> int | None:
    """A waist in inches: Poshmark's men's "Waist 32", a label's "W26" / "32x30" / "32W 30L", or a women's denim bottom
    sized 23-34 (jeans, jean shorts: their sizes ARE waists)."""
    for text in (s.value or "", s.printed or ""):
        if m := re.search(r"\bwaist\s*(\d{2})\b|\bW\s?(\d{2})\b|\b(\d{2})\s*[x×/]\s*\d{2}\b|\b(\d{2})\s*W\b", text, re.I):
            return int(next(g for g in m.groups() if g))
    if s.department == "Women" and denim_bottom(s) and s.value and re.fullmatch(r"\d{2}", s.value.strip()):
        n = int(s.value)
        return n if 23 <= n <= 36 else None
    return None


def denim_bottom(s: SizeIn) -> bool:
    words = f"{s.category} {s.subcategory or ''} {s.item_type}".lower()
    return s.category == "Jeans" or "jean" in words or "denim" in words


def neck(s: SizeIn) -> str | None:
    for text in (s.value or "", s.printed or ""):
        if m := re.search(r"\bneck\s*(\d{2}(?:\.5)?)\b", text, re.I):
            return m.group(1)
    return None


# Women's denim waist → US numeric (WO30; 33 and 34 continue the same steps).
WAIST_TO_US = {23: "00", 24: "00", 25: "0", 26: "2", 27: "4", 28: "6", 29: "8", 30: "10", 31: "12", 32: "14",
               33: "16", 34: "18"}


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


# ---------------------------------------------------------------- Vinted

# Women's numeric → size_0 (WO30): 00→XXXS, 0→XXS, 2→XS, 4/6→S, 8/10→M, 12/14→L, 16/18→XL, 20/22→XXL, then the plus
# steps of the menu (24/26 XXXL … 48 9XL).
V_WOMEN_NUM = {"00": 1226, "0": 102, "2": 2, "4": 3, "6": 3, "8": 4, "10": 4, "12": 5, "14": 5, "16": 6, "18": 6,
               "20": 7, "22": 7, "24": 310, "26": 310, "28": 311, "30": 311, "32": 312, "34": 312, "36": 1227,
               "38": 1227, "40": 1228, "42": 1228, "44": 1229, "46": 1229, "48": 1230}
V_WOMEN_LETTER = {"XXXS": 1226, "XXS": 102, "XS": 2, "S": 3, "M": 4, "L": 5, "XL": 6, "XXL": 7, "3XL": 310, "4XL": 311,
                  "5XL": 312, "6XL": 1227, "7XL": 1228, "8XL": 1229}
V_PLUS_X = {"0": 5, "1": 6, "2": 7, "3": 310, "4": 311, "5": 312, "6": 1227}       # 0X..6X
V_MEN_LETTER = {"XS": 206, "S": 207, "M": 208, "L": 209, "XL": 210, "XXL": 211, "3XL": 212, "4XL": 308, "5XL": 309,
                "6XL": 1192, "7XL": 1193, "8XL": 1194}
# Kids clothing → size_16: Poshmark's kids menus (Baby, Girls, Boys) onto Vinted's baby & kids sizes.
V_KIDS = {"PREEMIE": 610, "NEWBORN": 666, "0-3 MONTHS": 613, "3 MONTHS": 613, "3-6 MONTHS": 614, "6 MONTHS": 614,
          "6-9 MONTHS": 616, "9 MONTHS": 616, "9-12 MONTHS": 617, "12 MONTHS": 617, "12-18 MONTHS": 618,
          "18 MONTHS": 618, "18-24 MONTHS": 619, "24 MONTHS": 619, "2T": 619, "3T": 1567, "4T": 623, "4": 623,
          "5T": 624, "5": 624, "6": 625, "6X": 626, "7": 626, "8": 627, "9": 628, "10": 629, "11": 630, "12": 631,
          "14": 632, "16": 633, "18": 634, "20": 635, "XS": 1568, "S": 1569, "M": 1570, "L": 1571, "XL": 1572,
          "XXL": 1723}
# Kids shoes → size_17 ("0 baby" … "13 child", "1 junior" … "7.5 junior"): a half size the menu lacks goes up one.
V_KIDS_SHOE_C = {0: 657, 1: 585, 2: 586, 3: 587, 4: 588, 5: 589, 5.5: 590, 6: 591, 7: 592, 8: 593, 8.5: 594, 9: 595,
                 10: 596, 11: 597, 12: 598, 12.5: 599, 13: 600}
V_KIDS_SHOE_Y = {1: 601, 2: 602, 3: 603, 4: 604, 5: 605, 6: 606, 6.5: 607, 7: 608, 7.5: 609}


def _by_title(options: list[Option], title: str) -> Option | None:
    want = title.strip().lower()
    return next((o for o in options if o.title.strip().lower() == want), None)


def _opt(options: list[Option], oid: int) -> Option | None:
    return next((o for o in options if o.id == oid), None)


def _up(table: dict[float, int], n: float) -> tuple[float, int] | None:
    """The size itself, else the next one up the menu has (a kids shoe: a half size up fits)."""
    if n in table:
        return n, table[n]
    bigger = [k for k in table if k > n]
    return (min(bigger), table[min(bigger)]) if bigger and min(bigger) - n <= 1 else None


def vinted_size(s: SizeIn, lib: str, options: list[Option]) -> tuple[Option, str | None]:
    """(the option, a guess to report or None) for the leaf's size library. MappingError when nothing fits."""
    if not s.value:
        raise MappingError("no size on the item")
    guess = None

    def found(oid: int | None, note: str | None = None) -> tuple[Option, str | None]:
        o = _opt(options, oid) if oid is not None else None
        if o is None:
            raise MappingError(f"size {s.value!r} has no place on Vinted's {lib} menu")
        return o, note

    if s.one_size:
        o = next((o for o in options if o.title.strip().lower() == "one size"), None)
        if o is None:
            raise MappingError(f"one size, but Vinted's {lib} menu has no 'One size'")
        return o, None
    if lib == "size_0":                                  # women's clothing
        if (w := waist(s)) is not None and s.department == "Women":
            us = WAIST_TO_US.get(w)
            if us is None:
                raise MappingError(f"waist {w} is off the women's size steps")
            return found(V_WOMEN_NUM.get(us), None)
        if (m := _PLUS_X.match(s.value.strip())) and s.tab == "Plus":
            return found(V_PLUS_X[m.group(1)])
        if (ltr := letter(s.value)) is not None:
            return found(V_WOMEN_LETTER.get(ltr))
        if (n := number(s.value)) is not None:
            if n.isdigit() and int(n) % 2 == 1 and n != "00":        # juniors 1, 3, 5…: one step up
                guess = f"size {n} (juniors) listed as US {int(n) + 1}"
                n = str(int(n) + 1)
            return found(V_WOMEN_NUM.get(n), guess)
        raise MappingError(f"size {s.value!r} has no place on Vinted's women's sizes")
    if lib in ("size_9", "size_10", "size_11", "size_12"):          # men's
        if lib == "size_9" and (w := waist(s)) is not None:
            o = _by_title(options, f"W{w}")
            if o is None:
                up = next((x for x in (w + 1, w + 2) if _by_title(options, f"W{x}")), None)
                if up is None:
                    raise MappingError(f"waist {w} isn't on Vinted's men's pants sizes")
                return _by_title(options, f"W{up}"), f"size set to 'W{up}' (from waist {w})"
            return o, None
        if lib == "size_11" and (nk := neck(s)) is not None:
            return found(next((o.id for o in options if o.title == f"{nk} in"), None))
        if lib == "size_12" and (m := re.fullmatch(r"\s*(\d{2})\s*([SRL])\s*", s.value, re.I)):
            return found(next((o.id for o in options if o.title.upper() == f"{m.group(1)}{m.group(2).upper()}"), None))
        if (ltr := letter(s.value)) is not None:
            return found(V_MEN_LETTER.get(ltr))
        raise MappingError(f"size {s.value!r} has no place on Vinted's men's {lib} menu")
    if lib == "size_16":                                 # baby & kids clothing
        key = re.sub(r"\s*\((?:girls?|boys?|baby)\)\s*", "", s.value, flags=re.I).strip().upper()
        return found(V_KIDS.get(key))
    if lib == "size_17":                                 # kids shoes
        n = _shoe_number(s.value)
        if n is None:
            raise MappingError(f"kids shoe size {s.value!r} unreadable")
        table = V_KIDS_SHOE_Y if (s.kids_c_or_y or "C") == "Y" else V_KIDS_SHOE_C
        hit = _up(table, n)
        if hit is None:
            raise MappingError(f"kids shoe size {s.value!r} isn't on Vinted's kids shoe sizes")
        o = _opt(options, hit[1])
        if o is None:
            raise MappingError(f"kids shoe size {s.value!r} isn't on Vinted's kids shoe sizes")
        return o, (None if hit[0] == n else f"size set to '{o.title}' (from {_fmt(n)}{s.kids_c_or_y or 'C'})")
    if lib in ("size_5", "size_14"):                     # women's / men's shoes: the US number itself
        n = _shoe_number(s.value)
        if n is None:
            raise MappingError(f"shoe size {s.value!r} unreadable")
        return found(next((o.id for o in options if o.title == _fmt(n)), None))
    o = _by_title(options, s.value) or next((x for x in options if x.title.split(" | ")[0].split(" / ")[0].strip().lower()
                                             == s.value.strip().lower()), None)
    if o is None:
        raise MappingError(f"size {s.value!r} isn't on Vinted's {lib} menu")
    return o, None


def _shoe_number(v: str) -> float | None:
    m = re.search(r"(\d{1,2}(?:\.5)?)", v)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- Depop

D_KIDS = {"PREEMIE": "0-3 months", "NEWBORN": "0-3 months", "0-3 MONTHS": "0-3 months", "3 MONTHS": "0-3 months",
          "3-6 MONTHS": "3-6 months", "6 MONTHS": "3-6 months", "6-9 MONTHS": "6-9 months", "9 MONTHS": "6-9 months",
          "9-12 MONTHS": "9-12 months", "12 MONTHS": "9-12 months", "12-18 MONTHS": "12-18 months",
          "18 MONTHS": "12-18 months", "18-24 MONTHS": "18-24 months", "24 MONTHS": "18-24 months",
          "2T": "2 years", "3T": "3 years", "4T": "4 years", "4": "4 years", "5T": "5 years", "5": "5 years",
          "6": "6 years", "6X": "6 years", "7": "7 years", "8": "8 years", "9": "9 years", "10": "10 years",
          "11": "11 years", "12": "12 years", "13": "13 years", "14": "14 years", "15": "15 years", "16": "16 years"}
D_KIDS_SHOE_C = {0: "0-0.5 (newborn)", 0.5: "0-0.5 (newborn)", 1: "1-1.5 (baby)", 1.5: "1-1.5 (baby)", 2: "2-2.5",
                 2.5: "2-2.5", 3: "3-3.5", 3.5: "3-3.5", 4: "4-4.5", 4.5: "4-4.5", 5: "5-5.5", 5.5: "5-5.5",
                 6: "6-6.5", 6.5: "6-6.5", 7: "7-7.5", 7.5: "7-7.5", 8: "8", 8.5: "8.5", 9: "9-9.5", 9.5: "9-9.5",
                 10: "10-10.5", 10.5: "10-10.5", 11: "11", 11.5: "11.5", 12: "12-12.5", 12.5: "12-12.5",
                 13: "13-13.5", 13.5: "13-13.5"}
D_KIDS_SHOE_Y = {1: "1-1.5 (adult)", 1.5: "1-1.5 (adult)", 2: "2-2.5 (adult)", 2.5: "2-2.5 (adult)", 3: "3 (adult)",
                 3.5: "3.5 (adult)", 4: "4 (adult)", 5: "5 (adult)", 6: "6 (adult)", 7: "7 (adult)", 7.5: "7.5 (adult)"}
D_PLUS_X = {"0": "L", "1": "XL", "2": "XXL", "3": "3XL", "4": "4XL", "5": "5XL", "6": "6XL"}
NECK_TO_LETTER = ((14.5, "S"), (15.5, "M"), (16.5, "L"), (17.5, "XL"), (18.5, "XXL"), (19.5, "3XL"))


def depop_size(s: SizeIn, sizes: list[str], set_path: str) -> tuple[str, str | None]:
    """(the size set's value, a guess or None). `sizes` is the set ("Women > Bottoms > US")."""
    if not sizes:
        raise MappingError("no size field")
    if not s.value:
        raise MappingError("no size on the item")
    have = {v.lower(): v for v in sizes}

    def pick(v: str | None, note: str | None = None) -> tuple[str, str | None]:
        if v is None or v.lower() not in have:
            raise MappingError(f"size {s.value!r} isn't on Depop's {set_path} sizes")
        return have[v.lower()], note

    if s.one_size:
        return pick("One size")
    if set_path.startswith("Kids > Shoes"):
        n = _shoe_number(s.value)
        if n is None:
            raise MappingError(f"kids shoe size {s.value!r} unreadable")
        table = D_KIDS_SHOE_Y if (s.kids_c_or_y or "C") == "Y" else D_KIDS_SHOE_C
        if n in table:
            return pick(table[n])
        up = min((k for k in table if k > n), default=None)
        if up is None or up - n > 1:
            raise MappingError(f"kids shoe size {s.value!r} isn't on Depop's kids shoe sizes")
        return pick(table[up], f"Depop size set to '{table[up]}' (from {_fmt(n)}{s.kids_c_or_y or 'C'})")
    if set_path.startswith("Kids"):
        key = re.sub(r"\s*\((?:girls?|boys?|baby)\)\s*", "", s.value, flags=re.I).strip().upper()
        return pick(D_KIDS.get(key))
    if "Shoes" in set_path:
        n = _shoe_number(s.value)
        if n is None:
            raise MappingError(f"shoe size {s.value!r} unreadable")
        return pick(f"US {_fmt(n)}")
    if (w := waist(s)) is not None and ("Bottoms" in set_path):
        return pick(f'{w}"')
    if (m := _PLUS_X.match(s.value.strip())) and s.tab == "Plus":
        return pick(D_PLUS_X[m.group(1)])
    if (ltr := letter(s.value)) is not None:
        return pick(ltr)
    if (nk := neck(s)) is not None:
        lt = next((x for top, x in NECK_TO_LETTER if float(nk) <= top), None)
        return pick(lt, f"Depop size set to '{lt}' (from neck {nk})")
    if (n := number(s.value)) is not None:
        if n == "00" and "00" not in have:
            return pick("0", "Depop size set to '0' (from 00)")
        return pick(n)
    raise MappingError(f"size {s.value!r} isn't on Depop's {set_path} sizes")
