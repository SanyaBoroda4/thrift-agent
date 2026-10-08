"""Small shared pieces of thrift-api (WO33): the mode, timestamps, ids, the marketplaces' names, money, the errors a
request can end in. Python 3.12: nothing newer is used anywhere in the package."""
from __future__ import annotations

import email.utils
import math
import os
import re
import secrets
from datetime import datetime, timezone

UTC = timezone.utc
SITES = ("poshmark", "depop", "vinted")
SITE_NAMES = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
SITE_ORDER = {site: n for n, site in enumerate(SITES)}
MODES = ("off", "replay", "live")
DEFAULT_MODE = "replay"


class ApiError(Exception):
    """A request the API refuses; `status` is its HTTP status and the message is shown to the caller."""
    status = 400


class BadRequest(ApiError):
    status = 400


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


def mode() -> str:
    """SALES_MODE: off | replay | live; unset or anything else is replay (nothing reaches the group, no take-down)."""
    value = os.environ.get("SALES_MODE", "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(moment: datetime) -> datetime:
    """A naive datetime is UTC, never the machine's zone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    """The one way a time is stored: UTC to the second, "2026-10-08T14:05:00+00:00" (the Mac's own format), so two
    stored times compare as text."""
    return aware(moment).astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value: object) -> datetime | None:
    """An aware datetime from an ISO-8601 string ("…Z", fractions, an offset; none = UTC) or an RFC 2822 date header;
    None for anything else."""
    if isinstance(value, datetime):
        return aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in "zZ" else text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return aware(moment) if moment is not None else None


def new_id(prefix: str) -> str:
    """"s_" + 12 hex for a sale, "t_" + 12 hex for a task."""
    return f"{prefix}{secrets.token_hex(6)}"


def site_name(marketplace: object) -> str:
    key = str(marketplace or "").strip().lower()
    return SITE_NAMES.get(key) or key.capitalize() or "?"


def number(value: object) -> float | None:
    """A price as a float: a number, or a numeric string ("35", "$35.00", "1,250"); anything else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").removeprefix("US").removeprefix("$").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return float(text)
    return None


def money(value: object) -> str:
    """$35, $1,250, $12.50."""
    amount = number(value)
    if amount is None:
        return "—"
    return f"${int(amount):,}" if amount == int(amount) else f"${amount:,.2f}"


def and_list(words: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    words = [w for w in words if w]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""
