"""WO50: shop-window titles that every site accepts — the owner's rule (2026-10-24): "imagine a shop window downtown:
people walking past read the label". Short, natural, nice to read; no shouting capitals, no codes, no SKU-like strings,
no odd letters; never dropping what matters (brand, what it is, the one feature that sells it, colour, size).

ONE checker (`check`) and ONE deterministic fixer (`fix`), both reading each site's rules from data/site_rules.json, run
on every title before any site sees it: `shop_window` at the end of the copy's title (premium.title_with_feature),
`for_site` when Depop's and Vinted's fields are mapped (Vinted's title, Depop's first line) and again at the poster.
A title the fixer can't make pass gets one model rewrite (`rewrite`, from the pipeline); still failing, it never
reaches a site (`blocked`). The live titles are reviewed once (`review`): one private message, old → new, and the
owner's reply "go titles" applies them through each site's edit page (edits kind "text", `apply_review`).

Live 2026-10-24: Vinted refused "BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted Strap" — "Title contains too many
capital letters, try using lowercase letters" (38% capitals; every title it took had 29% or less)."""
from __future__ import annotations

import html
import json
import re
import unicodedata
from functools import lru_cache

from thrift_agent.config import ROOT

RULES_FILE = ROOT / "data" / "site_rules.json"
DISPLAY_FILE = ROOT / "data" / "brand_display.json"
NWT_TAIL = ", New with Tags"                       # the owner's example: "BCBG Max Azria … Shoulder Bag, New with Tags"
SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with", "&", "size"}


@lru_cache(maxsize=1)
def rules() -> dict:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _display() -> dict[str, str]:
    raw = json.loads(DISPLAY_FILE.read_text(encoding="utf-8"))
    return {_key(k): v for k, v in raw.items() if not k.startswith("_")}


def _key(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and"))


def _acronyms() -> set[str]:
    return {a.upper() for a in rules()["title"]["acronyms"]}


def site_rule(site: str | None, name: str, default=None):
    if site and name in (rules()["sites"].get(site) or {}):
        return rules()["sites"][site][name]
    return default


def title_max(site: str | None = None) -> int:
    """The site's limit; with no site, the strictest of the three (one title serves all)."""
    if site:
        return int(site_rule(site, "title_max", 80))
    return min(int(v.get("title_max", 80)) for v in rules()["sites"].values())


def caps_share(title: str) -> float:
    letters = [c for c in title if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


# ---------------------------------------------------------------- the brand as a shop sign writes it

def display_brand(brand: str | None) -> str:
    """"BCBGMAXAZRIA" → "BCBG Max Azria", "FARM Rio" → "Farm Rio", "MNG (Mango)" → "Mango"; UGG, H&M, DKNY stay; a
    brand already in normal case stays as it is."""
    name = re.sub(r"\s+", " ", str(brand or "")).strip()
    if not name:
        return ""
    if (shown := _display().get(_key(name))) is not None:
        return shown
    return " ".join(_soften(w, strict=0) for w in name.split())


def _soften(word: str, strict: int = 0) -> str:
    """One word with its shouting taken out: an all-caps part of 4+ letters (3+ when `strict`) that isn't an acronym →
    a capital first ("FARM" → "Farm"); hyphen parts one by one ("DRI-FIT" stays: 3-letter parts)."""
    acr = _acronyms()
    if word.upper() in acr or re.sub(r"[^\w&./]", "", word).upper() in acr:
        return word
    least = 3 if strict else int(rules()["title"]["caps_word_letters"])

    def part(p: str) -> str:
        letters = re.sub(r"[^A-Za-z]", "", p)
        if len(letters) >= least and letters.isupper() and p.upper() not in acr and letters.upper() not in acr:
            return re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m[0][0].upper() + m[0][1:].lower(), p, count=1)
        return p
    return "-".join(part(p) for p in word.split("-"))


# ---------------------------------------------------------------- what breaks the rules

_ITEM_ID = re.compile(r"\bi_\d{6}_[0-9a-f]{6}\b")
_CODE_PHRASE = re.compile(r"\b(?:SKU|RN|CA|Style|Model|Item|Ref|Art)\s*(?:#|No\.?|Number)?\s*[:#]?\s*"
                          r"[A-Z]{0,3}\d{3,}[A-Z0-9-]*\b", re.I)
_SIZE_LIKE = re.compile(r"^(?:\d{1,2}(?:\.\d)?[A-Za-z]{0,2}|W\d{2}(?:L\d{2})?|\d{2}[xX]\d{2}|\d+-Piece|\d+(?:st|nd|rd|th|s)|"
                        r"\d+%|\$\d+(?:\.\d\d)?|\d{2,3}[A-Za-z]{1,3}|[A-Za-z]{1,3}\d{1,2})$")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️‍]")
_ALLOWED = re.compile(r"[^\w\s.,'’&%$+()\-–/#!?:]", re.UNICODE)


def _codes(title: str) -> list[str]:
    found = [m[0] for m in _ITEM_ID.finditer(title)] + [m[0] for m in _CODE_PHRASE.finditer(title)]
    for tok in re.findall(r"[A-Za-z0-9-]+", title):
        bare = tok.strip("-")
        if not bare or _SIZE_LIKE.match(bare) or bare in found:
            continue
        has_d, has_l = any(c.isdigit() for c in bare), any(c.isalpha() for c in bare)
        if (has_d and has_l and len(bare) >= 5 and "-" not in bare) or (bare.isdigit() and len(bare) >= 5):
            found.append(bare)
    return list(dict.fromkeys(found))


def _dupes(title: str, brand: str | None = None) -> list[str]:
    seen, out = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z'’]+", title):
        k = w.lower().replace("’", "'")
        if k in SMALL or len(k) < 3:
            continue
        if k in seen and k not in out:
            out.append(w)
        seen.add(k)
    return out


def _condition_words(title: str) -> list[str]:
    out = []
    for w in rules()["title"]["condition_words"]:
        if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", title, re.I):
            out.append(w)
    return list(dict.fromkeys(out))


def check(title: str, site: str | None = None) -> list[str]:
    """What breaks the shop-window rules (WO50 §1), in words — [] when the title passes for `site` (None: all three)."""
    t = title or ""
    problems = []
    acr = _acronyms()
    shouting = [w for w in re.findall(r"[A-Za-z][A-Za-z&.'/-]*", t)
                if len(re.sub(r"[^A-Za-z]", "", w)) >= int(rules()["title"]["caps_word_letters"])
                and re.sub(r"[^A-Za-z]", "", w).isupper() and w.upper() not in acr
                and re.sub(r"[^A-Za-z&./]", "", w).upper() not in acr
                and not all(len(re.sub(r"[^A-Za-z]", "", p)) < 4 or p.upper() in acr for p in w.split("-"))]
    if shouting:
        problems.append("all-caps: " + ", ".join(dict.fromkeys(shouting)))
    limit = float(site_rule(site, "caps_share_max", rules()["title"]["caps_share_max"]))
    if caps_share(t) > limit:
        problems.append(f"capitals {caps_share(t):.0%} (limit {limit:.0%})")
    if codes := _codes(t):
        problems.append("a code: " + ", ".join(codes))
    if dupes := _dupes(t):
        problems.append("repeated: " + ", ".join(dupes))
    if re.search(r"\bsize\s+size\b", t, re.I):
        problems.append("size size")
    if words := _condition_words(t):
        problems.append("condition words: " + ", ".join(words))
    if re.search(r"&(?:amp|quot|#\d+|apos|lt|gt);", t):
        problems.append("an HTML artefact")
    if _EMOJI.search(t) or _ALLOWED.search(t.replace("’", "'")):
        problems.append("odd characters")
    if re.search(r"[A-Za-z]/[A-Za-z]", t) and not any(a in t for a in ("RE/DONE",)):
        problems.append("a slash")
    if len(t) > title_max(site):
        problems.append(f"too long: {len(t)} > {title_max(site)}")
    return problems


# ---------------------------------------------------------------- the deterministic fix

def fix(title: str, brand: str | None = None, condition: str | None = None, max_len: int | None = None,
        strict: int = 0, keep: list[str] | None = None) -> str:
    """The title rewritten to pass `check`, deterministically: HTML artefacts unescaped, emoji and odd characters
    out, "Black/Green" → "Black & Green", the brand as a shop sign writes it, condition words out ("New" stays), codes
    out, shouting softened, repeated words once, "size size" once, fitted to `max_len` (never the brand or the size:
    premium.fit_title with `keep`). `strict` (a site's own "too many capitals" refusal): 3-letter words softened too,
    then small words lower-case. NWT (WO50, the owner's example): ", New with Tags" at the end when it fits."""
    t = html.unescape(html.unescape(title or "")).replace("’", "'")
    t = _EMOJI.sub("", t)
    t = _ALLOWED.sub(" ", t)
    t = re.sub(r"(?<=[A-Za-z])\s*/\s*(?=[A-Za-z])", " & ", t) if "RE/DONE" not in t else t
    if not brand:                                    # a brand of the display table at the start ("BCBGMAXAZRIA New …")
        head = t.split()
        for n in (3, 2, 1):
            if len(head) >= n and _key(" ".join(head[:n])) in _display():
                brand = " ".join(head[:n])
                break
    shown = display_brand(brand) if brand else ""
    if brand:
        for form in {brand, *(p.strip() for p in re.split(r"[()]", brand) if p.strip())}:
            pat = r"[\s.\-']*".join(re.escape(w) for w in re.findall(r"[A-Za-z0-9&]+", form))
            if pat:
                t = re.sub(rf"(?<![\w]){pat}(?![\w])", shown, t, flags=re.I)
        t = re.sub(rf"({re.escape(shown)})\s*\(\s*{re.escape(shown)}\s*\)", r"\1", t, flags=re.I)
        t = re.sub(rf"({re.escape(shown)})(\s+{re.escape(shown)})+\b", r"\1", t, flags=re.I)
    t = re.sub(r"\bbrand\s+new\b", "New", t, flags=re.I)
    for w in sorted(_condition_words(t), key=len, reverse=True):
        t = re.sub(rf"(?<![\w-]){re.escape(w)}(?![\w-])", " ", t, flags=re.I)
    t = re.sub(r",?\s*New with(?:out)? Tags\b", " ", t, flags=re.I)        # put back below, in its one place
    for code in _codes(t):
        t = re.sub(rf"(?<![\w-]){re.escape(code)}(?![\w-])", " ", t)
    t = " ".join(w if (shown and w in shown.split()) else _soften(w, strict) for w in t.split())
    t = re.sub(r"\bsize\s+(?:size\s+)+", "size ", t, flags=re.I)
    words, seen = [], set()
    for w in t.split():
        k = re.sub(r"[^a-z']", "", w.lower())
        if k and len(k) >= 3 and k not in SMALL and k in seen and not (shown and w in shown.split()):
            continue
        seen.add(k)
        words.append(w)
    t = " ".join(words)
    safe = float(rules()["title"].get("caps_share_safe", rules()["title"]["caps_share_max"]))
    if caps_share(t) > safe:                          # aim for the share known to pass: connecting words lower-case
        t = " ".join(w.lower() if i and w.lower() in SMALL else w for i, w in enumerate(t.split()))
    if strict and caps_share(t) > safe:               # a site's own refusal: sentence case beyond the brand, acronyms
        keep = set(shown.split()) if shown else set()
        acr = _acronyms()
        t = " ".join(w if (i == 0 or w in keep or w.upper() in acr or not w[:1].isalpha() or w.lower() == "new")
                     else w.lower() for i, w in enumerate(t.split()))
    t = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", t))
    t = re.sub(r"(?:,\s*){2,}", ", ", t).strip(" ,-&/")
    cap = max_len or title_max()
    if condition == "NWT" and len(t) + len(NWT_TAIL) <= cap:
        t = re.sub(rf"^({re.escape(shown)}\s+)?New\s+", lambda m: m[1] or "", t) if shown else re.sub(r"^New\s+", "", t)
        t = t.rstrip(" ,") + NWT_TAIL
    elif condition == "NWT" and not re.search(r"\bNew\b", t):
        t = f"{shown} New {t[len(shown):].strip()}".strip() if shown and t.startswith(shown) else f"New {t}"
    if len(t) > cap:
        from thrift_agent.brain import premium
        t = premium.fit_title(t, [shown, *(keep or [])]) if cap == premium.TITLE_MAX else _cut(t, cap)
        if len(t) > cap:
            t = _cut(t, cap)
    return t


def _cut(t: str, cap: int) -> str:
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,-&")


def shop_window(title: str, facts) -> str:
    """The copy's title made a shop-window title (the end of premium.title_with_feature): the facts' brand, condition,
    and what must stay (the US size, the set phrase, the colours)."""
    from thrift_agent.brain import sizes
    keep = [sizes.title_size(facts) or "", f"{facts.set_pieces}-Piece Set" if facts.set_pieces else "",
            *(facts.colors or [])]
    return fix(title, (facts.brand.value or "").strip() or None, facts.condition, None, 0, keep)


def for_site(title: str, site: str, strict: int = 0, brand: str | None = None, condition: str | None = None) -> str:
    """The title as `site` gets it (Vinted's title, Depop's first line), fixed for its rules — the brand as a shop sign
    writes it when it is given; the strict level comes from the site's own refusal (`crosslist.record`: fixed and
    retried once)."""
    shown = display_brand(brand) if brand else ""
    if not check(title, site) and not strict and (not brand or shown == brand or brand not in title):
        return title
    return fix(title, brand, condition, title_max(site), strict)


def display_in_text(text: str, brand: str | None) -> str:
    """A description with the brand written as a shop sign writes it ("BCBGMAXAZRIA slouchy shoulder bag" → "BCBG Max
    Azria slouchy shoulder bag"); nothing else changes, and nothing when the brand is already in normal case."""
    shown = display_brand(brand) if brand else ""
    if not text or not shown or shown == brand:
        return text
    pat = r"[\s.\-']*".join(re.escape(w) for w in re.findall(r"[A-Za-z0-9&]+", brand))
    return re.sub(rf"(?<![\w]){pat}(?![\w])", shown, text) if pat else text


def blocked(title: str, site: str | None = None) -> list[str]:
    """What still breaks the rules after the fix — the title never reaches the site while this isn't []."""
    return check(fix(title, max_len=title_max(site)), site)


# ---------------------------------------------------------------- a site's own refusal

def fixable(site: str, error: str | None) -> str | None:
    """The fix a site's refusal names (data/site_rules.json "refusals"): "title_caps", "title_length", "title_chars" —
    or None when the code can't fix it itself."""
    for r in rules()["refusals"]:
        if r["site"] in ("*", site) and re.search(r["match"], error or "", re.I):
            return r["fix"]
    return None


# ---------------------------------------------------------------- the model's one rewrite

def rewrite(s, title: str, facts, problems: list[str]) -> str | None:
    """WO50: ONE model rewrite of a title the fixer couldn't make pass — the shop-window rules and what broke them; the
    answer goes through `fix` and `check` again (the pipeline). None when the call fails."""
    from pydantic import BaseModel, Field

    from thrift_agent.brain import llm

    class TitleOut(BaseModel):
        title: str = Field(description="The rewritten title, at most 80 characters")
    prompt = ("Rewrite this resale listing title for a shop window: short, natural, nice to read; the brand first (as "
              "a shop sign writes it, not an all-caps logo), what it is, the one feature that sells it, colour, size "
              "('size M'). No codes, no style numbers, no all-caps words but real acronyms (UGG, H&M, DKNY), no emoji, "
              "no slashes, no repeated words, no condition words but 'New'. Keep every fact; add none.\n"
              f"Title: {title}\nWhat breaks the rules: {'; '.join(problems)}\n"
              f"Brand: {facts.brand.value or 'none'} · Item: {facts.item_type} · Colours: {', '.join(facts.colors or [])}")
    try:
        out = llm.ask(s.get("models.copy", "claude-sonnet-5"), "You fix one listing title.", prompt, TitleOut,
                      tool="write_title", max_tokens=300)
    except Exception:  # noqa: BLE001 — the title then waits (blocked), said once
        return None
    return out.title if out and out.title else None


# ---------------------------------------------------------------- the live titles, reviewed once

REVIEW = "title_review"           # kv: {item: {"new": title, "old": {site: title}}} — what "go titles" applies


def live_titles(db) -> dict[str, dict[str, str]]:
    """{item: {site: the title the site shows}} for every posted listing: Poshmark's render, Vinted's mapped title,
    Depop's description's first line."""
    from thrift_agent.db import loads
    out: dict[str, dict[str, str]] = {}
    for r in db.conn.execute("SELECT l.item_id, l.marketplace, l.fields_json, i.renders FROM listings l JOIN items i "
                             "ON i.id = l.item_id WHERE l.status = 'posted' AND COALESCE(i.test, 0) = 0"):
        fields = loads(r["fields_json"]) or {}
        posh = (loads(r["renders"]) or {}).get("poshmark") or {}
        if r["marketplace"] == "poshmark":
            t = posh.get("title")
        elif r["marketplace"] == "vinted":
            t = fields.get("title") or posh.get("title")
        else:
            t = (str(fields.get("description") or "").split("\n", 1)[0].strip()) or posh.get("title")
        if t:
            out.setdefault(r["item_id"], {})[r["marketplace"]] = t
    return out


def review(db) -> list[dict]:
    """Every live title that breaks a rule, with its proposed new title: [{item, old: {site: title}, new, problems}].
    The proposal is the Poshmark title fixed (one title for all three sites); a site whose title already IS the new
    one is left out. Kept in kv `title_review` for "go titles"."""
    from thrift_agent.db import loads
    rows = []
    for iid, sites in sorted(live_titles(db).items()):
        it = db.item(iid)
        facts = (loads(it["facts"]) or {}) if it is not None else {}
        brand = ((facts.get("brand") or {}).get("value") if isinstance(facts.get("brand"), dict) else None)
        cond = facts.get("condition")
        base = sites.get("poshmark") or next(iter(sites.values()))
        problems = sorted({p for t in sites.values() for p in check(t)})
        if not problems:
            continue
        new = fix(base, brand, cond)
        old = {mp: t for mp, t in sites.items() if t != new}
        if old and not check(new):
            rows.append({"item": iid, "old": old, "new": new, "problems": problems})
    db.kv_set(REVIEW, json.dumps({r["item"]: {"new": r["new"], "old": r["old"]} for r in rows}))
    return rows


def review_message(rows: list[dict]) -> str:
    if not rows:
        return "✓ Every live title passes the shop-window rules."
    lines = [f"✏️ {len(rows)} live title{'s break' if len(rows) != 1 else ' breaks'} the shop-window rules — the old → "
             f"the new (the same on every site it is on):"]
    for r in rows:
        old = next(iter(dict.fromkeys(r["old"].values())))
        lines.append(f"• {old}\n   → {r['new']}  ({', '.join(r['problems'])[:80]})")
    lines.append("Reply 'go titles' to change them on the sites (nothing else on the listings changes).")
    return "\n".join(lines)


def apply_review(s, db) -> list[str]:
    """The owner's "go titles": each reviewed item's new title queued as a "text" edit on every site it is live on
    (Depop: its description's first line), its render and mapped fields following once the site shows it (edits
    `_mirror`). Returns the edit rows made."""
    from thrift_agent import edits
    from thrift_agent.db import loads
    plan = loads(db.kv_get(REVIEW)) or {}
    made = []
    for iid, entry in plan.items():
        made += edits.queue_text(db, iid, entry["new"], sites=list((entry.get("old") or {}).keys()))
    db.log(None, "titles_applied", {"items": list(plan), "rows": made})
    return made
