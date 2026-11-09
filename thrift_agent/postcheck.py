"""WO53 (live 2026-11-08): after the final click the site's own data decides — never a question, never a blind second
click, never a pause.

That night Poshmark took three listings. It landed each time on /closet/<user>?created_listing_id=<id> — naming the
new listing — but the closet's first page (its default order, "Just Shared") didn't show them in the 90 s the poster
watched: the poster called them "unconfirmed", asked the owner to check Poshmark and reply 'posted <url>' or 'retry',
counted them as three failures in a row, paused Poshmark and said "nothing was published" — while all three were live.
/listing/<id> showed each one at once (it redirects to /listing/<slug>-<id>).

Now:
- Poshmark, in order: the listing id Poshmark named (its landing address, or an id in its own answers to the click) →
  the public listing page: our listing → posted; else the closet sorted newest first (sort_by=added_desc), read from its
  page state: the named id, else our title (as Poshmark builds its slugs: letters and digits only) at our price,
  created after the click (the id's own timestamp — UTC, no time zones); two such → the cover photo decides; never a
  guess. Plain HTTP reads of public pages: no browser, no click, nothing typed.
- every site: a publish not confirmed at once is "verifying" — looked at again 10 s, 30 s, 1, 2, 3, 5 and 10 minutes
  after the click, then every 10 minutes, quietly; the next item never waits for it.
- absent only with proof (rule 3): two reads that show it absent, at least 10 minutes apart, the last at least 30
  minutes after the click. A read that couldn't be made is never "absent". Proven absent → back in line once, with one
  last look right before the click. Still unknown 2 hours after the click → ONE 🚨 with the evidence and a ready Claude
  Code request; never a question to the owner.
- an unconfirmed publish is never a failure: it never counts toward a pause.

The state rides on kv `unconfirmed_state` (crosslist.UNCONF): {"stage": wait|retry|reposted|given_up, "clicked_at",
"due", "round", "reads": [{"at", "result", "how"}], "created", "shot", "alarmed"}."""
from __future__ import annotations

import io
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from thrift_agent.db import DB, loads

SCHEDULE_S = (10, 30, 60, 120, 180, 300, 600)   # the looks after the click: 10 s, 30 s, 1, 2, 3, 5, 10 min …
EVERY_S = 600                                   # … then every 10 minutes
INLINE_S = (10, 30, 60)                         # Poshmark: the looks made before the next item (the rest wait in line)
ABSENT_GAP_S = 600                              # rule 3: two "absent" reads at least this far apart …
ABSENT_AFTER_S = 1800                           # … the last at least this long after the click
ALARM_AFTER_S = 2 * 3600                        # still unknown this long after the click: ONE 🚨
ID_SKEW_S = 120                                 # a Poshmark id stamped this much before our click is still "after" it
COVER_MAX_DIST = 10                             # two candidates: the cover photo decides (perceptual-hash distance)
POSH = "https://poshmark.com"
LISTING_PATH = re.compile(r"^/listing/(?P<slug>[^/]+)-(?P<id>[0-9a-f]{24})/?$")
HEX24 = re.compile(r"\b[0-9a-f]{24}\b")
LANDINGS = "site_landings"                      # kv: {site: [landing kinds seen after the final click]}
KNOWN_LANDINGS = {"poshmark": ["closet", "listing"], "depop": ["success", "listing"], "vinted": ["member", "listing"]}
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
BUTTON = {"poshmark": "List", "depop": "Post", "vinted": "Upload"}

Get = Callable[[str], tuple]                    # url → (status or None, text, final url)


def now() -> datetime:
    return datetime.now(timezone.utc)


def utc(v) -> datetime | None:
    """An ISO time (or a datetime) as an aware UTC datetime; None when unreadable."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- the schedule and rule 3

def next_due(clicked: datetime, at: datetime) -> datetime:
    """The next look after `at`: SCHEDULE_S after the click, then every EVERY_S (counted from the click)."""
    for s in SCHEDULE_S:
        t = clicked + timedelta(seconds=s)
        if t > at:
            return t
    last = SCHEDULE_S[-1]
    k = math.floor(((at - clicked).total_seconds() - last) / EVERY_S) + 1
    return clicked + timedelta(seconds=last + k * EVERY_S)


def absent_proven(reads: list[dict], clicked: datetime) -> bool:
    """Rule 3: two reads that showed the listing absent, at least ABSENT_GAP_S apart, the last at least ABSENT_AFTER_S
    after the click. A read that couldn't be made never counts."""
    times = sorted(t for r in reads or [] if r.get("result") == "absent" and (t := utc(r.get("at"))) is not None)
    if len(times) < 2 or (times[-1] - clicked).total_seconds() < ABSENT_AFTER_S:
        return False
    return any((times[-1] - t).total_seconds() >= ABSENT_GAP_S for t in times[:-1])


def overdue(clicked: datetime, at: datetime) -> bool:
    """Still unknown this long after the click: the one 🚨."""
    return (at - clicked).total_seconds() >= ALARM_AFTER_S


# ---------------------------------------------------------------- Poshmark's public pages

def _get(url: str) -> tuple:
    """A public Poshmark page as a browser asks for it: (status, text, final url); (None, "", url) when no answer came."""
    import httpx

    from thrift_agent import crosslist
    try:
        r = httpx.get(url, headers={"User-Agent": crosslist.UA, "Accept": "text/html"}, timeout=20,
                      follow_redirects=True, verify=crosslist._tls())
    except httpx.HTTPError:
        return None, "", url
    return r.status_code, r.text, str(r.url)


def _state(text: str) -> dict:
    from thrift_agent.harvest import parse_state
    try:
        return parse_state(text) or {}
    except ValueError:
        return {}


def _money(d: dict) -> float | None:
    try:
        v = (d.get("price_amount") or {}).get("val")
        v = d.get("price") if v in (None, "") else v
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError, AttributeError):
        return None


def posh_slug(title: str) -> str:
    """The slug Poshmark gives a listing with this title ("Twist-Front" → "TwistFront", "&" and "," dropped)."""
    return "-".join(re.sub(r"[^A-Za-z0-9\s]", "", title or "").split())


def address(lid: str, title: str | None = None, final: str | None = None, base: str = POSH) -> str:
    """The canonical https://poshmark.com/listing/<slug>-<id>: from the page Poshmark redirected to, else our slug."""
    m = LISTING_PATH.match(urlparse(final or "").path)
    if m and m["id"] == lid:
        return f"{base}/listing/{m['slug']}-{lid}"
    return f"{base}/listing/{posh_slug(title or '') or 'listing'}-{lid}"


def posh_listing(lid: str, get: Get | None = None, base: str = POSH) -> dict:
    """The public listing /listing/<id>: {"read": True, "url", "id", "title", "price", "status", "inventory"};
    {"read": True, "gone": True} on 404 / 410; {"read": False, "why"} when it couldn't be read."""
    status, text, final = (get or _get)(f"{base}/listing/{lid}")
    if status is None:
        return {"read": False, "why": "no answer"}
    if status in (404, 410):
        return {"read": True, "gone": True}
    if status != 200:
        return {"read": False, "why": f"HTTP {status}"}
    d = (_state(text).get("$_listing_details") or {}).get("listingDetails") or {}
    if not d:
        return {"read": False, "why": "no listing on the page"}
    if d.get("id") and d.get("id") != lid:
        return {"read": False, "why": f"the page shows another listing ({d.get('id')})"}
    return {"read": True, "url": address(lid, d.get("title"), final, base), "id": lid, "title": d.get("title"),
            "price": _money(d), "status": d.get("status"), "inventory": (d.get("inventory") or {}).get("status")}


def posh_closet(user: str, get: Get | None = None, base: str = POSH) -> dict:
    """The closet's first page, newest first (sort_by=added_desc), from its page state ($_closet.listingsPostData):
    {"read": True, "rows": [{"id", "title", "price", "created", "status", "inventory", "cover"}]} or {"read": False,
    "why"}."""
    status, text, _ = (get or _get)(f"{base}/closet/{user}?sort_by=added_desc")
    if status != 200:
        return {"read": False, "why": "no answer" if status is None else f"HTTP {status}"}
    data = ((_state(text).get("$_closet") or {}).get("listingsPostData") or {}).get("data")
    if not isinstance(data, list):
        return {"read": False, "why": "no closet data on the page"}
    rows = []
    for x in data:
        if isinstance(x, dict) and x.get("id"):
            cover = x.get("cover_shot") if isinstance(x.get("cover_shot"), dict) else {}
            rows.append({"id": x["id"], "title": x.get("title"), "price": _money(x), "created": x.get("created_at"),
                         "status": x.get("status"), "inventory": (x.get("inventory") or {}).get("status"),
                         "cover": cover.get("url") or x.get("picture_url")})
    return {"read": True, "rows": rows}


def title_key(title: str | None) -> str:
    """A title as Poshmark's slug sees it: ASCII letters and digits, lowercased — "Twist-Front" = "TwistFront", "&" and
    "," gone, "know one cares" = "Know One Cares"."""
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def id_time(lid: str | None) -> int | None:
    """The creation time (epoch seconds, UTC) in a 24-hex id: its first 8 hex digits."""
    try:
        return int(str(lid)[:8], 16) if lid and HEX24.fullmatch(str(lid)) else None
    except ValueError:
        return None


def same_price(a, b) -> bool:
    try:
        return a is not None and b is not None and round(float(a), 2) == round(float(b), 2)
    except (TypeError, ValueError):
        return False


def cover_matcher(ours: Path | str | None, get_bytes: Callable[[str], bytes | None] | None = None):
    """A check `url → True/False/None` (None: couldn't compare): is the picture at `url` our cover photo?"""
    if not ours or not Path(ours).is_file():
        return None

    def fetch(url: str) -> bytes | None:
        import httpx
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True)
            return r.content if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def check(url: str) -> bool | None:
        try:
            import imagehash
            from PIL import Image, ImageOps
            raw = (get_bytes or fetch)(url)
            if not raw:
                return None
            theirs = Image.open(io.BytesIO(raw)).convert("RGB")
            mine = ImageOps.exif_transpose(Image.open(ours)).convert("RGB")
            return (imagehash.phash(theirs) - imagehash.phash(mine)) <= COVER_MAX_DIST
        except Exception:  # noqa: BLE001 — a picture that can't be compared decides nothing
            return None
    return check


def find_poshmark(title: str, price, clicked: float, *, created: str | None = None, extra_ids: list[str] = (),
                  user: str | None = None, held: set[str] | frozenset = frozenset(), cover=None,
                  get: Get | None = None, base: str = POSH) -> tuple[str | None, dict]:
    """WO53 §1 — is our listing on Poshmark? In order: the id Poshmark named after our click (its listing page: there
    → ours; its title or price differing is noted, never refused — Poshmark named it); other ids from its answers
    (`extra_ids`: their page must show our title and price); the closet newest first — the named id, else our title at
    our price created after the click (`clicked`, epoch seconds UTC); two such → the cover photo (`cover`) decides,
    never a guess. An address another item holds (`held`) is never taken. Returns (the address or None, what was seen,
    with "result": found | absent | unknown — "absent" only when every page needed was read)."""
    seen: dict = {"created": created, "clicked": clicked}
    readable = True
    answered: list[str] = []                       # ids only seen in Poshmark's answers whose page shows our listing
    for lid, named in [*([(created, True)] if created else []), *[(i, False) for i in extra_ids if i != created]]:
        page = posh_listing(lid, get, base)
        seen.setdefault("listings", []).append({"id": lid, **{k: page.get(k) for k in ("read", "gone", "why", "title",
                                                                                         "price", "inventory")}})
        if not page.get("read"):
            readable = readable and not named         # the named id unread: never "absent"
            continue
        if page.get("gone"):
            continue
        ours = title_key(page.get("title")) == title_key(title) and same_price(page.get("price"), price)
        after = (id_time(lid) or 0) >= clicked - ID_SKEW_S
        if named and after and page["url"] not in held:
            if not ours:
                seen["note"] = (f"Poshmark's page for the listing it named shows {page.get('title')!r} at "
                                f"${page.get('price')} (ours: {title!r} at ${price})")
            return page["url"], {**seen, "result": "found", "how": "the listing id Poshmark named"}
        if ours and after and page["url"] not in held:
            answered.append(page["url"])
    if len(answered) == 1:
        return answered[0], {**seen, "result": "found", "how": "an id in Poshmark's answers"}
    if len(answered) > 1:                           # one click, two new listings with our title: never a guess
        return None, {**seen, "result": "unknown", "why": f"{len(answered)} new listings with our title in Poshmark's "
                                                          f"answers: never a guess"}
    closet = posh_closet(user, get, base) if user else {"read": False, "why": "no closet name"}
    seen["closet"] = {"read": closet.get("read"), "why": closet.get("why"), "rows": len(closet.get("rows") or [])}
    rows = closet.get("rows") or []
    if created and (row := next((x for x in rows if x["id"] == created), None)):
        url = address(created, row.get("title"), base=base)
        if url not in held:
            return url, {**seen, "result": "found", "how": "the closet shows the id Poshmark named"}
    want = title_key(title)
    cands = [x for x in rows if title_key(x.get("title")) == want and same_price(x.get("price"), price)
             and (id_time(x["id"]) or 0) >= clicked - ID_SKEW_S and address(x["id"], x.get("title"), base=base) not in held]
    seen["candidates"] = [x["id"] for x in cands]
    if len(cands) == 1:
        return address(cands[0]["id"], cands[0].get("title"), base=base), {**seen, "result": "found",
                                                              "how": "the closet: our title and price, created after "
                                                                     "the click"}
    if len(cands) > 1:
        verdicts = {x["id"]: (cover(x["cover"]) if cover is not None and x.get("cover") else None) for x in cands}
        seen["covers"] = verdicts
        mine = [x for x in cands if verdicts.get(x["id"]) is True]
        if len(mine) == 1:
            return address(mine[0]["id"], mine[0].get("title"), base=base), {**seen, "result": "found",
                                                                  "how": "the closet: two with our title — the cover "
                                                                         "photo decided"}
        return None, {**seen, "result": "unknown", "why": f"{len(cands)} new listings with our title: never a guess"}
    if not closet.get("read") or not readable:
        return None, {**seen, "result": "unknown", "why": closet.get("why") or "the listing page couldn't be read"}
    return None, {**seen, "result": "absent"}


# ---------------------------------------------------------------- a publish being verified (kv unconfirmed_state)

def begin(db: DB, iid: str, mp: str, *, clicked: datetime | None = None, created: str | None = None,
          shot: str | None = None, round_: int = 1, at: datetime | None = None) -> dict:
    """The final click happened and no address was confirmed at once: "verifying" from now — the next look due on the
    schedule, nothing said."""
    from thrift_agent import crosslist
    t = at or now()
    c = clicked or t
    state = {"stage": "wait", "clicked_at": iso(c), "due": iso(next_due(c, t)), "round": round_, "reads": [],
             "created": created, "shot": shot, "tries": 0, "alarmed": False}
    crosslist.set_unconf(db, iid, mp, **state)
    db.log(iid, "verifying", {"mp": mp, "clicked_at": state["clicked_at"], "due": state["due"], "created": created,
                              "round": round_})
    return state


def clicked_of(state: dict | None, row=None) -> datetime:
    """When the final click was: the state's, else the row's last update (an older unconfirmed row), else now."""
    c = utc((state or {}).get("clicked_at"))
    if c is None and row is not None:
        try:
            c = utc(row["updated_at"])
        except (KeyError, IndexError, TypeError):
            c = None
    return c or now()


def note_read(db: DB, iid: str, mp: str, result: str, how: str | None = None, at: datetime | None = None) -> dict:
    """One look's result (found | absent | unknown) on the state; the next look scheduled. Returns the new state."""
    from thrift_agent import crosslist
    t = at or now()
    st = crosslist.unconf_state(db, iid, mp) or {}
    clicked = clicked_of(st, db.listing(iid, mp))
    reads = [*(st.get("reads") or []), {"at": iso(t), "result": result, **({"how": how[:120]} if how else {})}][-16:]
    crosslist.set_unconf(db, iid, mp, reads=reads, clicked_at=iso(clicked), due=iso(next_due(clicked, t)),
                         tries=int(st.get("tries") or 0) + (1 if result == "unknown" else 0))
    return crosslist.unconf_state(db, iid, mp) or {}


# ---------------------------------------------------------------- where a site landed after the final click (§1, §7)

def landing_kind(mp: str, url: str | None, user: str | None = None) -> str:
    """What kind of page the site landed on after the final click: listing, closet, success, member, form, or
    other:<first path part>."""
    path = urlparse(url or "").path.rstrip("/")
    if not path:
        return "unknown"
    if mp == "poshmark":
        if LISTING_PATH.match(path + "/") or path.startswith("/listing/"):
            return "listing"
        if path.startswith("/closet/"):
            return "closet"
        if path.startswith("/create-listing"):
            return "form"
    if mp == "depop":
        if "/products/create/success" in path:
            return "success"
        if path.startswith("/products/"):
            return "listing"
    if mp == "vinted":
        if path.startswith("/member/"):
            return "member"
        if path.startswith("/items/new"):
            return "form"
        if path.startswith("/items/"):
            return "listing"
    return f"other:{path.split('/')[1] if '/' in path else path}"


def landed(db: DB, mp: str, kind: str, say=None) -> bool:
    """Record where `mp` landed after its final click; a kind never seen before is said ONCE privately ("🩹 Poshmark
    now lands on the closet after List — handled."). True when it was new."""
    if not kind or kind == "unknown":
        return False
    seen = loads(db.kv_get(LANDINGS)) or {}
    mine = list(seen.get(mp) or KNOWN_LANDINGS.get(mp, []))
    if kind in mine:
        if mp not in seen:
            seen[mp] = mine
            db.kv_set(LANDINGS, json.dumps(seen))
        return False
    seen[mp] = [*mine, kind]
    db.kv_set(LANDINGS, json.dumps(seen))
    db.log(None, "landing_new", {"mp": mp, "kind": kind})
    where = {"closet": "the closet", "listing": "the new listing", "form": "the same form", "member": "the profile page",
             "success": "its success page"}.get(kind, f"a new page ({kind.split(':', 1)[-1]})")
    text = f"🩹 {LABEL.get(mp, mp)} now lands on {where} after {BUTTON.get(mp, 'the final click')} — handled."
    if say is None:
        from thrift_agent import notify
        say = notify.say
    say(text)
    return True
