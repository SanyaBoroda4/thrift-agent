"""WO51 §2/§3 (live 2026-11-01): failures that aren't about the item never use its attempts; a site that keeps failing
pauses with one line and comes back by itself after a test run; Poshmark's closed browser is reopened; a brand or
character mark that isn't the item's own never reaches a listing."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from thrift_agent import brands, crosslist, health, notify
from thrift_agent.db import DB
from thrift_agent.post import parallel

T0 = datetime(2026, 11, 1, 17, 47, tzinfo=timezone.utc)
POSH = ("TargetClosedError: BrowserContext.new_page: Target page, context or browser has been closed")
VINTED = "Vinted brand: Illegal invocation"
DEPOP = "Depop shipping: shipping: not on the page ([data-testid='usps__shipping__input'] | #usps__shipping)"


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    monkeypatch.setattr(notify, "ops_photo", lambda shot, text: out.append(text))
    monkeypatch.setattr(notify, "group", lambda text: out.append("GROUP " + text))
    return out


def _row(db, iid, mp, attempts=1):
    db.upsert_listing(iid, mp, status="queued", attempts=attempts)


# ---------------------------------------------------------------- the kinds

@pytest.mark.parametrize("error,hint,kind", [
    (POSH, None, health.INFRA),
    (VINTED, None, health.INFRA),
    ("Vinted brand: TypeError: x is not a function (at vinted.js:240)", "code", health.INFRA),
    (DEPOP, None, health.SITE),
    ("Depop shipping: the section doesn't name Depop Shipping", "site", health.SITE),
    ("Vinted refused the form: Fill in brand to continue", None, health.ITEM),
    ("skipped: no size on the item", None, health.ITEM),
    ("something nobody has seen", None, health.ITEM),
])
def test_every_failure_has_a_kind(error, hint, kind):
    assert health.kind_of(error, hint) == kind


def test_a_failure_after_the_click_is_always_the_items():
    assert health.kind_of(POSH, clicked=True) == health.ITEM


def test_the_same_failure_on_another_item_has_the_same_signature():
    a = "Depop shipping: not on the page ([data-testid='x'])"
    b = "Depop shipping: not on the page ([data-testid='y'])"
    assert health.signature(a) == health.signature(b)
    assert health.signature("Mismatch: package Medium; brand X") == health.signature("Mismatch: brand Y; package S")


# ---------------------------------------------------------------- the pause

def test_one_non_item_failure_requeues_untouched(db, said):
    _row(db, "i1", "depop", attempts=1)          # the attempt was counted when the job began
    assert health.non_item(db, "depop", "i1", DEPOP, health.SITE, at=T0) == "requeued"
    row = db.listing("i1", "depop")
    assert row["status"] == "queued" and row["attempts"] == 0
    assert not health.paused(db, "depop") and said == []
    assert "i1" in health.deferred(db, "depop", at=T0 + timedelta(minutes=1))


def test_the_same_failure_on_two_items_pauses_the_site_with_one_line(db, said):
    for iid in ("i1", "i2", "i3"):
        _row(db, iid, "depop")
    health.non_item(db, "depop", "i1", DEPOP, health.SITE, at=T0)
    assert health.non_item(db, "depop", "i2", DEPOP, health.SITE, at=T0) == "paused"
    assert health.paused(db, "depop")
    assert [db.listing(i, "depop")["attempts"] for i in ("i1", "i2")] == [0, 0]
    assert len(said) == 1 and said[0].startswith("⛔ Depop paused: its sell form changed (shipping section not found)")
    assert "items waiting — nothing was published." in said[0]
    # no third item is thrown at it
    assert crosslist.blocked(db, "depop")
    # the same problem again says nothing
    health.non_item(db, "depop", "i3", DEPOP, health.SITE, at=T0)
    assert len(said) == 1


def test_a_closed_browser_says_so_in_plain_words(db, said):
    health.pause(db, "poshmark", f"infra: {POSH}", health.INFRA, error=POSH, at=T0)
    assert said == ["⛔ Poshmark paused: its browser was closed. 0 items waiting — nothing was published."]


def test_one_item_alone_meeting_the_same_failure_is_the_items_after_all(db, said):
    _row(db, "i1", "vinted")
    out = [health.non_item(db, "vinted", "i1", DEPOP, health.SITE, at=T0) for _ in range(health.ITEM_AFTER)]
    # the second already pauses (same as the site's last); the third on the same item alone is the item's
    assert out[-1] == "item"


def test_item_failures_still_trip_the_brake(db, said):
    for _ in range(3):
        crosslist.failure(db, "vinted", True)
    assert health.paused(db, "vinted").startswith("failures: 3 listings in a row failed")


# ---------------------------------------------------------------- the test run after the cool-down

def test_a_paused_site_is_tested_after_the_cool_down_and_resumes_with_one_line(db, said):
    health.pause(db, "depop", "site: x", health.SITE, error=DEPOP, at=T0)
    assert health.test_due(db, "depop", at=T0 + timedelta(minutes=29)) is None
    assert health.test_due(db, "depop", at=T0 + timedelta(minutes=30)) == "auto"
    assert health.tested(db, "depop", True, at=T0 + timedelta(minutes=30)) == "resumed"
    assert not health.paused(db, "depop")
    assert said[-1] == "✅ Depop back to normal — posting resumed."
    assert len(said) == 2


def test_a_failed_test_backs_off_with_no_new_line(db, said):
    health.pause(db, "vinted", "site: x", health.SITE, error=DEPOP, at=T0)
    t = T0 + timedelta(minutes=30)
    waits = []
    for _ in range(5):
        assert health.tested(db, "vinted", False, "still broken", at=t) == "still"
        p = health.state(db, "vinted")
        waits.append(p["wait"])
        t = datetime.fromisoformat(p["due"])
    assert waits == [3600, 7200, 14400, 14400, 14400]
    assert len(said) == 1                       # the pause's own line only


def test_the_owners_pause_waits_for_resume(db, said):
    health.owner_pause(db, "depop")
    assert health.test_due(db, "depop", at=T0 + timedelta(days=2)) is None
    assert health.owner_resume(db, "depop")
    assert health.test_due(db, "depop") == "resume"
    health.tested(db, "depop", True)
    assert not health.paused(db, "depop") and said == ["✅ Depop back to normal — posting resumed."]


def test_a_dry_resume_tests_and_stays_paused(db, said):
    health.pause(db, "depop", "site: x", health.SITE, error=DEPOP, at=T0)
    health.owner_resume(db, "depop", dry=True)
    assert health.tested(db, "depop", True) == "dry"
    assert health.paused(db, "depop")


def test_no_message_says_the_mac_must_wake():
    import inspect
    from thrift_agent.post import runner
    for mod in (crosslist, runner, parallel, health):
        assert "next time the Mac wakes" not in inspect.getsource(mod)
        assert "next time the Mac is open" not in inspect.getsource(mod)


def test_an_item_failure_is_retried_after_30_minutes_without_a_window(db, said, monkeypatch):
    from thrift_agent.config import Settings
    s = Settings({"machine_role": "dev"})
    db.upsert_listing("i1", "depop", status="failed", attempts=1, error="Depop size: no option 'M'")
    row_at = datetime.fromisoformat(db.listing("i1", "depop")["updated_at"])
    row_at = row_at if row_at.tzinfo else row_at.replace(tzinfo=timezone.utc)
    assert crosslist.retry_failed(s, db, at=row_at + timedelta(minutes=10)) == []
    assert crosslist.retry_failed(s, db, at=row_at + timedelta(minutes=31)) == [("i1", "depop")]
    assert db.listing("i1", "depop")["status"] == "queued"


# ---------------------------------------------------------------- Poshmark's browser reopened

class FakeCtx:
    def __init__(self):
        self.handlers = {}
        self.closed = False

    def on(self, ev, fn):
        self.handlers[ev] = fn

    async def close(self):
        self.closed = True

    async def new_page(self):
        raise AssertionError("no login check on the first opening")


class FakePw:
    async def stop(self):
        pass


def _settings(tmp_path):
    from thrift_agent.config import Settings
    return Settings({"machine_role": "dev", "paths": {"db": str(tmp_path / "state.db"),
                                                       "chrome_profile": str(tmp_path / "profile")},
                     "schedule": {"timezone": "America/New_York"}})


def test_a_closed_poshmark_browser_is_reopened_and_its_login_checked(tmp_path, db, said):
    opened = []

    class Ctx(FakeCtx):
        async def new_page(self):
            class P:
                async def close(self):
                    pass
            return P()

    async def opener(profile, tz):
        opened.append(profile)
        return FakePw(), Ctx()

    class Poster:
        checked = 0

        async def check_account(self, page):
            Poster.checked += 1

    b = parallel.Browser(_settings(tmp_path), db, Poster(), opener)

    async def go():
        assert await b.ensure()
        first = b.ctx
        first.handlers["close"]()          # the owner quit it from the Dock
        assert not b.alive
        assert await b.ensure()
        return first

    first = asyncio.run(go())
    assert len(opened) == 2 and b.ctx is not first and Poster.checked == 1
    assert said == ["Poshmark's Chrome was closed on the Mac — reopened. Please don't quit the Chrome windows on the "
                    "Mac."]                                    # WO52 §4: one plain line


def test_a_browser_that_wont_reopen_backs_off_and_pauses_once(tmp_path, db, said, monkeypatch):
    calls = {"n": 0}

    async def opener(profile, tz):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakePw(), FakeCtx()
        raise RuntimeError("Chrome failed to start")

    clock = {"t": 1000.0}
    monkeypatch.setattr(parallel.time, "monotonic", lambda: clock["t"])
    b = parallel.Browser(_settings(tmp_path), db, object(), opener)

    async def go():
        assert await b.ensure()
        b.lost("closed")
        results = []
        for _ in range(4):
            results.append(await b.ensure())
            clock["t"] += 700
        return results

    assert asyncio.run(go()) == [False, False, False, False]
    assert health.paused(db, "poshmark")
    assert len([x for x in said if x.startswith("⛔ Poshmark paused")]) == 1


def test_browser_closed_is_recognised():
    assert parallel.browser_closed(POSH)
    assert not parallel.browser_closed("Depop shipping: not on the page")


# ---------------------------------------------------------------- the trademark guard (§3)

def test_disney_never_reaches_a_relibeauty_costume():
    own = brands.own_names("Relibeauty", "Relibeauty")
    text, gone = brands.guard("Relibeauty Disney Belle Princess Costume Dress with Tiara", own)
    assert gone == ["Disney"] and text == "Relibeauty Belle Princess Costume Dress with Tiara"
    text, gone = brands.guard("A Disney-style princess dress.\n#disney #costume", own)
    assert gone == ["Disney"] and "isney" not in text


def test_the_items_own_brand_is_kept():
    own = brands.own_names("Nike", "NIKE")
    assert brands.guard("Nike Air Running Shoes", own) == ("Nike Air Running Shoes", [])
    own = brands.own_names("Polo Ralph Lauren")
    assert brands.guard("Polo Ralph Lauren Oxford Shirt", own)[1] == []


def test_a_collab_the_label_shows_may_stand():
    own = brands.own_names("Parke")
    assert brands.guard("Parke x Target Gingham Pajama Set", own)[1] == []        # Target isn't guarded
    own = brands.own_names("Uniqlo")
    assert brands.guard("Uniqlo x Disney Tee", own, ok_text="Uniqlo x Disney")[1] == []
    assert brands.guard("Uniqlo x Disney Tee", own)[1] == ["Disney"]


def test_a_word_inside_another_word_is_no_mark():
    own = brands.own_names("Zara")
    assert brands.guard("Ugg-style boots", own)[1] == ["UGG"]
    assert brands.guard("Snuggle soft fleece", own)[1] == []


# ---------------------------------------------------------------- one site's restart never touches another's browser

def test_the_thrift_chrome_restart_is_its_own_launch_agent_only(monkeypatch):
    import subprocess
    import sys
    from thrift_agent.config import Settings
    from thrift_agent.post import runner
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(runner.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    runner.restart_thrift_chrome(Settings({"machine_role": "prod"}))
    assert calls == [["launchctl", "kickstart", "-k", "gui/501/com.thrift.chrome-cross"]]


def test_nothing_quits_the_google_chrome_app_or_kills_by_name():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for folder, pattern in (("thrift_agent", "*.py"), ("deploy", "*.sh"), ("ext", "*.js")):
        for f in (root / folder).rglob(pattern):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for bad in ("killall", "pkill", 'quit app "Google Chrome"', "open -a \"Google Chrome\""):
                assert bad not in text, f"{f}: {bad}"


def test_poshmarks_relaunch_closes_only_its_own_context(tmp_path, db, said):
    closed = []

    class Ctx(FakeCtx):
        async def close(self):
            closed.append(self)

    made = []

    async def opener(profile, tz):
        made.append(Ctx())
        return FakePw(), made[-1]

    b = parallel.Browser(_settings(tmp_path), db, object(), opener)

    async def go():
        await b.ensure()
        b.lost("x")
        await b.ensure()

    asyncio.run(go())
    assert closed == [made[0]]                  # its own dead context, nothing else


def test_off_white_is_a_colour_not_a_mark():
    """The WO51 audit: "Off-White" in a title is the colour ("Edikted Crochet Knit Mini Dress, Off-White size M")."""
    own = brands.own_names("Edikted")
    assert brands.guard("Edikted Crochet Knit Mini Dress V-Neck Flower Tie, Off-White size M", own)[1] == []


def test_lounge_pants_in_sleepwear_are_no_wrong_family():
    assert crosslist.family_problem("plaid lounge pants", "poshmark", ("Women", "Intimates & Sleepwear", "Pajamas")) is None
    assert crosslist.family_problem("crochet-knit mini dress", "vinted",
                                    "Women > Clothing > Jumpers & sweaters > Sweaters > V-neck sweaters")


def test_astr_the_label_is_its_own_spelling():
    """Live 2026-11-01: Depop skipped 'ASTR the Label Cowl Neck Satin Slip Dress …' as an all-caps word."""
    from thrift_agent import titles
    assert not titles.check("ASTR the Label Cowl Neck Satin Slip Dress, Champagne Pink, size S")


def test_the_brand_named_twice_in_a_title_is_said_once():
    """Live 2026-11-01: "Zara TRF Collection 100% Genuine Leather Zara Moto Jacket" was held on every site."""
    from thrift_agent import titles
    t = titles.fix("Zara TRF Collection 100% Genuine Leather Zara Moto Jacket Black size S", brand="Zara")
    assert t == "Zara TRF Collection 100% Genuine Leather Moto Jacket Black size S" and not titles.check(t)


def test_no_message_assumes_the_mac_sleeps():
    """WO52 §6: the Mac stays on — no "safe to close", "close the Mac", "next time the Mac is open / wakes"."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bad = re.compile(r"Safe to close|close the Mac|next time the Mac|Mac is (?:next )?open|when the Mac wakes", re.I)
    for f in [*(root / "thrift_agent").rglob("*.py"), *(root / "api" / "thrift_api").rglob("*.py")]:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            strings = re.findall(r"""f?["']([^"']*)["']""", code)
            assert not any(bad.search(s) for s in strings), f"{f.name}:{n}: {line.strip()}"
