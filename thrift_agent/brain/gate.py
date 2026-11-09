"""The only reviewer when autopublish is on. Pure function — easy to test, easy to tighten.

WO54 §2 (the owner, 2026-11-09: "I give you permission to take your own decisions in order to post it other than just
hold off till human will intrude"): the gate never asks and never holds. Only four things wait for a person — the
price (her Telegram card), a size no photo shows (the card's size buttons), a password or sign-in code, a real
CAPTCHA — and none of them is the gate's. Every other doubt was decided before this (the brand: pipeline.settle_brand;
the condition: settle_nwt / settle_condition; the category: settle_open_questions / settle_category) and is kept here
as a note, said once in the item's private "Check —" line when it goes live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from thrift_agent.schema import Facts, PriceResult


@dataclass
class GateResult:
    decision: Literal["publish", "draft", "needs_info"]   # WO54 §2: always "publish" now; the others are old records'
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # kept for the record, never asked (e.g. an unsure condition)
    questions: list[str] = field(default_factory=list)  # WO54 §2: always empty — kept for the old records' shape


# The card questions of the items processed before WO54 (their gate docs still hold them): recognised, never asked.
OTHER_CATEGORY_QUESTION = "Category? — reply e.g. 'category Tops'"


SIZE_NOTE = "size: the best reading,"   # WO33: an unsure size is a note (the card's Size line), never a question
NO_BRAND_NOTE = "no brand: no brand label, tag or logo in the photos — listed with no brand"
BRAND_SEEN = ("read", "unreadable", "logo", "none")


def brand_seen(facts: Facts) -> str:
    """What the photos show of the brand (Alex, 2026-10-27): the model's own word (facts.brand_seen), else — facts read
    before it — what the facts hold: a reading → 'read' (its confidence decides), a photo cited for the brand without
    one → 'unreadable', nothing at all → 'none'."""
    seen = getattr(facts, "brand_seen", None)
    if seen in BRAND_SEEN:
        return seen
    if facts.brand.value:
        return "read"
    return "unreadable" if facts.brand.photos else "none"


def evaluate(facts: Facts, price: PriceResult, lint: list[str], unsupported: int, cfg: dict,
             pricing_cfg: dict, sized: bool = True) -> GateResult:
    """WO54 §2: "publish", always — what the item's doubts were decided as is kept as notes (never a question):

    - the brand: read surely (≥ min_confidence) → used; no brand to be seen → NO_BRAND_NOTE; a brand there but not
      read surely was decided before the gate (pipeline.settle_brand: the reading if it is a big brand, else no brand —
      the card's ⚠️ line says which) and needs no note here;
    - the size: the best reading is listed (WO33), noted; no size at all is the card's size buttons (not the gate's);
    - the condition: an unsure grade is listed as read (settle_condition), noted; NWT without a hang-tag photo was
      listed as Like New by settle_nwt (the card says so) — the owner's word ("NWT", condition nwt) restores it;
    - the category: always a real Poshmark path by now (settle_open_questions, settle_category) — an "Other" that is
      left is Poshmark's own "Other", noted.

    The model's own `questions` about optional facts are dropped (a missing optional fact is left out). The copy's
    findings (lint, the verifier's edits) are kept as reasons for the record and never hold (WO33). `price` and
    `pricing_cfg` are accepted for the caller's sake and not consulted. `sized` False — a category whose every size menu
    is "One Size" (bags, jewelry, Home) — means there is no size to note."""
    soft, notes = [], []
    mc = cfg["min_confidence"]

    confident = bool(facts.brand.value) and facts.brand.confidence >= mc["brand"]
    if facts.brand.source != "owner" and not confident and not facts.brand.value and brand_seen(facts) == "none":
        notes.append(NO_BRAND_NOTE)            # nothing to be seen: listed with no brand (the "Check —" line says so)
    if sized and (not facts.size_us.value or facts.size_us.confidence < mc["size"]):
        # WO33, the owner's rule: never a question about size, never a hold — the best reading is listed, the card
        # shows it (approve.item_caption: "Size S — my best reading; reply 'size …' to change it")
        notes.append(f"{SIZE_NOTE} {facts.size_us.value} ({facts.size_us.confidence:.2f})" if facts.size_us.value
                     else f"{SIZE_NOTE} none read")
    if facts.condition_evidence.confidence < mc.get("condition", 0.70):
        weighed = (f", weighed against {facts.condition_alternative}"
                   if facts.condition_alternative and facts.condition_alternative != facts.condition else "")
        notes.append(f"model unsure of the condition ({facts.condition_evidence.confidence:.2f}{weighed}): listed as "
                     f"{facts.condition}")
    # Invariant 2 stays: NWT needs the seller's own photo of the attached hang tag, or the owner saying so — an NWT
    # without either was listed as Like New by pipeline.settle_nwt before the gate; one that reaches here is noted.
    owner_says = facts.condition_evidence.source in ("note", "owner")
    if facts.condition == "NWT" and facts.hang_tag_photo is None and not owner_says:
        soft.append("NWT claimed without a hang-tag photo")
    if facts.category.strip().lower() in ("", "other") or (facts.subcategory or "").strip().lower() == "other":
        notes.append(f"category: Poshmark's own “Other” ({facts.department}) — nothing closer fits the photos")

    soft.extend(lint)
    if unsupported:
        soft.append(f"verifier removed {unsupported} unsupported claim(s) — review once")
    # WO33, the owner's rule: the copy check never holds an item — what code can fix it fixed before this (the size in
    # the title, …); WO54 §2: neither does anything else — the rest is kept here for the record, and the item publishes
    return GateResult("publish", soft, notes)
