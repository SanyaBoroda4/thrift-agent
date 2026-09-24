"""Second opinion + deterministic lint. Strips any claim the facts don't support."""
from __future__ import annotations

import json
import re

from thrift_agent.brain import llm
from thrift_agent.schema import CopyOut, Facts, VerifyOut

SYSTEM = """You audit resale listing copy against a fact sheet. A claim is unsupported if the facts
don't state it (fabric, fit, measurements, era, authenticity, odor/smoke claims, "true to size",
a condition better than the facts', a missing flaw). Return the copy with unsupported claims removed
and missing flaws added, changing nothing else. If everything is supported, return it unchanged."""


def verify(facts: Facts, copy: CopyOut, model: str) -> VerifyOut:
    content = [llm.text("FACTS\n" + facts.model_dump_json(indent=1) +
                        "\n\nCOPY\n" + json.dumps({
                            "poshmark_title": copy.poshmark_title,
                            "poshmark_description": copy.poshmark_description,
                            "depop_description": copy.depop_description}, indent=1))]
    return llm.ask(model, SYSTEM, content, VerifyOut, "report_audit", "Report the audit", max_tokens=2500)


FLAW_WORDS = r"flaw|wear|scuff|stain|mark|pill|snag|hole|tear|scratch|crease|fad|discolor|missing"
COLOR_WORDS = (r"\b(red|pink|orange|yellow|green|olive|blue|navy|purple|gold|silver|black|gray|grey|charcoal|"
               r"white|cream|ivory|brown|tan|beige|nude)\b")
BANNED = [r"\bsmoke[- ]free\b", r"\bpet[- ]free\b", r"\bauthentic\b", r"\btrue to size\b", r"\b100%\s*\w+"]


def lint(facts: Facts, copy: CopyOut) -> list[str]:
    problems: list[str] = []
    t, d = copy.poshmark_title, copy.poshmark_description
    everything = f"{t}\n{d}\n{copy.depop_description}"
    if len(t) > 80:
        problems.append("title over 80 chars")
    if len(copy.depop_description) > 1000:
        problems.append("depop description over 1000 chars")
    if facts.brand.value and facts.brand.value.split()[0].lower() not in t.lower():
        problems.append("brand missing from title")
    if facts.size_us.value and facts.size_us.value.lower() not in t.lower():
        problems.append("US size missing from title")
    if re.search(r"\bNWT\b|new with tags", everything, re.I) and facts.condition != "NWT":
        problems.append("says NWT but facts aren't NWT")
    if re.search(r"\d,5\b", everything):
        problems.append("decimal comma in a size")
    material = (facts.material.value or "").lower()
    for pat in BANNED:
        if pat.startswith(r"\b100%") and "100%" in material:
            continue
        if re.search(pat, everything, re.I):
            problems.append(f"unsupported phrase: {pat}")
    if facts.department != "Kids" and re.search(r"\bkids?'?\b|\btoddler|\bgirls'?\b|\bboys'?\b", everything, re.I):
        problems.append("mentions kids but the item isn't Kids")
    if facts.department == "Women" and re.search(r"\bmen'?s\b", everything, re.I) and "women" not in everything.lower():
        problems.append("mentions men's on a Women's item")
    named = set(re.findall(COLOR_WORDS, everything.lower()))
    allowed = {c.lower() for c in facts.colors} | set(re.findall(COLOR_WORDS, (facts.color_name or "").lower()))
    allowed |= {"beige", "tan", "cream", "ivory", "nude"} if allowed & {"tan", "cream", "beige", "brown"} else set()
    allowed |= {"grey", "gray", "charcoal"} if allowed & {"gray", "grey", "silver"} else set()
    if named - allowed:
        problems.append(f"color words not in facts: {sorted(named - allowed)}")
    if facts.flaws and not re.search(FLAW_WORDS, d, re.I):
        problems.append("facts list flaws but the description doesn't mention any")
    return problems
