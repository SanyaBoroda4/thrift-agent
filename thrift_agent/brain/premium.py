"""Premium details in the listing (WO26, the owner's rule): selling points stated exactly — a premium fiber, the country
of manufacture, a premium line, confirmed vintage, a collaboration, technical features, construction, the original
retail price — but ONLY what brain/labels.py read off a label or what the photos plainly show (facts.premium, every value
with its photos). config/premium.yaml lists the premium fibers, the "Made in" countries worth saying (any other country
is never mentioned), the premium lines and the price multipliers.

The title: Brand → the ONE strongest feature → item → color → US size, at most 80 characters: the least important words
go first, never the brand, the feature or the size. Strongest: a premium fiber from 90% of the main fabric ("100% Silk",
"Cashmere") > a premium line > "Vintage" (with its era) > "Made in Italy" > a collaboration > a premium blend, 50-89%
("Silk Blend"). The description: every confirmed feature in a plain line of its own, before the condition line."""
from __future__ import annotations

import re
from dataclasses import dataclass

from thrift_agent.brain import copy as copywriter, sizes
from thrift_agent.config import load_yaml
from thrift_agent.schema import Ev, Facts, Premium

TITLE_MAX = copywriter.TITLE_MAX
_NEGATIVE = re.compile(r"\b(?:not|no|without|unlined)\b", re.I)
_CONNECTORS = {"with", "and", "&", "in", "+", "-", "|", "for", "of", "the", "a"}


def config() -> dict:
    """config/premium.yaml (a private/premium.yaml replaces it)."""
    return load_yaml("premium.yaml") or {}


def _k(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _seen(ev: Ev) -> str | None:
    """The value, when a photo shows it."""
    return ev.value.strip() if ev.value and ev.value.strip() and ev.photos else None


def _fiber_name(fiber: str, fibers: dict) -> str | None:
    """The title's word for a premium fiber ("merino wool" -> "Merino Wool"), the longest config key first."""
    for key in sorted(fibers, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(key.lower())}(?![a-z])", fiber.lower()):
            return fibers[key]
    return None


def premium_fiber(facts: Facts, cfg: dict) -> tuple[str, int] | None:
    """(the title's word, its share of the main fabric) for the premium fiber with the largest share, or None."""
    p = facts.premium
    best = None
    for f in (p.composition if p else []):
        if f.part == "main" and f.photos and (name := _fiber_name(f.fiber, cfg.get("fibers") or {})):
            if best is None or f.pct > best[1]:
                best = (name, f.pct)
    return best


def made_in(facts: Facts, cfg: dict) -> str | None:
    """The country as the listing says it ("Italy", "USA", "Scotland"), only when config lists it; any other country
    — China, Bangladesh, Vietnam… — is never said."""
    printed = _seen(facts.premium.made_in) if facts.premium else None
    if not printed:
        return None
    printed = re.sub(r"(?i)^\s*made\s+in\s+", "", printed).strip(" .")
    table = cfg.get("made_in") or {}
    return next((said for label, said in table.items() if _k(label) == _k(printed)), None)


def premium_line(facts: Facts, cfg: dict) -> tuple[str, str] | None:
    """(the line as the description names it, as the title says it after the brand) — "J.Crew Collection",
    "Collection" — when the label prints a line config lists, else None."""
    printed = _seen(facts.premium.line) if facts.premium else None
    if not printed:
        return None
    brand = facts.brand.value or ""
    for line in cfg.get("lines") or []:
        if _k(line) in (_k(printed), _k(f"{brand} {printed}")):
            short = line[len(brand):].strip() if brand and _k(line).startswith(_k(brand)) and _k(line) != _k(brand) \
                else line
            return line, short
    return None


def vintage(facts: Facts) -> str | None:
    """"Vintage" — "Vintage 90s" when the era is clear — only with a concrete cue in the photos (WO26)."""
    p = facts.premium
    if not p or not p.vintage.value or not (p.vintage.photos or any(c.photos for c in p.vintage_cues)):
        return None
    era = re.search(r"(?:19|20)?(\d0)'?s\b", p.vintage.value)
    return f"Vintage {era[1]}s" if era else "Vintage"


def collab(facts: Facts) -> str | None:
    return _seen(facts.premium.collab) if facts.premium else None


def _shown(features) -> list[str]:
    """Features with a photo, never a negative one ("unlined")."""
    return [f.text.strip() for f in features if f.photos and f.text.strip() and not _NEGATIVE.search(f.text)]


@dataclass(frozen=True)
class Strongest:
    kind: str                    # fiber | line | vintage | made_in | collab | blend
    phrase: str                  # what the title says, right after the brand: "100% Silk", "Collection"
    weaker: tuple[str, ...]      # the same thing worded weaker elsewhere in the title, taken out: "Silk"


def strongest(facts: Facts, cfg: dict) -> Strongest | None:
    """The ONE feature the title states (WO26 order); None when no label or photo shows one."""
    fiber = premium_fiber(facts, cfg)
    if fiber and fiber[1] >= 90:
        return Strongest("fiber", f"100% {fiber[0]}" if fiber[1] == 100 else fiber[0], (fiber[0],))
    if line := premium_line(facts, cfg):
        return Strongest("line", line[1], ())
    if old := vintage(facts):
        return Strongest("vintage", old, ("Vintage",))
    if country := made_in(facts, cfg):
        return Strongest("made_in", f"Made in {country}", ())
    if together := collab(facts):
        return Strongest("collab", together, ())
    if fiber and fiber[1] >= 50:
        return Strongest("blend", f"{fiber[0]} Blend", (fiber[0],))
    return None


def _drop(words: list[str], phrase: str) -> list[str]:
    """`words` without each occurrence of `phrase` (compared without case or punctuation)."""
    pw = [_k(w) for w in phrase.split()]
    out, i = [], 0
    while i < len(words):
        if pw and [_k(w) for w in words[i:i + len(pw)]] == pw:
            i += len(pw)
            continue
        out.append(words[i])
        i += 1
    return out


def fit_title(title: str, keep: list[str]) -> str:
    """At most 80 characters (WO26): words go one at a time, the ones just before the size first and then backwards —
    a detail, a connector — but never a word of `keep` (the brand, the feature, the US size, the set phrase, the
    item's noun, its colours). A connector left hanging before the size or at the end goes too."""
    if len(title) <= TITLE_MAX:
        return title
    words = title.split()
    keys = [_k(w) for w in words]
    protected: set[int] = set()
    for phrase in filter(None, keep):
        pw = [_k(w) for w in phrase.split() if _k(w)]
        for i in range(len(keys) - len(pw) + 1):
            if pw and keys[i:i + len(pw)] == pw:
                protected.update(range(i, i + len(pw)))
    alive = list(range(len(words)))
    for i in reversed(range(len(words))):
        if len(" ".join(words[j] for j in alive)) <= TITLE_MAX:
            break
        if i not in protected:
            alive.remove(i)
    hanging = True
    while hanging:                                          # "… Pants with size M": the hanging "with" goes
        hanging = False
        for n, i in enumerate(alive):
            nxt = alive[n + 1] if n + 1 < len(alive) else None
            if words[i].lower() in _CONNECTORS and i not in protected and (nxt is None or nxt > i + 1):
                alive.remove(i)
                hanging = True
                break
    out = " ".join(words[j] for j in alive)
    return out if len(out) <= TITLE_MAX else copywriter.clamp_title(out)


def title_with_feature(title: str, facts: Facts, cfg: dict) -> str:
    """The strongest feature right after the brand ("Vince 100% Silk Black Slip Dress size S"; no brand: first), its
    weaker wording taken out of the rest ("White Silk Pants" -> "100% Silk White Pants"), then fitted to 80. Unchanged
    when no label or photo shows a feature. Run it twice, same title."""
    best = strongest(facts, cfg)
    if best is None:
        return title
    brand = (facts.brand.value or "").strip()
    words = title.split()
    head: list[str] = []
    if brand and _k(" ".join(words[:len(brand.split())])) == _k(brand):
        head, words = words[:len(brand.split())], words[len(brand.split()):]
    for phrase in (best.phrase, *best.weaker):
        words = _drop(words, phrase)
    new = " ".join([*head, *best.phrase.split(), *words])
    nouns = (facts.item_type or "").split()[-2:]                  # "flared pants set": pants, set
    colours = [*(facts.colors or []), *((facts.color_name or "").split())]
    keep = [brand, best.phrase, sizes.title_size(facts) or "", f"{facts.set_pieces}-Piece Set" if facts.set_pieces
            else "", *(form for noun in nouns for form in (noun, noun.rstrip("s"), noun + "s")), *colours]
    return fit_title(new, keep)


def feature_lines(facts: Facts, cfg: dict) -> list[str]:
    """Every confirmed feature, plainly, one line each (WO26): "Material: 100% silk." "Made in Italy." "J.Crew
    Collection." "Vintage 90s." "Gore-Tex, waterproof." "Fully lined." Never negative, never a country config doesn't
    list, nothing without its photo. The retail price is copy.ensure_retail_line's ("Original retail $128.")."""
    p = facts.premium
    if not p:
        return []
    lines = []
    main = [f for f in p.composition if f.part == "main" and f.photos]
    if main:
        lines.append("Material: " + ", ".join(f"{f.pct}% {f.fiber.strip().lower()}" for f in main) + ".")
    lining = [f for f in p.composition if f.part == "lining" and f.photos and _fiber_name(f.fiber, cfg.get("fibers")
                                                                                           or {})]
    if lining:
        lines.append("Lining: " + ", ".join(f"{f.pct}% {f.fiber.strip().lower()}" for f in lining) + ".")
    if country := made_in(facts, cfg):
        lines.append(f"Made in {country}.")
    if line := premium_line(facts, cfg):
        lines.append(f"{line[0]}.")
    if old := vintage(facts):
        lines.append(f"{old}.")
    if together := collab(facts):
        lines.append(f"{together.rstrip('.')}.")
    for group in (_shown(p.technical), _shown(p.construction)):
        if group:
            text = ", ".join(group)
            lines.append(f"{text[0].upper()}{text[1:]}.")
    return lines


_CONDITION = re.compile(r"^\s*(?:new with tags|new without tags|new in box)\.?\s*$", re.I)


def ensure_feature_lines(description: str, facts: Facts, cfg: dict) -> str:
    """The feature lines go in before the condition line (else at the end); a line the description already has is not
    repeated. Run it twice, same text."""
    rows = description.rstrip().split("\n")
    have = {r.strip().lower() for r in rows}
    add = [line for line in feature_lines(facts, cfg) if line.lower() not in have]
    if not add:
        return description
    at = next((i for i, r in enumerate(rows) if copywriter.HAS_CONDITION_LINE.search(r) or _CONDITION.match(r)),
              len(rows))
    return "\n".join(rows[:at] + add + rows[at:])


def price_factor(facts: Facts, cfg: dict) -> tuple[float, str | None]:
    """The suggested price's premium multiplier, applied once: the largest that applies (never stacked) — a premium fiber
    from 90%, a premium blend (50-89%), a premium line, confirmed vintage. (1.0, None) when none does."""
    m = cfg.get("multipliers") or {}
    options = []
    if fiber := premium_fiber(facts, cfg):
        if fiber[1] >= 90:
            options.append((float(m.get("fiber", 1)), f"{fiber[1]}% {fiber[0].lower()}"))
        elif fiber[1] >= 50:
            options.append((float(m.get("blend", 1)), f"{fiber[0].lower()} blend"))
    if line := premium_line(facts, cfg):
        options.append((float(m.get("line", 1)), line[0]))
    if vintage(facts):
        options.append((float(m.get("vintage", 1)), "vintage"))
    best = max(options, default=(1.0, None), key=lambda o: o[0])
    return best if best[0] > 1 else (1.0, None)


def merge(facts: Facts, read: Premium | None, n_photos: int) -> Facts:
    """The facts with the label read (WO26): premium set (only values a photo of this item shows); the composition of
    the main fabric is the material's evidence (lint's materials rule); a price on a hang tag is the retail price when no
    retailer screenshot gave one."""
    if read is None:
        return facts

    def ok(photos: list[int]) -> list[int]:
        return sorted({i for i in photos if 0 <= i < n_photos})

    def ev(e: Ev) -> Ev:
        return e.model_copy(update={"photos": ok(e.photos)}) if e.value and ok(e.photos) else Ev()

    clean = Premium(
        composition=[f.model_copy(update={"photos": ok(f.photos)}) for f in read.composition if ok(f.photos)],
        made_in=ev(read.made_in), line=ev(read.line), vintage=ev(read.vintage),
        vintage_cues=[c.model_copy(update={"photos": ok(c.photos)}) for c in read.vintage_cues if ok(c.photos)],
        collab=ev(read.collab),
        technical=[t.model_copy(update={"photos": ok(t.photos)}) for t in read.technical if ok(t.photos)],
        construction=[c.model_copy(update={"photos": ok(c.photos)}) for c in read.construction if ok(c.photos)],
        retail_price=ev(read.retail_price))
    update: dict = {"premium": clean}
    if main := [f for f in clean.composition if f.part == "main"]:
        update["material"] = Ev(value=", ".join(f"{f.pct}% {f.fiber.strip().lower()}" for f in main),
                                photos=sorted({i for f in main for i in f.photos}), source="photo", confidence=0.95)
    if clean.retail_price.value and not facts.retail_price.value:
        update["retail_price"] = clean.retail_price
    return facts.model_copy(update=update)


def summary(facts: Facts, cfg: dict) -> str:
    """What the labels gave, for `thrift recover` / `reprocess` lines and the report: "100% silk; Made in Italy"."""
    best = strongest(facts, cfg)
    lines = [line.rstrip(".") for line in feature_lines(facts, cfg)]
    if facts.premium and _seen(facts.premium.retail_price):
        lines.append(f"hang tag ${facts.premium.retail_price.value}")
    return ("title: " + best.phrase + "; " if best else "") + ("; ".join(lines) or "nothing premium on the labels")
