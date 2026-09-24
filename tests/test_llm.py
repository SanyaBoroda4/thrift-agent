"""llm.ask against a fake client: validation repair, transient retries, hard failures."""
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError
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
