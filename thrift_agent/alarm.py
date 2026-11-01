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


def request_text(site: str, step: str | None, error: str, evidence: str | None, waiting: int) -> str:
    """The request for Claude Code, ready to paste."""
    label = LABEL.get(site, site)
    return (f"{label} posting is blocked on the thrift-agent Mac ({waiting} item{'s' if waiting != 1 else ''} waiting). "
            f"Step: {step or 'unknown'}. Error: {error}. Evidence on the Mac: {evidence or 'none kept'}. "
            f"Read the evidence (ssh over Tailscale, read only), find the cause, fix it with tests, deploy with the "
            f"poster idle, run one dry run on {label}, then post the waiting items.")


def text(site: str, since: datetime, waiting: int, step: str | None, error: str, evidence: str | None) -> str:
    label = LABEL.get(site, site)
    where = f'Step "{step}": ' if step else ""
    ev = f" Evidence: {evidence}" if evidence else ""
    return (f"🚨 {label} posting is blocked since {_local(since)} — {waiting} item{'s' if waiting != 1 else ''} "
            f"waiting. {where}{error[:220]}.{ev}\n\nPaste this to Claude Code:\n```\n"
            f"{request_text(site, step, error, evidence, waiting)}\n```")


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
              since: datetime | None = None, at: datetime | None = None, why: str = "blocked") -> bool:
    """Say the alarm for `site` now (subject to the repeat rules). True when it went out."""
    t = at or _now()
    data = _all(db)
    a = data.get(site) or {}
    if not _may_say(a, t):
        return False
    started = since or _when(a.get("since")) or t
    notify.say(text(site, started, health.waiting(db, site), step, error, evidence))
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
    row = db.conn.execute("SELECT detail FROM events WHERE kind IN ('post_failed', 'post_nonitem') AND "
                          "json_extract(detail, '$.mp') = ? ORDER BY rowid DESC LIMIT 1", (mp,)).fetchone()
    shot = (loads(row["detail"]) or {}).get("shot") if row else None
    return f"var/failed/shots/{Path(shot).name}" if shot else None


def tick(db: DB, at: datetime | None = None) -> list[str]:
    """From the poster's housekeeping: every site paused 20 minutes with items waiting (not by the owner, not an account
    page) gets its alarm; a site no longer paused is forgotten (its ✅ line is health.resume's). Returns the sites said."""
    t = at or _now()
    data = _all(db)
    out = []
    for mp in list(data):
        if not health.state(db, mp):
            data.pop(mp)
            db.kv_set(KEY, json.dumps(data))
    for mp, p in (loads(db.kv_get(health.HEALTH)) or {}).items():
        if p.get("owner") or p.get("kind") == "account":
            continue
        since = _when(p.get("since"))
        if since is None or t - since < AFTER or health.waiting(db, mp) <= 0:
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
