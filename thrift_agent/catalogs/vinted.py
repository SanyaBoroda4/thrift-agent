"""The item on Vinted's upload form, every value (and its id) from data/vinted_catalog.json (WO30).

The title and the description are Poshmark's, as they are (no hashtags). The leaf decides which fields there are: its
size library (`fields.size.lib`), its condition library, and Skirts' required skirt length."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, create_model

from thrift_agent import catalogs
from thrift_agent.catalogs import categories
from thrift_agent.catalogs.common import (VINTED_PACKAGES, ItemView, MappingError, colors_for, condition_key,
                                          materials_for, package_class, photos_for)
from thrift_agent.catalogs.sizes import vinted_size


class VintedFields(BaseModel):
    """What the form gets. Saved to listings.fields_json before the form opens."""
    category_id: int
    category_path: str
    title: str
    description: str
    brand: str | None = None                   # looked up in Vinted's brands at fill time: exact or normalized only
    size_id: int | None = None
    size: str | None = None
    condition_id: int
    condition: str
    color_ids: list[int] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    material_ids: list[int] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    skirt_length_id: int | None = None
    skirt_length: str | None = None
    package_sizes: list[str] = Field(default_factory=list)   # in preference order: the first the leaf offers
    price: int
    photos: list[str] = Field(default_factory=list)
    category_source: str = "table"
    guesses: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


_SKIRT = (("Mini", r"\bmini\b"), ("Midi", r"\bmidi\b"), ("Maxi", r"\bmaxi\b|\bfloor[- ]length\b"),
          ("Knee-length", r"\bknee[- ]length\b|\bknee\b"), ("Asymmetrical", r"\basymmetric|\bhigh[- ]low\b"))


def skirt_length(view: ItemView, ask=None) -> str:
    """Vinted's required skirt length: the Poshmark subcategory or the item's words; unknown → the model picks one of
    the catalog's five (an enum)."""
    words = f"{view.render.subcategory or ''} {view.facts.item_type} {view.render.title}"
    if found := next((name for name, rx in _SKIRT if re.search(rx, words, re.I)), None):
        return found
    titles = tuple(o.title for o in catalogs.vinted_catalog().library("skirt_length_0"))
    return (ask or ask_skirt_length)(view, titles)


def ask_skirt_length(view: ItemView, titles: tuple[str, ...]) -> str:
    from thrift_agent.brain import llm
    from thrift_agent.config import settings

    s = settings()
    out = create_model("SkirtLength", length=(Literal[titles], Field(description="The skirt's length")))  # type: ignore[valid-type]
    text = f"Item: {view.facts.item_type}\nTitle: {view.render.title}\nDescription: {view.render.description[:600]}"
    return llm.ask(s["models"].get("crosslist", "claude-opus-5-5"),
                   "You list second-hand skirts on Vinted. Pick the skirt's length from the options.",
                   [{"type": "text", "text": text}], out, "skirt_length", "Record the skirt length").length


def map_vinted(view: ItemView, ask=None, ask_length=None) -> VintedFields:
    """The item's values for Vinted's form. MappingError when a required one can't be taken from the facts (the item is
    then skipped on Vinted only)."""
    cat = catalogs.vinted_catalog()
    r = view.render
    pick = categories.resolve("vinted", r.department, r.category, r.subcategory, view.facts.item_type, r.title,
                              r.kids_gender, ask=ask)
    leaf = cat.category(int(pick.value))
    guesses: list[str] = []
    notes: list[str] = [f"category {leaf.path} ({pick.source})"]
    size_id = size_title = None
    if (f := leaf.fields.get("size")) is not None and f.lib:
        try:
            option, guess = vinted_size(view.size_in(), f.lib, cat.library(f.lib))
            size_id, size_title = option.id, option.title
            if guess:
                guesses.append(guess)
        except MappingError:
            if f.required:
                raise
            notes.append("size left empty (optional here)")
    cond_lib = leaf.fields["condition"].lib or "condition_0"
    cid = cat.condition_map_from_agent.get(condition_key(r.condition))
    cond = next((o for o in cat.library(cond_lib) if o.id == cid), None)
    if cond is None:
        raise MappingError(f"condition {r.condition} isn't offered for {leaf.path}")
    color_max = (leaf.fields.get("color").max if leaf.fields.get("color") else None) or 2
    colors = colors_for(view, "vinted", color_max) if "color" in leaf.fields else []
    color_ids = [cat.color(c) for c in colors]
    colors = [c for c, i in zip(colors, color_ids) if i is not None]
    mat_ids: list[int] = []
    mats: list[str] = []
    if (m := leaf.fields.get("material")) is not None and m.lib:
        allowed = {o.title: o.id for o in cat.library(m.lib)}
        mats = materials_for(view, "vinted", list(allowed), m.max or 3)
        mat_ids = [allowed[t] for t in mats]
    sl_id = sl = None
    if (s := leaf.fields.get("skirt_length")) is not None and s.lib:
        sl = skirt_length(view, ask_length)
        sl_id = cat.option(s.lib, sl)
        if sl_id is None:
            raise MappingError(f"skirt length {sl!r} isn't Vinted's")
    packages = VINTED_PACKAGES[package_class(view)]
    known = {p.code for p in cat.package_sizes}
    if not set(packages) <= known:
        raise MappingError(f"package sizes {packages} aren't Vinted's")
    return VintedFields(category_id=int(pick.value), category_path=leaf.path, title=r.title, description=r.description,
                        brand=r.brand, size_id=size_id, size=size_title, condition_id=cond.id, condition=cond.title,
                        color_ids=[i for i in color_ids if i is not None], colors=colors, material_ids=mat_ids,
                        materials=mats, skirt_length_id=sl_id, skirt_length=sl, package_sizes=packages, price=view.price,
                        photos=photos_for(view, int(cat.form["photos"]["max"])), category_source=pick.source,
                        guesses=guesses, notes=notes)
