"""Brand names as each site's brand list spells them (WO27, WO34; the owner's rule of 2026-10-11).

A brand is picked by its EXACT name only — case, spaces, dots, hyphens and apostrophes ignored, "&" = "and" ("J.Crew"
is "J. Crew", "Solid & Striped" is "Solid and Striped") — or by one of the two parts of a name like "MNG (Mango)"
(candidates()). Nothing looser: never a first or longest word, a shorter "clean" name, a similar one ("Solid & Striped"
could land on a brand called "Solid", "Seven Wonders" on "Seven"). None → Brand left empty on Poshmark (optional),
Depop's "Other", Vinted's "List without brand"; the real brand stays in the title and description. What is typed into
a site's search may be another SPELLING of the same name (spellings(): "J.Crew" → "J. Crew"). Every name it had to
resolve is learned (Aliases.learn) into data/brand_aliases.yaml, a file the Mac writes and git ignores (a tracked file
written at runtime would block the deploy's pull), and the pipeline spells the brand that way from then on."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml

# Resolved live (2026-10-04): Poshmark lists "J. Crew" and "J. Crew Factory", never "J.Crew".
SEED = {"J.Crew": "J. Crew"}
HEADER = ("# Brand names as Poshmark's brand list spells them: our reading -> Poshmark's (WO27). The poster adds every\n"
          "# name it had to resolve; edit freely. Written at runtime on the Mac, ignored by git.\n")


def key(name: str | None) -> str:
    """Comparison form: case, spaces, dots, hyphens and apostrophes ignored, "&" = "and"."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s.replace("&", " and "))


def exact(ours: str, options: list[str]) -> str | None:
    """The option that IS our brand — the same key (case, spaces, dots, hyphens, apostrophes aside, "&" = "and") —
    else None. Never another line ("J. Crew Factory" is not J.Crew), never a shorter or similar name."""
    return next((o for o in dict.fromkeys(options) if o and key(o) and key(o) == key(ours)), None)


def pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """(the suggestion to pick, the guess to report) for our brand among Poshmark's suggestions — the exact name only
    (the owner's rule, 2026-10-11); (None, why) otherwise: Brand is optional, so it is left empty. A different spelling
    is reported, the same one isn't."""
    if (choice := exact(ours, options)) is not None:
        return choice, None if choice == ours else f"brand set to '{choice}' (from '{ours}')"
    offered = f" (it offers: {', '.join(list(dict.fromkeys(o for o in options if o))[:6])})" if options else ""
    return None, f"brand left empty: Poshmark's list has no '{ours}'{offered}"


def strict_pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """Depop, Vinted (WO30's Playwright posters): the exact name only, as pick(). (None, why) otherwise: the brand is
    left empty and the ops chat's check says so."""
    if (choice := exact(ours, options)) is not None:
        return choice, None if choice == ours else f"brand set to '{choice}' (from '{ours}')"
    return None, f"brand left empty (no '{ours}' in its brand list)"


def spellings(name: str) -> list[str]:
    """What may be typed into a site's brand search for `name`: the name as written, then other spellings of the SAME
    name — "J.Crew" / "J. Crew", "&" / "and" — never another word of it (the owner, 2026-10-11: no first-word search)."""
    out = [name, re.sub(r"\.\s+", ".", name), re.sub(r"\.(?=[A-Za-z])", ". ", name),
           re.sub(r"\s*&\s*", " and ", name)]
    return list(dict.fromkeys(v.strip() for v in out if v and v.strip()))


def parts(name: str | None) -> list[str]:
    """The two parts of a two-part name ("MNG (Mango)" → MNG, Mango; split at brackets and slashes, never at "&": H&M);
    [] for a one-part name."""
    found = [p.strip(" -") for p in re.split(r"[()\[\]/]", name or "") if p.strip(" -")]
    return found if len(found) > 1 else []


def spelling_of(name: str | None, other: str | None) -> bool:
    """`other` is the name itself or one of its parts, spelled another way (a learned alias may only be that)."""
    return bool(name and other) and key(other) in {key(n) for n in [name, *parts(name)]}


def candidates(name: str | None, aliases: "Aliases | None" = None) -> list[str]:
    """The names to try for a brand on every site, in order (WO34, the owner: "MNG (Mango)" left the brand empty on
    Poshmark and Depop): the full name and its known spelling, then each part of a two-part name ("MNG", "Mango" —
    split at brackets and slashes, never at "&": H&M), each part's known spelling. One entry per name (case ignored)."""
    if not name or not name.strip():
        return []
    out: list[str] = []
    for n in [name.strip(), *parts(name)]:
        spelled = aliases.spell(n) if aliases is not None else None
        for v in (n, spelled if spelling_of(name, spelled) else None):   # a learned spelling of these names only
            if v and key(v) and key(v) not in {key(o) for o in out}:
                out.append(v)
    return out


def pick_any(names: list[str], options: list[str]) -> tuple[str | None, str | None]:
    """The first of our names (candidates()) a site's list has, exactly (case and punctuation aside); else (None, why)
    with what the list offered. (choice, guess) as pick(): a different spelling is reported."""
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
        """Remember that Poshmark spells `ours` as `theirs` (True when it was new) — only another spelling of the name
        or of one of its parts (spelling_of): a different brand is never learned (the owner's rule, 2026-10-11)."""
        if not ours or not theirs or ours == theirs or self.path is None or self.table().get(key(ours)) == theirs:
            return False
        if not spelling_of(ours, theirs):
            return False
        data = self._file()
        data[ours] = theirs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(dict(sorted(data.items(), key=lambda kv: kv[0].lower())), allow_unicode=True,
                              sort_keys=False, default_flow_style=False)
        self.path.write_text(HEADER + body, encoding="utf-8")
        return True
