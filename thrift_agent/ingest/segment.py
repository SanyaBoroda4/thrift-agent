"""Split one shared batch (e.g. 20 photos of 3 items) into items.

Photos are ordered by capture time and items don't interleave, so the model's job is to find
boundaries in a sequence. Code then checks the answer; anything shaky goes to the seller as a
contact sheet instead of guessing.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from thrift_agent.brain import llm


class Group(BaseModel):
    photos: list[int] = Field(description="Photo indices in this item, in order")
    summary: str = Field(description="Short description, e.g. 'navy suede ankle boots'")
    full_item_photos: list[int] = Field(description="Photos showing the whole item")
    label_photos: list[int] = Field(default_factory=list, description="Size/brand/care label or insole photos")
    sizes_read: list[str] = Field(default_factory=list, description=(
        "One normalized size per physical label, e.g. 'US 7.5' — a label printing US/EU/UK is ONE size, not several"))
    confidence: float = Field(ge=0, le=1, description="Confidence all photos here are the same item")


class SegOut(BaseModel):
    groups: list[Group]
    screenshots: list[int] = Field(default_factory=list, description=(
        "Photos that are web/app screenshots rather than the seller's own photos"))
    unassigned: list[int] = Field(default_factory=list, description="Retail screenshots that match no item")
    notes: str = ""


SYSTEM = """You split a seller's photo roll into items for resale listings.
The seller's own photos are in capture order. The seller shoots one item completely, then the next; items do
not interleave, except that an occasional forgotten detail shot of an earlier item may appear later.
Signals: a wide full-item shot after close-ups usually starts a new item; fabric, color and pattern
continuity ties close-ups (labels, tags, soles, flaws) to the garment around them; a second size label
that disagrees with the first means a new item; a longer time gap is a weak hint.
Two items that look identical (same brand, type, color, size) cannot be separated from photos alone —
keep them apart only if something visible differs, and lower confidence when unsure.
Retail screenshots: some photos are phone screenshots of a retailer's product page (style name, colour, price).
Those are marked "retail screenshot"; list in `screenshots` every photo that is clearly a web/app screenshot,
marked or not. They have no place in the capture order: assign each to the item it depicts by CONTENT (brand,
product, colour), regardless of where it sits in the roll. A screenshot that matches no item goes in
`unassigned`, never into a group by guesswork.
Sizes: one normalized size per physical label, e.g. 'US 7.5' — a label printing US/EU/UK is ONE size, not several.
Every own photo must appear in exactly one group."""


def segment(photos: list[tuple[Path, datetime]], model: str, thumb_px: int,
            kinds: list[str] | None = None) -> SegOut:
    """`kinds[i]` is prep.photo_kind() of photo i ("own" | "retail"); None = all own."""
    kinds = kinds or ["own"] * len(photos)
    t0 = next((ts for (_, ts), k in zip(photos, kinds) if k != "retail"), photos[0][1])   # screenshots have no EXIF time
    content: list[dict] = []
    for i, ((p, ts), kind) in enumerate(zip(photos, kinds)):
        tag = "retail screenshot" if kind == "retail" else f"t=+{int((ts - t0).total_seconds())}s"
        content.append(llm.text(f"Photo {i} · {tag}"))
        content.append(llm.image(p, thumb_px))
    content.append(llm.text(f"{len(photos)} photos. Group them into items."))
    return llm.ask(model, SYSTEM, content, SegOut, "report_groups", "Report the item groups", max_tokens=3000)


def check(seg: SegOut, n: int, min_conf: float, kinds: list[str] | None = None) -> list[str]:
    """Code-side sanity checks. Empty list = trustworthy.
    Retail screenshots (per `kinds` or the model's own `screenshots`) sit outside the capture order: they don't
    count for contiguity, and an item needs at least one of the seller's own photos, one of them a full shot."""
    reasons: list[str] = []
    retail = {i for i, k in enumerate(kinds or []) if k == "retail"} | set(seg.screenshots)
    seen = [i for g in seg.groups for i in g.photos] + list(seg.unassigned)
    if sorted(seen) != list(range(n)):
        missing = sorted(set(range(n)) - set(seen))
        dupes = sorted({i for i in seen if seen.count(i) > 1})
        reasons.append(f"photos not partitioned (missing={missing}, duplicated={dupes})")
    for k, g in enumerate(seg.groups, 1):
        own = [i for i in g.photos if i not in retail]
        if not g.photos:
            reasons.append(f"item {k}: empty group")
        if g.photos and not own:
            reasons.append(f"item {k}: only screenshots, no own photo")
        elif not [i for i in g.full_item_photos if i not in retail]:
            reasons.append(f"item {k}: no full-item photo")
        if len({s.strip().lower() for s in g.sizes_read}) > 1:
            reasons.append(f"item {k}: conflicting sizes {g.sizes_read}")
        if g.confidence < min_conf:
            reasons.append(f"item {k}: low confidence {g.confidence:.2f}")
        if own and own != list(range(min(own), max(own) + 1)):
            reasons.append(f"item {k}: non-contiguous photos {own}")
    for i in sorted(set(seg.unassigned)):
        reasons.append(f"screenshot {i} matches no item — reply '{i}>2' (into item 2) or 'drop {i}'")
    return reasons


def parse_drops(cmd: str) -> tuple[str, list[int]]:
    """Split "drop N" parts out of a comma-separated correction: "drop 7, 3>2, drop 9" -> ("3>2", [7, 9]).
    The caller removes the dropped photos from the groups, then hands the rest ("ok" if nothing is left)
    to apply_correction."""
    keep, drops = [], []
    for part in [c.strip() for c in cmd.split(",") if c.strip()]:
        if m := re.fullmatch(r"drop\s+(\d+)", part.lower()):
            drops.append(int(m[1]))
        else:
            keep.append(part)
    return ", ".join(keep) or "ok", sorted(set(drops))


def apply_correction(groups: list[list[int]], cmd: str, n: int | None = None) -> list[list[int]]:
    """Seller replies from Telegram. Groups are 1-based in commands, photos are indices.
      ok            accept
      12>2          move photo 12 into item 2 (item N+1 starts a new item)
      split 7       photo 7 starts a new item
      merge 2 3     items 2 and 3 are the same item
    Several commands can be separated by commas. Item numbers refer to the groups as they were when the
    command string started (emptied items are dropped only at the end). Anything that would silently lose,
    duplicate or invent a photo raises ValueError: stop, don't guess.
    `n` is the batch's photo count: a photo the model left out of every group (the sheet labels it "item 0")
    can then be placed with `5>2` or `split 5`. Whether the result covers every photo is checked by the caller."""
    groups = [list(g) for g in groups]
    universe = sorted(i for g in groups for i in g)
    last = n - 1 if n is not None else (universe[-1] if universe else 0)

    def item(text: str, allow_new: bool = False) -> int:
        k = int(text)
        if not 1 <= k <= len(groups) + (1 if allow_new else 0):
            raise ValueError(f"no item {k} (items are 1..{len(groups)})")
        return k - 1

    def owner(photo: int) -> int | None:
        if not 0 <= photo <= last:
            raise ValueError(f"no photo {photo} (photos are 0..{last})")
        for k, g in enumerate(groups):
            if photo in g:
                return k
        return None                                              # exists, but the model put it in no item

    for part in [c.strip().lower() for c in cmd.split(",") if c.strip()]:
        if part == "ok":
            continue
        if m := re.fullmatch(r"(\d+)\s*>\s*(\d+)", part):
            photo = int(m[1])
            src, dest = owner(photo), item(m[2], allow_new=True)
            if src is not None:
                groups[src].remove(photo)
            if dest == len(groups):
                groups.append([])
            groups[dest] = sorted(groups[dest] + [photo])
        elif m := re.fullmatch(r"split\s+(\d+)", part):
            photo = int(m[1])
            k = owner(photo)
            if k is None:
                groups.append([photo])                           # an orphan photo becomes its own item
                continue
            idx = groups[k].index(photo)
            if idx > 0:
                groups[k:k + 1] = [groups[k][:idx], groups[k][idx:]]
        elif m := re.fullmatch(r"merge\s+(\d+)\s+(\d+)", part):
            a, b = item(m[1]), item(m[2])
            if a == b:
                raise ValueError(f"merge needs two different items, got {m[1]} twice")
            groups[a] = sorted(groups[a] + groups[b])
            groups[b] = []
        else:
            raise ValueError(f"can't read correction: {part!r}")
    out = [g for g in groups if g]
    got = sorted(i for g in out for i in g)
    if len(got) != len(set(got)) or not set(universe) <= set(got):   # belt and braces: nothing lost or doubled
        raise ValueError("correction would lose or duplicate photos")
    return out


PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c"]
LABEL_STRIP = 40           # px under each tile for "#i · item k"; the seller reads this on a phone


def contact_sheet(photos: list[Path], groups: list[list[int]], dst: Path, tile: int = 260, cols: int = 5,
                  kinds: list[str] | None = None) -> Path:
    """Tiles labelled "#i · item k"; retail screenshots read "#i · retail · item k" ("?" when in no item)."""
    owner = {i: k for k, g in enumerate(groups) for i in g}
    rows = (len(photos) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * (tile + LABEL_STRIP)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=26)                       # Pillow >= 10.1: a real (scalable) font
    small = ImageFont.load_default(size=20)                      # for the longer retail labels
    for i, p in enumerate(photos):
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((tile - 12, tile - 12))
            x, y = (i % cols) * tile, (i // cols) * (tile + LABEL_STRIP)
            k = owner.get(i)
            if kinds and kinds[i] == "retail":
                label = f"#{i} · retail · " + (f"item {k + 1}" if k is not None else "?")
                color = "#888888" if k is None else PALETTE[k % len(PALETTE)]
            else:
                label, color = f"#{i} · item {owner.get(i, -1) + 1}", PALETTE[owner.get(i, 0) % len(PALETTE)]
            draw.rectangle([x + 2, y + 2, x + tile - 3, y + tile - 3], outline=color, width=6)
            sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
            draw.rectangle([x, y + tile, x + tile - 1, y + tile + LABEL_STRIP - 1], fill="white")
            f = font if draw.textlength(label, font=font) <= tile - 16 else small
            draw.text((x + 8, y + tile + 5), label, fill=color, font=f)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, "PNG")
    return dst
