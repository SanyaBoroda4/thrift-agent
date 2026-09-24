"""Schema contracts: tool_schema keeps field descriptions, Ev.value coercion, empty copy is rejected."""
import pytest
from pydantic import ValidationError

from thrift_agent.schema import CopyOut, Ev, Facts, VerifyOut, tool_schema


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
