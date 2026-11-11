"""WO58 — nothing is ever skipped (the owner, 2026-11-10: "Absolutely nothing should be skipped neither at ebay or any
other platforms … agent must always post everywhere with less questions and choose appropriate sizes / categories
deciding by himself"). The size is the agent's decision when no photo shows one — set at her price, listed at once,
her later tap edits every site; every site's menu gives a value (exact, its spelling, the nearest — the bigger between
two —, its neutral choice, its middle); a category the form lacks is its closest; the only reasons left to keep an item
off a site are the site refusing it after every fallback, a person's step, and a site's own written ban (eBay: used
underwear and socks)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_wo43 import FakeBot, _callback, _item, shoe_facts  # noqa: F401 — the size-button helpers
from test_wo43 import env  # noqa: F401 — the fixture

from thrift_agent import approve, edits
from thrift_agent.approve import handle_update
from thrift_agent.catalogs.common import MappingError
from thrift_agent.post import fallback

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- the scan: no new skip path

# Every place in thrift_agent/ that can make a listing (or a row like it) "skipped", and why it may. A new one fails
# this test: WO58 — a field value never skips; add it here only for a reason that isn't a field's value.
ALLOWED = [
    ("crosslist.py", 'db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)\n    db.log(iid, "post_wrong_family"',
     "hold_family: unused since WO54 §1.4 (a category of another family is posted with a check line)"),
    ("crosslist.py", 'db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)\n        db.log(iid, "post_banned"',
     "cant_map: a site's own written ban (eBay's used underwear and socks) — WO58 rule 5"),
    ("ebay/poster.py", 'return Outcome("skipped", screenshot=shot, note=note, guesses=list(self.guesses),',
     "eBay refused the item for good (a policy) or removed it — the site refusing after every fallback"),
    ("edits.py", '_update(db, row["id"], status="skipped", error=why, done_at=_iso(_now()))',
     "a live edit with nothing to change (the listing gone, its photos gone) — not a listing"),
    ("pipeline.py", 'status="skipped", error=f"skipped: shared again as {iid}")', "a twin: the same item shared again"),
    ("pipeline.py", 'status="skipped", error=f"skipped: {TWIN_LIVE}{tid}")', "a twin already live"),
    ("post/base.py", 'return Outcome("skipped", screenshot=str(shot), error=str(e), note=_joined(self.notes + [left]),',
     "the Skipped handler: nothing raises Skipped since WO58 (see the test below)"),
    ("post/runner.py", 'return Outcome("skipped", error=f"can\'t map: {e}")',
     "a TEST run's verdict (the form check / a paused site's self-test): no listing row"),
    ("post/runner.py", 'return Outcome("skipped", error=wrong)', "a TEST run's verdict: no listing row"),
    ("pricedrop.py", "UPDATE price_changes SET status='skipped'", "a price drop not made (sold, her 'no drop')"),
    ("pricedrop.py", '_update(db, row["id"], status="skipped", error=why)', "a price drop not made"),
    ("sales.py", 'status="skipped", error=SOLD_SKIP)', "sold elsewhere: never listed again"),
    ("cleanup.py", '_close(db, table, row, "skipped",', "an old dry run / edit row closed by the cleanup"),
]
SKIP = re.compile(r'Outcome\("skipped"|status="skipped"|SET status=\'skipped\'|raise Skipped\(|'
                  r'_close\(db, table, row, "skipped"')        # what MAKES a skip (a query reading one isn't)


def _sources():
    for path in sorted((ROOT / "thrift_agent").rglob("*.py")):
        yield path.relative_to(ROOT / "thrift_agent").as_posix(), path.read_text(encoding="utf-8")


def test_no_new_skip_path_appears():
    found = []
    for rel, text in _sources():
        for m in SKIP.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            window = text[line_start:line_start + 400]
            if not any(rel == f and window.startswith(_lead(window, piece)) and piece.split("\n")[0].strip() in window
                       for f, piece, _ in ALLOWED):
                found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {window.splitlines()[0].strip()}")
    assert not found, ("a new way to skip an item (WO58: a field value never skips — fall back to the closest value, "
                       "or add it to ALLOWED with the reason that isn't a field's):\n" + "\n".join(found))


def _lead(window: str, piece: str) -> str:
    return window[: len(window) - len(window.lstrip())]


def test_nothing_raises_skipped_and_no_mapping_skips_a_field():
    """Poshmark's form never raises Skipped (a department, a category, a size it lacks: the closest, said); Depop's and
    Vinted's mappers raise MappingError only where no value of any kind exists (an empty catalog library)."""
    for rel, text in _sources():
        assert "raise Skipped(" not in text, rel
    sizes = (ROOT / "thrift_agent/catalogs/sizes.py").read_text(encoding="utf-8")
    assert "raise first" in sizes          # only after the neutral choice and the menu's middle (an empty menu)


# ---------------------------------------------------------------- every menu gives a value

@pytest.mark.parametrize("want,menu,value,how", [
    ("8", ["6", "8", "10"], "8", "exact"),
    ("9", ["4", "6", "8", "10", "12"], "10", "nearest"),                         # between two: the bigger
    ("15", ["5", "6", "7", "8", "9", "10", "11", "12"], "12", "nearest on the menu"),   # far: still its nearest
    ("M", ["0", "2", "4", "6", "8", "10", "12"], "10", "the same fit"),          # letters on a numbers menu
    ("XXXXL", ["One Size", "2", "4"], "One Size", "the menu's neutral choice"),   # never a 4 for a 4XL
    ("S/M", ["XS", "S", "M", "L"], "M", "the bigger of S/M"),
])
def test_every_menu_gives_a_value_in_the_owners_order(want, menu, value, how):
    assert fallback.menu_size(want, menu) == (value, how)


def test_the_menus_middle_is_the_last_choice_never_a_skip():
    assert fallback.menu_size("12 Months", ["2T", "3T", "4T"]) is None          # the caller's own next step …
    assert fallback.last_choice(["2T", "3T", "4T"]) == "3T"                      # … which is a value of the menu
    assert fallback.last_choice([]) is None


def test_a_category_the_form_lacks_is_its_closest():
    assert fallback.closest_option("Dress", ["Shirts & Tops", "Dresses", "Other"]) == ("Dresses", "the item's own words")
    assert fallback.closest_option("Gizmos", ["Tops", "Other"]) == ("Other", "the form's Other")
    assert fallback.closest_option("Gizmos", ["Tops", "Pants"])[0] == "Tops"


# ---------------------------------------------------------------- the agent's own size, at her price

def test_an_item_with_no_size_mark_goes_up_at_once_with_the_agents_size(env, tmp_path):   # noqa: F811
    """WO58 rule 3: no photo shows a size — the card still offers the size rows (the question, counted), but her price
    is enough: the ⭐ becomes the item's size at once, the size field only (never the title), and the item is queued."""
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, iid)
    assert approve.questions_today(db)["sizes"] == 1                             # the 9 PM line's count
    mid = bot.next_id
    handle_update(s, db, bot, _callback(f"approve:{iid}:120", mid))
    it = db.item(iid)
    assert it["status"] == "ready" and it["agent_size"] == "8" and it["owner_size"] is None
    assert not approve.needs_size(it)                                            # nothing waits for a size
    render = json.loads(it["renders"])["poshmark"]
    assert render["size_value"] == "8" and "size" not in render["title"].lower()  # never in the title
    assert bot.last_buttons()[-1][0]["text"] == "✓ $120 — queued"
    assert any(b["text"].startswith("✓ 8") for row in bot.last_buttons() for b in row)   # the rows stay
    assert approve.next_step(s, db, iid).endswith("in size 8 — tap another on its card to change it")
    assert approve.AGENT_SIZE_LINE.format(size="8") in approve.item_caption(iid, it)[0]
    assert approve.ask_sizes(s, db) == 0 and not group                          # "Waiting for a size" never sent


def test_an_item_priced_before_the_rule_and_skipped_for_no_size_is_settled_and_requeued(env, tmp_path):  # noqa: F811
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts(), status="ready")
    db.set_item(iid, owner_price=90)
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(iid, mp, status="skipped", error="skipped: no size on the item")
    assert approve.ask_sizes(s, db) == 0
    assert db.item(iid)["agent_size"] == "8"
    assert {r["marketplace"]: r["status"] for r in db.listings_for(iid)} == {"poshmark": "queued", "depop": "queued",
                                                                             "vinted": "queued"}


def test_her_late_tap_edits_every_site_the_item_is_live_on(env, tmp_path):  # noqa: F811
    """WO58: the agent's size went up on four sites; her tap of another size is her size now, and one "size" edit goes
    to each live site (Poshmark, Depop, Vinted, eBay); a site not live yet lists the new size by itself."""
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, iid)
    mid = bot.next_id
    handle_update(s, db, bot, _callback(f"approve:{iid}:120", mid))
    assert db.item(iid)["agent_size"] == "8"
    db.set_item(iid, status="posted")
    for mp, url in (("poshmark", "https://poshmark.com/listing/x-6acd8ea6ce5b0ac5ccde36eb"),
                    ("depop", "https://www.depop.com/products/shop-on-sneakers/"),
                    ("vinted", "https://www.vinted.com/items/123"), ("ebay", "https://www.ebay.com/itm/1234567890")):
        db.upsert_listing(iid, mp, status="posted", url=url,
                          fields_json={"category_id": "95672"} if mp == "ebay" else {})
    handle_update(s, db, bot, _callback(f"size:{iid}:9", mid))
    it = db.item(iid)
    assert it["owner_size"] == "9"
    rows = {r["marketplace"]: json.loads(r["payload"]) for r in
            db.conn.execute("SELECT * FROM listing_edits WHERE item_id=? AND kind='size'", (iid,))}
    assert set(rows) == {"poshmark", "depop", "vinted", "ebay"}, rows
    assert rows["poshmark"]["want"]["size"] == "9" and rows["ebay"]["fields"]["size"] == "9"
    assert rows["depop"]["fields"]["size"] and rows["vinted"]["fields"]["size"]
    assert "size" in edits.KINDS


# ---------------------------------------------------------------- the mappers never refuse a field value

def test_depop_and_vinted_never_refuse_a_size_or_a_package():
    from thrift_agent.catalogs.sizes import SizeIn, depop_size
    s = SizeIn("Women", "Shoes", "Sneakers", "running sneakers", None, None, None)
    v, note = depop_size(s, ["US 5", "US 6", "US 7"], "Women > Shoes > US")
    assert v in ("US 5", "US 6", "US 7") and "no size on the item" in note
    s = SizeIn("Women", "Dresses", "Midi", "wrap dress", "Q", None, None)
    v, note = depop_size(s, ["XS", "S", "M"], "Women > Dresses > US")
    assert v == "S" and "middle" in note


def test_ebays_written_ban_is_the_only_reason_and_never_for_new_or_swimwear():
    from thrift_agent.ebay import mapping
    from test_ebay_mapping import view
    assert mapping.policy_ban(view(cat="Intimates & Sleepwear", sub="Panties", item_type="cotton briefs")) is not None
    assert mapping.policy_ban(view(cat="Accessories", sub="Socks", item_type="wool crew socks")) is not None
    assert mapping.policy_ban(view(cat="Intimates & Sleepwear", sub="Panties", item_type="cotton briefs",
                                   cond="NWT")) is None                            # new: allowed
    assert mapping.policy_ban(view(cat="Swim", sub="Bikinis", item_type="bikini briefs")) is None
    assert mapping.policy_ban(view(cat="Intimates & Sleepwear", sub="Bras", item_type="lace bralette")) is None
    assert mapping.policy_ban(view(cat="Shoes", sub="Sandals", item_type="thong sandals")) is None
    ban = mapping.policy_ban(view(cat="Intimates & Sleepwear", sub="Panties", item_type="lace thong"))
    assert isinstance(ban, MappingError) and "Used underwear and socks are not allowed" in str(ban)
