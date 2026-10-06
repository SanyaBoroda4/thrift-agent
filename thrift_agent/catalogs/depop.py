"""The item on Depop's listing form, every value from data/depop_catalog.json (WO30).

Depop has no title: the description's first line is the Poshmark title, then a blank line, the Poshmark description
(the same condition and wording rules), then up to 5 hashtags (brand, item, style, colour, era). 1,000 characters at
most: the body is trimmed, never the first line or the hashtags."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from thrift_agent import catalogs
from thrift_agent.catalogs import categories
from thrift_agent.catalogs.common import (DEPOP_PACKAGES, ItemView, MappingError, brand_tag, colors_for,
                                          condition_key, materials_for, package_class, photos_for)
from thrift_agent.catalogs.sizes import depop_size

SHIPPING = "Depop Shipping"          # Depop Shipping (USPS) with the package size; Boost is never turned on


class DepopFields(BaseModel):
    """What the form gets. Saved to listings.fields_json before the form opens."""
    category: str                                  # "Women > Bottoms > Skirts"
    description: str
    hashtags: list[str] = Field(default_factory=list)
    brand: str | None = None                       # typed into brand-input; Depop's own option picked at fill time
    size: str | None = None                        # a value of size_sets_US[size_set], or None (no size field)
    size_set: str | None = None
    condition: str
    colors: list[str] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)
    age: str | None = None
    style: list[str] = Field(default_factory=list)
    attributes: dict[str, list[str]] = Field(default_factory=dict)
    package_size: str
    shipping: str = SHIPPING
    price: int
    photos: list[str] = Field(default_factory=list)
    category_source: str = "table"
    guesses: list[str] = Field(default_factory=list)   # for the "Posted ✓ … — check:" note
    notes: list[str] = Field(default_factory=list)     # for the ops chat


_LENGTH = (("Maxi", r"\bmaxi\b|\bankle[- ]length\b|\bfloor[- ]length\b"), ("Midi", r"\bmidi\b|\btea[- ]length\b"),
           ("Mini", r"\bmini\b"))
_BOTTOM_STYLE = (("Wide leg", r"\bwide[- ]leg"), ("Flare", r"\bflared?\b"), ("Bootcut", r"\bboot[- ]?cut\b"),
                 ("Skinny", r"\bskinny\b|\bjeggings?\b"), ("Straight leg", r"\bstraight\b"), ("Slim", r"\bslim\b"),
                 ("High waisted", r"\bhigh[- ](?:waist(?:ed)?|rise)\b"), ("Low rise", r"\blow[- ]rise\b"),
                 ("Tailored", r"\btailored\b|\btrousers?\b"))
_ERA = re.compile(r"(?:19)?([5-9]0)s|20?(00)s|\by2k\b", re.I)


def era(view: ItemView) -> str | None:
    """A decade only when the label read gave one with a concrete vintage cue (premium.vintage): "90s", "00s"."""
    p = view.facts.premium
    v = (p.vintage.value or "") if p else ""
    if not v:
        return None
    if re.search(r"\by2k\b", v, re.I):
        return "00s"
    m = _ERA.search(v)
    return f"{m.group(1) or m.group(2)}s" if m else None


def _first(rules, text: str) -> str | None:
    return next((name for name, rx in rules if re.search(rx, text, re.I)), None)


def attributes(view: ItemView, path: str) -> tuple[dict[str, list[str]], list[str]]:
    """The optional attributes the facts support (WO30): material (labels only), dress-length, bottom-style (fit words),
    body-fit (Petite / Plus / Tall / Maternity from the label). Occasion and style are left empty."""
    cat = catalogs.depop_catalog()
    have = set(cat.attributes(path))
    out: dict[str, list[str]] = {}
    notes: list[str] = []
    words = f"{view.render.subcategory or ''} {view.facts.item_type} {view.render.title}"
    if "material" in have:
        if mats := materials_for(view, "depop", cat.attribute_values["material"], cat.attribute_limits.get("material", 4)):
            out["material"] = mats
    if "dress-length" in have and (length := _first(_LENGTH, words)):
        out["dress-length"] = [length]
    if "bottom-style" in have:
        styles = [name for name, rx in _BOTTOM_STYLE if re.search(rx, words, re.I)][:2]
        if styles:
            out["bottom-style"] = styles
    if "body-fit" in have:
        label = f"{view.render.size_tab or ''} {view.facts.size_printed.value or ''} {view.facts.item_type}"
        fits = [name for name, rx in (("Petite", r"\bpetite\b"), ("Plus size", r"\bplus\b"), ("Tall", r"\btall\b"),
                                      ("Maternity", r"\bmaternity\b")) if re.search(rx, label, re.I)]
        if fits:
            out["body-fit"] = fits[: cat.attribute_limits.get("body-fit", 2)]
    for name, values in out.items():
        allowed = cat.attribute_values.get(name, [])
        bad = [v for v in values if v not in allowed]
        if bad:                                     # never a value the catalog doesn't list
            notes.append(f"attribute {name}: {bad} not in the catalog, left out")
            out[name] = [v for v in values if v in allowed]
    return {k: v for k, v in out.items() if v}, notes


def hashtags(view: ItemView, colors: list[str], limit: int = 5) -> list[str]:
    """Up to 5: brand, item, style, colour, era — lower case, no spaces."""
    r = view.render
    item = r.subcategory if r.subcategory and r.subcategory not in ("Maxi", "Midi", "Mini") else \
        (view.facts.item_type.split()[-1] if view.facts.item_type else r.category)
    tags = [brand_tag(r.brand), brand_tag(item), brand_tag(r.tags[0]) if r.tags else None,
            brand_tag(colors[0]) if colors else None, brand_tag(era(view))]
    out: list[str] = []
    for t in tags:
        if t and t not in out:
            out.append(t)
    return out[:limit]


def description(title: str, body: str, tags: list[str], limit: int = 1000) -> str:
    """The title, a blank line, the body, a blank line, the hashtags — at most `limit` characters: only the body is
    trimmed (at a line or sentence end when it can be), never the first line or the hashtags."""
    body = "\n".join(line for line in body.strip().splitlines() if not re.fullmatch(r"\s*(#\w+\s*)+", line)).strip()
    tag_line = " ".join(f"#{t}" for t in tags)
    head, tail = title.strip(), (f"\n\n{tag_line}" if tag_line else "")
    room = limit - len(head) - len(tail) - 2
    if room <= 0:
        raise MappingError("the title and hashtags alone are over Depop's 1000 characters")
    if len(body) > room:
        cut = body[:room]
        stop = max(cut.rfind("\n"), cut.rfind(". "))
        body = (cut[: stop + 1] if stop > room // 2 else cut[: room - 1].rstrip() + "…").rstrip()
    return f"{head}\n\n{body}{tail}" if body else f"{head}{tail}"


def map_depop(view: ItemView, ask=None) -> DepopFields:
    """The item's values for Depop's form. MappingError when a required one can't be taken from the facts (the item is
    then skipped on Depop only)."""
    cat = catalogs.depop_catalog()
    r = view.render
    pick = categories.resolve("depop", r.department, r.category, r.subcategory, view.facts.item_type, r.title,
                              r.kids_gender, ask=ask)
    path = pick.value
    guesses: list[str] = []
    notes: list[str] = [f"category {path} ({pick.source})"]
    size = sid = None
    if (sid := cat.size_set_id(path)) is not None:
        size, guess = depop_size(view.size_in(), cat.size_set(sid), cat.size_sets_US[sid].path)
        if guess:
            guesses.append(guess)
    key = condition_key(r.condition)
    condition = cat.condition_map_from_agent.get(key) or cat.condition_map_from_agent.get("Like New")
    if key == "New without tags":                   # Poshmark's Like New (WO18): the same on Depop
        condition = cat.condition_map_from_agent["Like New"]
    if condition not in cat.condition or condition == cat.condition_map_from_agent.get("never use"):
        raise MappingError(f"condition {r.condition} has no Depop value")
    colors = [c for c in colors_for(view, "depop", int(cat.color.get("max", 2))) if c in cat.colors()]
    v_era = era(view)
    source = ["Vintage"] if view.facts.premium and view.facts.premium.vintage.value else [cat.source["default"]]
    age = v_era if v_era in cat.age["values"] else cat.age["default"]
    attrs, attr_notes = attributes(view, path)
    package = DEPOP_PACKAGES[package_class(view)]
    if package not in {p["name"] for p in cat.package_sizes}:
        raise MappingError(f"package size {package!r} isn't Depop's")
    tags = hashtags(view, colors, int(cat.form["description"]["max_hashtags"]))
    text = description(r.title, r.description, tags, int(cat.form["description"]["max_chars"]))
    return DepopFields(category=path, description=text, hashtags=tags, brand=r.brand, size=size, size_set=sid,
                       condition=condition, colors=colors, source=source, age=age, attributes=attrs,
                       package_size=package, price=view.price, photos=photos_for(view, int(cat.form["photos"]["max"])),
                       category_source=pick.source, guesses=guesses, notes=notes + attr_notes)
