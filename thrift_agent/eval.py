"""M0: measure before automating.

eval/fixtures/<case>/
    photos/*.jpg|heic      one real shooting session (1+ items)
    expected.yaml          what you know is true:
        items:
          - photos: [0, 1, 2, 3]        # indices after capture-time sort + dedupe
            brand: Tory Burch
            size_us: "7.5"
            category: Shoes
            condition: excellent
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from thrift_agent.brain.extract import extract
from thrift_agent.config import Settings
from thrift_agent.ingest import prep, segment as seg

FIELDS = ("brand", "size_us", "category", "condition")


def _eq(a, b) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def run_case(s: Settings, case: Path) -> dict:
    exp = yaml.safe_load((case / "expected.yaml").read_text(encoding="utf-8"))
    work = s.path("work") / "_eval" / case.name
    shutil.rmtree(work, ignore_errors=True)
    listed = prep.list_photos(case / "photos")
    norm = [prep.normalize(p, work / f"{i:03d}.jpg", s["images"]["work_long_edge"]) for i, (p, _) in enumerate(listed)]
    kept, _ = prep.drop_near_duplicates(norm, [t for _, t in listed], s["images"]["dedupe_hamming"])
    times = [listed[int(p.stem)][1] for p in kept]

    out = seg.segment(list(zip(kept, times)), s["models"]["segment"], s["images"]["thumb_long_edge"])
    got_groups = [g.photos for g in out.groups]
    want_groups = [i["photos"] for i in exp["items"]]
    result = {"case": case.name, "segmentation_ok": got_groups == want_groups,
              "groups": {"want": want_groups, "got": got_groups},
              "flags": seg.check(out, len(kept), s["segmentation"]["min_confidence"]), "fields": {}}

    # Extraction is scored on the TRUE groups so one segmentation miss doesn't hide extraction quality.
    for k, item in enumerate(exp["items"]):
        facts = extract([kept[i] for i in item["photos"]], None, s["models"]["extract"], s["images"]["llm_long_edge"])
        got = {"brand": facts.brand.value, "size_us": facts.size_us.value,
               "category": facts.category, "condition": facts.condition}
        result["fields"][k] = {f: {"want": item.get(f), "got": got[f], "ok": _eq(item.get(f), got[f])}
                               for f in FIELDS if f in item}
    return result


def summarize(results: list[dict]) -> dict:
    seg_ok = sum(r["segmentation_ok"] for r in results)
    per_field = {f: [0, 0] for f in FIELDS}
    for r in results:
        for fields in r["fields"].values():
            for f, v in fields.items():
                per_field[f][0] += v["ok"]
                per_field[f][1] += 1
    return {"segmentation": f"{seg_ok}/{len(results)}",
            **{f: f"{a}/{b}" for f, (a, b) in per_field.items() if b}}
