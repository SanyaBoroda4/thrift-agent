"""Thin Anthropic wrapper: images in, one forced tool call out, validated by pydantic."""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import TypeVar

from anthropic import Anthropic, APIStatusError
from PIL import Image
from pydantic import BaseModel

from thrift_agent.schema import tool_schema

T = TypeVar("T", bound=BaseModel)
_client: Anthropic | None = None


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
    for attempt in range(retries):
        try:
            resp = client().messages.create(
                model=model, max_tokens=max_tokens, system=system,
                tools=[{"name": tool, "description": description, "input_schema": tool_schema(out)}],
                tool_choice={"type": "tool", "name": tool},
                messages=[{"role": "user", "content": content}],
            )
            for block in resp.content:
                if block.type == "tool_use" and block.name == tool:
                    return out.model_validate(block.input)
            raise RuntimeError(f"no {tool} call in response (stop_reason={resp.stop_reason})")
        except APIStatusError as e:
            if e.status_code in (429, 500, 529) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")
