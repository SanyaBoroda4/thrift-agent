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
  T.need = async (selectors, what, opts = {}) => {
    const el = await T.waitFor(selectors, opts);
    if (!el) throw new Error(`${what}: not on the page (${[].concat(selectors).join(" | ")})`);
    return el;
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

  // ---- typing: the native setter, so a framework-controlled input takes the value as if typed
  T.setValue = (el, value) => {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
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

  // ---- the brand: our name, or the shorter clean form ("J. Crew" for "J.Crew") — nothing looser (WO30)
  const QUALIFIERS = new Set(["factory", "outlet", "kids", "kid", "baby", "babies", "home", "collection", "sport",
    "sports", "junior", "juniors", "girls", "boys", "men", "mens", "women", "womens", "petite", "plus", "maternity",
    "studio", "essentials", "basics", "active", "golf", "beauty", "swim", "intimates", "lingerie", "accessories",
    "shoes", "vintage"]);
  const words = (s) => (String(s).normalize("NFKD").toLowerCase().replace(/&/g, " and ").replace(/'/g, "")
    .match(/[a-z0-9]+/g) || []);
  const key = (s) => words(s).join("");
  T.strictPick = (ours, options) => {
    const mine = new Set(words(ours));
    const usable = options.filter((o) => o && !words(o).some((w) => !mine.has(w) && QUALIFIERS.has(w)));
    const same = usable.find((o) => key(o) === key(ours));
    if (same) return { choice: same, guess: same === ours ? null : `brand set to '${same}' (from '${ours}')` };
    const base = words(ours).filter((w) => !QUALIFIERS.has(w)).join("");
    const plain = base && usable.find((o) => key(o) === base);
    if (plain) return { choice: plain, guess: `brand set to '${plain}' (from '${ours}')` };
    return { choice: null, guess: null };
  };

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
    if (pages.form && T.$(pages.form.selectors)) return "form";
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
      const message = String(e && e.message ? e.message : e).slice(0, 300);
      st.failed.push(`${name}: ${message}`);
      T.emit({ event: "step", job_id: st.job_id, name, ok: false, detail: message,
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

  // ---- one job on this page (the background worker sends it once the page is up)
  const STOP = ["login", "block", "captcha", "verify"];
  let go = null;
  T.run = async ({ job, selectors, photos, pace }) => {
    T.selectors = selectors;
    const p = pace ?? job.pace;
    if (typeof p === "number") { T.mode = "human"; T.speed = p; } else if (p) { T.mode = p === "human" ? "human" : "fast"; }
    const site = T.sites[job.site];
    const st = { job_id: job.job_id, failed: [], picked: {}, shown: {}, guesses: [], notes: [],
                 strict: job.mode === "publish" };
    T.jobId = job.job_id;
    const fail = (stage, kind, message) => T.emit({ event: "error", job_id: job.job_id, stage, page: kind, message });
    if (T.off.has(job.job_id)) return fail("cancelled", "unknown", "called off by the bridge");
    const page = await T.settled(job.site, job.mode === "find" ? 4000 : 15000);
    if (job.mode === "check_login") {
      return page === "form" ? T.emit({ event: "result", job_id: job.job_id, page }) : fail("check_login", kindOf(page), `${page} page`);
    }
    if (STOP.includes(page)) {
      await T.screenshot(page);
      return fail(job.mode === "after_publish" ? "after_publish" : "open", page, `${page} page`);
    }
    if (job.mode === "find") return site.find(job, st);
    if (job.mode === "after_publish" && page === "landing") {   // WO33: Vinted's member page after Upload
      const did = site.landed ? await site.landed(job, st) : "nothing to answer";
      await T.screenshot("landing");
      return fail("after_publish", "landing", `landed on ${location.href} (${did}): the shop check finds the listing`);
    }
    if (job.mode === "verify" || job.mode === "after_publish") {
      if (page !== "listing") {
        await T.screenshot(page);
        const shown = T.$$(["[role='alert']", "[class*='error' i]"]).filter(T.visible).map((e) => e.textContent.trim());
        return fail(job.mode, page, `not a listing page: ${location.href}${shown.length ? ` (${shown.slice(0, 3).join(" | ")})` : ""}`);
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
    if (job.mode === "probe") {                       // WO33: the take-down control looked for — never clicked
      if (page !== "listing") return fail("probe", "unknown", `not a listing page: ${location.href}`);
      return site.probe(job, st);
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
      await site.fill(job, (photos || []).map(T.fileOf), st);
    } catch (e) {
      await T.screenshot("form");
      return fail(st.failed.length ? st.failed[0].split(":")[0] : "fill", "form",
                  String(e && e.message ? e.message : e).slice(0, 300));
    }
    const seen = site.readBack(job, st);
    seen.submit_buttons = site.submitButtons().length;               // seen, never clicked in a dry run
    seen.fill_ms = Math.round(performance.now() - t0);
    T.emit({ event: "step", job_id: job.job_id, name: "submit_seen", ok: seen.submit_buttons === 1,
             detail: `${seen.submit_buttons} button(s)` });
    await T.screenshot("form");
    const report = { job_id: job.job_id, seen, failed: st.failed, guesses: st.guesses, notes: st.notes, shown: st.shown };
    if (job.mode === "dry_run") return T.emit({ event: "result", dry_run: true, ...report });
    if (T.off.has(job.job_id)) return fail("cancelled", "form", "called off by the bridge");
    T.emit({ event: "ready", ...report });
    const ok = await new Promise((resolve) => { go = resolve; });     // the bridge's go-ahead, after its diff
    go = null;
    if (!ok) return fail("cancelled", "form", "cancelled");
    await site.submit(job, st);                                       // exactly one click; the page then navigates
  };
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
          T.run(msg).catch((e) => T.emit({ event: "error", job_id: msg.job.job_id, stage: "script", page: "unknown",
                                           message: String(e && e.message ? e.message : e).slice(0, 300) }));
        }
        sendResponse({ ok: true });
      } else if (msg.type === "submit" || msg.type === "cancel") {
        const id = msg.job_id || T.jobId;
        if (msg.type === "cancel") T.off.add(id);
        const ok = !!go && id === T.jobId;
        if (ok) go(msg.type === "submit");
        sendResponse({ ok: ok || msg.type === "cancel" });
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
