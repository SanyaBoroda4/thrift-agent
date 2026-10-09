"""The owner's marketplace emails for building the parsers (WO33 C2), on the PC, after the Apps Script's `dumpSamples()`:

    python deploy/azure/samples.py pull     # thrift-api's `samples` table → private/email_samples/<site>/*.txt, scrubbed
    python deploy/azure/samples.py drop     # the table emptied (WO33 Part I step 8, once the parsers ship)

Scrubbed before anything is written: e-mail addresses, phone numbers, the lines of a shipping address (after "Ship to",
"Shipping address", … and any street / city-state-ZIP line), greetings' names, buyer usernames, tracking numbers and
the shop's own names (from private/settings.yaml). Read every file before it is committed to the private repo — a
heuristic misses things. Needs this PC on the server's firewall (`provision.py infra` adds thrift-setup-tmp; `cleanup`
removes it) and var/azure/ from provision.py."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var" / "azure"
OUT = ROOT / "private" / "email_samples"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
TRACKING = re.compile(r"\b(?:\d[ ]?){18,34}\b|\b1Z[0-9A-Z]{16}\b|\b[A-Z]{2}\d{9}US\b")
STREET = re.compile(r"^\s*\d{1,6}\s+\S.*\b(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|ct|court|"
                    r"pl|place|ter|terrace|pkwy|parkway|hwy|highway|cir|circle|apt|suite|unit)\b\.?.*$", re.I | re.M)
CITY_ZIP = re.compile(r"^.*\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b.*$", re.M)
ADDRESS_HEAD = re.compile(r"^\s*(ship(?:ping)?\s+(?:to|address)|deliver(?:y)?\s+(?:to|address)|send\s+to|buyer'?s?\s+"
                          r"address|address)\s*:?\s*$", re.I)
SECTION = re.compile(r"^\s*(payment|order|item|what you|next steps|shipping (?:details|method)|summary|"
                     r"total|price|your earnings|track|view)\b", re.I)
GREETING = re.compile(r"^(\s*(?:hi|hello|hey|dear)\s+)([^\s,!]{1,40})([,!])", re.I | re.M)
BUYER = re.compile(r"(?i)\b(buyer|purchased by|bought by|sold to|ordered by|offer from|from user)\s*:?\s*@?"
                   r"([A-Za-z0-9_.-]{2,40})")
HANDLE = re.compile(r"(?<![\w/])@([A-Za-z0-9_.]{2,40})")
KEEP = {"poshmark", "depop", "vinted", "the", "a", "your", "you"}


def shop_names() -> list[str]:
    """The seller's own names, to be replaced by [shop]: the shops in private/settings.yaml, plus SHOP_EXTRA (names a
    marketplace prints that the settings don't hold, e.g. a Vinted username) — from private/scrub.yaml if present."""
    names = []
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "private" / "settings.yaml").read_text(encoding="utf-8")) or {}
        for mp in (cfg.get("marketplaces") or {}).values():
            for key in ("shop", "username", "member_id"):
                if (mp or {}).get(key):
                    names.append(str(mp[key]))
        extra = yaml.safe_load((ROOT / "private" / "scrub.yaml").read_text(encoding="utf-8")) or {}
        names += [str(x) for x in extra.get("names") or []]
    except (OSError, ValueError):
        pass
    return [n for n in names if len(n) >= 4]


def buyer_handles(subject: str, text: str) -> set[str]:
    """The buyer's usernames an email names, to be replaced everywhere in it: "@<handle>" anywhere, Vinted's
    "<handle> has bought", Depop's line after "Profile Picture", Poshmark's "just sold to @<handle>"."""
    found = set(re.findall(r"@([A-Za-z0-9_.]{2,40})", f"{subject}\n{text}"))
    found |= set(re.findall(r"^\s*([A-Za-z0-9_.]{3,40}) has bought\b", text, re.M))
    for m in re.finditer(r"Profile Picture\s*\n+\s*([A-Za-z0-9_.]{3,40})\s*$", text, re.M):
        found.add(m.group(1))
    return {h for h in found if h.lower() not in KEEP and not h.lower().endswith(".com")}


def scrub(text: str, shops: list[str], subject: str = "") -> str:
    """The personal data out of one email's text (see the module). The text is first made line-shaped (HTML
    entities decoded, invisible spacers dropped, runs of spaces as line breaks), so an address block is lines."""
    import html as _html
    text = _html.unescape(text or "")
    text = re.sub(r"[͏­​-‍⁠﻿]", "", text)
    text = re.sub(r"[ \t]{3,}", "\n", text)
    handles = buyer_handles(subject, text)
    text = EMAIL.sub("[email]", text)
    for name in sorted(shops, key=len, reverse=True):
        text = re.sub(re.escape(name), "[shop]", text, flags=re.I)
    for h in sorted(handles, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.]){re.escape(h)}(?![\w])", "[user]", text, flags=re.I)
    text = PHONE.sub("[phone]", text)
    text = TRACKING.sub("[tracking]", text)
    lines, out, hiding, hiding_kind = text.splitlines(), [], 0, "address"
    for line in lines:
        if hiding and not line.strip():
            out.append(line)                            # a blank line inside the block (Depop spaces its lines)
            continue
        if hiding and SECTION.match(line):          # the next section ends the block
            hiding = 0
        if hiding:
            out.append("[address]" if hiding_kind == "address" else "[buyer]")
            hiding -= 1
            continue
        out.append(line)
        if ADDRESS_HEAD.match(line):
            hiding, hiding_kind = 6, "address"          # name, street, city, state, zip, country: up to the blank line
        elif re.match(r"^\s*buyer\s*:?\s*$", line, re.I):
            hiding, hiding_kind = 2, "buyer"            # Poshmark's "Buyer" block: the name, then the @handle
    text = "\n".join(out)
    text = STREET.sub("[street]", text)
    text = CITY_ZIP.sub("[city, state zip]", text)
    text = GREETING.sub(lambda m: f"{m[1]}[name]{m[3]}", text)
    text = re.sub(r"^(\s*)[A-Za-z0-9_.]{3,40}(, your sale is complete)", r"\1[shop]\2", text, flags=re.M)
    text = BUYER.sub(lambda m: m[0] if m[2].lower() in KEEP else m[0].replace(m[2], "[buyer]"), text)
    text = HANDLE.sub(lambda m: m[0] if m[1].lower() in KEEP else "@[user]", text)
    return text


def connect():
    import psycopg
    st = json.loads((VAR / "state.json").read_text(encoding="utf-8"))
    sec = json.loads((VAR / "secrets.json").read_text(encoding="utf-8"))
    return psycopg.connect(host=st["pg_fqdn"], dbname="thrift", user="thrift_app",
                           password=sec["thrift_app_password"], sslmode="require")


def pull() -> None:
    shops = shop_names()
    with connect() as conn:
        rows = conn.execute("SELECT message_id, marketplace, subject, received_at, text FROM samples "
                            "ORDER BY received_at").fetchall()
    index = []
    for message_id, mp, subject, received, text in rows:
        site = (mp or "unknown").lower()
        name = f"{str(received)[:10]}_{hashlib.sha1(message_id.encode()).hexdigest()[:8]}.txt"
        path = OUT / site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Subject: {scrub(subject or '', shops)}\nDate: {received}\n\n"
                        f"{scrub(text or '', shops, subject or '')}\n",
                        encoding="utf-8")
        index.append({"file": f"{site}/{name}", "subject": scrub(subject or "", shops), "received": str(received)})
    (OUT / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    sites = sorted({i["file"].split("/")[0] for i in index})
    print(f"{len(rows)} samples → {OUT.relative_to(ROOT)} ({', '.join(sites) or 'none'}); read them before committing")


def drop() -> None:
    with connect() as conn:
        n = conn.execute("DELETE FROM samples").rowcount
    print(f"samples table emptied ({n} rows)")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    {"pull": pull, "drop": drop}.get(what, lambda: sys.exit(__doc__))()
