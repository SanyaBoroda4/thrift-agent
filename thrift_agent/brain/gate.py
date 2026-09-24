"""The only reviewer when autopublish is on. Pure function — easy to test, easy to tighten."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from thrift_agent.schema import Facts, PriceResult


@dataclass
class GateResult:
    decision: Literal["publish", "draft", "needs_info"]
    reasons: list[str] = field(default_factory=list)


def evaluate(facts: Facts, price: PriceResult, lint: list[str], unsupported: int, cfg: dict,
             pricing_cfg: dict) -> GateResult:
    need, soft = [], []
    mc = cfg["min_confidence"]

    if not facts.brand.value or facts.brand.confidence < mc["brand"]:
        need.append(f"brand unclear ({facts.brand.value}, {facts.brand.confidence:.2f})")
    if not facts.size_us.value or facts.size_us.confidence < mc["size"]:
        need.append(f"size unclear ({facts.size_us.value}, {facts.size_us.confidence:.2f})")
    if facts.condition_evidence.confidence < mc["condition"]:
        need.append(f"condition unclear ({facts.condition}, {facts.condition_evidence.confidence:.2f})")
    if facts.condition == "NWT" and not (facts.condition_evidence.photos or facts.condition_evidence.source == "note"):
        need.append("NWT claimed without a hang-tag photo")
    need.extend(f"question: {q}" for q in facts.questions)

    if price.list_price is None:
        need.append("no price basis")
    elif price.source == "category_default" and not cfg.get("allow_category_default_price"):
        need.append(f"brand not in price table — category default ${price.list_price}")
    elif price.list_price < pricing_cfg["floor"]:
        need.append(f"price ${price.list_price} below floor")

    soft.extend(lint)
    if unsupported:
        soft.append(f"verifier removed {unsupported} unsupported claim(s) — review once")

    if need:
        return GateResult("needs_info", need + soft)
    if soft:
        return GateResult("draft", soft)
    return GateResult("publish", [])
