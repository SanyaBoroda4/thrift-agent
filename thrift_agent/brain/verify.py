"""Second opinion + deterministic lint. Strips any claim the facts don't support."""
from __future__ import annotations

import json
import re
import unicodedata

from thrift_agent.brain import llm
from thrift_agent.brain.copy import strip_tag_lines
from thrift_agent.schema import CopyOut, Facts, VerifyOut

SYSTEM = """You audit resale listing copy against a fact sheet. A claim is unsupported if the facts
don't state it (fabric, fit, measurements, era, authenticity, odor/smoke claims, "true to size",
a condition better than the facts', a missing flaw). Return the copy with unsupported claims removed
and missing flaws added, changing nothing else. If everything is supported, return it unchanged.
poshmark_style_tags are shown for the audit only (an unsupported tag goes in `unsupported`); they are not
part of your output, and the Depop hashtag line is added by code after your audit."""


def verify(facts: Facts, copy: CopyOut, model: str) -> VerifyOut:
    # The Depop body goes without its hashtag line so the verifier never edits or duplicates the tags;
    # copy.clean() re-attaches the draft's hashtags after the audit. Style tags are included so the audit sees
    # everything the buyer will; VerifyOut carries no tags, so they come back from the draft untouched.
    content = [llm.text("FACTS\n" + facts.model_dump_json(indent=1) +
                        "\n\nCOPY\n" + json.dumps({
                            "poshmark_title": copy.poshmark_title,
                            "poshmark_description": copy.poshmark_description,
                            "poshmark_style_tags": copy.poshmark_style_tags,
                            "depop_description": strip_tag_lines(copy.depop_description)}, indent=1))]
    return llm.ask(model, SYSTEM, content, VerifyOut, "report_audit", "Report the audit", max_tokens=2500)


MIN_DESCRIPTION = 20
# Whole words and their inflections only: "activewear", "footwear", "market", "pillow", "whole", "stainless"
# must not count as a flaw disclosure.
FLAW_WORDS = (r"\b(flaws?|wear|worn|scuff(?:s|ed|ing)?|stain(?:s|ed|ing)?|marks?|markings?|pill(?:s|ed|ing)?|"
              r"snag(?:s|ged|ging)?|holes?|tears?|torn|scratch(?:es|ed|ing)?|creas(?:e|es|ed|ing)|"
              r"fad(?:e|es|ed|ing)|discolou?r(?:ed|ation|ing)?|missing)\b")
# Shades copy uses for the palette colours (schema.Color). Both the copy's colour words and the facts' are normalised
# through SHADES before comparing, so "navy" on a Blue item or "ivory" on a Cream one is not a mismatch.
SHADES = {
    "navy": "blue", "cobalt": "blue", "teal": "blue", "turquoise": "blue",
    "olive": "green", "khaki": "green", "sage": "green", "mint": "green",
    "ivory": "cream", "ecru": "cream", "natural": "cream", "off white": "cream", "oatmeal": "cream", "bone": "cream",
    "charcoal": "gray", "slate": "gray", "graphite": "gray", "grey": "gray",
    "beige": "tan", "camel": "tan", "taupe": "tan", "sand": "tan", "nude": "tan",
    "burgundy": "red", "maroon": "red", "wine": "red", "rust": "red",
    "blush": "pink", "rose": "pink", "coral": "pink",
    "mustard": "yellow",
    "lavender": "purple", "lilac": "purple", "plum": "purple",
    "chocolate": "brown", "cognac": "brown", "espresso": "brown", "tobacco": "brown",
    "rose gold": "gold",
    "gunmetal": "silver",
}
PALETTE = ("red", "pink", "orange", "yellow", "green", "blue", "purple", "gold", "silver", "black", "gray", "white",
           "cream", "brown", "tan")
# Multi-word shades first so "rose gold" / "off-white" match as a unit, not as "rose" + "gold" or "white".
COLOR_WORDS = (r"\b(rose[ -]gold|off[ -]white|" + "|".join(w for w in SHADES if " " not in w) + "|" +
               "|".join(PALETTE) + r")\b")
# A metal colour naming hardware ("gold hardware", "silver-tone buckle") is not the item's colour: the hardware noun
# must be the next word or the one after.
HARDWARE_COLOR = re.compile(
    r"\b(?:rose[ -]gold|gold|silver|gunmetal)(?:[ -]+\w+)?[ -]+"
    r"(?:hardware|buckles?|zippers?|zip|chains?|clasps?|buttons?|studs|logo|pins?|trim|eyelets|grommets|rivets|"
    r"plaques?|accents)\b", re.I)
BANNED = [r"\bsmoke[- ]?free\b", r"\bpet[- ]?free\b", r"\bauthentic\b", r"\btrue to size\b"]
KIDS_WORDS = r"\bkids['’]?\b|\bkid['’]s\b|\btoddler|\bgirls['’]?\b|\bgirl['’]s\b|\bboys['’]?\b|\bboy['’]s\b"
# Condition ladder, worst to best. A phrase claiming a grade above the facts' grade is a problem.
RANK = ["fair", "good", "excellent", "like_new", "NWOT", "NWT"]
CONDITION_CLAIMS = {
    "NWT": r"\bNWT\b|new with tags",
    "NWOT": r"\bNWOT\b|new without tags|never worn|\bunworn\b|brand new",
    "like_new": r"like[- ]new",
}


def _key(s: str) -> str:
    """Comparison form: ASCII letters and digits only, lowercased ("Levi’s" == "Levi's", "J.Crew" == "J. Crew")."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _brand_needle(brand: str) -> str:
    """The leading word(s) of the brand, in comparison form, long enough to mean something: "Tory Burch" -> "tory",
    "J. Crew" -> "jcrew" (a lone "j" would match any title), "Levi's" -> "levis"."""
    key = ""
    for word in brand.split():
        key += _key(word)
        if len(key) >= 3:
            break
    return key


def _shade(word: str) -> str:
    """A colour word's palette colour: "navy" -> "blue", "Rose-Gold" -> "gold", "Red" -> "red"."""
    word = re.sub(r"[\s-]+", " ", word.strip().lower())
    return SHADES.get(word, word)


def _colors_in(text: str) -> set[str]:
    """The palette colours named in a piece of text ("Silver Jeans Co" -> {"silver"})."""
    return {_shade(w) for w in re.findall(COLOR_WORDS, text.lower())}


def lint(facts: Facts, copy: CopyOut) -> list[str]:
    problems: list[str] = []
    t, d, dd = copy.poshmark_title, copy.poshmark_description, copy.depop_description
    everything = f"{t}\n{d}\n{dd}\n{' '.join(copy.poshmark_style_tags)}"
    if len(t) > 80:
        problems.append("title over 80 chars")
    if len(dd) > 1000:
        problems.append("depop description over 1000 chars")
    if len(d.strip()) < MIN_DESCRIPTION:
        problems.append("poshmark description too short")
    if len(strip_tag_lines(dd)) < MIN_DESCRIPTION:                 # the prose, not the hashtag line
        problems.append("depop description too short")
    needle = _brand_needle(facts.brand.value or "")
    if needle and needle not in _key(t):
        problems.append("brand missing from title")
    style = _key(facts.style_name.value or "")
    if style and style not in _key(t):                       # buyers search "Birkenstock Arizona", not "sandals"
        problems.append("style name missing from title")
    size = (facts.size_us.value or "").strip()
    # "size 8" must be there as such: '8' inside '98' or '8.5', or 'M' inside 'Madewell', does not count.
    if size and not re.search(rf"\bsize\s*{re.escape(size)}(?!\w|\.\d)", t, re.I):
        problems.append("US size missing from title")
    rank = RANK.index(facts.condition)
    for grade, pat in CONDITION_CLAIMS.items():
        if RANK.index(grade) > rank and re.search(pat, everything, re.I):
            problems.append("says NWT but facts aren't NWT" if grade == "NWT"
                            else f"copy claims {grade} but facts are {facts.condition}")
    if rank < RANK.index("NWOT") and re.match(r"new\b", t, re.I):
        problems.append(f"title starts with New but facts are {facts.condition}")
    if re.search(r"\d,5\b", everything):
        problems.append("decimal comma in a size")
    names = f"{facts.brand.value or ''} {facts.style_name.value or ''}"    # "Vans Authentic" is a style, not a claim
    for pat in BANNED:
        m = re.search(pat, everything, re.I)
        if m and not re.search(pat, names, re.I):
            problems.append(f"unsupported phrase: {m.group(0).lower()}")
    material = (facts.material.value or "").lower()
    for m in re.finditer(r"100%\s*([a-z]+)", everything, re.I):     # "100% <fiber>" only when the label says so
        fiber = m.group(1).lower()
        if fiber not in material:
            problems.append(f"unsupported phrase: 100% {fiber}")
    if facts.department != "Kids" and re.search(KIDS_WORDS, everything, re.I):
        problems.append("mentions kids but the item isn't Kids")
    if facts.department == "Women" and re.search(r"\bmen'?s\b", everything, re.I) and "women" not in everything.lower():
        problems.append("mentions men's on a Women's item")
    # Colours compare on the palette (SHADES): "navy" on a Blue item passes, "olive" on a Red one doesn't. A colour
    # word that is part of the brand, style, item type, material or a feature is fine ("Silver Jeans Co"), and so is
    # a metal colour naming hardware ("gold hardware" on a black bag is not a gold bag).
    allowed = {_shade(c) for c in facts.colors}
    for source in (facts.color_name, facts.brand.value, facts.item_type, facts.material.value,
                   facts.style_name.value, *facts.features):
        allowed |= _colors_in(source or "")
    if allowed & {"tan", "cream", "brown"}:
        allowed |= {"tan", "cream"}
    if allowed & {"gray", "silver"}:
        allowed.add("gray")
    named = {w: _shade(w) for w in re.findall(COLOR_WORDS, HARDWARE_COLOR.sub(" ", everything.lower()))}
    if off := sorted(w for w, base in named.items() if base not in allowed):
        problems.append(f"color words not in facts: {off}")
    if facts.flaws:
        for marketplace, text in (("poshmark", d), ("depop", dd)):
            if not re.search(FLAW_WORDS, text, re.I):
                problems.append(f"facts list flaws but the {marketplace} description doesn't mention any")
    return problems
