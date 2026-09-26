"""Schema contracts: tool_schema keeps field descriptions, Ev.value coercion, empty copy is rejected, and a retail
screenshot is never evidence for what only the seller's own photos can show."""
import pytest
from pydantic import ValidationError

from thrift_agent.brain import extract as ex
from thrift_agent.schema import CopyOut, Ev, Facts, Flaw, VerifyOut, tool_schema


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_tool_schema_field_description_wins_over_ref_docstring():
    props = tool_schema(Facts)["properties"]
    assert props["size_us"]["description"].startswith("US size")
    assert props["condition_evidence"]["description"].startswith("Photo(s) and the observation")
    assert props["brand"]["description"] == "A fact plus the proof for it."       # no field text -> Ev's own
    assert props["size_us"]["type"] == "object" and "value" in props["size_us"]["properties"]   # Ev inlined


def test_tool_schema_leaves_no_refs():
    for model in (Facts, CopyOut, VerifyOut):
        assert not set(_keys(tool_schema(model))) & {"$ref", "$defs"}


def test_ev_value_accepts_numbers_and_drops_blanks():
    assert Ev(value=7.5).value == "7.5"
    assert Ev(value=8).value == "8"
    assert Ev(value=8.0).value == "8"
    assert Ev(value=" 7.5M ").value == "7.5M"
    assert Ev(value="   ").value is None
    assert Ev(value="").value is None
    assert Ev(value=None).value is None


def test_facts_take_a_numeric_size_from_tool_input(facts):
    data = facts().model_dump()
    data["size_us"]["value"] = 7.5                                   # what a model emitting a JSON number looks like
    assert Facts.model_validate(data).size_us.value == "7.5"


@pytest.mark.parametrize("field", ["poshmark_title", "poshmark_description", "depop_description"])
def test_empty_copy_text_fails_validation(field):
    copy = dict(poshmark_title="t", poshmark_description="d", poshmark_style_tags=[], depop_description="d",
                depop_hashtags=[])
    audit = dict(poshmark_title="t", poshmark_description="d", depop_description="d")
    CopyOut(**copy)
    VerifyOut(**audit)
    with pytest.raises(ValidationError):
        CopyOut(**{**copy, field: ""})
    with pytest.raises(ValidationError):
        VerifyOut(**{**audit, field: ""})


def test_strip_screenshot_evidence_keeps_only_the_sellers_photos(facts):
    f = facts(size_us=Ev(value="7.5", photos=[5], source="photo", confidence=0.9),
              size_printed=Ev(value="7.5M", photos=[3, 5], source="photo", confidence=0.9),
              size_eu=Ev(value="38", photos=[5], source="derived", confidence=0.8),
              condition="NWT", hang_tag_photo=5,
              condition_evidence=Ev(value="hang tag on the page", photos=[5], source="photo", confidence=0.9),
              flaws=[Flaw(description="scuff", photos=[5]), Flaw(description="stain", photos=[4, 5]),
                     Flaw(description="small hole (seller note)")],
              retail_price=Ev(value="128", photos=[5], source="photo", confidence=0.9))
    out = ex.strip_screenshot_evidence(f, {5})
    assert (out.size_us.value, out.size_us.photos, out.size_us.source, out.size_us.confidence) == (None, [], "none", 0.0)
    assert (out.size_printed.value, out.size_printed.photos) == ("7.5M", [3])
    assert out.size_eu.value is None                                       # derived from the screenshot's size
    assert (out.condition_evidence.value, out.condition_evidence.source) == (None, "none")
    assert [(x.description, x.photos) for x in out.flaws] == [("stain", [4]), ("small hole (seller note)", [])]
    assert out.hang_tag_photo is None
    assert (out.retail_price.value, out.retail_price.photos) == ("128", [5])     # retail facts keep the screenshot
    assert f.size_us.value == "7.5" and f.hang_tag_photo == 5 and len(f.flaws) == 3   # the input is not mutated
    assert ex.strip_screenshot_evidence(facts(), set()) == facts()


def test_extract_labels_retail_screenshots(facts, monkeypatch, tmp_path):
    seen = {}

    def fake_ask(model, system, content, out, tool, description, **kw):
        seen["labels"] = [c["text"] for c in content if c["type"] == "text"]
        return facts()
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask)
    monkeypatch.setattr("thrift_agent.brain.llm.image", lambda p, long_edge: {"type": "image"})
    photos = [tmp_path / f"{i}.jpg" for i in range(3)]
    ex.extract(photos, None, "m", 1024, kinds=["photo", "retail", "photo"])
    assert seen["labels"] == ["Photo 0", "Photo 1 (retail screenshot)", "Photo 2", "No seller note."]
    ex.extract(photos, "NWT", "m", 1024)
    assert seen["labels"] == ["Photo 0", "Photo 1", "Photo 2", "Seller note: NWT"]
