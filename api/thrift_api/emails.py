"""The marketplaces' emails (WO33): who sent one (`marketplace_of`), what it is (`classify`: SALE, SHIPPED, DELIVERED,
CANCELLED or OTHER) and the few facts the sales tracking needs (`parse`). Pure text work: nothing is read or stored.

Written from the seller's own mail (WO33: 165 samples of 90 days, Poshmark 120, Depop 26, Vinted 19 —
scrubbed, in the private repo): each site's table opens with the exact subjects its real emails carry (`REAL`), then
the general rules. Each site has ONE table, `RULES[site]`, read top to bottom: the first rule that matches decides,
the subject's rules before the body's, and an email nothing matches is OTHER. Offers, likes, shares, follows,
messages, payouts, promotions, newsletters, account mail, shipping reminders, cancellation REQUESTS and the seller's
own purchases are OTHER on purpose: the `GUARDS`, right after the real subjects.

Buyer data never leaves the text: `parse` returns only the fields in `SALE_FIELDS` / `FOLLOWUP_FIELDS` — a title, a
listing's address and id, our item id, a price, an order id, dates — and a title taken from a sentence is cut before
anything that could name a person ("… to @jane", "… to Jane Doe")."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote

from .deadlines import local
from .util import aware

DOMAINS = {"poshmark.com": "poshmark", "depop.com": "depop", "vinted.com": "vinted"}
KINDS = ("SALE", "SHIPPED", "DELIVERED", "CANCELLED", "OTHER")
FOLLOWUPS = ("SHIPPED", "DELIVERED", "CANCELLED")
SALE_FIELDS = ("title", "listing_url", "listing_id", "sku", "price", "order_id", "sold_at", "ship_by_stated")
FOLLOWUP_FIELDS = ("order_id", "title", "listing_url", "listing_id", "sku")
ITEM_ID = re.compile(r"\bi_\d{6}_[0-9a-f]{6}\b")          # our item ids, the SKU on Depop: i_261001_abc123


class ParseError(ValueError):
    """An email of a known kind without what its kind needs (a sale with no title, listing or SKU)."""


@dataclass(frozen=True)
class Rule:
    kind: str
    where: str                              # "subject" or "text"
    pattern: re.Pattern
    unless: re.Pattern | None = None        # the rule doesn't apply when this matches the same field

    def hit(self, subject: str, text: str) -> bool:
        field = subject if self.where == "subject" else text
        return bool(self.pattern.search(field)) and not (self.unless and self.unless.search(field))


def rule(kind: str, where: str, pattern: str, unless: str | None = None) -> Rule:
    return Rule(kind, where, re.compile(pattern, re.I), re.compile(unless, re.I) if unless else None)


# "You made a sale", "You sold …", "Your item has sold", "Item sold", "Your bundle was sold"
SOLD = (r"\bmade a sale\b|\byou(?:'ve| have)? (?:just )?sold\b"
        r"|\b(?:item|listing|bundle)s? (?:has |have |was |were )?(?:just )?(?:been )?sold\b")

GUARDS = (
    rule("OTHER", "subject", r"\bcancel(?:l)?ation request|\b(?:requests?|requested|wants|asked) to cancel\b"),
    rule("OTHER", "subject", r"\b(?:counter[- ]?)?offers?\b", unless=SOLD),
    rule("OTHER", "subject", r"\bliked\b|\blikes? your\b|\byou like\b|\bfavou?rite"),          # "An item you liked sold"
    rule("OTHER", "subject", r"\bshared\b|\bfollow(?:s|ed|ing|ers?)?\b"),
    rule("OTHER", "subject", r"\bmessages?\b|\bcomment|\bmentioned you\b|\breview|\bratings?\b|\brate (?:your|the)\b"
                             r"|\bfeedback\b"),
    rule("OTHER", "subject", r"\bpay ?outs?\b|\bpaid\b|\bearn(?:ed|ings?)\b|\bredeem|\bredemption\b|\bbalance\b|\bfunds?\b"
                             r"|\bwithdraw|\bdeposit|\bmoney\b|\btransfer|\bwallet\b|\binvoice\b"),
    rule("OTHER", "subject", r"\breminder\b|\bdon'?t forget\b|\bremember to\b|\boverdue\b|\blate\b"
                             r"|\b(?:hasn'?t|has not|haven'?t|have not|not yet) (?:been )?shipped\b"),
    rule("OTHER", "subject", r"\byou (?:bought|purchased|ordered)\b|\byour (?:purchase|order) (?:is |has been )?confirmed\b"
                             r"|\b(?:order|purchase) confirmation\b"
                             r"|\bthanks? (?:you )?for (?:your )?(?:order|purchase|shopping)\b"),
    rule("OTHER", "subject", r"%|\bpromo|\bdiscount|\bcoupon|\bnewsletter|\bparty\b|\bprice drop|\bdrops?\b|\bjust in\b"
                             r"|\btrending|\bweekly\b|\bmonthly\b|\bdigest\b|\brecap\b|\bsummary\b|\bnew arrivals?\b"
                             r"|\bwe miss you\b|\binvit|\brefer|\bsurvey\b|\bgiveaway\b|\bwebinar\b|\bclear ?out\b|\bboost"
                             r"|\bbump", unless=SOLD),
    rule("OTHER", "subject", r"\bpassword\b|\bverif(?:y|ied|ication)\b|\bsecurity\b|\bsign[- ]?in\b|\blog[- ]?in\b"
                             r"|\bconfirm your\b|\bterms\b|\bprivacy\b|\bpolicy\b|\bwelcome\b"),
)

# The real subjects (WO33, from the samples), ahead of everything else in each site's table.
REAL = {
    "poshmark": (
        # '"<title>" just sold to @x on Poshmark!', 'Thank you for shipping <title>',
        # 'Please do not ship: "<title>" for @x was canceled'
        rule("SALE", "subject", r'^\s*"[^"]{3,200}"\s+just sold to\b.*\bon poshmark\b'),
        rule("SHIPPED", "subject", r"^\s*thank you for shipping\b"),
        rule("CANCELLED", "subject", r"^\s*please do not ship\b.*\bwas cancel(?:l)?ed\b"),
    ),
    "vinted": (
        rule("SALE", "subject", r"^\s*you sold an item on vinted\b"),
        rule("SALE", "subject", r"\bshipping label\s*[-–—]\s*use by\b"),    # the label: its "Shipment deadline" fills the sale
        rule("DELIVERED", "subject", r"^\s*this order is completed\b"),       # the buyer confirmed: the sale is complete
    ),
    "depop": (
        rule("SALE", "subject", r"\bshipping label and sale confirmation\b"),
        rule("DELIVERED", "subject", r"^\s*your sale to\b.*\bwas delivered\b"),
    ),
}

CANCELLED_SUBJECT = r"\bcancel(?:l)?ed\b|\bcancel(?:l)?ation\b"
CANCELLED_TEXT = r"\b(?:order|sale|transaction) (?:has been|was|is) cancel(?:l)?ed\b"
DELIVERED_TEXT = r"\b(?:order|package|parcel|shipment|item) (?:has been|was) delivered\b"

RULES: dict[str, tuple[Rule, ...]] = {
    "poshmark": (
        *REAL["poshmark"],
        *GUARDS,
        rule("CANCELLED", "subject", CANCELLED_SUBJECT),
        rule("DELIVERED", "subject", r"\bdelivered\b"),
        rule("SHIPPED", "subject", r"\blabel (?:was |has been |is )?scanned\b|\bscanned\b|\bin transit\b"
                                   r"|\b(?:has|have|was|were|been) shipped\b|\bon (?:its|the) way\b"),
        rule("SALE", "subject", SOLD + r"|\bcongrat(?:s|ulations)\b.*\bsold\b|\bnew order\b"),
        rule("CANCELLED", "text", CANCELLED_TEXT),
        rule("DELIVERED", "text", DELIVERED_TEXT),
        rule("SHIPPED", "text", r"\blabel (?:has been|was) scanned\b|\b(?:package|shipment|order) (?:is|has been|was) "
                                r"(?:now )?in transit\b"),
        rule("SALE", "text", r"\byou(?:'ve| have)? (?:just )?made a sale\b"
                             r"|\byour (?:item|listing|bundle) (?:has |was )?(?:just )?(?:been )?sold\b"),
    ),
    "depop": (
        *REAL["depop"],
        *GUARDS,
        rule("CANCELLED", "subject", CANCELLED_SUBJECT),
        rule("DELIVERED", "subject", r"\bdelivered\b"),
        rule("SHIPPED", "subject", r"\bmarked as shipped\b|\b(?:has|have|was|were|been) shipped\b|\bin transit\b"
                                   r"|\bscanned\b"),
        rule("SALE", "subject", SOLD + r"|\bbought your\b|\bpurchased your\b|\bnew sale\b"),
        rule("CANCELLED", "text", CANCELLED_TEXT),
        rule("DELIVERED", "text", DELIVERED_TEXT),
        rule("SHIPPED", "text", r"\bmarked as shipped\b|\blabel (?:has been|was) scanned\b"),
        rule("SALE", "text", r"\byou(?:'ve| have)? (?:just )?sold\b|\byour item (?:has |was )?(?:just )?(?:been )?sold\b"),
    ),
    "vinted": (
        *REAL["vinted"],
        *GUARDS,
        rule("CANCELLED", "subject", CANCELLED_SUBJECT),
        rule("DELIVERED", "subject", r"\bdelivered\b"),
        rule("SHIPPED", "subject", r"\b(?:has|have|was|were|been) shipped\b|\bin transit\b|\bscanned\b"
                                   r"|\bon (?:its|the) way\b|\bdropped off\b"),
        rule("SALE", "subject", SOLD + r"|\bhas been (?:purchased|bought)\b|\bsomeone (?:bought|purchased)\b"
                                r"|\bnew order\b"),
        rule("CANCELLED", "text", CANCELLED_TEXT),
        rule("DELIVERED", "text", DELIVERED_TEXT),
        rule("SHIPPED", "text", r"\b(?:parcel|package|order) (?:is|has been|was) (?:now )?in transit\b"
                                r"|\blabel (?:has been|was) scanned\b"),
        rule("SALE", "text", r"\byou(?:'ve| have)? (?:just )?made a sale\b"
                             r"|\byour item (?:\S+ ){0,12}?(?:has |was )(?:just )?(?:been )?sold\b"),
    ),
}

# The listing's address on each site and the id in it: Poshmark /listing/<slug>-<24 hex>, Depop /products/<slug>/,
# Vinted /items/<digits>[-slug]
LISTING_URLS = {
    "poshmark": re.compile(r"https?://(?:www\.)?poshmark\.com/listing/(?:[^\s/?#\"'<>]*-)?(?P<id>[0-9a-f]{24})(?![0-9a-z])",
                           re.I),
    "depop": re.compile(r"https?://(?:www\.)?depop\.com/products/(?P<id>[a-z0-9][a-z0-9_-]*)/?", re.I),
    "vinted": re.compile(r"https?://(?:www\.)?vinted\.com/items/(?P<id>\d+)(?:-[^\s/?#\"'<>]*)?", re.I),
}
ORDER_URLS = {"poshmark": re.compile(r"poshmark\.com/order/sales/(?P<id>[0-9a-f]{24})", re.I)}
ORDER_ID = re.compile(r"\b(?:order|transaction)[ \t]*(?:id|number|no\.?|#)[ \t]*[:#.]?\s*#?[ \t]*"
                      r"(?P<id>(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,40})\b", re.I)

AMOUNT = r"(?:US)?[$£€][ \t]?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
PRICES = (re.compile(r"\b(?:sold for|sale price|item price|listing price|price)\b[ \t]*:?[ \t]*(?:of[ \t]+)?" + AMOUNT, re.I),
          re.compile(r"\bfor[ \t]+" + AMOUNT, re.I),
          re.compile(r"\b(?:order total|subtotal|total)\b[ \t]*:?[ \t]*" + AMOUNT, re.I),
          re.compile(AMOUNT))
NOT_A_PRICE = re.compile(r"\b(?:earn\w*|fees?|shipping|postage|tax(?:es)?|commission|payout|balance|discount|credit|bonus"
                         r"|make|receive|get)\b[^$\n]{0,25}$", re.I)

MONTHS = {name: n for n, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                            "dec"), start=1)}
SHIP_BY = re.compile(
    r"\bship(?:ped)?\b[^.\n]{0,40}?\b(?:by|before|no later than):?[ \t]+"
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?[ \t]+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[ \t]+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?[ \t]+(?P<year>\d{4}))?|(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?)\b", re.I)

# A title: a labelled line, a quoted name, or the words after "you sold" / between "your item" and "has sold".
# (pattern, field, cut): `cut` titles come from a sentence and are cut where the sentence goes on.
TITLES = (
    # WO33, the real emails: Vinted "<buyer> has bought\n\n<title>", "Your sale of <title> was completed"; Depop "Order
    # details\n(image)\n<title>"; Poshmark "Thank you for shipping <title>"
    (re.compile(r"\bhas bought[ \t]*\n+[ \t]*(?P<t>[^\n]{3,200})", re.I), "text", False),
    (re.compile(r"\byour sale of (?P<t>[^\n]{3,200}?) was completed\b", re.I), "text", False),
    (re.compile(r"\border details[ \t]*\n(?:[ \t]*(?:image)?[ \t]*\n)*[ \t]*(?P<t>[^\n]{3,200})", re.I), "text", False),
    (re.compile(r"^\s*thank you for shipping (?P<t>[^\n]{3,200})$", re.I), "subject", False),
    (re.compile(r"^[ \t]*(?:item(?: name)?|title|listing|product)[ \t]*:[ \t]*(?P<t>[^\n]{3,200})$", re.I | re.M), "text",
     False),
    (re.compile(r"\"(?P<t>[^\"\n]{3,200})\""), "subject", False),
    (re.compile(r"\byou(?:'ve| have)? (?:just )?sold (?:your |an? |the )?(?P<t>[^\n]{3,200})", re.I), "subject", True),
    (re.compile(r"\"(?P<t>[^\"\n]{3,200})\""), "text", False),
    (re.compile(r"\byou(?:'ve| have)? (?:just )?sold (?:your |an? |the )?(?P<t>[^\n]{3,200})", re.I), "text", True),
    (re.compile(r"\byour (?:item|listing) (?P<t>[^\n]{3,200}?) (?:has|was) (?:just )?(?:been )?(?:sold|purchased|bought"
                r"|shipped|delivered|cancel(?:l)?ed)\b", re.I), "text", True),
)
CUT = re.compile(r"[ \t]+(?:for[ \t]+(?:US)?\$|for[ \t]+\d|to[ \t]|on[ \t]+(?:poshmark|depop|vinted)\b|has[ \t]|was[ \t]|is[ \t]"
                 r"|-[ \t]*\$|—)|[!?]", re.I)
GENERIC = {"item", "an item", "your item", "the item", "it", "listing", "your listing", "a sale", "sale", "something",
           "an order", "order", "your order"}
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', " ": " ", "​": ""})


def marketplace_of(from_addr: object) -> str | None:
    """"poshmark" / "depop" / "vinted" when the From ADDRESS is at poshmark.com, depop.com or vinted.com or a subdomain
    of one (a display name or a look-alike domain never counts); else None."""
    text = re.sub(r"\([^()]*\)", " ", str(from_addr or "")).strip()
    angle = re.search(r"<([^<>]*)>\s*$", text)
    address = (angle.group(1) if angle else text).strip().lower()
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().rstrip(".")
    for suffix, site in DOMAINS.items():
        if domain == suffix or domain.endswith("." + suffix):
            return site
    return None


def classify(marketplace: str | None, subject: str, text: str) -> str:
    """The email's kind by its site's table (see the module): SALE, SHIPPED, DELIVERED, CANCELLED or OTHER."""
    rules = RULES.get(str(marketplace or "").lower())
    if not rules:
        return "OTHER"
    s, t = _clean(subject), _clean(text)
    for each in rules:
        if each.hit(s, t):
            return each.kind
    return "OTHER"


def parse(marketplace: str | None, kind: str, subject: str, text: str, received_at: datetime) -> dict:
    """The facts of a SALE (`SALE_FIELDS`: sold_at is the email's time, ship_by_stated a date the email gives or
    None) or of a SHIPPED / DELIVERED / CANCELLED email (`FOLLOWUP_FIELDS`); {} for OTHER. A sale without a title, a
    listing and a SKU, or a follow-up without an order id, a title and a listing, is a ParseError."""
    mp = str(marketplace or "").lower()
    s, t = _clean(subject), _clean(text)
    both = f"{s}\n{t}"
    if kind not in ("SALE", *FOLLOWUPS):
        return {}
    url, listing_id = listing_ref(mp, both)
    facts = {"order_id": order_id(mp, both), "title": title(mp, s, t, url), "listing_url": url,
             "listing_id": listing_id, "sku": sku(both)}
    if kind != "SALE":
        return facts                            # nothing to go on: the API finds no sale and says so (no retries)
    if not (facts["title"] or facts["listing_id"] or facts["sku"]):
        raise ParseError("a sale email with no title, listing or SKU")
    amount = price(t)
    return {**facts, "price": amount if amount is not None else price(s), "sold_at": aware(received_at),
            "ship_by_stated": ship_by_stated(t, received_at) or ship_by_stated(s, received_at)}


# --- the facts, one by one ------------------------------------------------------------------------------------------

def listing_ref(marketplace: str, text: str) -> tuple[str | None, str | None]:
    """The first listing address of the site in the text (also inside an encoded redirect link) and its id."""
    pattern = LISTING_URLS.get(marketplace)
    if pattern is None:
        return None, None
    for source in (text, unquote(text)):
        found = pattern.search(source)
        if found:
            ident = found.group("id")
            return found.group(0), ident.lower() if marketplace == "poshmark" else ident
    return None, None


def listing_id_from_url(marketplace: str, url: str | None) -> str | None:
    """The id in a stored listing address (Poshmark's 24 hex, Depop's slug, Vinted's number); None if it has none."""
    return listing_ref(marketplace, url)[1] if url else None


def sku(text: str) -> str | None:
    found = ITEM_ID.search(text)
    return found.group(0) if found else None


def order_id(marketplace: str, text: str) -> str | None:
    by_url = ORDER_URLS.get(marketplace)
    found = by_url.search(text) if by_url else None
    if found:
        return found.group("id").lower()
    found = ORDER_ID.search(text)
    return found.group("id") if found else None


def price(text: str) -> float | None:
    """The sale's price: a labelled one ("Price: $35.00", "sold for $35") first, then "for $35", then a total, then
    the first amount — never one said to be a fee, shipping, earnings or a payout."""
    for pattern in PRICES:
        for found in pattern.finditer(text):
            if not NOT_A_PRICE.search(text[max(0, found.start() - 40):found.start()]):
                return float(found.group("amount").replace(",", ""))
    return None


def title(marketplace: str, subject: str, text: str, url: str | None = None) -> str | None:
    """The item's title as the email gives it (see TITLES), else the words of a Poshmark listing's address."""
    subject, text = _clean(subject), _clean(text)
    for pattern, field, cut in TITLES:
        found = pattern.search(subject if field == "subject" else text)
        name = _tidy_title(found.group("t"), cut) if found else None
        if name:
            return name
    if marketplace == "poshmark" and url:
        slug = re.search(r"/listing/(?P<slug>[^\s/?#]*)-[0-9a-f]{24}", url, re.I)
        if slug:
            return _tidy_title(unquote(slug.group("slug")).replace("-", " "), False)
    return None


DEADLINE = re.compile(r"\b(?:shipment deadline|shipping deadline|use by)[ \t]*:?[ \t]*"
                      r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})\b", re.I)


def ship_by_stated(text: str, received_at: datetime) -> date | None:
    """A ship-by date the email states ("Please ship by Fri, Oct 9", "ship before 10/09/2026", Vinted's label
    "Shipment deadline: 10/06/2026 09:51 AM"): the year is the email's own unless given (a date more than a week back
    is next year's); a date outside a day before to 45 days after the email is no date."""
    found = DEADLINE.search(text) or SHIP_BY.search(text)
    if not found:
        return None
    sent = local(received_at).date()
    g = found.groupdict()
    if g.get("mon"):
        month, day, year = MONTHS[g["mon"].lower()[:3]], int(g["day"]), g.get("year")
    else:
        month, day, year = int(found.group("m")), int(found.group("d")), found.group("y")
    explicit = year is not None
    year = (int(year) + 2000 if len(year) == 2 else int(year)) if explicit else sent.year
    try:
        stated = date(year, month, day)
    except ValueError:
        return None
    if not explicit and stated < sent - timedelta(days=7):
        try:
            stated = date(year + 1, month, day)
        except ValueError:
            return None
    return stated if sent - timedelta(days=1) <= stated <= sent + timedelta(days=45) else None


def _clean(text: object) -> str:
    """Straight quotes and apostrophes, no non-breaking or zero-width spaces, single spaces."""
    return re.sub(r"[ \t]+", " ", str(text or "").translate(QUOTES))


def _tidy_title(raw: str, cut: bool) -> str | None:
    """A title as it can be stored: one line, cut where its sentence goes on (when `cut`), no @handle, no URL, no
    generic word; a trailing ellipsis kept (it marks a title the email shortened)."""
    name = raw.strip().replace("...", "…")
    if cut:
        found = CUT.search(name)
        if found:
            name = name[:found.start()]
    name = re.sub(r"@\S+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" \t\"'“”‘’.,!?:;-–—")
    if len(name) < 3 or len(name) > 200 or re.search(r"https?://|www\.", name, re.I) or name.lower() in GENERIC:
        return None
    return name
