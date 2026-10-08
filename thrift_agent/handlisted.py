"""Items the owner already listed by hand on Depop or Vinted (WO33): found before any backfill so nothing is listed
twice. Her shops are read through the Thrift Chrome's extension (the `find` job: each listing's address and text — on
Vinted the tile's description "<title>, brand: …, condition: …, size: …, $35.00", on Depop the address's words); each
of our items is scored against them; the list goes to the owner's private chat, numbered, sure and unsure alike, and
nothing is recorded until the owner confirms (`thrift crosslist --hand-listed --apply 1,3`): then the item's row for
that site is `posted` with her listing's address, so the backfill skips it and a sale elsewhere takes it down too."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from thrift_agent.db import DB, loads, now

KEY = "hand_listed_candidates"        # kv: the last list sent to the owner, by number
STOP = {"size", "sz", "the", "with", "and", "for", "new", "nwt", "nwot", "kids", "kid", "girls", "girl", "boys", "boy",
        "womens", "women", "mens", "men", "toddler", "baby", "us", "eu", "piece", "pieces", "set", "brand", "condition",
        "good", "very", "like", "without", "tags", "child", "one"}
SURE, MAYBE = 0.62, 0.35              # a score at least this high: listed as "same item" / "not sure"


@dataclass
class Candidate:
    n: int
    item: str
    title: str
    site: str
    url: str
    theirs: str
    score: float
    sure: bool


def words(text: str) -> list[str]:
    text = (text or "").lower().replace("&", " and ").replace("'", "")
    return [w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text) if w not in STOP and len(w) > 1 or w.isdigit()]


def _brand_words(brand: str) -> set[str]:
    return set(words(brand)) - {"and"}


def _sizes(text: str) -> set[str]:
    """The size tokens a title or a description gives: "size 7.5", "size: S / US 4-6", "size M", "26"."""
    out = set()
    for m in re.finditer(r"size[:\s]+([a-z0-9./-]+)", (text or "").lower()):
        out.add(m.group(1).strip(".,"))
    return out


def _price(text: str) -> float | None:
    m = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text or "")
    return float(m.group(1)) if m else None


def score(ours: dict, theirs: str, url: str) -> float:
    """How alike our item and her listing look, 0–1: the brand (a near spelling counts: Missguided / Misguided), the
    item's own words, the size, the price."""
    t_words = set(words(f"{theirs} {url.rsplit('/', 1)[-1]}"))
    brand = _brand_words(ours.get("brand") or "")
    s = 0.0
    if brand:
        hit = brand <= t_words or any(_near(b, w) for b in brand for w in t_words)
        s += 0.4 if hit else -0.2
    o_words = set(words(ours.get("title") or "")) - brand
    if o_words:
        s += 0.4 * len(o_words & t_words) / len(o_words)
    o_size = {str(ours.get("size") or "").lower()} | _sizes(ours.get("title") or "")
    t_size = _sizes(theirs) | {w for w in t_words if re.fullmatch(r"\d+(?:\.\d)?|xxs|xs|s|m|l|xl|xxl|\dt", w)}
    if o_size & t_size:
        s += 0.15
    p = _price(theirs)
    if p and ours.get("price") and abs(p - float(ours["price"])) <= 0.25 * float(ours["price"]):
        s += 0.05
    return round(max(0.0, min(1.0, s)), 2)


def _near(a: str, b: str) -> bool:
    """One letter apart (a doubled letter dropped: Missguided / Misguided), for brands of 5 letters or more."""
    if len(a) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    long, short = (a, b) if len(a) > len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def candidates(items: list[dict], shops: dict[str, list[dict]], ours_urls: set[str]) -> list[Candidate]:
    """For each of our items, her best-looking listing on each site (our own listings left out), when it scores at
    least MAYBE; numbered for the owner's answer."""
    out = []
    for it in items:
        for site, listings in shops.items():
            scored = [(score(it, x.get("text") or "", x.get("url") or ""), x) for x in listings
                      if (x.get("url") or "") not in ours_urls]
            if not scored:
                continue
            best, x = max(scored, key=lambda pair: pair[0])
            if best >= MAYBE:
                out.append(Candidate(0, it["id"], it.get("title") or "", site, x["url"], x.get("text") or "", best,
                                     best >= SURE))
    out.sort(key=lambda c: (not c.sure, -c.score))
    for n, c in enumerate(out, 1):
        c.n = n
    return out


def message(found: list[Candidate], counts: dict[str, int]) -> str:
    """The owner's list: what was read, then every candidate numbered, the sure ones first."""
    head = (f"🔎 Listed by hand already? I read your shops ({', '.join(f'{s.title()} {n}' for s, n in counts.items())} "
            "listings) and compared them with our items. Nothing is marked until you confirm.")
    if not found:
        return head + "\nNo matches."
    lines = [head]
    for c in found:
        lines.append(f"{c.n}. {'SAME?' if c.sure else 'not sure'} — ours: {c.title} ↔ {c.site.title()}: "
                     f"{c.theirs[:90]} {c.url} (score {c.score})")
    lines.append("Tell Claude Code which numbers are the same item (e.g. 'hand-listed: 1, 2').")
    return "\n".join(lines)


def save(db: DB, found: list[Candidate]) -> None:
    db.kv_set(KEY, json.dumps({"at": now(), "candidates": [asdict(c) for c in found]}))


def apply(db: DB, numbers: list[int]) -> list[str]:
    """The owner's confirmed numbers recorded: the item's row for that site `posted` with her listing's address —
    the backfill then skips it, and a sale elsewhere takes it down too. An item already holding a row there is left."""
    saved = loads(db.kv_get(KEY)) or {}
    by_n = {c["n"]: c for c in saved.get("candidates") or []}
    done = []
    for n in numbers:
        c = by_n.get(n)
        if c is None:
            done.append(f"{n}: no such number in the last list")
            continue
        row = db.listing(c["item"], c["site"])
        if row is not None and row["status"] in ("posted", "posting"):
            done.append(f"{n}: {c['item']} already {row['status']} on {c['site']} — left as it is")
            continue
        db.upsert_listing(c["item"], c["site"], status="posted", url=c["url"], error="listed by hand (the owner)")
        db.log(c["item"], "hand_listed", {"site": c["site"], "url": c["url"], "score": c["score"]})
        done.append(f"{n}: {c['item']} recorded on {c['site']}: {c['url']}")
    return done
