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
  assert.ok(result.notes.some((n) => /changed the package size to 'Medium': set back to Large/.test(n)), result.notes);
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

test("Depop: a brand the menu doesn't offer is left empty and cleared", async () => {
  const p = page("depop", undefined, undefined, DEPOP_FIX);
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, brand: "Zara", brand_typed: "Zara" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.deepEqual(plain(r.guesses), ["brand left empty (Depop has no 'Zara')"]);
  assert.equal(p.d.querySelector("#brand-input").value, "");          // its own J.Crew cleared too
  assert.equal(p.w.__picked["brand-input"], undefined);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "menu-brand-input"));
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

test("the brand is ours or its clean form — never a qualified other brand", () => {
  const { w } = page("vinted");
  const pick = w.Thrift.strictPick;
  assert.deepEqual(plain(pick("J.Crew", ["J. Crew Factory", "J. Crew"])),
                   { choice: "J. Crew", guess: "brand set to 'J. Crew' (from 'J.Crew')" });
  assert.equal(pick("J. Crew", ["J. Crew Factory"]).choice, null);
  assert.equal(pick("Zara Basic", ["Zara"]).choice, null);             // looser picks belong to Poshmark only
  assert.equal(pick("Tory Burch", ["Tory Burch", "Tory Sport"]).guess, null);
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
        dialog.querySelector("#yes").addEventListener("click", () => { window.clicks.push("confirm"); dialog.remove(); });
      });
    </script></body></html>`;
};

for (const site of ["depop", "vinted"]) {
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
