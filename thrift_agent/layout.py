"""WO54 §1.3: replies typed with the wrong keyboard layout, and brand replies that aren't a brand.

Live 2026-11-09 8:43: the owner answered the 🏷️ question for the Glitter Toe Sneakers with "тщ икфтв" — "no brand" typed
with the keyboard on Russian (ЙЦУКЕН). It was taken literally: the brand became "тщ икфтв", Depop went live titled
"Тщ икфтв Glitter Toe Sneakers…", and Vinted failed three times on it. Now:
- a reply in Cyrillic that reads, key for key on the Latin layout, as one of the bot's commands or a brand we know is
  understood as that ("тщ икфтв" → "no brand", "ыуфыщт" → ...); the reverse too — Latin keys that spell a Cyrillic
  name we know (`normalize`);
- a brand reply that matches no brand we know and isn't a plausible shop name (Latin letters; not a sentence) is "no
  brand", with one check line — it is never typed into a form (`plausible_brand`)."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# The Russian PC layout (ЙЦУКЕН) over the US one (QWERTY), key for key, lower and upper case.
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю.ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
TO_LATIN = str.maketrans(_RU, _EN)
TO_CYRILLIC = str.maketrans({e: r for r, e in zip(_RU, _EN) if e.isalpha()})
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
LATIN = re.compile(r"[A-Za-z]")

# The bot's own words (approve.py): a reply in the other layout that reads as one of these is understood as it.
COMMAND = re.compile(
    r"^(?:no\s*brand|unbranded|brand\b|size\b|price\b|list\b|\$?\d|nwt\b|nwot\b|new\b|like\s*new|good\b|excellent\b|"
    r"condition\b|boys?\b|girls?\b|men\b|women\b|kids?\b|category\b|cover\b|later\b|same\s+item|different\s+item|"
    r"no\s+drop|post\s+now|quiet\s+now|doctor\b|go\s+titles|retry\b|posted\b|shipped\b|ok\b|yes\b|no\b|split\b|merge\b|"
    r"drop\b|skip\b|show\b|done\b)", re.I)                 # WO56: a human step's "show" / "later" / "done"

DATA = Path(__file__).resolve().parent.parent / "data"


@lru_cache(maxsize=1)
def _known_keys() -> frozenset:
    """Every brand name we know (the curated aliases, the big brands, the learned aliases on the Mac) as brands.key."""
    from thrift_agent import brands
    names: set[str] = set()
    for f in ("brand_aliases.json", "big_brands.json", "brand_display.json", "brand_parents.json"):
        try:
            data = json.loads((DATA / f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, str):
                names.add(x)
            elif isinstance(x, dict):
                names.update(k for k in x if isinstance(k, str))
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    try:
        from thrift_agent.config import settings
        p = Path(str(settings().get("paths.brand_aliases") or ""))
        if p.is_file():
            learned = json.loads(p.read_text(encoding="utf-8"))
            stack = [learned]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    names.add(x)
                elif isinstance(x, dict):
                    names.update(k for k in x if isinstance(k, str))
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)
    except Exception:  # noqa: BLE001 - the learned file is a bonus
        pass
    return frozenset(k for k in (brands.key(n) for n in names) if k and len(k) >= 2)


def known_brand(name: str | None) -> bool:
    from thrift_agent import brands
    k = brands.key(name)
    return bool(k) and k in _known_keys()


def to_latin(text: str) -> str:
    return (text or "").translate(TO_LATIN)


def to_cyrillic(text: str) -> str:
    return (text or "").translate(TO_CYRILLIC)


def _reads(text: str) -> bool:
    """Does `text` read as one of the bot's commands, or as a brand we know?"""
    t = (text or "").strip()
    if not t:
        return False
    if COMMAND.match(t):
        return True
    m = re.match(r"^brand\s+(.+)$", t, re.I)
    return known_brand(m.group(1) if m else t)


def normalize(text: str | None) -> tuple[str, bool]:
    """(the reply as she meant it, whether it was re-read from the other layout). Only a reply that is wholly in one
    script and reads as a command or a brand we know in the other is changed; anything else comes back as it was."""
    t = (text or "").strip()
    if not t:
        return t, False
    if CYRILLIC.search(t) and not LATIN.search(t):
        latin = to_latin(t)
        if _reads(latin) and not _reads(t):
            return latin, True
        return t, False
    if LATIN.search(t) and not CYRILLIC.search(t) and not _reads(t):
        cyr = to_cyrillic(t)
        if known_brand(cyr):
            return cyr, True
    return t, False


def plausible_brand(name: str | None) -> bool:
    """A brand reply worth typing into a site's form: a brand we know, or a shop-style name — mostly Latin letters (the
    sites' brand lists are), at least two letters, at most 6 words and 40 characters, not one of the bot's commands.
    "тщ икфтв" (no Latin letter), "???", "12" and "please post it now" are not."""
    n = re.sub(r"\s+", " ", str(name or "")).strip().strip("'\"").strip()
    if not n:
        return False
    if known_brand(n):
        return True
    letters = re.findall(r"[^\W\d_]", n)
    latin = re.findall(r"[A-Za-zÀ-ÿ]", n)
    if len(latin) < 2 or len(latin) < 0.8 * max(1, len(letters)):
        return False
    if len(n) > 40 or len(n.split()) > 6:
        return False
    # the bot's own words are never a brand ("later", "size M", "price 20", "no brand") — a brand that merely starts
    # with one ("New Balance", "Good American", "No Boundaries") is
    return not re.fullmatch(r"(?:later|no\s*brand|unbranded|size\b.*|price\b.*|\$?\d+(?:\.\d+)?|retry|ok|yes|no)",
                            n, re.I)
