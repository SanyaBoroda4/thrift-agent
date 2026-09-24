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

import json
import shutil
from datetime import datetime
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


def run_all(s: Settings, fixtures: Path) -> list[dict]:
    """Score every fixture case (a dir with expected.yaml), printing each result as it completes.

    A case that blows up (bad expected.yaml, API error, unreadable photo) becomes {"case", "error"} instead of
    throwing away the paid-for results of the others. Rows are written to eval/results_<stamp>.json after every
    case, so a partial or interrupted run is still inspectable.
    """
    cases = [c for c in sorted(fixtures.iterdir()) if (c / "expected.yaml").exists()]
    out = fixtures.parent / f"results_{datetime.now():%Y%m%d-%H%M%S}.json"
    results: list[dict] = []

    def save() -> None:
        out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")

    print(f"{len(cases)} cases -> {out}")
    save()
    for case in cases:
        try:
            r = run_case(s, case)
        except Exception as e:  # keep going; the row says what broke
            r = {"case": case.name, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(r)
        save()
    return results


def summarize(results: list[dict]) -> dict:
    scored = [r for r in results if "error" not in r]
    seg_ok = sum(r["segmentation_ok"] for r in scored)
    per_field = {f: [0, 0] for f in FIELDS}
    for r in scored:
        for fields in r["fields"].values():
            for f, v in fields.items():
                per_field[f][0] += v["ok"]
                per_field[f][1] += 1
    out = {"segmentation": f"{seg_ok}/{len(scored)}",
           **{f: f"{a}/{b}" for f, (a, b) in per_field.items() if b}}
    if len(scored) != len(results):
        out["errors"] = len(results) - len(scored)
    return out
