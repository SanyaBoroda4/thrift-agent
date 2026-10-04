"""Poshmark's category tree (data/poshmark_taxonomy.yaml): the names the create-listing form offers.

fit() puts the model's department, category and subcategory onto those names right after extraction (Kids "Tops" is
Poshmark's "Shirts & Tops"), so the poster never meets a name the form doesn't have. What it can't place is a question
for the owner (a department or category Poshmark doesn't have) or a note (a subcategory, which Poshmark treats as
optional: it is left out and the owner is told). Lists marked unverified (Men, Home) are never grounds for a question:
the poster asks (needs_owner) if the live form turns out not to have the name.
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
    category = _find(facts.category, categories) or _alias("categories", facts.department, facts.category, categories)
    if category is None:
        if not dept.get("verified"):
            return facts, [], []                      # an unconfirmed list: the poster asks if the form lacks it
        example = "Tops" if "Tops" in categories else next(iter(categories), "Other")
        return facts, [], [f"category '{facts.category}' is not one of Poshmark's {facts.department} categories — "
                            f"reply e.g. 'category {example}'"]
    subs = categories.get(category)
    sub, notes = facts.subcategory, []
    if sub and subs:
        found = _find(sub, subs) or _alias("subcategories", f"{facts.department}/{category}", sub, subs)
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
