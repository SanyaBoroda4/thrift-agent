"""WO54 §2 — the owner, 2026-11-09 9:16 AM: "I give you permission to take your own decisions in order to post it other
than just hold off till human will intrude."

Only four things wait for a person: the price (her card), a size no photo shows (the card's size buttons), a password
or sign-in code, a real CAPTCHA. Every other doubt gets the most reasonable choice, the post, and one line in the
item's private "Check —" message; every failure is tried again by itself; one 🚨 only for her hands or for code. The
agent never types a password and never solves a CAPTCHA. Live that morning: the 🏷️ question for the Glitter Toe
Sneakers got "тщ икфтв" ("no brand" on the Russian keyboard) — it went into Vinted's form and paused Vinted.

Fakes only: no network, no browser, no model."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import wo54_prompts
from PIL import Image
from test_crosslist_flow import (Stub, _posters, _settings as flow_settings, driver,  # noqa: F401 (fixtures)
                                 fields_for, loop, verified_selectors)
from test_pipeline import _jpg, _settings, _split, fake_ask, owner_messages  # noqa: F401 (owner_messages: fixture)

from thrift_agent import approve, crosslist, daily, pipeline, titles
from thrift_agent.brain import gate as gate_mod
from thrift_agent.db import DB, loads
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import Outcome
from thrift_agent.schema import Ev, Facts, Render


def _photos(tmp_path: Path, name: str = "item", n: int = 3) -> Path:
    d = tmp_path / name
    for i, c in enumerate(["red", "green", "blue"][:n]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    return d


def _processed(tmp_path, monkeypatch, facts, name="item", **kw):
    """An item through the whole intake (the model stubbed): (settings, db, item id)."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **k: facts(**{**kw, **k})))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {}, "aliases": {}, "category_defaults": {}})
    iid = db.add_item(_split(db, f"share-{name}", 3), 1, str(_photos(tmp_path, name)))
    pipeline.process_item(s, db, iid)
    return s, db, iid


def _card(db, iid) -> str:
    it = db.item(iid)
    return approve.item_caption(iid, it)[0]


def _buttons(db, iid) -> list[str]:
    return [b["text"] for row in approve.card_buttons(db, iid, db.item(iid), 25) for b in row]


# ---------------------------------------------------------------- the brand: decided, never asked

def test_an_unreadable_brand_is_listed_with_no_brand_and_nothing_asks(tmp_path, monkeypatch, facts):
    """A brand label in the photos that can't be read: no brand (Poshmark empty, Depop "Other", Vinted "List without
    brand"), said on the card as a statement, never a ❓; her price lists it at once."""
    s, db, iid = _processed(tmp_path, monkeypatch, facts,
                            brand=Ev(value=None, photos=[2], source="photo", confidence=0.0), brand_seen="unreadable")
    it = db.item(iid)
    gate = loads(it["gate"])
    assert gate["decision"] == "publish" and gate["questions"] == [] and gate["brand_unsure"]
    assert loads(it["facts"])["brand"]["value"] is None and loads(it["renders"])["poshmark"]["brand"] is None
    card = _card(db, iid)
    assert "⚠️ Brand: none — a brand label is in the photos but couldn't be read" in card and "❓" not in card
    assert approve.NO_BRAND_BUTTON not in _buttons(db, iid)                 # nothing to take away
    assert pipeline.set_price(s, db, iid, 25) == "ready"
    it = db.item(iid)
    assert not approve.needs_brand(it) and runner.approved(it, loads(it["renders"])["poshmark"])
    assert approve.ask_brands(s, db) == 0


def test_a_big_brand_read_with_doubt_is_listed_as_it_reads(tmp_path, monkeypatch, facts):
    """WO52: a big brand never ends as "no brand" — its unsure reading is the brand; the card offers [No brand]."""
    s, db, iid = _processed(tmp_path, monkeypatch, facts,
                            brand=Ev(value="Nike", photos=[1], source="photo", confidence=0.55), brand_seen="logo")
    it = db.item(iid)
    assert loads(it["facts"])["brand"]["value"] == "Nike"
    assert "⚠️ Brand: Nike — read with doubt (0.55); a big brand, so it's listed as Nike" in _card(db, iid)
    assert approve.NO_BRAND_BUTTON in _buttons(db, iid)
    assert pipeline.set_price(s, db, iid, 40) == "ready" and runner.approved(db.item(iid), {"price": 40})


def test_an_unsure_small_brand_is_never_put_on_a_listing(tmp_path, monkeypatch, facts):
    s, db, iid = _processed(tmp_path, monkeypatch, facts,
                            brand=Ev(value="Zell", photos=[2], source="photo", confidence=0.4), brand_seen="unreadable")
    it = db.item(iid)
    assert loads(it["facts"])["brand"]["value"] is None
    assert all(r.get("brand") is None for r in loads(it["renders"]).values())
    assert "Brand: none — the label reads “Zell” only with doubt (0.40)" in _card(db, iid)


def test_her_brand_reply_still_corrects_it_and_the_decision_goes(tmp_path, monkeypatch, facts):
    s, db, iid = _processed(tmp_path, monkeypatch, facts,
                            brand=Ev(value=None, photos=[2], source="photo", confidence=0.0), brand_seen="unreadable")
    assert approve.brand_reply("Zella", loads(db.item(iid)["gate"])) == "Zella"   # a plain reply is the brand
    pipeline.set_brand(s, db, iid, "Zella")
    it = db.item(iid)
    gate = loads(it["gate"])
    assert it["owner_brand"] == "Zella" and not any(n.startswith("Brand: ") for n in gate["notes"])
    assert "brand_unsure" not in gate and "Brand: none" not in _card(db, iid)


# ---------------------------------------------------------------- the card: statements, never questions

def test_a_new_card_holds_no_question_and_only_the_price_and_size_wait(tmp_path, monkeypatch, facts):
    """Every doubt at once — NWT without a tag photo, an unsure kids gender and category, an unreadable brand: the card
    says each as done (⚠️ / "my best reading"), asks nothing; the only buttons that wait are the prices (and a size
    row when no photo shows one)."""
    s, db, iid = _processed(tmp_path, monkeypatch, facts, department="Kids", category="Tops", subcategory=None,
                            kids_gender="unisex", kids_gender_confidence=0.4, category_confidence=0.5,
                            condition="NWT", condition_evidence=Ev(value="looks new", photos=[2], source="photo",
                                                                   confidence=0.9),
                            size_us=Ev(value="4T", photos=[1], source="photo", confidence=0.9),
                            brand=Ev(value=None, photos=[2], source="photo", confidence=0.0), brand_seen="unreadable")
    it = db.item(iid)
    gate = loads(it["gate"])
    assert gate["questions"] == [] and gate["decision"] == "publish" and not gate["ask_kids"] \
        and gate["ask_category"] == [] and gate["hold"] is None
    card = _card(db, iid)
    assert "❓" not in card and "?" not in card.replace(approve.CORRECT_HINT, "")
    assert f"⚠️ {pipeline.NWT_QUESTION}" in card
    assert approve.card(iid, it)[0] == "item"                                  # its one message: the price card
    assert set(_buttons(db, iid)) - {"Later", "Change", "Wrong photos", "‹ lower", "higher ›"} == \
        {b for b in _buttons(db, iid) if b.startswith(("$", "⭐$"))}            # nothing else to answer


def test_the_check_line_says_every_decision_once_and_asks_nothing(tmp_path, monkeypatch, facts):
    """The item's ONE private "Check —" line when it goes live: what was decided under doubt, without the old
    correction hints ("reply …", "check before approving"); her own answers since leave theirs out."""
    said = []
    monkeypatch.setattr(crosslist.notify, "say", said.append)
    s, db, iid = _processed(tmp_path, monkeypatch, facts, condition="fair", condition_alternative=None,
                            brand=Ev(value=None, photos=[2], source="photo", confidence=0.0), brand_seen="unreadable")
    db.set_item(iid, gate={**loads(db.item(iid)["gate"]), "info": [
        "kids: girls (not sure from the photos) — a reply 'boys' / 'girls' changes it",          # an older doc's words
        "model unsure of the condition (0.60): listed as good; reply with the condition if it is wrong"]})
    pipeline.set_price(s, db, iid, 25)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24)
    crosslist._say_checks(db, iid, ["poshmark"])
    [line] = [m for m in said if m.startswith("Check — ")]
    assert "- Brand: none — a brand label is in the photos but couldn't be read, so it's listed with no brand" in line
    assert "- looked well-worn — listed as Good" in line and "- kids: girls (not sure from the photos)" in line
    assert "- model unsure of the condition (0.60): listed as good" in line
    assert "reply" not in line.lower() and "check before approving" not in line and "?" not in line
    crosslist._say_checks(db, iid, ["poshmark"])
    assert len([m for m in said if m.startswith("Check — ")]) == 1                 # once


# ---------------------------------------------------------------- the category: the closest real path

@pytest.mark.parametrize("kw,path", [
    (dict(department="Unisex", category="Gadgets", subcategory=None, item_type="puffer jacket"),
     ("Women", "Jackets & Coats")),                                          # no Unisex on Poshmark: Women, its words
    (dict(department="Unisex", department_alternative="Men", category="Gadgets", subcategory=None,
          item_type="graphic tee"), ("Men", "Shirts")),                       # the department the model weighed
    (dict(category="Other", subcategory=None, item_type="unknown item"), ("Women", "Other")),   # Poshmark's own Other
])
def test_a_category_poshmark_lacks_is_the_closest_real_one_never_a_question(tmp_path, monkeypatch, facts, kw, path):
    s, db, iid = _processed(tmp_path, monkeypatch, facts, size_us=Ev(value="M", photos=[1], source="photo",
                                                                        confidence=0.9), **kw)
    it = db.item(iid)
    gate, posh = loads(it["gate"]), loads(it["renders"])["poshmark"]
    assert (posh["department"], posh["category"]) == path
    assert gate["questions"] == [] and gate["decision"] == "publish"
    assert pipeline.set_price(s, db, iid, 30) == "ready" and runner.approved(db.item(iid), {"price": 30})


# ---------------------------------------------------------------- the title: built from the facts, never held

def test_a_title_nothing_could_fix_is_built_from_the_facts(facts, monkeypatch):
    f = facts(brand=Ev(value="Sunshine Coast Mountain Wilderness Outfitters Collective", photos=[1], source="photo",
                       confidence=0.9), item_type="wide leg sweater pants", category="Pants & Jumpsuits",
              subcategory="Wide Leg", colors=["Gray", "Blue"], condition="NWT")
    for site in (None, "poshmark", "depop", "vinted"):
        t = titles.fallback(f, site)
        assert titles.check(t, site) == [] and t.startswith("Sunshine Coast") and "size 7.5" in t, (site, t)
    bare = facts(brand=Ev(value=None), item_type="", category="Other", subcategory=None, colors=[])
    assert titles.check(titles.fallback(bare)) == [] and titles.fallback(bare).strip()
    monkeypatch.setattr(titles, "rewrite", lambda *a, **k: None)              # the model's one rewrite fails too
    broken = "TORY BURCH!!! SKU 123456 flats flats"
    monkeypatch.setattr(titles, "check", lambda t, site=None: ["a test's rule"] if "!!!" in t else [])
    new, note = pipeline.shop_window_title(None, broken, facts())
    assert new == titles.fallback(facts()) and note.startswith("title: ") and "built from the facts" in note


def test_the_poster_never_holds_a_title_it_replaces_it(tmp_path, monkeypatch, facts):
    """runner.shop_window_render: an item processed before (its title breaking the rules, the fix unable) goes up
    with the title built from its facts — never None any more ("it waits; reply to its card with the title")."""
    s = flow_settings(tmp_path)
    db = DB(s.path("db"))
    bid = db.add_batch("share", 1)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, 1, str(tmp_path / "i"))
    r = Render(marketplace="poshmark", title="Tory Burch Red Ballet Flats size 7.5 !!!", description="Red flats.",
               tags=[], brand="Tory Burch", department="Women", category="Shoes", subcategory="Flats & Loafers",
               size="7.5", colors=["Red"], condition="good", price=85, photos=[], sku=iid)
    db.set_item(iid, status="ready", owner_price=85, facts=facts().model_dump(), gate={"decision": "publish"},
                renders={"poshmark": r.model_dump()})
    real = titles.check
    monkeypatch.setattr(titles, "check", lambda t, site=None: ["a test's rule"] if "!!!" in t else real(t, site))
    monkeypatch.setattr(titles, "fix", lambda t, *a, **k: t)                   # the deterministic fix can't help
    out = runner.shop_window_render(db, db.item(iid), r, loads(db.item(iid)["renders"]))
    assert out is not None and out.title == titles.fallback(facts()) and "!!!" not in out.title
    assert loads(db.item(iid)["renders"])["poshmark"]["title"] == out.title
    assert any(c.startswith("title: ") for c in loads(db.item(iid)["gate"])["checks"])


def test_a_site_title_that_still_breaks_is_replaced_never_a_skipped_site(tmp_path, monkeypatch, facts):
    from types import SimpleNamespace
    db = DB(tmp_path / "state.db")
    bid = db.add_batch("share", 1)
    iid = db.add_item(bid, 1, str(tmp_path / "i"))
    db.set_item(iid, gate={"decision": "publish"})
    real = titles.check
    monkeypatch.setattr(titles, "check", lambda t, site=None: ["a test's rule"] if "!!!" in t else real(t, site))
    monkeypatch.setattr(titles, "for_site", lambda t, *a, **k: t)
    view = SimpleNamespace(facts=facts())
    fields = SimpleNamespace(title="Tory Burch Red Ballet Flats !!!", brand="Tory Burch")
    out = crosslist.strict_title(db, iid, "vinted", fields, view)            # was: MappingError, Vinted skipped
    assert out.title == titles.fallback(facts(), "vinted", 0) and "!!!" not in out.title
    fields = SimpleNamespace(description="Tory Burch Red Ballet Flats !!!\n\nRed flats.", brand="Tory Burch")
    out = crosslist.strict_title(db, iid, "depop", fields, view)
    assert out.description.split("\n")[0] == titles.fallback(facts(), "depop", 0)
    assert out.description.endswith("\n\nRed flats.")
    assert len(loads(db.item(iid)["gate"])["checks"]) == 2                     # said in its "Check —" line


# ---------------------------------------------------------------- the old questions, decided at the worker's start

def test_the_items_asked_before_are_decided_once(tmp_path, monkeypatch, facts):
    """An item processed before WO54 whose card asks — NWT, a re-share, "Other" — gets the decision: the questions
    become the card's statements, the unplaceable category is processed again; nothing waits."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline, "_photos_of", lambda it: ([], []))
    bid = _split(db, "share", 3)

    def asked(questions, **kw):
        iid = db.add_item(bid, len(db.conn.execute("SELECT id FROM items").fetchall()) + 1, str(_photos(tmp_path,
                                                                                                   f"i{len(questions)}")))
        r = Render(marketplace="poshmark", title="Tory Burch Red Ballet Flats size 7.5", description="Red flats.",
                   tags=[], brand="Tory Burch", department="Women", category="Shoes", subcategory="Flats & Loafers",
                   size="7.5", colors=["Red"], condition="like_new", price=85, photos=[], sku=iid)
        db.set_item(iid, status="ready", owner_price=85, facts=facts(**kw).model_dump(), renders={"poshmark":
                    r.model_dump()}, price={"target": 70, "list_price": 85, "source": "owner"},
                    gate={"decision": "needs_info", "reasons": list(questions), "questions": list(questions),
                          "notes": [], "info": []})
        return iid
    twin = "looks like item i_261001_aaaaaa (Old Flats) — same item? Reply 'same item' to drop it; a price lists it"
    nwt = asked([pipeline.NWT_QUESTION + " — reply 'NWT' if the tag is attached", twin])
    other = asked([gate_mod.OTHER_CATEGORY_QUESTION], category="Other", subcategory=None)
    assert sorted(pipeline.apply_decide_rule(s, db)) == sorted([nwt, other])
    g = loads(db.item(nwt)["gate"])
    assert g["questions"] == [] and g["decision"] == "publish" and any(n.startswith("looks like item") for n in
                                                                       g["notes"])
    assert pipeline.twin_of(db.item(nwt)) == "i_261001_aaaaaa"                   # settle_twin still finds it
    assert db.item(other)["status"] == "new"                                     # processed again: decided then
    assert pipeline.apply_decide_rule(s, db) == []                               # once


# ---------------------------------------------------------------- the whole poster loop: it goes up

@pytest.mark.parametrize("driver", ["playwright"], indirect=True)
def test_an_item_whose_brand_couldnt_be_read_goes_up_on_all_three_sites(tmp_path, loop):
    """Priced, the card holding the old 🏷️ question: Poshmark, Depop and Vinted all take it (the site's own no-brand
    choice); the group hears its ✅ card, nobody is asked."""
    said, run = loop
    s = flow_settings(tmp_path)
    db = DB(s.path("db"))
    bid = db.add_batch("share", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, 1, "work/1")
    r = Render(marketplace="poshmark", title="Red Ballet Flats size 7.5", description="Red flats.", tags=[],
               brand=None, department="Women", category="Shoes", subcategory="Flats & Loafers", size="7.5",
               colors=["Red"], condition="good", price=85, photos=[], sku=iid)
    db.set_item(iid, status="ready", owner_price=85, renders={"poshmark": r.model_dump()},
                gate={"decision": "needs_info", "reasons": ["brand unclear (None, 0.00)"],
                      "questions": ["Brand? Couldn't read it — reply 'brand …'"], "notes": [], "info": []})
    ps = _posters()
    run(s, db, ps)
    assert {m: x["status"] for m, x in ((x["marketplace"], x) for x in db.listings_for(iid))} == \
        {"poshmark": "posted", "depop": "posted", "vinted": "posted"}
    assert not any("🏷️" in m or "What brand" in m for m in [*said.group, *said.ops])


# ---------------------------------------------------------------- the waits that remain: four, and only four

# What may still prompt a reply or ask a question, by category. WAITS are what waits for a person — the owner's four:
# the price, a size no photo shows, a password / sign-in code, a real CAPTCHA (the last two are the sites' own pages —
# WO56: one private message, "reply show", the owner's hands on the Mac's screen; never the group). Everything else
# never waits.
WAITS = {"price", "size", "sign-in", "captcha"}
NEVER_WAITS = {
    "correction": "a reply changes a decision already made — the item goes up without it",
    "answer": "the answer to a message of hers (what a reply must look like, why it couldn't be taken)",
    "her request": "a reply to what she asked for herself ([Wrong photos], `thrift titles --review`, hand-listed)",
    "old flow": "segmentation.auto_confirm: false — her own setting (the contact sheet of before WO20b)",
    "legacy": "handles a message sent before WO37/WO53/WO54 — nothing sends one any more",
    "record": "the item's or the share's record (gate reasons, the share ledger, thrift status) — never sent",
    "log": "the event log's words for what a handler did — never sent",
    "operator": "the terminal (`thrift …` on the Mac) or stderr",
    "model": "a prompt to the model",
    "regex": "a pattern, not words",
    "statement": "a statement with a question mark in it — nothing to answer",
    "other agent": "WO54's parallel work changes it (the lead's §1.4 families, the Chrome lifecycle)",
}
ALLOWED = [
    # the price — her card, its hints, its [Change]
    ("approve.py", "Tap a price, type a number, or reply with an answer like", "price"),
    ("approve.py", "No price yet: type one", "price"),
    ("approve.py", "is under the ${…} floor — reply to the card with", "price"),
    ("approve.py", "Reply to this message with the price (or just type it).", "price"),
    # a size no photo shows — the card's size buttons and the one line about them
    ("approve.py", "tap a size to list it", "size"),
    ("approve.py", "Waiting for a size: {title} — tap one on its card", "size"),
    ("approve.py", "📏 No size on the photos — tap it below", "size"),
    ("approve.py", "Reply with the size, e.g. 'size S' or 'size 8.5'", "size"),
    ("approve.py", "Which item? Reply 'size …' to its card", "size"),
    ("pipeline.py", "which size? Reply 'size S'", "size"),
    ("crosslist.py", "— it goes up once you reply 'size …' to its card", "size"),
    # WO56: a password / sign-in code and a real CAPTCHA — the owner's private chat (never the group): "show" brings the
    # site's own page to the front on the Mac's screen; his hands do it, the agent never types or solves it
    ("humansteps.py", "wants you to sign in{…}, since {…}. Connect to the Mac's screen (Screen Sharing, {…}), reply "
                      "show and type the password or code yourself", "sign-in"),
    ("humansteps.py", "wants a person — {…}{…}, since {…}. 1 minute: connect to the Mac's screen (Screen Sharing, "
                      "{…}), reply show,", "captcha"),
    ("humansteps.py", "still wants a person — {…}, since {…}. Connect to the Mac's screen (Screen Sharing, {…}) and "
                      "reply show — or later", "captcha"),
    ("humansteps.py", "for 12 hours — it still waits; reply show when you're at a screen.", "captcha"),
    ("humansteps.py", " · reply show in the private chat", "record"),
    # corrections — shown with a decision made, never awaited
    ("approve.py", "To change: reply size … · brand …", "correction"),
    ("approve.py", " — my best reading; reply 'size …' to change it", "correction"),
    ("approve.py", " — my best reading; reply 'men' or 'women' to change it", "correction"),
    ("approve.py", "Reply 'price 80' here to change the price on every site", "correction"),
    ("pipeline.py", "so they go on without it (a reply to the right item's card add", "correction"),
    # answers to her own messages
    ("approve.py", "Reply with the brand, e.g. 'Zella', or 'no brand'", "answer"),
    ("approve.py", "Replies here act on an 'Unmatched sale' message", "answer"),
    ("approve.py", "I can't tell which item that is — reply 'no drop' to the item's own card", "answer"),
    ("approve.py", "Poshmark has no {…} under {…} — reply 'category {…} › …' with its name", "answer"),
    ("approve.py", "The sales tracker didn't answer — reply again in a minute", "answer"),
    ("approve.py", "Condition: reply 'condition nwt'", "answer"),
    ("pipeline.py", "Poshmark has no category {…} — reply e.g. 'Skirts › Skirt Sets'", "answer"),
    ("pipeline.py", "not ready — reply to its card with the fix first", "answer"),
    ("pipeline.py", "photos {…} are in no item — reply e.g.", "answer"),
    # what she asked for herself
    ("approve.py", "Reply with the fix: 12>2 | split 7", "her request"),
    ("approve.py", "Wrong photos? Batch {…}", "her request"),
    ("pipeline.py", "is waiting for its photo fix ([Wrong photos]) — answer that first", "her request"),
    ("titles.py", "Reply 'go titles' to change them on the sites", "her request"),
    ("handlisted.py", "🔎 Listed by hand already? I read your shops", "her request"),
    ("shares.py", "reply to the photo sheet", "record"),
    ("shares.py", "her reply to the photo sheet ([Wrong photos])", "record"),
    # her own setting: segmentation.auto_confirm false
    ("approve.py", "Reply to this message: ok | 12>2", "old flow"),
    ("ingest/segment.py", "matches no item — reply '{…}>2' (into item 2) or 'drop {…}'", "old flow"),
    # the messages of before — handled, never sent any more
    ("approve.py", "(still held as a possible re-share: reply 'different item'", "legacy"),
    ("approve.py", "Brand new or worn? (couldn't tell from the photos)", "legacy"),
    ("approve.py", "Tap a button, or reply nwt / like new / good", "legacy"),
    ("approve.py", "Girls or Boys? (Poshmark files kids sizes under one of them)", "legacy"),
    ("approve.py", "Tap a button, or reply girls / boys", "legacy"),
    ("approve.py", "Which category? (not sure from the photos)", "legacy"),
    ("approve.py", "Tap a category, or reply with one", "legacy"),
    ("approve.py", "🏷️ What brand is {title}? I couldn't read it", "legacy"),
    ("approve.py", "next: brand new or worn? (the buttons above)", "legacy"),
    ("approve.py", "next: Girls or Boys?", "legacy"),
    ("approve.py", "Reply to this message with the answer.", "legacy"),
    ("approve.py", " — answer the question above first", "legacy"),
    ("brain/gate.py", "Category? — reply e.g. 'category Tops'", "legacy"),
    ("pipeline.py", "first its condition: brand new or worn? — tap NWT, Like New or Good", "legacy"),
    ("shares.py", "answer (brand new or worn?)", "legacy"),
    ("shares.py", "answer (Girls or Boys?)", "legacy"),
    ("shares.py", "answer (which category?)", "legacy"),
    # the record: taxonomy.fit's reasons (settle_category places the item; the check line drops the hint), doubts
    ("brain/taxonomy.py", "department — reply 'department Women' or 'department Men'", "record"),
    ("brain/taxonomy.py", "categories — reply e.g. 'category {…}'", "record"),
    ("brain/taxonomy.py", "listed without one; reply e.g. 'subcategory {…}' to set it", "record"),
    ("ingest/segment.py", "— two items?", "record"),
    ("ingest/segment.py", "— one item?", "record"),
    # the event log
    ("approve.py", ": unreadable reply {…}", "log"),
    ("approve.py", ": empty reply", "log"),
    ("approve.py", "ignored: not a reply to the bot", "log"),
    ("approve.py", "a size needs a reply to its card", "log"),
    ("post/ext_driver.py", "(another poster has it?)", "log"),
    ("post/runner.py", "could not claim {…} (another poster has it?)", "log"),
    ("pipeline.py", "(moved away, or still downloading from iCloud?)", "log"),
    ("post/ext_driver.py", "the Thrift Chrome extension didn't take the job in {…} min — is the Thrift Chrome open?",
     "log"),
    # the terminal
    ("approve.py", "TELEGRAM_ALLOWED_USER_IDS is empty", "operator"),
    ("cli.py", "no updates seen — send the bot a message (in a group: reply to it or /start)", "operator"),
    ("cli.py", "Publish this one item, supervised: on the Mac", "operator"),
    ("cli.py", "Open the sell pages in the Thrift Chrome: logged in?", "operator"),
    ("cli.py", "which of our items she already listed by hand?", "operator"),
    ("cli.py", "the Thrift Chrome extension didn't connect in 2 minutes — is the Thrift Chrome open?", "operator"),
    ("bridge.py", "is it on at chrome://extensions, and its token saved in its options?", "operator"),
    ("bridge.py", "is the poster service running? Stop it first", "operator"),
    ("post/runner.py", "Type LIST to publish", "operator"),
    ("post/runner.py", "Type POST to publish", "operator"),
    ("post/runner.py", "is the poster service still running? Stop it first", "operator"),
    ("post/takedown.py", "is the poster service still running? Stop it first", "operator"),
    # the model's prompts
    ("heal.py", "Which control is the {…} field?", "model"),
    # patterns
    ("catalogs/categories.py", "mary ?janes?", "regex"),
    ("catalogs/categories.py", "flip[- ]?flops?", "regex"),
    ("catalogs/categories.py", "rain ?boots?", "regex"),
    # statements
    ("edits.py", "changed by hand?", "statement"),
    ("pipeline.py", "(Wi-Fi, Low Power Mode, iCloud storage?)", "statement"),
    ("pipeline.py", "(deleted on the phone, or moved?)", "statement"),
    # WO54's parallel work: the families (the lead's §1.4) and the Thrift Chrome's lifecycle (§4)
    ("crosslist.py", "it waits until the category is fixed (reply 'category …' to its card", "other agent"),
    ("bridge.py", "🧩 The Thrift Chrome extension hasn't checked in for 10 minutes", "other agent"),
]


def test_only_the_four_waits_remain_in_the_agents_own_words():
    """Every string in thrift_agent/ that asks a question or prompts a reply / a tap is one of ALLOWED — and of those
    only the four waits wait: the price and a size no photo shows (her card), a sign-in / code and a real CAPTCHA
    (WO56: the owner's private chat — "reply show", his hands on the Mac's screen; the group never hears them)."""
    unlisted = []
    for rel, line, text in wo54_prompts.flagged():
        if not any(f == rel and piece in text for f, piece, _ in ALLOWED):
            unlisted.append(f"{rel}:{line}: {text[:140]!r}")
    assert not unlisted, ("a question or a reply prompt that isn't one of the four waits (WO54 §2 — decide, post, one "
                          "check line; or add it to ALLOWED with why it never waits):\n" + "\n".join(unlisted))
    assert {c for _, _, c in ALLOWED} <= WAITS | set(NEVER_WAITS)
    assert {c for _, _, c in ALLOWED if c in WAITS} == WAITS
    assert {f for f, _, c in ALLOWED if c in ("sign-in", "captcha")} == {"humansteps.py"}   # WO56: one place says them


def test_the_scan_finds_a_question_or_a_reply_prompt_wherever_it_is_written():
    src = '''
X = "🏷️ What brand is {title}? reply to this message"
def f(title):
    """A docstring may ask? It is never sent."""
    notify.say(f"Which size is {title}? Tap one below")
    bot.send_message("Reply with the colour", reply_to=1)
    re.compile(r"colou?r(?:s)? reply")
    llm.text("Which picture shows the item upright?")
    return Field(description="What the seller must answer?")
'''
    found = [text for _, _, text in wo54_prompts.scan(src)]
    assert found == ["🏷️ What brand is {title}? reply to this message", "Which size is {…}? Tap one below",
                     "Reply with the colour"]


def test_the_groups_action_lines_are_a_sign_in_a_captcha_a_check_and_a_block_that_passes_by_itself():
    """The only lines that ask her hands on the Mac for a site: a login, a CAPTCHA, a check page (a sign-in code) —
    and the block page's, which asks nothing."""
    assert set(crosslist.ALERTS) == {"login", "captcha", "verify", "block"}
    assert crosslist.ALERTS["login"] == "{site} needs you to log in on the Mac."
    assert crosslist.ALERTS["captcha"] == "{site} shows a CAPTCHA — solve it in its Chrome window on the Mac."
    assert crosslist.ALERTS["verify"] == "{site} asks for a check — open it on the Mac."
    assert "by myself" in crosslist.ALERTS["block"]


def test_nothing_but_a_size_is_named_as_waiting_for_her():
    import inspect
    assert "BRAND_WAIT" not in inspect.getsource(daily.owner_waits).split('"""', 2)[2]   # the 💤 line: a size only
    assert approve.needs_brand(None) is False
    assert "needs_brand" not in inspect.getsource(runner.approved).split('"""', 2)[2]
    assert "needs_brand" not in inspect.getsource(crosslist.pending)
    src = inspect.getsource(pipeline.process_item)
    assert '"questions": []' in src and "needs_info" not in src.split("gate_doc = ")[1].split("\n")[0]


def test_a_gate_never_holds_and_never_asks(facts, gate_cfg, pricing_cfg):
    from thrift_agent.schema import PriceResult
    pr = PriceResult(target=70, list_price=85, source="brand")
    for kw in (dict(brand=Ev(value=None, photos=[2], confidence=0.0), brand_seen="unreadable"),
               dict(brand=Ev(value="Zell", photos=[2], confidence=0.4)), dict(category="Other"),
               dict(condition="NWT", condition_evidence=Ev(value="looks new", photos=[], source="photo",
                                                           confidence=0.9)),
               dict(size_us=Ev(value=None)), dict(condition_evidence=Ev(value="x", photos=[1], confidence=0.3))):
        r = gate_mod.evaluate(facts(**kw), pr, ["a lint finding"], 3, gate_cfg, pricing_cfg)
        assert r.decision == "publish" and r.questions == [], kw
        assert not any("reply" in n.lower() or "?" in n for n in r.notes), kw
