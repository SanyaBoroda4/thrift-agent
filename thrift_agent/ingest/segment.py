"""Split one shared batch (e.g. 20 photos of 3 items) into items.

Photos are ordered by capture time and items don't interleave, so the model's job is to find
boundaries in a sequence. Code then checks the answer; anything shaky goes to the seller as a
contact sheet instead of guessing.
"""
from __future__ import annotations

import re
import statistics
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
not interleave, except that an occasional forgotten detail shot of an earlier item may appear later. Items are
often shot in quick bursts, a minute or less apart, so decide by what the photos show.
One roll is ONE item unless the photos clearly show different things: a different garment type, colour, pattern,
brand or size label. NEVER split for the background, the light or white balance, the angle or distance, or for a
hanger shot vs a flat lay vs a photo worn vs one on a stand — the same shirt on a hanger and on the floor is one
item. A photo you are not sure about joins the most likely item of the roll, never an item of its own: a one-photo
group needs a visible difference from every other group.
Visual identity first. Photos of the same item share:
- the fabric or material texture (knit, denim, suede, grain, sequins), the color and shade, the print or pattern;
- the shape and silhouette (neckline, sleeves, hem, heel, toe), construction details (seams, pockets, stitching),
  hardware (buttons, zippers, buckles, chains) and trims;
- the label, brand, size tag, insole or sole stamp.
A close-up (label, tag, sole, flaw, detail) belongs to the item whose fabric, color and pattern it shows. A second
size or brand label that disagrees with the first means a new item. A wide full-item shot after close-ups often starts
a new item — check its identity.
Time is a tiebreaker only. A line like "— pause 2 min —" marks a break in shooting much longer than this roll's usual
gap: it supports a new item when the photos also differ, nothing more. Never split on a pause alone, and never keep
different-looking photos together because there is no pause between them.
Two items that look identical (same brand, type, color, size) cannot be separated from photos alone —
keep them apart only if something visible differs, and lower confidence when unsure.
Retail screenshots: some photos are phone screenshots of a retailer's product page (style name, colour, price).
Those are marked "retail screenshot"; list in `screenshots` every photo that is clearly a web/app screenshot,
marked or not. They have no place in the capture order: assign each to the item it depicts by CONTENT (brand,
product, colour), regardless of where it sits in the roll. A screenshot that matches no item goes in
`unassigned`, never into a group by guesswork.
Sizes: one normalized size per physical label, e.g. 'US 7.5' — a label printing US/EU/UK is ONE size, not several.
Every own photo must appear in exactly one group."""


MAX_REQUEST_BYTES = 20_000_000    # the API takes 32 MB per request: above this the previews fall back to a smaller size


def fmt_pause(seconds: float) -> str:
    """45 -> "45 s", 61 -> "1 min", 150 -> "3 min", 3900 -> "1 h 5 min"."""
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes = int(seconds / 60 + 0.5)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def own_pairs(kinds: list[str]) -> list[tuple[int, int]]:
    """Consecutive pairs of the seller's own photos, in capture order; retail screenshots (assigned by content, their
    times are only file times) take no part."""
    own = [i for i, k in enumerate(kinds) if k != "retail"]
    return list(zip(own, own[1:]))


def pauses(times: list[datetime], kinds: list[str] | None = None, min_seconds: float = 30,
           factor: float = 4) -> dict[int, float]:
    """Breaks in shooting, relative to this batch: {photo that comes after the break: seconds}. A gap between two
    consecutive own photos is a pause when it is longer than max(min_seconds, factor × the batch's median gap) — a
    roll shot in quick bursts has a short median, so even a minute between items stands out, while a slow roll needs
    a longer break. Fewer than two gaps: no pauses."""
    kinds = kinds or ["own"] * len(times)
    gaps = {b: (times[b] - times[a]).total_seconds() for a, b in own_pairs(kinds)}
    if len(gaps) < 2:
        return {}
    at = max(min_seconds, factor * statistics.median(gaps.values()))
    return {b: g for b, g in gaps.items() if g > at}


class Attachment(BaseModel):
    photo: int = Field(description="The new photo's number")
    item: str | None = Field(None, description="The letter of the item it shows, or null when it shows none of them")
    confidence: float = Field(ge=0, le=1, description="How sure you are")


class AttachOut(BaseModel):
    photos: list[Attachment]
    notes: str = ""


ATTACH_SYSTEM = """You decide whether a seller's late photos show items already made from the same photo roll.
You see each item (a letter, its listing title, a few of its photos), then the new photos, numbered.
The same item shares the garment type, colour and shade, print or pattern, fabric texture, shape (neckline,
sleeves, hem, heel, toe), construction details, hardware and trims, and its label, brand or size tag. A close-up
(a logo, pocket, label, buttons, fabric, flaw) belongs to the item whose fabric, colour and pattern it shows.
NEVER call a photo a different item for the background, the light or white balance, the angle or distance, or
for a hanger shot vs a flat lay vs a photo worn: the same shirt on a hanger and on the floor is one item.
A different garment type, colour, pattern, brand or size label IS a different item: then the photo shows none of
the items listed (item null). For each new photo: the letter of the item it shows, or null, with your confidence."""


def attach(new: list[Path], refs: list[tuple[str, str, list[Path]]], model: str, thumb_px: int) -> AttachOut:
    """WO45 §5/§6: which of the items already made (refs: (letter, title, a few photos each)) each new photo shows —
    the late photos of a share, a small follow-up share, a one-photo group checked against the others — by the
    photos themselves, never by comparing text descriptions (those change with the light and the angle)."""
    content: list[dict] = []
    for letter, title, photos in refs:
        content.append(llm.text(f"Item {letter} — {title}"))
        content.extend(llm.image(p, thumb_px) for p in photos)
    content.append(llm.text("New photos:"))
    for i, p in enumerate(new):
        content.append(llm.text(f"Photo {i}"))
        content.append(llm.image(p, thumb_px))
    content.append(llm.text(f"{len(new)} new photos. For each: the item it shows (a letter) or null."))
    return llm.ask(model, ATTACH_SYSTEM, content, AttachOut, "report_attachments",
                   "Report which item each new photo shows", max_tokens=1500)


def _content(photos: list[tuple[Path, datetime]], kinds: list[str], breaks: dict[int, float], px: int,
             lead: str | None = None) -> list[dict]:
    content: list[dict] = [llm.text(lead)] if lead else []       # WO51 §6: a part of a long roll says so first
    for i, ((p, _), kind) in enumerate(zip(photos, kinds)):
        if i in breaks:
            content.append(llm.text(f"— pause {fmt_pause(breaks[i])} —"))
        content.append(llm.text(f"Photo {i} · retail screenshot" if kind == "retail" else f"Photo {i}"))
        content.append(llm.image(p, px))
    content.append(llm.text(f"{len(photos)} photos. Group them into items."))
    return content


def _request_bytes(content: list[dict]) -> int:
    return sum(len(c["source"]["data"]) if c["type"] == "image" else len(c.get("text", "")) for c in content)


def segment(photos: list[tuple[Path, datetime]], model: str, thumb_px: int, kinds: list[str] | None = None,
            breaks: dict[int, float] | None = None, fallback_px: int | None = None,
            max_bytes: int = MAX_REQUEST_BYTES, report: dict | None = None, lead: str | None = None) -> SegOut:
    """`kinds[i]` is prep.photo_kind() of photo i ("own" | "retail"); None = all own. `breaks` are pauses() — shown
    to the model as "— pause 2 min —" lines, the only timing it sees. Previews are `thumb_px` on the long edge;
    when that request would be larger than `max_bytes`, or the API answers 413 (too large), they are sent at
    `fallback_px` instead of failing. `report` (if given) gets the size used: {"preview_px": …}. `lead`: a line
    before the photos (WO51 §6: which part of a long roll this is) — None for a roll segmented in one call."""
    kinds = kinds or ["own"] * len(photos)
    breaks = breaks or {}
    px = thumb_px
    content = _content(photos, kinds, breaks, px, lead)
    if fallback_px and fallback_px < px and _request_bytes(content) > max_bytes:
        px, content = fallback_px, _content(photos, kinds, breaks, fallback_px, lead)
    try:
        out = llm.ask(model, SYSTEM, content, SegOut, "report_groups", "Report the item groups", max_tokens=3000)
    except Exception as e:                            # a request too large after all: once more, smaller
        if getattr(e, "status_code", None) != 413 or not fallback_px or fallback_px >= px:
            raise
        px = fallback_px
        out = llm.ask(model, SYSTEM, _content(photos, kinds, breaks, px, lead), SegOut, "report_groups",
                      "Report the item groups", max_tokens=3000)
    if report is not None:
        report["preview_px"] = px
    return out


# ---------------------------------------------------------------- a big share in parts (WO51 §6)
#
# Live 2026-11-01: a 70-photo share went to the model in one call (it worked); a share past 90 photos failed outright
# ("share it in smaller sets") and the upload took 60 at most. A share of any size is now grouped in parts: a roll of
# up to PART_PHOTOS photos is ONE call exactly as before (the same request: the golden replays' recorded answers still
# apply — the biggest golden share has 29 photos); a bigger one in windows of at most PART_PHOTOS, neighbours sharing
# PART_OVERLAP photos. Why 40 and 8: 40 previews at 768 px stay far below the API's 100 images and the 20 MB request
# budget (no fall back to 384 px), and 40 is about the biggest roll seen grouped well in one call before (the golden
# 29-photo batch, the 42-photo share of Oct 19); 8 shared photos give each side of a seam 4 photos of context — more
# than half an item (today's 12 items had 3–10 photos, 6 typical).
#
# The stitch (`stitch`): every photo has one part TRUSTED for it — the part it lies nearer the middle of (each overlap
# is cut at its midpoint). The pieces both neighbours agree on (the overlap photos grouped alike by both — a "meet") and
# each part's own photos (its exclusive stretch) are the atoms; each part links the atoms of its groups only through
# photos it is trusted for. So an item that straddles a seam is one item (both parts saw it whole around the seam), and
# a part's mistake near its own edge — where it lacks context — never merges or splits anything: the other part,
# trusted there, decides. Where the two parts grouped the shared photos differently, a doubt is recorded (thrift
# status), never a question.

PART_PHOTOS = 40      # a roll of up to this many photos: one call, as before; a bigger one: parts of at most this many
PART_OVERLAP = 8      # photos two neighbouring parts both see — 4 on each side of the seam


def plan_parts(n: int, part: int = PART_PHOTOS, overlap: int = PART_OVERLAP) -> list[tuple[int, int]]:
    """[(start, end)] windows over range(n), in order: one window when n <= part; else as few as fit, each at most
    `part` photos, neighbours sharing `overlap` photos, all about the same length (no short last part)."""
    if n <= part:
        return [(0, n)]
    overlap = max(1, min(overlap, (part - 1) // 2))   # every part keeps photos of its own between its two overlaps
    k = -(-(n - overlap) // (part - overlap))         # ceil: the fewest windows that cover n
    size = -(-(n + (k - 1) * overlap) // k)           # ceil: even lengths (<= part, since k(part - overlap) >= n - overlap)
    step = size - overlap
    return [(i * step, n if i == k - 1 else i * step + size) for i in range(k)]


def part_lead(k: int, total: int, start: int, end: int, n: int) -> str:
    """The line before a part's photos: which part of the roll this is, and that its edges may continue elsewhere."""
    return (f"This is part {k} of {total} of one long roll of {n} photos (its photos {start}–{end - 1}, numbered from 0 "
            f"here). The first and last few photos may be the end or the start of an item shown mostly in the "
            f"neighbouring part: group them by what they show — the parts are matched up afterwards.")


def segment_roll(photos: list[tuple[Path, datetime]], model: str, thumb_px: int, kinds: list[str] | None = None,
                 breaks: dict[int, float] | None = None, fallback_px: int | None = None,
                 max_bytes: int = MAX_REQUEST_BYTES, report: dict | None = None, part: int = PART_PHOTOS,
                 overlap: int = PART_OVERLAP) -> SegOut:
    """The roll grouped into items, whatever its size: `segment` itself (the same call) up to `part` photos; else one
    `segment` per window of `plan_parts`, stitched into one SegOut in the roll's own numbering. `report` gets
    "preview_px" (the smallest size any part used), and for a roll in parts "parts" ([[start, end], …]) and "doubts"
    (where two parts grouped their shared photos differently)."""
    kinds = kinds or ["own"] * len(photos)
    breaks = breaks or {}
    windows = plan_parts(len(photos), part, overlap)
    if len(windows) == 1:
        return segment(photos, model, thumb_px, kinds=kinds, breaks=breaks, fallback_px=fallback_px,
                       max_bytes=max_bytes, report=report)
    outs, sizes = [], []
    for k, (start, end) in enumerate(windows, 1):
        used: dict = {}
        outs.append(segment(photos[start:end], model, thumb_px, kinds=kinds[start:end],
                            # a pause before a part's first photo has no photo before it in that part: left out
                            breaks={i - start: sec for i, sec in breaks.items() if start < i < end},
                            fallback_px=fallback_px, max_bytes=max_bytes, report=used,
                            lead=part_lead(k, len(windows), start, end, len(photos))))
        sizes.append(used.get("preview_px"))
    out, doubts = stitch(len(photos), windows, outs)
    if report is not None:
        report["preview_px"] = min((p for p in sizes if p), default=None)
        report["parts"] = [[start, end] for start, end in windows]
        report["doubts"] = doubts
    return out


def stitch(n: int, windows: list[tuple[int, int]], outs: list[SegOut]) -> tuple[SegOut, list[str]]:
    """Each part's grouping (in its own numbering) as ONE grouping of range(n), and the doubts in the owner's words.
    See the comment above `PART_PHOTOS`. A photo no part placed is in no group (the partition check says so: the
    contact sheet asks); a photo a part put in two groups counts for the first (a doubt)."""
    last = len(windows) - 1
    doubts: list[str] = []
    groups: list[list[list[int]]] = []            # part → group → its photos, in the roll's numbering
    owner: list[dict[int, int]] = []              # part → photo → its group there (the first, if listed twice)
    for j, ((start, _), out) in enumerate(zip(windows, outs)):
        gs = [[start + i for i in g.photos] for g in out.groups]
        own: dict[int, int] = {}
        for gi, g in enumerate(gs):
            for p in g:
                if p in own and own[p] != gi:
                    doubts.append(f"part {j + 1} put photo {p} in two items")
                own.setdefault(p, gi)
        groups.append(gs)
        owner.append(own)
    # the overlaps, each cut at its midpoint: part j is trusted for [mids[j], mids[j + 1])
    overlaps = [(windows[j + 1][0], windows[j][1]) for j in range(last)]
    mids = [0, *[(a + b) // 2 for a, b in overlaps], n]

    def trusted(p: int) -> int:
        return next(j for j in range(last + 1) if mids[j] <= p < mids[j + 1])

    def overlap_of(p: int) -> int | None:
        return next((j for j, (a, b) in enumerate(overlaps) if a <= p < b), None)

    # the atoms: a part's group's own photos (seen by that part alone), and in each overlap the photos both parts
    # grouped alike (keyed by their group in each part; a photo one part left out has None there) — each an int id
    ids: dict[tuple, int] = {}
    atom_of: dict[int, int] = {}
    for p in range(n):
        o = overlap_of(p)
        if o is None:
            j = trusted(p)
            if p in owner[j]:
                atom_of[p] = ids.setdefault(("own", j, owner[j][p]), len(ids))
        else:
            key = (owner[o].get(p), owner[o + 1].get(p))
            if key != (None, None):
                atom_of[p] = ids.setdefault(("meet", o, key), len(ids))
    parent = list(range(len(ids)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    # each part links the atoms of each of its groups, through the photos it is trusted for only
    for j, gs in enumerate(groups):
        for gi, g in enumerate(gs):
            atoms = sorted({atom_of[p] for p in g if owner[j].get(p) == gi and p in atom_of and trusted(p) == j})
            for a in atoms[1:]:
                ra, rb = find(atoms[0]), find(a)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    items: dict[int, list[int]] = {}
    for p, a in sorted(atom_of.items()):
        items.setdefault(find(a), []).append(p)
    ordered = sorted(items.values(), key=lambda ps: ps[0])
    # where the two parts grouped their shared photos differently: a doubt naming the items around it
    item_of = {p: k for k, ps in enumerate(ordered, 1) for p in ps}
    for o, (a, b) in enumerate(overlaps):
        shared = [p for p in range(a, b) if p in owner[o] and p in owner[o + 1]]
        if any((owner[o][p] == owner[o][q]) != (owner[o + 1][p] == owner[o + 1][q])
               for x, p in enumerate(shared) for q in shared[x + 1:]):
            near = sorted({item_of[p] for p in range(a, b) if p in item_of})
            doubts.append(f"photos {a}–{b - 1}: two parts of the roll grouped them differently — check item"
                          f"{'s' if len(near) != 1 else ''} {', '.join(map(str, near))}")
    screenshots = sorted(p for p in range(n) if p - windows[trusted(p)][0] in outs[trusted(p)].screenshots)
    unassigned = sorted(p for p in range(n) if p not in atom_of
                        and any(p - windows[j][0] in outs[j].unassigned for j in range(last + 1)
                                if windows[j][0] <= p < windows[j][1]))
    return SegOut(groups=[_merged_group(ps, windows, groups, owner, outs, trusted) for ps in ordered],
                  screenshots=screenshots, unassigned=unassigned,
                  notes=" ".join(o.notes for o in outs if o.notes).strip()), doubts


def _merged_group(photos: list[int], windows, groups, owner, outs: list[SegOut], trusted) -> Group:
    """One stitched item's Group: its photos; a photo is a full shot / a label as the part trusted for it said; the
    sizes, the lowest confidence and the summary of the part groups it is made of (the summary of the one with the
    most of its photos)."""
    mine = set(photos)
    full, labels = [], []
    for p in photos:
        j = trusted(p)
        gi = owner[j].get(p)
        g = outs[j].groups[gi] if gi is not None else None
        if g is None:                                  # placed by the other part only (it left this one out here)
            j = next((k for k in range(len(outs)) if p in owner[k]), j)
            gi = owner[j].get(p)
            g = outs[j].groups[gi] if gi is not None else None
        if g is None:
            continue
        local = p - windows[j][0]
        if local in g.full_item_photos:
            full.append(p)
        if local in g.label_photos:
            labels.append(p)
    parts = []                                         # (part, group, how many of this item's photos it holds)
    for j, gs in enumerate(groups):
        for gi, g in enumerate(gs):
            held = sum(1 for p in g if p in mine and owner[j].get(p) == gi)
            if held:
                parts.append((j, gi, held, g))
    sizes: list[str] = []
    for j, gi, held, g in parts:
        src = outs[j].groups[gi]
        label_here = [windows[j][0] + i for i in src.label_photos if windows[j][0] + i in mine]
        if label_here or (not src.label_photos and held * 2 >= len(g)):
            sizes += [s for s in src.sizes_read if s not in sizes]
    best = max(parts, key=lambda t: t[2]) if parts else None
    return Group(photos=photos, summary=outs[best[0]].groups[best[1]].summary if best else "",
                 full_item_photos=full, label_photos=labels, sizes_read=sizes,
                 confidence=min((outs[j].groups[gi].confidence for j, gi, _, _ in parts), default=0.0))


# ---------------------------------------------------------------- the code's own reading of the roll

SIG_PX, HUES, SATS, GREYS = 48, 12, 3, 3
GREY_SAT, DARK = 48, 40          # HSV (0..255): below this saturation, or this brightness, a pixel is a grey


def color_signature(path: Path) -> list[float]:
    """The colours of the photo's central area — mostly the item, not the backdrop — as a coarse histogram: 12 hues ×
    3 saturations, plus 3 grey levels. Sums to 1."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        data = im.crop((w // 5, h // 5, w - w // 5, h - h // 5)).resize((SIG_PX, SIG_PX)).convert("HSV").tobytes()
    bins = [0] * (HUES * SATS + GREYS)
    for k in range(0, len(data), 3):
        hue, sat, val = data[k], data[k + 1], data[k + 2]
        if sat < GREY_SAT or val < DARK:
            bins[HUES * SATS + min(val * GREYS // 256, GREYS - 1)] += 1
        else:
            bins[(hue * HUES // 256) * SATS + min((sat - GREY_SAT) * SATS // (256 - GREY_SAT), SATS - 1)] += 1
    return [b / (SIG_PX * SIG_PX) for b in bins]


def color_distance(a: list[float], b: list[float]) -> float:
    """0 = the same colours, 1 = nothing in common (one minus the histograms' overlap)."""
    return 1.0 - sum(min(x, y) for x, y in zip(a, b))


def visual_changes(photos: list[Path], kinds: list[str] | None = None, min_distance: float = 0.45,
                   factor: float = 2.5) -> tuple[dict[int, float], set[int]]:
    """({own photo: colour distance from the own photo before it}, {photos where the look changes}). A change is a
    distance of at least max(min_distance, factor × the batch's median distance): close-ups of one item vary, so the
    bar is relative to the roll. Colour only — it can't tell two black dresses apart; the model judges identity."""
    kinds = kinds or ["own"] * len(photos)
    pairs = own_pairs(kinds)
    sigs = {i: color_signature(photos[i]) for pair in pairs for i in pair}
    dist = {b: round(color_distance(sigs[a], sigs[b]), 3) for a, b in pairs}
    if not dist:
        return {}, set()
    at = max(min_distance, factor * statistics.median(dist.values()))
    return dist, {b for b, d in dist.items() if d >= at}


def timing_check(groups: list[list[int]], kinds: list[str] | None, breaks: dict[int, float],
                 changes: set[int], confidence: list[float] | None = None, min_conf: float = 0.0) -> list[str]:
    """What the code doubts in the model's grouping (shown in the contact-sheet message): one item spanning a pause
    AND a visual change (two items?), or a boundary between items with neither (one item?). Only where the model
    itself was unsure (a group's confidence below min_conf): a pause or a colour histogram never second-guesses what
    Opus split with confidence — two different white tees shot back to back, a try-on shot after a break (WO20).
    Never a decision: the owner confirms the batch."""
    kinds = kinds or ["own"] * (max((i for g in groups for i in g), default=-1) + 1)
    owner = {i: k for k, g in enumerate(groups) for i in g}
    confidence = confidence or [0.0] * len(groups)
    reasons = []
    for a, b in own_pairs(kinds):
        if a not in owner or b not in owner:
            continue                                  # a partition error: check() says so
        if min(confidence[owner[a]], confidence[owner[b]]) >= min_conf > 0:
            continue                                  # the model was sure of both: no doubt raised here
        pause, change = b in breaks, b in changes
        if owner[a] == owner[b] and pause and change:
            reasons.append(f"item {owner[a] + 1}: a pause ({fmt_pause(breaks[b])}) and a visual change between "
                           f"photos {a} and {b} — two items?")
        elif owner[a] != owner[b] and not pause and not change:
            reasons.append(f"items {owner[a] + 1} and {owner[b] + 1}: no pause and no visual change between photos "
                           f"{a} and {b} — one item?")
    return reasons


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
        if own and own != list(range(min(own), max(own) + 1)) and g.confidence < min_conf:
            # Not when the model is confident: the owner shoots try-on / mirror photos at the end of the roll (WO20).
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
                  kinds: list[str] | None = None, breaks: dict[int, float] | None = None) -> Path:
    """Tiles labelled "#i · item k"; retail screenshots read "#i · retail · item k" ("?" when in no item). A photo
    taken after a pause (pauses()) carries a dark "pause 2 min" banner at its top left."""
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
            if breaks and i in breaks:                          # shot after a break: where a new item may start
                mark = f"pause {fmt_pause(breaks[i])}"
                draw.rectangle([x + 8, y + 8, x + 16 + int(draw.textlength(mark, font=small)), y + 36], fill="#222222")
                draw.text((x + 12, y + 10), mark, fill="white", font=small)
            draw.rectangle([x, y + tile, x + tile - 1, y + tile + LABEL_STRIP - 1], fill="white")
            f = font if draw.textlength(label, font=font) <= tile - 16 else small
            draw.text((x + 8, y + tile + 5), label, fill=color, font=f)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, "PNG")
    return dst
