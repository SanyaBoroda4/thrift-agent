"""WO52 §5 (the owner, 2026-11-01): ONE alarm that can't be missed, with the fix request ready. Live: Poshmark's browser
dead for four days, Vinted's form rebuilt, Depop's shipping gone — nobody knew until her 12 items hit all three.

When: a site paused (thrift_agent.health — not by the owner, not a login / CAPTCHA page: those have their own plain
line) for 20 minutes with items waiting; a form check failed twice in a row (formcheck → `raise_for`); a browser that
can't be reopened (health's infra pause). What: ONE private message — "🚨 Vinted posting is blocked since 12:47 — 9
items waiting. Step "brand": Illegal invocation (content/vinted.js:812). Evidence: var/failed/…" — and a ready-to-paste
request for Claude Code in a code block. Repeated only if still blocked 4 hours later, at most twice a day. When it
clears: "✅ Vinted back to normal — posting resumed, 9 items going up." (health.resume says it).

kv `alarms`: {site: {"since", "said": [iso…], "problem"}}."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import health, notify
from thrift_agent.db import DB, loads

KEY = "alarms"
AFTER = timedelta(minutes=20)
REPEAT = timedelta(hours=4)
PER_DAY = 2
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _when(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _local(t: datetime) -> str:
    return t.astimezone().strftime("%I:%M %p").lstrip("0")


def request_text(site: str, step: str | None, error: str, evidence: str | None, waiting: int,
                 why: str = "blocked", label: str | None = None) -> str:
    """The request for Claude Code, ready to paste."""
    label = label or LABEL.get(site, site)
    if why == "unconfirmed":
        return (f"A {label} listing is still unconfirmed on the thrift-agent Mac: {error}. Evidence on the Mac: "
                f"{evidence or 'none kept'}. Read it (ssh over Tailscale, read only), find why {label}'s own data "
                f"doesn't show the listing (or shows it and our check misses it), fix the check with tests, deploy with "
                f"the poster idle; never press {label}'s publish button again for it unless its data proves it absent.")
    if why == "account":
        return (f"{label} shows a {step or 'login'} page in the Thrift Chrome on the thrift-agent Mac ({waiting} "
                f"item{'s' if waiting != 1 else ''} waiting): {error}. Evidence on the Mac: {evidence or 'none kept'}. "
                f"Read it (ssh over Tailscale, read only) and check that the page really is one (a hidden element "
                f"must never count); if it is, the owner acts on the Mac — never type credentials or solve it.")
    return (f"{label} posting is blocked on the thrift-agent Mac ({waiting} item{'s' if waiting != 1 else ''} waiting). "
            f"Step: {step or 'unknown'}. Error: {error}. Evidence on the Mac: {evidence or 'none kept'}. "
            f"Read the evidence (ssh over Tailscale, read only), find the cause, fix it with tests, deploy with the "
            f"poster idle, run one dry run on {label}, then post the waiting items.")


def text(site: str, since: datetime, waiting: int, step: str | None, error: str, evidence: str | None,
         why: str = "blocked", label: str | None = None) -> str:
    label = label or LABEL.get(site, site)
    where = f'Step "{step}": ' if step else ""
    ev = f" Evidence: {evidence}" if evidence else ""
    items = f"{waiting} item{'s' if waiting != 1 else ''} waiting"
    if why == "unconfirmed":
        head = f"🚨 {label}: a listing is still not confirmed since {_local(since)}. {error[:300]}.{ev}"
    elif why == "account":
        head = (f"🚨 {label} has shown a {step or 'login'} page since {_local(since)} — {items}. {error[:220]}.{ev} "
                f"Only you can act on it, on the Mac.")
    else:
        head = f"🚨 {label} posting is blocked since {_local(since)} — {items}. {where}{error[:220]}.{ev}"
    return (f"{head}\n\nPaste this to Claude Code:\n```\n"
            f"{request_text(site, step, error, evidence, waiting, why, label)}\n```")


def _all(db: DB) -> dict:
    return loads(db.kv_get(KEY)) or {}


def _may_say(a: dict, t: datetime) -> bool:
    said = [x for x in (_when(s) for s in a.get("said") or []) if x is not None]
    if not said:
        return True
    if t - max(said) < REPEAT:
        return False
    return sum(1 for x in said if t - x < timedelta(days=1)) < PER_DAY


def raise_for(db: DB, site: str, error: str, *, step: str | None = None, evidence: str | None = None,
              since: datetime | None = None, at: datetime | None = None, why: str = "blocked",
              label: str | None = None) -> bool:
    """Say the alarm for `site` now (subject to the repeat rules). True when it went out. WO53: `why` "unconfirmed"
    (one listing still unknown 2 hours after the click: `site` is "<mp>:verify:<item>", `label` the site's name) and
    "account" (a login / CAPTCHA page with items waiting) have their own words."""
    t = at or _now()
    data = _all(db)
    a = data.get(site) or {}
    if not _may_say(a, t):
        return False
    started = since or _when(a.get("since")) or t
    mp = site.split(":", 1)[0]
    notify.say(text(site, started, health.waiting(db, mp), step, error, evidence, why, label))
    data[site] = {**a, "since": started.isoformat(timespec="seconds"), "why": why,
                  "said": [*(a.get("said") or []), t.isoformat(timespec="seconds")][-6:], "problem": error[:200]}
    db.kv_set(KEY, json.dumps(data))
    db.log(None, "alarm", {"site": site, "why": why, "error": error[:300], "evidence": evidence})
    return True


def _step_error(mp: str, p: dict) -> tuple[str | None, str]:
    reason = re.sub(r"^(?:site|infra|items|failures): ", "", p.get("reason") or "")
    step, rest = health._step_of(mp, reason)
    return (step or None), (rest or reason or "paused")


def _evidence(db: DB, mp: str) -> str | None:
    row = db.conn.execute("SELECT detail FROM events WHERE kind IN ('post_failed', 'post_nonitem', 'crosslist_dry_run') "
                          "AND json_extract(detail, '$.mp') = ? AND json_extract(detail, '$.shot') IS NOT NULL "
                          "ORDER BY rowid DESC LIMIT 1", (mp,)).fetchone()
    shot = (loads(row["detail"]) or {}).get("shot") if row else None
    return f"var/failed/shots/{Path(shot).name}" if shot else None


ACCOUNT_LINE = "account_line_said"   # kv: {site: iso} — the group's plain line for a login / CAPTCHA page, last said
ACCOUNT_LINE_EVERY = timedelta(hours=12)


def account_line(db: DB, mp: str, page: str | None, at: datetime | None = None, force: bool = False,
                 reason: str = "") -> bool:
    """The group's plain action line for a login / CAPTCHA / check page ("Depop needs you to log in on the Mac."): when
    the pause begins (`force`), and again at most every 12 hours while items wait (WO53 §3: said once on Nov 7 at
    4:05 PM, then 31 hours of silence while three items waited). True when it went out."""
    from thrift_agent import crosslist
    t = at or _now()
    said = loads(db.kv_get(ACCOUNT_LINE)) or {}
    last = _when(said.get(mp))
    if not force and last is not None and t - last < ACCOUNT_LINE_EVERY:
        return False
    notify.group(crosslist.ALERTS[crosslist.alert_kind(reason or page or "", page)].format(site=LABEL.get(mp, mp)))
    said[mp] = t.isoformat(timespec="seconds")
    db.kv_set(ACCOUNT_LINE, json.dumps(said))
    return True


def tick(db: DB, at: datetime | None = None, hours_open: bool = True) -> list[str]:
    """From the poster's housekeeping: every site paused 20 minutes with items waiting (not by the owner) gets its
    alarm — WO53 §3: a login / CAPTCHA / check page too, inside the posting hours (its own words: only the owner can act,
    and the group's plain line again, at most every 12 hours); a site no longer paused is forgotten (its ✅ line is
    health.resume's). Returns the sites said."""
    t = at or _now()
    data = _all(db)
    out = []
    for key in list(data):
        if not health.state(db, key.split(":", 1)[0]) and ":verify:" not in key:
            data.pop(key)
            db.kv_set(KEY, json.dumps(data))
    for mp, p in (loads(db.kv_get(health.HEALTH)) or {}).items():
        if p.get("owner"):
            continue
        since = _when(p.get("since"))
        if since is None or t - since < AFTER or health.waiting(db, mp) <= 0:
            continue
        if p.get("kind") == "account":
            if not hours_open:
                continue
            page = p.get("page") or "login"
            if raise_for(db, mp, p.get("reason") or f"a {page} page", step=page, evidence=_evidence(db, mp),
                         since=since, at=t, why="account"):
                account_line(db, mp, page, t, reason=p.get("reason") or "")
                out.append(mp)
            continue
        step, err = _step_error(mp, p)
        if raise_for(db, mp, err, step=step, evidence=_evidence(db, mp), since=since, at=t):
            out.append(mp)
    return out


def form_check_failed(db: DB, site: str, error: str, shot=None, n_waiting: int | None = None) -> bool:
    """WO52 §3's call point: a form check failed twice in a row (or once with items waiting) — the site's alarm."""
    step, rest = health._step_of(site, error or "")
    evidence = f"var/failed/shots/{Path(str(shot)).name}" if shot else None
    return raise_for(db, site, rest or error or "form check failed", step=step or "form check", evidence=evidence,
                     why="form_check")
