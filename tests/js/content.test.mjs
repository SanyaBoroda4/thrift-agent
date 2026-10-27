// WO32: the Thrift crosslister's content scripts (ext/content/*.js) in jsdom, on stand-in copies of the two forms built
// from the first live dry run's recording (tests/fixtures/ext_vinted_new_item.html, ext_depop_create.html): text typed
// through the native setter, dropdowns picked by real clicks on their options (Vinted's category tree walked row by row,
// Depop's category under the item's department heading, its brand menu answered a moment later, its package size),
// photos attached as Files to a hidden input, colours ≤ 2, Boost turned off, a dry run that never submits, a publish
// that clicks exactly once after the go-ahead, and the login / block / CAPTCHA pages that stop before anything is filled.
// Run: npm ci --prefix tests/js && node --test tests/js/content.test.mjs (pytest runs it: tests/test_ext_js.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");
const SELECTORS = JSON.parse(read("ext/selectors.json"));
const SCRIPTS = { vinted: ["ext/content/common.js", "ext/content/vinted.js"],
                  depop: ["ext/content/common.js", "ext/content/depop.js"] };
const URLS = { vinted: "https://www.vinted.com/items/new", depop: "https://www.depop.com/products/create/" };
const FIXTURES = { vinted: "tests/fixtures/ext_vinted_new_item.html", depop: "tests/fixtures/ext_depop_create.html" };
const plain = (x) => JSON.parse(JSON.stringify(x));   // values from the page's realm, compared as data
const PHOTO = { name: "photo-1.jpg", type: "image/jpeg", b64: Buffer.from("\xff\xd8\xff photo").toString("base64") };

// A page as Chrome would hold it: the site's HTML, then our two content scripts in it, chrome.runtime faked.
function page(site, html = read(FIXTURES[site]), url = URLS[site], fixture = {}) {
  const vc = new VirtualConsole();          // the stand-ins navigate after Post / Upload: jsdom can't, and says so
  const dom = new JSDOM(html.replace("<head>", `<head><script>window.__FIXTURE = ${JSON.stringify(fixture)}</script>`),
                        { url, runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  // What Chrome has and jsdom lacks: DataTransfer and a settable input.files.
  w.DataTransfer = class {
    constructor() { this.list = []; this.items = { add: (f) => this.list.push(f) }; }
    get files() { return this.list; }
  };
  Object.defineProperty(w.HTMLInputElement.prototype, "files", {
    configurable: true, get() { return this._files || []; }, set(v) { this._files = v; } });
  // jsdom loads no images: a photo "displays" unless the test says the site's images are broken.
  Object.defineProperty(w.HTMLImageElement.prototype, "naturalWidth", {
    configurable: true, get() { return w.__brokenImages ? 0 : 100; } });
  Object.defineProperty(w.HTMLImageElement.prototype, "complete", { configurable: true, get() { return true; } });
  // jsdom has no MessageChannel (the fast waits' tick): one on Node's setImmediate, which never keeps the run alive.
  w.MessageChannel = class {
    constructor() {
      const a = { onmessage: null }, b = { onmessage: null };
      a.postMessage = (data) => setImmediate(() => b.onmessage?.({ data }));
      b.postMessage = (data) => setImmediate(() => a.onmessage?.({ data }));
      this.port1 = a;
      this.port2 = b;
    }
  };
  const fetches = [];
  w.fetch = (u) => { fetches.push(String(u)); return Promise.resolve({ ok: true, json: async () => ({}) }); };
  const sent = [];
  const listeners = [];
  w.chrome = { runtime: { sendMessage: (m) => { sent.push(m); return Promise.resolve({ ok: true }); },
                          onMessage: { addListener: (fn) => listeners.push(fn) } } };
  for (const f of SCRIPTS[site]) w.eval(read(f));
  w.Thrift.speed = 0;                       // human mode's pauses: none in a test
  w.Thrift.timeoutScale = 0.05;
  w.Thrift.minStepMs = 5000;                // a step's no-progress limit (20 s × 0.05 = 1 s) at least 5 s: a loaded
                                            // Windows CI runner took longer than 1 s over a step (WO33, two flakes)
  const events = () => sent.filter((m) => m.type === "event");
  const last = () => events().at(-1);
  const tell = (msg) => listeners.forEach((fn) => fn(msg, {}, () => {}));
  return { w, d: w.document, fetches, sent, events, last, tell, listeners };
}

async function until(cond, ms = 8000) {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error("timed out");
    await new Promise((r) => setTimeout(r, 20));
  }
}

const VINTED_JOB = {
  job_id: "vinted-1", site: "vinted", mode: "dry_run", price: 35,
  fields: { category_id: 1071, category_path: "Women > Clothing > Pants & leggings > Wide-leg pants", brand: "J. Crew",
            size: "S / US 4-6", condition: "New without tags", colors: ["Cream", "Red", "White"], materials: ["Wool"],
            skirt_length: null, package_sizes: ["MEDIUM", "LARGE"] },
  copy: { title: "J. Crew Wide Leg Sweater Pants Cream size S", description: "Wide leg pants.\nNew without tags." },
};
const DEPOP_JOB = {
  job_id: "depop-1", site: "depop", mode: "dry_run", price: 35,
  fields: { category: "Women > Bottoms > Pants", brand: "J. Crew", brand_typed: "J. Crew", size: "S", sku: "i_261001_abc123",
            condition: "Like new", colors: ["Cream"], source: ["Preloved"], age: "Modern", style: [],
            attributes: { material: ["Wool"] }, shipping: "Depop Shipping", package_size: "Large" },
  copy: { title: "J. Crew Wide Leg Sweater Pants Cream size S",
          description: "J. Crew Wide Leg Sweater Pants Cream size S\n\nWide leg pants.\n\n#jcrew" },
};
const DEPOP_FIX = { sizeLag: 150, brandDelay: 100 };

const run = (p, job, pace = "fast") => p.w.Thrift.run({ job, selectors: SELECTORS, photos: [PHOTO], pace });

test("Vinted: a dry run fills every field the way a person would and never submits", async () => {
  const p = page("vinted");
  const title = p.d.querySelector("[data-testid='title--input']");
  // A React-style value tracker on the input: a plain `el.value = x` would update it and the framework would see no
  // change; the native setter leaves it behind, so every input event is a change.
  let tracked = "";
  const proto = Object.getOwnPropertyDescriptor(p.w.HTMLInputElement.prototype, "value");
  Object.defineProperty(title, "value", { configurable: true, get() { return proto.get.call(this); },
                                          set(v) { tracked = v; proto.set.call(this, v); } });
  const changes = { input: 0, change: 0, seen: 0 };
  title.addEventListener("input", () => { changes.input++; if (title.value !== tracked) changes.seen++; });
  title.addEventListener("change", () => changes.change++);
  await run(p, VINTED_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.equal(result.dry_run, true);
  assert.deepEqual(plain(result.failed), []);
  const seen = plain(result.seen);
  assert.equal(seen.title, VINTED_JOB.copy.title);
  assert.equal(seen.description, VINTED_JOB.copy.description);
  assert.equal(seen.price, "35");
  assert.equal(seen.photos, "1");                                      // the hidden input took the file
  assert.equal(seen.photos_loaded, "1");
  assert.deepEqual(seen.category, [VINTED_JOB.fields.category_path]);
  assert.deepEqual(seen.brand, ["J. Crew"]);
  assert.equal(seen.brand_shown, "J.Crew");
  assert.deepEqual(seen.size, ["S / US 4-6"]);                         // the form's own values
  assert.deepEqual(seen.condition, ["New without tags"]);
  assert.deepEqual(seen.color, ["Cream", "Red"]);                     // colours: at most 2
  assert.deepEqual(seen.material, ["Wool"]);
  assert.equal(seen.package, "MEDIUM");                                // Vinted's own Recommended one, ours too
  assert.equal(seen.submit_buttons, 1);
  assert.deepEqual(plain(result.guesses), ["brand set to 'J.Crew' (from 'J. Crew')"]);   // Vinted's own spelling
  assert.ok(changes.input >= 2 && changes.seen === changes.input - 1 && changes.change === 1, JSON.stringify(changes));
  assert.equal(p.d.querySelector("[data-testid='catalog-select-dropdown-input']").value, "Wide-leg pants");
  assert.equal(p.d.querySelector("#package-size-2").getAttribute("role"), "presentation");
  assert.equal(p.d.querySelector("#material-content").innerHTML, "");  // no panel left open over Upload
  assert.equal(p.d.querySelector("#color-content").innerHTML, "");
  assert.equal(p.d.querySelectorAll("[data-testid$='-chevron-up']").length, 0);
  assert.deepEqual(plain(p.fetches), []);                              // Upload never pressed
  const steps = p.events().filter((e) => e.event === "step").map((e) => e.name);
  assert.deepEqual(steps.slice(0, 4), ["photos", "title", "description", "category"]);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "photos"));
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "photos-end"));
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "form" && m.html.includes("upload-form-save")));
});

test("Vinted: our package size over the one Vinted recommends; a colour chosen already is left as it is", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, package_sizes: ["LARGE"], colors: ["Cream"] } });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(plain(r.seen).package, "LARGE");
  assert.equal(p.d.querySelector("#package-size-3").getAttribute("role"), "presentation");
  const q = page("vinted");
  q.d.querySelector("#color").value = "Cream";                         // chosen already: a click would unchoose it
  await run(q, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, colors: ["Cream"] } });
  assert.deepEqual(plain(q.last().seen).color, ["Cream"]);
});

test("Vinted: photos that never display are read back as such", async () => {
  const p = page("vinted");
  p.w.__brokenImages = true;
  await run(p, VINTED_JOB);
  const seen = plain(p.last().seen);
  assert.equal(seen.photos, "1");
  assert.equal(seen.photos_loaded, "0");                               // the Python diff fails the dry run on it
});

test("Vinted: a category row that isn't there fails the step with the open list on record", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, category_id: 9, category_path: "Women > Gowns > Ball" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.match(plain(r.failed)[0], /^category: no 'Gowns' \(offered: Clothing, Shoes\)/);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "menu-gowns" && m.html.includes("category-list")));
});

test("Vinted: a publish waits for the go-ahead and clicks Upload exactly once", async () => {
  const p = page("vinted");
  const job = { ...VINTED_JOB, job_id: "vinted-2", mode: "publish" };
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  assert.deepEqual(plain(p.fetches), []);                              // filled, not submitted
  p.tell({ type: "submit", job_id: job.job_id });
  await done;
  p.tell({ type: "submit", job_id: job.job_id });                      // a second go-ahead: nothing
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(plain(p.fetches), ["/api/upload-click"]);
  assert.ok(p.events().some((e) => e.event === "step" && e.name === "submit" && e.clicked));
});

test("Vinted: a form kept after Upload is a refusal with every error it showed, even one that went away (WO39)", async () => {
  // live 2026-10-16, the Sam Edelman twice: Upload left /items/new, nothing in sight at the bottom of the form
  const p = page("vinted");
  p.w.Thrift.sites.vinted.fill = async () => {};
  const job = { ...VINTED_JOB, job_id: "vinted-kept", mode: "publish" };
  const upload = p.d.querySelector("[data-testid='upload-form-save-button']");
  let clicks = 0;
  upload.addEventListener("click", () => {
    clicks++;
    setTimeout(() => {                                              // a snackbar at the top, gone a moment later
      const bar = p.d.createElement("div");
      bar.className = "web_ui__Snackbar__content";
      bar.textContent = "Something went wrong. Please try again.";
      p.d.body.prepend(bar);
      setTimeout(() => bar.remove(), 30);
      p.d.querySelector("[data-testid='title--input']").setAttribute("aria-invalid", "true");   // high up the form
    }, 5);
  });
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  p.tell({ type: "submit", job_id: job.job_id });
  await done;
  await new Promise((r) => setTimeout(r, 80));
  await run(p, { ...job, mode: "after_publish" });                // the background's look minutes later: still the form
  const end = plain(p.last());
  assert.equal(end.event, "error");
  assert.equal(end.stage, "after_publish");
  assert.equal(end.page, "form");
  assert.match(end.message, /^refused: .*Something went wrong\. Please try again\./);
  assert.match(end.message, /: marked invalid/);
  assert.equal(clicks, 1);                                          // never a second click
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "refused"));
});

test("Vinted: a cancelled publish never clicks", async () => {
  const p = page("vinted");
  const done = run(p, { ...VINTED_JOB, job_id: "vinted-3", mode: "publish" });
  await until(() => p.events().some((e) => e.event === "ready"));
  p.tell({ type: "cancel", job_id: "vinted-3" });
  await done;
  assert.deepEqual(plain(p.fetches), []);
  assert.equal(p.last().event, "error");
  assert.equal(p.last().stage, "cancelled");
});

test("a job the bridge calls off stops at the next step, whatever the mode", async () => {
  const p = page("vinted");
  const done = run(p, { ...VINTED_JOB, job_id: "vinted-4" });
  await until(() => p.events().some((e) => e.event === "step" && e.name === "title"));
  p.tell({ type: "cancel", job_id: "vinted-4" });
  await done;
  assert.equal(p.last().event, "error");
  assert.match(p.last().message, /called off by the bridge/);
  assert.deepEqual(plain(p.fetches), []);
});

test("a cancel that reaches the page before its job is kept: the job ends at once, nothing filled", async () => {
  const p = page("vinted");
  p.tell({ type: "cancel", job_id: "vinted-early" });                 // the bridge gave up while the tab was opening
  await run(p, { ...VINTED_JOB, job_id: "vinted-early" });
  assert.equal(p.last().event, "error");
  assert.match(p.last().message, /called off by the bridge/);
  assert.equal(p.d.querySelector("[data-testid='title--input']").value, "");
  assert.deepEqual(plain(p.fetches), []);
  await run(p, { ...VINTED_JOB, job_id: "vinted-next" });              // another job on the page is not affected
  assert.equal(p.last().event, "result");
});

test("fast mode (the default) sets a text field in one go; human mode types it in chunks", async () => {
  for (const pace of ["fast", "human"]) {
    const p = page("vinted");
    const title = p.d.querySelector("[data-testid='title--input']");
    const values = [];
    title.addEventListener("input", () => values.push(title.value));
    await run(p, { ...VINTED_JOB, job_id: `vinted-${pace}` }, pace);
    assert.equal(p.last().event, "result", JSON.stringify(p.last()));
    const typed = values.filter(Boolean);
    if (pace === "fast") assert.deepEqual(typed, [VINTED_JOB.copy.title]);
    else assert.ok(typed.length > 3 && typed.at(-1) === VINTED_JOB.copy.title, JSON.stringify(typed));
    assert.equal(typeof p.last().seen.fill_ms, "number");             // the fill's time, for the report
    const steps = p.events().filter((e) => e.event === "step");
    assert.ok(steps.filter((e) => e.name !== "submit_seen").every((e) => typeof e.ms === "number"));   // each step's time
    assert.match(steps.find((e) => e.name === "photos").detail, /^1\/1 shown$/);
  }
});

test("a step with no progress for its limit (20 s) fails, with its name and a screenshot", async () => {
  const p = page("vinted");
  p.w.Thrift.timeoutScale = 0.01;                        // 20 s → 0.2 s
  p.w.Thrift.minStepMs = 0;                              // no floor: the stall itself is under test
  p.w.Thrift.click = () => new Promise(() => {});        // a click that never comes back: the category step stalls
  const t0 = Date.now();
  await run(p, { ...VINTED_JOB, job_id: "vinted-stall" });
  const err = p.last();
  assert.equal(err.event, "error", JSON.stringify(err));
  assert.equal(err.stage, "category");
  assert.match(err.message, /category: nothing happened for 20 s/);
  assert.ok(Date.now() - t0 < 5000);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "stalled-category"));
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "form"));
  assert.deepEqual(plain(p.fetches), []);                // nothing submitted
});

// WO32b §1: Chrome runs a hidden tab's timers at most once a second (once a minute after 5 minutes) — the live stall.
// Every timer of the page and of our scripts made to wait at least 1 s: the fill still takes seconds, because it waits
// on the page itself (MutationObserver) and settles on message ticks, never on chains of short timers. (The pacing
// before WO32b — typing in chunks, pauses between steps, polls — needed well over a minute here.)
for (const site of ["vinted", "depop"]) {
  test(`${site}: a hidden tab's throttled timers (every one ≥ 1 s) don't slow the fill`, async () => {
    const p = page(site, undefined, undefined, site === "depop" ? DEPOP_FIX : {});
    p.w.Thrift.timeoutScale = 1;                       // the real limits (20 s a step): a timer of 1 s is no stall
    const real = p.w.setTimeout.bind(p.w);
    p.w.setTimeout = (fn, ms, ...args) => real(fn, Math.max(1000, Number(ms) || 0), ...args);
    const t0 = Date.now();
    await run(p, { ...(site === "vinted" ? VINTED_JOB : DEPOP_JOB), job_id: `${site}-hidden` });
    assert.equal(p.last().event, "result", JSON.stringify(p.last()));
    assert.deepEqual(plain(p.last().failed), []);
    assert.ok(Date.now() - t0 < 8000, `${Date.now() - t0} ms`);
  });
}

test("the worker's second injection (a page whose idle came late) never answers a job twice", async () => {
  const p = page("vinted");
  for (const f of SCRIPTS.vinted) p.w.eval(read(f));                 // the scripts again, in the same page
  assert.equal(p.listeners.length, 1);
  p.tell({ type: "job", job: { ...VINTED_JOB, job_id: "vinted-twice" }, selectors: SELECTORS, photos: [PHOTO],
           pace: "fast" });
  await until(() => p.events().some((e) => e.event === "result"));
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(p.events().filter((e) => e.event === "result").length, 1);
});

test("waits are on the page: an element that appears later is seen at once, without a timer chain", async () => {
  const p = page("vinted");
  const T = p.w.Thrift;
  setTimeout(() => { const el = p.d.createElement("div"); el.id = "late"; p.d.body.append(el); }, 30);
  const t0 = Date.now();
  const el = await T.waitFor(["#late"], { timeout: 5000 });
  assert.ok(el && el.id === "late" && Date.now() - t0 < 1000);
  assert.equal(await T.until(() => false, 50), null);    // nothing: null at the timeout
  const s0 = Date.now();
  await T.sleep(5000);                                   // fast mode: a fixed wait is a settle of 150 ms at most
  assert.ok(Date.now() - s0 < 1000);
});

test("Depop: the category under the item's department, the lagging size menu, the package size, Depop's own fill-ins tidied", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  p.d.querySelector("input[data-testid='switch']").checked = true;     // Boost on by itself: we never leave it on
  p.d.querySelector("#manual__shipping").checked = true;               // and Depop Shipping is what we use
  await run(p, DEPOP_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.deepEqual(plain(result.failed), []);
  const seen = plain(result.seen);
  assert.equal(seen.description, DEPOP_JOB.copy.description);
  assert.deepEqual(seen["group-input"], ["Women > Bottoms > Pants"]);
  assert.equal(p.w.__picked.category, "Women > Bottoms > Pants");      // the page's own record: Women's, not Men's
  assert.deepEqual(plain(result.shown)["group-input"], ["Women > Bottoms › Pants"]);
  assert.deepEqual(seen["variants-input"], ["S"]);
  assert.equal(seen["brand-input"], "J.Crew");                         // Depop's own fill-in was ours: kept
  assert.ok(plain(result.guesses).includes("brand set to 'J.Crew' (from 'J. Crew')"));
  assert.deepEqual(seen["condition-input"], ["Like new"]);
  assert.deepEqual(seen["colour-input"], ["Cream"]);                   // its Grey removed, its Cream kept (not toggled)
  assert.deepEqual(plain(p.w.__chosen["colour-input"]), ["Cream"]);
  assert.deepEqual(seen["source-input"], ["Preloved"]);
  assert.deepEqual(seen["age-input"], ["Modern"]);
  assert.deepEqual(seen["style-input"], []);
  assert.deepEqual(seen["attributes.material-input"], ["Wool"]);
  assert.equal(seen.package, "Large");                                 // not its Medium
  assert.equal(seen.shipping, "Depop Shipping");
  assert.equal(p.d.querySelector("#usps__shipping").checked, true);
  assert.equal(seen.boost, false);
  assert.equal(seen.boost_found, 1);
  assert.equal(seen.price, "35");
  assert.equal(seen.sku, "i_261001_abc123");                           // WO33 A4: our item id, in Depop's SKU
  assert.equal(seen.photos, "1");                                      // the tile, not the other Depop images
  assert.equal(seen.photos_loaded, "1");
  assert.equal(seen.submit_buttons, 1);
  const notes = plain(result.notes);
  assert.ok(notes.includes("Boost was on: turned off"));
  assert.ok(notes.includes("removed 'Grey' from colour-input (Depop's own suggestion)"), JSON.stringify(notes));
  assert.ok(notes.some((n) => n.startsWith("category menu: Men > Bottoms › Pants, Women > Bottoms › Pants")));
  assert.deepEqual(plain(p.fetches), []);
});

test("Depop: its own suggestions coming late are waited for, then ours go over them (live: the package)", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100, aiDelay: 2000 });
  p.w.Thrift.timeoutScale = 1;                          // the real waits: Depop's suggestions 2 s after the photos
  await run(p, DEPOP_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.deepEqual(plain(result.failed), []);
  assert.match(result.seen.package, /^Large/);          // ours, not Depop's "Medium"
  assert.deepEqual(plain(result.seen["colour-input"]), ["Cream"]);
  const s = p.events().find((e) => e.event === "step" && e.name === "suggestions");
  assert.equal(s.detail, "Depop's own came");
});

test("Depop: a package size Depop changes after ours is set back before the read-back", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100, ai: false });
  const send = p.w.chrome.runtime.sendMessage;
  p.w.chrome.runtime.sendMessage = (m) => {
    if (m.type === "event" && m.event === "step" && m.name === "package") {
      p.d.getElementById("shippingMethods-input").value = "Medium";     // Depop's own, landing just after ours
    }
    return send(m);
  };
  await run(p, DEPOP_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.match(result.seen.package, /^Large/);
  assert.ok(result.notes.some((n) => /changed the package size to 'Medium' \(below Large\): set back/.test(n)), result.notes);
});

test("Depop: a colour Depop puts back after the tidy is taken out again before the read-back (WO37)", async () => {
  // Live 2026-10-12 (the lululemon jacket, the UGG Tazz): Depop re-added its own colour after our tidy, and the form
  // check stopped the listing ("picked ['tan'], ['Grey', 'Tan']").
  const p = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100 });
  p.w.Thrift.timeoutScale = 1;                          // the tidy's real wait for the page to settle
  const send = p.w.chrome.runtime.sendMessage;
  p.w.chrome.runtime.sendMessage = (m) => {
    if (m.type === "event" && m.event === "step" && m.name === "package") {
      const watch = setInterval(() => {                 // once the tidy has taken Depop's Grey out...
        if (!(p.w.__chosen["colour-input"] || []).includes("Grey")) {
          clearInterval(watch);
          setTimeout(() => p.w.__readd("colour-input", "Grey"), 300);   // ... Depop puts it back
        }
      }, 20);
    }
    return send(m);
  };
  await run(p, DEPOP_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.deepEqual(plain(result.failed), []);
  assert.deepEqual(plain(result.seen["colour-input"]), ["Cream"]);
  assert.ok(result.notes.includes("Depop changed the form again after the tidy: set back once more"),
    JSON.stringify(result.notes));
});

test("Depop: the brand typed as Depop spells it when our spelling finds nothing", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  await run(p, DEPOP_JOB);
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(plain(r.seen)["brand-input"], "J.Crew");
  assert.equal(p.w.__picked["brand-input"], "J.Crew");                 // "J. Crew" found only Other; "J.Crew" found it
  assert.ok(plain(r.guesses).includes("brand set to 'J.Crew' (from 'J. Crew')"));
});

// WO34, the owner: "MNG (Mango)" left the brand empty — the full name, then each part, on every site
const MNG = { brand: "MNG (Mango)", brand_typed: "MNG (Mango)", brand_candidates: ["MNG (Mango)", "MNG", "Mango"] };

test("Depop: a two-part brand finds the part Depop lists (MNG (Mango) -> Mango)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, ...MNG } });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.w.__picked["brand-input"], "Mango");
  assert.ok(plain(r.guesses).includes("brand set to 'Mango' (from 'MNG (Mango)')"), JSON.stringify(r.guesses));
});

test("Vinted: a two-part brand finds the part Vinted lists (MNG (Mango) -> Mango)", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, ...MNG } });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.d.getElementById("brand").value, "Mango");
  assert.ok(plain(r.guesses).includes("brand set to 'Mango' (from 'MNG (Mango)')"), JSON.stringify(r.guesses));
});

test("Depop: the brand spellings tried", () => {
  const { w } = page("depop");
  assert.deepEqual(plain(w.Thrift.brandSpellings("J. Crew", "J. Crew")), ["J. Crew", "J.Crew", "J Crew"]);
  assert.deepEqual(plain(w.Thrift.brandSpellings("Levi's", null)), ["Levi's", "Levis"]);
});

test("Depop: a department the menu doesn't list fails the category, the menu on record", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, category: "Home > Bottoms > Pants" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.match(plain(r.failed)[0], /^category: group-input: no option 'Home > Bottoms > Pants'/);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "menu-group-input" && m.html.includes("Women > Bottoms")));
});

test("Depop: a brand the menu doesn't offer gets Depop's own Other — Depop requires one (WO35)", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: "Zara", brand_typed: "Zara" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.deepEqual(plain(r.guesses), ["brand set to 'Other' (Depop has no 'Zara')"]);
  assert.equal(p.d.querySelector("#brand-input").value, "Other");     // its own J.Crew replaced too
  assert.equal(p.w.__picked["brand-input"], "Other");
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "menu-brand-input"));
});

test("Depop: an item with no brand gets Other, never Depop's own guess (WO35: the silk pants)", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: null, brand_typed: null, brand_candidates: [] } });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(plain(r)));
  assert.deepEqual(plain(r.guesses), ["brand set to 'Other' (no brand)"]);
  assert.equal(p.d.querySelector("#brand-input").value, "Other");     // Depop's own J.Crew suggestion replaced
  assert.equal(plain(r.seen)["brand-input"], "Other");
});

test("Depop: a package size Depop chose already is kept as it is; a menu that offers nothing typed gives its whole list", async () => {
  // live 2026-10-11: Depop had put "Small" in by itself, and its menu answered "No option" to "Small" typed again
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, pkgNoFilter: true });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, package_size: "Medium" } });   // Depop's own pick: Medium
  let r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "Medium");
  const q = page("depop", undefined, undefined, { ...DEPOP_FIX, pkgNoFilter: true });
  await run(q, DEPOP_JOB);                                                                    // ours: Large
  r = plain(q.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "Large");
});

test("Depop: a form Depop refuses (Brand: This field is required) is said at once — nothing published (WO35)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, sizeLag: 0 });
  p.w.Thrift.sites.depop.fill = async () => {};                       // the form as the silk pants had it: no brand
  const done = run(p, { ...DEPOP_JOB, job_id: "depop-refused", mode: "publish" });
  await until(() => p.events().some((e) => e.event === "ready"));
  p.tell({ type: "submit", job_id: "depop-refused" });
  await done;
  const end = plain(p.last());
  assert.equal(end.event, "error");
  assert.equal(end.page, "form");
  assert.equal(end.stage, "after_publish");
  assert.match(end.message, /^refused: .*This field is required/);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "refused"));
});

test("Depop: Post's success page names the new listing — its View listing link (WO35)", async () => {
  const url = "https://www.depop.com/products/create/success/?productId=946436605";
  const html = `<html><head><title>Done - Depop</title></head><body><main>
    <h1>Nice! It's listed</h1><p>Turn more of your clothes into cash by listing another item.</p>
    <a href="/products/tetiana9orokina-levis-501-distressed-denim-cutoff-e832/manage/">View listing</a>
    <a href="/products/create/">List another item</a></main></body></html>`;
  const p = page("depop", html, url);
  await run(p, { ...DEPOP_JOB, job_id: "depop-landed", mode: "after_publish" });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const r = plain(p.events().find((e) => e.event === "result"));
  assert.equal(r.url, "https://www.depop.com/products/tetiana9orokina-levis-501-distressed-denim-cutoff-e832/manage/");
  assert.equal(r.landing, url);
});

test("Depop: the seller's /manage/ page gives its SKU — our item id (WO35)", async () => {
  const url = "https://www.depop.com/products/tetianasorokin0-levis-501-cutoff-denim-shorts-ed56/manage/";
  const ld = { "@type": "Product", name: "Levi's 501", description: "Levi's 501 Cutoff Denim Shorts size 26\n\nShorts.",
               offers: { price: "30.00", availability: "https://schema.org/InStock" }, image: ["a.jpg"] };
  const html = `<html><head><title>Depop</title><script type="application/ld+json" data-testid="meta__jsonLd">
    ${JSON.stringify(ld)}</script></head><body><main><h1>Levi's Bottoms</h1><p aria-description="Price">$30.00</p>
    <dl><dt>SKU:</dt> <dd>i_261004_dd0c98</dd></dl>
    <button>Edit listing</button></main></body></html>`;
  const p = page("depop", html, url);
  await run(p, { job_id: "depop-sku", site: "depop", mode: "verify", listing_url: url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const r = plain(p.events().find((e) => e.event === "result"));
  assert.deepEqual(r.live.skus, ["i_261004_dd0c98"]);
  assert.equal(r.live.title, "Levi's 501 Cutoff Denim Shorts size 26");
});

test("Vinted: a brand Vinted doesn't list is 'List without brand' — Vinted requires one (WO35)", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: "Seven Wonders",
                                          brand_candidates: ["Seven Wonders"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.ok(r.guesses.includes("listed without a brand (Vinted has no 'Seven Wonders')"), JSON.stringify(r.guesses));
  assert.equal(p.w.__noBrand, true);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "menu-brand"));
});

test("Vinted: an item with no brand is 'List without brand' too (WO35)", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: null, brand_candidates: [] } });
  const r = plain(p.last());
  assert.equal(r.event, "result");
  assert.ok(r.guesses.includes("listed without a brand"), JSON.stringify(r.guesses));
  assert.equal(p.w.__noBrand, true);
});

test("Vinted: never a brand by one word of ours — the exact name or 'List without brand' (the owner, 2026-10-11)", async () => {
  // the stand-in's search answers nothing for "&": "Solid & Striped" is not found as written (or as "Solid and
  // Striped"), and its first word is never typed — "Solid" is never searched for, never picked
  const p = page("vinted");
  const typed = [];
  p.d.addEventListener("input", (e) => { if (e.target?.getAttribute?.("data-testid") === "brand-select-dropdown-search-input") typed.push(e.target.value); }, true);
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: "Solid & Striped",
                                          brand_candidates: ["Solid & Striped"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result");
  assert.ok(!typed.some((t) => t.trim().toLowerCase() === "solid"), JSON.stringify(typed));
  assert.ok(typed.includes("Solid & Striped") && typed.includes("Solid and Striped"), JSON.stringify(typed));
  assert.equal(p.w.__noBrand, true);
  assert.ok(r.guesses.includes("listed without a brand (Vinted has no 'Solid & Striped')"), JSON.stringify(r.guesses));
});

// ---- WO36, the Alo set (2026-10-12): "Alo" typed found no "Alo"; Depop's own right "Alo Yoga" was replaced by "Other",
// Vinted took its separate "alo". Now: the shop's name, the label's, the alias file's (the job's candidates, in order),
// each exact; then the ONE option that is a name of ours followed by more words; else the site's fallback, with what it
// offered in the note.
const DEPOP_ALO_MENU = ["Aloye", "Jo Malone", "Halogen", "Paloma Wool", "Buffalo London", "Salomon", "Dermalogica",
                        "Saloni", "Buffalo David Bitton", "Alo Yoga", "Yves Salomon", "Malo", "Malone Souliers"];   // live
const ALO = { brand: "Alo Yoga", brand_typed: "Alo Yoga", brand_candidates: ["Alo Yoga", "alo"] };

test("Depop: Depop's own Alo Yoga is kept when it is our name (WO36: it had been replaced by Other)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, aiBrand: "Alo Yoga", brands: DEPOP_ALO_MENU, brandContains: true });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, ...ALO } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.seen["brand-input"], "Alo Yoga");
  assert.equal(r.seen.brand_choice, "Alo Yoga");
  assert.deepEqual(r.guesses, []);
});

test("Depop: 'Alo' finds Alo Yoga — the one option that is our name followed by more words (WO36)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false, brands: DEPOP_ALO_MENU, brandContains: true });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: "Alo", brand_typed: "Alo", brand_candidates: ["Alo"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.w.__picked["brand-input"], "Alo Yoga");               // never Aloye, Halogen, Malo…
  assert.deepEqual(r.guesses, ["brand set to 'Alo Yoga' (from 'Alo')"]);
});

test("Depop: two options that start with our name are no guess — Other, and the note names them (WO36)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false, brands: ["Alo Yoga", "Alo Kids", "Aloye"] });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: "Alo", brand_typed: "Alo", brand_candidates: ["Alo"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.w.__picked["brand-input"], "Other");
  assert.deepEqual(r.guesses, ["brand set to 'Other' (Depop has no 'Alo'; it offers: Alo Yoga, Alo Kids, Aloye)"]);
});

test("Depop: 'Solid & Striped' is never Depop's 'Solid' — Other (the owner, 2026-10-11; WO36)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false, brands: ["Solid", "Striped Sun", "Solid Gold"] });
  const typed = [];
  p.d.addEventListener("input", (e) => { if (e.target?.id === "brand-input") typed.push(e.target.value); }, true);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: "Solid & Striped", brand_typed: "Solid & Striped",
                                         brand_candidates: ["Solid & Striped"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.w.__picked["brand-input"], "Other");
  assert.ok(!typed.some((t) => t.trim().toLowerCase() === "solid"), JSON.stringify(typed));
});

test("Depop: Style is never filled — ours or Depop's own (the owner, 2026-10-12; WO36)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, aiStyle: "Minimalist" });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, style: ["Casual"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.deepEqual(r.seen["style-input"], []);                         // Depop's Minimalist removed, our Casual never set
  assert.deepEqual(plain(p.w.__chosen["style-input"] || []), []);
  assert.ok(!p.events().some((e) => e.event === "step" && e.name === "style-input"));
  assert.ok(r.notes.includes("removed 'Minimalist' from style-input (Depop's own suggestion)"), JSON.stringify(r.notes));
});

test("Depop: a colour the menu doesn't offer is left empty — never the first option, and a publish goes on (WO36)", async () => {
  for (const mode of ["dry_run", "publish"]) {
    const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false, sizeLag: 0 });
    const job = { ...DEPOP_JOB, job_id: `depop-teal-${mode}`, mode, fields: { ...DEPOP_JOB.fields, colors: ["Teal"] } };
    const done = run(p, job);
    if (mode === "publish") {
      await until(() => p.events().some((e) => e.event === "ready" || e.event === "error"));
      p.tell({ type: "cancel", job_id: job.job_id });
    }
    await done;
    const r = plain(p.events().find((e) => e.event === (mode === "publish" ? "ready" : "result")));
    assert.ok(r, JSON.stringify(plain(p.last())));
    assert.deepEqual(r.failed, []);
    assert.deepEqual(r.seen["colour-input"], []);                       // not Black, the menu's first
    assert.deepEqual(r.seen.left, { "colour-input": ["Teal"] });
    assert.ok(r.notes.some((n) => /^colour-input: no option 'Teal'.* — left empty$/.test(n)), JSON.stringify(r.notes));
    assert.deepEqual(plain(p.fetches), []);                             // nothing posted
  }
});

test("Depop: a required choice that changed after ours is chosen again once before the read-back (WO36 §4)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  const send = p.w.chrome.runtime.sendMessage;
  let changed = false;
  p.w.chrome.runtime.sendMessage = (m) => {
    if (!changed && m.type === "event" && m.event === "step" && m.name === "price") {
      changed = true;
      p.d.getElementById("condition-input").value = "Used - Good";      // something changed it after ours
    }
    return send(m);
  };
  await run(p, DEPOP_JOB);
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.seen["condition-input"], ["Like new"]);
  assert.ok(r.notes.includes("the condition read 'Used - Good': chosen again"), JSON.stringify(r.notes));
});

test("Vinted: the shop's name before the label's — Alo Yoga, not Vinted's separate 'alo' (WO36)", async () => {
  const p = page("vinted", undefined, undefined, { brands: ["alo", "Alo Yoga", "Aloha Rag"] });
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, ...ALO } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.d.getElementById("brand").value, "Alo Yoga");
  assert.equal(r.seen.brand_shown, "Alo Yoga");
  assert.deepEqual(r.guesses, []);
});

test("Vinted: 'Alo' finds Alo Yoga by the one-longer-name rule; two such names are no guess (WO36)", async () => {
  const p = page("vinted", undefined, undefined, { brands: ["Alo Yoga", "Aloha Rag"] });
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: "Alo", brand_candidates: ["Alo"] } });
  let r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(p.d.getElementById("brand").value, "Alo Yoga");
  assert.deepEqual(r.guesses, ["brand set to 'Alo Yoga' (from 'Alo')"]);
  const q = page("vinted", undefined, undefined, { brands: ["Alo Yoga", "Alo Kids"] });
  await run(q, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: "Alo", brand_candidates: ["Alo"] } });
  r = plain(q.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(q.w.__noBrand, true);
  assert.equal(r.seen.brand_shown, "List without brand");
  assert.deepEqual(r.guesses, ["listed without a brand (Vinted has no 'Alo'; it offers: Alo Yoga, Alo Kids)"]);
  assert.ok(!r.notes.some((n) => /brand read/.test(n)), JSON.stringify(r.notes));   // its field's own words: no fix
});

test("Vinted: a material the list doesn't offer is left empty — never another row (WO36)", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, materials: ["Cashmere", "Wool"] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.deepEqual(r.seen.material, ["Wool"]);
  assert.deepEqual(r.seen.left, { material: ["Cashmere"] });
});

test("Vinted: a size that changed after ours is chosen again once before the read-back (WO36 §4)", async () => {
  const p = page("vinted");
  const send = p.w.chrome.runtime.sendMessage;
  let changed = false;
  p.w.chrome.runtime.sendMessage = (m) => {
    if (!changed && m.type === "event" && m.event === "step" && m.name === "package") {
      changed = true;
      p.d.getElementById("size").value = "M / US 8-10";
    }
    return send(m);
  };
  await run(p, VINTED_JOB);
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.seen.size, ["S / US 4-6"]);
  assert.ok(r.notes.includes("size read 'M / US 8-10': chosen again"), JSON.stringify(r.notes));
});

test("Vinted: package sizes drawn late are waited for (WO35: live, none there yet when the price was typed)", async () => {
  const p = page("vinted", undefined, undefined, { packageLag: 400 });
  p.w.Thrift.timeoutScale = 1;                               // the real 10 s wait; the sizes come after 0.4 s
  await run(p, VINTED_JOB);
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "MEDIUM");
});

test("Depop: a publish posts exactly once", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 0, brandDelay: 50 });
  const done = run(p, { ...DEPOP_JOB, job_id: "depop-2", mode: "publish" });
  await until(() => p.events().some((e) => e.event === "ready"));
  p.tell({ type: "submit", job_id: "depop-2" });
  await done;
  p.tell({ type: "submit", job_id: "depop-2" });
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(plain(p.fetches), ["/api/post-click"]);
});

test("Depop: a value the menu doesn't offer fails that step (a dry run goes on, a publish stops)", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, condition: "Mint" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.match(plain(r.failed)[0], /^condition: condition-input: no option 'Mint'/);
  assert.deepEqual(plain(r.seen["colour-input"]), ["Cream"]);         // the dry run went on
  const q = page("depop", undefined, undefined, DEPOP_FIX);
  await run(q, { ...DEPOP_JOB, job_id: "depop-3", mode: "publish", fields: { ...DEPOP_JOB.fields, condition: "Mint" } });
  assert.equal(q.last().event, "error");
  assert.equal(q.last().stage, "condition");
  assert.ok(!q.events().some((e) => e.event === "ready"));
});

const STOPS = [
  ["vinted", "login", `<html><head><title>Vinted</title></head><body><a href="/member/signup">Sign up</a>
    <h1>Join and sell pre-loved clothes with no fees</h1></body></html>`],
  ["depop", "block", `<html><head><title>403 Forbidden</title></head><body><h1>Sorry, not authorized.</h1>
    <p>403 Forbidden — you were blocked.</p></body></html>`],
  ["depop", "captcha", `<html><head><title>Depop</title></head><body><div id="px-captcha"></div>
    <p>Press &amp; Hold to confirm you are a human</p></body></html>`],
  ["depop", "login", `<html><head><title>Log in</title></head><body><h1>Sign up or log in</h1>
    <button>Continue with email</button></body></html>`],
];
for (const [site, kind, html] of STOPS) {
  test(`${site}: a ${kind} page stops before anything is filled`, async () => {
    const p = page(site, html);
    await run(p, site === "vinted" ? VINTED_JOB : DEPOP_JOB);
    const r = p.last();
    assert.equal(r.event, "error");
    assert.equal(r.page, kind);
    assert.ok(!p.events().some((e) => e.event === "step"));
    assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === kind));
  });
}

test("check_login: the sell form is a yes, a login page a no", async () => {
  const p = page("vinted");
  await run(p, { ...VINTED_JOB, mode: "check_login" });
  assert.equal(p.last().event, "result");
  assert.equal(p.last().page, "form");
  const q = page("vinted", STOPS[0][2]);
  await run(q, { ...VINTED_JOB, mode: "check_login" });
  assert.equal(q.last().page, "login");
});

test("the brand is the exact name only — never another line, a shorter or a similar name (the owner, 2026-10-11)", () => {
  const { w } = page("vinted");
  const pick = w.Thrift.strictPick;
  assert.deepEqual(plain(pick("J.Crew", ["J. Crew Factory", "J. Crew"])),
                   { choice: "J. Crew", guess: "brand set to 'J. Crew' (from 'J.Crew')" });
  assert.equal(pick("J. Crew", ["J. Crew Factory"]).choice, null);
  assert.equal(pick("J.Crew Factory", ["J. Crew"]).choice, null);       // no "clean form" any more
  assert.equal(pick("Zara Basic", ["Zara"]).choice, null);
  assert.equal(pick("Solid & Striped", ["Solid", "Striped Sun"]).choice, null);
  assert.equal(pick("Seven Wonders", ["Seven", "Seven For All Mankind"]).choice, null);
  assert.equal(pick("Solid & Striped", ["Solid and Striped"]).choice, "Solid and Striped");   // "&" is "and"
  assert.equal(pick("Tory Burch", ["Tory Burch", "Tory Sport"]).guess, null);
  assert.deepEqual(plain(w.Thrift.spellings("J.Crew")), ["J.Crew", "J. Crew"]);
  assert.deepEqual(plain(w.Thrift.spellings("Solid & Striped")), ["Solid & Striped", "Solid and Striped"]);
});

test("Vinted: the page check waits for the title's text, not just its element (2026-10-11: read empty once)", async () => {
  const url = "https://www.vinted.com/items/10303333852-levis-501";
  const html = `<html><head><title>Vinted</title></head><body><main><h1 data-testid="item-page-title"></h1>
    <p data-testid="item-price">$30.00</p></main><script>
    setTimeout(() => { document.querySelector("h1").textContent = "Levi's 501 Distressed Denim Cut-Off Jean Shorts"; }, 400);
  </script></body></html>`;
  const p = page("vinted", html, url);
  p.w.Thrift.timeoutScale = 1;
  await run(p, { job_id: "vinted-verify-late", site: "vinted", mode: "verify", listing_url: url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"), 15000);
  const r = plain(p.events().find((e) => e.event === "result"));
  assert.equal(r.live.title, "Levi's 501 Distressed Denim Cut-Off Jean Shorts");
});

test("Depop's option match: the text IS the value; a category path shows its parts in order", () => {
  const { w } = page("depop");
  const m = w.Thrift.matchOption;
  assert.equal(m(["Women > Bottoms > Skirts", "Kids > Bottoms > Skirts"], "Kids > Bottoms > Skirts", true), 1);
  assert.equal(m(["Skirts (Women, Bottoms)"], "Women > Bottoms > Skirts", true), -1);
  assert.equal(m(["Women Bottoms Skirts"], "Women > Bottoms > Skirts", true), 0);
  assert.equal(m(["S", "XS"], "s"), 0);
  assert.equal(m(["One size"], "S"), -1);
});

test("Depop's department headings: a group's label, else the nearest text above the option", () => {
  const { w, d } = page("depop");
  const menu = d.createElement("ul");
  menu.innerHTML = `<li>Menswear</li><li role="option">Pants</li><li>Womenswear</li><li role="option">Pants</li>
    <li role="group" aria-label="Kidswear"><ul><li role="option">Pants</li></ul></li>`;
  d.body.appendChild(menu);
  const opts = [...menu.querySelectorAll("[role='option']")];
  assert.deepEqual(opts.map((o) => w.Thrift.headerOf(o, menu)), ["Menswear", "Womenswear", "Kidswear"]);
});

// ---------------------------------------------------------------- WO33: take-downs (reversible) and their probe
// Our listing's own page: Depop's Mark as sold, Vinted's Hide, each with the confirmation the site may show. `delist`
// presses the control and its confirmation once each (never a Delete); `probe` only looks — nothing is clicked, the
// page and its picture are the evidence the selector is recorded from.
const LISTING_PAGES = {
  depop: { url: "https://www.depop.com/products/myshop-j-crew-wide-leg-pants-11b1/", control: "Mark as sold",
           attrs: "" },
  vinted: { url: "https://www.vinted.com/items/7123456789-j-crew-wide-leg-pants", control: "Hide",
            attrs: ' data-testid="item-hide-button"' },
};
const listingPage = (site, withControl = true) => {
  const { control, attrs } = LISTING_PAGES[site];
  return `<html><head><title>J. Crew Wide Leg Pants</title></head><body><h1>J. Crew Wide Leg Pants</h1>
    <button>Edit</button><button>Delete</button>
    ${withControl ? `<button id="control"${attrs}>${control}</button>` : ""}
    <script>
      window.clicks = [];
      const control = document.getElementById("control");
      if (control) control.addEventListener("click", () => {
        window.clicks.push("control");
        const dialog = document.createElement("div");
        dialog.setAttribute("role", "dialog");
        dialog.innerHTML = "<p>Are you sure?</p><button id='yes'>${control}</button><button>Cancel</button>";
        document.body.appendChild(dialog);
        dialog.querySelector("#yes").addEventListener("click", () => {
          window.clicks.push("confirm");
          dialog.remove();
          if (!window.__keepControl) control.remove();      // taken: the page no longer offers it (Vinted: "Hide" gone)
        });
      });
    </script></body></html>`;
};

for (const site of ["vinted"]) {               // Depop's take-down is a delete since WO33: its own tests below
  const control = LISTING_PAGES[site].control;

  test(`${site}: the take-down presses ${control} and its confirmation once each, never Delete`, async () => {
    const p = page(site, listingPage(site), LISTING_PAGES[site].url);
    await run(p, { job_id: `${site}-delist`, site, mode: "delist", listing_url: LISTING_PAGES[site].url });
    await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
    const result = p.events().find((e) => e.event === "result");
    assert.ok(result && result.delisted, JSON.stringify(plain(p.events())));
    assert.deepEqual(plain(p.w.clicks), ["control", "confirm"]);
    assert.ok(!p.d.querySelector("[role=dialog]"));
  });

  test(`${site}: the probe finds ${control} and clicks nothing`, async () => {
    const p = page(site, listingPage(site), LISTING_PAGES[site].url);
    await run(p, { job_id: `${site}-probe`, site, mode: "probe", listing_url: LISTING_PAGES[site].url });
    await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
    const result = plain(p.events().find((e) => e.event === "result"));
    assert.equal(result.probe, true);
    assert.equal(result.found, true);
    assert.equal(result.text, control);
    assert.deepEqual(plain(p.w.clicks), []);                      // looked at, never pressed
  });

  test(`${site}: a page without ${control} is an error, not a guess`, async () => {
    const p = page(site, listingPage(site, false), LISTING_PAGES[site].url);
    await run(p, { job_id: `${site}-delist-none`, site, mode: "delist", listing_url: LISTING_PAGES[site].url });
    await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
    assert.ok(p.events().some((e) => e.event === "error" && new RegExp(`no ${control}`, "i").test(e.message)),
              JSON.stringify(plain(p.events())));
    assert.deepEqual(plain(p.w.clicks), []);
  });
}

// ---------------------------------------------------------------- WO40: the UGG's Vinted take-down
// Live 2026-10-16: "no Hide button" (read the moment the page script started) and "the page reloaded during the
// take-down" (Vinted's /session-refresh). Hide is waited for; a job handed again after a reload with `pressed` never
// presses again — the page only says whether it took; an "Unhide" page is hidden already.
const vintedSeller = (buttons) => `<html><head><title>UGG</title></head><body><h1>UGG Slippers</h1>
  <button data-testid="item-edit-button">Edit listing</button>${buttons}
  <script>window.clicks = [];</script></body></html>`;

test("vinted (WO40): a Hide drawn late is waited for and pressed once", async () => {
  const p = page("vinted", listingPage("vinted").replace('<button id="control" data-testid="item-hide-button">Hide</button>', ""),
                 LISTING_PAGES.vinted.url);
  p.w.Thrift.timeoutScale = 1;                                   // the real wait (15 s); Hide comes after 1.5 s
  setTimeout(() => {
    const b = p.d.createElement("button");
    b.id = "control";
    b.setAttribute("data-testid", "item-hide-button");
    b.textContent = "Hide";
    b.addEventListener("click", () => { p.w.clicks.push("control"); b.remove(); });
    p.d.body.appendChild(b);
  }, 1500);
  await run(p, { job_id: "vinted-delist-late", site: "vinted", mode: "delist", listing_url: LISTING_PAGES.vinted.url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"), 15000);
  assert.ok(p.events().some((e) => e.event === "result" && e.delisted), JSON.stringify(plain(p.events())));
  assert.deepEqual(plain(p.w.clicks), ["control"]);
});

test("vinted (WO40): after a reload with Hide pressed, a page without Hide is done and nothing is pressed", async () => {
  const p = page("vinted", vintedSeller(""), LISTING_PAGES.vinted.url);
  p.w.Thrift.timeoutScale = 0.05;
  await run(p, { job_id: "vinted-delist-pressed", site: "vinted", mode: "delist", pressed: true,
                 listing_url: LISTING_PAGES.vinted.url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.ok(p.events().some((e) => e.event === "result" && e.delisted), JSON.stringify(plain(p.events())));
});

test("vinted (WO40): after a reload with Hide pressed, Hide still there is an error — never pressed twice", async () => {
  const p = page("vinted", vintedSeller('<button data-testid="item-hide-button" onclick="window.clicks.push(1)">Hide</button>'),
                 LISTING_PAGES.vinted.url);
  await run(p, { job_id: "vinted-delist-pressed2", site: "vinted", mode: "delist", pressed: true,
                 listing_url: LISTING_PAGES.vinted.url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.ok(p.events().some((e) => e.event === "error" && /still offers Hide/.test(e.message)));
  assert.deepEqual(plain(p.w.clicks), []);
});

test("vinted (WO40): a listing that offers Unhide is hidden already", async () => {
  const p = page("vinted", vintedSeller("<button>Unhide</button>"), LISTING_PAGES.vinted.url);
  await run(p, { job_id: "vinted-delist-unhide", site: "vinted", mode: "delist", listing_url: LISTING_PAGES.vinted.url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.ok(p.events().some((e) => e.event === "result" && e.delisted && e.hidden_already));
});

// ---------------------------------------------------------------- WO33: Vinted's landing after Upload
// Recorded from the first supervised publish: the member page (?promo_shown=true) with an "Item listed" dialog —
// "List another" and "Later" — and the page's Bump buttons. Only Later is pressed; then the shop check takes over.
const MEMBER_URL = "https://www.vinted.com/member/1234?promo_shown=true";
const memberPage = (withDialog = true) => `<html><head><title>Vinted</title></head><body>
  <h2>tattishop</h2>
  <div data-testid="grid-item"><a href="/items/9876543210-j-crew-wide-leg-sweater-pants">J. Crew Wide Leg Sweater Pants</a>
    <button type="button" data-testid="bump-button" id="bump">Bump</button></div>
  ${withDialog ? `<div role="dialog" class="web_ui__Dialog"><h1>Item listed</h1>
    <button type="button" data-testid="list-promotion-submit-cta" id="another">List another</button>
    <button type="button" data-testid="list-promotion-cancel-cta" id="later">Later</button></div>` : ""}
  <script>
    window.clicks = [];
    for (const id of ["bump", "another", "later"]) {
      const b = document.getElementById(id);
      if (b) b.addEventListener("click", () => {
        window.clicks.push(id);
        if (id === "later") document.querySelector("[role=dialog]").remove();
      });
    }
  </script></body></html>`;

test("vinted: after Upload the member page's Item listed dialog gets Later — never List another, never Bump", async () => {
  const p = page("vinted", memberPage(), MEMBER_URL);
  await run(p, { job_id: "vinted-landed", site: "vinted", mode: "after_publish" });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const end = plain(p.events().find((e) => e.event === "error"));
  assert.equal(end.page, "landing");
  assert.match(end.message, /Item listed: Later/);
  assert.deepEqual(plain(p.w.clicks), ["later"]);
  assert.ok(!p.d.querySelector("[role=dialog]"));
});

test("vinted: after Upload the new listing is read from Vinted's own JSON — the exact title (WO35)", async () => {
  const p = page("vinted", memberPage(), MEMBER_URL);
  const items = [{ id: 10303459596, title: "Levi's 501 Cutoff Denim Shorts Washed Black Raw Hem size 26",
                   url: "https://www.vinted.com/items/10303459596-levis-501-cutoff" },
                 { id: 10303333852, title: "Levi's 501 Distressed Denim Cut-Off Jean Shorts Light Wash size 4",
                   url: "https://www.vinted.com/items/10303333852-levis-501-distressed" },
                 { id: 10303187091, title: "Levi's 501 Distressed Denim Cutoff Shorts Light Wash size 26",
                   url: "https://www.vinted.com/items/10303187091-levis-501-distressed" }];
  p.w.fetch = (u) => { p.fetches.push(String(u)); return Promise.resolve({ ok: true, json: async () => ({ items }) }); };
  await run(p, { job_id: "vinted-landed-json", site: "vinted", mode: "after_publish", shop: "1234",
                 copy: { title: "Levi's 501 Distressed Denim Cut-Off Jean Shorts Light Wash size 4" } });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const r = plain(p.events().find((e) => e.event === "result"));
  assert.equal(r.url, "https://www.vinted.com/items/10303333852-levis-501-distressed");
  assert.match(p.fetches[0], /\/api\/v2\/wardrobe\/1234\/items\?order=newest_first/);
  assert.deepEqual(plain(p.w.clicks), ["later"]);
});

test("vinted: the shop read takes Vinted's JSON of the newest items when it answers (WO35)", async () => {
  const p = page("vinted", "<html><body><main></main></body></html>", "https://www.vinted.com/member/1234");
  const items = [{ id: 7, title: "Zara Blue Floral Print Mini Skirt Side Zip Lined size M" }];
  p.w.fetch = (u) => Promise.resolve({ ok: true, json: async () => ({ items }) });
  await run(p, { job_id: "vinted-find-json", site: "vinted", mode: "find", shop: "1234", wardrobe: true });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const r = plain(p.events().find((e) => e.event === "result"));
  assert.equal(r.source, "wardrobe");
  assert.deepEqual(r.listings.map(({ id, title, text, url }) => ({ id, title, text, url })),
                   [{ id: "7", title: items[0].title, text: items[0].title, url: "https://www.vinted.com/items/7" }]);
  assert.equal(r.listings[0].photos, null);                            // WO45 follow-up: the item's facts ride along
});

test("vinted: a member page without the dialog presses nothing", async () => {
  const p = page("vinted", memberPage(false), "https://www.vinted.com/member/1234");
  await run(p, { job_id: "vinted-landed-plain", site: "vinted", mode: "after_publish" });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.match(plain(p.events().find((e) => e.event === "error")).message, /no Item listed dialog/);
  assert.deepEqual(plain(p.w.clicks), []);
});

test("vinted: the shop read waits for the member page to draw its tiles (WO33: live, read too early it found none)", async () => {
  const html = `<html><head><title>Vinted</title></head><body><main id="grid"></main><script>
    setTimeout(() => {
      document.getElementById("grid").innerHTML =
        '<a href="/items/111" title="Naturino size 7.5, brand: Naturino, $40.00">x</a>' +
        '<a href="/items/222" title="Zara dress size M, brand: Zara, $100.00">y</a>';
    }, 400);
  </script></body></html>`;
  const p = page("vinted", html, "https://www.vinted.com/member/1234");
  p.w.Thrift.timeoutScale = 1;                              // the real waits: the tiles come after 0.4 s
  await run(p, { job_id: "vinted-find", site: "vinted", mode: "find", shop: "1234" });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"), 15000);
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.equal(result.listings.length, 2);
  assert.match(result.listings[0].text, /Naturino size 7\.5/);
});

test("vinted: a Hide that doesn't take is an error, never a false done (WO33: the button must be gone afterwards)", async () => {
  const p = page("vinted", listingPage("vinted").replace("<script>", "<script>window.__keepControl = true;"),
                 LISTING_PAGES.vinted.url);
  p.w.Thrift.timeoutScale = 0.05;
  await run(p, { job_id: "vinted-delist-stuck", site: "vinted", mode: "delist", listing_url: LISTING_PAGES.vinted.url });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.ok(p.events().some((e) => e.event === "error" && /not hidden/.test(e.message)), JSON.stringify(plain(p.events())));
  assert.ok(!p.events().some((e) => e.event === "result"));
});

// ---------------------------------------------------------------- WO33: Depop's take-down deletes (the owner's rule)
// On our listing's page the bin next to Copy listing opens "Delete listing" ("Are you sure you want to delete this
// listing? You will lose all the likes.", Cancel / Delete listing). Sold already (JSON-LD SoldOut): nothing pressed.
const DEPOP_URL = "https://www.depop.com/products/myshop-j-crew-wide-leg-pants-11b1/";
const depopListing = (availability = "https://schema.org/InStock") => `<html><head><title>Depop</title>
  <script type="application/ld+json" data-testid="meta__jsonLd">${JSON.stringify({
    "@type": "Product", name: "J.Crew Women's Cream Trousers", description: "J. Crew Wide Leg Pants\n\nWide leg pants.",
    offers: { "@type": "Offer", price: "35.00", availability } })}</script></head><body>
  <a href="/products/edit/myshop-j-crew-wide-leg-pants-11b1/">Edit</a><button type="button">Boost listing</button>
  <a href="/products/create/?copy=1">Copy listing</a>
  <button type="button" aria-haspopup="dialog" aria-label="Delete listing" id="bin"><svg><title>Delete listing</title></svg></button>
  <script>
    window.clicks = [];
    document.getElementById("bin").addEventListener("click", () => {
      window.clicks.push("bin");
      const d = document.createElement("div");
      d.setAttribute("role", "dialog");
      d.innerHTML = "<h2>Delete listing</h2><p>Are you sure you want to delete this listing? You will lose all the likes.</p>" +
                    "<button type='button' id='no'>Cancel</button><button type='button' id='yes'>Delete listing</button>";
      document.body.appendChild(d);
      d.querySelector("#no").addEventListener("click", () => { window.clicks.push("cancel"); d.remove(); });
      d.querySelector("#yes").addEventListener("click", () => { window.clicks.push("delete"); d.remove(); });
    });
  </script></body></html>`;

test("depop: the take-down presses the bin, then Delete listing in its window — once each", async () => {
  const p = page("depop", depopListing(), DEPOP_URL + "manage/");
  await run(p, { job_id: "depop-delete", site: "depop", mode: "delist", listing_url: DEPOP_URL + "manage/", check_url: DEPOP_URL });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.deepEqual(plain(p.w.clicks), ["bin", "delete"]);
  assert.ok(p.events().some((e) => e.event === "result" && e.delete_pressed), JSON.stringify(plain(p.events())));
});

test("depop: a listing that shows sold already is never deleted — it sold twice", async () => {
  const p = page("depop", depopListing("https://schema.org/SoldOut"), DEPOP_URL + "manage/");
  await run(p, { job_id: "depop-sold", site: "depop", mode: "delist", listing_url: DEPOP_URL + "manage/", check_url: DEPOP_URL });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  assert.ok(p.events().some((e) => e.event === "result" && e.sold));
  assert.deepEqual(plain(p.w.clicks), []);
});

test("depop: the practice run opens the window, records it and presses Cancel — never Delete listing", async () => {
  const p = page("depop", depopListing(), DEPOP_URL + "manage/");
  p.w.fetch = async () => ({ status: 200, text: async () => '<script data-testid="meta__jsonLd">{"@type":"Product"}</script>' });
  await run(p, { job_id: "depop-practice", site: "depop", mode: "practice", listing_url: DEPOP_URL + "manage/", check_url: DEPOP_URL });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.deepEqual(plain(p.w.clicks), ["bin", "cancel"]);
  assert.equal(result.practice, true);
  assert.equal(result.closed, true);
  assert.equal(result.live, true);
  assert.deepEqual(result.window.buttons, ["cancel", "delete listing"]);
  assert.match(result.window.text, /you will lose all the likes/);
});

test("depop: the probe finds the bin and presses nothing", async () => {
  const p = page("depop", depopListing(), DEPOP_URL);
  await run(p, { job_id: "depop-probe", site: "depop", mode: "probe", listing_url: DEPOP_URL });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.equal(result.found, true);
  assert.equal(result.text, "Delete listing");
  assert.deepEqual(plain(p.w.clicks), []);
});

// ---------------------------------------------------------------- WO37: a live listing's cover, on its edit page

const EDIT = { depop: "https://www.depop.com/products/edit/tetianusorokina-ugg-new-chestnut-shearling-platform-87d9/",
               vinted: "https://www.vinted.com/items/10356580141/edit" };
const editPage = (site, fixture = {}) => page(site, read("tests/fixtures/ext_edit_photos.html"), EDIT[site],
                                              { site, ...fixture });
const coverJob = (site, extra = {}) => ({ job_id: `${site}-cover`, site, mode: "cover", from: 2, count: 6,
                                          edit_url: EDIT[site], listing_url: null, ...extra });

for (const site of ["depop", "vinted"]) {
  test(`${site}: the right photo is moved first with the keyboard, checked, and saved once on the go-ahead (WO37)`,
    async () => {
      const p = editPage(site);
      const before = plain(p.w.__order());
      const done = run(p, coverJob(site, { from: 5 }));                // the 6th photo: one row down, one column in
      await until(() => p.events().some((e) => e.event === "ready" || e.event === "error"));
      const ready = plain(p.events().find((e) => e.event === "ready"));
      assert.ok(ready, JSON.stringify(p.last()));
      assert.deepEqual(ready.seen.cover_before, before);
      assert.deepEqual(ready.seen.cover_after, [before[5], ...before.slice(0, 5)]);
      assert.equal(ready.seen.submit_buttons, 1);
      assert.equal(p.w.__saves, 0);                                    // nothing saved before the go-ahead
      p.tell({ type: "submit", job_id: `${site}-cover` });
      await done;
      assert.equal(p.w.__saves, 1);                                    // Save, exactly once
      assert.deepEqual(plain(p.w.__order()), [before[5], ...before.slice(0, 5)]);
    });

  test(`${site}: a cover dry run moves the photo on the page and never saves (WO37)`, async () => {
    const p = editPage(site);
    await run(p, coverJob(site, { dry: true }));
    const r = plain(p.last());
    assert.equal(r.event, "result", JSON.stringify(r));
    assert.equal(r.dry_run, true);
    assert.equal(r.seen.cover_after[0], r.seen.cover_before[2]);
    assert.equal(p.w.__saves, 0);
  });
}

test("vinted: a keyboard handle inside the photo is found when the photo itself doesn't start a drag (WO37)",
  async () => {
    const p = editPage("vinted", { handleInside: true });
    const before = plain(p.w.__order());
    await run(p, coverJob("vinted", { dry: true }));
    const r = plain(p.last());
    assert.equal(r.event, "result", JSON.stringify(r));
    assert.deepEqual(r.seen.cover_after, [before[2], before[0], before[1], ...before.slice(3)]);
    assert.equal(p.w.__saves, 0);
  });

test("a grid that doesn't answer the keyboard, or a listing with other photos, is never saved (WO37)", async () => {
  let p = editPage("vinted", { ignoreKeys: true });
  await run(p, coverJob("vinted"));
  let r = plain(p.last());
  assert.equal(r.event, "error", JSON.stringify(r));
  assert.match(r.message, /^photo 3 didn't get to the front \(the order now: 1, 2, 3, 4, 5, 6\): nothing saved/);
  assert.equal(p.w.__saves, 0);
  p = editPage("depop", { stuck: true });                             // each move stops one place short
  await run(p, coverJob("depop"));
  r = plain(p.last());
  assert.equal(r.event, "error", JSON.stringify(r));
  assert.equal(p.w.__saves, 0);
  p = editPage("depop", { photos: 7 });                               // the owner added a photo since
  await run(p, coverJob("depop"));
  r = plain(p.last());
  assert.equal(r.event, "error");
  assert.match(r.message, /the listing shows 7 photos, not the 6 posted: changed since — left as it is/);
  assert.equal(p.w.__saves, 0);
});

// ---------------------------------------------------------------- WO38: the sold orders, read only

const ORDERS_URL = { depop: "https://www.depop.com/sellinghub/sold-items/",
                     vinted: "https://www.vinted.com/my_orders?order_type=sold" };
const ORDERS_API = { depop: "https://webapi.depop.com/api/v1/sellinghub/sold-items/?limit=20",
                     vinted: "https://www.vinted.com/api/v2/my_orders?type=sold&status=all&page=1" };
const BUYER = ["Jane Doe", "janedoe", "12 Main St", "9400111899223344556677", "jane@example.com", "hi, Jane here"];

function ordersPage(site) {
  const html = `<!doctype html><html><head><title>Sold items</title></head><body><main>
    <ul data-testid="sold-list">
      <li data-testid="order-row"><a href="/transactions/22942401831">Nike sandals boy size 10</a>
        <span data-testid="buyer">Jane Doe</span> <span>@janedoe</span> <span>12 Main St, Springfield</span>
        <span data-testid="order-status">Awaiting shipment</span> <span>Ship by Oct 14</span>
        <button id="ship">Mark as shipped</button> <button id="label">Print label</button>
        <a id="msg" href="/inbox/123">Message buyer</a></li>
      <li data-testid="order-row"><a href="/transactions/22845581943">Sparkling boots size 6</a>
        <span data-testid="order-status">Delivered</span> <span>Tracking 9400111899223344556677</span></li>
    </ul></main>
    <script>
      window.clicks = [];
      for (const kind of ["click", "pointerdown", "mousedown", "keydown", "submit", "input", "change"]) {
        document.addEventListener(kind, (e) => window.clicks.push(kind + ":" + (e.target.id || e.target.tagName)), true);
      }
    </script></body></html>`;
  const p = page(site, html, ORDERS_URL[site]);
  const action = site === "vinted" ? "https://www.vinted.com/api/v2/transactions/22942401831/mark_as_shipped"
    : "https://webapi.depop.com/api/v1/orders/1/mark-shipped";
  const entries = [ORDERS_API[site], action, "https://www.google-analytics.com/g/collect?v=2",
                   `https://www.${site}.com/api/v2/conversations/123`, ORDERS_API[site]];
  p.w.performance.getEntriesByType = (kind) => (kind === "resource"
    ? [...entries.map((name) => ({ name, initiatorType: "fetch" })), { name: "https://x/y.jpg", initiatorType: "img" }]
    : []);
  p.fetched = [];
  p.w.fetch = (u) => {
    p.fetched.push(String(u));
    const body = { my_orders: [{ transaction_id: 22942401831, title: "Nike sandals boy size 10", status: "Payment successful",
                                 status_text: "Awaiting shipment", buyer: { login: "janedoe", real_name: "Jane Doe" },
                                 shipping_address: { line1: "12 Main St" }, tracking_code: "9400111899223344556677",
                                 buyerEmail: "jane@example.com", message: "hi, Jane here",
                                 price: { amount: "5.0", currency_code: "USD" }, date: "2026-10-07T04:55:28Z" }] };
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json; charset=utf-8" },
                             json: async () => body });
  };
  return p;
}

for (const site of ["depop", "vinted"]) {
  test(`${site}: the sold orders are read — scrubbed, nothing clicked, no picture, no action re-requested (WO38)`,
       async () => {
    const p = ordersPage(site);
    await run(p, { job_id: `${site}-orders`, site, mode: "orders", orders_url: ORDERS_URL[site] });
    await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
    const result = plain(p.events().find((e) => e.event === "result"));
    assert.ok(result, JSON.stringify(plain(p.events())));
    assert.deepEqual(plain(p.w.clicks), []);                                   // nothing on the page was touched
    assert.equal(p.sent.filter((m) => m.type === "screenshot").length, 0);    // an order page is never pictured
    assert.deepEqual(p.fetched, [ORDERS_API[site]]);                          // only the read, once; never an action
    const read = result.resources.find((r) => r.json);
    const order = read.json.my_orders[0];
    assert.equal(order.title, "Nike sandals boy size 10");
    assert.equal(order.status_text, "Awaiting shipment");
    assert.equal(order.transaction_id, 22942401831);
    assert.equal(order.price.amount, "5.0");
    for (const k of ["buyer", "shipping_address", "tracking_code", "buyerEmail", "message"]) assert.equal(order[k], "<scrubbed>");
    assert.ok(result.status_texts.includes("Awaiting shipment") && result.status_texts.includes("Delivered"));
    assert.ok(result.testids.includes("order-row"));
    const all = JSON.stringify(p.sent);
    for (const secret of BUYER) assert.ok(!all.includes(secret), `${secret} left the page`);
  });
}

for (const site of ["depop", "vinted"]) {
  test(`${site}: probe 2 reads only the site's own JSON it is given, the rows as drawn, our titles only (WO38)`,
       async () => {
    const p = ordersPage(site);
    p.d.cookie = "access_token=tok123";
    const calls = [];
    const inner = p.w.fetch;
    p.w.fetch = (u, init) => { calls.push({ u: String(u), auth: init?.headers?.Authorization || null }); return inner(u, init); };
    const own = site === "depop" ? "https://webapi.depop.com/presentation/api/v1/receipts/?role=seller&limit=12"
      : "https://www.vinted.com/api/v2/my_orders?type=sold&status=all&page=1&per_page=20";
    await run(p, { job_id: `${site}-orders2`, site, mode: "orders", orders_url: ORDERS_URL[site],
                   candidates: [own, "https://evil.example.com/steal"], known: ["Nike Sandals Boy size 10"] });
    await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
    const result = plain(p.events().find((e) => e.event === "result"));
    assert.ok(result, JSON.stringify(plain(p.events())));
    assert.deepEqual(plain(p.w.clicks), []);
    assert.deepEqual(calls.map((c) => c.u), [own]);                      // the page's other requests are not repeated
    // Depop's API host takes the page's own token, as its page does; Vinted's own address needs none
    assert.equal(calls[0].auth, site === "depop" ? "Bearer tok123" : null);
    assert.equal(result.candidates[0].http, 200);
    assert.equal(result.candidates[0].json.my_orders[0].buyer, "<scrubbed>");
    assert.match(result.candidates[1].error, /another site/);
    assert.equal(result.rows.length, 2);
    assert.equal(result.rows[0].title, "Nike sandals boy size 10");          // ours: kept
    assert.equal(result.rows[1].title, "<other>");                           // not one we know: never leaves the page
    assert.deepEqual(result.rows[0].hrefs[0], "/transactions/<n>");
    assert.ok(result.rows[0].status_texts.includes("Awaiting shipment"));
    const all = JSON.stringify(p.sent);
    for (const secret of [...BUYER, "tok123"]) assert.ok(!all.includes(secret), `${secret} left the page`);
  });
}

// ---------------------------------------------------------------- WO38 §2: the sweep's readers, read only

function soldPage(site, fixture = {}) {
  // Depop as drawn live (2026-10-14): the receipt's link holds the picture and the buyer; the status sits beside it
  const row = site === "depop"
    ? `<div class="row"><a href="/sellinghub/sold-items/915093750/"><span>Jane Doe</span><span>@janedoe</span></a>
         <div><span>Sold on</span><span>20 days ago</span></div><span>Shipped</span></div>
       <div class="row"><a href="/sellinghub/sold-items/915093751/"><span>Bob</span></a><span>To ship</span>
         <span>2 days ago</span><button id="label" type="button">Print label</button></div>`
    : `<a data-testid="my-orders-item" href="/inbox/01a114b7"><span data-testid="my-orders-item--title">Nike sandals
         boy size 10</span><span>Package delivered.</span></a>`;
  const html = `<!doctype html><html><head><title>Sold</title></head><body><main>
    <nav><a href="/sellinghub/sold-items/">All</a><span>To ship</span><span>Completed</span></nav>
    ${row}
    <button id="ship" type="button">Mark as shipped</button><button id="msg" type="button">Message buyer</button>
    </main><script>
      window.clicks = [];
      for (const kind of ["click", "pointerdown", "mousedown", "keydown", "submit"]) {
        document.addEventListener(kind, (e) => window.clicks.push(kind + ":" + (e.target.id || e.target.tagName)), true);
      }
    </script></body></html>`;
  const p = page(site, html, ORDERS_URL[site], fixture);
  p.calls = [];
  p.answer = {};
  p.w.fetch = (u, init) => {
    p.calls.push({ u: String(u), credentials: init?.credentials, auth: init?.headers?.Authorization || null });
    const a = p.answer[String(u)];
    if (!a) return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve({ ok: a.status === 200, status: a.status, headers: { get: () => "application/json" },
                             json: async () => a.body });
  };
  return p;
}

test("vinted: the sweep reads Vinted's own JSON of the sold orders — our fields only, nothing clicked (WO38)",
     async () => {
  const p = soldPage("vinted");
  const json = "https://www.vinted.com/api/v2/my_orders?type=sold&status=all&page=1&per_page=20";
  p.answer[json] = { status: 200, body: { my_orders: [
    { transaction_id: 22942401001, conversation_id: "01a1", title: "Nike sandals boy size 10",
      price: { amount: "5.00", currency_code: "USD" }, status: "Package delivered.", date: "2026-10-07T04:55:13Z",
      transaction_user_status: "completed", buyer: { login: "janedoe", real_name: "Jane Doe" },
      shipping_address: { line1: "12 Main St" }, tracking_code: "9400111899223344556677" }] } };
  await run(p, { job_id: "vinted-sweep", site: "vinted", mode: "orders", orders_url: ORDERS_URL.vinted, sweep: true });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.ok(result, JSON.stringify(plain(p.events())));
  assert.deepEqual(result.orders, [{ order_id: "22942401001", title: "Nike sandals boy size 10", price: "5.00",
                                     sold_at: "2026-10-07T04:55:13Z", status_text: "Package delivered." }]);
  assert.equal(result.source, "json");
  assert.deepEqual(p.calls.map((c) => [c.u, c.credentials]), [[json, "include"]]);
  assert.deepEqual(plain(p.w.clicks), []);
  assert.equal(p.sent.filter((m) => m.type === "screenshot").length, 0);
  const all = JSON.stringify(p.sent);
  for (const secret of BUYER) assert.ok(!all.includes(secret), `${secret} left the page`);
});

test("depop: the sweep reads Depop's receipts with the page's token, never its cookies — our fields only (WO38)",
     async () => {
  const p = soldPage("depop");
  p.d.cookie = "access_token=tok123";
  const json = "https://webapi.depop.com/presentation/api/v1/receipts/?role=seller&limit=24";
  p.answer[json] = { status: 200, body: { objects: [            // the shape Depop answered live (2026-10-14)
    { purchase_id: 475978927001, transaction_id: 336479001, buyer: { username: "janedoe", name: "Jane Doe" },
      status: "delivered_pending_feedback", sold_timestamp: "2026-09-23T21:32:51.360803Z", currency: "GBP",
      items_amount: "30.00", buyer_amount: "35.00",
      line_items: [{ id: 336479001, product_id: 915093750, description: "Madewell sweater", picture_url: "x" }],
      shipping_address: { line1: "12 Main St" },
      parcels: [{ parcel_status: "DELIVERED", tracking: "9400111899223344556677", provider_name: "USPS",
                  shipped_at: "2026-09-25T19:16:02.000Z" }] }], page_info: { has_more: false } } };
  await run(p, { job_id: "depop-sweep", site: "depop", mode: "orders", orders_url: ORDERS_URL.depop, sweep: true });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.ok(result, JSON.stringify(plain(p.events())));
  assert.equal(result.source, "json");
  assert.deepEqual(result.orders, [{ order_id: "475978927001", status_text: "delivered_pending_feedback / DELIVERED",
                                     sold_at: "2026-09-23T21:32:51.360803Z", price: "30.00",
                                     shipped_at: "2026-09-25T19:16:02.000Z", listing_id: "915093750", title: null }]);
  assert.deepEqual(p.calls.map((c) => [c.u, c.credentials, c.auth]), [[json, "omit", "Bearer tok123"]]);
  assert.deepEqual(plain(p.w.clicks), []);
  const all = JSON.stringify(p.sent);
  for (const secret of [...BUYER, "tok123"]) assert.ok(!all.includes(secret), `${secret} left the page`);
});

test("depop: receipts refused → the list as drawn: each receipt's id and status word, never a name (WO38)", async () => {
  const p = soldPage("depop");
  p.d.cookie = "access_token=tok123";                    // the API host answers nothing (p.answer is empty)
  await run(p, { job_id: "depop-sweep2", site: "depop", mode: "orders", orders_url: ORDERS_URL.depop, sweep: true });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const result = plain(p.events().find((e) => e.event === "result"));
  assert.ok(result, JSON.stringify(plain(p.events())));
  assert.equal(result.source, "page");
  assert.deepEqual(result.orders, [{ order_id: "915093750", status_text: "Shipped", sold_ago: "20 days ago" },
                                   { order_id: "915093751", status_text: "To ship", sold_ago: "2 days ago" }]);
  assert.match(result.note, /receipts JSON/);
  assert.deepEqual(plain(p.w.clicks), []);
  const all = JSON.stringify(p.sent);
  for (const secret of ["Jane Doe", "janedoe", "Bob", "tok123"]) assert.ok(!all.includes(secret), `${secret} left the page`);
});

test("a sweep whose site answers an error ends with the error, nothing clicked (WO38)", async () => {
  const p = soldPage("vinted");
  const json = "https://www.vinted.com/api/v2/my_orders?type=sold&status=all&page=1&per_page=20";
  p.answer[json] = { status: 500, body: {} };
  await run(p, { job_id: "vinted-sweep-err", site: "vinted", mode: "orders", orders_url: ORDERS_URL.vinted, sweep: true });
  await until(() => p.events().some((e) => e.event === "result" || e.event === "error"));
  const error = plain(p.events().find((e) => e.event === "error"));
  assert.equal(error.stage, "orders");
  assert.match(error.message, /answered 500/);
  assert.deepEqual(plain(p.w.clicks), []);
});

// ---------------------------------------------------------------- WO41: a price drop on the listing's edit page

// The edit pages as the stand-in forms with the listing's values in them: Depop's (its Save "Save changes"), Vinted's
// (its Save the form's one save button) — the price field holding the live price.
function priceEdit(site, { price = "25.00", blank = false } = {}) {
  let html = read(FIXTURES[site]);
  if (site === "depop") {
    html = html.replace('step="any" min="0" value="0.00"', `step="any" min="0" value="${price}"`)
      .replace(">Post</button>", ">Save changes</button>");
  } else {
    html = html.replace('type="text" name="price">', `type="text" name="price" value="${price}">`)
      .replace(">Upload</button>", ">Save</button>").replace('class="hidden-until-category" id="details"', 'id="details"');
  }
  const p = page(site, html, EDIT[site], site === "depop" ? { ...DEPOP_FIX, ai: false } : {});
  if (site === "depop" && !blank) {               // the listing's own values, as the edit page shows them
    for (const [id, v] of [["group-input", "Pants"], ["brand-input", "J. Crew"], ["condition-input", "Like new"]]) {
      p.d.getElementById(id).value = v;
    }
  }
  return p;
}
const priceJob = (site, extra = {}) => ({ job_id: `${site}-price`, site, mode: "price", price: 22, expect: 25,
                                          edit_url: EDIT[site], listing_url: null,
                                          fields: site === "depop" ? DEPOP_JOB.fields : {}, ...extra });

for (const site of ["depop", "vinted"]) {
  test(`${site}: a price drop types the new price, nothing else, and saves once on the go-ahead (WO41)`, async () => {
    const p = priceEdit(site);
    const box = p.d.querySelector(site === "depop" ? "[data-testid='priceAmount__input']"
      : "[data-testid='price-input--input']");
    const done = run(p, priceJob(site));
    await until(() => p.events().some((e) => e.event === "ready" || e.event === "error"));
    const ready = plain(p.events().find((e) => e.event === "ready"));
    assert.ok(ready, JSON.stringify(p.last()));
    assert.equal(ready.seen.price_before, "25.00");
    assert.equal(ready.seen.price, "22");
    assert.equal(ready.seen.mismatch, false);
    assert.equal(ready.seen.submit_buttons, 1);
    assert.equal(box.value, "22");
    p.tell({ type: "submit", job_id: `${site}-price` });
    await done;
    assert.ok(p.events().some((e) => e.event === "step" && e.name === "price"));
  });

  test(`${site}: an edit page showing another price is never typed into (WO41)`, async () => {
    const p = priceEdit(site, { price: "30.00" });
    await run(p, priceJob(site, { dry: true }));
    const r = plain(p.last());
    assert.equal(r.event, "result", JSON.stringify(r));
    assert.equal(r.seen.mismatch, true);
    assert.equal(r.seen.price, null);
  });
}

test("depop: the fields its edit page left blank are filled from the listing before the price (WO41)", async () => {
  const p = priceEdit("depop", { blank: true });
  await run(p, priceJob("depop", { dry: true }));
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.seen.refilled, ["category", "size", "brand", "condition", "colour"]);
  assert.equal(p.d.getElementById("group-input").value, "Pants");
  assert.equal(p.d.getElementById("condition-input").value, "Like new");
  assert.equal(r.seen.price, "22");
});


test("Depop: a package size its menu doesn't offer becomes the smallest bigger one it does (WO45; live: no Extra small)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, pkgOnly: ["Medium", "Large", "Extra large"], ai: false });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, package_size: "Extra small" } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "Medium");
  assert.equal(r.seen.package_choice, "Medium");                            // the driver's read-back takes that one
  assert.ok(r.guesses.some((g) => /^package size set to 'Medium' \(Depop offers no 'Extra small': Medium, Large, Extra large\)/.test(g)), r.guesses);
  assert.ok(!r.notes.some((n) => /set back to Extra small/.test(n)), r.notes);   // the tidy keeps the choice made
});

test("Depop: a 'photos' job on the edit page adds the files after the tiles it has and counts them (WO45 §6)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  const first = run(p, { ...DEPOP_JOB, job_id: "depop-p0", mode: "dry_run" });    // one photo on the page first
  await first;
  const before = p.d.querySelectorAll("[aria-roledescription='sortable']").length;
  assert.equal(before, 1);
  await p.w.Thrift.run({ job: { job_id: "depop-p1", site: "depop", mode: "photos", had: 1, dry: true,
                                edit_url: URLS.depop, listing_url: URLS.depop, fields: {}, copy: {}, photos: [] },
                         selectors: SELECTORS, photos: [PHOTO, PHOTO], pace: "fast" });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.dry_run, true);
  assert.equal(r.seen.photos_before, 1);
  assert.equal(r.seen.photos, 3);
  await p.w.Thrift.run({ job: { job_id: "depop-p2", site: "depop", mode: "photos", had: 1, dry: true, read_only: true,
                                edit_url: URLS.depop, listing_url: URLS.depop, fields: {}, copy: {}, photos: [] },
                         selectors: SELECTORS, photos: [], pace: "fast" });
  const q = plain(p.last());
  assert.equal(q.event, "result");
  assert.equal(q.seen.photos, 3);                                            // read only: counted, nothing added
});

test("Vinted: the shop read in bulk — every wardrobe item's price, state and photo count from its JSON (WO45 follow-up)", async () => {
  const p = page("vinted", undefined, "https://www.vinted.com/member/12345");
  p.w.fetch = async (u) => ({ ok: true, status: 200, json: async () => (String(u).includes("page=1") ? { items: [
    { id: 1, title: "A", price: { amount: "40.0" }, photos: [{}, {}, {}], is_hidden: false },
    { id: 2, title: "B", price: "35.0", is_reserved: true } ] } : { items: [] }) });
  await p.w.Thrift.run({ job: { job_id: "v-bulk", site: "vinted", mode: "find", shop: "12345", wardrobe: true, all: true },
                         selectors: SELECTORS, photos: [], pace: "fast" });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.source, "wardrobe");
  assert.deepEqual(r.shape, ["id", "title", "price", "photos", "is_hidden"]);
  assert.deepEqual(r.listings.map((x) => [x.id, x.price, x.photos, x.is_reserved]), [["1", "40.0", 3, false], ["2", "35.0", null, true]]);
});

test("Depop: the shop read in bulk from its products JSON with the page's token; the tiles when it can't be read", async () => {
  const p = page("depop", undefined, "https://www.depop.com/tetianasorokina/");
  p.d.cookie = "access_token=tok123";
  const asked = [];
  p.w.fetch = async (u, opts) => { asked.push([String(u), opts?.headers?.Authorization]); return { ok: true, status: 200,
    json: async () => ({ objects: [{ id: 7, slug: "tetiana-shirt-24cb", price: { priceAmount: "40.0" }, pictures: [{}, {}], sold: false }],
                          meta: { end: true } }) }; };
  await p.w.Thrift.run({ job: { job_id: "d-bulk", site: "depop", mode: "find", shop: "tetianasorokina", all: true },
                         selectors: SELECTORS, photos: [], pace: "fast" });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.source, "json");
  assert.ok(asked[0][0].includes("/shop/tetianasorokina/products/") && asked[0][1] === "Bearer tok123", asked);
  assert.deepEqual(r.listings, [{ id: "7", slug: "tetiana-shirt-24cb", price: "40.0", sold: false, photos: 2,
                                  url: "https://www.depop.com/products/tetiana-shirt-24cb/" }]);
});

// ---------------------------------------------------------------- the Vinted incident of 2026-10-20

test("Vinted: its system-error dialog during the photo upload ends the step with its words once no fresh page is left (live 2026-10-20: 0 of 8)", async () => {
  // the last allowed reload is spent (background.js counts them): a publish job ends at the photo step, nothing else filled
  const p = page("vinted", undefined, undefined, { photosError: true });
  await run(p, { ...VINTED_JOB, mode: "publish", reloads: 2 });
  const r = p.last();
  assert.equal(r.event, "error", JSON.stringify(r));
  assert.equal(r.stage, "photos");
  assert.match(r.message, /^Vinted's system error during the photo upload: Sorry, something went wrong/);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "photos"
                               && m.html.includes("item-upload-critical-error-dialog")));
  assert.equal(p.d.querySelector("[data-testid='title--input']").value, "");
  const step = p.events().find((e) => e.event === "step" && e.name === "photos");
  assert.equal(step.ok, false);
  assert.match(step.detail, /system error during the photo upload/);
  // a dry run records the failed step and goes on (WO30's rule), so the evidence shows the whole form
  const d = page("vinted", undefined, undefined, { photosError: true });
  await run(d, { ...VINTED_JOB, reloads: 2 });
  const dr = d.last();
  assert.equal(dr.event, "result", JSON.stringify(dr));
  assert.match(plain(dr.failed)[0], /^photos: Vinted's system error during the photo upload/);
  assert.equal(plain(dr.seen).photos, "0");
});

test("Vinted: its system-error dialog during the photo upload asks for a fresh page after a pause while a reload is left", async () => {
  const q = page("vinted", undefined, undefined, { photosError: true });
  const reloads = [];
  q.w.Thrift.photosRetryPauseMs = 10;
  q.w.Thrift.reloadPage = () => reloads.push(Date.now());
  run(q, { ...VINTED_JOB, mode: "publish" });          // never settles: the page is on its way out
  await until(() => reloads.length === 1);
  const progress = q.events().filter((e) => e.event === "progress").map((e) => e.what);
  assert.ok(progress.some((w) => /Vinted's system error during the photo upload .* 2 tries left/.test(w)), JSON.stringify(progress));
  assert.ok(!q.events().some((e) => e.event === "error"), JSON.stringify(q.events()));
  assert.ok(q.sent.some((m) => m.type === "screenshot" && m.label === "photos"));
  const one = page("vinted", undefined, undefined, { photosError: true });
  const again = [];
  one.w.Thrift.photosRetryPauseMs = 10;
  one.w.Thrift.reloadPage = () => again.push(1);
  run(one, { ...VINTED_JOB, mode: "publish", reloads: 1 });
  await until(() => again.length === 1);
  assert.ok(one.events().some((e) => e.event === "progress" && /1 try left/.test(e.what)));
  q.w.close();
  one.w.close();
});

test("Vinted: a photo count short of the files has the missing ones attached once more, then the step says so", async () => {
  // the stand-in draws a tile per file, but loses the first attach entirely: the second attach brings them
  const p = page("vinted", undefined, undefined, { loseFirstAttach: true });
  p.w.Thrift.timeoutScale = 0.02;                       // the waits for the lost tiles, short
  const two = [PHOTO, { ...PHOTO, name: "photo-2.jpg" }];
  await p.w.Thrift.run({ job: VINTED_JOB, selectors: SELECTORS, photos: two, pace: "fast" });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(plain(r.seen).photos, "2");
  assert.ok(plain(r.notes).some((n) => /photos: 0 of 2 shown — the missing 2 attached again/.test(n)), JSON.stringify(r.notes));
});

test("Vinted: its Recommended package size landing after our pick is put back once, and one landing before it is waited out (live 2026-10-20)", async () => {
  // after our click on Large the page moves to Medium (its recommendation): Large chosen again, said in the notes
  const p = page("vinted", undefined, undefined, { recommendAfterPick: 5, recommendTo: 2 });
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, package_sizes: ["LARGE"] } });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(plain(r.seen).package, "LARGE");
  assert.equal(p.d.querySelector("#package-size-3").getAttribute("role"), "presentation");
  assert.ok(plain(r.notes).some((n) => /package size read 'MEDIUM' .*large chosen again|package size read 'MEDIUM' at the check: chosen again/.test(n)),
            JSON.stringify(r.notes));
  // the recommendation (Large) comes 40 ms after the price — before our pick, or during the check's settle: the check
  // sees it and Medium is chosen again, whatever the machine's pace (CI's Linux runners saw Large here before the settle)
  const q = page("vinted", undefined, undefined, { recommendLate: 40, recommendTo: 3 });
  await run(q, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, package_sizes: ["MEDIUM"] } });
  const s = q.last();
  assert.equal(s.event, "result", JSON.stringify(s));
  assert.equal(plain(s.seen).package, "MEDIUM");
  assert.equal(q.d.querySelector("#package-size-2").getAttribute("role"), "presentation");
  assert.equal(q.d.querySelector("[data-testid='package-size-suggestion-badge-id-3']").textContent, "Recommended");
});

test("Vinted: a Recommended package size landing after the check is put back right before the Upload click (live 2026-10-20)", async () => {
  // Vinted's choice (Large) lands 400 ms after the price: past the check, while the form waits for the go-ahead
  const p = page("vinted", undefined, undefined, { recommendLate: 400, recommendTo: 3 });
  const job = { ...VINTED_JOB, job_id: "vinted-pkg", mode: "publish",
                fields: { ...VINTED_JOB.fields, package_sizes: ["MEDIUM"] } };
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  await new Promise((r) => setTimeout(r, 600));                        // its late choice has landed by now
  assert.equal(p.d.querySelector("#package-size-3").getAttribute("role"), "presentation");
  p.tell({ type: "submit", job_id: job.job_id });
  await done;
  assert.deepEqual(plain(p.fetches), ["/api/upload-click"]);          // clicked once — with our size back on
  assert.equal(p.d.querySelector("#package-size-2").getAttribute("role"), "presentation");
  assert.ok(p.events().some((e) => e.event === "progress"
                                  && /package size had moved to 'LARGE' before the click: MEDIUM chosen again/.test(e.what)),
            JSON.stringify(p.events().filter((e) => e.event === "progress")));
});

test("the brand key reads a company suffix as one word: Thursday Boot Co is Thursday Boot Company (live 2026-10-20)", () => {
  const T = page("vinted").w.Thrift;
  assert.equal(T.brandKey("Thursday Boot Co"), T.brandKey("Thursday Boot Company"));
  assert.equal(T.brandKey("Thursday Boot Co."), "thursdaybootco");
  assert.deepEqual(plain(T.strictPick("Thursday Boot Co", ["Thursday", "Thursday Boot Company"])),
                   { choice: "Thursday Boot Company", guess: "brand set to 'Thursday Boot Company' (from 'Thursday Boot Co')" });
  assert.equal(T.brandKey("AllSaints"), T.brandKey("All Saints"));
  assert.notEqual(T.brandKey("Solid"), T.brandKey("Solid & Striped"));
  assert.equal(T.brandKey("Hollister Co."), "hollisterco");
});

test("the site's requests after the click come from an observer, never the 250-entry buffer (live 2026-10-20: a refusal with no request kept)", () => {
  const p = page("vinted");
  let cb = null;
  p.w.PerformanceObserver = class { constructor(fn) { cb = fn; } observe() {} disconnect() {} };
  const T = p.w.Thrift;
  T.watchAfterClick();
  cb({ getEntries: () => [{ name: "https://www.vinted.com/api/v2/items", initiatorType: "fetch", responseStatus: 500 },
                          { name: "https://www.vinted.com/assets/app.js", initiatorType: "script", responseStatus: 200 },
                          { name: "https://www.vinted.com/api/v2/photos", initiatorType: "xmlhttprequest", responseStatus: 0 }] });
  assert.deepEqual(plain(T.requestsAfterClick()), ["/api/v2/items 500", "/api/v2/photos ?"]);
  const none = page("vinted").w.Thrift;             // no observer in this browser: the buffer's entries, if any
  none.watchAfterClick();
  assert.deepEqual(plain(none.requestsAfterClick()), []);
});

// ---------------------------------------------------------------- 2026-10-20, the leggings: Depop's late package choice, a lost photo upload

test("Depop: a package size Depop moves after the tidy is set back right before the Post click (live 2026-10-20: the leggings)", async () => {
  // at 13:01 Depop's own Medium landed after the tidy's set-back and the form read back Medium for our Small; now the
  // click is preceded by one last look
  const p = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100, ai: false });
  const job = { ...DEPOP_JOB, job_id: "depop-pkg", mode: "publish" };
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  const before = p.fetches.length;
  p.d.getElementById("shippingMethods-input").value = "Medium";       // Depop's own choice, landing late
  p.tell({ type: "submit", job_id: job.job_id });
  await done;
  assert.equal(p.fetches.length, before + 1);                          // clicked once — with our size back on
  assert.match(p.d.getElementById("shippingMethods-input").value, /^Large/);
  assert.ok(p.events().some((e) => e.event === "progress"
                                  && /package size had moved to 'Medium' before the click: Large chosen again/.test(e.what)),
            JSON.stringify(p.events().filter((e) => e.event === "progress")));
});

test("Depop: a photo count short of the files has the missing ones attached once more, then the step says so (live 2026-10-20)", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100, ai: false, loseFirstAttach: true });
  p.w.Thrift.timeoutScale = 0.02;                       // the waits for the lost tiles, short
  const two = [PHOTO, { ...PHOTO, name: "photo-2.jpg" }];
  await p.w.Thrift.run({ job: DEPOP_JOB, selectors: SELECTORS, photos: two, pace: "fast" });
  const r = p.last();
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(plain(r.seen).photos, "2");
  assert.ok(plain(r.notes).some((n) => /photos: 0 of 2 shown — the missing 2 attached again/.test(n)), JSON.stringify(r.notes));
  // a publish whose second attach shows nothing either ends at the photo step, nothing else filled
  const q = page("depop", undefined, undefined, { sizeLag: 150, brandDelay: 100, ai: false, loseAttaches: 2 });
  q.w.Thrift.timeoutScale = 0.02;
  await q.w.Thrift.run({ job: { ...DEPOP_JOB, job_id: "depop-nophotos", mode: "publish" }, selectors: SELECTORS,
                         photos: two, pace: "fast" });
  const e = q.last();
  assert.equal(e.event, "error", JSON.stringify(e));
  assert.equal(e.stage, "photos");
  assert.match(e.message, /^0 of 2 photos shown after a second attach/);
  assert.ok(!q.events().some((x) => x.event === "step" && x.name === "category"));   // nothing else filled
});

// 2026-10-22 (the owner, after the dress i_261021_93bd13 — the third listing the package race stopped in five days):
// Depop's package size is a rule, not a timing: its own choice is kept when it is at least ours, ours goes in only over
// an empty field or a smaller one, and a size the form shows is never a failure.
test("Depop: its own package size is kept when it is at least ours — nothing clicked over it (2026-10-22, the dress)", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);          // Depop's own suggestions after the photos: Medium
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, package_size: "Small" } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "Medium");                            // Depop's, not our Small
  assert.ok(r.notes.some((n) => /^package: Depop's 'Medium' kept \(ours would be Small\)/.test(n)), r.notes);
  assert.ok(!r.notes.some((n) => /set back/.test(n)), r.notes);
  const step = p.events().find((e) => e.event === "step" && e.name === "package");
  assert.equal(step.detail, "Medium (kept)");
  assert.equal(p.w.__picked["shippingMethods-input"], undefined);  // the menu was never opened for it
});

test("Depop: its package choice landing after the tidy, right before the read-back, is no failure when at least ours (the dress, live 2026-10-21)", async () => {
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  const send = p.w.chrome.runtime.sendMessage;
  p.w.chrome.runtime.sendMessage = (m) => {
    if (m.type === "event" && m.event === "step" && m.name === "tidy") {
      p.d.getElementById("shippingMethods-input").value = "Medium";    // Depop's own, after every look of the tidy
    }
    return send(m);
  };
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, package_size: "Small" } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.deepEqual(r.failed, []);
  assert.equal(r.seen.package, "Medium");                            // read back as Depop left it: the driver takes it
  assert.equal(r.seen.package_choice, "Small");
});

test("Depop: a package size moved below ours before the click is chosen again; one that can't be is posted as Depop's, said in a note", async () => {
  const late = (p) => {
    const send = p.w.chrome.runtime.sendMessage;
    p.w.chrome.runtime.sendMessage = (m) => {
      if (m.type === "event" && m.event === "step" && m.name === "tidy") {
        p.d.getElementById("shippingMethods-input").value = "Medium";  // Depop's own, after the tidy: below our Large
      }
      return send(m);
    };
  };
  const p = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  late(p);
  const job = { ...DEPOP_JOB, job_id: "depop-pkg-back", mode: "publish" };
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  assert.equal(plain(p.events().find((e) => e.event === "ready").seen.package), "Medium");
  p.tell({ type: "submit", job_id: job.job_id });
  await done;
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(plain(p.fetches), ["/api/post-click"]);           // clicked once, with Large back on the form
  assert.equal(p.d.getElementById("shippingMethods-input").value, "Large");
  assert.ok(p.sent.some((m) => m.type === "event" && m.event === "progress"
    && /^the package size had moved to 'Medium' before the click: Large chosen again/.test(m.what)), plain(p.sent.filter((m) => m.event === "progress")));
  assert.ok(!p.sent.some((m) => m.type === "event" && m.event === "note"));
  // Depop insists: it puts Medium back after every set-back of ours — two looks, then Depop's Medium goes, the note
  // says so (the row's "check:" line), and the click still happens once
  const q = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  let insist = null;
  const sendQ = q.w.chrome.runtime.sendMessage;
  q.w.chrome.runtime.sendMessage = (m) => {
    if (m.type === "event" && m.event === "step" && m.name === "tidy" && !insist) {
      const pkg = q.d.getElementById("shippingMethods-input");
      pkg.value = "Medium";                                            // at once, then again and again
      insist = setInterval(() => { if (pkg.value !== "Medium") pkg.value = "Medium"; }, 30);
    }
    return sendQ(m);
  };
  const job2 = { ...DEPOP_JOB, job_id: "depop-pkg-depops", mode: "publish" };
  const done2 = run(q, job2);
  await until(() => q.events().some((e) => e.event === "ready"));
  q.tell({ type: "submit", job_id: job2.job_id });
  await done2;
  clearInterval(insist);
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(plain(q.fetches), ["/api/post-click"]);
  const again = q.sent.filter((m) => m.type === "event" && m.event === "progress" && /chosen again/.test(m.what));
  assert.equal(again.length, 2, JSON.stringify(plain(q.sent.filter((m) => ["progress", "note", "error"].includes(m.event)))));          // twice at most, a settle between
  const note = q.sent.find((m) => m.type === "event" && m.event === "note");
  assert.ok(note, JSON.stringify(plain(q.sent.filter((m) => m.event === "note" || m.event === "progress"))));
  assert.match(note.text, /^package 'Medium' at the click, below the plan \(Large\) — Depop's own choice; check the label when it sells/);
  assert.ok(q.events().some((e) => e.event === "step" && e.name === "submit" && e.clicked));
});


// WO50: a listing's title / description changed on its edit page — only when the page still shows what we posted
test("Vinted and Depop: a 'text' job types the new title / first line, never over a text changed by hand (WO50)", async () => {
  const NEW = "BCBG Max Azria Black Slouchy Shoulder Bag, New with Tags";
  const p = page("vinted", undefined, undefined, { ai: false });
  p.d.querySelector("[data-testid='title--input']").value = "BCBGMAXAZRIA New Black Slouchy Shoulder Bag";
  await run(p, { job_id: "vinted-text", site: "vinted", mode: "text", dry: true, title: NEW,
                 expect_title: "BCBGMAXAZRIA New Black Slouchy Shoulder Bag", fields: {}, copy: {} });
  let r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.seen.title, NEW);
  assert.equal(r.seen.mismatch, false);
  const q = page("vinted", undefined, undefined, { ai: false });
  q.d.querySelector("[data-testid='title--input']").value = "Her own title";
  await run(q, { job_id: "vinted-text-2", site: "vinted", mode: "text", dry: true, title: NEW,
                 expect_title: "BCBGMAXAZRIA New Black Slouchy Shoulder Bag", fields: {}, copy: {} });
  r = plain(q.last());
  assert.equal(r.seen.mismatch, true);
  assert.equal(q.d.querySelector("[data-testid='title--input']").value, "Her own title");      // nothing typed
  const d = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  d.d.querySelector("textarea#description").value = "BCBGMAXAZRIA New Black Slouchy Shoulder Bag\n\nA bag.";
  await run(d, { job_id: "depop-text", site: "depop", mode: "text", dry: true,
                 description: `${NEW}\n\nA bag.`, expect_first_line: "BCBGMAXAZRIA New Black Slouchy Shoulder Bag",
                 fields: {}, copy: {} });
  r = plain(d.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.equal(r.seen.description, `${NEW}\n\nA bag.`);
});

test("a 'text' job never types over a description changed by hand (WO50, expect_description)", async () => {
  const d = page("depop", undefined, undefined, { ...DEPOP_FIX, ai: false });
  d.d.querySelector("textarea#description").value = "BCBGMAXAZRIA New Black Slouchy Shoulder Bag\n\nHer own words.";
  await run(d, { job_id: "depop-text-3", site: "depop", mode: "text", dry: true,
                 description: "BCBG Max Azria Black Slouchy Shoulder Bag\n\nA bag.",
                 expect_first_line: "BCBGMAXAZRIA New Black Slouchy Shoulder Bag",
                 expect_description: "BCBGMAXAZRIA New Black Slouchy Shoulder Bag\n\nA bag.", fields: {}, copy: {} });
  const r = plain(d.last());
  assert.equal(r.seen.mismatch, true, JSON.stringify(r));
  assert.equal(d.d.querySelector("textarea#description").value, "BCBGMAXAZRIA New Black Slouchy Shoulder Bag\n\nHer own words.");
});

test("Vinted: a brand panel that doesn't open at the first click still gives 'List without brand' (live 2026-10-27)", async () => {
  const p = page("vinted", undefined, undefined, { brandOpensLate: true });
  await run(p, { ...VINTED_JOB, fields: { ...VINTED_JOB.fields, brand: null, brand_candidates: [] } });
  const r = plain(p.last());
  assert.equal(r.event, "result", JSON.stringify(r));
  assert.ok(r.guesses.includes("listed without a brand"), JSON.stringify(r.guesses));
  assert.equal(p.w.__noBrand, true);
});
