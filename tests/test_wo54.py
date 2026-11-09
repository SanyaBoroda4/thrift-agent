"""WO54 (2026-11-09): the morning's causes — each built from that morning's evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thrift_agent import crosslist, formcheck, health, notify
from thrift_agent.db import DB
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import Outcome

T0 = datetime(2026, 11, 9, 13, 1, 46, tzinfo=timezone.utc)          # 8:01:46 AM EST, the snowsuit's first failure
SNOWSUIT = "i_261109_76ba86"
SNOWSUIT_ERR = ("PosterError: after picking '12 Months' the size menu neither showed Done nor closed with that size on "
                "the form (the size field shows '12M')")
SNEAKERS = "i_261109_755102"
SNEAKERS_ERR = "Mismatch: brand shown тщ икфтв (plan List without brand, No brand, No brand indicated)"


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    monkeypatch.setattr(notify, "ops_photo", lambda shot, text: out.append(text))
    monkeypatch.setattr(notify, "photo", lambda shot, text: out.append(text))
    monkeypatch.setattr(notify, "group", lambda text: out.append("GROUP " + text))
    return out


# ---------------------------------------------------------------- §1.2 rule 4: one item never stops its site

@pytest.mark.parametrize("error", [SNOWSUIT_ERR, SNEAKERS_ERR,
                                   "Mismatch: package nothing (plan package (any of Depops; the plan Large))",
                                   "Vinted brand: a big brand is never 'List without brand' (Nike)"])
def test_an_error_about_one_items_value_is_the_items(error):
    """Live 08:01 and 08:46: both were tagged "site" — and paused Poshmark and Vinted for every item."""
    assert health.kind_of(error) == health.ITEM
    assert health.kind_of(error, "site") == health.ITEM


def test_a_site_change_is_still_the_sites():
    assert health.kind_of("Depop shipping: not on the page ([data-testid='x'])") == health.SITE
    assert health.kind_of("PosterError: unknown dialog after Next") == health.SITE


def _failed(db, iid, mp, error, at, attempts):
    db.upsert_listing(iid, mp, status="failed", error=error, attempts=attempts)
    db.conn.execute("UPDATE listings SET updated_at=? WHERE item_id=? AND marketplace=?",
                    (at.isoformat(timespec="seconds"), iid, mp))


def test_the_snowsuit_morning_never_pauses_poshmark(db, said):
    """08:01, 08:02, 08:03: the snowsuit alone failed three times → Poshmark paused for all 20. Now: twice, then set
    aside on Poshmark with one line; Poshmark goes on with the next item; the snowsuit comes back by itself later."""
    others = [f"i_261109_o{n:02}" for n in range(3)]
    for iid in (SNOWSUIT, *others):
        db.upsert_listing(iid, "poshmark", status="queued")
    # first failure: recorded, the brake fed (one item: nothing)
    _failed(db, SNOWSUIT, "poshmark", SNOWSUIT_ERR, T0, attempts=1)
    assert not health.brake(db, "poshmark", SNOWSUIT, SNOWSUIT_ERR, at=T0)
    assert crosslist.retry_failed(None, db, at=T0 + timedelta(minutes=31)) == [(SNOWSUIT, "poshmark")]
    assert db.listing(SNOWSUIT, "poshmark")["status"] == "queued"
    # second failure → set aside, never a pause
    _failed(db, SNOWSUIT, "poshmark", SNOWSUIT_ERR, T0 + timedelta(minutes=32), attempts=2)
    assert not health.brake(db, "poshmark", SNOWSUIT, SNOWSUIT_ERR, at=T0 + timedelta(minutes=32))
    crosslist.retry_failed(None, db, at=T0 + timedelta(minutes=63))
    row = db.listing(SNOWSUIT, "poshmark")
    assert row["status"] == "failed" and row["error"].startswith(health.SET_ASIDE + "PosterError: after picking")
    assert not health.paused(db, "poshmark") and not crosslist.blocked(db, "poshmark")
    lines = [m for m in said if m.startswith("⏸ Poshmark: ")]
    assert len(lines) == 1 and "set aside after 2 tries" in lines[0] and "Poshmark goes on with the other items" in lines[0]
    # the next item goes through: the run ends; the others were never touched
    health.worked(db, "poshmark", others[0])
    assert all(db.listing(i, "poshmark")["status"] == "queued" for i in others)
    # not before its hold … then back in line with fresh attempts
    assert crosslist.retry_failed(None, db, at=T0 + timedelta(minutes=70)) == []
    later = T0 + timedelta(minutes=63) + timedelta(seconds=health.ASIDE_HOLD[0] + 1)
    assert crosslist.retry_failed(None, db, at=later) == [(SNOWSUIT, "poshmark")]
    row = db.listing(SNOWSUIT, "poshmark")
    assert row["status"] == "queued" and row["attempts"] == 0


def test_a_poster_start_lets_every_set_aside_item_go_again(db, said):
    db.upsert_listing(SNEAKERS, "vinted", status="queued", attempts=2)
    health.aside(db, "vinted", SNEAKERS, SNEAKERS_ERR, at=datetime.now(timezone.utc))
    assert not health.aside_due(db, SNEAKERS, "vinted")
    assert health.release_asides(db) == 1 and health.aside_due(db, SNEAKERS, "vinted")
    assert crosslist.retry_failed(None, db, at=datetime.now(timezone.utc)) == [(SNEAKERS, "vinted")]


def test_the_third_set_aside_raises_one_alarm_and_it_still_comes_back(db, said, monkeypatch):
    raised = []
    from thrift_agent import alarm
    monkeypatch.setattr(alarm, "raise_for", lambda db_, key, text, **k: raised.append((key, text)))
    for n in range(health.ASIDE_ALARM):
        health.aside(db, "vinted", SNEAKERS, SNEAKERS_ERR, at=T0 + timedelta(days=n))
    assert len(raised) == 1 and raised[0][0] == f"vinted:aside:{SNEAKERS}"
    assert health.asides(db)[f"{SNEAKERS}|vinted"]["until"]                 # still tried again later


def test_the_same_failure_on_different_items_still_pauses(db, said):
    for iid in ("a", "b"):
        db.upsert_listing(iid, "vinted", status="queued", attempts=1)
    err = "Vinted brand: not on the page ([data-testid=brand])"
    assert health.non_item(db, "vinted", "a", err, health.SITE) == "requeued"
    assert health.non_item(db, "vinted", "b", err, health.SITE) == "paused"
    assert not health.brake(db, "depop", "a", SNEAKERS_ERR)
    assert health.brake(db, "depop", "b", SNEAKERS_ERR)                     # two items, the same mismatch
    assert health.paused(db, "depop").startswith("failures: the same failure on 2 different listings in a row")


def test_an_infra_failure_is_never_the_items(db, said):
    """08:24:11: "Vinted cancelled: the bridge went away before the go-ahead" (the lid closed) — never sets it aside."""
    db.upsert_listing("i_261109_600947", "vinted", status="queued", attempts=1)
    err = "Vinted cancelled: the bridge went away before the go-ahead (the poster or the CLI stopped)"
    assert health.kind_of(err) == health.INFRA
    for _ in range(4):
        assert health.non_item(db, "vinted", "i_261109_600947", err, health.INFRA) == "requeued"
    row = db.listing("i_261109_600947", "vinted")
    assert row["status"] == "queued" and row["attempts"] == 0 and not health.paused(db, "vinted")


# ---------------------------------------------------------------- the self-test: the canary, never the failed item

def test_poshmarks_self_test_runs_on_the_canary(db, said, monkeypatch, tmp_path):
    """08:38 live: Poshmark's self-test re-tried the very snowsuit that had failed (✗, next test 11:31)."""
    health.pause(db, "poshmark", f"site: {SNOWSUIT_ERR}", health.SITE, error=SNOWSUIT_ERR, at=T0)
    monkeypatch.setattr(formcheck, "ensure_canary", lambda s, db_: dict(id="i_canary_bd8fed"))
    monkeypatch.setattr(formcheck, "_render", lambda it: SimpleNamespace(title="canary", price=10, sku=it["id"]))
    seen = []

    class Posh:
        async def post(self, ctx, render, mode, dry, shots, stage=None):
            seen.append((render.sku, dry, stage))
            return Outcome("dryrun", screenshot=str(tmp_path / "x.png"))

    s = SimpleNamespace(path=lambda k: tmp_path / k, get=lambda k, d=None: d, flag_set=lambda f: False)
    monkeypatch.setattr(parallel, "can_post", lambda *a, **k: (True, "ok"))
    assert asyncio.run(parallel._poshmark_test(s, db, {"poshmark": Posh()}, None, "auto")) is True
    assert seen == [("i_canary_bd8fed", True, "form")]
    assert not health.paused(db, "poshmark")                                # the canary passed: resumed


def test_depop_and_vinteds_self_test_runs_on_the_canary(db, said, monkeypatch, tmp_path):
    health.pause(db, "vinted", f"item: {SNEAKERS_ERR}", health.SITE, error=SNEAKERS_ERR, at=T0)
    db.upsert_listing(SNEAKERS, "vinted", status="queued", attempts=1)
    monkeypatch.setattr(formcheck, "ensure_canary", lambda s, db_: dict(id="i_canary_bd8fed"))
    ran = []

    async def run_cross(s, db_, ps, ctx, iid, mp, **k):
        ran.append((iid, mp, k.get("dry"), k.get("request")))
        return Outcome("dryrun")
    monkeypatch.setattr(runner, "run_cross", run_cross)
    monkeypatch.setattr(parallel, "can_post", lambda *a, **k: (True, "ok"))
    assert asyncio.run(parallel._cross_test(None, db, {"vinted": object()}, "vinted", "auto")) is True
    assert ran == [("i_canary_bd8fed", "vinted", True, True)]                # never the sneakers
    assert not health.paused(db, "vinted")


# ---------------------------------------------------------------- §1.3 the other keyboard layout, implausible brands

from test_wo39 import FakeBot, _item, _reply, env  # noqa: E402, F401, F811 (env: fixture)

from thrift_agent import approve, layout, pipeline  # noqa: E402


@pytest.mark.parametrize("typed,meant", [
    ("тщ икфтв", "no brand"),            # live 8:43, the Glitter Toe Sneakers
    ("Тщ икфтв", "No brand"),
    ("ышяу Ь", "size M"),
    ("зщые тщц", "post now"),
    ("здфсуы", "places"),                # not a command and no brand we know: left as typed
])
def test_a_reply_typed_on_the_russian_layout_is_read_on_the_latin_one(typed, meant):
    got, relaid = layout.normalize(typed)
    if meant == "places":
        assert (got, relaid) == (typed, False)
    else:
        assert (got, relaid) == (meant, True)


def test_plain_replies_are_never_changed():
    for t in ["28", "$35", "no brand", "Zella", "size 6", "Привет", "Mick Mack"]:
        assert layout.normalize(t) == (t, False)


@pytest.mark.parametrize("brand,ok", [("Zella", True), ("New Balance", True), ("J. Crew", True), ("H&M", True),
                                      ("Mick Mack", True), ("тщ икфтв", False), ("???", False), ("12", False),
                                      ("later", False), ("size M", False), ("please list it with no brand thanks", False)])
def test_a_brand_reply_is_typed_only_when_it_is_a_brand(brand, ok):
    assert layout.plausible_brand(brand) is ok


def _sneakers(db, tmp_path, facts):
    iid = _item(db, tmp_path, facts, "Glitter Toe Sneakers with Leopard Star, White & Silver, Toddler size 6",
                status="ready", size="6", name="sneakers")
    db.set_item(iid, owner_price=18)
    return iid


def _ask_brand(db, bot, iid):
    text = "🏷️ What brand is Glitter Toe Sneakers? reply with the brand, or 'no brand'."
    approve.once(db, bot, "brand", iid, text, lambda: bot.send_message(text))
    return bot.next_id


def test_the_sneakers_tshch_ikftv_reply_is_no_brand(env, tmp_path, facts, monkeypatch):  # noqa: F811
    """Live 8:43:09: "тщ икфтв" set the brand to "тщ икфтв" — Depop titled "Тщ икфтв Glitter Toe Sneakers…", Vinted
    failed three times. Now it is "no brand"."""
    s, db, said = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _sneakers(db, tmp_path, facts)
    _reply(db, s, bot, "тщ икфтв", to=_ask_brand(db, bot, iid))
    it = db.item(iid)
    assert it["owner_brand"] == pipeline.NO_BRAND
    assert "тщ" not in (approve.loads(it["renders"])["poshmark"]["title"] or "").lower()
    assert db.conn.execute("SELECT 1 FROM events WHERE kind='reply_relaid'").fetchone()


def test_an_implausible_brand_reply_is_no_brand_with_one_check_line(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, said = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    lines = []
    monkeypatch.setattr(approve.notify, "say", lines.append)
    iid = _sneakers(db, tmp_path, facts)
    _reply(db, s, bot, "абвгд ежз", to=_ask_brand(db, bot, iid))
    assert db.item(iid)["owner_brand"] == pipeline.NO_BRAND
    checks = [m for m in lines if m.startswith("Check — ") and "isn't a brand name" in m]
    assert len(checks) == 1, lines


def test_set_brand_itself_never_takes_an_implausible_name(env, tmp_path, facts):  # noqa: F811
    s, db, _ = env
    iid = _sneakers(db, tmp_path, facts)
    pipeline.set_brand(s, db, iid, "???")                                  # `thrift edit --brand`, any path
    assert db.item(iid)["owner_brand"] == pipeline.NO_BRAND


def test_a_real_brand_reply_still_sets_the_brand(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _sneakers(db, tmp_path, facts)
    _reply(db, s, bot, "Mick Mack", to=_ask_brand(db, bot, iid))
    assert db.item(iid)["owner_brand"] == "Mick Mack"


# ---------------------------------------------------------------- §1.4 a category of another family: never refused

from thrift_agent import families  # noqa: E402
from thrift_agent.catalogs import categories  # noqa: E402


def test_the_dog_coat_is_a_pet_item_and_goes_up_under_accessories():
    """i_261109_de26f1 "quilted dog coat" (Women › Accessories): refused on Depop and Vinted at 8:43 as "a outerwear,
    not a accessory" — live nowhere."""
    for t in ("quilted dog coat", "Quilted Green Dog Coat with Diamond Stitching", "harness for dogs", "cat bed"):
        assert families.family_of_type(t) == "pet", t
    for t, fam in (("dog print dress", "dress"), ("cat lover tee", "top"), ("dog mom sweatshirt", "sweater")):
        assert families.family_of_type(t) == fam, t
    for site, cat in (("poshmark", ("Women", "Accessories", None)), ("depop", "Women > Accessories > Other"),
                      ("vinted", "Women > Accessories > Other accessories")):
        assert crosslist.family_problem("quilted dog coat", site, cat) is None


def test_a_site_category_of_another_family_becomes_the_closest_of_its_own():
    """WO51's live miss (a crochet mini dress under Sweaters › V-Necks) — now moved into Dresses, never refused."""
    p = categories.Pick(value="Women > Tops > Sweaters", path="Women > Tops > Sweaters", source="model")
    got = categories.in_family("depop", "Women", p, "crochet-knit mini dress", "Edikted Crochet Mini Dress")
    assert got.path.startswith("Women > Dresses") and got.source == "family" and got.was == "Women > Tops > Sweaters"
    leaves = categories.candidates("vinted", "Women")
    sweater = next(k for k, v in leaves.items() if v.endswith("V-neck sweaters"))
    got = categories.in_family("vinted", "Women", categories.Pick(value=sweater, path=leaves[sweater], source="model"),
                               "crochet-knit mini dress", "Edikted Crochet Mini Dress")
    assert got.path == "Women > Clothing > Dresses > Mini-dresses"
    keep = categories.Pick(value="Women > Dresses > Other", path="Women > Dresses > Other", source="table")
    assert categories.in_family("depop", "Women", keep, "maxi dress") is keep        # already its family


def test_poshmarks_wrong_family_moves_the_item_and_every_site_follows(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, said = env
    lines = []
    monkeypatch.setattr(runner.notify, "say", lines.append)
    iid = _item(db, tmp_path, facts, "Edikted Crochet Mini Dress Cream size S", status="ready", size="S", name="dress")
    it = db.item(iid)
    f = approve.loads(it["facts"])
    f["item_type"] = "crochet-knit mini dress"
    f["department"], f["category"], f["subcategory"] = "Women", "Sweaters", "V-Necks"
    renders = approve.loads(it["renders"])
    for r in renders.values():
        r.update(department="Women", category="Sweaters", subcategory="V-Necks")
        for k, v in (("brand", "Edikted"), ("colors", ["Cream"]), ("condition", "excellent"), ("photos", []),
                     ("sku", iid), ("price", 40), ("tags", [])):
            r.setdefault(k, v)
    db.set_item(iid, facts=f, renders=renders, owner_price=40)
    from thrift_agent.schema import Render
    render = Render.model_validate(renders["poshmark"])
    fixed = runner.refamily(db, db.item(iid), render, renders, "crochet-knit mini dress")
    assert fixed is not None and fixed.category == "Dresses" and fixed.subcategory == "Mini"
    after = approve.loads(db.item(iid)["renders"])
    assert {r["category"] for r in after.values()} == {"Dresses"}
    assert approve.loads(db.item(iid)["facts"])["category"] == "Dresses"
    assert [m for m in lines if m.startswith("Check — ") and "listed under Women › Dresses › Mini instead" in m]


def test_a_category_with_no_closer_one_is_posted_with_one_check_line(db, said):
    crosslist.family_check(db, "i1", "vinted", "Some Item", "category 'X' isn't the item's family (a pet, not a top)")
    crosslist.family_check(db, "i1", "vinted", "Some Item", "category 'X' isn't the item's family (a pet, not a top)")
    assert len([m for m in said if m.startswith("Check — Some Item: Vinted's category")]) == 1
    assert db.listing("i1", "vinted") is None                                        # nothing held, nothing skipped


# ---------------------------------------------------------------- WO55: the Birkenstock Milano's kids size

def test_the_milanos_kids_size_finds_its_tile():
    """Live 2026-11-09 2:28 PM: "11.5 (kids)" skipped on Poshmark's Kids › Shoes Girls tab, which offers "11.5"."""
    from thrift_agent.post.fallback import kids_bare, menu_size
    girls = ["7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11", "11.5", "12", "12.5", "13", "13.5", "1", "1.5", "2",
             "2.5", "3", "3.5", "4", "4.5", "5", "5.5", "6", "6.5", "7"]
    assert menu_size("11.5 (kids)", girls)[0] == "11.5"
    assert menu_size("11.5C", girls)[0] == "11.5"
    assert menu_size("4Y", girls)[0] == "4"
    assert kids_bare("4C") is None and kids_bare("5 (kids)") is None     # a toddler 4C is never a youth 4
    assert kids_bare("11.5") is None and kids_bare("13 (Toddler Girl)") is None
