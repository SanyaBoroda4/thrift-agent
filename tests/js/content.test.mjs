// WO32: the Thrift crosslister's content scripts (ext/content/*.js) in jsdom, on the stand-in copies of the two forms
// (tests/fixtures/vinted_new_item.html, depop_create.html): text typed through the native setter, dropdowns picked by
// real clicks on their options, photos attached as Files, colours ≤ 2, a dry run that never submits, a publish that
// clicks exactly once after the go-ahead, and the login / block / CAPTCHA pages that stop before anything is filled.
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
const FIXTURES = { vinted: "tests/fixtures/vinted_new_item.html", depop: "tests/fixtures/depop_create.html" };
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
  const fetches = [];
  w.fetch = (u) => { fetches.push(String(u)); return Promise.resolve({ ok: true, json: async () => ({}) }); };
  const sent = [];
  const listeners = [];
  w.chrome = { runtime: { sendMessage: (m) => { sent.push(m); return Promise.resolve({ ok: true }); },
                          onMessage: { addListener: (fn) => listeners.push(fn) } } };
  for (const f of SCRIPTS[site]) w.eval(read(f));
  w.Thrift.pace = 0;                        // no human pauses in a test
  w.Thrift.timeoutScale = 0.05;
  const events = () => sent.filter((m) => m.type === "event");
  const last = () => events().at(-1);
  const tell = (msg) => listeners.forEach((fn) => fn(msg, {}, () => {}));
  return { w, d: w.document, fetches, sent, events, last, tell };
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
  fields: { category_id: 5523, category_path: "Women > Clothing > Skirts", brand: "J. Crew", size: "S / US 4-6",
            condition: "New without tags", colors: ["Red", "Blue", "White"], materials: ["Cotton"],
            skirt_length: null, package_sizes: ["MEDIUM", "LARGE"] },
  copy: { title: "J. Crew Red Skirt size S", description: "A red skirt.\nNew without tags." },
};
const DEPOP_JOB = {
  job_id: "depop-1", site: "depop", mode: "dry_run", price: 35,
  fields: { category: "Women > Bottoms > Skirts", brand: "J. Crew", brand_typed: "J. Crew", size: "6",
            condition: "Used - Good", colors: ["Red"], source: ["Preloved"], age: null, style: [],
            attributes: { material: ["Cotton"] }, shipping: "Depop Shipping", package_size: "Small" },
  copy: { title: "J. Crew Red Skirt size 6", description: "J. Crew Red Skirt size 6\n\nA red skirt.\n\n#jcrew" },
};

const run = (p, job) => p.w.Thrift.run({ job, selectors: SELECTORS, photos: [PHOTO], pace: 0 });

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
  assert.equal(result.event, "result");
  assert.equal(result.dry_run, true);
  assert.deepEqual(plain(result.failed), []);
  const seen = plain(result.seen);
  assert.equal(seen.title, "J. Crew Red Skirt size S");
  assert.equal(seen.description, "A red skirt.\nNew without tags.");
  assert.equal(seen.price, "35");
  assert.equal(seen.photos, "1");
  assert.deepEqual(seen.category, ["Women > Clothing > Skirts"]);
  assert.deepEqual(seen.brand, ["J. Crew"]);
  assert.deepEqual(seen.size, ["S / US 4-6"]);
  assert.deepEqual(seen.condition, ["New without tags"]);
  assert.deepEqual(seen.color, ["Red", "Blue"]);                       // colours: at most 2
  assert.deepEqual(seen.material, ["Cotton"]);
  assert.equal(seen.package, "MEDIUM");
  assert.equal(seen.submit_buttons, 1);
  assert.ok(changes.input >= 2 && changes.seen === changes.input - 1 && changes.change === 1, JSON.stringify(changes));
  assert.equal(p.d.querySelector("[data-testid='catalog-select-dropdown-input']").value, "Women > Clothing > Skirts");
  assert.ok(p.d.querySelector("#packages input:checked").nextSibling.textContent.startsWith("Medium"));
  assert.deepEqual(plain(p.fetches), []);                                     // Upload never pressed
  const steps = p.events().filter((e) => e.event === "step").map((e) => e.name);
  assert.deepEqual(steps.slice(0, 4), ["photos", "title", "description", "category"]);
  assert.ok(p.sent.some((m) => m.type === "screenshot" && m.label === "form" && m.html.includes("upload-form-save")));
});

test("Vinted: a publish waits for the go-ahead and clicks Upload exactly once", async () => {
  const p = page("vinted");
  const job = { ...VINTED_JOB, job_id: "vinted-2", mode: "publish" };
  const done = run(p, job);
  await until(() => p.events().some((e) => e.event === "ready"));
  assert.deepEqual(plain(p.fetches), []);                                     // filled, not submitted
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

test("Depop: comboboxes cleared, typed and picked; the lagging size menu; Depop Shipping; Boost turned off", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 150 });
  p.d.querySelector("input[name='boost']").checked = true;             // on by itself: we never leave it on
  await run(p, DEPOP_JOB);
  const result = p.last();
  assert.equal(result.event, "result", JSON.stringify(result));
  assert.deepEqual(plain(result.failed), []);
  const seen = plain(result.seen);
  assert.equal(seen.description, DEPOP_JOB.copy.description);
  assert.deepEqual(seen["group-input"], ["Women > Bottoms > Skirts"]);
  assert.deepEqual(seen["variants-input"], ["6"]);
  assert.deepEqual(seen["brand-input"], ["J. Crew"]);                  // not "J. Crew Factory"
  assert.deepEqual(seen["condition-input"], ["Used - Good"]);
  assert.deepEqual(seen["colour-input"], ["Red"]);
  assert.deepEqual(seen["attributes.material-input"], ["Cotton"]);
  assert.equal(seen.package, "Small (up to 12 oz)");
  assert.equal(seen.boost, false);
  assert.equal(seen.photos, "1");
  assert.equal(seen.submit_buttons, 1);
  assert.ok(plain(result.notes).includes("Boost was on: turned off"));
  assert.deepEqual(plain(p.fetches), []);
});

test("Depop: a publish posts exactly once", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 0 });
  const done = run(p, { ...DEPOP_JOB, job_id: "depop-2", mode: "publish" });
  await until(() => p.events().some((e) => e.event === "ready"));
  p.tell({ type: "submit", job_id: "depop-2" });
  await done;
  p.tell({ type: "submit", job_id: "depop-2" });
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(plain(p.fetches), ["/api/post-click"]);
});

test("Depop: a value the menu doesn't offer fails that step (a dry run goes on, a publish stops)", async () => {
  const p = page("depop", undefined, undefined, { sizeLag: 0 });
  await run(p, { ...DEPOP_JOB, fields: { ...DEPOP_JOB.fields, condition: "Mint" } });
  const r = p.last();
  assert.equal(r.event, "result");
  assert.match(r.failed[0], /^condition: condition-input: no option 'Mint'/);
  assert.deepEqual(plain(r.seen["colour-input"]), ["Red"]);                   // the dry run went on
  const q = page("depop", undefined, undefined, { sizeLag: 0 });
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
