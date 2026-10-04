"""Which photo shows the FRONT of the item, and how to turn it upright (WO23).

Front and back are a comparison, not a guess from one photo: the model sees the item's own photos of the item alone side
by side, numbered, at a readable size, and answers one question — which shows the front? — with, for each photo, the
side it shows, how much design detail it has, and the turn that would put the item upright. pipeline.choose_cover
checks the answer in code. Also here: a focused re-read of a size label, for a kids label whose cm or years were left
out of the first reading."""
from __future__ import annotations

from pathlib import Path

from thrift_agent.brain import llm
from thrift_agent.schema import FrontOut, SizeLabel

SYSTEM = """You compare photos of ONE item (a garment, a pair of shoes, a bag, ...) to find the photo that shows its \
FRONT: the listing's cover.

- The photos are the same item from different sides. For each one say which side it shows: front, back, side or \
unclear.
- The FRONT is the side with a print, graphic, text, logo, buttons, a zip, pockets or the lower neckline. A plain side \
is the BACK when another photo of the same item shows a print or other design on the other side. Pants, jeans and \
skirts: back pockets and the yoke are the back. A zip: a fly zip with a button (pants, shorts, jeans) is the front, but \
a zip running down the middle of a skirt or a dress is almost always at the back. Shoes: the outer side profile or the \
pair seen from the front are both a front.
- When two sides look alike, the one with more visible design detail is the front.
- A garment photographed sideways or upside down — laid on the floor in any direction — is still a valid front flat \
lay: judge the side that is shown, not the angle.
- design: how much printed design the photo shows — a print, graphic, text or logo (none / some / strong); seams, zips and
  pockets don't count here.
- upright: the clockwise turn (0, 90, 180 or 270 degrees) that would put the item upright — collar, neckline or \
waistband at the top; shoes with the soles down. Find the collar or waistband first (a skirt's or pants' waistband is \
the straight end where the zip or the button starts; the hem is the open end): on the left of the photo → 90, on the \
right → 270, at the bottom → 180, already at the top → 0. Items are often laid sideways: look, don't assume 0.
- front: the number of the photo that shows the front."""

LABEL_SYSTEM = """Read the size label in these photos. Copy the size exactly as it is printed: every size system and \
unit on the label, e.g. "4 ans / 104 cm", "4A", "5-6 Y", "110 cm", "EU 38 / US 7.5". Nothing else (not the brand, not \
the care symbols). null if no size can be read."""


def front_check(photos: list[tuple[int, Path]], model: str, long_edge: int) -> FrontOut:
    """photos: (number, path) of the item's own photos of the item alone. One model call."""
    content: list[dict] = []
    for i, p in photos:
        content.append(llm.text(f"Photo {i}"))
        content.append(llm.image(p, long_edge))
    content.append(llm.text("Which photo shows the front of the item?"))
    return llm.ask(model, SYSTEM, content, FrontOut, "report_front", "Report which photo shows the front",
                   max_tokens=800)


def read_size_label(photos: list[Path], model: str, long_edge: int) -> str | None:
    """The size printed on a label, every unit included — for a kids label whose cm or years the first reading left
    out. One model call."""
    content: list[dict] = []
    for p in photos:
        content.append(llm.image(p, long_edge))
    content.append(llm.text("What size is printed on this label, exactly as printed?"))
    out = llm.ask(model, LABEL_SYSTEM, content, SizeLabel, "report_size_label", "Report the printed size",
                  max_tokens=300)
    return (out.printed or "").strip() or None
