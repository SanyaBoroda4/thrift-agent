// Thrift crosslister (WO32, WO32b): what the Vinted and Depop content scripts share. They run in the extension's
// isolated world on the two sites' pages and fill the seller's own listing form: values through the inputs' native
// value setter (+ input / change events), real clicks on the real option elements, photos attached as Files. Every
// selector comes from selectors.json (sent with the job); nothing is guessed, nothing is read but the sell page and the
// listing page this job created.
//
// Speed (WO32b): "fast" (the default) sets each value in one go and waits only for the page — an element appearing, a
// list rendering, thumbnails loading — through a MutationObserver plus a light poll, never through chains of short
// timers: a hidden tab throttles timers to one a second, then to one a minute, and that stalled a live run. Fixed
// settles (≤ 150 ms, where React needs one) count MessageChannel round trips, which no throttling slows. "human" (the
// job's pace) keeps typing in chunks with pauses. Every step gives up after 20 s (photos: 120 s) with its name and a
// screenshot.
(function () {
  const T = (globalThis.Thrift = globalThis.Thrift || {});
  T.sites = T.sites || {};
  T.mode = T.mode ?? "fast";         // "fast" | "human" (the job's pace)
  T.speed = T.speed ?? 1;            // human mode's pauses, × this (the tests set it near 0)
  T.timeoutScale = T.timeoutScale ?? 1;
  T.off = T.off || new Set();        // the jobs the bridge called off — a cancel may come before its job: kept
  T.ran = T.ran || new Set();        // the jobs started on this page: one the worker asks again (no answer in 3 s) runs once

  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const fast = () => T.mode !== "human";

  // ---- time without timers: one MessageChannel round trip is a task, never throttled
  const channel = new MessageChannel();
  const waiting = [];
  channel.port1.onmessage = () => { const f = waiting.shift(); if (f) f(); };
  T.tick = () => new Promise((resolve) => { waiting.push(resolve); channel.port2.postMessage(0); });
  T.settle = async (ms) => {
    const end = performance.now() + ms;
    while (performance.now() < end) await T.tick();
  };
  // A fixed wait: fast mode settles at most 150 ms (React re-rendering); human mode waits it out (× speed).
  T.sleep = (ms) => (fast() ? T.settle(Math.min(ms, 150)) :
    new Promise((resolve) => setTimeout(resolve, Math.max(0, ms * T.speed))));
  T.pause = () => (fast() ? T.tick() : T.sleep(rand(250, 900)));
  T.norm = (s) => String(s ?? "").replace(/\s+/g, " ").trim().toLowerCase();

  T.$ = (selectors, root = document) => {
    for (const s of [].concat(selectors || [])) {
      try {
        const el = root.querySelector(s);
        if (el) return el;
      } catch (e) { /* a selector this browser can't parse: the next one */ }
    }
    return null;
  };
  T.$$ = (selectors, root = document) => {
    const out = [];
    for (const s of [].concat(selectors || [])) {
      try { out.push(...root.querySelectorAll(s)); } catch (e) { /* next */ }
    }
    return [...new Set(out)];
  };
  T.visible = (el) => {
    if (!el || !el.isConnected) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden" || n.hidden) return false;
    }
    return true;
  };

  // ---- waits on the page itself: true as soon as the DOM changes into it (MutationObserver), else at a light poll
  // (properties like an input's value or an image's size change without a mutation), else null at the timeout.
  T.until = (pred, timeout = 8000, { events = [] } = {}) => new Promise((resolve) => {
    let done = false, timer = null, poll = null, obs = null;
    const finish = (v) => {
      if (done) return;
      done = true;
      obs?.disconnect();
      clearTimeout(timer);
      clearTimeout(poll);
      for (const e of events) document.removeEventListener(e, check, true);
      resolve(v);
    };
    const value = () => { try { return pred(); } catch (e) { return null; } };
    const check = () => { if (!done) { const v = value(); if (v) finish(v); } };
    // The poll re-arms from a message task, never from its own timer: no timer chain for Chrome to throttle to once a
    // minute in a hidden tab.
    const tick = () => { check(); if (!done) T.tick().then(() => { if (!done) poll = setTimeout(tick, 100); }); };
    obs = new MutationObserver(check);
    obs.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true,
                                                        characterData: true });
    for (const e of events) document.addEventListener(e, check, true);
    poll = setTimeout(tick, 100);
    timer = setTimeout(() => finish(value() || null), timeout * T.timeoutScale);
    check();
  });
  // The page has stopped changing: no DOM mutation for `ms` (a framework done re-rendering), `max` at most.
  T.quiet = (ms = 600, max = 3000) => new Promise((resolve) => {
    let timer = null, cap = null;
    const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, ms * T.timeoutScale); });
    function done() { obs.disconnect(); clearTimeout(timer); clearTimeout(cap); resolve(); }
    obs.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true,
                                                        characterData: true });
    timer = setTimeout(done, ms * T.timeoutScale);
    cap = setTimeout(done, max * T.timeoutScale);
  });
  T.waitFor = (selectors, { timeout = 8000, visible = true, root = document } = {}) =>
    T.until(() => T.$$(selectors, root).find((e) => !visible || T.visible(e)), timeout);
  // ---- WO52 §2: a field found again when the site changed its form. In order: a selector learned before (the job's
  // `learned`, tried first), the recorded one, the field's visible label / accessible name, then the model (the page's
  // snapshot of its controls — labels, roles, the visible text of buttons and options, never an input's value — and ONE
  // question, answered by the Mac through the bridge). The model is never shown, and never answered with, a control that
  // publishes, saves or deletes (T.forbidden: the site's recorded submit / save / delete / never_click selectors, a
  // submit button, or such words on something to press). What worked is reported as "learned" (its stable selector),
  // kept on the Mac and sent with the next jobs. A step that still can't find its field fails as before.
  T.learned = T.learned || {};
  T.found = T.found || {};                     // field → the element a fallback found (the read-back reads it too)
  T.asks = T.asks || new Map();
  const LABELS = { title: /^(title|item title|listing title)\b/, description: /^(description|describe)/,
                   price: /^(price|listing price)\b/, sku: /^sku\b/, brand: /^brand\b/, category: /^category\b/,
                   size: /^size\b/, condition: /^condition\b/, color: /^colou?rs?\b/, colour: /^colou?rs?\b/,
                   material: /^materials?\b/, package: /^(package|parcel)( size)?\b/, skirt_length: /length\b/,
                   group: /^category\b/, variants: /^size\b/, source: /^source\b/, age: /^age\b/, style: /^style\b/,
                   shippingMethods: /^(package|parcel)( size)?\b/ };
  const HEALABLE = new Set(["title", "description", "price", "sku", "brand", "category", "size", "condition", "color",
                            "colour", "material", "package", "skirt_length", "group", "variants", "source", "age",
                            "style", "shippingMethods", "occasion"]);
  T.fieldKey = (what) => String(what || "").replace(/-input$/, "").replace(/^attributes\./, "");
  T.healable = (what) => HEALABLE.has(T.fieldKey(what)) || /^attributes\./.test(String(what || ""));
  const PRESS = /\b(publish(ed|ing)?|post(ing)?|upload|list (this |an? |another )?item|list another|list it|save( changes| draft)?|delete|remove listing|submit|discard|buy( now)?|boost|bump|promote|mark as sold|hide|confirm|pay|checkout)\b/i;
  const neverSelectors = () => {
    const steps = (T.selectors && T.site && T.selectors[T.site] && T.selectors[T.site].steps) || {};
    return ["submit", "edit_save", "delete_button", "delete_dialog", "never_click", "hide", "mark_sold", "list_item",
            "publish", "save_draft", "upload"].flatMap((k) => [].concat((steps[k] && steps[k].selectors) || []));
  };
  T.typeable = T.typeable || ((el) => !!el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT"
    && !/^(button|submit|checkbox|radio|file|image|reset|hidden)$/i.test(el.type || ""))));
  T.labelOf = (el) => {
    if (!el) return "";
    const parts = [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("data-placeholder")];
    const by = el.getAttribute("aria-labelledby");
    if (by) for (const id of by.split(/\s+/)) parts.push(document.getElementById(id)?.textContent);
    if (el.id) { try { parts.push(document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent); } catch (e) { /* */ } }
    const lab = el.closest("label");
    if (lab) parts.push(lab.textContent);
    // a label beside it in its own field box ("Category" over a combobox moved to another section)
    const box = el.parentElement && el.parentElement.closest("div, li, fieldset, section");
    const own = box && [...box.querySelectorAll("label, legend")].filter((l) => !l.contains(el));
    if (own && own.length === 1) parts.push(own[0].textContent);
    return parts.filter(Boolean).map((x) => String(x).replace(/\s+/g, " ").trim()).join(" · ").slice(0, 120);
  };
  const pressable = (el) => {
    const tag = el.tagName.toLowerCase(), role = (el.getAttribute("role") || "").toLowerCase();
    return tag === "button" || tag === "a" || role === "button" || role === "link" || role === "menuitem"
      || (tag === "input" && /^(button|submit|image|reset)$/i.test(el.type || ""));
  };
  T.forbidden = (el) => {
    if (!el) return true;
    for (const s of neverSelectors()) {
      try { if (el.matches(s) || el.querySelector(s) || el.closest(s)) return true; } catch (e) { /* */ }
    }
    if (/^(submit|image|reset)$/i.test(el.getAttribute("type") || "") && el.tagName !== "SELECT") return true;
    const words = [T.labelOf(el), pressable(el) ? el.textContent : "", el.getAttribute("data-testid") || "",
                   el.getAttribute("name") || ""].join(" ");
    return pressable(el) && PRESS.test(words);
  };
  const CONTROLS = "input:not([type=hidden]):not([type=file]),textarea,select,button,[role=combobox],[role=button],"
    + "[role=listbox],[role=radio],[role=checkbox],[role=option],[role=menuitem],[contenteditable=true]";
  // The form's controls as the model sees them: an id each (data-thrift-ai), labels, roles, the visible text of
  // buttons and options — never an input's value (nothing personal leaves the page); forbidden controls left out.
  T.snapshot = (els = null, cap = 150) => {
    const out = [];
    let n = 0;
    const list = els || T.$$([CONTROLS]).filter(T.visible);
    for (const el of list) {
      if (out.length >= cap || T.forbidden(el)) continue;
      const id = `e${++n}`;
      el.setAttribute("data-thrift-ai", id);
      const tag = el.tagName.toLowerCase();
      let section = "";
      for (let p = el.parentElement; p && !section; p = p.parentElement) {
        const h = p.querySelector?.("h1, h2, h3, h4, legend, [role=heading]");
        if (h && !h.contains(el)) section = h.textContent.replace(/\s+/g, " ").trim().slice(0, 40);
      }
      out.push({ id, tag, role: el.getAttribute("role") || "", type: (el.getAttribute("type") || "").toLowerCase(),
                 testid: el.getAttribute("data-testid") || "", name: el.getAttribute("name") || "",
                 label: T.labelOf(el), text: T.typeable(el) ? "" : (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60),
                 section });
    }
    return out;
  };
  // ONE question to the Mac (the bridge's "ask" → the driver → heal.pick → an "answer" message): the id picked, or null.
  T.ask = (kind, what, want, candidates, timeout = 90000) => new Promise((resolve) => {
    if (!candidates.length || !T.jobId || !T.canAsk) return resolve(null);   // the Mac has no model wired: no question
    const ask_id = `a${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const timer = setTimeout(() => { T.asks.delete(ask_id); resolve(null); }, timeout);
    T.asks.set(ask_id, (pick) => { clearTimeout(timer); T.asks.delete(ask_id); resolve(pick && pick !== "none" ? pick : null); });
    T.emit({ event: "ask", job_id: T.jobId, ask_id, kind, field: what, want: String(want || ""), candidates });
  });
  T.answer = (msg) => { const f = T.asks.get(msg.ask_id); if (f) f(msg.pick); };
  // A selector that names this element alone, from its own stable hooks (a test id, a name, a label) — else null.
  T.stableSelector = (el) => {
    const tag = el.tagName.toLowerCase();
    const one = (s) => { try { return document.querySelectorAll(s).length === 1 && document.querySelector(s) === el ? s : null; } catch (e) { return null; } };
    for (const a of ["data-testid", "name", "aria-label", "placeholder"]) {
      const v = el.getAttribute(a);
      if (v) { const s = one(`${tag}[${a}="${v.replace(/"/g, '\\"')}"]`); if (s) return s; }
    }
    if (el.id && !/\d{3,}|:/.test(el.id)) return one(`#${el.id}`);
    return null;
  };
  T.learn = (what, el, how) => {
    const selector = T.stableSelector(el);
    T.found[T.fieldKey(what)] = el;
    if (selector) T.learned[what] = [selector, ...(T.learned[what] || []).filter((x) => x !== selector)];
    T.emit({ event: "learned", job_id: T.jobId, field: what, selector, how });
    T.emit({ event: "progress", job_id: T.jobId, what: `${T.fieldKey(what)}: found by ${how === "ai" ? "the model" : "its label"}` });
  };
  // The field by its label: the one visible control whose label says the field's name.
  T.byLabel = (what) => {
    const rec = T.selectors && T.site && T.selectors[T.site]?.steps?.[what];
    const re = rec && rec.label ? new RegExp(rec.label, "i") : LABELS[T.fieldKey(what)];
    if (!re) return null;
    const hits = T.$$([CONTROLS]).filter((el) => T.visible(el) && !T.forbidden(el)
      && !/^(option|listbox|radio|checkbox|menuitem)$/i.test(el.getAttribute("role") || "")   // a field, not its list
      && re.test(T.norm(T.labelOf(el))));
    if (hits.length === 1) return hits[0];
    const typing = hits.filter(T.typeable);
    return typing.length === 1 && ["title", "description", "price", "sku"].includes(T.fieldKey(what)) ? typing[0] : null;
  };
  T.aiFind = async (what, want = "") => {
    const cands = T.snapshot();
    const pick = await T.ask("element", what, want, cands);
    const el = pick ? document.querySelector(`[data-thrift-ai="${pick}"]`) : null;
    return el && !T.forbidden(el) ? el : null;      // never a publish / save / delete control, whatever came back
  };
  // An option that means our value in the site's new wording: the model picks among `options` (elements) — only one
  // that IS our value reworded; the read-back takes it as ours (`st.reworded`). -1 when none.
  T.isReworded = (st, name, plan, shown) => ((st && st.reworded && st.reworded[name]) || [])
    .some((x) => T.norm(x.plan) === T.norm(plan) && T.norm(x.shown) === T.norm(shown));
  T.aiOption = async (st, name, value, options, textOf = null) => {
    const els = options.filter((o) => !T.forbidden(o));
    if (!els.length) return -1;
    const norm = (el) => T.norm(textOf ? textOf(el) : el.textContent);
    const known = ((st && st.reworded && st.reworded[name]) || []).find((x) => T.norm(x.plan) === T.norm(value));
    if (known) {                                       // the same value asked again: its wording is known, no new question
      const k = options.findIndex((o) => norm(o) === T.norm(known.shown));
      if (k >= 0) return k;
    }
    const cands = T.snapshot(els, 200);
    const pick = await T.ask("option", name, value, cands);
    const el = pick ? document.querySelector(`[data-thrift-ai="${pick}"]`) : null;
    const i = el ? options.indexOf(el) : -1;
    if (i >= 0 && st) {
      const shown = (textOf ? textOf(el) : el.textContent || "").replace(/\s+/g, " ").trim();
      ((st.reworded = st.reworded || {})[name] = st.reworded[name] || []).push({ plan: value, shown });
      st.notes.push(`${name}: '${shown}' taken for '${value}' (the site's new wording)`);
    }
    return i;
  };
  T.need = async (selectors, what, opts = {}) => {
    const mine = [].concat(T.learned[what] || []);
    const el = await T.waitFor([...mine, ...[].concat(selectors || [])], opts);
    if (el) return el;
    // there but hidden (a field that shows only after an earlier step — the category — took): nothing moved, no healing
    const present = T.$$([...mine, ...[].concat(selectors || [])]).length > 0;
    const healed = opts.heal !== false && !present ? await T.heal(what, opts.want) : null;
    if (healed) return healed;
    const err = new Error(`${what}: not on the page (${[].concat(selectors).join(" | ")})`);
    if (T.SiteChanged) Object.setPrototypeOf(err, T.SiteChanged.prototype);
    throw err;
  };
  // The recorded lookup failed: the label, then the model; learned and reported when found. null when neither.
  T.heal = async (what, want = "") => {
    if (!T.healable(what)) return null;
    const byLabel = T.byLabel(what);
    if (byLabel) { T.learn(what, byLabel, "label"); return byLabel; }
    const byAi = await T.aiFind(what, want);
    if (byAi) { T.learn(what, byAi, "ai"); return byAi; }
    return null;
  };
  // An element by its id — Depop's comboboxes are found by id — else the one a fallback found for it, else a learned
  // selector for it (the id renamed: WO52 §2).
  T.byId = (id) => {
    const el = document.getElementById(id);
    if (el) return el;
    const f = T.found[T.fieldKey(id)];
    if (f && f.isConnected && /-input$/.test(id)) return f;
    return T.learned[id] ? T.$(T.learned[id]) : null;
  };
  // The field's element now: a fallback's find, else the learned / recorded selector (the read-backs use it).
  T.field = (name, selectors) => {
    const f = T.found[T.fieldKey(name)];
    if (f && f.isConnected) return f;
    return T.$([...(T.learned[name] || []), ...[].concat(selectors || [])]);
  };
  // The photos as a person sees them: images that have loaded (naturalWidth > 0).
  T.loaded = (selectors) => T.$$(selectors).filter((img) => img.complete && img.naturalWidth > 0).length;
  T.waitLoaded = async (selectors, n, timeout) => {
    T.$(selectors)?.scrollIntoView?.({ block: "center" });
    await T.until(() => T.loaded(selectors) >= n, timeout, { events: ["load", "error"] });
    return T.loaded(selectors);
  };
  T.waitCount = async (selectors, n, timeout) => {
    await T.until(() => T.$$(selectors).length >= n, timeout);
    return T.$$(selectors).length;
  };

  // ---- WO51: a failure that is the page's (an element missing, the form changed) is a SiteChanged; one our own script
  // threw (TypeError, "Illegal invocation" …) is reported as code, with its file and line — the bridge then never
  // counts it against the item (live 2026-11-01: Vinted's brand step called the <input> value setter on a <button>)
  class SiteChanged extends Error {
    constructor(message) { super(message); this.name = "SiteChanged"; }
  }
  T.SiteChanged = SiteChanged;
  const CODE = /^(TypeError|ReferenceError|RangeError|SyntaxError|EvalError|URIError)$/;
  T.errInfo = (e) => {
    const name = (e && e.name) || "";
    const raw = String(e && e.message ? e.message : e).slice(0, 300);
    if (e instanceof SiteChanged || name === "SiteChanged") return { message: raw, kind: "site", where: null };
    if (!CODE.test(name)) return { message: raw, kind: null, where: null };
    const where = String((e && e.stack) || "").split("\n").slice(1, 4)
      .map((l) => l.trim().replace(/^at\s+/, "").replace(/chrome-extension:\/\/[^/]+\//g, "").replace(/\s*\(\s*\)$/, ""))
      .filter(Boolean).join(" ← ").slice(0, 300);
    return { message: `${name}: ${raw}${where ? ` (at ${where})` : ""}`, kind: "code", where };
  };
  // ---- WO52 §3: the form's structure — every control a person fills: its visible label, type, hook, required. The
  // same reading as STRUCTURE_JS in thrift_agent/post/base.py (Poshmark); thrift_agent.formcheck diffs it.
  T.structure = () => {
    const norm = (x) => String(x || "").replace(/\s+/g, " ").trim().slice(0, 60);
    const root = document.querySelector("form") || document.querySelector("main") || document.body;
    const sel = "input:not([type=hidden]):not([type=submit]), textarea, select, [role=combobox], "
      + "button[aria-haspopup], [role=radiogroup]";
    const labelOf = (el) => {
      const id = el.getAttribute("id");
      const lab = id && document.querySelector(`label[for="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
      const by = (el.getAttribute("aria-labelledby") || "").split(/\s+/).map((x) => document.getElementById(x))
        .filter(Boolean).map((x) => x.textContent).join(" ");
      let near = "";
      for (let p = el.parentElement, k = 0; p && k < 4 && !near; p = p.parentElement, k++) {
        const l = p.querySelector(":scope > label, :scope > legend, :scope > [class*=label i], :scope > h2, :scope > h3");
        if (l && !l.contains(el)) near = l.textContent;
      }
      return norm(el.getAttribute("aria-label") || (lab && lab.textContent) || by
                  || (el.closest("label") && el.closest("label").textContent) || near
                  || el.getAttribute("placeholder") || "");
    };
    const out = [], seen = new Set();
    try {
      for (const el of root.querySelectorAll(sel)) {
        if (el.closest("[role=option], [role=listbox], [role=menu]")) continue;
        const tag = el.tagName.toLowerCase(), role = el.getAttribute("role");
        const kind = role === "combobox" ? "combobox" : role === "radiogroup" ? "radio"
          : tag === "input" ? (["radio", "checkbox"].includes(el.type) ? el.type : `input:${el.type || "text"}`) : tag;
        const key = el.getAttribute("data-testid") || el.getAttribute("data-test") || el.getAttribute("data-vv-name")
          || el.getAttribute("data-et-name") || el.getAttribute("id") || el.getAttribute("name") || "";
        const label = labelOf(el);
        const required = !!el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label);
        const k = `${label}|${kind}|${key}`;
        if (seen.has(k) || (!label && !key)) continue;
        seen.add(k);
        out.push({ label, kind, key, required });
      }
    } catch (e) {
      return null;                                   // never the reason a dry run fails
    }
    return out;
  };
  // ---- typing: the native setter, so a framework-controlled input takes the value as if typed — on a text box only
  // (WO51: never a <button> or a <div>: the page changed, said as such instead of the browser's "Illegal invocation")
  T.typeable = (el) => !!el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT"
    && !/^(button|submit|checkbox|radio|file|image|reset|hidden)$/i.test(el.type || "")));
  T.setValue = (el, value) => {
    if (!T.typeable(el)) {
      const tid = el && el.getAttribute ? el.getAttribute("data-testid") : null;
      throw new SiteChanged(`can't type into a <${String((el && el.tagName) || "nothing").toLowerCase()}>`
                            + `${tid ? ` [${tid}]` : ""}: the page changed`);
    }
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  // WO51: a form field's value as the page shows it — an <input>'s own value, else (a custom dropdown, Vinted's form
  // since 2026-11: a <button> whose `<testid>--value` span draws the choice, data-placeholder while none) the text it
  // draws; never a button's .value (always "").
  T.fieldValue = (el) => {
    if (!el) return null;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return el.value;
    if (el.hasAttribute && el.hasAttribute("data-placeholder")) return "";
    const v = el.querySelector && el.querySelector("[data-testid$='--value'], .select-value");
    return ((v || el).textContent || "").replace(/\s+/g, " ").trim();
  };
  T.type = async (el, text) => {
    el.scrollIntoView?.({ block: "center" });
    el.focus?.();
    T.setValue(el, "");
    const s = String(text);
    if (fast()) {
      T.setValue(el, s);                                   // in one go
    } else {
      for (let i = 0; i < s.length;) {
        const n = 2 + Math.floor(Math.random() * 9);
        T.setValue(el, el.value + s.slice(i, i + n));
        i += n;
        await T.sleep(rand(40, 140));
      }
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };

  // ---- clicking: the pointer moves over the element, then the press, then the element's own click
  T.click = async (el) => {
    el.scrollIntoView?.({ block: "center" });
    if (!fast()) await T.sleep(rand(120, 320));
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0, width: 0, height: 0 };
    const at = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const Pointer = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Pointer("pointerover", at));
    el.dispatchEvent(new MouseEvent("mouseover", at));
    el.dispatchEvent(new Pointer("pointermove", at));
    el.dispatchEvent(new MouseEvent("mousemove", at));
    if (!fast()) await T.sleep(rand(60, 180));
    el.dispatchEvent(new Pointer("pointerdown", at));
    el.dispatchEvent(new MouseEvent("mousedown", at));
    el.focus?.();
    el.dispatchEvent(new Pointer("pointerup", at));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    el.click();
  };

  // ---- photos: Files from the bridge's bytes, all at once on the file input (else dropped on the drop zone)
  T.fileOf = ({ name, type, b64 }) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: type || "image/jpeg" });
  };
  T.attach = async (input, files, dropSelectors) => {
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    try {
      input.files = dt.files;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return "input";
    } catch (e) {
      const zone = T.$(dropSelectors);
      if (!zone) throw e;
      for (const t of ["dragenter", "dragover", "drop"]) {
        zone.dispatchEvent(new DragEvent(t, { bubbles: true, cancelable: true, dataTransfer: dt }));
      }
      return "drop";
    }
  };

  // ---- the site's own requests, with their HTTP statuses (the Vinted incident of 2026-10-20: a refusal came back
  // with no request at all — `performance.getEntriesByType` reads a 250-entry buffer a sell page fills before the
  // form is touched). A PerformanceObserver sees every entry from here on; the buffer is the fallback. Returns a
  // function giving the /api/ fetch / XHR calls made since, "<path> <status>", the last 8.
  T.watchRequests = () => {
    const t0 = performance.now();
    const got = [];
    let po = null;
    try {
      po = new PerformanceObserver((list) => { for (const e of list.getEntries()) got.push(e); });
      po.observe({ type: "resource", buffered: false });
    } catch (e) { po = null; }
    const ours = (e) => ["fetch", "xmlhttprequest"].includes(e.initiatorType) && /\/api\//.test(e.name);
    return () => {
      const entries = po ? got : (typeof performance?.getEntriesByType === "function"
        ? performance.getEntriesByType("resource").filter((e) => e.startTime >= t0) : []);
      return entries.filter(ours)
        .map((e) => { try { return `${new URL(e.name).pathname} ${e.responseStatus || "?"}`; } catch (x) { return null; } })
        .filter(Boolean).slice(-8);
    };
  };
  // A fresh page after Vinted's system error during the photo upload (the job is handed to it by background.js,
  // twice at most); the pause before it. Both are hooks so the tests see the reload instead of jsdom's "not implemented".
  T.photosRetryPauseMs = 25000;
  T.reloadPage = () => location.reload();

  // ---- the brand (WO36, the owner's rules of 2026-10-11 and 2026-10-12): our names in order — the brand as shops list
  // it, the label's text, the alias file's names, the two parts of a name like "MNG (Mango)" (the job's candidates,
  // brands.candidates) — each picked EXACTLY (case, spaces, punctuation and accents aside, "&" = "and"); else the ONE
  // option that is a whole name of ours followed by more words ("alo" → "Alo Yoga"; whole words: never "ALOHAS"). Never
  // the other way round ("Solid & Striped" never lands on "Solid"), never a similar name.
  // A company suffix is one word however the site writes it ("Thursday Boot Co" = "Thursday Boot Company": live
  // 2026-10-20, Poshmark's Brand left empty and Depop's "Other" for a brand both list; brands.py says the same).
  const SUFFIX = { company: "co", corporation: "corp", incorporated: "inc", limited: "ltd" };
  const words = (s) => (String(s ?? "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/&/g, " and ").replace(/['\u2019`]/g, "").match(/[a-z0-9]+/g) || []).map((w) => SUFFIX[w] || w);
  const key = (s) => words(s).join("");
  T.brandKey = key;
  T.brandNames = (f) => [...new Set([...(f.brand_candidates || []), f.brand].filter(Boolean))];
  T.strictPick = (ours, options) => {
    const same = options.find((o) => o && key(o) && key(o) === key(ours));
    if (same) return { choice: same, guess: same === ours ? null : `brand set to '${same}' (from '${ours}')` };
    return { choice: null, guess: null };
  };
  // `option` is the whole of `ours` followed by more words ("Alo" → "Alo Yoga")
  T.extendsName = (ours, option) => {
    const o = words(ours), x = words(option);
    return o.length > 0 && x.length > o.length && o.every((w, i) => x[i] === w);
  };
  // {choice, how: "exact" | "extends", from: the name of ours it came from}, or {choice: null}
  T.brandChoose = (names, options) => {
    const opts = [...new Set(options.filter((o) => o && key(o)))];
    for (const n of names) {
      const same = opts.find((o) => key(o) === key(n));
      if (same) return { choice: same, how: "exact", from: n };
    }
    for (const n of names) {
      const longer = [...new Map(opts.filter((o) => T.extendsName(n, o)).map((o) => [key(o), o])).values()];
      if (longer.length === 1) return { choice: longer[0], how: "extends", from: n };
    }
    return { choice: null };
  };
  // " (it offers: A, B, …)": the site's top suggestions, for the note that lets the alias be added
  T.offered = (options, limit = 6) => {
    const shown = [...new Set(options.filter((o) => o && key(o) && key(o) !== "other"))].slice(0, limit);
    return shown.length ? ` (it offers: ${shown.join(", ")})` : "";
  };
  // The note for a brand picked: nothing for our own name as written (case aside), else what it was set to
  T.brandNote = (value, choice) => (T.norm(choice) === T.norm(value) ? null : `brand set to '${choice}' (from '${value}')`);
  // What may be typed into a brand search: the name as written, then other spellings of the SAME name ("J.Crew" /
  // "J. Crew", "&" / "and") — never a word of it.
  T.spellings = (name) => [...new Set([name, String(name).replace(/\.\s+/g, "."), String(name).replace(/\.(?=[A-Za-z])/g, ". "),
                                       String(name).replace(/\s*&\s*/g, " and ")].map((v) => v.trim()).filter(Boolean))];

  // ---- what kind of page this is: block, captcha, login, verify (a check the site asks for), the form, or a listing
  T.textOf = () => {
    const body = document.body;
    let text = body ? body.innerText : "";
    if (body && text === undefined) {        // no layout (jsdom): the text without scripts, as innerText would be
      const copy = body.cloneNode(true);
      copy.querySelectorAll("script, style, noscript, template").forEach((n) => n.remove());
      text = copy.textContent;
    }
    return `${document.title}\n${(text || "").slice(0, 4000)}`;
  };
  T.classify = (site) => {
    const conf = T.selectors?.[site] || {};
    const pages = conf.pages || {};
    const text = T.textOf();
    for (const name of ["block", "captcha", "login", "verify"]) {
      const p = pages[name];
      if (!p) continue;
      if ((p.selectors && T.$(p.selectors)) || (p.text && new RegExp(p.text, "im").test(text))) return name;
    }
    if (conf.listing_url && new RegExp(conf.listing_url).test(location.href)) return "listing";
    if (conf.landing_url && new RegExp(conf.landing_url).test(location.href)) return "landing";   // WO33: after Upload
    // WO52 §2: the form also by a title learned since (its recorded hook renamed) or another hook of its own (also)
    if (pages.form && T.$([...[].concat(pages.form.selectors || []), ...(T.learned.title || []),
                           ...[].concat(pages.form.also || [])])) return "form";
    return "unknown";
  };
  T.settled = async (site, timeout = 15000) => {
    const page = await T.until(() => { const p = T.classify(site); return p === "unknown" ? null : p; }, timeout);
    return page || "unknown";
  };

  // ---- reporting to the bridge (through the background worker)
  T.emit = (event) => {
    try {
      const p = chrome.runtime.sendMessage({ type: "event", ...event });
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* the worker is restarting: the bridge times the job out */ }
  };
  // The visible tab as the background worker captures it, with the page's HTML beside it (the evidence the selectors
  // are recorded from, like the Playwright posters' failed/shots/*.html).
  T.screenshot = async (label) => {
    if (T.noShots) return;                         // WO38: an order page is never pictured (buyer names, addresses)
    const html = (document.documentElement?.outerHTML || "").slice(0, 3_000_000);
    try { await chrome.runtime.sendMessage({ type: "screenshot", label, html, url: location.href }); } catch (e) { /* best effort */ }
  };
  // One step: its event (with how long it took), and in a dry run a step that fails is recorded and the form goes on
  // (WO30's rule). A step with no progress for its limit (20 s; photos 120 s) ends the job, with its name and a
  // screenshot (WO32b: fail fast). A job the bridge called off stops at the next step, whatever the mode.
  class Stalled extends Error {}
  T.step = async (name, st, fn, { limit = 20000 } = {}) => {
    if (T.off.has(st.job_id)) throw new Error("called off by the bridge");
    const t0 = performance.now();
    let timer = null;
    const stalled = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Stalled(`${name}: nothing happened for ${Math.round(limit / 1000)} s`)),
                         Math.max(limit * T.timeoutScale, T.minStepMs || 0));   // minStepMs: the tests' floor
    });
    try {
      const detail = await Promise.race([fn(), stalled]);
      T.emit({ event: "step", job_id: st.job_id, name, ok: true, ms: Math.round(performance.now() - t0),
               ...(typeof detail === "string" ? { detail } : {}) });
    } catch (e) {
      const info = T.errInfo(e);                   // WO51: our own script's error named as such, with its line
      const message = info.message;
      st.failed.push(`${name}: ${message}`);
      if (info.kind && !st.errorKind) st.errorKind = info.kind;
      T.emit({ event: "step", job_id: st.job_id, name, ok: false, detail: message, kind: info.kind || undefined,
               ms: Math.round(performance.now() - t0) });
      if (e instanceof Stalled) {
        await T.screenshot(`stalled-${name.replace(/[^a-z0-9]+/gi, "-")}`);
        throw e;
      }
      if (st.strict) throw e;
    } finally {
      clearTimeout(timer);
    }
    await T.pause();
  };
  T.picked = (st, name, value, shown) => {
    (st.picked[name] = st.picked[name] || []).push(value);
    (st.shown[name] = st.shown[name] || []).push(shown);
  };
  T.dismiss = async (selectors) => {
    for (const b of T.$$(selectors).filter(T.visible)) {
      try { await T.click(b); } catch (e) { /* gone already */ }
    }
  };

  // ---- WO38: the sold orders, READ ONLY. Nothing on the page is clicked, typed or submitted; no picture and no HTML of
  // an order page ever leaves it (buyer names, addresses, tracking numbers): what leaves is scrubbed here first.
  // A key is cut into its words (camelCase and snake_case alike); a value under a word of DENY is "<scrubbed>" (its
  // whole subtree), a string is kept only under a word of SAFE (masked: @handles, e-mail addresses, long numbers).
  const DENY_WORDS = new Set(("buyer buyers seller sellers user users username usernames login email emails mail phone "
    + "address addresses street city zip zipcode postal postcode recipient recipients name names firstname lastname "
    + "fullname nickname tracking carrier photo photos avatar avatars picture pictures image images thumb thumbnail "
    + "thumbnails message messages body note notes comment comments feedback review reviews token tokens secret "
    + "password signature lat lng latitude longitude location geo ip").split(" "));
  const SAFE_WORDS = new Set(("id ids uuid status state statuses title price amount currency total subtotal fee fees "
    + "earning earnings payout date time at created updated paid sold shipped sent delivered completed cancelled "
    + "canceled refunded returned type kind count size brand sku slug url path href code transaction order item items "
    + "listing product variant quantity condition category val value step stage phase display text label").split(" "));
  const wordsOf = (key) => String(key).replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase().split(/[^a-z0-9]+/)
    .filter(Boolean);
  T.mask = (text) => String(text).replace(/[\w.+-]+@[\w-]+\.[\w.]+/g, "<email>").replace(/@[\w.-]+/g, "@<user>")
    .replace(/\d{15,}/g, "<long number>");
  T.scrub = (obj, key = "", depth = 0, maxList = 8) => {
    const words = key ? wordsOf(key) : [];
    const flag = /^(is|has|can)$/.test(words[0] || "") || /^(count|at|date)$/.test(words[words.length - 1] || "");
    if (words.some((w) => DENY_WORDS.has(w)) && !flag) {
      if (words.includes("label") && !words.some((w) => ["url", "pdf", "file", "link", "download"].includes(w))) {
        /* a label's state is a status ("label_printed"); its file is the buyer's address: scrubbed below */
      } else {
        return obj === null || obj === undefined || obj === "" ? obj : "<scrubbed>";
      }
    }
    if (words.includes("label") && words.some((w) => ["url", "pdf", "file", "link", "download"].includes(w))) {
      return obj === null || obj === undefined || obj === "" ? obj : "<scrubbed>";
    }
    if (depth > 12) return "<deep>";
    if (Array.isArray(obj)) {
      const out = obj.slice(0, maxList).map((v) => T.scrub(v, key, depth + 1, maxList));
      return obj.length > maxList ? [...out, `<${obj.length - maxList} more>`] : out;
    }
    if (obj && typeof obj === "object") {
      const out = {};
      for (const [k, v] of Object.entries(obj)) out[k] = T.scrub(v, k, depth + 1, maxList);
      return out;
    }
    if (typeof obj === "string") return words.some((w) => SAFE_WORDS.has(w)) ? T.mask(obj).slice(0, 120) : "<str>";
    return obj;
  };
  // The short texts a page shows an order is at ("Awaiting shipment", "Delivered", "Order complete") — kept only when
  // EVERY word is one of these (or a number, a price, a date): a person's name never passes.
  const ORDER_VOCAB = new Set(("a an the to be is was has have been on in out of for by at its it your you this that "
    + "and or not yet no all new view details more order orders ordered sold sale sales item items paid payment "
    + "received pending processing awaiting await waiting ship shipped shipping shipment sent send parcel package "
    + "label labels printed print generated ready transit delivered delivery deliver arrived complete completed "
    + "completion cancelled canceled cancel cancellation returned return returns refund refunded dispute disputed "
    + "issue reported open opened closed status tracking track way dropped off picked up confirm confirmed accept "
    + "accepted rate rated rating left day days hour hours minute minutes ago today tomorrow yesterday overdue late "
    + "buyer seller mark marked as expected estimated date due jan feb mar apr may jun jul aug sep sept oct nov dec "
    + "january february march april june july august september october november december mon tue wed thu fri sat "
    + "sun am pm est edt").split(" "));
  T.statusTexts = (texts, limit = 40) => {
    const out = [];
    for (const raw of texts) {
      const t = String(raw || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > limit || out.includes(t)) continue;
      const words = t.toLowerCase().split(/[\s,·•|()]+/).filter(Boolean);
      // a number is a price, a date, a count — 10 characters at most: a tracking number never passes
      const ok = words.every((w) => ORDER_VOCAB.has(w.replace(/[.:!?]+$/, "")) || /^[#$]?\d[\d,.:/-]{0,9}$/.test(w));
      if (ok && words.some((w) => !/^[#$]?\d/.test(w))) out.push(t);
    }
    return out;
  };
  // A request the page made that is safe to make again: a read of the site itself — never an action, an event, a
  // conversation or a label.
  const ACTION = /(?:^|[/_.?=&-])(mark|accept|decline|cancel|confirm|rate|rating|ratings|feedback|message|messages|conversation|conversations|inbox|offer|offers|label|labels|print|download|event|events|track|tracking|log|logs|metric|metrics|analytics|collect|beacon|pixel|consent|refresh|token|oauth|auth|login|logout|session|sessions|report|reports|ship|shipment)(?:$|[/_.?=&-])/i;
  T.readable = (url) => {
    try {
      const u = new URL(url, location.href);
      const host = location.hostname.split(".").slice(-2).join(".");
      return /^https:$/.test(u.protocol) && (u.hostname === host || u.hostname.endsWith(`.${host}`))
        && !ACTION.test(u.pathname + u.search);
    } catch (e) {
      return false;
    }
  };
  const pathShape = (url) => {
    try {
      const u = new URL(url, location.href);
      const q = [...u.searchParams.keys()].map((k) => `${k}=${T.mask(u.searchParams.get(k)).slice(0, 24)}`).join("&");
      return `${u.hostname}${u.pathname.replace(/\d{6,}/g, "<n>")}${q ? `?${q}` : ""}`;
    } catch (e) {
      return "<bad url>";
    }
  };
  // Two titles with most of their words in common (case and punctuation aside): a row shows ours, maybe cut.
  T.sameWords = (a, b, share = 0.6) => {
    const words = (x) => new Set(String(x || "").toLowerCase().match(/[a-z0-9]+/g) || []);
    const wa = words(a), wb = words(b);
    const both = [...wa].filter((w) => wb.has(w)).length;
    return wa.size > 0 && wb.size > 0 && both >= 2 && both >= share * Math.min(wa.size, wb.size);
  };
  // WO38 §1, the diagnostic: what the sold-orders page is made of — the JSON it loaded (each request read again, GET,
  // with the page's own cookies, scrubbed), the JSON it carries (Next.js data, JSON-LD), the status words it shows, its
  // data-testid names and the shapes of its links. Nothing else.
  T.orders = async (job) => {
    await T.until(() => document.readyState === "complete" || null, 15000);
    if (job.sweep) {                     // WO38 §2: the site's own reader (vinted.js / depop.js `orders`), our fields only
      try {
        const found = await T.sites[job.site].orders(job);
        return T.emit({ event: "result", job_id: job.job_id, orders: found.orders, source: found.source,
                        note: found.note || null });
      } catch (e) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "orders", page: "unknown",
                        message: String(e && e.message ? e.message : e).slice(0, 300) });
      }
    }
    await T.quiet(1500, 12000);
    const seen = new Set();
    const resources = [];
    const entries = performance.getEntriesByType("resource")
      .filter((e) => ["fetch", "xmlhttprequest"].includes(e.initiatorType)).map((e) => e.name);
    for (const url of entries) {
      const shape = pathShape(url);
      if (seen.has(shape)) continue;
      seen.add(shape);
      const entry = { url: shape };
      if (job.candidates || !T.readable(url) || resources.filter((r) => r.json !== undefined).length >= 12) {
        entry.read = false;
        resources.push(entry);
        continue;
      }
      try {
        const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
        entry.http = r.status;
        entry.type = (r.headers.get("content-type") || "").split(";")[0];
        if (r.ok && /json/.test(entry.type)) entry.json = T.scrub(await r.json());
      } catch (e) {
        entry.error = String(e && e.message ? e.message : e).slice(0, 120);
      }
      resources.push(entry);
    }
    const documents = [];
    for (const s of T.$$("script#__NEXT_DATA__, script[type='application/json'], script[type='application/ld+json']")) {
      try {
        documents.push({ id: s.id || null, type: s.type, json: T.scrub(JSON.parse(s.textContent || "null")) });
      } catch (e) {
        documents.push({ id: s.id || null, type: s.type, error: "not JSON" });
      }
      if (documents.length >= 6) break;
    }
    const leaves = [...document.querySelectorAll("body *")].filter((n) => !n.childElementCount && !n.closest("script, style"))
      .map((n) => n.textContent);
    const testids = [...new Set(T.$$("[data-testid]").map((n) => n.getAttribute("data-testid")))].slice(0, 300);
    const links = {};
    for (const a of T.$$("a[href]")) {
      const shape = pathShape(a.href).replace(/^[^/]*/, "");
      links[shape] = (links[shape] || 0) + 1;
    }
    // Probe 2: the addresses the job names (the site's own JSON of the orders), each read once with GET — on this
    // site only; Depop's API host wants the page's own sign-in token, which stays in this page (never sent anywhere
    // but to that host, never reported).
    const candidates = [];
    for (const url of (job.candidates || []).slice(0, 4)) {
      const entry = { url: pathShape(url) };
      try {
        const u = new URL(url, location.href);
        const host = location.hostname.split(".").slice(-2).join(".");
        if (u.protocol !== "https:" || !(u.hostname === host || u.hostname.endsWith(`.${host}`))) throw new Error("another site");
        const headers = { Accept: "application/json" };
        const token = (document.cookie.match(/(?:^|;\s*)access_token=([^;]+)/) || [])[1];
        if (u.hostname !== location.hostname && token) headers.Authorization = `Bearer ${decodeURIComponent(token)}`;
        entry.token = u.hostname !== location.hostname ? !!token : null;
        const r = await fetch(u.href, { credentials: "include", headers });
        entry.http = r.status;
        entry.type = (r.headers.get("content-type") || "").split(";")[0];
        if (r.ok && /json/.test(entry.type)) entry.json = T.scrub(await r.json(), "", 0, 12);
      } catch (e) {
        entry.error = String(e && e.message ? e.message : e).slice(0, 120);
      }
      candidates.push(entry);
    }
    // The order rows as drawn: an element that shows an order's status word, up to the nearest one with a link —
    // its links (long numbers masked), its status words, and its longest short text (the title; kept by the Mac only
    // when it is one of our titles).
    const STATUS_LEAF = /^(to ship|awaiting shipment|in transit|shipped|delivered|completed|order complete|order completed|canceled|cancelled|order cancelled|package delivered\.?|order shipped and on its way!?|processing|returned|refunded)$/i;
    const rows = [];
    const seenRows = new Set();
    for (const leaf of [...document.querySelectorAll("body *")].filter((n) => !n.childElementCount
           && STATUS_LEAF.test((n.textContent || "").trim()) && !n.closest("nav, header, [role=tablist], [role=tab], button"))) {
      let row = leaf.parentElement;
      while (row && row !== document.body && !row.querySelector("a[href]") && !row.closest("a[href]")) row = row.parentElement;
      if (row && row.closest("a[href]")) row = row.closest("a[href]");
      if (!row || row === document.body || seenRows.has(row)) continue;
      seenRows.add(row);
      const texts = [...row.querySelectorAll("*")].filter((n) => !n.childElementCount).map((n) => (n.textContent || "").trim())
        .filter(Boolean);
      const hrefs = [...new Set([row, ...row.querySelectorAll("a[href]")].filter((a) => a.href)
        .map((a) => pathShape(a.href).replace(/^[^/]*/, "")))];
      // the row's title only when it is one of ours (job.known): any other text could be a person's name
      const title = texts.find((x) => x.length <= 120 && (job.known || []).some((k) => T.sameWords(x, k))) || "";
      rows.push({ hrefs, status_texts: T.statusTexts(texts), title: title ? T.mask(title) : "<other>",
                  tag: row.tagName.toLowerCase(), testid: row.getAttribute("data-testid") || null });
      if (rows.length >= 15) break;
    }
    T.emit({ event: "result", job_id: job.job_id, url: pathShape(location.href), title: T.mask(document.title),
             resources, documents, status_texts: T.statusTexts(leaves), testids, candidates, rows,
             links: Object.fromEntries(Object.entries(links).sort((a, b) => b[1] - a[1]).slice(0, 60)) });
  };

  // ---- one job on this page (the background worker sends it once the page is up)
  const STOP = ["login", "block", "captcha", "verify"];
  let go = null;
  T.run = async ({ job, selectors, photos, pace }) => {
    T.selectors = selectors;
    const p = pace ?? job.pace;
    if (typeof p === "number") { T.mode = "human"; T.speed = p; } else if (p) { T.mode = p === "human" ? "human" : "fast"; }
    const site = T.sites[job.site];
    const st = { job_id: job.job_id, failed: [], picked: {}, shown: {}, guesses: [], notes: [], left: {},
                 strict: job.mode === "publish" };
    T.jobId = job.job_id;
    T.site = job.site;                               // WO52 §2: the field lookups heal on this site
    T.learned = { ...(job.learned || {}) };          // the selectors learned before, tried first
    T.found = {};
    T.canAsk = !!job.heal;                           // the bridge set it: this site's driver answers "ask"
    T.noShots = job.mode === "orders";
    const fail = (stage, kind, message) => T.emit({ event: "error", job_id: job.job_id, stage, page: kind, message });
    if (T.off.has(job.job_id)) return fail("cancelled", "unknown", "called off by the bridge");
    let page = await T.settled(job.site, job.mode === "find" || job.mode === "orders" ? 4000 : 15000);
    if (job.mode === "check_login") {
      return page === "form" ? T.emit({ event: "result", job_id: job.job_id, page }) : fail("check_login", kindOf(page), `${page} page`);
    }
    if (STOP.includes(page)) {
      await T.screenshot(page);
      return fail(job.mode === "after_publish" ? "after_publish" : "open", page, `${page} page`);
    }
    if (job.mode === "find") return site.find(job, st);
    if (job.mode === "orders") return T.orders(job, st);           // WO38: read only — nothing is clicked
    if (job.mode === "after_publish" && page === "landing") {   // WO33: Vinted's member page; WO35: Depop's success page
      const did = site.landed ? await site.landed(job, st) : "nothing to answer";
      await T.screenshot("landing");
      if (did && did.url) {                           // WO35: the new listing's address, from the page it landed on
        return T.emit({ event: "result", job_id: job.job_id, url: did.url, live: null, landing: location.href,
                        how: did.how || null });
      }
      return fail("after_publish", "landing", `landed on ${location.href} (${did}): the shop check finds the listing`);
    }
    if (job.mode === "verify" || job.mode === "after_publish") {
      if (page !== "listing") {
        // WO39: still the sell form after the click is a refusal — with every error the form showed since it
        if (job.mode === "after_publish" && page === "form") return T.refusal(fail);
        await T.screenshot(page);
        const shown = T.errorsShown();
        return fail(job.mode, page, `not a listing page: ${location.href}${shown.length ? ` (${shown.join(" | ")})` : ""}`);
      }
      await T.dismiss(T.selectors[job.site].steps.promo_close?.selectors);   // a bump / boost offer: closed, never taken
      const live = await site.verify(job);
      await T.screenshot(job.mode === "verify" ? "listing" : "after-publish");
      return T.emit({ event: "result", job_id: job.job_id, url: location.href, live });
    }
    if (job.mode === "delist") {
      if (page !== "listing") return fail("delist", "unknown", `not a listing page: ${location.href}`);
      return site.delist(job, st);
    }
    if (job.mode === "practice") {                    // WO33: Depop's delete window opened, then Cancel — never Delete
      if (page !== "listing" || !site.practice) return fail("practice", "unknown", `not a listing page: ${location.href}`);
      return site.practice(job, st);
    }
    if (job.mode === "delete") {                      // WO52 follow-up: Vinted's delete, its confirm text recorded first
      if (page !== "listing" || !site.remove) return fail("delete", "unknown", `not a listing page: ${location.href}`);
      return site.remove(job, st);
    }
    if (job.mode === "probe") {                       // WO33: the take-down control looked for — never clicked
      if (page !== "listing") return fail("probe", "unknown", `not a listing page: ${location.href}`);
      return site.probe(job, st);
    }
    if (page !== "form" && ["cover", "price", "fields", "photos", "text"].includes(job.mode)) {
      // WO43 (live 2026-10-18): Depop's edit page came up as a header and a footer with no form for 15 s — the form
      // is waited for a while longer before the page is called something else
      const late = await T.until(() => (T.classify(job.site) === "form" ? true : null), 30000);
      if (late) page = "form";
    }
    if (page !== "form") {
      await T.screenshot(page);
      return fail("open", "unknown", `not the sell form: ${location.href}`);
    }
    if (!fast()) await T.sleep(rand(2000, 5000));                     // human mode: a person looks at the page first
    if (document.visibilityState === "hidden") {      // the waits don't mind; the site's own page may not draw
      T.emit({ event: "progress", job_id: job.job_id,
               what: "the tab is hidden — is the Thrift Chrome window minimized? (behind other windows is fine)" });
    }
    const t0 = performance.now();
    try {
      if (job.mode === "cover") await T.coverFirst(job, st);          // WO37: the right photo first, nothing else
      else if (job.mode === "price") await T.priceSet(job, st);       // WO41: the price, nothing else
      else if (job.mode === "fields") await T.fieldsSet(job, st);     // WO43: the category and size, nothing else
      else if (job.mode === "photos") await T.photosAdd(job, st, (photos || []).map(T.fileOf));   // WO45 §6
      else if (job.mode === "text") await T.textSet(job, st);         // WO50: the title / description, nothing else
      else await site.fill(job, (photos || []).map(T.fileOf), st);
    } catch (e) {
      await T.screenshot("form");
      const info = T.errInfo(e);                   // WO51: "code" (our script) / "site" (the page changed) for the bridge
      return T.emit({ event: "error", job_id: job.job_id, stage: st.failed.length ? st.failed[0].split(":")[0] : "fill",
                      page: "form", message: info.message, kind: info.kind || st.errorKind || undefined });
    }
    const edit = ["cover", "price", "fields", "photos", "text"].includes(job.mode);
    const seen = job.mode === "cover" ? T.coverSeen(job, st) : job.mode === "price" ? T.priceSeen(job, st)
      : job.mode === "fields" ? T.fieldsSeen(job, st) : job.mode === "photos" ? T.photosSeen(job, st)
        : job.mode === "text" ? T.textSeen(job, st) : site.readBack(job, st);
    seen.submit_buttons = (edit ? T.saveButtons(job.site) : site.submitButtons()).length;
    seen.fill_ms = Math.round(performance.now() - t0);
    T.emit({ event: "step", job_id: job.job_id, name: "submit_seen", ok: seen.submit_buttons === 1,
             detail: `${seen.submit_buttons} button(s)` });
    await T.screenshot("form");
    const report = { job_id: job.job_id, seen, failed: st.failed, guesses: st.guesses, notes: st.notes, shown: st.shown };
    if (job.mode === "dry_run" || (edit && job.dry)) {
      if (job.mode === "dry_run") report.structure = T.structure();   // WO52 §3: the form check's diff
      return T.emit({ event: "result", dry_run: true, ...report });   // a cover dry run: moved on the page, never saved
    }
    if (T.off.has(job.job_id)) return fail("cancelled", "form", "called off by the bridge");
    T.emit({ event: "ready", ...report });
    const ok = await new Promise((resolve) => { go = resolve; });     // the bridge's go-ahead, after its diff
    go = null;
    if (!ok) return fail("cancelled", "form", "cancelled");
    if (edit) {
      try {
        await T.saveEdit(job, st);                                    // exactly one click on Save
      } catch (e) {
        await T.screenshot("form");
        return fail("save", "form", String(e && e.message ? e.message : e).slice(0, 300));
      }
    } else {
      T.watchAfterClick();                                            // WO39: from the click on, every error kept
      await site.submit(job, st);                                     // exactly one click; the page then navigates
    }
    // WO35: a form the site refused stays, with its error (live 2026-10-09: Depop "This field is required" under
    // Brand; Vinted "Fill in brand to continue"): said at once, with the error — nothing was published. WO39: also a
    // message that showed only for a moment (a snackbar), a field marked invalid anywhere on the form.
    // An error still on the page says it at once; one that came and went (a snackbar) is said with the rest when the
    // page is still the form minutes later (the after_publish check) — a page still uploading is never a refusal.
    const refused = await T.until(() => (T.classify(job.site) === "form" && T.errorsShown().length ? true : null), 8000);
    if (refused) {
      await T.settle(1500);
      if (T.classify(job.site) === "form" && T.errorsShown().length) return T.refusal(fail);
    }
  };
  // ---- WO39: a form the site refused (live 2026-10-16: Vinted kept the Sam Edelman's sell form twice, no error in
  // sight at the bottom). From the click on, every error the page shows is kept — an alert, a snackbar or toast that
  // goes away, a field marked aria-invalid anywhere on the form — and the site's own requests after the click with
  // their HTTP status (the page's resource timing: nothing is asked again). Said as "refused: …" with all of it.
  T.watchAfterClick = () => {
    const seen = { t0: performance.now(), errors: [], requests: T.watchRequests() };
    T.afterClick = seen;
    let queued = false;
    const grab = () => {
      queued = false;
      for (const e of T.errorsShown(12)) if (!seen.errors.includes(e)) seen.errors.push(e);
    };
    try {
      const mo = new MutationObserver(() => { if (!queued) { queued = true; Promise.resolve().then(grab); } });
      mo.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true,
                                             attributeFilter: ["aria-invalid", "class", "hidden", "style"] });
      setTimeout(() => mo.disconnect(), 240000);
    } catch (e) { /* no observer: the errors on the page when asked are still read */ }
  };
  T.requestsAfterClick = () => (T.afterClick?.requests ? T.afterClick.requests() : []);
  T.refusalErrors = () => {
    const now = T.errorsShown(12);
    return [...now, ...(T.afterClick?.errors || []).filter((e) => !now.includes(e))].slice(0, 6);
  };
  T.refusal = async (fail) => {
    const errors = T.refusalErrors();
    const first = T.firstError();                    // the picture shows the error, wherever it is on the form
    if (first) first.scrollIntoView?.({ block: "center" }); else window.scrollTo?.(0, 0);
    await T.screenshot("refused");
    const asked = T.requestsAfterClick();
    return fail("after_publish", "form", `refused: ${errors.length ? errors.join(" | ") : "no error shown on the form"}`
                + (asked.length ? ` (its requests after the click: ${asked.join(", ")})` : ""));
  };
  // The errors a form shows: alerts and error texts that say something (an empty error wrapper — Depop's red icon —
  // is no message), with the field each belongs to when its label is known.
  // ---- WO37: a live listing's cover fixed on its edit page (the owner, 2026-10-12: "put the right photo first and
  // change nothing else"). Both sites' photo grids are dnd-kit sortables (recorded on their edit pages 2026-10-12:
  // [aria-roledescription=sortable] per photo, Depop's "Cover" / Vinted's "Main" badge on the first; Vinted's alt text:
  // "To reorder photos, press the space bar"). The photo is focused, picked up with Space, moved with the arrow keys —
  // up to the first row, then left — and dropped with Space; the order is read back, again up to three rounds.
  const KEY_CODES = { " ": "Space", ArrowLeft: "ArrowLeft", ArrowUp: "ArrowUp" };
  const press = (el, key) => {
    const at = { key, code: KEY_CODES[key] || key, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent("keydown", at));
    el.dispatchEvent(new KeyboardEvent("keyup", at));
  };
  const sameList = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  T.editTiles = (site) => T.$$(T.selectors[site].steps.edit_photos.selectors).filter(T.visible);
  T.tileSrc = (tile) => tile.querySelector("img")?.getAttribute("src") || "";
  T.saveButtons = (site) => T.$$(T.selectors[site].steps.edit_save.selectors).filter(T.visible);
  T.coverFirst = async (job, st) => {
    const order = () => T.editTiles(job.site).map(T.tileSrc);
    await T.until(() => (order().length >= job.count && order().every(Boolean) ? true : null), 20000);
    const before = order();
    st.coverBefore = before;
    st.coverAfter = before;
    if (before.length !== job.count) {
      throw new Error(`the listing shows ${before.length} photos, not the ${job.count} posted: changed since — left as it is`);
    }
    if (!(job.from > 0 && job.from < before.length)) throw new Error(`no photo ${job.from + 1} to move first`);
    const target = before[job.from];
    const want = [target, ...before.filter((_, i) => i !== job.from)];
    for (let round = 0; round < 3 && !sameList(order(), want); round++) {
      const tiles = T.editTiles(job.site);
      const tile = tiles[order().indexOf(target)];
      const r = tile.getBoundingClientRect();
      const rects = tiles.map((t) => t.getBoundingClientRect());
      const up = [...new Set(rects.map((x) => Math.round(x.top)))].filter((top) => top < r.top - 4).length;
      const left = rects.filter((x) => Math.abs(x.top - r.top) < 5 && x.left < r.left - 4).length;
      tile.scrollIntoView?.({ block: "center" });
      // dnd-kit marks the dragged sortable aria-pressed; its keyboard handle may be an element inside it (live,
      // 2026-10-13: Vinted didn't pick the photo up from the sortable itself) — tried in turn until a drag starts
      const dragging = () => [tile, ...tile.querySelectorAll("[aria-pressed]")]
        .some((n) => n.getAttribute("aria-pressed") === "true");
      let handle = tile;
      for (const h of [tile, ...tile.querySelectorAll("[tabindex]:not([tabindex='-1'])")]) {
        h.focus();
        await T.settle(150);
        press(h, " ");                                                // picked up
        await T.settle(300);
        handle = h;
        if (dragging()) break;
      }
      for (let k = 0; k < up; k++) { press(document.activeElement || handle, "ArrowUp"); await T.settle(200); }
      for (let k = 0; k < left; k++) { press(document.activeElement || handle, "ArrowLeft"); await T.settle(200); }
      press(document.activeElement || handle, " ");                   // dropped
      await T.until(() => (sameList(order(), want) ? true : null), 3000);
      T.emit({ event: "step", job_id: job.job_id, name: "cover_move", ok: sameList(order(), want),
               detail: `round ${round + 1}: ${up} up, ${left} left` });
    }
    st.coverAfter = order();
    if (!sameList(st.coverAfter, want)) {
      throw new Error(`photo ${job.from + 1} didn't get to the front (the order now: ${
        st.coverAfter.map((x) => before.indexOf(x) + 1).join(", ")}): nothing saved`);
    }
  };
  T.coverSeen = (job, st) => ({ cover_before: st.coverBefore || [], cover_after: st.coverAfter || [],
                                save_enabled: T.saveButtons(job.site).some((b) => !b.disabled) });
  T.saveEdit = async (job, st) => {
    const save = await T.until(() => T.saveButtons(job.site).find((b) => !b.disabled) || null, 5000);
    if (!save) throw new Error("the edit page's Save stayed disabled: nothing saved");
    st.notes.push("Save pressed once");
    await T.click(save);
  };

  // ---- WO41: a price drop on the listing's edit page — the price field read (it must be the price the bridge read
  // on the site: else nothing is typed), Depop's fields its edit page left blank filled from the listing's own (live
  // 2026-10-13: the Tory loafers' edit page came up without category, brand, condition and colour, and Save was
  // refused), then the new price typed and read back. Nothing else on the listing is touched.
  T.money = (v) => {
    const m = String(v ?? "").replace(/,/g, "").match(/\d+(?:\.\d+)?/);
    return m ? Number(m[0]) : null;
  };
  T.priceSet = async (job, st) => {
    const box = await T.need(T.selectors[job.site].steps.price.selectors, "price");
    await T.until(() => (T.money(box.value) != null ? true : null), 10000);
    st.priceBefore = box.value;
    if (job.expect != null && T.money(box.value) !== Number(job.expect)) {
      st.priceMismatch = true;
      st.notes.push(`the edit page shows ${box.value}, not ${job.expect}: nothing typed`);
      return;
    }
    const site = T.sites[job.site];
    if (site.refill) await site.refill(job, st);
    await T.step("price", st, async () => T.type(box, String(job.price)));
    await T.settle(300);
    st.priceAfter = box.value;
  };
  T.priceSeen = (job, st) => ({ price_before: st.priceBefore ?? null, price: st.priceAfter ?? null,
                                mismatch: !!st.priceMismatch, refilled: st.refilled || [] });

  // ---- WO50: a listing's title and/or description changed on its edit page (the owner's shop-window titles): the page
  // must still show what we posted (`expect_title` / `expect_first_line` / `expect_description`: a text she changed by
  // hand is never overwritten
  // — nothing typed), Depop's fields its edit page left blank filled first (as the price job, WO41), the new text typed
  // and read back; a `read_only` job only reads (the check after Save). Save is the bridge's go-ahead; nothing else on
  // the listing is touched. Depop has no title: its description's first line is the title.
  T.textSet = async (job, st) => {
    const steps = T.selectors[job.site].steps;
    const squash = (x) => String(x || "").split(/\s+/).join(" ").trim();
    const titleBox = steps.title && (job.title != null || job.expect_title != null || job.read_only)
      ? await T.need(steps.title.selectors, "title") : null;
    const descBox = job.description != null || job.expect_first_line != null || job.expect_description != null
      || job.read_only
      ? await T.need(steps.description.selectors, "description") : null;
    await T.until(() => ((titleBox ? titleBox.value : descBox && descBox.value) ? true : null), 10000);
    st.titleBefore = titleBox ? titleBox.value : null;
    st.descBefore = descBox ? descBox.value : null;
    const first = (v) => squash(String(v || "").split("\n")[0]);
    if ((job.expect_title != null && titleBox && squash(titleBox.value) !== squash(job.expect_title))
        || (job.expect_first_line != null && descBox && first(descBox.value) !== squash(job.expect_first_line))
        || (job.expect_description != null && descBox && squash(descBox.value) !== squash(job.expect_description))) {
      st.textMismatch = true;
      st.notes.push("the page shows another text than we posted: nothing typed (changed by hand?)");
      return;
    }
    if (job.read_only) {
      st.titleAfter = st.titleBefore;
      st.descAfter = st.descBefore;
      return;
    }
    const site = T.sites[job.site];
    if (site.refill) await site.refill(job, st);
    if (titleBox && job.title != null) await T.step("title", st, async () => T.type(titleBox, job.title));
    if (descBox && job.description != null) await T.step("description", st, async () => T.type(descBox, job.description));
    await T.settle(300);
    st.titleAfter = titleBox ? titleBox.value : null;
    st.descAfter = descBox ? descBox.value : null;
  };
  T.textSeen = (job, st) => ({ title_before: st.titleBefore ?? null, title: st.titleAfter ?? null,
                               description_before: st.descBefore ?? null, description: st.descAfter ?? null,
                               mismatch: !!st.textMismatch, read_only: !!job.read_only, refilled: st.refilled || [] });

  // ---- WO43: a listing's category and size changed on its edit page (live 2026-10-17: men's sneakers listed under
  // Women): the site's own category and size steps, as the sell form's, on the values the bridge sends; a
  // `read_only` job reads what the page shows and changes nothing (the check after Save). Nothing else is touched.
  T.fieldsSet = async (job, st) => {
    const site = T.sites[job.site];
    await T.quiet(600, 4000);
    st.fieldsBefore = site.fieldsSeen(job, st);
    if (!job.read_only) await site.setFields(job, st);
    await T.settle(300);
    st.fieldsAfter = site.fieldsSeen(job, st);
  };
  T.fieldsSeen = (job, st) => ({ ...(st.fieldsAfter || {}), before: st.fieldsBefore || null, read_only: !!job.read_only });

  // ---- WO45 §6: photos added to a live listing on its edit page (live 2026-10-18: the Ralph Lauren shirt went up on
  // Vinted and Depop with one photo of four). The page's photo tiles are counted (they must be the `had` recorded, else
  // nothing is touched), the files set on the page's photo input (the same input as the sell form's: `photos`
  // selectors), the tiles counted again until they show had + the files; a `read_only` job only counts. Save is the
  // bridge's go-ahead, as for every edit; nothing else on the listing is touched.
  T.photosAdd = async (job, st, files) => {
    const tiles = () => T.editTiles(job.site).length;
    await T.until(() => (tiles() >= 1 ? true : null), 20000);
    st.photosBefore = tiles();
    st.photosAfter = st.photosBefore;
    if (job.read_only) return;
    if (!files.length) throw new Error("no photos to add");
    if (job.had != null && st.photosBefore !== Number(job.had)) {
      throw new Error(`the listing shows ${st.photosBefore} photos, not the ${job.had} recorded: changed since — left as it is`);
    }
    const sel = T.selectors[job.site].steps.photos;
    const input = await T.need(sel.selectors, "photos", { visible: false });
    await T.step("photos_add", st, async () => {
      st.attached = await T.attach(input, files, sel.drop);
      const want = st.photosBefore + files.length;
      await T.until(() => (tiles() >= want ? true : null), 90000);
      await T.quiet(600, 4000);
      st.photosAfter = tiles();
      return `${st.photosAfter} tiles (${st.photosBefore} + ${files.length})`;
    }, { limit: 120000 });
    if (st.photosAfter < st.photosBefore + files.length) {
      throw new Error(`the edit page shows ${st.photosAfter} photos after adding ${files.length} to ${st.photosBefore}: nothing saved`);
    }
  };
  T.photosSeen = (job, st) => ({ photos_before: st.photosBefore ?? null, photos: st.photosAfter ?? null,
                                 attached: st.attached || null, read_only: !!job.read_only,
                                 save_enabled: T.saveButtons(job.site).some((b) => !b.disabled) });

  // WO35: a listing's title as the site keeps it, the same as ours (spaces aside — the exact words, the exact case).
  T.sameTitle = (a, b) => String(a ?? "").replace(/\s+/g, " ").trim() === String(b ?? "").replace(/\s+/g, " ").trim();
  // WO39: also a snackbar / toast / notification that says something, an error a test id names, and a field marked
  // aria-invalid anywhere on the form (by its label: "Price: invalid"); never a badge (Vinted's "warning" badges).
  const ERROR_SEL = ["[role='alert']", "[class*='error' i]", "[class*='validation' i]", "[class*='snackbar' i]",
                     "[class*='toast' i]", "[data-testid*='error' i]", "[data-testid*='snackbar' i]",
                     "[data-testid*='toast' i]"];   // never aria-live: dnd-kit's photo grid announces there
  const labelOf = (el) => {
    const id = el.getAttribute?.("id");
    const byFor = id ? document.querySelector(`label[for='${CSS?.escape ? CSS.escape(id) : id}']`) : null;
    const byAria = (el.getAttribute?.("aria-labelledby") || "").split(/\s+/).map((x) => x && document.getElementById(x))
      .find(Boolean);
    return (byFor || byAria || el.closest?.("label"))?.textContent?.replace(/\s+/g, " ").trim()
      || el.getAttribute?.("name") || el.getAttribute?.("placeholder") || "a field";
  };
  T.errorEls = () => T.$$(ERROR_SEL).filter((e) => T.visible(e) && !/badge/i.test(e.className || ""));
  T.errorsShown = (max = 3) => {
    const out = [];
    for (const e of T.errorEls()) {
      const text = (e.textContent || "").replace(/\s+/g, " ").trim();
      if (!text || text.length > 160 || out.some((o) => o.includes(text))) continue;
      const field = e.closest("[class*='field' i], [class*='input' i], label, li")
        ?.querySelector("label, [class*='title' i]")?.textContent?.replace(/\s+/g, " ").trim();
      out.push(field && field !== text && field.length < 40 ? `${field}: ${text}` : text);
    }
    for (const e of T.$$(["[aria-invalid='true']"]).filter(T.visible)) {
      const said = `${labelOf(e)}: marked invalid`;
      if (!out.some((o) => o.startsWith(`${labelOf(e)}:`))) out.push(said);
    }
    return out.slice(0, max);
  };
  T.firstError = () => T.errorEls().find((e) => (e.textContent || "").trim())
    || T.$$(["[aria-invalid='true']"]).find(T.visible) || null;
  const kindOf = (page) => (STOP.includes(page) ? page : "unknown");

  // Once per page: the manifest injects this at document_idle, and the worker injects it again when the page doesn't
  // answer (an idle that is late: WO32b) — the second copy must not answer a job a second time.
  if (globalThis.chrome && chrome.runtime && chrome.runtime.onMessage && !T.listening) {
    T.listening = true;
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (msg.type === "job") {
        const id = `${msg.job.job_id}:${msg.job.mode}`;
        if (!T.ran.has(id)) {
          T.ran.add(id);
          T.run(msg).catch((e) => {
            const info = T.errInfo(e);
            T.emit({ event: "error", job_id: msg.job.job_id, stage: "script", page: "unknown", message: info.message,
                     kind: info.kind || "code" });
          });
        }
        sendResponse({ ok: true });
      } else if (msg.type === "submit" || msg.type === "cancel") {
        const id = msg.job_id || T.jobId;
        if (msg.type === "cancel") T.off.add(id);
        const ok = !!go && id === T.jobId;
        if (ok) go(msg.type === "submit");
        sendResponse({ ok: ok || msg.type === "cancel" });
      } else if (msg.type === "answer") {               // WO52 §2: the Mac's answer to a field the page asked about
        T.answer(msg);
        sendResponse({ ok: true });
      } else if (msg.type === "ping") {                 // the worker's watchdog: still here
        sendResponse({ ok: true });
      } else if (msg.type === "classify") {
        T.selectors = msg.selectors;
        sendResponse({ page: T.classify(msg.site), url: location.href });
      }
      return false;
    });
    try {
      const p = chrome.runtime.sendMessage({ type: "page", url: location.href });
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* no worker yet */ }
  }
})();
