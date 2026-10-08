"""Shared pieces of the thrift-api tests (WO33): a request through http.handle, the three marketplaces' sample emails
(provisional wording, as emails.py's rules expect it), and an item listed on the sites."""
import json
from datetime import datetime, timezone

from thrift_api import core
from thrift_api.http import handle

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)                 # Thu Oct 8, 11:00 in New York
ITEM = "i_261001_abc123"
TITLE = "Lacoste Tee White size M"
POSH_ID, DEPOP_ID, VINTED_ID = "6700aa11bb22cc33dd44ee55", "thriftshop-lacoste-tee-white", "7012345678"
URLS = {"poshmark": f"https://poshmark.com/listing/Lacoste-Tee-White-size-M-{POSH_ID}",
        "depop": f"https://www.depop.com/products/{DEPOP_ID}/",
        "vinted": f"https://www.vinted.com/items/{VINTED_ID}-lacoste-tee-white"}
IDS = {"poshmark": POSH_ID, "depop": DEPOP_ID, "vinted": VINTED_ID}
BUYER = ("Jane Q. Buyer", "janeq_closet", "123 Secret Lane", "Springfield, IL 62704")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def call(db, method: str, path: str, body=None, query=None, now=NOW, raw: bytes | None = None):
    """(status, the JSON answer — or the HTML text, headers) of one request."""
    data = raw if raw is not None else b"" if body is None else json.dumps(body).encode()
    status, headers, out = handle(method, path, query or {}, data, now=now, db=db)
    if headers.get("Content-Type", "").startswith("application/json"):
        return status, json.loads(out), headers
    return status, out.decode("utf-8"), headers


def seed(db, item_id: str = ITEM, title: str = TITLE, sites=("poshmark", "depop", "vinted"), status: str = "posted",
         price: float = 35, urls: dict | None = None, ids: dict | None = None):
    """One item listed on `sites`, as the Mac's /sync sends it ({} for urls / ids: none)."""
    urls, ids = URLS if urls is None else urls, IDS if ids is None else ids
    core.sync(db, {"items": [{"id": item_id, "title": title, "price": price, "created_at": "2026-10-04T12:00:00+00:00",
                              "updated_at": "2026-10-04T12:00:00+00:00"}],
                   "listings": [{"item_id": item_id, "marketplace": site, "status": status, "url": urls.get(site),
                                 "listing_id": ids.get(site), "price": price, "posted_at": "2026-10-05T12:00:00+00:00",
                                 "updated_at": "2026-10-05T12:00:00+00:00"} for site in sites]})


def email(site: str, subject: str, text: str, message_id: str, date: str | None = "2026-10-08T14:05:00.000Z") -> dict:
    sender = {"poshmark": "Poshmark <noreply@poshmark.com>", "depop": "Depop <hello@mail.depop.com>",
              "vinted": "Vinted <no-reply@vinted.com>"}[site]
    return {"message_id": message_id, "thread_id": f"thread-{message_id}", "from": sender, "subject": subject,
            "date": date, "text": text}


def posh_sale(message_id: str = "posh-sale-1", title: str = TITLE, url: str | None = URLS["poshmark"],
              order: str | None = "6702bb11cc22dd33ee44ff55", price: str = "$35.00", **kw) -> dict:
    lines = ["Congrats, you made a sale!", f"Item: {title}", f"Price: {price}", "Shipping: $7.97",
             "You'll earn: $28.00"]
    if order:
        lines.append(f"Order ID: {order}")
    if url:
        lines.append(f"View listing: {url}")
    lines += ["Ship within 7 days with the prepaid label.", f"Buyer: {BUYER[0]} (@{BUYER[1]})",
              f"Ship to: {BUYER[2]}, {BUYER[3]}"]
    return email("poshmark", "Congratulations! Your item has sold", "\n".join(lines), message_id, **kw)


def depop_sale(message_id: str = "depop-sale-1", title: str = TITLE, sku: str | None = ITEM, **kw) -> dict:
    text = (f"Woohoo! You sold “{title}” for $35.00 to @{BUYER[1]}.\n"
            + (f"SKU: {sku}\n" if sku else "")
            + f"View it: {URLS['depop']}\nOrder number: 88771234\nShip to {BUYER[0]}, {BUYER[2]}, {BUYER[3]}")
    return email("depop", "You sold an item!", text, message_id, **kw)


def vinted_sale(message_id: str = "vinted-sale-1", title: str = TITLE, url: str | None = URLS["vinted"],
                ship_by: str = "Oct 13", **kw) -> dict:
    text = (f"Great news! Your item {title} has been sold.\nPrice: $35.00\nTransaction ID: 9876543210\n"
            f"Please ship by {ship_by}." + (f"\n{url}" if url else ""))
    return email("vinted", "Your item has been sold", text, message_id, **kw)


def followup(site: str, kind: str, message_id: str, order: str | None = None, title: str | None = None, **kw) -> dict:
    """A SHIPPED / DELIVERED / CANCELLED email naming its order and/or the item."""
    subject = {"SHIPPED": "Your shipment has been scanned", "DELIVERED": "Your order was delivered",
               "CANCELLED": "Your order was cancelled"}[kind]
    lines = ([f"Order ID: {order}"] if order else []) + ([f"Item: {title}"] if title else [])
    return email(site, subject, "\n".join(lines + ["Thanks for selling with us."]), message_id, **kw)


def tasks(db, sale_id: str | None = None) -> list[dict]:
    """The take-downs (of one sale), oldest first, those made together in the sites' order."""
    from thrift_api.core import TASK_ORDER
    sql = "SELECT * FROM delist_tasks" + (" WHERE sale_id = ?" if sale_id else "") + f" ORDER BY {TASK_ORDER}"
    return db.query(sql, (sale_id,) if sale_id else ())


def sale(db, sale_id: str) -> dict:
    return db.one("SELECT * FROM sales WHERE id = ?", (sale_id,))


def texts(sent: list, chat: str | None = None) -> list[str]:
    return [text for where, text, _ in sent if chat is None or where == chat]
