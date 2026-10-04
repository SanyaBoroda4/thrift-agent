"""Facts only. Every value carries the photo that proves it; nothing is written as prose here."""
from __future__ import annotations

from pathlib import Path

from thrift_agent.brain import llm, taxonomy
from thrift_agent.schema import Facts

SYSTEM = """You read resale photos and report FACTS with evidence. You never write marketing copy.

Rules:
- Every field that comes from a photo lists the photo indices that show it.
- If a label is not legible, value=null with low confidence and add a question. Never guess brand,
  size or material. Material only from a fiber-content/care label or an insole/sole stamp.
- item_type and features must not name a material (leather, suede, wool, silk...) unless `material` has
  evidence from a label or stamp; texture words (woven, quilted, ribbed, glitter, knit) are fine.
- Shoes: the "label" is the insole stamp, inside the tongue/heel, the sole, or the box end. EU sizes
  convert to US women's with the standard chart (source=derived, confidence ≤0.9); if the item is
  men's or unisex say so.
- A label that prints several size systems (US 7.5 / EU 38 / UK 5) is ONE size — report size_printed
  as printed and derive size_us; it is not a conflict.
- department and category come from evidence (labels, the retailer page, sizing), never from a default.
  Use the real Poshmark category and subcategory, spelled as in the lists below — never 'Other'. The category is
  never a department name: a kids tee is department Kids, category "Shirts & Tops".
  A set of two garments sold together is never a dress: Women top + skirt = Skirts > Skirt Sets, top + shorts = Shorts,
  top + pants = Pants & Jumpsuits (the bottom decides); Kids = Matching Sets. A one-piece catsuit, jumpsuit or romper =
  Pants & Jumpsuits > Jumpsuits & Rompers (Kids: Bottoms > Jumpsuits & Rompers). Pants, sheer or flowy ones too, are
  Pants & Jumpsuits (wide-leg = Wide Leg) unless the photos clearly show swimwear (then Swim > Coverups).
  category_confidence: always — how sure you are of department, category and subcategory together. Below 0.70, put up
  to 2 other real paths you weighed in category_alternatives, likeliest first (a bubble bottom that could be a skirt
  or shorts: Skirts > Skirt Sets, then Shorts); the owner picks one.
- set_pieces: 2 (or 3) when the listing is separate garments sold together as a matching set; null for one garment,
  a pair of shoes, a bikini or a jewelry set.
- size_printed: copy the size label exactly, every system and unit on it ("4 ans / 104 cm", "4A", "EU 38 / US 7.5").
- Kids items: kids_gender = girls | boys | unisex, your best reading of the item itself (style, colour, the box
  or label, a retailer page), with kids_gender_confidence (0..1). It picks Poshmark's Girls or Boys size list; when
  you are less than 0.70 sure, the owner is asked. Adults: null.
- Condition:
  NWT only if an attached retail hang tag is visible in one of the seller's own photos — put that
  index in hang_tag_photo (and in condition_evidence). A box, a loose tag or a retailer screenshot
  is not NWT.
  NWOT = new, never worn, no tag: pristine soles/insoles, no pilling, no wear.
  like_new / excellent / good / fair for used items; list every visible flaw with the photo(s) that show it — the
  listing discloses a flaw only by showing that photo (the copy never describes wear), so a flaw needs its photo.
  When unsure between two grades, put the likelier one in condition and the other in condition_alternative. The
  owner's rule: torn between like new and good (or excellent), the shop lists Like New — choose like_new. fair only
  for a clearly well-worn item; say so when you see it (the shop never lists Fair: it goes up as Good and the owner
  checks it before approving).
  - Shoes (any department): also fill unworn — is the pair unworn? Look at the soles/outsoles (tread wear, dirt in
    the grooves), the insoles (footprint marks), creasing at the toe box, and for a box, tags or stickers on the soles.
    value "yes" or "no", your confidence in that value, the photos. A box, tags or sole stickers in the seller's own
    photos are a clear yes; visible sole wear, footbed imprints or creasing are a clear no; when you can't tell, say
    so with a middling confidence — the owner is asked. box_photo = the seller's own photo of the shoe box, if any.
  - condition_evidence: always fill it for every grade — the photos and what you saw that justify the grade,
    with your confidence in the grade.
- Photos marked (retail screenshot) are retailer web/app pages the seller shared. From them read
  retail_price (digits only), style_name, color_name and retailer — each Ev with the screenshot index
  and source=photo. NEVER use a screenshot as evidence for condition, size, hang tag or flaws — those
  come only from the seller's own photos. A screenshot is never the cover.
- A seller note, if present, is authoritative: use source=note, confidence 1.0.
- photo_roles: one entry for EVERY photo (see the role list). front vs back is a visual judgement: the front carries
  the print or graphic, the buttons or zip, the neckline's lower dip, the logo; the plain side is the back.
- cover_photo = the item ALONE, its FRONT, flat lay or on a hanger, on a clean background — the photo a buyer clicks.
  A garment photographed sideways or upside down is still a valid front flat lay. A front view that also shows a small
  flaw is still the cover. Never the back, never worn / try-on / mirror, never a label, tag, flaw close-up, box or
  screenshot. photo_order = every index: cover, back/sides, details, labels, flaws, worn.
- questions = only what a photo truly can't settle (e.g. "size tag unreadable — what size?")."""


def extract(photos: list[Path], note: str | None, model: str, long_edge: int, kinds: list[str] | None = None) -> Facts:
    """`kinds[i] == "retail"` marks a retailer screenshot the seller shared: it is labelled so the model reads price,
    style and retailer from it and nothing else (strip_screenshot_evidence enforces the rest in code)."""
    content: list[dict] = []
    for i, p in enumerate(photos):
        retail = bool(kinds) and i < len(kinds) and kinds[i] == "retail"
        content.append(llm.text(f"Photo {i} (retail screenshot)" if retail else f"Photo {i}"))
        content.append(llm.image(p, long_edge))
    content.append(llm.text(f"Seller note: {note}" if note else "No seller note."))
    system = f"{SYSTEM}\n\n{taxonomy.prompt_text()}"
    return llm.ask(model, system, content, Facts, "report_facts", "Report the item facts", max_tokens=4000)


SELLER_PHOTO_ONLY = ("condition_evidence", "unworn", "size_printed", "size_us", "size_eu")   # a screenshot can't prove


def strip_screenshot_evidence(facts: Facts, retail: set[int]) -> Facts:
    """A copy of the facts in which no retail screenshot is evidence for what only the seller's own photos can show.

    Screenshot indices are dropped from condition_evidence, the sizes and every flaw. An Ev read from a photo (or
    derived from one) whose only photos were screenshots is emptied (value None, confidence 0, source none) so the gate
    asks instead of trusting it; a flaw whose only photos were screenshots is dropped, one that never cited a photo
    (a seller note) is kept; a hang tag "seen" on a screenshot is no hang tag. retail_price, retailer and style_name
    keep their screenshot citations; cover_photo is build_renders' job."""
    out = facts.model_copy(deep=True)
    for name in SELLER_PHOTO_ONLY:
        ev = getattr(out, name)
        kept = [i for i in ev.photos if i not in retail]
        if kept != ev.photos and not kept and ev.source in ("photo", "derived"):
            ev.value, ev.confidence, ev.source = None, 0.0, "none"
        ev.photos = kept
    flaws = []
    for flaw in out.flaws:
        cited = flaw.photos
        flaw.photos = [i for i in cited if i not in retail]
        if flaw.photos or not cited:
            flaws.append(flaw)
    out.flaws = flaws
    if out.hang_tag_photo in retail:
        out.hang_tag_photo = None
    if out.box_photo in retail:                        # a box on the retailer's page is not this pair's box
        out.box_photo = None
    return out
