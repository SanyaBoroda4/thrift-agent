"""WO33 C2: the owner's email samples are scrubbed before they are written anywhere (deploy/azure/samples.py) — no
buyer name, address, e-mail, phone, tracking number or the shop's own name survives. A made-up email; no network."""
import sys

from thrift_agent.config import ROOT

sys.path.insert(0, str(ROOT / "deploy" / "azure"))
import samples  # noqa: E402

MADE_UP = """Hi Tanya,
Great news! @jane_doe99 purchased your listing from Poshmark.
Ship to:
Jane Doe
123 Main St Apt 4
Springfield, IL 62704

Buyer: janed
Contact: jane.doe@example.com, (217) 555-0134
Tracking 9400 1000 0000 0000 0000 00
Listing: https://poshmark.com/listing/x-abc by myshopname — Order #A1B2C3, $35.00
"""


def test_nothing_personal_survives_the_scrub():
    out = samples.scrub(MADE_UP, ["myshopname"])
    for personal in ("Tanya", "jane_doe99", "Jane Doe", "123 Main", "Springfield", "62704", "janed", "jane.doe@",
                     "555-0134", "9400 1000", "myshopname"):
        assert personal not in out, personal
    for kept in ("purchased your listing", "Poshmark", "https://poshmark.com/listing/x-abc", "Order #A1B2C3", "$35.00"):
        assert kept in out, kept
