// WO54 §4: the Thrift crosslister's background service worker (ext/background.js) in Node's vm, with a stand-in for the
// chrome.* APIs it uses — no browser. Live 2026-11-09: the Thrift Chrome, started at 1 AM with --restore-last-session
// and a Vinted start page, held 9.6 GB in 51 pages on her 8 GB laptop; two of them burned a core each until 9:35 AM, and
// the tidy had skipped them (the active tab of a window, pages off the three sites). Checked here: the worker connects
// by itself in a Chrome started with no window; a site's window is created unfocused and nothing ever focuses one; a
// job's tab is its own window's active tab; the tidy closes every page no running job holds (the idle pages, the page
// left for the owner, a page she just looked at and a page just born aside), also while a job runs; the pings carry
// her focus and the tab count. Run: node --test tests/js/background.test.mjs (pytest runs it: tests/test_ext_js.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const SOURCE = readFileSync(new URL("ext/background.js", ROOT), "utf8");
const SELECTORS = readFileSync(new URL("ext/selectors.json", ROOT), "utf8");
const EXT = "chrome-extension://thrift/";
const MIN = 60 * 1000;
const UNSEEN_MS = 15 * MIN;                   // background.js UNSEEN

function event() {
  const fns = [];
  return { fns, addListener: (fn) => fns.push(fn), removeListener: (fn) => fns.splice(fns.indexOf(fn), 1),
           fire: (...args) => Promise.all(fns.map((fn) => fn(...args))) };
}

// background.js in a fresh realm: `tabs` / `windows` are what this Chrome holds when the worker starts.
function load({ tabs = [], windows = [], token = "tok-" + "x".repeat(30) } = {}) {
  const calls = [];
  const stores = { local: { token }, session: {} };
  const area = (name) => ({
    get: async (keys) => {
      const s = stores[name];
      const list = keys == null ? Object.keys(s) : Array.isArray(keys) ? keys : [keys];
      return JSON.parse(JSON.stringify(Object.fromEntries(list.filter((k) => k in s).map((k) => [k, s[k]]))));
    },
    set: async (obj) => { Object.assign(stores[name], JSON.parse(JSON.stringify(obj))); },
  });
  const st = { tabs: new Map(), windows: new Map(), next: 500 };
  for (const w of windows) st.windows.set(w.id, { type: "normal", focused: false, state: "normal", ...w });
  for (const t of tabs) st.tabs.set(t.id, { active: false, windowId: 1, status: "complete", ...t });
  const chrome = {
    runtime: { getURL: (p) => EXT + p, getManifest: () => ({ version: "0.1.0" }), reload: () => calls.push(["reload"]),
               onMessage: event(), onInstalled: event(), onStartup: event() },
    storage: { local: area("local"), session: area("session"), onChanged: event() },
    alarms: { get: async () => null, create: async (name, o) => calls.push(["alarms.create", name, o]), onAlarm: event() },
    management: { getSelf: async () => ({ installType: "development" }) },
    windows: {
      WINDOW_ID_NONE: -1,
      create: async (o) => {
        const id = st.next++, tabId = st.next++;
        st.windows.set(id, { id, type: "normal", focused: !!o.focused, state: o.state || "normal" });
        st.tabs.set(tabId, { id: tabId, windowId: id, url: o.url, active: true, status: "complete" });
        calls.push(["windows.create", o]);
        return { id, tabs: [{ id: tabId }] };
      },
      get: async (id) => { if (!st.windows.has(id)) throw new Error("No window with id"); return st.windows.get(id); },
      getAll: async () => [...st.windows.values()],
      getLastFocused: async () => {
        const all = [...st.windows.values()];
        const w = all.find((x) => x.focused) || all[0];
        if (!w) throw new Error("No last-focused window");
        return { ...w, tabs: [...st.tabs.values()].filter((t) => t.windowId === w.id) };
      },
      update: async (id, o) => { calls.push(["windows.update", id, o]); return st.windows.get(id); },
      onFocusChanged: event(),
    },
    tabs: {
      create: async (o) => {
        const id = st.next++;
        const t = { id, windowId: o.windowId, url: o.url, active: !!o.active, status: "complete" };
        st.tabs.set(id, t);
        calls.push(["tabs.create", o]);
        return t;
      },
      get: async (id) => { if (!st.tabs.has(id)) throw new Error("No tab with id"); return st.tabs.get(id); },
      query: async () => [...st.tabs.values()],
      remove: async (ids) => {
        for (const id of [].concat(ids)) st.tabs.delete(id);
        calls.push(["tabs.remove", [].concat(ids)]);
      },
      update: async (id, o) => { calls.push(["tabs.update", id, o]); return st.tabs.get(id); },
      sendMessage: async () => ({ ok: true }),
      captureVisibleTab: async () => "data:image/png;base64,AAAA",
      onUpdated: event(), onRemoved: event(), onActivated: event(), onCreated: event(),
    },
    scripting: { executeScript: async () => [] },
  };
  const sockets = [];
  class FakeSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }
  const posted = [];                                 // events sent over HTTP (the bridge's POST /events)
  const fetch = async (u, init = {}) => {
    if (String(u).startsWith(EXT)) {
      return { ok: true, arrayBuffer: async () => new TextEncoder().encode(String(u)).buffer,
               json: async () => JSON.parse(SELECTORS) };
    }
    if (String(u).endsWith("/events")) {
      posted.push(JSON.parse(init.body));
      return { ok: true, status: 200, json: async () => ({ commands: [] }) };
    }
    return { ok: false, status: 204, json: async () => ({}) };     // GET /jobs/next: nothing waiting
  };
  const timers = [];
  const keep = (t) => { if (t && t.unref) t.unref(); timers.push(t); return t; };
  const ctx = vm.createContext({
    chrome, WebSocket: FakeSocket, fetch, console, TextEncoder, crypto: globalThis.crypto, URL, URLSearchParams, btoa,
    setTimeout: (fn, ms) => keep(setTimeout(fn, ms)), clearTimeout,
    setInterval: (fn, ms) => keep(setInterval(fn, ms)), clearInterval,
  });
  vm.runInContext(SOURCE, ctx, { filename: "ext/background.js" });
  const ev = (code) => vm.runInContext(code, ctx);
  return { chrome, calls, stores, st, sockets, posted, ev, stop: () => timers.forEach((t) => clearTimeout(t)) };
}

const flush = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

async function connected(x) {
  await flush();
  const sock = x.sockets.at(-1);
  sock.readyState = 1;
  sock.onopen();
  await sock.onmessage({ data: JSON.stringify({ type: "welcome", ext_hash: "h" }) });
  await flush();
  return sock;
}

test("a Thrift Chrome started with no window: the worker connects by itself and says hello", async () => {
  const x = load();
  try {
    await flush();
    assert.equal(x.sockets.length, 1, "the top-level connect() opened the socket");
    assert.equal(x.sockets[0].url, "ws://127.0.0.1:8765/ext");
    assert.equal(x.chrome.runtime.onStartup.fns.length, 1, "runtime.onStartup wakes the worker with the profile");
    assert.ok(x.calls.some(([what, name]) => what === "alarms.create" && name === "poll"), "the alarm: the second way in");
    await x.chrome.runtime.onStartup.fire();
    await flush();
    assert.equal(x.sockets.length, 1, "a start while connecting opens no second socket");
    x.sockets[0].readyState = 1;
    x.sockets[0].onopen();
    assert.equal(x.sockets[0].sent[0].type, "hello");
    assert.equal(x.sockets[0].sent[0].token, "tok-" + "x".repeat(30));
    assert.equal(x.st.windows.size, 0, "it opened no window and no page");
  } finally { x.stop(); }
});

test("a job opens its site's window unfocused, its tab active in that window, and nothing focuses a window", async () => {
  const x = load();
  try {
    const sock = await connected(x);
    const job = { job_id: "vinted-1", site: "vinted", mode: "verify", listing_url: "https://www.vinted.com/items/1" };
    await sock.onmessage({ data: JSON.stringify({ type: "job", job }) });
    await flush(60);
    const made = x.calls.filter(([what]) => what === "windows.create").map(([, o]) => o);
    assert.equal(made.length, 1);
    assert.equal(made[0].focused, false);
    assert.equal(made[0].state, "normal", "never minimized: its job tab would be a hidden page");
    assert.match(made[0].url, /idle\.html\?site=vinted$/);
    const opened = x.calls.filter(([what]) => what === "tabs.create").map(([, o]) => o);
    assert.equal(opened.length, 1);
    assert.equal(opened[0].active, true, "the job's tab is its own window's active tab (a hidden page stalls)");
    assert.equal(opened[0].url, job.listing_url);
    const win = [...x.st.windows.values()][0];
    assert.equal(opened[0].windowId, win.id);
    assert.equal(win.focused, false);
    assert.deepEqual(x.calls.filter(([what]) => what === "windows.update"), [], "no window is ever focused or raised");
    assert.ok(sock.sent.some((m) => m.type === "event" && m.event === "progress" && m.what === "tab opened"));
  } finally { x.stop(); }
});

test("nothing in the worker ever focuses a window or opens a tab of its own accord", () => {
  assert.doesNotMatch(SOURCE, /focused:\s*true/);
  assert.doesNotMatch(SOURCE, /windows\.update\(/);
  assert.doesNotMatch(SOURCE, /state:\s*"minimized"/);
  assert.match(SOURCE, /windows\.create\(\{ url: idle, focused: false, state: "normal" \}\)/);
});

function stage(now) {
  // window 1: the Vinted window (its idle page, the job's tab, a popup the job's page opened, a second idle page);
  // window 2: a window of old pages (a Vinted home page with ads that was its active tab, her eBay page, a Google
  // search), and pages the tidy keeps: one she looked at a minute ago, one born 10 s ago, the login page left for her
  const tabs = [
    { id: 1, windowId: 1, url: `${EXT}idle.html?site=vinted` },
    { id: 2, windowId: 1, url: "https://www.vinted.com/items/new", active: true },
    { id: 3, windowId: 1, url: "https://www.vinted.com/oauth", openerTabId: 2 },
    { id: 4, windowId: 1, url: `${EXT}idle.html?site=vinted` },
    { id: 5, windowId: 2, url: "https://www.vinted.com/", active: true },
    { id: 6, windowId: 2, url: "https://www.ebay.com/sl/sell" },
    { id: 7, windowId: 2, url: "https://www.google.com/search?q=x" },
    { id: 8, windowId: 2, url: "https://www.depop.com/products/create/" },
    { id: 9, windowId: 2, url: "https://www.depop.com/login/" },
    { id: 10, windowId: 2, url: "https://www.vinted.com/member/1" },
  ];
  const x = load({ tabs, windows: [{ id: 1 }, { id: 2 }] });
  x.ev("jobs").vinted = { job_id: "vinted-9", site: "vinted", mode: "publish", tabId: 2, windowId: 1, clicked: false,
                          started: now - MIN, job: {} };
  x.ev("seenAt")[8] = now - MIN;                        // she looked at it a minute ago (logging in?)
  x.ev("bornAt")[10] = now - 10 * 1000;                 // just made: a job's tab between its creation and its job
  x.stores.session.left_open = { depop: { tab: 9, at: now - 5 * MIN } };
  return x;
}

test("the tidy closes every page no running job holds — also while a job runs — whatever its site", async () => {
  const now = Date.now();
  const x = stage(now);
  try {
    await flush();
    await x.ev("tidyTabs")(now, true);
    const closed = x.calls.filter(([what]) => what === "tabs.remove").flatMap(([, ids]) => ids).sort((a, b) => a - b);
    assert.deepEqual(closed, [4, 5, 6, 7], "the second idle page, the old active Vinted page, her eBay page, Google");
    for (const kept of [1, 2, 3, 8, 9, 10]) assert.ok(x.st.tabs.has(kept), `tab ${kept} kept`);
    assert.deepEqual(x.posted.filter((e) => e.event === "tidied"),
                     [{ event: "tidied", closed: 4, kept: 6, tabs: 10, why: { unheld: 4 } }]);   // every run told
  } finally { x.stop(); }
});

test("once the job is over its popup goes; the page left for her goes after 6 hours; a page she left 15 min ago goes",
     async () => {
  const now = Date.now();
  const x = stage(now);
  try {
    await flush();
    delete x.ev("jobs").vinted;                          // the job ended (its own tab closed by finish())
    x.st.tabs.delete(2);
    await x.ev("tidyTabs")(now, true);
    let closed = x.calls.filter(([what]) => what === "tabs.remove").flatMap(([, ids]) => ids);
    assert.ok(closed.includes(3), "the popup the job's page opened");
    assert.ok(!closed.includes(9) && !closed.includes(8));
    const later = now + 6 * 60 * MIN + 1000;
    await x.ev("tidyTabs")(later, true);
    closed = x.calls.filter(([what]) => what === "tabs.remove").flatMap(([, ids]) => ids);
    assert.ok(closed.includes(9), "the login page left for her: 6 hours at most");
    assert.ok(closed.includes(8), "a page she last looked at long ago");
    assert.ok(x.st.tabs.has(1), "the window's idle page stays");
  } finally { x.stop(); }
});

test("a page she is looking at now is never closed under her hands", async () => {
  const now = Date.now();
  const x = load({ tabs: [{ id: 6, windowId: 2, url: "https://www.ebay.com/sl/sell", active: true }],
                   windows: [{ id: 2, focused: true }] });
  try {
    await flush();
    await x.ev("tidyTabs")(now, true);
    assert.deepEqual(x.calls.filter(([what]) => what === "tabs.remove"), []);
    x.st.windows.get(2).focused = false;               // she went back to her own apps
    await x.ev("tidyTabs")(now + UNSEEN_MS + 1000, true);
    assert.deepEqual(x.calls.filter(([what]) => what === "tabs.remove").flatMap(([, ids]) => ids), [6]);
  } finally { x.stop(); }
});

test("no tidy while a job's tab is being opened (its id isn't known yet)", async () => {
  const now = Date.now();
  const x = load({ tabs: [{ id: 6, windowId: 2, url: "https://www.vinted.com/" }], windows: [{ id: 2 }] });
  try {
    await flush();
    x.ev("jobs").depop = { job_id: "depop-1", site: "depop", mode: "publish", tabId: null, started: now, job: {} };
    await x.ev("tidyTabs")(now, true);
    assert.deepEqual(x.calls.filter(([what]) => what === "tabs.remove"), []);
  } finally { x.stop(); }
});

test("each ping says whether a Thrift Chrome window has her focus, and its tabs and windows", async () => {
  const x = load({ tabs: [{ id: 1, windowId: 1, url: `${EXT}idle.html?site=vinted`, active: true }],
                   windows: [{ id: 1 }] });
  try {
    const sock = await connected(x);
    await x.ev("ping")();
    let p = sock.sent.filter((m) => m.type === "ping").at(-1);
    assert.equal(p.using, false);
    assert.equal(p.tabs, 1);
    assert.equal(p.windows, 1);
    x.st.windows.get(1).focused = true;
    await x.ev("ping")();
    p = sock.sent.filter((m) => m.type === "ping").at(-1);
    assert.equal(p.using, true);
  } finally { x.stop(); }
});
