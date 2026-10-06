"""What Depop's and Vinted's mappings share (WO30): the item as the cross-lister sees it (the approved Poshmark listing
plus the facts), and the fixed tables for condition, colour, material and package size."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from thrift_agent.catalogs.sizes import MappingError, SizeIn
from thrift_agent.db import loads
from thrift_agent.schema import Facts, Render

__all__ = ["ItemView", "MappingError", "condition_key", "colors_for", "materials_for", "package_class",
           "photos_for", "brand_tag"]


@dataclass
class ItemView:
    """One item for the cross-lister: the Poshmark listing the owner approved (title, description, photos, size,
    condition, colours, brand) and the facts behind it. The price is the owner's approved price, the same on every
    marketplace (no markup)."""
    iid: str
    facts: Facts
    render: Render
    price: int
    flaw_photos: set[str] = field(default_factory=set)

    @classmethod
    def from_row(cls, it) -> ItemView:
        renders = loads(it["renders"]) or {}
        if "poshmark" not in renders:
            raise MappingError("no Poshmark listing to cross-list from")
        render = Render.model_validate(renders["poshmark"])
        facts = Facts.model_validate(loads(it["facts"]) or {})
        price = int(it["owner_price"] or 0)
        if not price:
            raise MappingError("no owner-approved price")
        photos = sorted((Path(it["dir"]) / "photos").glob("*.jpg"))
        flawed = {str(photos[i]) for f in facts.flaws for i in f.photos if 0 <= i < len(photos)}
        return cls(iid=it["id"], facts=facts, render=render, price=price, flaw_photos=flawed)

    @property
    def department(self) -> str:
        return self.render.department

    @property
    def words(self) -> str:
        """The item's own words: what the table's refinements look at."""
        return f"{self.facts.item_type} {self.render.title}"

    def size_in(self) -> SizeIn:
        from thrift_agent.brain import sizes as posh_sizes
        r, f = self.render, self.facts
        cy = None
        if parts := posh_sizes.kids_parts(f):
            _, segment, n = parts
            cy = "C" if segment == "Toddler" or (segment == "Little Kid" and float(n) >= 8) else "Y"
        return SizeIn(department=r.department, category=r.category, subcategory=r.subcategory, item_type=f.item_type,
                      value=r.size_value or f.size_us.value or r.size, tab=r.size_tab, printed=f.size_printed.value,
                      kids_c_or_y=cy)


# ---------------------------------------------------------------- condition (never Fair)

def condition_key(grade: str) -> str:
    """Our grade → the key of the catalogs' condition_map_from_agent. NWT stays NWT (it needed a hang-tag photo or the
    owner's word, invariant 2); new without tags; like new and excellent are Like New / Excellent; good — and a fair
    reading, which is always listed as Good (WO17) — is Good. Fair is never a key."""
    return {"NWT": "NWT", "NWOT": "New without tags", "like_new": "Like New", "excellent": "Excellent",
            "good": "Good", "fair": "Good"}[grade]


# ---------------------------------------------------------------- colour

# Our palette (schema.Color) → the site's colour name.
DEPOP_PALETTE = {"Red": "Red", "Pink": "Pink", "Orange": "Orange", "Yellow": "Yellow", "Green": "Green", "Blue": "Blue",
                 "Purple": "Purple", "Gold": "Gold", "Silver": "Silver", "Black": "Black", "Gray": "Grey",
                 "White": "White", "Cream": "Cream", "Brown": "Brown", "Tan": "Tan"}
VINTED_PALETTE = {"Red": "Red", "Pink": "Pink", "Orange": "Orange", "Yellow": "Yellow", "Green": "Green", "Blue": "Blue",
                  "Purple": "Purple", "Gold": "Gold", "Silver": "Silver", "Black": "Black", "Gray": "Gray",
                  "White": "White", "Cream": "Cream", "Brown": "Brown", "Tan": "Beige"}
# The colour words of the listing that name a shade the site has: (words, the palette colours it can refine,
# Depop's name, Vinted's name). The first that matches wins.
SHADES = [
    (r"\bnavy\b", {"Blue", "Black"}, "Navy", "Navy"),
    (r"\bburgundy\b|\bmaroon\b|\bwine\b|\boxblood\b|\bbordeaux\b", {"Red", "Purple", "Brown"}, "Burgundy", "Burgundy"),
    (r"\bolive\b|\bkhaki\b|\barmy\b", {"Green", "Brown", "Tan"}, "Khaki", "Khaki"),
    (r"\bteal\b", {"Green", "Blue"}, "Green", "Turquoise"),
    (r"\bturquoise\b|\baqua\b", {"Blue", "Green"}, "Blue", "Turquoise"),
    (r"\bbeige\b|\bnude\b|\bcamel\b|\bsand\b|\btaupe\b|\boatmeal\b", {"Tan", "Cream", "Brown"}, "Tan", "Beige"),
    (r"\bmint\b", {"Green"}, "Green", "Mint"),
    (r"\blilac\b|\blavender\b", {"Purple", "Pink"}, "Purple", "Lilac"),
    (r"\bcoral\b", {"Orange", "Pink", "Red"}, "Orange", "Coral"),
    (r"\bmustard\b", {"Yellow"}, "Yellow", "Mustard"),
    (r"\blight blue\b|\bbaby blue\b|\bsky blue\b|\bpowder blue\b|\bpale blue\b", {"Blue"}, "Blue", "Light blue"),
    (r"\bdark green\b|\bforest\b|\bemerald\b|\bhunter green\b|\bbottle green\b", {"Green"}, "Green", "Dark green"),
    (r"\brose\b|\bblush\b|\bdusty pink\b", {"Pink"}, "Pink", "Rose"),
    (r"\bapricot\b|\bpeach\b", {"Orange", "Pink"}, "Orange", "Apricot"),
    (r"\bivory\b|\boff[- ]white\b|\becru\b", {"White", "Cream"}, "Cream", "Cream"),
]
_MULTI = re.compile(r"\bmulti(?:colou?r(?:ed)?)?\b|\brainbow\b", re.I)


def colors_for(view: ItemView, site: str, limit: int = 2) -> list[str]:
    """Up to `limit` colour names of `site` ("depop" | "vinted"): each palette colour of the listing, as the site
    names its shade when the listing's colour words say which ("navy" for Blue, "olive" for Green)."""
    palette = DEPOP_PALETTE if site == "depop" else VINTED_PALETTE
    words = f"{view.facts.color_name or ''}"
    if _MULTI.search(words) or len(view.render.colors) > 2:
        return ["Multi"]
    out: list[str] = []
    for c in view.render.colors:
        name = palette.get(c)
        for rx, family, depop_name, vinted_name in SHADES:
            if c in family and re.search(rx, words, re.I):
                name = depop_name if site == "depop" else vinted_name
                break
        if name and name not in out:
            out.append(name)
    return out[:limit]


# ---------------------------------------------------------------- material (labels only)

# A fiber as the label reads it (premium.composition, English, lower case) → (Vinted's material, Depop's material).
FIBERS = [
    (r"organic cotton", "Cotton", "Cotton - Organic"),
    (r"recycled cotton", "Cotton", "Cotton - Recycled"),
    (r"recycled polyester", "Polyester", "Polyester - Recycled"),
    (r"merino", "Merino", "Wool"),
    (r"cashmere", "Cashmere", "Cashmere"),
    (r"lambswool|lamb'?s ?wool|virgin wool|\bwool\b", "Wool", "Wool"),
    (r"alpaca", "Alpaca", None),
    (r"mohair", "Mohair", None),
    (r"\bsilk\b", "Silk", "Silk"),
    (r"\blinen\b|\bflax\b", "Linen", "Linen"),
    (r"viscose", "Rayon", "Viscose"),
    (r"\bmodal\b", "Rayon", "Modal"),
    (r"lyocell|tencel", "Rayon", "Lyocell"),
    (r"\brayon\b", "Rayon", "Rayon"),
    (r"nylon|polyamide", "Nylon", "Nylon"),
    (r"elastane|spandex|lycra", "Elastane", "Elastane / Lycra / Spandex"),
    (r"acrylic", "Acrylic", "Acrylic"),
    (r"polyester", "Polyester", "Polyester"),
    (r"(?:faux|vegan|pu|polyurethane)\s*leather|polyurethane", "Faux leather", "Faux leather"),
    (r"patent leather", "Patent leather", "Leather"),
    (r"\bsuede\b", "Suede", "Suede"),
    (r"\bleather\b", "Leather", "Leather"),
    (r"faux fur", "Faux fur", "Faux fur"),
    (r"\bdown\b", "Down", None),
    (r"fleece", "Fleece", "Fleece"),
    (r"corduroy", "Corduroy", "Corduroy"),
    (r"\bdenim\b", "Denim", "Denim"),
    (r"velvet", "Velvet", "Velvet"),
    (r"velour", "Velour", None),
    (r"\bsatin\b", "Satin", None),
    (r"\blace\b", "Lace", "Lace"),
    (r"\btweed\b", "Tweed", "Tweed"),
    (r"\bhemp\b", None, "Hemp"),
    (r"bamboo", "Bamboo", None),
    (r"\bcotton\b|pima|supima", "Cotton", "Cotton"),
]
_PCT_FIBER = re.compile(r"(\d{1,3})\s*%\s*([a-zA-Z' -]+?)(?=\s*(?:\d|,|/|;|\.|$))")


def _fibers(view: ItemView) -> list[str]:
    """The main fabric's fibers as a label prints them, the largest share first. Only a read label counts: the close
    read of the labels (premium.composition), else facts.material when it came from a photo of a label or a seller
    note. Nothing when no label was read (WO30: the material is never guessed)."""
    p = view.facts.premium
    if p and p.composition:
        main = [f for f in p.composition if f.part == "main"] or list(p.composition)
        return [f.fiber.lower() for f in sorted(main, key=lambda f: -f.pct)]
    m = view.facts.material
    if not m.value or m.source not in ("photo", "note", "owner"):
        return []
    found = [(int(pct), fiber.strip().lower()) for pct, fiber in _PCT_FIBER.findall(m.value)]
    if found:
        return [f for _, f in sorted(found, key=lambda x: -x[0])]
    return [m.value.strip().lower()]


def materials_for(view: ItemView, site: str, allowed: list[str], limit: int) -> list[str]:
    """Up to `limit` of the site's materials (`allowed`, its list), from the label only."""
    out: list[str] = []
    for fiber in _fibers(view):
        for rx, vinted_name, depop_name in FIBERS:
            if re.search(rx, fiber, re.I):
                name = vinted_name if site == "vinted" else depop_name
                if name and name in allowed and name not in out:
                    out.append(name)
                break
    return out[:limit]


# ---------------------------------------------------------------- package size

_HEAVY = re.compile(r"\bcoat|\bparka|\bpuffer|\btrench|\bovercoat|\bpeacoat|\bpea coat|\bshearling|\bdown jacket",
                    re.I)
_KNIT = re.compile(r"\bsweater|\bcardigan|\bhoodie|\bsweatshirt|\bknit|\bfleece", re.I)
_XS_TOPS = re.compile(r"\bt-?shirt|\btee\b|\btees\b|\btank\b|\bcami|\bcrop top|\bbodysuit|\bbralette|\bbra\b|\bpant(?:y|ies)\b",
                      re.I)


def package_class(view: ItemView) -> str:
    """xs | light | medium | heavy | kids_shoes — the weight estimate behind both sites' package sizes (WO30): tees,
    tanks, lingerie and kids clothing are extra small; other light clothing small; jeans, sweaters, shoes, bags and
    light jackets medium; coats and boots large."""
    r, f = view.render, view.facts
    cat, sub = r.category, r.subcategory or ""
    words = f"{f.item_type} {r.title}"
    if r.department == "Kids":
        if cat == "Shoes":
            return "kids_shoes"
        return "medium" if cat == "Jackets & Coats" or _HEAVY.search(words) else "xs"
    if cat == "Shoes":
        return "heavy" if "boot" in f"{sub} {words}".lower() else "medium"
    if cat == "Jackets & Coats":
        return "heavy" if _HEAVY.search(f"{sub} {words}") else "medium"
    if cat in ("Bags", "Jeans", "Sweaters", "Suits & Blazers") or _KNIT.search(words) or (f.set_pieces or 0) >= 2:
        return "medium"
    if cat in ("Jewelry", "Accessories", "Intimates & Sleepwear", "Swim", "Underwear & Socks") or _XS_TOPS.search(words):
        return "xs"
    return "light"


VINTED_PACKAGES = {"xs": ["X_SMALL", "SMALL"], "light": ["SMALL"], "medium": ["MEDIUM"], "kids_shoes": ["MEDIUM"],
                   "heavy": ["LARGE"]}
DEPOP_PACKAGES = {"xs": "Extra small", "light": "Small", "medium": "Large", "kids_shoes": "Medium",
                  "heavy": "Extra large"}


# ---------------------------------------------------------------- photos, hashtags

def photos_for(view: ItemView, limit: int) -> list[str]:
    """The listing's photos in Poshmark's order (the front cover first), cut to `limit`: a flaw photo is kept before
    any other (every flaw shown, WO16), then the rest in order."""
    photos = list(view.render.photos)
    if len(photos) <= limit:
        return photos
    cover, rest = photos[0], photos[1:]
    flaws = [p for p in rest if p in view.flaw_photos][: limit - 1]
    keep = set(flaws)
    for p in rest:
        if len(keep) >= limit - 1:
            break
        keep.add(p)
    return [cover] + [p for p in rest if p in keep]


def brand_tag(brand: str | None) -> str | None:
    t = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
    return t or None
