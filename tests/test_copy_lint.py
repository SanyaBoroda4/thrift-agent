from thrift_agent.brain.copy import clamp_title, clean
from thrift_agent.brain.verify import lint
from thrift_agent.schema import CopyOut, Flaw


def co(**kw):
    base = dict(poshmark_title="Tory Burch Red Suede Bow Ballet Flats size 7.5",
                poshmark_description="Classic flats.\n\nCondition: excellent, light wear on soles.",
                poshmark_style_tags=["classic"], depop_description="cute flats", depop_hashtags=["torybur ch", "flats"])
    base.update(kw)
    return CopyOut(**base)


def test_title_cleanup():
    assert clamp_title("COPY - Nike Cortez size 7,5") == "Nike Cortez size 7.5"
    long = "Tory Burch " + "very " * 30 + "flats"
    assert len(clamp_title(long)) <= 80 and not clamp_title(long).endswith(" ")


def test_depop_hashtags_and_limit():
    out = clean(co(depop_description="x " * 800 + "#old #tags", depop_hashtags=["Tory Burch", "flats", "red", "y2k", "shoes"]))
    assert len(out.depop_description) <= 1000
    assert out.depop_description.endswith("#toryburch #flats #red #y2k #shoes")
    assert "#old" not in out.depop_description


def test_lint(facts):
    assert lint(facts(), co()) == []
    probs = lint(facts(flaws=[Flaw(description="scuff on left toe")]),
                 co(poshmark_title="Red Flats", poshmark_description="Smoke-free home. NWT."))
    joined = " ".join(probs)
    assert "brand missing" in joined and "US size missing" in joined and "NWT" in joined
    assert "smoke" in joined and "flaws" in joined


def test_lint_catches_past_mistakes(facts):
    # Typical AI-copy errors: women's sandals described as kids' sandals,
    # white sneakers described as light gray.
    kids = lint(facts(), co(poshmark_description="Stylish purple kids sandals. Condition: excellent, light wear."))
    assert any("kids" in p for p in kids)
    color = lint(facts(), co(poshmark_description="Light gray flats. Condition: excellent, light wear."))
    assert any("color" in p for p in color)
