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

SYSTEM = """You look at EVERY photo of ONE item (a garment, a pair of shoes, a set, a bag, ...) and label each one, \
so the listing's cover can be chosen: the WHOLE item, from the FRONT, nothing cut off, not too close.

- For every photo give:
  - view: front / three_quarter / side / back = the item seen from there; top = looked down at from above (for shoes: \
down into their opening, insoles showing); worn = on a person (a try-on, a mirror selfie); label = a brand, size or \
care label, an insole stamp, a hang tag or a price tag; detail = a close-up of part of the item (stitching, toes, a \
heel, a zip, a fabric); inside = the inside of the item; sole = a shoe's sole; flaw = a close-up of a flaw; box = the \
box or packaging; other (a screenshot, anything else).
  - whole: true only when the WHOLE item is in the frame — every part of it: both shoes of a pair, both pieces of a \
set, a garment from its top to its hem, both straps of a bag. A close-up is never whole.
  - cut_off: true when part of the item runs out of the frame at any edge.
  - fill: how much of the frame the item fills, 0 to 1.
  - held: true when a hand holds the item (a hand holding a shoe up from behind is held, view back).
- Photos marked "worn" are on a person, usually a mirror selfie, i.e. the FRONT as it is worn: use them to recognise \
the front side (where the print, buttons, zip or pockets sit when it is worn).
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
- front: the number of the photo you would make the cover — the whole item from the front, nothing cut off: \
clothing flat or on a hanger (worn only when no such photo exists); shoes the pair from the side or a 3/4 front view, \
both shoes whole; a set both pieces together. Never a label, a close-up, a sole, a look down into shoes, a flaw, a \
hand holding the item up from behind, or a photo where part of the item is outside the frame."""

LABEL_SYSTEM = """Read the size label in these photos. Copy the size exactly as it is printed: every size system and \
unit on the label, e.g. "4 ans / 104 cm", "4A", "5-6 Y", "110 cm", "EU 38 / US 7.5". Nothing else (not the brand, not \
the care symbols). null if no size can be read."""


def front_check(photos: list[tuple[int, Path]], model: str, long_edge: int,
                worn: list[tuple[int, Path]] = ()) -> FrontOut:
    """photos: (number, path) of the item's own photos — WO37: every one of them, each labelled (view, whole, cut off,
    fill, held); worn: try-on / mirror photos the caller marks as such (labelled too). One model call; pipeline.
    choose_cover decides from the labels."""
    content: list[dict] = []
    marked = {i for i, _ in worn}
    for i, p in sorted([*photos, *worn], key=lambda t: t[0]):
        content.append(llm.text(f"Photo {i}" + (" (worn)" if i in marked else "")))
        content.append(llm.image(p, long_edge))
    content.append(llm.text("Label every photo, then give the number of the photo you would make the cover."))
    return llm.ask(model, SYSTEM, content, FrontOut, "report_front", "Label every photo and name the cover",
                   max_tokens=400 + 120 * (len(photos) + len(worn)))


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
