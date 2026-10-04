"""Poshmark's category tree (data/poshmark_taxonomy.yaml): the names the create-listing form offers.

fit() puts the model's department, category and subcategory onto those names right after extraction (Kids "Tops" is
Poshmark's "Shirts & Tops"; "Jumpsuits & Rompers" given as the category is Pants & Jumpsuits > Jumpsuits & Rompers), so
the poster never meets a name the form doesn't have. What it can't place is a question for the owner (a department or
category Poshmark doesn't have) or a note (a subcategory, which Poshmark treats as optional: it is left out and noted in
the item's record). Since WO24 every department's lists are the form's own catalog (verified); a list marked unverified
would never be grounds for a question: the poster asks (needs_owner) if the live form turns out not to have the name.
"""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

from thrift_agent.config import ROOT
from thrift_agent.schema import Facts

TAXONOMY_FILE = ROOT / "data" / "poshmark_taxonomy.yaml"


@lru_cache(maxsize=1)
def load() -> dict:
    return yaml.safe_load(TAXONOMY_FILE.read_text(encoding="utf-8"))


def _key(name: str) -> str:
    """Comparison form: case, spacing, punctuation and "and" vs "&" ignored ("Jackets and Coats" == "Jackets & Coats")."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", "&", str(name).lower()))


def _find(name: str, choices) -> str | None:
    """The choice that is `name` (see _key), allowing a plural s/es either way ("Dress" finds "Dresses")."""
    k = _key(name)
    for choice in choices:
        c = _key(choice)
        if k in (c, c + "s", c + "es") or c in (k + "s", k + "es"):
            return choice
    return None


def _alias(kind: str, scope: str, name: str, choices) -> str | None:
    """aliases.<kind>.<scope> (scope = department, or "Department/Category" for subcategories): the model's word ->
    Poshmark's name, used only when that name is one of `choices`."""
    table = ((load().get("aliases") or {}).get(kind) or {}).get(scope) or {}
    for word, target in table.items():
        if _key(word) == _key(name) and target in choices:
            return target
    return None


def other_names(department: str, category: str) -> tuple[str, ...]:
    """The other words for a Poshmark category in this department, from aliases.categories — the price table may file
    Poshmark's Kids "Shirts & Tops" under "Tops" — in the file's order, which lists the general word first ("Tops"
    before "Sweaters"), so a kids tee is never priced as a sweater."""
    table = ((load().get("aliases") or {}).get("categories") or {}).get(department) or {}
    return tuple(word for word, target in table.items() if _key(target) == _key(category))


DEPARTMENT_WORDS = ("Women", "Men", "Kids", "Home", "Unisex", "Baby", "Boys", "Girls", "Pets", "Electronics")


def _from_words(text: str | None, department: str, categories) -> str | None:
    """Poshmark's category named by a word or two of `text` ("graphic tee" -> Shirts & Tops), by name or alias."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    for size in (2, 1):
        for k in range(len(words) - size, -1, -1):              # the noun comes last: "denim jacket" -> jacket
            phrase = " ".join(words[k:k + size])
            for form in (phrase, phrase + "s", phrase.rstrip("s")):
                if found := _find(form, categories) or _alias("categories", department, form, categories):
                    return found
    return None


_PARTS = re.compile(r"\s*&\s*|\s+-\s+|\s+or\s+")       # "Sweatshirts & Hoodies", "Tees - Short Sleeve", "A-Line or Full"


def _whole(name: str, subs) -> list[str]:
    return [s for s in subs or [] if _find(name, [s])]


def _part(name: str, subs) -> list[str]:
    """The subcategories one part of whose name is `name` ("Hoodies": "Sweatshirts & Hoodies")."""
    return [s for s in subs or [] if any(_find(name, [part]) for part in _PARTS.split(s) if part.strip())]


def _matches(name: str, subs) -> list[str]:
    """The subcategories that are `name`: by the whole name, else by one part of a two-part name ("Hoodies" is
    "Sweatshirts & Hoodies", "Jumpsuits" is "Jumpsuits & Rompers")."""
    return _whole(name, subs) or _part(name, subs)


def _parent(name: str, categories) -> tuple[str, str | None] | None:
    """(category, subcategory) when `name` is a subcategory of exactly one category here (WO24, live: the model put
    "Jumpsuits & Rompers", Poshmark's subcategory of Pants & Jumpsuits, in the category slot; Men's hoodies are Shirts >
    Sweatshirts & Hoodies). A whole name wins over a part of another's ("Wide Leg" is Pants' own, not Jeans' "Flare &
    Wide Leg"). Several subcategories of that one category match ("Tees"): the category, no subcategory. None when no
    category or several match ("Maxi" is both a dress and a skirt)."""
    if not name or not name.strip():
        return None
    hits = {}
    for match in (_whole, _part):
        if hits := {category: found for category, subs in categories.items() if (found := match(name, subs))}:
            break
    if len(hits) != 1:
        return None
    (category, subs), = hits.items()
    return category, subs[0] if len(subs) == 1 else None


def _example(categories) -> str:
    return next((c for c in categories if "top" in c.lower()), next(iter(categories), "Other"))


def fit(facts: Facts) -> tuple[Facts, list[str], list[str]]:
    """(facts on Poshmark's names, notes for the owner, questions for the owner).

    A department Poshmark doesn't have (Unisex) or a category missing from a verified department's list is a question,
    folded into the approval message like "category Other". A subcategory missing from a recorded list is dropped with
    a note (the form's subcategory is optional). Canonical spelling is fixed silently ("shoes" -> "Shoes")."""
    departments = load()["departments"]
    dept = departments.get(facts.department)
    if dept is None:
        return facts, [], [f"Poshmark has no {facts.department} department — reply 'department Women' or "
                            "'department Men'"]
    categories = dept.get("categories") or {}
    sub, notes = facts.subcategory, []
    if _key(facts.category) in {_key(d) for d in (*DEPARTMENT_WORDS, *departments)}:
        # A department is never a category (WO23; live: category "Kids", subcategory "Shirts & Tops" — the model put
        # the department one level down). The real category is the subcategory it gave, else the item type's noun.
        if found := _find(sub or "", categories):
            facts, sub = facts.model_copy(update={"category": found}), None
        elif lifted := _parent(sub or "", categories):          # "Sneakers": Shoes, and the subcategory kept
            facts, sub = facts.model_copy(update={"category": lifted[0]}), lifted[1]
        elif found := _alias("categories", facts.department, sub or "", categories):
            facts, sub = facts.model_copy(update={"category": found}), None
        elif found := _from_words(facts.item_type, facts.department, categories):
            facts = facts.model_copy(update={"category": found})
    category = _find(facts.category, categories)
    if category is None and (lifted := _parent(facts.category, categories)):
        category, sub = lifted[0], lifted[1] or sub             # Poshmark's subcategory, given as the category
    if category is None:
        category = _alias("categories", facts.department, facts.category, categories)
    if category is None:
        if not dept.get("verified"):
            return facts, [], []                      # an unconfirmed list: the poster asks if the form lacks it
        return facts, [], [f"category '{facts.category}' is not one of Poshmark's {facts.department} categories — "
                            f"reply e.g. 'category {_example(categories)}'"]
    subs = categories.get(category)
    if sub and subs:
        one = _matches(sub, subs)
        found = (one[0] if len(one) == 1 else None) or _alias("subcategories", f"{facts.department}/{category}", sub,
                                                               subs)
        if found is None:
            notes.append(f"subcategory '{sub}' is not in Poshmark's {facts.department} {category} list: listed "
                         f"without one; reply e.g. 'subcategory {subs[0]}' to set it")
        sub = found
    return facts.model_copy(update={"category": category, "subcategory": sub}), notes, []


def style_tags() -> list[str]:
    """Poshmark's curated style tags, as its form lists them."""
    return list(load().get("style_tags") or [])


def style_tag(name: str) -> str | None:
    """Poshmark's spelling of a curated style tag ("leopard print" -> "Leopard Print", "y2k" -> "Y2K"), else None."""
    return _find(name, style_tags()) if name and name.strip() else None


def prompt_text() -> str:
    """The lists for the extraction prompt, so the model picks Poshmark's own names."""
    lines = ["Poshmark's category names (use them exactly; a subcategory from its list when one is given):"]
    for name, dept in load()["departments"].items():
        categories = dept.get("categories") or {}
        mark = "" if dept.get("verified") else " (unconfirmed list)"
        lines.append(f"- {name}{mark}: {', '.join(categories)}")
        for category, subs in categories.items():
            if subs:
                lines.append(f"  - {name} > {category}: {', '.join(subs)}")
    return "\n".join(lines)
