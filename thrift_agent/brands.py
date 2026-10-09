"""Brand names as Poshmark's brand list spells them (WO27).

The poster never stops to ask about a brand: it types ours, reads Poshmark's suggestions and picks (pick()): the same
name once case, spaces, dots, hyphens and apostrophes are ignored and "&" is "and" ("J.Crew" is "J. Crew"); else our
name without a qualifier we don't have (never "J. Crew Factory" for J.Crew); else the longest name all of whose words
are ours ("Zara" for "Zara Basic"); else the closest by similarity; else none —
Brand is optional on the form, so it is left empty. Every name it had to resolve is learned (Aliases.learn) into
data/brand_aliases.yaml, a file the Mac writes and git ignores (a tracked file written at runtime would block the
deploy's pull), and the pipeline spells the brand that way from then on, so it is exact the next time."""
from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

import yaml

# Resolved live (2026-10-04): Poshmark lists "J. Crew" and "J. Crew Factory", never "J.Crew".
SEED = {"J.Crew": "J. Crew"}
# Words that make a brand another line of it: never picked unless our brand has the word too.
QUALIFIERS = frozenset({
    "factory", "outlet", "kids", "kid", "baby", "babies", "home", "collection", "sport", "sports", "junior", "juniors",
    "girls", "boys", "men", "mens", "women", "womens", "petite", "plus", "maternity", "studio", "essentials", "basics",
    "active", "golf", "beauty", "swim", "intimates", "lingerie", "accessories", "shoes", "vintage",
})
SIMILAR = 0.85          # difflib ratio of the two keys: "Mistguided" ~ "Missguided" 0.9; "Gap" ~ "Gaia" 0.57
HEADER = ("# Brand names as Poshmark's brand list spells them: our reading -> Poshmark's (WO27). The poster adds every\n"
          "# name it had to resolve; edit freely. Written at runtime on the Mac, ignored by git.\n")


def key(name: str | None) -> str:
    """Comparison form: case, spaces, dots, hyphens and apostrophes ignored, "&" = "and"."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s.replace("&", " and "))


def _words(name: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s.replace("&", " and ").replace("'", ""))


def pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """(the suggestion to pick, the guess to report) for our brand among Poshmark's suggestions; (None, why) when none
    is close: the brand is left empty. A different spelling is reported, the exact one isn't."""
    mine = set(_words(ours))
    usable = [o for o in dict.fromkeys(options) if o and not (set(_words(o)) - mine) & QUALIFIERS]

    def reported(choice: str) -> tuple[str, str | None]:
        return choice, None if choice == ours else f"brand set to '{choice}' (from '{ours}')"

    if same := [o for o in usable if key(o) == key(ours)]:
        return reported(same[0])
    base = key(" ".join(w for w in _words(ours) if w not in QUALIFIERS))
    if base and (plain := [o for o in usable if key(o) == base]):
        return reported(plain[0])
    # Ours with a line word added ("Zara Basic", "Free People Movement"): the longest name all of whose words are ours.
    if within := sorted((o for o in usable if _words(o) and set(_words(o)) < mine), key=lambda o: -len(_words(o))):
        return reported(within[0])
    scored = sorted(((difflib.SequenceMatcher(None, key(ours), key(o)).ratio(), o) for o in usable), reverse=True)
    if scored and scored[0][0] >= SIMILAR:
        return reported(scored[0][1])
    offered = f" (it offers: {', '.join(options[:6])})" if options else ""
    return None, f"brand left empty: Poshmark's list has no '{ours}'{offered}"


def strict_pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """WO30 (Depop, Vinted): only the same name normalised, or the shorter clean form without a qualifier we don't have
    ("J. Crew" for "J.Crew") — nothing looser. (None, why) otherwise: the brand is left empty and the "Posted ✓" check
    says so."""
    mine = set(_words(ours))
    usable = [o for o in dict.fromkeys(options) if o and not (set(_words(o)) - mine) & QUALIFIERS]
    if same := [o for o in usable if key(o) == key(ours)]:
        return same[0], None if same[0] == ours else f"brand set to '{same[0]}' (from '{ours}')"
    base = key(" ".join(w for w in _words(ours) if w not in QUALIFIERS))
    if base and (plain := [o for o in usable if key(o) == base]):
        return plain[0], f"brand set to '{plain[0]}' (from '{ours}')"
    return None, f"brand left empty (no '{ours}' in its brand list)"


def candidates(name: str | None, aliases: "Aliases | None" = None) -> list[str]:
    """The names to try for a brand on every site, in order (WO34, the owner: "MNG (Mango)" left the brand empty on
    Poshmark and Depop): the full name and its known spelling, then each part of a two-part name ("MNG", "Mango" —
    split at brackets and slashes, never at "&": H&M), each part's known spelling. One entry per name (case ignored)."""
    if not name or not name.strip():
        return []
    parts = [p.strip(" -") for p in re.split(r"[()\[\]/]", name) if p.strip(" -")]
    out: list[str] = []
    for n in [name.strip(), *(parts if len(parts) > 1 else [])]:
        for v in (n, aliases.spell(n) if aliases is not None else None):
            if v and key(v) and key(v) not in {key(o) for o in out}:
                out.append(v)
    return out


def pick_any(names: list[str], options: list[str]) -> tuple[str | None, str | None]:
    """The first of our names (candidates()) a site's list has, case and punctuation aside — then, for the full name
    only, pick()'s looser rules (Poshmark). (choice, guess) as pick(): a different spelling is reported."""
    for n in names:
        if same := [o for o in dict.fromkeys(options) if o and key(o) == key(n)]:
            return same[0], None if same[0].lower() == names[0].lower() else f"brand set to '{same[0]}' (from '{names[0]}')"
    return pick(names[0], options) if names else (None, None)


def for_settings(s) -> "Aliases":
    """The aliases of these settings' file (paths.brand_aliases), or the seed alone when none is set."""
    return Aliases(s.path("brand_aliases") if s.get("paths.brand_aliases") else None)


class Aliases:
    """Our brand -> Poshmark's spelling: SEED plus the file the poster writes (the file wins)."""

    def __init__(self, path: Path | None):
        self.path = path

    def _file(self) -> dict[str, str]:
        if self.path is None or not self.path.is_file():
            return {}
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return {str(k): str(v) for k, v in data.items() if k and v} if isinstance(data, dict) else {}

    def table(self) -> dict[str, str]:
        return {key(k): v for k, v in {**SEED, **self._file()}.items()}

    def spell(self, brand: str | None) -> str | None:
        """Poshmark's spelling of our brand, or the brand as it is."""
        return self.table().get(key(brand), brand) if brand else brand

    def learn(self, ours: str, theirs: str) -> bool:
        """Remember that Poshmark spells `ours` as `theirs` (True when it was new)."""
        if not ours or not theirs or ours == theirs or self.path is None or self.table().get(key(ours)) == theirs:
            return False
        data = self._file()
        data[ours] = theirs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(dict(sorted(data.items(), key=lambda kv: kv[0].lower())), allow_unicode=True,
                              sort_keys=False, default_flow_style=False)
        self.path.write_text(HEADER + body, encoding="utf-8")
        return True
