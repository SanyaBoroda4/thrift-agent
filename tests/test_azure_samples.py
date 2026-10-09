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


def test_the_live_shapes_are_scrubbed_too():
    """WO33, the first real samples: Poshmark's "Buyer" block (name, then @handle), Vinted's "<handle> has bought",
    Depop's "Ship to" run together with spaces and an HTML-encoded name, and its "Profile Picture" handle."""
    posh = "Hi Ann! Great news\nBuyer\nMary Smithfield\n@mary_s\n\nOrder ID 6ab7cb1fff8a471e6bfa36ed\nPrice: $22.00"
    out = samples.scrub(posh, [], subject='"Nike Kids Sneakers" just sold to @mary_s on Poshmark!')
    assert "Smithfield" not in out and "mary_s" not in out and "Order ID 6ab7cb1fff8a471e6bfa36ed" in out
    vinted = "Hello Ann,\nmasey99 has bought\n\nSee Kai run size 8\n$6.40\nget in touch with masey99 when sent."
    out = samples.scrub(vinted, [])
    assert "masey99" not in out and "See Kai run size 8" in out and "$6.40" in out
    depop = ("Order details   Madewell sweater   Size:   S   £30.00    Ship to    Kayla O&rsquo;Callahan    "
             "1 Lake St    Chicago    IL    60601    Profile Picture\n\nkaylaoc\nPayment details")
    out = samples.scrub(depop, [], subject="Your USPS shipping label and sale confirmation for @kaylaoc")
    for personal in ("Kayla", "Callahan", "1 Lake St", "Chicago", "60601", "kaylaoc"):
        assert personal not in out, personal
    assert "Madewell sweater" in out and "£30.00" in out
    assert "[shop], your sale is complete" in samples.scrub("tattiseller, your sale is complete.", [])


def test_blank_lines_inside_an_address_block_keep_it_masked():
    out = samples.scrub("Ship to\n\n\nKayla Smith\n12 Elm St\n\n\nChicago\nIL\n60601\nPayment details", [])
    assert "Kayla" not in out and "Chicago" not in out and "Payment details" in out
