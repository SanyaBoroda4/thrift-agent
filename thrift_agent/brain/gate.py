"""The only reviewer when autopublish is on. Pure function — easy to test, easy to tighten.

The gate asks only what the owner alone can settle; the price is always approved by the owner in the Telegram message.
"""
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
    """needs_info when a fact only the owner can settle is missing or unsure (brand, size, condition, NWT proof, the
    category, an open question); draft when the copy needs one look (lint, verifier edits); publish otherwise.

    `price` and `pricing_cfg` are accepted for the caller's sake and not consulted: a brand missing from the price
    table, a category default or a price under the floor is not a question for the owner — the approval message shows
    the price (and "no price history") and the owner settles it there."""
    need, soft = [], []
    mc = cfg["min_confidence"]

    if not facts.brand.value or facts.brand.confidence < mc["brand"]:
        need.append(f"brand unclear ({facts.brand.value}, {facts.brand.confidence:.2f})")
    if not facts.size_us.value or facts.size_us.confidence < mc["size"]:
        need.append(f"size unclear ({facts.size_us.value}, {facts.size_us.confidence:.2f})")
    if facts.condition_evidence.confidence < mc["condition"]:
        need.append(f"condition unclear ({facts.condition}, {facts.condition_evidence.confidence:.2f})")
    # Invariant 2: NWT needs the seller's own photo of the attached hang tag, or a seller note saying so.
    if facts.condition == "NWT" and facts.hang_tag_photo is None and facts.condition_evidence.source != "note":
        need.append("NWT claimed without a hang-tag photo")
    if facts.category.strip().lower() in ("", "other") or (facts.subcategory or "").strip().lower() == "other":
        need.append("category/subcategory is 'Other' — pick the real Poshmark category")
    need.extend(f"question: {q}" for q in facts.questions)

    soft.extend(lint)
    if unsupported:
        soft.append(f"verifier removed {unsupported} unsupported claim(s) — review once")

    if need:
        return GateResult("needs_info", need + soft)
    if soft:
        return GateResult("draft", soft)
    return GateResult("publish", [])
