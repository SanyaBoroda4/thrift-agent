"""Brand names as each site's brand list has them (WO27, WO34; WO36 — the owner's rules of 2026-10-11 and 2026-10-12).

The names tried, in order (candidates()): the brand as shops list it (the copy step's `brand`: "Alo Yoga" for an "alo"
label, "Mango" for "MNG"), the label's own text (`brand_label`), the alias file's name for either (alo → Alo Yoga,
MNG → Mango, …), then the two parts of a name like "MNG (Mango)".

On each site (choose()):
1. a name of ours EXACTLY — case, spaces, punctuation, "&" vs "and" and accents aside (key());
2. else the site's own suggestion that is a whole name of ours followed by more words ("alo" → "Alo Yoga"; whole
   words: "Alo" is never "ALOHAS") — taken only when exactly one option fits;
3. else none: Depop's "Other", no brand on Vinted and Poshmark — and the note lists what the site offered, so the alias
   can be added.
Never the other way round: a site's name that is only the START of ours ("Solid" for "Solid & Striped", "Seven" for
"Seven Wonders") is never taken, nor a similar one. What is typed into a site's search is a name of ours or another
spelling of it (spellings(): "J.Crew" → "J. Crew") — never a word of it.

The alias file maps a reading — a label's text, a short form, a spelling — to the name shops list:
data/brand_aliases.json (tracked: the curated start, alo → Alo Yoga, MNG → Mango, …) under the corrections learned on the
Mac (paths.brand_aliases, data/brand_aliases.learned.json, ignored by git — a tracked file written at runtime would block
the deploy's pull): the owner's card reply "brand Alo Yoga" (the label's text → her name), and a site's own spelling
of the same name ("J.Crew" → "J. Crew")."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CURATED = ROOT / "data" / "brand_aliases.json"     # tracked: the well-known short forms (WO36)
# Resolved live (2026-10-04): Poshmark lists "J. Crew" and "J. Crew Factory", never "J.Crew".
SEED = {"J.Crew": "J. Crew"}


# A company suffix is one word however a site writes it: "Thursday Boot Co" = "Thursday Boot Company" (live
# 2026-10-20: Poshmark's Brand left empty and Depop's "Other" for a brand both list as "Thursday Boot Company").
SUFFIX = {"company": "co", "corporation": "corp", "incorporated": "inc", "limited": "ltd"}


def key(name: str | None) -> str:
    """Comparison form: case, spaces, punctuation and accents ignored, "&" = "and", "Company" = "Co"."""
    return "".join(words(name))


def words(name: str | None) -> list[str]:
    """A name's words as key() reads them ("Levi's" → levis, "H&M" → h and m, "Boot Company" → boot co)."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return [SUFFIX.get(w, w) for w in re.findall(r"[a-z0-9]+", re.sub(r"['’`]", "", s.replace("&", " and ")))]


def exact(ours: str, options: list[str]) -> str | None:
    """The option that IS our brand (the same key), else None."""
    return next((o for o in dict.fromkeys(options) if o and key(o) and key(o) == key(ours)), None)


def extends(ours: str | None, option: str | None) -> bool:
    """`option` is the whole of `ours` followed by more words ("Alo" → "Alo Yoga") — whole words only ("Alo" never
    "ALOHAS"); never the other way round ("Solid & Striped" is not extended by "Solid")."""
    o, x = words(ours), words(option)
    return bool(o) and len(x) > len(o) and x[:len(o)] == o


def choose(names: list[str], options: list[str]) -> tuple[str | None, str | None, str | None]:
    """(the option to pick, how — "exact" | "extends", the name of ours it came from), or (None, None, None): a name
    of ours exactly, in order; else the ONE option that is a name of ours followed by more words."""
    opts = [o for o in dict.fromkeys(options) if o and key(o)]
    for n in names:
        if (o := exact(n, opts)) is not None:
            return o, "exact", n
    for n in names:
        longer = {key(o): o for o in opts if extends(n, o)}
        if len(longer) == 1:
            return next(iter(longer.values())), "extends", n
    return None, None, None


def offered(options: list[str], limit: int = 6) -> str:
    """" (it offers: A, B, …)" — the site's top suggestions, for the note that lets the alias be added."""
    shown = list(dict.fromkeys(o for o in options if o and key(o) != "other"))[:limit]
    return f" (it offers: {', '.join(shown)})" if shown else ""


def pick_any(names: list[str], options: list[str], site: str = "Poshmark") -> tuple[str | None, str | None]:
    """(the option to pick, the note to report) for our names (candidates()) among a site's options; (None, why — with
    what it offered) when none fits: Brand is then left empty (optional on Poshmark). A different spelling, or a
    longer name, is reported; the same name isn't."""
    choice, how, came = choose(names, options)
    if choice is None:
        return None, f"brand left empty: {site}'s list has no '{names[0] if names else ''}'{offered(options)}"
    first = names[0]
    if how == "exact" and choice.lower() == first.lower():
        return choice, None
    return choice, f"brand set to '{choice}' (from '{came if how == 'extends' else first}')"


def pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """pick_any for one name, on Poshmark's form."""
    return pick_any([ours], options)


def strict_pick(ours: str, options: list[str]) -> tuple[str | None, str | None]:
    """Depop, Vinted (WO30's Playwright posters): the same rules, their own note."""
    choice, how, came = choose([ours], options)
    if choice is None:
        return None, f"brand left empty (no '{ours}' in its brand list){offered(options)}"
    return choice, None if choice == ours else f"brand set to '{choice}' (from '{ours}')"


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
    """`other` is the name itself or one of its parts, spelled another way."""
    return bool(name and other) and key(other) in {key(n) for n in [name, *parts(name)]}


def candidates(brand: str | None, label: str | None = None, aliases: "Aliases | None" = None) -> list[str]:
    """The names to try on every site, in order (WO36): the brand as shops list it, the label's text, the alias file's
    name for each, then the two parts of a two-part name ("MNG (Mango)" → MNG, Mango) and their aliases. One entry per
    name (key())."""
    out: list[str] = []

    def add(n: str | None) -> None:
        n = (n or "").strip()
        if n and key(n) and key(n) not in {key(o) for o in out}:
            out.append(n)
    tops = [n for n in (brand, label) if n and n.strip()]
    for n in tops:
        add(n)
    for n in tops:
        add(aliases.alias(n) if aliases is not None else None)
    for n in tops:
        for p in parts(n):
            add(p)
            add(aliases.alias(p) if aliases is not None else None)
    return out


def for_settings(s) -> "Aliases":
    """The aliases of these settings' learned file (paths.brand_aliases), over the curated one."""
    return Aliases(s.path("brand_aliases") if s.get("paths.brand_aliases") else None)


def _load(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("aliases"), dict):
        data = data["aliases"]
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v and not str(k).startswith("_")}


class Aliases:
    """A reading → the name shops list: SEED, the curated file, a legacy data/brand_aliases.yaml, then the learned file
    (the later wins)."""

    def __init__(self, path: Path | None, curated: Path | None = CURATED):
        self.path, self.curated = path, curated

    def table(self) -> dict[str, str]:
        legacy = self.path.with_name("brand_aliases.yaml") if self.path is not None else None
        merged = {**SEED, **_load(self.curated), **_load(legacy), **_load(self.path)}
        return {key(k): v for k, v in merged.items() if key(k)}

    def alias(self, name: str | None) -> str | None:
        """The name shops list for this reading, or None when the files have none."""
        return self.table().get(key(name)) if name else None

    def spell(self, brand: str | None) -> str | None:
        """The name shops list for our brand, or the brand as it is."""
        return (self.alias(brand) or brand) if brand else brand

    def learn(self, ours: str, theirs: str, owner: bool = False) -> bool:
        """Remember `ours` → `theirs` (True when it was new). The owner's correction ("brand Alo Yoga" on a card: the
        label's text → her name) is always kept; anything else only when it is another spelling of the same name."""
        if not ours or not theirs or ours == theirs or self.path is None or self.table().get(key(ours)) == theirs:
            return False
        if not owner and not spelling_of(ours, theirs):
            return False
        data = _load(self.path)
        data[ours] = theirs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(dict(sorted(data.items(), key=lambda kv: kv[0].lower())), indent=1,
                                        ensure_ascii=False) + "\n", encoding="utf-8")
        return True
