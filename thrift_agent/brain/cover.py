"""Which photo shows the FRONT of the item, and how to turn it upright (WO23).

Front and back are a comparison, not a guess from one photo: the model sees the item's own photos of the item alone side
by side, numbered, at a readable size, and answers one question — which shows the front? — with, for each photo, the
side it shows, how much design detail it has, and the turn that would put the item upright. pipeline.choose_cover
checks the answer in code. Also here: a focused re-read of a size label, for a kids label whose cm or years were left
out of the first reading."""
from __future__ import annotations

from pathlib import Path

from thrift_agent.brain import llm
from PIL import Image

from thrift_agent.schema import FrontOut, SizeLabel, UprightOut

SYSTEM = """You compare photos of ONE item (a garment, a pair of shoes, a bag, ...) to find the photo that shows its \
FRONT: the listing's cover.

- Photos marked "item alone" are the candidates. Photos marked "worn — reference" show the item on a person, usually a \
mirror selfie, i.e. its FRONT as it is worn: use them to recognise the front side (where the print, buttons, zip or \
pockets sit when it is worn). A worn photo is never the answer.
- For each "item alone" photo say which side it shows: front, back, side or unclear.
- The FRONT is the side with a print, graphic, text, logo, buttons, pockets or the lower neckline. A plain side is the \
BACK when another photo shows a print or other design on the other side. Pants, jeans and skirts: back pockets and the \
yoke are the back. A zip: a fly zip with a button (pants, shorts, jeans) is the front, but a zip running down the \
middle of a skirt or a dress is almost always at the back — check the worn reference: if no zip shows there, the zip \
side is the back. Shoes: the outer side profile or the pair seen from the front are both a front.
- When two sides look alike, the one with more printed design is the front.
- A garment photographed sideways or upside down — laid on the floor in any direction — is still a valid front flat \
lay: judge the side that is shown, not the angle.
- design: how much printed design the photo shows — a print, graphic, text or logo (none / some / strong).
- top: where the TOP of the item lies in the photo — the collar or neckline; for skirts, pants and shorts the \
waistband (the straight end where the zip or the button starts; the hem is the open end); for shoes the opening, with \
the sole at the opposite side. Answer top, left, right or bottom: the edge of the photo it is nearest to. Items are \
often laid sideways: look, don't assume top.
- front: the number of the "item alone" photo that shows the front."""

LABEL_SYSTEM = """Read the size label in these photos. Copy the size exactly as it is printed: every size system and \
unit on the label, e.g. "4 ans / 104 cm", "4A", "5-6 Y", "110 cm", "EU 38 / US 7.5". Nothing else (not the brand, not \
the care symbols). null if no size can be read."""


def front_check(photos: list[tuple[int, Path]], model: str, long_edge: int,
                worn: list[tuple[int, Path]] = ()) -> FrontOut:
    """photos: (number, path) of the item's own photos of the item alone — the candidates; worn: try-on / mirror
    photos, shown as a reference for how the front looks when worn (never the answer). One model call."""
    content: list[dict] = []
    for i, p in photos:
        content.append(llm.text(f"Photo {i} (item alone)"))
        content.append(llm.image(p, long_edge))
    for i, p in worn:
        content.append(llm.text(f"Photo {i} (worn \u2014 reference, never the answer)"))
        content.append(llm.image(p, long_edge))
    content.append(llm.text("Which 'item alone' photo shows the front of the item?"))
    return llm.ask(model, SYSTEM, content, FrontOut, "report_front", "Report which photo shows the front",
                   max_tokens=800)


UPRIGHT_SYSTEM = """You see the same photo four times, turned four ways: A, B, C and D. Which one shows the item \
UPRIGHT, the way it is worn or used — a garment with its collar, neckline or waistband at the top and its hem at the \
bottom; shoes standing on their soles; a bag with its handles or opening at the top? Answer the letter."""
TURNS = {"A": 0, "B": 90, "C": 180, "D": 270}                # clockwise degrees each picture was turned


def upright_check(photo: Path, model: str, long_edge: int = 512) -> int:
    """The clockwise turn (0, 90, 180, 270) that puts the item in `photo` upright: the photo shown turned all four
    ways, the model picks the upright picture (WO23 — a choice between pictures is far steadier than naming where a
    collar lies; live, that reading varied from run to run). One small call."""
    content: list[dict] = []
    with Image.open(photo) as im:
        im = im.convert("RGB")
        im.thumbnail((long_edge, long_edge))
        for letter, turn in TURNS.items():
            turned = im.rotate(-turn, expand=True) if turn else im
            path = photo.parent / f".upright_{letter}.jpg"
            turned.save(path, "JPEG", quality=85)
            content.append(llm.text(f"Picture {letter}"))
            content.append(llm.image(path, long_edge))
            path.unlink(missing_ok=True)
    content.append(llm.text("Which picture shows the item upright?"))
    out = llm.ask(model, UPRIGHT_SYSTEM, content, UprightOut, "report_upright", "Report the upright picture",
                  max_tokens=200)
    return TURNS[out.upright]


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
