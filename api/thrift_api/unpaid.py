"""WO68 — unpaid eBay orders (the owner, 2026-11-14: "automatic reminders so agent will remind to pay for the goods … send
a reminder after 24hr after purchase and then if after 24 more hours still not paid then cancel the deal and relist it
everywhere. Also I want agent to clearly state in 9pm list if the item was paid or not yet.").

eBay's rules, as read on 2026-11-14 (the report quotes them): eBay emails the buyer itself 24 h and 48 h after the
purchase; the seller may cancel for non-payment only after 4 calendar days (the Edikted dress's order page, sold Thu
Nov 12 6:37 PM: "You can cancel the order for non-payment beginning Nov 17"), and with the true reason — "buyer hasn't
paid" gives her no defect, "out of stock" does. So the owner's 48-hour cancel is the first moment eBay allows.

- **Seen** (`read`, eBay's 5-minute work after the Fulfillment read): the Fulfillment API shows only orders whose checkout
  is complete, so an unpaid one is read with the Trading API's GetOrders (the last 10 days, every state; only ids, dates,
  states, the SKU, title and price — never the buyer). A line whose SKU is one of our items and that isn't paid is a
  sale with `payment = unpaid`, its `committed_at` (eBay's CreatedTime), `cancel_from` and `pay_ref` (eBay's
  "<item>-<transaction>", which the paid order carries again as legacyItemId + lineItemId): recorded once — the
  take-downs elsewhere as for any sale, and the group's "🛒 Sold on eBay: <title> — $44 · not paid yet, don't ship."
  instead of the 💰 line. A sale of ours already recorded (the Mac saw the listing sold) is marked so, with the same line.
  Her own eBay listings are never acted on here (no SKU of ours).
- **Paid** at any point (`mark_paid`: the Fulfillment API's order tied by `pay_ref`, or GetOrders' PaidTime): everything
  stops; "✅ Paid on eBay: <title> — ship by Tue Nov 17." (eBay's own ship-by when the order names it); a normal package
  from then on.
- **The two notes** (`notes`): 24 h after the purchase "Hi! Just a friendly reminder …", 48 h after it the final one
  with the cancel's date and time — through SendInvoice (its CheckoutInstructions; only the order line's id, no buyer
  data), else AddMemberMessageAAQToPartner (the buyer's id read for that one call, never kept or logged). Each claimed in
  `reminders` before it is sent: once, never a third; never in the quiet hours, never 10 PM – 8 AM New York (a note due
  then goes at the next allowed minute); one that falls due when the next is due already gives way to it. ONE private
  line each: "💳 Reminder sent to the eBay buyer of <title>."
- **The day-4 cancel** (`cancel_due`): eBay's own automatic cancel is turned on at its minimum, 4 days, without its relist
  (`set_preferences`: OptInStatus, DelayBeforeOpeningDispute 4, AutoRelist false — read back by GetUserPreferences). As a
  backstop, an hour after `cancel_from` an order still unpaid is cancelled here with reason UNPAID_CONTRACT ("buyer
  hasn't paid" — `cancel_unpaid` takes no reason: no other can be sent) through eBay's Cancellation API, never before
  `cancel_from` nor 4 days after the purchase (`TooEarly`). Without its scopes (sell.commitment.read: to find the unpaid
  order; sell.cancellation: to cancel it) ONE private message asks the owner for them (`ask_scopes`, the existing consent
  link) and, when the cancel is due, ONE line with the taps.
- **Cancelled** (by eBay or here; GetOrders shows it): the sale is cancelled (relist why "unpaid") — the existing relist
  rule puts the item back on every site it came down from, eBay included, at each site's last price, each with its
  "♻️ Back on …" line; the group hears "↩️ <title>: the eBay buyer didn't pay — cancelled and relisted everywhere."

`cancel_from` (`cancel_from`): eBay names none in its data, so it is 5 days after the purchase — the moment that comes
after eBay's "4 calendar days" however they are counted (Nov 12 6:37 PM → Tue Nov 17 6:37 PM, the day eBay's own page
names); the backstop and the notes say that moment. Nothing here logs or keeps a buyer's id, name, address or message."""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from . import core, deadlines, ebay, match, quiet, util
from .db import Database
from .util import ApiError, iso, parse_time

log = logging.getLogger(__name__)

SITE = util.EBAY
TRADING_URL = {"production": "https://api.ebay.com/ws/api.dll", "sandbox": "https://api.sandbox.ebay.com/ws/api.dll"}
TRADING_VERSION = "1451"
NS = "urn:ebay:apis:eBLBaseComponents"
READ_BACK = timedelta(days=10)          # GetOrders looks this far back (an unpaid order is cancelled by day 5)
PAGES = 5
NOTE_1_AFTER = timedelta(hours=24)
NOTE_2_AFTER = timedelta(hours=48)
CANCEL_AFTER = timedelta(days=5)        # see the module: after eBay's 4 calendar days however counted
MIN_CANCEL = timedelta(days=4)          # never before this after the purchase, whatever is stored
BACKSTOP = timedelta(hours=1)           # eBay's own cancel gets this long first
CANCEL_EVERY = timedelta(hours=1)       # a backstop that didn't work is tried again this often
TAPS_AFTER = timedelta(hours=3)         # … and still not cancelled this long after it was due: the taps, once
NIGHT_FROM, NIGHT_TO = 22, 8            # no note to a buyer 10 PM – 8 AM New York
NOTE_TRIES = 3
NOTE_1, NOTE_2, NOTE_1_SKIPPED = "unpaid_note_1", "unpaid_note_2", "unpaid_note_1_skipped"   # reminders' kinds
NOTES_SENT = (NOTE_1, NOTE_2)
EBAY_REMINDS = (timedelta(hours=24), timedelta(hours=48))    # eBay's own emails to the buyer
NOTE_FAILED = "unpaid_note_failed:"     # settings: + sale id + kind — {"tries", "what"}
CANCEL_STATE = "unpaid_cancel:"         # settings: + sale id — {"tries", "at", "what", "taps_said"}
SCOPES_ASKED = "ebay_scopes_asked"      # settings: the scopes the one private message asked for
SCOPES_NEEDED = (f"{ebay.APP_SCOPE}/sell.commitment.read", f"{ebay.APP_SCOPE}/sell.cancellation")
PREFS = {"OptInStatus": "true", "DelayBeforeOpeningDispute": "4", "AutoRelist": "false"}

NOTE_1_TEXT = ("Hi! Just a friendly reminder that your order for {title} is waiting for payment. We'll ship it within "
               "2 business days of payment. Thank you!")
NOTE_2_TEXT = ("Hi! Your order for {title} is still unpaid. If payment isn't received by {deadline}, the order will be "
               "cancelled. Thanks!")
SUBJECT = "Your order is waiting for payment"
SOLD_LINE = "🛒 Sold on eBay: {title}{price} · not paid yet, don't ship."
PAID_LINE = "✅ Paid on eBay: {title} — ship by {day}."
CANCELLED_LINE = "↩️ {title}: the eBay buyer didn't pay — cancelled and relisted everywhere."
SENT_LINE = {NOTE_1: "💳 Reminder sent to the eBay buyer of {title}.",
             NOTE_2: "💳 Final reminder sent to the eBay buyer of {title} — it cancels {deadline} if still unpaid."}
UNSURE_LINE = ("💳 The {which} to the eBay buyer of {title} may not have reached eBay (no answer) — not sent again; "
               "eBay's own reminders go on.")
NOTE_FAILED_LINE = ("⚠️ Couldn't send the eBay buyer of {title} the {which} ({what}) — eBay's own reminders go on; it "
                    "cancels {deadline} if still unpaid.")
TAPS_LINE = ("⚠️ {title} — the eBay buyer hasn't paid and the order can be cancelled since {since}, but {why}. 1 minute in "
             "the eBay app: Selling → Orders → {title} → Cancel order → Buyer hasn't paid. It is then relisted "
             "everywhere by itself.")
SCOPES_LINE = ("🔐 eBay: one more permission for the agent — to cancel an unpaid order as “buyer hasn't paid” when eBay "
               "itself doesn't (an unpaid order can be cancelled after 4 days). It adds exactly {n}: {names}. "
               "1 minute: {how}. Everything else keeps working meanwhile.")
SCOPE_WHY = {"sell.commitment.read": "sell.commitment.read (to find the unpaid order)",
             "sell.cancellation": "sell.cancellation (to cancel it as “buyer hasn't paid”)"}

# WO68 §6 (the owner, 2026-11-14): the Edikted Crochet Knit Mini Dress — sold Thu Nov 12, 6:37 PM, unpaid. Its 24 h and
# 48 h moments are past and she reminded the buyer by hand: the agent's FINAL note goes once, after 2:00 PM Sun Nov 15
# (its first note is not sent); no new group line (its sale was announced on Nov 12, and she knows).
SEEDED = {"407358644533-10086346752915": {"notes_after": "2026-11-15T19:00:00+00:00", "skip_first": True,
                                          "quiet": True}}


class TradingError(ApiError):
    """eBay's Trading API answered Failure: its error codes (never a message, which can name a buyer)."""
    status = 502

    def __init__(self, call: str, codes: list[str]):
        super().__init__(f"{call}: eBay error {', '.join(codes) or 'unnamed'}")
        self.codes = codes


class TooEarly(Exception):
    """The guard: no cancel before eBay allows it (`cancel_from`, and never within 4 days of the purchase)."""


class NoScope(Exception):
    """The Cancellation / Item Commitment API refuses the token: its scopes aren't in the consent yet."""


# --- the Trading API (XML) -----------------------------------------------------------------------------------------

def _x(text: object) -> str:
    return escape(str(text or ""))


def trading(token: str, call: str, body: str) -> ET.Element:
    """One Trading API call with the OAuth user token (X-EBAY-API-IAF-TOKEN): the answer's root, its namespace dropped.
    eBay's Failure raises TradingError with its codes; HTTP other than 200: ebay.Refused."""
    xml = f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="{NS}">{body}</{call}Request>'
    status, data = ebay._send("POST", TRADING_URL[ebay.environment()], {
        "X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_VERSION, "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml"}, xml.encode("utf-8"))
    if status != 200:
        raise ebay.Refused(f"HTTP {status}")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ebay.Refused(f"{call}: an answer that isn't XML") from None
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    if root.findtext("Ack") not in ("Success", "Warning"):
        codes = [ebay._code(e.findtext("ErrorCode"), 12) for e in root.findall("Errors")
                 if (e.findtext("SeverityCode") or "Error") == "Error"]
        raise TradingError(call, [c for c in codes if c])
    return root


def _stamp(moment: datetime) -> str:
    return util.aware(moment).astimezone(util.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line_state(order: ET.Element) -> str:
    """paid | cancelled | cancelling | unpaid — of one GetOrders order (its lines share it)."""
    status = (order.findtext("OrderStatus") or "").strip()
    cancel = (order.findtext("CancelStatus") or "").strip()
    if status in ("Cancelled", "Inactive") or cancel.startswith("CancelClosed") or cancel == "CancelComplete":
        return "cancelled"
    if order.findtext("PaidTime") or (order.findtext("CheckoutStatus/Status") or "") == "Complete" \
            or status in ("Completed", "Shipped"):
        return "paid"
    if cancel in ("CancelPending", "CancelRequested") or status == "CancelPending":
        return "cancelling"
    return "unpaid"


def order_lines(root: ET.Element) -> list[dict]:
    """GetOrders' lines — ONLY: the line's id, the item and transaction ids, the SKU, title and price, the order's
    creation and payment times and its state (never BuyerUserID, ShippingAddress, a message or anything else)."""
    out = []
    for order in root.findall("OrderArray/Order"):
        state = line_state(order)
        created, paid = parse_time(order.findtext("CreatedTime")), parse_time(order.findtext("PaidTime"))
        for t in order.findall("TransactionArray/Transaction"):
            item_id, tx = ebay._code(t.findtext("Item/ItemID"), 20), ebay._code(t.findtext("TransactionID"), 20)
            ref = ebay._code(t.findtext("OrderLineItemID"), 44) or (f"{item_id}-{tx}" if item_id and tx else "")
            if not ref or not item_id:
                continue
            out.append({"ref": ref, "item_id": item_id, "order_id": ebay._code(order.findtext("OrderID"), 44),
                        "sku": (t.findtext("Item/SKU") or "").strip()[:64] or None,
                        "title": (t.findtext("Item/Title") or "").strip()[:200] or None,
                        "price": util.number(t.findtext("TransactionPrice")),
                        "created": parse_time(t.findtext("CreatedDate")) or created, "paid_at": paid, "state": state})
    return out


def fetch_lines(token: str, now: datetime) -> list[dict]:
    """The seller's orders created in the last 10 days, every state, page by page."""
    lines: list[dict] = []
    for page in range(1, PAGES + 1):
        root = trading(token, "GetOrders", f"<CreateTimeFrom>{_stamp(now - READ_BACK)}</CreateTimeFrom>"
                                           f"<CreateTimeTo>{_stamp(now)}</CreateTimeTo><OrderRole>Seller</OrderRole>"
                                           "<OrderStatus>All</OrderStatus><Pagination><EntriesPerPage>100"
                                           f"</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>")
        lines += order_lines(root)
        if (root.findtext("HasMoreOrders") or "false").lower() != "true":
            break
    return lines


def preferences(token: str) -> dict:
    """GetUserPreferences' unpaid-item preferences: {"opt_in", "delay_days", "auto_relist", "excluded"} (None: unset)."""
    root = trading(token, "GetUserPreferences", "<ShowUnpaidItemAssistancePreference>true"
                                                "</ShowUnpaidItemAssistancePreference>")
    pref = root.find("UnpaidItemAssistancePreferences")

    def flag(name: str) -> bool | None:
        value = pref.findtext(name) if pref is not None else None
        return None if value is None else value.strip().lower() == "true"
    delay = pref.findtext("DelayBeforeOpeningDispute") if pref is not None else None
    return {"opt_in": flag("OptInStatus"), "delay_days": int(delay) if delay and delay.strip().isdigit() else None,
            "auto_relist": flag("AutoRelist"),
            "excluded": len(pref.findall("ExcludedUser")) if pref is not None else 0}


def wanted(prefs: dict) -> bool:
    return prefs.get("opt_in") is True and prefs.get("delay_days") == 4 and prefs.get("auto_relist") is False


def set_preferences(token: str) -> dict:
    """eBay's own automatic cancel at its minimum, 4 days, without its relist (ours relists every site), then read back.
    eBay: a preference set can't be changed again for 7 days, and may take up to 7 days to act — so this is called once,
    by hand (the report), never by a timer."""
    fields = "".join(f"<{k}>{v}</{k}>" for k, v in PREFS.items())
    trading(token, "SetUserPreferences", f"<UnpaidItemAssistancePreferences>{fields}</UnpaidItemAssistancePreferences>")
    return preferences(token)


# --- the moments ---------------------------------------------------------------------------------------------------

def cancel_from(committed: datetime) -> datetime:
    return util.aware(committed) + CANCEL_AFTER


def allowed_from(sale: dict) -> datetime | None:
    """The first moment a cancel may be sent: the stored `cancel_from`, never earlier than 4 days after the purchase."""
    committed = parse_time(sale.get("committed_at"))
    if committed is None:
        return None
    stored = parse_time(sale.get("cancel_from")) or cancel_from(committed)
    return max(stored, committed + MIN_CANCEL)


def deadline_text(moment: datetime) -> str:
    """"Tuesday, November 17 at 6:37 PM ET" — for the buyer."""
    local = deadlines.local(moment)
    hm = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return f"{local.strftime('%A')}, {local.strftime('%B')} {local.day} at {hm} ET"


def short_when(moment: datetime) -> str:
    """"Tue Nov 17 6:37 PM" — the owner's zone."""
    local = deadlines.local(moment)
    hm = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return f"{deadlines.fmt_day(local.date())} {hm}"


def may_write(now: datetime, q: quiet.Quiet) -> bool:
    """A note to a buyer: never in the quiet hours, never 10 PM – 8 AM New York."""
    hour = deadlines.local(now).hour
    return not (hour >= NIGHT_FROM or hour < NIGHT_TO) and not quiet.is_quiet(now, q)


def reminders_count(db: Database, sale: dict, now: datetime) -> int:
    """The reminders the buyer has had: eBay's own (24 h, 48 h after the purchase) and the agent's notes sent."""
    committed = parse_time(sale.get("committed_at"))
    own = sum(1 for after in EBAY_REMINDS if committed is not None and now >= committed + after)
    marks = ", ".join("?" * len(NOTES_SENT))
    ours = db.one(f"SELECT COUNT(*) AS n FROM reminders WHERE sale_id = ? AND kind IN ({marks})",
                  (sale["id"], *NOTES_SENT))
    return own + int(ours["n"] if ours else 0)


def _title(db: Database, sale: dict) -> str:
    return " ".join(str(sale.get("title_seen") or "").split()) or core._title(db, sale.get("item_id"), None,
                                                                              sale.get("listing_id"))


# --- seen, paid, cancelled -----------------------------------------------------------------------------------------

def _sale_for(db: Database, line: dict, item_id: str) -> dict | None:
    """Our sale of this order line: by its `pay_ref`; else the item's standing eBay sale without an order (the Mac saw
    the listing sold) recorded within 3 days of the purchase."""
    row = db.one("SELECT * FROM sales WHERE marketplace = ? AND pay_ref = ? AND status <> 'merged' "
                 "ORDER BY created_at LIMIT 1", (SITE, line["ref"]))
    if row:
        return row
    for row in db.query("SELECT * FROM sales WHERE marketplace = ? AND item_id = ? AND status NOT IN ('cancelled', "
                        "'merged') AND (order_id IS NULL OR order_id = '') AND (pay_ref IS NULL OR pay_ref = '') "
                        "ORDER BY created_at DESC", (SITE, item_id)):
        sold = parse_time(row["sold_at"])
        if sold is None or line["created"] is None or abs(sold - line["created"]) <= timedelta(days=3):
            return row
    return None


def _flag(db: Database, sale: dict, line: dict, now: datetime, mode: str, out: core.Outbox) -> bool:
    """An already-recorded sale of ours found unpaid: marked so (once), its line said (unless seeded quiet)."""
    committed = line["created"] or parse_time(sale.get("sold_at")) or now
    seed = SEEDED.get(line["ref"]) or {}
    recorded = parse_time(sale.get("sold_at"))
    sold = committed if recorded is None or committed < recorded else recorded   # eBay's purchase time, not the Mac's look
    if not db.execute("UPDATE sales SET payment = 'unpaid', committed_at = ?, cancel_from = ?, pay_ref = ?, "
                      "sold_at = ?, title_seen = COALESCE(NULLIF(title_seen, ''), ?) WHERE id = ? AND (payment IS NULL "
                      "OR payment = '')", (iso(committed), iso(cancel_from(committed)), line["ref"], iso(sold),
                                           line["title"], sale["id"])):
        return False
    if seed.get("skip_first"):
        db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) ON CONFLICT (sale_id, kind) DO "
                   "NOTHING", (sale["id"], NOTE_1_SKIPPED, iso(now)))
    if not seed.get("quiet") and mode == "live" and not core.hers(SITE, sale.get("item_id")):
        out.group(SOLD_LINE.format(title=_title(db, {**sale, "title_seen": sale.get("title_seen") or line["title"]}),
                                   price=f" — {util.money(line['price'])}" if line["price"] is not None else ""))
    return True


def _record(db: Database, line: dict, item_id: str, now: datetime, mode: str, out: core.Outbox) -> dict | None:
    """A new unpaid sale of ours: the sale path of any order (take-downs elsewhere), its 🛒 line instead of 💰."""
    message_id = f"{core.MAC_PREFIX}order:{SITE}:{line['ref']}"
    committed = line["created"] or now
    facts = {"title": line["title"], "listing_id": line["item_id"], "sku": line["sku"] or item_id,
             "price": line["price"], "order_id": None, "sold_at": iso(committed)}
    event = {"message_id": message_id, "thread_id": None, "marketplace": SITE, "kind": "SALE",
             "subject": "found unpaid on eBay (GetOrders)", "received_at": iso(committed),
             "parsed_json": json.dumps(facts), "raw_text": None}
    if not core._insert_event(db, event, "new", now):
        return None
    parsed = {**facts, "sold_at": committed, "ship_by_stated": None,
              "unpaid": {"committed_at": iso(committed), "cancel_from": iso(cancel_from(committed)),
                         "pay_ref": line["ref"]}}
    result = core._sale(db, SITE, parsed, message_id, event["subject"], core._handling(db, mode, committed, now), now,
                        out)
    db.execute("UPDATE email_events SET status = ?, attempts = 1 WHERE message_id = ?", (result["status"], message_id))
    if result.get("duplicate"):                       # folded into a sale of this item already open: not a new one
        log.warning("unpaid: %s went into an open sale of its item (%s)", line["ref"], result.get("sale_id"))
        return None
    return db.one("SELECT * FROM sales WHERE id = ?", (result["sale_id"],))


def mark_paid(db: Database, sale: dict, at: datetime | None, ship_by_stated, out: core.Outbox, mode: str) -> bool:
    """Paid: the notes and the cancel stop (payment = paid, once), the ship-by from the payment (eBay's own day when
    the order names it) and "✅ Paid on eBay: <title> — ship by Tue Nov 17." True when it changed now."""
    at = at or util.utcnow()
    if not db.execute("UPDATE sales SET payment = 'paid', paid_at = ? WHERE id = ? AND payment = 'unpaid'",
                      (iso(at), sale["id"])):
        return False
    if ship_by_stated is not None:
        day, source = ship_by_stated, "email"
    else:
        day, source = deadlines.ship_by(SITE, at)
    db.execute("UPDATE sales SET ship_by = ?, ship_by_source = ? WHERE id = ?", (day.isoformat(), source, sale["id"]))
    if mode in core.ACTING and not core.hers(SITE, sale.get("item_id")) and sale.get("status") != "cancelled":
        out.group(PAID_LINE.format(title=_title(db, sale), day=deadlines.fmt_day(day)))
    return True


def _cancelled(db: Database, sale: dict, now: datetime, mode: str, out: core.Outbox) -> bool:
    if sale.get("status") == "cancelled":
        return False
    current = db.one("SELECT * FROM sales WHERE id = ?", (sale["id"],))
    return core.apply_followup(db, current, "CANCELLED", now, "live" if mode == "live" else mode, out)


def read(db: Database, now: datetime, mode: str) -> dict:
    """GetOrders, and each line of ours acted on (see the module). {"read", "lines", "unpaid", "changed"}."""
    token = ebay.access_token(db, "user", now, mode)["access_token"]
    try:
        lines = fetch_lines(token, now)
    except ebay.Refused as e:
        if str(e) == "HTTP 401":
            core.drop_setting(db, ebay.TOKEN + "user")
        raise
    changed, unpaid = [], 0
    for line in lines:
        item_id = match.item_for_sku(db, line["sku"])
        if not item_id:
            continue                                  # her own listing (no SKU of ours): never acted on here
        out = core.Outbox(now, mode)
        with db.tx():
            sale = _sale_for(db, line, item_id)
            state = line["state"]
            if state in ("unpaid", "cancelling"):
                unpaid += 1
                if sale is None:
                    if state == "unpaid" and _record(db, line, item_id, now, mode, out):
                        changed.append(f"{line['ref']}: recorded unpaid")
                elif not sale.get("payment") and sale.get("status") != "cancelled" and _flag(db, sale, line, now,
                                                                                            mode, out):
                    changed.append(f"{line['ref']}: marked unpaid")
            elif sale is not None and sale.get("payment") == "unpaid":
                if state == "paid" and mark_paid(db, sale, line["paid_at"] or now, None, out, mode):
                    changed.append(f"{line['ref']}: paid")
                elif state == "cancelled" and _cancelled(db, sale, now, mode, out):
                    changed.append(f"{line['ref']}: cancelled")
        out.flush()
    if any(c.endswith("recorded unpaid") for c in changed):
        core._ebay_kick(db, now, mode)                # a take-down a new sale made: done from here, at once
    return {"read": True, "lines": len(lines), "unpaid": unpaid, "changed": changed}


# --- the two notes -------------------------------------------------------------------------------------------------

def _open(db: Database) -> list[dict]:
    """Our unpaid sales still open (not cancelled), oldest first."""
    return db.query("SELECT * FROM sales WHERE marketplace = ? AND payment = 'unpaid' AND status NOT IN ('cancelled', "
                    "'merged', 'done') AND item_id IS NOT NULL AND pay_ref IS NOT NULL ORDER BY committed_at, id",
                    (SITE,))


def note_due(db: Database, sale: dict, now: datetime) -> str | None:
    """The note to send now (NOTE_1 | NOTE_2), or None. The first gives way to the second once that is due; never a
    third; none once the cancel is allowed."""
    committed = parse_time(sale.get("committed_at"))
    allowed = allowed_from(sale)
    if committed is None or allowed is None or now >= allowed:
        return None
    hold = parse_time((SEEDED.get(sale.get("pay_ref") or "") or {}).get("notes_after"))
    if hold is not None and now < hold:
        return None
    done = {r["kind"] for r in db.query("SELECT kind FROM reminders WHERE sale_id = ?", (sale["id"],))}
    if NOTE_2 in done:
        return None
    if now >= committed + NOTE_2_AFTER:
        return NOTE_2
    if now >= committed + NOTE_1_AFTER and not done & {NOTE_1, NOTE_1_SKIPPED}:
        return NOTE_1
    return None


def note_text(db: Database, sale: dict, which: str) -> str:
    title = _title(db, sale)
    if which == NOTE_1:
        return NOTE_1_TEXT.format(title=title)
    return NOTE_2_TEXT.format(title=title, deadline=deadline_text(allowed_from(sale)))


def _buyer(token: str, ref: str) -> str | None:
    """The buyer's eBay id of this order line — read only for the one message, never kept, logged or returned further."""
    root = trading(token, "GetOrders", f"<OrderIDArray><OrderID>{_x(ref)}</OrderID></OrderIDArray>"
                                       "<OrderRole>Seller</OrderRole>")
    order = root.find("OrderArray/Order")
    found = (order.findtext("BuyerUserID") or "").strip() if order is not None else ""
    return found or None


def send_note(token: str, sale: dict, text: str) -> str:
    """The note through SendInvoice (the order line's id and the text, no buyer data); refused → as a message to the
    buyer of that item (AddMemberMessageAAQToPartner). "invoice" | "message"; TradingError when both are refused;
    ebay.Unreachable when an answer didn't come (it may have been sent)."""
    ref = str(sale["pay_ref"])
    try:
        trading(token, "SendInvoice", f"<OrderLineItemID>{_x(ref)}</OrderLineItemID>"
                                      f"<CheckoutInstructions>{_x(text[:500])}</CheckoutInstructions>"
                                      "<EmailCopyToSeller>false</EmailCopyToSeller>")
        return "invoice"
    except TradingError:
        pass
    buyer = _buyer(token, ref)
    if not buyer:
        raise TradingError("AddMemberMessageAAQToPartner", ["no buyer on the order"])
    item_id = ref.split("-", 1)[0]
    trading(token, "AddMemberMessageAAQToPartner",
            f"<ItemID>{_x(item_id)}</ItemID><MemberMessage><Subject>{_x(SUBJECT)}</Subject>"
            f"<Body>{_x(text[:2000])}</Body><QuestionType>General</QuestionType>"
            f"<RecipientID>{_x(buyer)}</RecipientID></MemberMessage>")
    return "message"


def notes(db: Database, now: datetime, mode: str, q: quiet.Quiet | None = None) -> list[dict]:
    """Each note due now, claimed in `reminders` and then sent (see the module). Live only. [{sale, note, how}]."""
    if mode != "live":
        return []
    q = q or quiet.read(db)
    if not may_write(now, q):
        return []
    done = []
    for sale in _open(db):
        which = note_due(db, sale, now)
        if which is None:
            continue
        with db.tx():
            if not db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) ON CONFLICT "
                              "(sale_id, kind) DO NOTHING", (sale["id"], which, iso(now))):
                continue                              # another run has it
            if which == NOTE_2:
                db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) ON CONFLICT (sale_id, kind) "
                           "DO NOTHING", (sale["id"], NOTE_1_SKIPPED, iso(now)))
        title, deadline = _title(db, sale), short_when(allowed_from(sale))
        named = "reminder" if which == NOTE_1 else "final reminder"
        out = core.Outbox(now, mode)
        try:
            token = ebay.access_token(db, "user", now, mode)["access_token"]
            how = send_note(token, sale, note_text(db, sale, which))
            out.ops(SENT_LINE[which].format(title=title, deadline=deadline))
        except ebay.Unreachable:
            how = "unknown"                           # it may have gone: never sent again
            out.ops(UNSURE_LINE.format(which=named, title=title))
        except (TradingError, ebay.Refused, ebay.NotConnected, ApiError) as e:
            how = "failed"
            key = f"{NOTE_FAILED}{sale['id']}:{which}"
            state = core._loads(core.setting(db, key)) or {"tries": 0}
            state.update(tries=int(state.get("tries") or 0) + 1, what=str(e)[:120])
            with db.tx():
                core.put_setting(db, key, json.dumps(state))
                if state["tries"] < NOTE_TRIES:       # nothing went: the claim is given back, tried at the next run
                    db.execute("DELETE FROM reminders WHERE sale_id = ? AND kind = ?", (sale["id"], which))
                else:
                    out.ops(NOTE_FAILED_LINE.format(title=title, which=named, what=state["what"], deadline=deadline))
        out.flush()
        log.info("unpaid: %s %s for %s", which, how, sale["id"])
        done.append({"sale": sale["id"], "note": which, "how": how})
    return done


# --- the day-4 cancel (the backstop) -------------------------------------------------------------------------------

Q_COMMITMENTS = ("query UnpaidCommitments($input: ItemCommitmentsInput!) { itemCommitments { sellerItemCommitments("
                 "input: $input) { itemCommitments { itemCommitmentId createdAt lineItem { item { itemId } "
                 "itemSku { sellerSku } } } } } }")
M_CANCEL = ("mutation CancelUnpaid($input: CreateItemCommitmentCancellationInput!) { createItemCommitmentCancellation("
            "input: $input) { __typename ... on CreateItemCommitmentCancellationSuccess { itemCommitmentCancellation { "
            "cancellationId } } ... on ItemCommitmentCancellationEligibilityError { errorCode ineligibilityReasons { "
            "code } } ... on ItemCommitmentNotFoundError { errorCode } } }")
REASON = "UNPAID_CONTRACT"              # the ONE reason ever sent: "buyer hasn't paid" (no defect for her)


def _graphql(token: str, query: str, variables: dict) -> dict:
    try:
        return ebay._graphql(token, query, variables)
    except ebay.Refused as e:
        if "FORBIDDEN" in str(e) or str(e) == "HTTP 403":
            raise NoScope(str(e)) from None
        raise


def commitment_id(token: str, sale: dict) -> str | None:
    """eBay's id of this unpaid order (an "item commitment"): the seller's active commitments on its listing, created
    within a day of the purchase."""
    item_id = str(sale["pay_ref"]).split("-", 1)[0]
    committed = parse_time(sale.get("committed_at")) or util.utcnow()
    data = _graphql(token, Q_COMMITMENTS, {"input": {"itemCommitmentsFirstPage": {"maxPageSize": 20, "filter": {
        "dateRange": {"dateType": "CREATED", "dateTimeRange": {"startDateTime": _stamp(committed - timedelta(days=1)),
                                                               "endDateTime": _stamp(committed + timedelta(days=1))}},
        "filter": {"includeActive": True, "listingId": item_id},
        "sort": {"sortField": "CREATED_AT", "sortOrder": "DESC"}}}}})
    found = (((data.get("itemCommitments") or {}).get("sellerItemCommitments") or {}).get("itemCommitments")) or []
    ours = [c for c in found if isinstance(c, dict) and str(((c.get("lineItem") or {}).get("item") or {})
                                                            .get("itemId") or "") == item_id]
    if len(ours) != 1:
        return None                                   # none, or several: never a guess
    return str(ours[0].get("itemCommitmentId") or "") or None


def cancel_unpaid(token: str, sale: dict, now: datetime) -> str:
    """Cancel this unpaid order as "buyer hasn't paid" — the guard first (TooEarly), the one reason, no relist by eBay.
    Returns eBay's cancellation id; raises NoScope, TooEarly, or ebay.Refused with eBay's codes."""
    allowed = allowed_from(sale)
    if allowed is None or now < allowed:
        raise TooEarly(f"not before {iso(allowed) if allowed else 'a known purchase time'}")
    if sale.get("payment") != "unpaid" or sale.get("status") == "cancelled":
        raise TooEarly("not an unpaid order")
    commitment = commitment_id(token, sale)
    if commitment is None:
        raise ebay.Refused("eBay showed no single unpaid order for it")
    data = _graphql(token, M_CANCEL, {"input": {"itemCommitmentId": commitment,
                                                "contractCancellationReason": REASON, "relistItem": False}})
    out = data.get("createItemCommitmentCancellation") or {}
    if out.get("__typename") == "CreateItemCommitmentCancellationSuccess":
        return str(((out.get("itemCommitmentCancellation") or {}).get("cancellationId")) or "?")
    codes = [ebay._code(r.get("code")) for r in out.get("ineligibilityReasons") or [] if isinstance(r, dict)]
    said = ", ".join(c for c in [ebay._code(out.get("errorCode")), *codes] if c) or "no reason given"
    raise ebay.Refused(f"eBay refused the cancel ({said})")


def missing_scopes(db: Database) -> list[str]:
    consent = ebay._consent(db) or {}
    have = set(str(consent.get("scopes") or "").split())
    return [s for s in SCOPES_NEEDED if s not in have]


def ask_scopes(db: Database, now: datetime, mode: str) -> bool:
    """ONE private message (per missing set): the consent link, naming exactly the scopes the backstop needs and why."""
    missing = missing_scopes(db)
    if not missing or mode != "live" or ebay._consent(db) is None:
        return False
    value = " ".join(missing)
    if not db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                      "excluded.value WHERE settings.value <> excluded.value", (SCOPES_ASKED, value)):
        return False
    names = [SCOPE_WHY.get(s.rsplit("/", 1)[-1], s.rsplit("/", 1)[-1]) for s in missing]
    ebay._say(now, mode, SCOPES_LINE.format(n="two" if len(names) == 2 else "one", names=" and ".join(names),
                                            how=ebay._how()))
    return True


def cancel_due(db: Database, now: datetime, mode: str) -> list[dict]:
    """The backstop (see the module): an hour after `cancel_from`, each order still unpaid cancelled here as "buyer
    hasn't paid"; tried hourly; no scope, or still not cancelled 3 hours after it was due → ONE line with the taps. The
    cancellation itself is seen by the next GetOrders read (then the relist). Live only. [{sale, result}]."""
    if mode != "live":
        return []
    done = []
    for sale in _open(db):
        allowed = allowed_from(sale)
        if allowed is None or now < allowed + BACKSTOP:
            continue
        key = CANCEL_STATE + sale["id"]
        state = core._loads(core.setting(db, key)) or {}
        last = parse_time(state.get("at"))
        if last is not None and now - last < CANCEL_EVERY:
            continue
        state.update(at=iso(now), tries=int(state.get("tries") or 0) + 1)
        why = None
        try:
            token = ebay.access_token(db, "user", now, mode)["access_token"]
            state["cancellation"] = cancel_unpaid(token, sale, now)
            result = "cancelled"
        except NoScope:
            result, why = "no scope", "the agent doesn't have eBay's permission to cancel yet"
            ask_scopes(db, now, mode)
        except TooEarly as e:
            result = f"too early ({e})"
        except (ebay.Refused, ApiError) as e:
            result, why = "refused", f"eBay didn't take the agent's cancel ({str(e)[:120]})"
        state["what"] = result
        out = core.Outbox(now, mode)
        if why and not state.get("taps_said") and (result == "no scope" or now >= allowed + TAPS_AFTER):
            state["taps_said"] = iso(now)
            out.ops(TAPS_LINE.format(title=_title(db, sale), since=short_when(allowed), why=why))
        with db.tx():
            core.put_setting(db, key, json.dumps(state, sort_keys=True))
        out.flush()
        done.append({"sale": sale["id"], "result": result})
    return done


# --- eBay's 5-minute work ------------------------------------------------------------------------------------------

def tick(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """After the Fulfillment read: the unpaid orders read; then, only when that read worked, the notes and the
    backstop; the scopes asked for once while an unpaid order of ours is open. Never raising (its type and place)."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off" or not ebay.configured() or ebay._consent(db) is None or ebay.listing_api() != "listing":
        return {"read": False}
    try:
        got = read(db, now, mode)
    except ebay.NotConnected:
        return {"read": False, "why": "not connected"}
    except (ApiError, ebay.Refused) as e:
        log.warning("unpaid: the read failed: %s", ebay._code(str(e), 80))
        return {"read": False, "why": str(e)[:120]}
    if _open(db):
        got["scopes_asked"] = ask_scopes(db, now, mode)
    got["notes"] = notes(db, now, mode)
    got["cancels"] = cancel_due(db, now, mode)
    return got


def status(db: Database, now: datetime | None = None) -> dict:
    """For GET /health: the unpaid sales of ours open now, and the scopes the backstop still lacks."""
    now = util.aware(now) if now else util.utcnow()
    return {"open": [{"sale": s["id"], "cancel_from": s["cancel_from"], "reminders": reminders_count(db, s, now)}
                     for s in _open(db)],
            "missing_scopes": [re.sub(r".*/", "", s) for s in missing_scopes(db)] if ebay.configured() else []}
