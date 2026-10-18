"""Thin Anthropic wrapper: images in, one forced tool call out, validated by pydantic."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import TypeVar

from anthropic import Anthropic, APIConnectionError, APIStatusError
from PIL import Image
from pydantic import BaseModel, ValidationError

from thrift_agent.schema import tool_schema

T = TypeVar("T", bound=BaseModel)
_client: Anthropic | None = None
RETRY_STATUS = (429, 500, 529)
MAX_TOKENS_CAP = 16000      # the one retry after a cut-off response doubles max_tokens up to this
# Models whose API answers a forced tool_choice ({"type": "tool"} / {"type": "any"}) with a 400 "tool_choice: type
# "tool" and "any" are not supported for this model" (docs, Define tools > Forcing tool use, Oct 2026). They get
# tool_choice auto, an explicit instruction to call the tool, and one reminder. A model not listed here that answers
# the same way is remembered for the rest of the process (_REJECTS_FORCED) and switched over at once.
NO_FORCED_TOOL_CHOICE = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")
_REJECTS_FORCED: set[str] = set()
CALL_THE_TOOL = "Answer only by calling the {tool} tool, once, with the complete result. Write nothing outside the call."
REMINDER = "You answered without calling {tool}. Call the {tool} tool now, once, with your complete answer."


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def image(path: Path, long_edge: int) -> dict:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((long_edge, long_edge))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    data = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def forces_tool_choice(model: str) -> bool:
    """Whether `model` takes the forced tool_choice {"type": "tool"} that ask() prefers."""
    return not model.startswith(NO_FORCED_TOOL_CHOICE) and model not in _REJECTS_FORCED


def _rejects_forced(e: Exception) -> bool:
    """The API's 400 for a model without forced tool use (invalid_request_error, 'tool_choice: type "tool" and "any"
    are not supported for this model.')."""
    message = str(getattr(e, "message", "") or e)
    return getattr(e, "status_code", None) == 400 and "tool_choice" in message and "not supported" in message


class Answers:
    """WO45 §10: recorded answers for the replay harness — every call keyed by its model, prompt, tool and content
    (an image by the hash of its bytes), the model's tool input kept as JSON. `record`: a miss asks the model and keeps
    the answer; otherwise a miss raises (CI never pays for a model call). One file per golden share."""

    def __init__(self, path: Path, record: bool = False):
        self.path, self.record = path, record
        self.data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.hits = self.misses = 0

    @staticmethod
    def key(model: str, system: str, content: list[dict], tool: str) -> str:
        parts = [{"text": c.get("text")} if c.get("type") == "text"
                 else {"image": hashlib.sha256(c["source"]["data"].encode()).hexdigest()} for c in content]
        raw = json.dumps({"model": model, "system": system, "tool": tool, "content": parts}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, value: dict, tool: str) -> None:
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


class MissingAnswer(RuntimeError):
    """A replay without a recorded answer for this call (the prompts changed, or the share was never recorded)."""


RECORDER: Answers | None = None     # set by replay.run for the length of a replay


def ask(model: str, system: str, content: list[dict], out: type[T], tool: str, description: str,
        max_tokens: int = 4096, retries: int = 3) -> T:
    """One tool call, validated into `out` (see `_ask`); WO45 §10: with a recorder, a recorded answer instead of the
    model, and a new answer recorded."""
    if RECORDER is None:
        return _ask(model, system, content, out, tool, description, max_tokens, retries)
    key = RECORDER.key(model, system, content, tool)
    hit = RECORDER.get(key)
    if hit is not None:
        RECORDER.hits += 1
        return out.model_validate(hit)
    if not RECORDER.record:
        raise MissingAnswer(f"no recorded answer for {tool} ({model}): record the share again")
    RECORDER.misses += 1
    answer = _ask(model, system, content, out, tool, description, max_tokens, retries)
    RECORDER.put(key, answer.model_dump(mode="json"), tool)
    return answer


def _ask(model: str, system: str, content: list[dict], out: type[T], tool: str, description: str,
         max_tokens: int = 4096, retries: int = 3) -> T:
    """One tool call, validated into `out`.

    The call is forced (tool_choice {"type": "tool"}) on models that take it. Claude Opus 5.5, Sonnet 5.5, Fable 5.1
    and Mythos 5.1 reject a forced tool_choice with a 400 — and any other model whose 400 says the same is switched
    over at once and remembered: they get tool_choice auto (one call at most), an explicit instruction to call the tool,
    and ONE reminder when they answer in text instead; a second miss raises.

    Transient API failures (connection/timeout, 429/500/529) are retried with a short backoff. A response cut off
    before the tool call (stop_reason == "max_tokens", or no call at all on a forced model) is retried ONCE with
    max_tokens doubled, capped at MAX_TOKENS_CAP. A response that fails pydantic validation (a colour outside the
    palette, a confidence of 1.2, a bad enum) is sent back to the model ONCE as an error tool_result so it can correct
    the call, instead of failing the whole item."""
    tools = [{"name": tool, "description": description, "input_schema": tool_schema(out)}]
    messages: list[dict] = [{"role": "user", "content": content}]
    forced = forces_tool_choice(model)
    api_attempts, repaired, grown, reminded = 0, False, False, False
    while True:
        choice = {"type": "tool", "name": tool} if forced else {"type": "auto", "disable_parallel_tool_use": True}
        prompt = system if forced else f"{system}\n\n{CALL_THE_TOOL.format(tool=tool)}"
        try:
            resp = client().messages.create(
                model=model, max_tokens=max_tokens, system=prompt, tools=tools, tool_choice=choice,
                messages=messages,
            )
        except (APIConnectionError, APIStatusError) as e:   # APITimeoutError is an APIConnectionError
            if forced and _rejects_forced(e):              # not for this model: the same request, tool_choice auto
                _REJECTS_FORCED.add(model)
                forced = False
                continue
            api_attempts += 1
            transient = isinstance(e, APIConnectionError) or e.status_code in RETRY_STATUS
            if transient and api_attempts < retries:
                time.sleep(5 * api_attempts)
                continue
            raise
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == tool), None)
        if block is None:
            if (forced or resp.stop_reason == "max_tokens") and not grown:   # cut off before the call: more room
                grown = True
                max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
                continue
            if not forced and resp.stop_reason != "max_tokens" and not reminded:   # it answered in text: remind it
                reminded = True
                last = messages[-1]
                messages = messages[:-1] + [{"role": "user", "content": [*last["content"],
                                                                           text(REMINDER.format(tool=tool))]}]
                continue
            raise RuntimeError(f"{model}: no {tool} call in response (stop_reason={resp.stop_reason}"
                               + (", even after a reminder)" if reminded else ")"))
        try:
            return out.model_validate(block.input)
        except ValidationError as e:
            if repaired:
                raise
            repaired = True
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"Invalid {tool} input. Fix these problems and call {tool} again with the "
                               f"complete, corrected input:\n{e}",
                }]},
            ]
