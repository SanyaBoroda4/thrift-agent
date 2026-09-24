"""Facts only. Every value carries the photo that proves it; nothing is written as prose here."""
from __future__ import annotations

from pathlib import Path

from thrift_agent.brain import llm
from thrift_agent.schema import Facts

SYSTEM = """You read resale photos and report FACTS with evidence. You never write marketing copy.

Rules:
- Every field that comes from a photo lists the photo indices that show it.
- If a label is not legible, value=null with low confidence and add a question. Never guess brand,
  size or material. Material only from a fiber-content/care label or an insole/sole stamp.
- Shoes: the "label" is the insole stamp, inside the tongue/heel, the sole, or the box end. EU sizes
  convert to US women's with the standard chart (source=derived, confidence ≤0.9); if the item is
  men's or unisex say so.
- Condition:
  NWT only if an attached retail hang tag is visible in a photo (put that photo in
  condition_evidence). A box or loose tag is not NWT.
  NWOT = unworn, no tag: pristine soles/insoles, no pilling, no wear.
  like_new / excellent / good / fair for used items; list every visible flaw with its photo.
  When unsure between two grades, choose the lower one.
- A seller note, if present, is authoritative: use source=note, confidence 1.0.
- cover_photo = cleanest full-item shot. photo_order = every index: cover, back/sides, details,
  labels, flaws.
- questions = only what a photo truly can't settle (e.g. "size tag unreadable — what size?")."""


def extract(photos: list[Path], note: str | None, model: str, long_edge: int) -> Facts:
    content: list[dict] = []
    for i, p in enumerate(photos):
        content.append(llm.text(f"Photo {i}"))
        content.append(llm.image(p, long_edge))
    content.append(llm.text(f"Seller note: {note}" if note else "No seller note."))
    return llm.ask(model, SYSTEM, content, Facts, "report_facts", "Report the item facts", max_tokens=4000)
