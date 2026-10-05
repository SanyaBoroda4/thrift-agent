"""The labels read closely (WO26): an item's label and tag photos at full size, once, for the premium details a listing
states exactly — the fiber composition, the country of manufacture, a premium line, vintage cues, a collaboration or
limited edition, technical features, construction, a retail price printed on a hang tag. The item's detail photos come
along at the usual size, for construction a photo shows (a lining, beading). Only what is printed or plainly visible,
every value with the photos that show it; brain/premium.py words it for the listing."""
from __future__ import annotations

from pathlib import Path

from thrift_agent.brain import llm
from thrift_agent.schema import Premium

SYSTEM = """You read the LABELS and TAGS of one second-hand item (clothing, shoes, a bag) for its resale listing. Report \
only what is printed on them or plainly visible in the photos, each value with the numbers of the photos that show it. \
Never guess: leave a field empty when the photos don't show it. Photos may be sideways or upside down and the print \
small: read every label in any orientation, all of its lines.

- composition: the fiber content exactly as printed — one entry per fiber, in English and lower case (silk, cashmere, \
merino wool, wool, cotton, polyester, elastane…), with its percentage and the part it is for: main (the shell or body \
fabric), lining, trim, fill. The same content printed in several languages ("100% SILK / 100% SOIE / 100% SEDA") is \
ONE entry: silk 100 main. A shoe or bag stamp like "genuine leather" or "leather upper" is leather 100 main.
- made_in: the country of manufacture as printed ("MADE IN ITALY" -> Italy).
- line: a premium line or sub-label printed next to the brand (Collection, Purple Label, Heritage, Made & Crafted, \
Studio, Limited Edition, We The Free, Maeve, Pilcro), not the brand itself.
- vintage: only with a concrete cue the photos show — a union label, an old tag style, a single-stitch hem, Levi's Big E \
red tab, a date printed on the care tag. List each cue in vintage_cues; vintage = the era when it is clear ("1990s"), \
else "vintage". No cue: leave both empty.
- collab: a collaboration ("x Erdem"), "Limited Edition", or a sample — "Sample" with the sample type the tag prints, \
e.g. "Sample: 1st Proto Fit" (a factory sample tag: "SAMPLE TYPE: 1ST PROTO FIT").
- technical: performance features as printed: Gore-Tex, waterproof, down fill (the percentage or fill power), \
Primaloft, UPF 50+.
- construction: fully lined, silk lining, hand-knit, handmade, hand-beaded or embroidered, Goodyear welt — only when a \
label prints it or a photo plainly shows it. Never a negative ("unlined").
- retail_price: a price printed on an ATTACHED hang tag, digits only ("$128.00" -> "128")."""


def read_labels(labels: list[tuple[int, Path]], details: list[tuple[int, Path]], model: str, label_edge: int,
                detail_edge: int) -> Premium:
    """labels: (number, path) of the item's label and tag photos, sent at label_edge; details: its detail photos, at
    detail_edge. One model call."""
    content: list[dict] = []
    for i, p in labels:
        content.append(llm.text(f"Photo {i} (label or tag)"))
        content.append(llm.image(p, label_edge))
    for i, p in details:
        content.append(llm.text(f"Photo {i} (detail)"))
        content.append(llm.image(p, detail_edge))
    content.append(llm.text("Read the labels and tags: what do they print, exactly?"))
    return llm.ask(model, SYSTEM, content, Premium, "report_labels", "Report what the labels print",
                   max_tokens=1500)
