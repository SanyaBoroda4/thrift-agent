"""One call writes both marketplaces' copy from the same facts; code enforces the hard limits."""
from __future__ import annotations

import json
import re

import yaml

from thrift_agent.brain import llm, taxonomy
from thrift_agent.brain.sizes import size_label, title_size
from thrift_agent.config import style_dir
from thrift_agent.schema import CopyOut, Ev, Facts, VerifyOut

TITLE_MAX, DEPOP_MAX = 80, 1000
TEXT_FIELDS = ("poshmark_title", "poshmark_description", "depop_description")
# kids_gender is the model's best guess for Poshmark's size tab, not evidence: the copy never states it. The flaws and
# the condition evidence are what the photos show: the copy never describes them (the owner's condition rule, below).
VIEW_EXCLUDE = {"cover_photo", "photo_order", "photo_roles", "questions", "kids_gender", "kids_gender_confidence",
                "flaws", "condition_evidence", "condition_alternative", "hang_tag_photo", "unworn", "box_photo"}
TAG_LINE = re.compile(r"(?m)^[ \t]*(#\w+[ \t]*)+\r?$")   # a line that is nothing but hashtags
TRAILING_TAGS = re.compile(r"(\s*#\w+)+\s*$")           # hashtags tacked onto the end of the last sentence

# The owner's condition rule (WO16, after the first live listing). Wear and flaws are never put in words — not in the
# title, the descriptions or the tags: the photos show them (every flaw photo is in the listing, never the cover), and a
# used item says so in ONE neutral line. A used item is never called like new, excellent or flawless.
USED = ("like_new", "excellent", "good", "fair")
CONDITION_LINE = "Gently pre-loved, please see photos for condition."
CONDITION_LINES = {"NWT": "New with tags.", "NWOT": "New without tags.", **{c: CONDITION_LINE for c in USED}}
NEW_IN_BOX = "New in box."      # NWT shoes whose box is in the seller's photos (WO18)
HAS_CONDITION_LINE = re.compile(r"gently\s+pre-?loved,?\s+please\s+see\s+(?:the\s+)?photos\s+for\s+(?:the\s+)?"
                                r"condition", re.I)
# Whole words and their inflections. "wear" alone is fine ("everyday wear"), wear with a measure of it is not ("light
# wear", "signs of wear", "wear and tear"); "worn" never is, even "never worn" (say "new without tags"). Left out on
# purpose, for what they also mean: faded (a wash), spots (a print), marks (a brand), wrinkle-free, stretch.
_WEAR_MEASURE = (r"(?:signs?\s+of|light|lightly|minor|minimal|some|slight|slightly|small|visible|general|normal|heavy|"
                 r"heavily|moderate|gentle|noticeable|little|faint)")
NEGATIVE_WORDS = re.compile(
    r"\b(dirt|dirty|grime|grimy|stain(?:s|ed|ing)?|scuff(?:s|ed|ing)?|worn|"
    rf"(?:{_WEAR_MEASURE}\s+)?wear[\s-]+and[\s-]+tear|{_WEAR_MEASURE}\s+wear(?:ing)?|"
    r"fray(?:s|ed|ing)?|pill(?:s|ed|ing)?|bobbl(?:e|es|ed|ing)|holes?|tears?|torn|tearing|rips?|ripped|ripping|"
    r"snag(?:s|ged|ging)?|smell(?:s|y|ed|ing)?|odou?rs?|musty|scratch(?:es|ed|ing)?|crack(?:s|ed|ing)?|"
    r"peel(?:s|ed|ing)|creas(?:e|es|ed|ing)|discolou?r(?:ed|ation|ing)?|yellow(?:ed|ing)|damage[ds]?|"
    r"defects?|defective|flaws?|flawed|imperfections?|blemish(?:es|ed)?|wrinkled|missing|loose\s+threads?|"
    r"stretched\s+out|(?:small|minor|light|faint|some|few|visible)\s+marks?|markings?)\b", re.I)
# Claims a used item never makes (the facts' grade says what Poshmark's condition field shows; the copy doesn't grade).
USED_CLAIMS = re.compile(r"\b(like[\s-]+new|excellent|mint\s+condition|pristine|perfect\s+condition|flawless|"
                         r"no\s+flaws|without\s+flaws|no\s+(?:signs\s+of\s+)?wear|as\s+new|new\s+condition)\b", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def style_examples() -> str:
    path = style_dir()
    parts = []
    for f in sorted(path.glob("*.yaml")):
        parts.append(f"# {f.name}\n{f.read_text(encoding='utf-8')}")
    for f in sorted(path.glob("*.json")):
        parts.append(f"# {f.name}\n{f.read_text(encoding='utf-8')[:12000]}")
    return "\n\n".join(parts)


SYSTEM = """You write resale listings for one seller's closet, in her established style.

POSHMARK
- Title ≤80 chars: Brand, then item type, standout detail/material, color, then "size X" (US size).
  Include the style name when known — buyers search for it (e.g. "Birkenstock Arizona ...").
  Add "New" at the start only for NWT/NWOT. Use the room — short titles don't get found.
  Decimal sizes use a dot (7.5), never a comma. No emojis, no ALL CAPS words except brand styling.
- Sizes in the TITLE are US only, never EU: adults end with title_size ("size 7.5"); kids shoes use title_size
  verbatim ("Toddler size 7.5" / "Little Kid size 13" / "Big Kid size 4"), never a bare "size 7.5" and never
  "EU 24" in the title. The kids groups are Poshmark's (Toddler up to 12C, Little Kid 12.5-13.5C and 1-3Y, Big Kid
  3.5Y and up), not a brand's size chart: never another group word anywhere in the title. The EU size and the full
  label size_label ("EU 24 / US Toddler 7.5") go in the description's fit line only.
- Description, in this closet's proven shape:
  1) 2–4 short sentences describing what the photos show (type, color, material if known, details).
     Plain and specific; at most one adjective like "chic" or "versatile" — never a string of them.
  2) Then the condition: condition_line from the facts, verbatim, as its own line ("New with tags." / "New in box." /
     "New without tags." / for every used item "Gently pre-loved, please see photos for condition."). A fit line may go before it
     ("Size 38 EU, fits US 7.5-8.").
  3) If retail_price is known, the description ends with "Retail $<price>." as its own last line
     (after the condition line).
  4) Then the footer if one is given. No keyword stuffing, no emojis.
- Style tags: up to 3 from POSHMARK STYLE TAGS below, spelled as listed, or none. A material tag (Leather, Suede,
  Wool, Silk, Cashmere, Linen, Nylon, Satin, Denim, Faux Fur) only when `material` states that material.

DEPOP
- Casual, first-person-seller voice, lowercase is fine. Same facts, fewer words; condition_line verbatim.
- ≤1000 characters INCLUDING a final line of exactly 5 hashtags. Then the footer.

HARD RULES
- Use ONLY the facts given. If a field is null, don't mention it. No guessed fabric, fit,
  measurements, era, "authentic", "smoke-free", or "true to size". A material word (leather, suede, silk...)
  only when `material` states it — an item_type or feature wording is not evidence.
- Color words must match the facts' colors; department words must match the department
  (never "kids" or "men's" for a Women's item) — past listings lost buyers over exactly this.
- Condition is never put in words beyond condition_line: no wear or flaw words anywhere — title, descriptions,
  style tags, hashtags (dirt, dirty, stain, scuff, worn — even "never worn" —, wear and tear, light wear, fraying,
  pilling, hole, tear, rip, snag, smell, odor, crease, crack, peeling, discoloration, damage, flaw, imperfection).
  The photos show the condition. A used item is never "like new", "excellent", "perfect", "pristine", "flawless" or
  "no flaws".
- Never copy wording from the examples — match their shape, not their text (older examples describe wear in words:
  never do that)."""


def facts_view(facts: Facts) -> dict:
    """The facts as the copywriter sees them. Null facts are omitted (invariant 1): an Ev with no value, a None
    scalar or an empty list is simply absent, so there is nothing null for the model to restate. The flaws and the
    condition evidence are left out (the photos show them); condition_line is the one thing to say about condition."""
    view: dict = {}
    for name, val in facts.model_dump(exclude=VIEW_EXCLUDE).items():
        if isinstance(getattr(facts, name), Ev):
            if val["value"] is None:
                continue
        elif val is None or val == []:
            continue
        view[name] = val
    if (label := size_label(facts)) is not None:      # "EU 24 / US Toddler 7.5" for a kids shoe, size_us otherwise
        view["size_label"] = label
    if (ts := title_size(facts)) is not None:          # "Toddler size 7.5" / "size 7.5": the only size the title shows
        view["title_size"] = ts
    view["condition_line"] = condition_line(facts)
    return view


def condition_line(facts: Facts) -> str:
    """The one thing the copy says about condition: "New with tags." ("New in box." for shoes whose box is in the
    photos), "New without tags.", or for any used item the neutral pre-loved line."""
    if facts.condition == "NWT" and facts.box_photo is not None:
        return NEW_IN_BOX
    return CONDITION_LINES[facts.condition]


def write(facts: Facts, model: str, cfg: dict) -> CopyOut:
    view = facts_view(facts)
    content = [llm.text(
        "STYLE EXAMPLES\n" + style_examples() +
        "\n\nFACTS\n" + json.dumps(view, indent=1) +
        f"\n\nPOSHMARK FOOTER: {cfg['poshmark_footer']}\nDEPOP FOOTER: {cfg['depop_footer']}"
        "\n\nPOSHMARK STYLE TAGS (the only ones Poshmark offers): " + ", ".join(taxonomy.style_tags())
    )]
    out = llm.ask(model, SYSTEM, content, CopyOut, "write_listing", "Write both listings", max_tokens=2500)
    return clean(out)


def fix_decimal_commas(s: str) -> str:
    return re.sub(r"(?<=\d),(?=5\b)", ".", s)


def _cut_at_word(text: str, limit: int) -> str:
    """At most `limit` chars, backed up to the last space when there is one."""
    head = text[:limit + 1]
    return head.rsplit(" ", 1)[0] if " " in head else text[:limit]


def clamp_title(title: str) -> str:
    title = re.sub(r"^\s*copy\s*[-–:]\s*", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", fix_decimal_commas(title)).strip()
    if len(title) <= TITLE_MAX:
        return title
    return _cut_at_word(title, TITLE_MAX).rstrip(" -,|")


def strip_tag_lines(body: str) -> str:
    """Remove hashtag-only lines anywhere in the body (plus a run of hashtags at its very end), then tidy the gaps.

    Anywhere, not just the tail: after the verifier appends a sentence below the model's tag line, a trailing-only
    strip would leave those hashtags in the body and clean() would add a second tag line."""
    body = TAG_LINE.sub("", body)
    body = TRAILING_TAGS.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def changed_fields(draft: CopyOut, audit: VerifyOut) -> list[str]:
    """Names of the copy fields whose text the verifier actually changed.

    Compared after whitespace normalisation (any run of whitespace -> one space, stripped, casefolded); the Depop
    body is compared without its hashtag line, which the verifier never sees (verify() strips it)."""

    def norm(name: str, text: str) -> str:
        if name == "depop_description":
            text = strip_tag_lines(text)
        return re.sub(r"\s+", " ", text).strip().casefold()

    return [name for name in TEXT_FIELDS
            if norm(name, getattr(draft, name)) != norm(name, getattr(audit, name))]


def clean(out: CopyOut) -> CopyOut:
    out.poshmark_title = clamp_title(out.poshmark_title)
    out.poshmark_description = fix_decimal_commas(out.poshmark_description).strip()
    out.poshmark_style_tags = [t.strip() for t in out.poshmark_style_tags if t and t.strip()][:3]
    tags = [re.sub(r"[^\w]", "", t.lower()) for t in out.depop_hashtags]
    out.depop_hashtags = [t for t in tags if t][:5]
    body = strip_tag_lines(fix_decimal_commas(out.depop_description))   # the model's own hashtags, wherever they are
    tag_line = " ".join(f"#{t}" for t in out.depop_hashtags)
    limit = DEPOP_MAX - len(tag_line) - 2                    # body + blank line + tag line <= DEPOP_MAX
    if len(body) > limit:
        body = _cut_at_word(body, limit - 1).rstrip() + "\u2026"   # one char reserved for the ellipsis
    out.depop_description = f"{body}\n\n{tag_line}" if tag_line else body
    return out


def ensure_retail_line(description: str, facts: Facts) -> str:
    """The Poshmark description ends with "Retail $<price>." when the facts know the retail price and the copy
    doesn't already say it ("Retail $128", "Retails for $128" anywhere in the text is left alone)."""
    m = re.search(r"\d+(?:\.\d+)?", (facts.retail_price.value or "").replace(",", ""))
    text = description.rstrip()
    if not m or re.search(r"\bretail\w*[^\n$]{0,10}\$", text, re.I):
        return description
    return f"{text}\nRetail ${int(float(m.group()))}."


def condition_wording(text: str, condition: str) -> str:
    """`text` under the owner's condition rule: every sentence that puts wear or a flaw in words (NEGATIVE_WORDS), or
    grades a used item (USED_CLAIMS), is dropped — the photos show the condition — and a used item's text carries
    CONDITION_LINE: where the first dropped sentence was, else as its own line before a closing "Retail $…" line,
    else at the end. Nothing else changes: a dropped sentence is never rewritten, only left out."""
    used, mark, marked = condition in USED, "\x00", False
    lines = []
    for line in text.split("\n"):
        kept = []
        for sentence in _SENTENCES.split(line.strip()) if line.strip() else []:
            if NEGATIVE_WORDS.search(sentence) or (used and USED_CLAIMS.search(sentence)):
                if not marked:
                    kept.append(mark)
                    marked = True
            else:
                kept.append(sentence)
        lines.append(" ".join(kept))
    body = "\n".join(lines)
    if used and not HAS_CONDITION_LINE.search(body):
        if marked:
            body = body.replace(mark, CONDITION_LINE)
        else:
            rows = body.rstrip().split("\n")
            at = len(rows) - 1 if re.match(r"\s*retail\b", rows[-1], re.I) and len(rows) > 1 else len(rows)
            body = "\n".join(rows[:at] + [CONDITION_LINE] + rows[at:])
    body = re.sub(r"[ \t]+\n", "\n", body.replace(mark, "")).replace(" \n", "\n")
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def condition_rule(out: CopyOut, facts: Facts) -> CopyOut:
    """The owner's condition rule on finished copy (after the verifier): both descriptions through
    condition_wording(), and no style tag or hashtag that names wear. The title is left as it is: a title that breaks
    the rule is a lint problem, not something to cut."""
    out.poshmark_description = condition_wording(out.poshmark_description, facts.condition)
    out.depop_description = condition_wording(strip_tag_lines(out.depop_description), facts.condition)
    out.poshmark_style_tags = [t for t in out.poshmark_style_tags if not NEGATIVE_WORDS.search(t)]
    out.depop_hashtags = [t for t in out.depop_hashtags if not NEGATIVE_WORDS.search(t)]
    return clean(out)                                  # re-attaches the hashtag line within Depop's limit


def dump(out: CopyOut) -> dict:
    return yaml.safe_load(out.model_dump_json())
