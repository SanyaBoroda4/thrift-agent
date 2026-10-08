"""Ship-by dates and shipping reminders (WO33): pure date logic — nothing is read, stored or sent here.

Each marketplace gives the seller a number of days to ship (`DEFAULT_RULES`, overridden by `shipping.deadlines`):
Poshmark 7 calendar days, Vinted 5 business days, Depop 5 calendar days after the sale's LOCAL date (America/New_York:
a sale at 23:30 local is that day's, whatever the UTC date). A date the sale email states wins over the rule. Business
days are Monday to Friday except the observed US federal holidays. Reminders are due from 09:00 local: the day before
the ship-by date, on it, and once after it; `quiet` tells the night hours (23:00-08:00 local)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from zoneinfo import ZoneInfo

TZ = "America/New_York"
DEFAULT_RULES = {"poshmark": {"kind": "calendar", "days": 7},
                 "vinted": {"kind": "business", "days": 5},
                 "depop": {"kind": "calendar", "days": 5}}
RULE_KINDS = ("calendar", "business")
CLOSED = frozenset({"cancelled", "double_sale", "done"})      # a sale in one of these needs no reminder
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MON, THU = 0, 3


def rules(cfg: dict | None = None) -> dict:
    """The ship-by rule per marketplace: DEFAULT_RULES with the overrides of `cfg`, the `shipping.deadlines` mapping
    ({"vinted": {"days": 4}} keeps Vinted's kind). A new marketplace needs both kind and days; a bad rule is a
    ValueError."""
    out = {mp: dict(rule) for mp, rule in DEFAULT_RULES.items()}
    for mp, rule in (cfg or {}).items():
        key = str(mp).lower()
        if rule is None:
            continue
        if not isinstance(rule, dict):
            raise ValueError(f"shipping.deadlines.{key}: expected {{kind, days}}, got {rule!r}")
        merged = {**out.get(key, {}), **rule}
        if merged.get("kind") not in RULE_KINDS:
            raise ValueError(f"shipping.deadlines.{key}: kind {merged.get('kind')!r} is not calendar or business")
        try:
            merged["days"] = int(merged.get("days"))
        except (TypeError, ValueError):
            raise ValueError(f"shipping.deadlines.{key}: days {merged.get('days')!r} is not a number") from None
        if merged["days"] < 0:
            raise ValueError(f"shipping.deadlines.{key}: days {merged['days']} is below 0")
        out[key] = merged
    return out


def _observed(d: date) -> date:
    # observed: a Saturday holiday is the Friday before, a Sunday one the Monday after
    return d + timedelta(days={5: -1, 6: 1}.get(d.weekday(), 0))


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Monday 0) of the month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


@cache
def _holidays(year: int) -> frozenset[date]:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25),
             date(year + 1, 1, 1)]      # next year's New Year's Day: on a Saturday it is observed on Dec 31 of this one
    may31 = date(year, 5, 31)
    floating = [_nth(year, 1, MON, 3),                          # Martin Luther King Jr. Day
                _nth(year, 2, MON, 3),                          # Washington's Birthday
                may31 - timedelta(days=may31.weekday()),        # Memorial Day, the last Monday of May
                _nth(year, 9, MON, 1),                          # Labor Day
                _nth(year, 10, MON, 2),                         # Columbus Day
                _nth(year, 11, THU, 4)]                         # Thanksgiving
    return frozenset(d for d in [*map(_observed, fixed), *floating] if d.year == year)


def us_federal_holidays(year: int) -> set[date]:
    """The observed dates of the 11 US federal holidays that fall in `year`. A holiday on a Saturday is observed the
    Friday before, on a Sunday the Monday after; New Year's Day on a Saturday is Dec 31, in the year before's set."""
    return set(_holidays(year))


def is_business_day(d: date) -> bool:
    """Monday to Friday and not an observed federal holiday."""
    return d.weekday() < 5 and d not in _holidays(d.year)


def add_business_days(start: date, n: int) -> date:
    """The n-th business day after `start`; `start` itself never counts, whatever day it is."""
    if n < 0:
        raise ValueError(f"n is below 0: {n}")
    d = start
    while n:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def local(now_utc: datetime, tz: str = TZ) -> datetime:
    """The aware local time in `tz`; a naive datetime is taken as UTC (never the machine's own zone)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(ZoneInfo(tz))


def ship_by(marketplace: str, sold_at: datetime, stated: date | None = None, cfg: dict | None = None,
            tz: str = TZ) -> tuple[date, str]:
    """The date a sale must ship by and where it comes from: the date the sale email states ("email"), else the
    marketplace's rule counted from the sale's local date in `tz` ("rule"). An unknown marketplace is a ValueError."""
    rule = rules(cfg).get(marketplace.lower())
    if rule is None:
        raise ValueError(f"no shipping deadline rule for marketplace {marketplace!r}")
    if stated is not None:
        return _day(stated), "email"
    sold = local(sold_at, tz).date()
    if rule["kind"] == "business":
        return add_business_days(sold, rule["days"]), "rule"
    return sold + timedelta(days=rule["days"]), "rule"


def _day(value: date | str) -> date:
    """A date, or the date of an ISO string ("2026-10-09")."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _hm(text: str) -> time:
    """A time of day written "09:00" (or "9:00")."""
    try:
        hours, minutes = str(text).split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise ValueError(f"not a time of day (HH:MM): {text!r}") from None


def reminders_due(now_utc: datetime, sales: Iterable[dict], sent: set[tuple[str, str]], tz: str = TZ,
                  at: str = "09:00") -> list[tuple[str, str]]:
    """The reminders to send now, (sale id, kind) in the order of `sales`, for the open sales only (not shipped, not
    cancelled / double_sale / done). From `at` local time: "day_before" on the day before the ship-by date, "due_today"
    on it, "overdue" on any day after; each kind once per sale (`sent`). A day missed is not made up: on the due day
    only "due_today" can come, after it only "overdue"."""
    now = local(now_utc, tz)
    if now.time() < _hm(at):
        return []
    due = []
    for sale in sales:
        if sale.get("shipped_at") or sale.get("status") in CLOSED or not sale.get("ship_by"):
            continue
        left = (_day(sale["ship_by"]) - now.date()).days
        if left > 1:
            continue
        kind = "day_before" if left == 1 else "due_today" if left == 0 else "overdue"
        if (str(sale["id"]), kind) not in sent:
            due.append((str(sale["id"]), kind))
    return due


def quiet(now_utc: datetime, tz: str = TZ, start: str = "23:00", end: str = "08:00") -> bool:
    """True inside the quiet hours, from `start` (included) to `end` (not) local time; the window may cross midnight."""
    t, s, e = local(now_utc, tz).time(), _hm(start), _hm(end)
    return s <= t < e if s <= e else (t >= s or t < e)


def fmt_day(d: date) -> str:
    """The day as "Thu Oct 9": English names whatever the machine's locale."""
    return f"{DAY_NAMES[d.weekday()]} {MONTH_NAMES[d.month - 1]} {d.day}"
