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
GREETING = re.compile(r"^(\s*(?:hi|hello|hey|dear)\s+)([^\s,!]{1,40})([,!])", re.I | re.M)
BUYER = re.compile(r"(?i)\b(buyer|purchased by|bought by|sold to|ordered by|offer from|from user)\s*:?\s*@?"
                   r"([A-Za-z0-9_.-]{2,40})")
HANDLE = re.compile(r"(?<![\w/])@([A-Za-z0-9_.]{2,40})")
KEEP = {"poshmark", "depop", "vinted", "the", "a", "your", "you"}


def shop_names() -> list[str]:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "private" / "settings.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    names = []
    for mp in (cfg.get("marketplaces") or {}).values():
        for key in ("shop", "username", "member_id"):
            if (mp or {}).get(key):
                names.append(str(mp[key]))
    return [n for n in names if len(n) >= 4]


def scrub(text: str, shops: list[str]) -> str:
    """The personal data out of one email's text (see the module)."""
    for name in sorted(shops, key=len, reverse=True):
        text = re.sub(re.escape(name), "[shop]", text, flags=re.I)
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub("[phone]", text)
    text = TRACKING.sub("[tracking]", text)
    lines, out, hiding = text.splitlines(), [], 0
    for line in lines:
        if hiding and line.strip():
            out.append("[address]")
            hiding -= 1
            continue
        hiding = 0
        out.append(line)
        if ADDRESS_HEAD.match(line):
            hiding = 5                                  # the address block under its heading, to the blank line
    text = "\n".join(out)
    text = STREET.sub("[street]", text)
    text = CITY_ZIP.sub("[city, state zip]", text)
    text = GREETING.sub(lambda m: f"{m[1]}[name]{m[3]}", text)
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
        path.write_text(f"Subject: {scrub(subject or '', shops)}\nDate: {received}\n\n{scrub(text or '', shops)}\n",
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
