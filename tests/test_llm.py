"""llm.ask against a fake client: validation repair, transient retries, hard failures."""
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError, BadRequestError
from pydantic import BaseModel, Field, ValidationError

from thrift_agent.brain import llm


class Out(BaseModel):
    tags: list[str]
    n: int = Field(ge=0, le=1)


def block(inp):
    return SimpleNamespace(type="tool_use", name="t", id="toolu_1", input=inp)


def resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use")


def fake_client(responses):
    calls = []

    def create(**kw):
        calls.append(kw)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def status(code):
    return APIStatusError(f"http {code}", response=httpx.Response(code, request=REQ), body=None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_validation_error_is_sent_back_once(monkeypatch):
    c, calls = fake_client([resp(block({"tags": ["a"], "n": 5})), resp(block({"tags": ["a"], "n": 1}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    out = llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc")
    assert out.n == 1 and len(calls) == 2
    msgs = calls[1]["messages"]                      # second call carries the bad turn + the error
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    tr = msgs[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["is_error"] and tr["tool_use_id"] == "toolu_1" and "n" in tr["content"]


def test_second_validation_error_raises(monkeypatch):
    c, calls = fake_client([resp(block({"tags": [], "n": 5})), resp(block({"tags": [], "n": 7}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    with pytest.raises(ValidationError):
        llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc")
    assert len(calls) == 2


def test_transient_errors_are_retried(monkeypatch):
    c, calls = fake_client([APIConnectionError(request=REQ), status(529), resp(block({"tags": [], "n": 0}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    assert llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc").n == 0
    assert len(calls) == 3


def test_permanent_error_is_not_retried(monkeypatch):
    c, calls = fake_client([status(400), resp(block({"tags": [], "n": 0}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    with pytest.raises(APIStatusError):
        llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc")
    assert len(calls) == 1


def test_retry_budget_is_finite(monkeypatch):
    c, calls = fake_client([status(529)] * 5)
    monkeypatch.setattr(llm, "client", lambda: c)
    with pytest.raises(APIStatusError):
        llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc", retries=3)
    assert len(calls) == 3


def cut_off():
    # The model ran out of output tokens before it got to the tool block: text only, no tool_use.
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="Let me look...")], stop_reason="max_tokens")


def test_missing_tool_block_is_retried_once_with_doubled_max_tokens(monkeypatch):
    c, calls = fake_client([cut_off(), resp(block({"tags": ["a"], "n": 0}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    assert llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc", max_tokens=4096).n == 0
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 4096 and calls[1]["max_tokens"] == 8192
    assert calls[1]["messages"] == calls[0]["messages"]      # same request, just more room


def test_missing_tool_block_twice_raises(monkeypatch):
    c, calls = fake_client([cut_off(), cut_off(), resp(block({"tags": [], "n": 0}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    with pytest.raises(RuntimeError, match="stop_reason=max_tokens"):
        llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc")
    assert len(calls) == 2                                    # one retry, not an endless loop


def test_max_tokens_retry_is_capped(monkeypatch):
    c, calls = fake_client([cut_off(), resp(block({"tags": [], "n": 0}))])
    monkeypatch.setattr(llm, "client", lambda: c)
    llm.ask("m", "sys", [llm.text("hi")], Out, "t", "desc", max_tokens=12000)
    assert calls[1]["max_tokens"] == llm.MAX_TOKENS_CAP == 16000



# ---------------------------------------------------------------- WO19: models that reject a forced tool_choice

FORCED_400 = 'tool_choice: type "tool" and "any" are not supported for this model.'     # as the Mac got it


# The models the docs list as rejecting a forced tool_choice (Define tools > Forcing tool use, Oct 2026) — kept here on
# purpose, independent of llm.NO_FORCED_TOOL_CHOICE: the fake API must not follow the code it tests.
API_REJECTS_FORCED = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")


def like_the_api(*replies, rejects=API_REJECTS_FORCED):
    """A fake client that answers like the Messages API: a model in `rejects` gets the 400 invalid_request_error for a
    forced tool_choice (type "tool" or "any"), exactly as claude-opus-5-5 answered on the Mac (WO19); anything else
    gets the next reply."""
    calls, replies = [], list(replies)

    def create(**kw):
        calls.append(kw)
        if kw["model"].startswith(rejects) and kw["tool_choice"]["type"] in ("tool", "any"):
            body = {"type": "error", "error": {"type": "invalid_request_error", "message": FORCED_400}}
            raise BadRequestError(FORCED_400, response=httpx.Response(400, request=REQ, json=body), body=body)
        return replies.pop(0)
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


def said(text_):
    """A reply in plain text: the model answered without calling the tool."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text_)], stop_reason="end_turn")


@pytest.fixture(autouse=True)
def forget_rejections():
    getattr(llm, "_REJECTS_FORCED", set()).clear()
    yield
    getattr(llm, "_REJECTS_FORCED", set()).clear()


def test_opus_5_5_is_never_sent_a_forced_tool_choice(monkeypatch):
    c, calls = like_the_api(resp(block({"tags": ["a"], "n": 1})))
    monkeypatch.setattr(llm, "client", lambda: c)
    assert llm.ask("claude-opus-5-5", "sys", [llm.text("hi")], Out, "t", "desc").n == 1
    assert len(calls) == 1 and calls[0]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert calls[0]["system"].startswith("sys") and "calling the t tool" in calls[0]["system"]


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-haiku-4-5-20251001"])
def test_models_that_take_a_forced_call_still_get_one(monkeypatch, model):
    c, calls = like_the_api(resp(block({"tags": [], "n": 0})))
    monkeypatch.setattr(llm, "client", lambda: c)
    llm.ask(model, "sys", [llm.text("hi")], Out, "t", "desc")
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "t"} and calls[0]["system"] == "sys"


def test_a_model_the_api_says_no_to_is_switched_over_and_remembered(monkeypatch):
    c, calls = like_the_api(resp(block({"tags": [], "n": 0})), resp(block({"tags": [], "n": 1})),
                            rejects=("claude-future-6",))
    monkeypatch.setattr(llm, "client", lambda: c)
    assert llm.ask("claude-future-6", "sys", [llm.text("hi")], Out, "t", "desc").n == 0
    assert [k["tool_choice"]["type"] for k in calls] == ["tool", "auto"]          # the 400, then the same, auto
    assert llm.ask("claude-future-6", "sys", [llm.text("hi")], Out, "t", "desc").n == 1
    assert [k["tool_choice"]["type"] for k in calls] == ["tool", "auto", "auto"]  # no second 400


def test_a_text_answer_gets_one_reminder_then_a_clear_error(monkeypatch):
    c, calls = like_the_api(said("These look like two items."), resp(block({"tags": ["x"], "n": 0})))
    monkeypatch.setattr(llm, "client", lambda: c)
    assert llm.ask("claude-opus-5-5", "sys", [llm.text("hi")], Out, "t", "desc").tags == ["x"]
    reminder = calls[1]["messages"][-1]["content"][-1]["text"]
    assert reminder == llm.REMINDER.format(tool="t") and calls[1]["messages"][-1]["content"][0]["text"] == "hi"

    c, calls = like_the_api(said("Two items."), said("Still two items."), resp(block({"tags": [], "n": 0})))
    monkeypatch.setattr(llm, "client", lambda: c)
    with pytest.raises(RuntimeError, match="claude-opus-5-5: no t call in response .*even after a reminder"):
        llm.ask("claude-opus-5-5", "sys", [llm.text("hi")], Out, "t", "desc")
    assert len(calls) == 2


def test_a_cut_off_answer_on_opus_5_5_gets_more_room_not_a_reminder(monkeypatch):
    c, calls = like_the_api(cut_off(), resp(block({"tags": [], "n": 0})))
    monkeypatch.setattr(llm, "client", lambda: c)
    llm.ask("claude-opus-5-5", "sys", [llm.text("hi")], Out, "t", "desc", max_tokens=3000)
    assert [k["max_tokens"] for k in calls] == [3000, 6000] and calls[1]["messages"] == calls[0]["messages"]


def test_every_configured_model_gets_through_the_apis_tool_choice_rule(monkeypatch):
    """The models in config/settings.yaml, against an API that rejects a forced tool_choice where the docs say it
    does: the Mac's failure (models.segment = claude-opus-5-5) can't come back through a model change."""
    from thrift_agent.config import settings
    for role, model in settings()["models"].items():
        c, calls = like_the_api(resp(block({"tags": [role], "n": 0})))
        monkeypatch.setattr(llm, "client", lambda c=c: c)
        assert llm.ask(model, "sys", [llm.text("hi")], Out, "t", "desc").tags == [role], (role, model)
        assert len(calls) == 1, (role, model)                 # known models: never a wasted 400


def test_segmentation_on_claude_opus_5_5_parses_like_any_other(monkeypatch, tmp_path):
    """The call that failed on the Mac (b_261003_8dea57): seg.segment on claude-opus-5-5."""
    from datetime import datetime
    from PIL import Image
    from thrift_agent.ingest import segment
    photos = []
    for i, colour in enumerate(["red", "blue"]):
        photos.append((tmp_path / f"{i}.jpg", datetime(2026, 10, 3, 14, 0, 10 * i)))
        Image.new("RGB", (60, 80), colour).save(photos[-1][0])
    groups = {"groups": [{"photos": [0], "summary": "red top", "full_item_photos": [0], "confidence": 0.95},
                         {"photos": [1], "summary": "blue top", "full_item_photos": [1], "confidence": 0.95}]}
    c, calls = like_the_api(SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="report_groups",
                                                                     id="toolu_9", input=groups)],
                                            stop_reason="tool_use"))
    monkeypatch.setattr(llm, "client", lambda: c)
    out = segment.segment(photos, "claude-opus-5-5", 64)
    assert [g.photos for g in out.groups] == [[0], [1]] and calls[0]["tool_choice"]["type"] == "auto"

