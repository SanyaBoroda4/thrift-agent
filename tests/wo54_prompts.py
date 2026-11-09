"""WO54 §2's scan of the agent's own words (tests/test_wo54_decide.py): every string literal in thrift_agent/ that asks
the owner something or prompts a reply / a tap — found by its words, never by where it is sent — so the test can hold
it against the one list of what may still wait for a person. Pure: reads the source files of this checkout only."""
from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "thrift_agent"

# A prompt: "reply …", "tap …", "type a number" / "Type LIST", "swipe the card", "answer …" as something to do.
PROMPT = re.compile(r"\b(?:reply|replies|tap|swipe)\b|\btype\s+(?:a|one|it|list|post)\b|\banswer\s+(?:the|that|it)\b",
                    re.I)
# A question: a sentence ending in "?" (a word, a quote or a bracket before it; a space, a bracket or the end after it).
QUESTION = re.compile(r"[A-Za-z0-9’'”\")…]\?(?=\s|$|\)|[\"”])")
SQL = re.compile(r"\b(?:SELECT|INSERT INTO|UPDATE \w+ SET|DELETE FROM|WHERE)\b")
REGEX_CALLS = {"compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall", "finditer"}
MODEL_NAMES = {"SYSTEM", "UPRIGHT_SYSTEM", "LABEL_SYSTEM", "ATTACH_SYSTEM", "CALL_THE_TOOL", "REMINDER"}


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{…}" for v in node.values)
    return ""


def _skipped(tree: ast.AST) -> set[int]:
    """Nodes that are never words to the owner: docstrings, a pydantic Field's description, a regex pattern, the
    model's own prompts (the module's SYSTEM texts, llm.text(...) parts), and the parts of an f-string (read whole)."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.JoinedStr):
            skip |= {id(v) for v in node.values}
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ""
            if owner == "re" and name in REGEX_CALLS and node.args:
                skip.add(id(node.args[0]))
            if name == "Field" or (owner == "llm" and name == "text"):
                for sub in ast.walk(node):
                    skip.add(id(sub))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in MODEL_NAMES for t in node.targets):
            for sub in ast.walk(node.value):
                skip.add(id(sub))
    return skip


def scan(source: str, rel: str = "<source>") -> list[tuple[str, int, str]]:
    """(rel, line, the string with "{…}" for an f-string's values) of every literal in `source` that prompts a reply
    or a tap, or asks a question."""
    tree = ast.parse(source)
    skip = _skipped(tree)
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _text(node)
        if len(text) < 6 or " " not in text.strip() or SQL.search(text):
            continue
        if PROMPT.search(text) or QUESTION.search(text):
            out.append((rel, getattr(node, "lineno", 0), text))
    return out


def flagged() -> list[tuple[str, int, str]]:
    """`scan` over every module of thrift_agent/ (paths relative to it)."""
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        out += scan(path.read_text(encoding="utf-8"), path.relative_to(PACKAGE).as_posix())
    return out


if __name__ == "__main__":                                      # a look at what the scan finds
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for rel, line, text in flagged():
        print(f"{rel}:{line}: {text[:160]!r}")
