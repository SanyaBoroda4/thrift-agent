"""WO52 §2: forms that heal themselves — a field found again by its label or by the model when a site changes its form;
the model never offered, and never answered with, a control that publishes, saves or deletes; what worked learned."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from thrift_agent import bridge as bridge_mod
from thrift_agent import heal, notify
from thrift_agent.post import ext_driver

CANDS = [
    {"id": "e1", "tag": "input", "type": "text", "label": "Title", "testid": "listing-headline-field"},
    {"id": "e2", "tag": "textarea", "label": "Describe your item"},
    {"id": "e3", "tag": "button", "type": "button", "text": "Upload", "testid": "upload-form-save-button"},
    {"id": "e4", "tag": "button", "type": "button", "text": "Save draft"},
    {"id": "e5", "tag": "button", "text": "Delete listing"},
    {"id": "e6", "tag": "button", "type": "submit", "text": "Next"},
    {"id": "e7", "tag": "a", "text": "List This Item"},
    {"id": "e8", "tag": "button", "text": "Brand", "never": True},              # a recorded never_click
    {"id": "e9", "tag": "button", "role": "combobox", "text": "Select a brand", "label": "Brand"},
    {"id": "e10", "tag": "input", "type": "text", "label": "Post title"},    # a text box: it presses nothing
    {"id": "e11", "tag": "button", "text": "Post"},
]


def fake_ask(answer):
    """brain.llm.ask's stand-in: records the question, answers `answer` (validated by the enum the way the API's
    answer is, unless `raw`)."""
    seen = {}

    def ask(model, system, content, out, tool, description, max_tokens=4096, retries=3):
        seen.update(model=model, content=content, out=out, tool=tool)
        value = answer(content) if callable(answer) else answer
        try:
            return out.model_validate({"pick": value})
        except Exception:  # noqa: BLE001 — the enum refused it: what a model outside it would get is "none"
            return SimpleNamespace(pick=value)
    return ask, seen


def test_publish_save_delete_controls_are_never_offered():
    ids = [c["id"] for c in heal.allowed(CANDS)]
    assert ids == ["e1", "e2", "e9", "e10"]


def test_the_enum_holds_only_what_was_offered():
    ask, seen = fake_ask("e1")
    assert heal.pick(ask, "element", "vinted", "title", "", CANDS, "claude-sonnet-5") == "e1"
    choices = seen["out"].model_fields["pick"].annotation.__args__
    assert set(choices) == {"e1", "e2", "e9", "e10", "none"}
    text = seen["content"][-1]["text"]
    assert "Upload" not in text and "Save draft" not in text and "List This Item" not in text


@pytest.mark.parametrize("bad", ["e3", "e4", "e5", "e6", "e7", "e8", "e11", "e99", "none"])
def test_an_answer_naming_a_forbidden_control_or_none_is_refused(bad):
    ask, _ = fake_ask(bad)
    assert heal.pick(ask, "element", "vinted", "title", "", CANDS, "m") is None


def test_an_image_goes_with_the_question():
    ask, seen = fake_ask("e9")
    assert heal.pick(ask, "element", "vinted", "brand", "", CANDS, "m", image=b"\x89PNG") == "e9"
    assert seen["content"][0]["type"] == "image"


def test_learned_selectors_are_kept_first_and_said_once(tmp_path):
    store = heal.Learned(tmp_path / "learned.json")
    assert store.learn("vinted", "title", 'input[data-testid="a"]', "label") is True
    assert store.learn("vinted", "title", 'input[data-testid="b"]', "ai") is False      # said once only
    assert store.for_site("vinted") == {"title": ['input[data-testid="b"]', 'input[data-testid="a"]']}
    assert store.learn("vinted", "brand", None, "ai") is False                          # nothing stable: nothing kept
    assert heal.moved_line("depop", "group-input") == "🩹 Depop's category field moved — learned the new one."


def test_every_job_carries_the_selectors_learned_for_its_site():
    async def go():
        b = bridge_mod.Bridge("t" * 32, port=0)
        b.learned_for = lambda site: {"title": ['input[data-testid="x"]']} if site == "vinted" else {}
        return b.submit("vinted", "dry_run", {"fields": {}}).message(), b.submit("depop", "dry_run", {}).message()
    vinted, depop = asyncio.run(go())
    assert vinted["learned"] == {"title": ['input[data-testid="x"]']} and depop["learned"] == {}


def test_the_bridge_answer_carries_the_pick():
    async def go():
        b = bridge_mod.Bridge("t" * 32, port=0)
        b.askers["depop"] = lambda job, ev: "e9"
        job = b.submit("depop", "dry_run", {"fields": {}})
        b._event({"job_id": job.id, "event": "ask", "ask_id": "a7", "kind": "element", "field": "brand-input",
                  "candidates": CANDS})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if job.commands:
                break
        return job
    job = asyncio.run(go())
    assert job.commands == [{"type": "answer", "job_id": job.id, "ask_id": "a7", "pick": "e9"}]


def test_a_failed_question_is_none():
    async def go():
        b = bridge_mod.Bridge("t" * 32, port=0)

        def boom(job, ev):
            raise RuntimeError("the API is down")
        b.askers["vinted"] = boom
        job = b.submit("vinted", "dry_run", {"fields": {}})
        b._event({"job_id": job.id, "event": "ask", "ask_id": "a2", "field": "title", "candidates": CANDS})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if job.commands:
                break
        return job.commands
    assert asyncio.run(go())[0]["pick"] == "none"


def test_the_driver_asks_the_model_and_keeps_what_was_learned(tmp_path, monkeypatch):
    said = []
    monkeypatch.setattr(notify, "say", said.append)
    p = ext_driver.ExtensionPoster("vinted")
    p.learned = heal.Learned(tmp_path / "learned.json")
    p.ask, seen = fake_ask("e9")
    p.heal_model = "claude-sonnet-5"
    job = SimpleNamespace(shots=[])
    assert p.answer_ask(job, {"kind": "element", "field": "brand", "candidates": CANDS}) == "e9"
    assert seen["model"] == "claude-sonnet-5"
    p.on_learned({"field": "brand", "selector": 'button[data-testid="brand-x"]', "how": "ai"})
    p.on_learned({"field": "brand", "selector": 'button[data-testid="brand-x"]', "how": "ai"})
    assert said == ["🩹 Vinted's brand field moved — learned the new one."]
    assert p.learned.for_site("vinted") == {"brand": ['button[data-testid="brand-x"]']}


def test_the_driver_wires_its_bridge(tmp_path):
    b = bridge_mod.Bridge("t" * 32, port=0)
    p = ext_driver.ExtensionPoster("depop", bridge=b)
    p.learned = heal.Learned(tmp_path / "learned.json")
    p.learned.learn("depop", "condition-input", "input#cond-picker", "label")
    p._bridge()
    assert b.askers["depop"] == p.answer_ask

    async def go():
        return b.submit("depop", "dry_run", {}).payload["learned"]
    assert asyncio.run(go()) == {"condition-input": ["input#cond-picker"]}


def test_a_reworded_option_reads_back_as_ours():
    seen = {"condition": ["Brand new and no tags"], "condition-input": ["Brand new and no tags"], "size": ["S"],
            "reworded": {"condition": [{"plan": "New without tags", "shown": "Brand new and no tags"}]}}
    out = ext_driver.reworded(seen)
    assert out["condition"] == ["New without tags"]
    assert out["condition-input"] == ["Brand new and no tags"]          # only the field the page named
    other = {"condition": ["Good"], "reworded": {"condition": [{"plan": "New without tags", "shown": "Brand new"}]}}
    assert ext_driver.reworded(other)["condition"] == ["Good"]


# ---------------------------------------------------------------- Poshmark (Playwright), when a Chrome is here

pw_api = pytest.importorskip("playwright.async_api")


@pytest.fixture(scope="module")
def chrome():
    loop = asyncio.new_event_loop()

    async def start():
        pw = await pw_api.async_playwright().start()
        for kw in ({"channel": "chrome"}, {}):
            try:
                return pw, await pw.chromium.launch(headless=True, **kw)
            except Exception:  # noqa: BLE001
                continue
        await pw.stop()
        raise RuntimeError("no Chrome")
    try:
        pw, browser = loop.run_until_complete(start())
    except Exception as e:  # noqa: BLE001
        loop.close()
        pytest.skip(f"no headless Chrome for Playwright here ({e})")
    yield loop, browser
    loop.run_until_complete(browser.close())
    loop.run_until_complete(pw.stop())
    loop.close()


FORM = """<html><body><form>
<h2>Listing details</h2>
{label}<input id="headline" data-vv-name="headline" type="text">
<textarea data-vv-name="description"></textarea>
<button type="button" data-et-name="save_draft">Save Draft</button>
<button type="button" data-et-name="next">Next</button>
<a data-et-name="discard" href="#">Cancel</a>
</form></body></html>"""


def _posh(tmp_path, monkeypatch, answer):
    from thrift_agent.post import poshmark
    monkeypatch.setattr(poshmark, "FIND_MS", 300)
    p = poshmark.PoshmarkPoster("seller")
    p.learned = heal.Learned(tmp_path / "learned.json")
    p.ask, seen = fake_ask(answer)
    p.heal_model = "m"
    return p, seen


def test_poshmark_title_renamed_is_found_by_its_label(chrome, tmp_path, monkeypatch):
    loop, browser = chrome
    said = []
    monkeypatch.setattr(notify, "say", said.append)
    p, seen = _posh(tmp_path, monkeypatch, "none")

    async def go():
        page = await browser.new_page()
        await page.set_content(FORM.format(label='<label for="headline">Title</label>'))
        loc = await p._find(page, "title")
        await loc.fill("J. Crew Wide Leg Pants")
        value = await p._loc(page, "title").first.input_value()
        await page.close()
        return value
    assert loop.run_until_complete(go()) == "J. Crew Wide Leg Pants"
    assert not seen                                       # the label was enough: no model
    assert p.learned.for_site("poshmark") == {"title": ['input[data-vv-name="headline"]']}
    assert said == ["🩹 Poshmark's title field moved — learned the new one."]


def test_poshmark_field_nothing_labels_is_found_by_the_model_never_a_save_or_next(chrome, tmp_path, monkeypatch):
    loop, browser = chrome
    monkeypatch.setattr(notify, "say", lambda *_: None)

    def pick(content):
        text = content[-1]["text"]
        assert "Save Draft" not in text and "Next" not in text and "Cancel" not in text
        line = next(x for x in text.splitlines() if "headline" in x)
        return line.split()[1].removeprefix("id=")
    p, seen = _posh(tmp_path, monkeypatch, pick)

    async def go():
        page = await browser.new_page()
        await page.set_content(FORM.format(label=""))
        loc = await p._find(page, "title")
        await loc.fill("x")
        out = await page.locator("#headline").input_value()
        await page.close()
        return out
    assert loop.run_until_complete(go()) == "x"
    assert seen["tool"] == "pick_control"


def test_poshmark_a_model_that_names_save_draft_is_refused(chrome, tmp_path, monkeypatch):
    loop, browser = chrome
    from thrift_agent.post.base import PosterError

    def pick(content):                         # the Save Draft button isn't in the question: any id it names isn't ours
        return "e999"
    p, _ = _posh(tmp_path, monkeypatch, pick)

    async def go():
        page = await browser.new_page()
        await page.set_content(FORM.format(label=""))
        try:
            with pytest.raises(PosterError, match="title: not on the page"):
                await p._find(page, "title")
        finally:
            await page.close()
    loop.run_until_complete(go())
