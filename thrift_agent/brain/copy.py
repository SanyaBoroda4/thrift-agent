"""One call writes both marketplaces' copy from the same facts; code enforces the hard limits."""
from __future__ import annotations

import json
import re

import yaml

from thrift_agent.brain import llm
from thrift_agent.config import style_dir
from thrift_agent.schema import CONDITION_LABEL, CopyOut, Facts

TITLE_MAX, DEPOP_MAX = 80, 1000


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
  Add "New" at the start only for NWT/NWOT. Use the room — short titles don't get found.
  Decimal sizes use a dot (7.5), never a comma. No emojis, no ALL CAPS words except brand styling.
- Description, in this closet's proven shape:
  1) 2–4 short sentences describing what the photos show (type, color, material if known, details).
     Plain and specific; at most one adjective like "chic" or "versatile" — never a string of them.
  2) Then ONE short, plain line in the seller's voice with condition and anything a buyer must know:
     "New, no tags." / "Worn once, light wear on soles as shown." / "Size 38 EU, fits US 7.5-8."
  3) Then the footer if one is given. No keyword stuffing, no emojis.
- Style tags: up to 3 short ones, or none.

DEPOP
- Casual, first-person-seller voice, lowercase is fine. Same facts, fewer words.
- ≤1000 characters INCLUDING a final line of exactly 5 hashtags. Then the footer.

HARD RULES
- Use ONLY the facts given. If a field is null, don't mention it. No guessed fabric, fit,
  measurements, era, "authentic", "smoke-free", or "true to size".
- Color words must match the facts' colors; department words must match the department
  (never "kids" or "men's" for a Women's item) — past listings lost buyers over exactly this.
- Condition wording must match the facts' condition exactly; mention every flaw.
- Never copy wording from the examples — match their shape, not their text."""


def write(facts: Facts, model: str, cfg: dict) -> CopyOut:
    view = facts.model_dump(exclude={"cover_photo", "photo_order", "questions"})
    view["condition_label"] = CONDITION_LABEL[facts.condition]
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


def clean(out: CopyOut) -> CopyOut:
    out.poshmark_title = clamp_title(out.poshmark_title)
    out.poshmark_description = fix_decimal_commas(out.poshmark_description).strip()
    out.poshmark_style_tags = [t.strip() for t in out.poshmark_style_tags if t and t.strip()][:3]
    tags = [re.sub(r"[^\w]", "", t.lower()) for t in out.depop_hashtags]
    out.depop_hashtags = [t for t in tags if t][:5]
    body = fix_decimal_commas(out.depop_description).strip()
    body = re.sub(r"(\s*#\w+)+\s*$", "", body)             # drop any hashtags the model inlined
    tag_line = " ".join(f"#{t}" for t in out.depop_hashtags)
    limit = DEPOP_MAX - len(tag_line) - 2                    # body + blank line + tag line <= DEPOP_MAX
    if len(body) > limit:
        body = _cut_at_word(body, limit - 1).rstrip() + "\u2026"   # one char reserved for the ellipsis
    out.depop_description = f"{body}\n\n{tag_line}" if tag_line else body
    return out


def dump(out: CopyOut) -> dict:
    return yaml.safe_load(out.model_dump_json())
