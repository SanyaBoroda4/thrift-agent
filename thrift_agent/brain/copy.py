"""One call writes both marketplaces' copy from the same facts; code enforces the hard limits."""
from __future__ import annotations

import json
import re

import yaml

from thrift_agent.brain import llm
from thrift_agent.brain.sizes import size_label
from thrift_agent.config import style_dir
from thrift_agent.schema import CONDITION_LABEL, CopyOut, Ev, Facts, VerifyOut

TITLE_MAX, DEPOP_MAX = 80, 1000
TEXT_FIELDS = ("poshmark_title", "poshmark_description", "depop_description")
VIEW_EXCLUDE = {"cover_photo", "photo_order", "questions"}
KEEP_EMPTY = {"flaws"}          # an empty flaws list tells the writer there is nothing to disclose
TAG_LINE = re.compile(r"(?m)^[ \t]*(#\w+[ \t]*)+\r?$")   # a line that is nothing but hashtags
TRAILING_TAGS = re.compile(r"(\s*#\w+)+\s*$")           # hashtags tacked onto the end of the last sentence


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
- Kids: the size always carries its system in the title and the description, e.g. "EU 24 / US Toddler 7.5"
  (C sizes = Toddler / Little Kid, Y = Big Kid); never a bare "size 7.5" for kids. The facts give the exact
  string as size_label — use it verbatim.
- Description, in this closet's proven shape:
  1) 2–4 short sentences describing what the photos show (type, color, material if known, details).
     Plain and specific; at most one adjective like "chic" or "versatile" — never a string of them.
  2) Then ONE short, plain line in the seller's voice with condition and anything a buyer must know:
     "New, no tags." / "Worn once, light wear on soles as shown." / "Size 38 EU, fits US 7.5-8."
  3) If retail_price is known, the description ends with "Retail $<price>." as its own last line
     (after the condition line).
  4) Then the footer if one is given. No keyword stuffing, no emojis.
- Style tags: up to 3 short ones, or none.

DEPOP
- Casual, first-person-seller voice, lowercase is fine. Same facts, fewer words.
- ≤1000 characters INCLUDING a final line of exactly 5 hashtags. Then the footer.

HARD RULES
- Use ONLY the facts given. If a field is null, don't mention it. No guessed fabric, fit,
  measurements, era, "authentic", "smoke-free", or "true to size". A material word (leather, suede, silk...)
  only when `material` states it — an item_type or feature wording is not evidence.
- Color words must match the facts' colors; department words must match the department
  (never "kids" or "men's" for a Women's item) — past listings lost buyers over exactly this.
- Condition wording must match the facts' condition exactly; mention every flaw.
- Never copy wording from the examples — match their shape, not their text."""


def facts_view(facts: Facts) -> dict:
    """The facts as the copywriter sees them. Null facts are omitted (invariant 1): an Ev with no value, a None
    scalar or an empty list is simply absent, so there is nothing null for the model to restate."""
    view: dict = {}
    for name, val in facts.model_dump(exclude=VIEW_EXCLUDE).items():
        if isinstance(getattr(facts, name), Ev):
            if val["value"] is None:
                continue
        elif val is None or (val == [] and name not in KEEP_EMPTY):
            continue
        view[name] = val
    if (label := size_label(facts)) is not None:      # "EU 24 / US Toddler 7.5" for a kids shoe, size_us otherwise
        view["size_label"] = label
    view["condition_label"] = CONDITION_LABEL[facts.condition]
    return view


def write(facts: Facts, model: str, cfg: dict) -> CopyOut:
    view = facts_view(facts)
    content = [llm.text(
        "STYLE EXAMPLES\n" + style_examples() +
        "\n\nFACTS\n" + json.dumps(view, indent=1) +
        f"\n\nPOSHMARK FOOTER: {cfg['poshmark_footer']}\nDEPOP FOOTER: {cfg['depop_footer']}"
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


def dump(out: CopyOut) -> dict:
    return yaml.safe_load(out.model_dump_json())
