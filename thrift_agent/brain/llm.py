"""Thin Anthropic wrapper: images in, one forced tool call out, validated by pydantic."""
from __future__ import annotations

import base64
import io
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


def ask(model: str, system: str, content: list[dict], out: type[T], tool: str, description: str,
        max_tokens: int = 4096, retries: int = 3) -> T:
    """One forced tool call, validated into `out`.

    Transient API failures (connection/timeout, 429/500/529) are retried with a short backoff. A response that
    fails pydantic validation (a colour outside the palette, a confidence of 1.2, a bad enum) is sent back to the
    model ONCE as an error tool_result so it can correct the call, instead of failing the whole item."""
    tools = [{"name": tool, "description": description, "input_schema": tool_schema(out)}]
    messages: list[dict] = [{"role": "user", "content": content}]
    api_attempts, repaired = 0, False
    while True:
        try:
            resp = client().messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=tools,
                tool_choice={"type": "tool", "name": tool}, messages=messages,
            )
        except (APIConnectionError, APIStatusError) as e:   # APITimeoutError is an APIConnectionError
            api_attempts += 1
            transient = isinstance(e, APIConnectionError) or e.status_code in RETRY_STATUS
            if transient and api_attempts < retries:
                time.sleep(5 * api_attempts)
                continue
            raise
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == tool), None)
        if block is None:
            raise RuntimeError(f"no {tool} call in response (stop_reason={resp.stop_reason})")
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
