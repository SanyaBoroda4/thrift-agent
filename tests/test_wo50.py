"""WO50: shop-window titles every site accepts (one checker, one fixer, each site's rules from data/site_rules.json);
a site's fixable refusal fixed and retried once by itself; the live titles reviewed once and applied on "go titles"
through each site's edit page; dual sizes ("S/M") that always map — the bigger one, the label kept in the description
and on the card, one private line when no menu value is reasonable."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from test_crosslist_flow import (TITLE, Stub, _item, _posters, _settings, driver,  # noqa: F401 (fixtures)
                                 fields_for, loop, verified_selectors)

from thrift_agent import approve, crosslist, edits, titles
from thrift_agent.catalogs.sizes import SizeIn, depop_size
from thrift_agent.db import DB
from thrift_agent.post import runner
from thrift_agent.post.base import Outcome
from thrift_agent.post.fallback import dual_parts, menu_size

BAG = "BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted Strap"
CAPS_REFUSAL = ("Vinted refused the form: Title contains too many capital letters, try using lowercase letters. "
                "(its requests after the click: /api/v2/item_upload/items 400)")


# ---------------------------------------------------------------- the titles

def test_the_bcbg_title_becomes_a_shop_window_title():
    assert titles.check(BAG) == ["all-caps: BCBGMAXAZRIA", "capitals 38% (limit 35%)"]
    fixed = titles.fix(BAG, "BCBGMAXAZRIA", "NWT")
    assert fixed == "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags"
    assert titles.check(fixed) == [] and len(fixed) <= 80
    assert titles.fix(fixed, "BCBGMAXAZRIA", "NWT") == fixed                       # run twice, same title


def test_all_caps_brands_soften_and_real_acronyms_stay():
    assert titles.fix("FARM Rio Zodiac Print Maxi Dress Black Red Balloon Sleeve size M", "FARM Rio") == \
        "Farm Rio Zodiac Print Maxi Dress Black Red Balloon Sleeve size M"
    assert titles.fix("FRAME Le High Straight Jeans Medium Blue Wash Raw Hem size 00", "FRAME") == \
        "Frame Le High Straight Jeans Medium Blue Wash Raw Hem size 00"
    for same in ("UGG Tan & Sage Green Suede Platform Slippers size 7", "H&M Houndstooth Pea Coat Black & White size XS",
                 "DKNY Black Logo Sweatshirt size M", "Nike Dri-FIT Graphic Tee Black size XL", "NYDJ Dark Wash Jeans size 8"):
        assert titles.fix(same) == same and titles.check(same) == [], same       # real acronyms are never held
    assert titles.display_brand("MNG (Mango)") == "Mango" and titles.display_brand("ASOS") == "ASOS"


def test_codes_duplicates_emoji_and_length_are_caught_and_fixed():
    bad = "New Balance 990v5 Grey Sneakers RN 54867 i_261024_abc123 size size 9 😍 &amp; Lace Lace Laces"
    problems = titles.check(bad)
    assert any(p.startswith("a code:") for p in problems) and "size size" in problems and "an HTML artefact" in problems
    assert "odd characters" in problems and any(p.startswith("repeated:") for p in problems)
    fixed = titles.fix(bad, "New Balance")
    assert titles.check(fixed) == [] and "990v5" not in fixed and "54867" not in fixed and "i_261024" not in fixed
    assert "MNG" not in titles.fix("MNG (Mango) Mango Satin Button-Down Blouse Camel Brown size 4", "MNG (Mango)")
    long = "Vince " + " ".join(["Beautiful Silk Charmeuse"] * 6) + " Slip Dress Black size S"
    assert any(p.startswith("too long") for p in titles.check(long))
    out = titles.fix(long, "Vince")
    assert len(out) <= 80 and out.startswith("Vince") and out.endswith("size S")
    assert titles.fix("Zara Black/Green Midi Skirt NWT EUC size M", "Zara") == "Zara Black & Green Midi Skirt size M"


def test_vinteds_capital_limit_is_recorded_from_its_own_answers():
    rules = titles.rules()["title"]
    assert rules["caps_share_safe"] == 0.29 and rules["caps_share_max"] == 0.35
    assert "38%" in rules["caps_share_why"] and "29%" in rules["caps_share_why"]
    assert titles.caps_share("FRAME Le High Straight Jeans Medium Blue Wash Raw Hem size 00") < 0.30   # accepted
    assert titles.caps_share(BAG) > 0.35                                                              # refused
    strict = titles.fix("UGG Tan Sage Green Suede Platform Slippers XL Box size 7", strict=1)
    assert strict.startswith("UGG ") and titles.caps_share(strict) <= 0.29                          # after a refusal
    assert titles.fixable("vinted", "Title contains too many capital letters, try using lowercase letters.") == \
        "title_caps"
    assert titles.fixable("depop", "Brand: This field is required") is None


def test_depop_and_vinted_get_the_same_shop_window_title(tmp_path):
    from thrift_agent.catalogs.vinted import vinted_title
    assert vinted_title(BAG, "BCBGMAXAZRIA", "NWT") == "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, " \
                                                        "New with Tags"
    view = SimpleNamespace(facts=SimpleNamespace(brand=SimpleNamespace(value="BCBGMAXAZRIA"), condition="NWT"))
    fields = SimpleNamespace(description=f"{BAG}\n\nBCBGMAXAZRIA slouchy bag.")
    db = DB(tmp_path / "state.db")
    out = crosslist.strict_title(db, "i_1", "depop", fields, view)
    assert out.description.split("\n")[0] == "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags"
    assert titles.display_in_text("BCBGMAXAZRIA slouchy bag.", "BCBGMAXAZRIA") == "BCBG Max Azria slouchy bag."


# ---------------------------------------------------------------- a site's fixable refusal: fixed, retried once

def test_a_vinted_capitals_refusal_is_fixed_and_retried_once_then_posted(tmp_path, loop, monkeypatch):  # noqa: F811
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    ps = _posters(vinted=Stub("vinted", Outcome("failed", error=CAPS_REFUSAL),
                              Outcome("posted", url="https://www.vinted.com/items/{iid}")))
    run(s, db, ps)
    assert len(ps["vinted"].calls) == 2                                   # refused, fixed, retried once: posted
    assert db.listing(iid, "vinted")["status"] == "posted"
    assert not [m for m in said.ops if m.startswith("⛔")]                # the owner is never asked to 'retry'
    assert any(m.startswith("✓ Vinted had refused") and "title was fixed and it is live" in m for m in said.ops)
    assert crosslist.strict_level(db, iid, "vinted") == 0


def test_a_second_refusal_after_the_fix_is_one_line_and_no_loop(tmp_path, loop, monkeypatch):  # noqa: F811
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    ps = _posters(vinted=Stub("vinted", Outcome("failed", error=CAPS_REFUSAL)))
    run(s, db, ps)
    assert len(ps["vinted"].calls) == 2                                   # the one retry, never a third
    assert db.listing(iid, "vinted")["status"] == "failed"
    assert len([m for m in said.ops if m.startswith("⛔") and "Reply 'retry'" in m]) == 1   # the one reply-able line


# ---------------------------------------------------------------- sizes

@pytest.mark.parametrize("label,menu,want", [
    ("S/M", ["One Size", "XXS", "XS", "S", "M", "L", "XL"], "M"),
    ("M/L", ["XS", "S", "M", "L", "XL"], "L"),
    ("XS/S", ["XXS", "XS", "S", "M"], "S"),
    ("0/2", ["00", "0", "2", "4", "6"], "2"),
    ("7/8", ["6", "7", "8", "10", "12"], "8"),
    ("8-10", ["6", "8", "10", "12"], "10"),
    ("One Size (S–L)", ["S", "M", "L", "XL"], "L"),
])
def test_dual_sizes_map_to_the_bigger_one(label, menu, want):
    assert menu_size(label, menu, "dress")[0] == want


def test_the_fallback_order_and_one_size_only_for_one_size_items():
    menu = ["One Size", "XS", "S", "M", "L", "XL"]
    assert menu_size("M", menu) == ("M", "exact")
    assert menu_size("XXL", menu) == ("XL", "nearest")
    assert menu_size("XXXXL", menu, "plush robe") == ("One Size", "one size")
    assert menu_size("XXXXL", menu, "wide leg pants") is None              # never One Size for a sized item
    assert dual_parts("M") is None and dual_parts("S/M") == ("S", "M")


def test_the_robe_maps_on_every_site_and_says_its_label(tmp_path):
    from thrift_agent.brain import copy as copywriter
    from thrift_agent import pipeline
    from thrift_agent.schema import Ev, Facts
    s = SizeIn("Women", "Intimates & Sleepwear", "Robes", "plush robe", "S/M", None, None)
    assert depop_size(s, ["XXS", "XS", "S", "M", "L", "XL"], "Women > Lingerie > US")[0] == "M"
    facts = Facts(item_type="plush robe", department="Women", category="Intimates & Sleepwear",
                  brand=Ev(value="Bath & Body Works", photos=[0], source="photo", confidence=0.9),
                  size_printed=Ev(), size_us=Ev(value="S/M", photos=[1], source="photo", confidence=0.9),
                  condition="good", condition_evidence=Ev(value="clean", photos=[0], source="photo", confidence=0.9),
                  colors=["Purple"], cover_photo=0, photo_order=[0, 1])
    out = pipeline.settle_dual_size(facts)
    assert out.size_us.value == "M" and out.size_printed.value == "S/M"
    assert copywriter.ensure_label_size("Plush robe.", out) == "Plush robe.\nSize S/M (fits S–M)."
    db = DB(tmp_path / "state.db")
    bid = db.add_batch("share_r", 2)
    iid = db.add_item(bid, 1, "work/r")
    db.set_item(iid, status="awaiting_price", facts=out.model_dump(),
                renders={"poshmark": {"title": "Bath & Body Works Plush Robe size M", "size": "M"}})
    kind, text = approve.card(iid, db.item(iid))
    assert "Size M (label S/M)" in text, text


def test_a_size_no_menu_value_fits_is_one_private_line(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    said = []
    monkeypatch.setattr(crosslist.notify, "say", said.append)
    err = crosslist.SKIPPED + "size '15' isn't on Poshmark's Women/Shoes (Standard) menu, nor one near it"
    assert crosslist.size_skip(err)
    crosslist.say_size_skip(db, "i_1", "poshmark", "Clarks Loafers", err)
    crosslist.say_size_skip(db, "i_1", "poshmark", "Clarks Loafers", err)
    assert len(said) == 1 and said[0].startswith("📏 Clarks Loafers: Poshmark has no reasonable value for its size")


# ---------------------------------------------------------------- the live titles: reviewed once, "go titles"

def _live(db, title=BAG):
    bid = db.add_batch("share_x", 3)
    iid = db.add_item(bid, 1, "work/x")
    db.set_item(iid, status="posted", facts={"brand": {"value": "BCBGMAXAZRIA"}, "condition": "NWT"},
                renders={"poshmark": {"title": title, "description": "BCBGMAXAZRIA bag."}})
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24)
    db.upsert_listing(iid, "vinted", status="posted", url="https://www.vinted.com/items/1",
                      fields_json={"title": "Bcbgmaxazria New Black Slouchy Shoulder Bag Knotted Strap"})
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-bag-d07b/",
                      fields_json={"description": f"{title}\n\nBCBGMAXAZRIA bag.\n\n#bcbg"})
    return iid


def test_the_review_is_one_message_and_go_titles_queues_each_site(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live(db)
    ok_bid = db.add_batch("share_y", 1)
    ok = db.add_item(ok_bid, 1, "work/y")
    db.set_item(ok, status="posted", renders={"poshmark": {"title": TITLE}})
    db.upsert_listing(ok, "poshmark", status="posted", url="https://poshmark.com/listing/y-" + "b" * 24)
    rows = titles.review(db)
    assert [r["item"] for r in rows] == [iid]                             # the good one isn't flagged
    msg = titles.review_message(rows)
    assert msg.startswith("✏️ 1 live title breaks the shop-window rules") and "→ BCBG Max Azria" in msg
    assert msg.rstrip().endswith("Reply 'go titles' to change them on the sites (nothing else on the listings changes).")
    sent = []

    class Bot:
        def send_message(self, text, reply_to=None):
            sent.append(text)
    monkeypatch.setattr(approve, "ops_bot_for", lambda s_: Bot())
    out = approve._route_ops(s, db, {"message": {"message_id": 1, "chat": {"id": 1}, "text": "go titles"}})
    assert out == "ops: go titles (3)" and sent[0].startswith("✓ 3 title changes queued")
    payloads = {r["marketplace"]: json.loads(r["payload"]) for r in
                db.conn.execute("SELECT marketplace, payload FROM listing_edits WHERE kind='text'")}
    new = "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags"
    assert payloads["poshmark"] == {"expect_title": BAG, "title": new}
    assert payloads["vinted"] == {"expect_title": "Bcbgmaxazria New Black Slouchy Shoulder Bag Knotted Strap",
                                  "title": new}
    assert payloads["depop"]["expect_first_line"] == BAG
    assert payloads["depop"]["description"].split("\n")[0] == new and payloads["depop"]["description"].endswith("#bcbg")


class TextPoster:
    """A site's text edit, as the posters answer it: saved and shown — or a page that shows another title."""

    def __init__(self, mismatch=None):
        self.calls, self.mismatch = [], mismatch

    async def set_text(self, *args, **kw):
        payload = args[2] if len(args) > 2 else args[1]
        self.calls.append(payload)
        if self.mismatch:
            return {"ok": False, "saved": False, "mismatch": self.mismatch}
        if kw.get("go"):
            kw["go"]()
        return {"ok": True, "saved": True, "shot": None}


def test_a_text_edit_is_saved_once_and_the_records_follow(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live(db)
    said = []
    monkeypatch.setattr(edits.notify, "say", said.append)
    monkeypatch.setattr(edits, "may_edit", lambda *a, **k: True)
    new = "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags"
    edits.queue_text(db, iid, new)
    for site in ("poshmark", "vinted", "depop"):
        poster = TextPoster()
        assert asyncio.run(edits.run_one(s, db, site, poster, None, [site])) is True
        assert len(poster.calls) == 1
    assert json.loads(db.item(iid)["renders"])["poshmark"]["title"] == new
    assert json.loads(db.listing(iid, "vinted")["fields_json"])["title"] == new
    assert json.loads(db.listing(iid, "depop")["fields_json"])["description"].startswith(new + "\n")
    done = [m for m in said if m.startswith("✓ ")]
    assert len(done) == 1, said                                   # one line for the item, once its last site is done
    assert "new title on Poshmark, Depop and Vinted" in done[0] and new in done[0]


def test_a_title_she_changed_by_hand_is_left_as_it_is(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live(db)
    said = []
    monkeypatch.setattr(edits.notify, "say", said.append)
    monkeypatch.setattr(edits, "may_edit", lambda *a, **k: True)
    edits.queue_text(db, iid, "BCBG Max Azria Black Slouchy Shoulder Bag", sites=["poshmark"])
    asyncio.run(edits.run_one(s, db, "poshmark", TextPoster(mismatch="Her own bag title"), None, ["poshmark"]))
    row = db.conn.execute("SELECT status, error FROM listing_edits WHERE kind='text'").fetchone()
    assert row["status"] == "skipped" and "changed by hand" in row["error"]
    assert json.loads(db.item(iid)["renders"])["poshmark"]["title"] == BAG     # nothing changed here either
    assert said and "Left as it is" in said[0]


def test_an_old_poshmark_title_is_fixed_before_the_form_opens(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    renders = json.loads(db.item(iid)["renders"])
    renders["poshmark"]["title"] = "FARM Rio Zodiac Print Maxi Dress Black Red size M"
    db.set_item(iid, renders=renders, facts={"brand": {"value": "FARM Rio"}, "condition": "good"})
    job = runner.next_job(s, db, ["poshmark"], dry=False)
    assert job is not None and job[2].title == "Farm Rio Zodiac Print Maxi Dress Black Red size M"
    assert json.loads(db.item(iid)["renders"])["poshmark"]["title"] == job[2].title


def test_a_description_edit_carries_the_description_we_posted(tmp_path):
    """Her own wording on a site is never overwritten (live 2026-10-24, the robe's size line): every edit that types a
    description carries the one we posted, and a Depop title edit — its whole description typed again — always does."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live(db)
    edits.queue_text(db, iid, None, descriptions={"poshmark": "New body.", "vinted": "New body.",
                                                  "depop": f"{BAG}\n\nNew body."})
    p = {r["marketplace"]: json.loads(r["payload"]) for r in
         db.conn.execute("SELECT marketplace, payload FROM listing_edits WHERE kind='text'")}
    assert p["poshmark"]["expect_description"] == "BCBGMAXAZRIA bag."
    assert "expect_description" in p["vinted"]                    # the posted one (none recorded here)
    assert p["depop"]["expect_description"] == f"{BAG}\n\nBCBGMAXAZRIA bag.\n\n#bcbg"
    db.conn.execute("DELETE FROM listing_edits")
    edits.queue_text(db, iid, "BCBG Max Azria Black Slouchy Shoulder Bag")
    p = {r["marketplace"]: json.loads(r["payload"]) for r in
         db.conn.execute("SELECT marketplace, payload FROM listing_edits WHERE kind='text'")}
    assert "expect_description" not in p["poshmark"] and "expect_description" not in p["vinted"]   # a title only
    assert p["depop"]["expect_description"] == f"{BAG}\n\nBCBGMAXAZRIA bag.\n\n#bcbg"


def test_a_dry_text_edit_is_kept_in_the_event_log(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live(db)
    monkeypatch.setattr(edits.notify, "say", lambda *a, **k: None)
    monkeypatch.setattr(edits, "may_edit", lambda *a, **k: True)
    edits.queue_text(db, iid, "BCBG Max Azria Black Slouchy Shoulder Bag", sites=["poshmark"], dry=True)

    class Dry(TextPoster):
        async def set_text(self, *args, **kw):
            return {"ok": True, "saved": False, "shot": None, "seen": {"title_before": BAG}}
    asyncio.run(edits.run_one(s, db, "poshmark", Dry(), None, ["poshmark"]))
    ev = db.conn.execute("SELECT detail FROM events WHERE kind='listing_edit_dry'").fetchone()
    assert ev and json.loads(ev["detail"])["ok"] is True and json.loads(ev["detail"])["seen"]["title_before"] == BAG


def test_a_long_review_fits_one_telegram_message_and_keeps_its_reply_line():
    rows = [{"item": f"i_{n}", "old": {"poshmark": "BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted Strap " + str(n)},
             "new": "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags",
             "problems": ["all-caps: BCBGMAXAZRIA", "capitals 38% (limit 35%)"]} for n in range(80)]
    msg = titles.review_message(rows)
    assert len(msg) <= 4000 and msg.startswith("✏️ 80 live titles break")
    assert "more — 'go titles' changes every one of them" in msg and msg.endswith("nothing else on the listings changes).")
