"""WO52 §2 (the owner, 2026-11-01): a form step finds its field again when a site changes its form — live between Oct
27 and Nov 1 Vinted turned its dropdowns into buttons and moved its brand search, and Depop dropped its shipping radios.

Each fill step looks, in order: (1) a selector learned before (this file's store, used first), (2) the recorded
selector (ext/selectors.json, post/poshmark.py SEL), (3) the field's visible label or accessible name, (4) the model:
a compact snapshot of the form's controls (labels, roles, the visible text of buttons and options — never an input's
value: nothing personal leaves the page), each with an id, and ONE enum-constrained question — which id is the field
(or which option means our value)? The step then acts, and the read-back must show our value, or the step fails as
before. A fallback that worked becomes a learned selector for its site and field (with its date), sent with every job
and tried first; the private chat hears it ONCE ("Vinted's brand field moved — learned the new one").

**The model never presses Publish, Save or Delete (invariant 3):** a control that publishes, saves, uploads, posts,
lists, deletes, removes, submits, discards, boosts, bumps, deactivates, signs or logs out, or opens an address or a
payment — by its words, by being a submit button, by a link's address (NEVER_HREF), or by matching a site's recorded
publish / save / delete / never_click selectors (the page marks those `never`) — is taken out of the snapshot before
the question (`allowed`), and an answer naming one is refused (`pick`). Those controls stay recorded-and-verified
only, pressed by code alone."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, create_model

# WO54 (left from WO53: Depop's sell form shows the account's ship-from address with a "Remove" button that has no
# aria-label — the model could in principle have picked it): remove, delete, discard, deactivate, sign / log out,
# address and payment controls are never offered either (ext/content/common.js PRESS says the same words).
FORBIDDEN = re.compile(
    r"\b(?:publish(?:ed|ing)?|post(?:ing)?|upload|list(?:\s+(?:this|an?|another))?\s*item|list\s+another|list\s+it|"
    r"save(?:\s+(?:changes|draft))?|delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing)|submit|discard|buy(?:\s+now)?|boost|"
    r"bump|promote|mark\s+as\s+sold|hide|confirm|pay|payments?|checkout|deactivat(?:e|es|ed|ing)|"
    r"sign(?:\s*-?\s*)out|log(?:\s*-?\s*)out|addresse?s?)\b", re.I)
# … and a link whose address leads there (an icon with no words: the page marks it `never`, SNAPSHOT_JS / T.forbidden)
NEVER_HREF = r"log-?out|sign-?_?out|/settings|/account|payment|address|/wallet|/balance"
CLICKABLE = {"button", "a", "link", "menuitem"}
MAX_CANDIDATES = 150           # the snapshot's cap (the page sends at most this many, labels cut to 80 characters)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
STORE = "learned_selectors.json"


def forbidden(c: dict) -> bool:
    """A candidate the model may never be shown or answer with: a control that publishes, saves or deletes — by the
    page's own mark (`never`: a recorded publish / save / delete / never_click selector), by being a submit button, or by
    its words when it is something to press (a text box labelled "Post title" types, it presses nothing)."""
    if c.get("never"):
        return True
    tag, role, typ = (str(c.get(k) or "").lower() for k in ("tag", "role", "type"))
    if typ in ("submit", "image", "reset"):
        return True
    pressable = tag in CLICKABLE or role in CLICKABLE or (tag == "input" and typ in ("button",))
    words = " ".join(str(c.get(k) or "") for k in ("label", "text", "testid", "name"))
    return pressable and bool(FORBIDDEN.search(words))


def allowed(candidates: list[dict]) -> list[dict]:
    """The candidates the model may see: forbidden controls out, the cap kept."""
    return [c for c in candidates if c.get("id") and not forbidden(c)][:MAX_CANDIDATES]


class Pick(BaseModel):
    pick: str
    reason: str = ""


def _choice_model(ids: list[str]) -> type[BaseModel]:
    """The tool's answer: one of these ids or "none" (an enum the model can't step outside)."""
    return create_model("FieldPick", pick=(Literal[tuple([*ids, "none"])], ...), reason=(str, ""))


SYSTEM = ("You help a form-filling program find one field or option on a marketplace's sell form after the site "
          "changed its markup. You see a list of the form's controls (each with an id, its tag, role, label and visible "
          "text) and sometimes a screenshot. Answer with the id of the ONE control that is the field asked for — or, "
          "when asked for an option, the option that means the SAME value (a new wording of it, never a different "
          "value). Answer \"none\" when no control clearly is it. Never choose a button that publishes, posts, uploads, "
          "saves, lists, deletes, removes, submits, discards, deactivates, signs or logs out, or opens an address or a "
          "payment: those are not in the list and must never be guessed.")


def question(kind: str, site: str, field: str, want: str, candidates: list[dict]) -> str:
    what = (f"Which control is the {field!r} field?" + (f" (it should hold or open: {want})" if want else "")
            if kind == "element" else
            f"Which option of the {field!r} list means the same as {want!r}? (only a new wording of that value)")
    lines = [f"Site: {LABEL.get(site, site)}. {what}", "", "Controls:"]
    for c in candidates:
        parts = [f"id={c['id']}", str(c.get("tag") or "")]
        for k in ("role", "type", "testid", "name", "label", "text", "section"):
            v = str(c.get(k) or "").strip()
            if v:
                parts.append(f"{k}={v[:80]!r}")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def pick(ask, kind: str, site: str, field: str, want: str, candidates: list[dict], model: str,
         image: bytes | None = None) -> str | None:
    """The model's choice of a candidate id for this field (`kind` "element") or option ("option"), or None. `ask` is
    brain.llm.ask (a fake in the tests). Forbidden controls are never offered, and an answer naming one — or any id not
    offered — is refused."""
    offered = allowed(candidates)
    if not offered:
        return None
    ids = [str(c["id"]) for c in offered]
    content = []
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(image).decode()}})
    content.append({"type": "text", "text": question(kind, site, field, want, offered)})
    answer = ask(model, SYSTEM, content, _choice_model(ids), "pick_control",
                 "The id of the one control (or option) asked for, or none.", max_tokens=400)
    chosen = str(getattr(answer, "pick", "") or "")
    if chosen not in ids:                         # "none", or anything outside what was offered
        return None
    if forbidden(next(c for c in offered if str(c["id"]) == chosen)):
        return None                               # belt and braces: never a publish / save / delete control
    return chosen


# ---------------------------------------------------------------- the learned selectors

class Learned:
    """{site: {field: {"selectors": [...], "how": "label" | "ai", "at": iso, "said": bool}}} in one JSON file on the
    Mac (next to the database). A learned selector is tried first, before the recorded one."""

    def __init__(self, path: Path | None):
        self.path = path

    def _load(self) -> dict:
        if self.path is None:
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def for_site(self, site: str) -> dict[str, list[str]]:
        return {f: list(v.get("selectors") or []) for f, v in (self._load().get(site) or {}).items()
                if v.get("selectors")}

    def learn(self, site: str, field: str, selector: str | None, how: str) -> bool:
        """Keep it (the newest first); True the first time this field of this site was learned — the private line is
        said then only."""
        if not selector or self.path is None:
            return False
        data = self._load()
        mine = data.setdefault(site, {}).setdefault(field, {"selectors": [], "said": False})
        first = not mine.get("said")
        mine["selectors"] = [selector, *[x for x in mine.get("selectors") or [] if x != selector]][:3]
        mine["how"], mine["at"] = how, datetime.now(timezone.utc).isoformat(timespec="seconds")
        mine["said"] = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            return False
        return first


def store(s=None) -> Learned:
    """The learned selectors of these settings (the file beside the database). Without settings (a poster a test made
    bare): nothing learned, nothing kept — the machine's own disk is never read."""
    try:
        return Learned(s.path("db").parent / STORE) if s is not None else Learned(None)
    except Exception:  # noqa: BLE001 — settings without a database path
        return Learned(None)


def model_for(s=None) -> str:
    """The model that names a moved field (settings models.heal; Sonnet)."""
    try:
        return str((s.get("models.heal", None) if s is not None else None) or "claude-sonnet-5")
    except Exception:  # noqa: BLE001
        return "claude-sonnet-5"


def moved_line(site: str, field: str) -> str:
    name = re.sub(r"-input$", "", field).replace("_", " ")
    name = {"group": "category", "variants": "size", "shippingMethods": "package size"}.get(name, name)
    return f"🩹 {LABEL.get(site, site)}'s {name} field moved — learned the new one."


# The snapshot for Playwright pages (Poshmark): the same reading as the extension's T.snapshot — each control gets a
# data-thrift-ai id; labels, roles and the visible text of buttons and options, never an input's value.
SNAPSHOT_JS = r"""
(args) => {
  const [never, cap, neverHref] = args;
  const vis = (el) => { for (let n = el; n && n.nodeType === 1; n = n.parentElement) { const cs = getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden' || n.hidden) return false; } return true; };
  const neverEls = new Set();
  for (const s of never) { try { document.querySelectorAll(s).forEach((e) => neverEls.add(e)); } catch (e) {} }
  // WO54: a link to sign out, the account's settings, an address or a payment — whatever its words (an icon)
  const hrefRe = neverHref ? new RegExp(neverHref, 'i') : null;
  if (hrefRe) document.querySelectorAll('a[href]').forEach((a) => { if (hrefRe.test(a.getAttribute('href') || ''))
    neverEls.add(a); });
  const clean = (t) => String(t || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const labelOf = (el) => {
    const parts = [el.getAttribute('aria-label'), el.getAttribute('placeholder')];
    const by = el.getAttribute('aria-labelledby');
    if (by) for (const id of by.split(/\s+/)) parts.push(document.getElementById(id)?.textContent);
    if (el.id) { try { parts.push(document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent); } catch (e) {} }
    const lab = el.closest('label'); if (lab) parts.push(lab.textContent);
    return clean(parts.filter(Boolean).join(' · '));
  };
  const out = []; let n = 0;
  const sel = 'input:not([type=hidden]),textarea,select,button,a[href],[role=combobox],[role=button],'
    + '[role=option],[role=radio],[role=checkbox],[role=menuitem],[contenteditable=true]';
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el) || out.length >= cap) continue;
    const id = 'e' + (++n); el.setAttribute('data-thrift-ai', id);
    const tag = el.tagName.toLowerCase(), typ = (el.getAttribute('type') || '').toLowerCase();
    const typing = (tag === 'input' && !/^(button|submit|checkbox|radio|image|reset)$/.test(typ)) || tag === 'textarea';
    const testid = el.getAttribute('data-testid') || el.getAttribute('data-vv-name') || el.getAttribute('data-et-name') || '';
    out.push({ id, tag, role: el.getAttribute('role') || '', type: typ, testid,
      name: el.getAttribute('name') || '', label: labelOf(el), text: typing ? '' : clean(el.textContent),
      never: [...neverEls].some((x) => x === el || x.contains(el)) });
  }
  return out;
}
"""
STABLE_JS = r"""
(el) => {
  const q = (s) => { try { return document.querySelectorAll(s).length === 1 ? s : null; } catch (e) { return null; } };
  const tag = el.tagName.toLowerCase();
  for (const a of ['data-testid', 'data-vv-name', 'data-et-name', 'name', 'aria-label', 'placeholder']) {
    const v = el.getAttribute(a); if (v) { const s = q(`${tag}[${a}="${v.replace(/"/g, '\\"')}"]`); if (s) return s; }
  }
  if (el.id && !/\d{3,}/.test(el.id)) return q(`#${CSS.escape(el.id)}`);
  return null;
}
"""
