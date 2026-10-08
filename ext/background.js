// Thrift crosslister (WO32, WO32b, WO33) — the background service worker. The link to the bridge inside the poster
// process on this Mac: a WebSocket to ws://127.0.0.1:8765/ext (the token in the first message, a message every 20 s so
// the worker stays up while it is open), tried again 1 s, 2 s, 5 s after it drops and then every 5 s — a bridge that
// starts is seen within seconds — and, for when the worker was put to sleep (MV3 workers go idle), an alarm that asks
// GET /jobs/next. One job at a time PER SITE (WO33: a Depop job and a Vinted job run together), each in its site's own
// window of the Thrift Chrome — created once, kept with a small idle page, never focused or raised over what the owner
// is using — in a tab of its own that is that window's active tab, so no job tab is ever hidden: the job's photos
// fetched from the bridge, the content script started on the page, its events relayed, that window captured on
// request (10 s at most). After the one publish click it watches the tab land on the listing page. A job whose bridge
// goes away before the go-ahead can't be published any more: it is called off and its tab closed. It opens nothing a
// job didn't ask for.
const BRIDGE = "http://127.0.0.1:8765";
const WS_URL = "ws://127.0.0.1:8765/ext";
const PING_MS = 20000;
const OPEN_MS = 45000;
const AFTER_CLICK_MS = 60000;
const BACKOFF = [1000, 2000, 5000];  // reconnect: 1 s, 2 s, 5 s, then every 5 s
const REFUSED_MS = 30000;            // after the bridge refused our token: every 30 s (or at once when it is saved)
const FILL_MS = 4 * 60000;           // a form not filled 4 min after its job came (the bridge gives up at 3): ended
const READY_MS = 15 * 60000;         // a filled form waits this long for the owner's go-ahead
const CLICKED_MS = 5 * 60000;        // after the click: the landing page is reported within a minute; this is the end
const CAPTURE_MS = 10000;            // a capture that doesn't come back (a minimized window isn't drawn): skipped
const ANSWER_MS = 3000;              // one message to the page: answered (or refused) within 3 s, else asked again
const REACH_MS = 20000;              // ... for 20 s in all: a page that never answers ends the job, with what it shows
const QUIET_MS = 30000;              // a page filling a form, silent this long, is asked if it is still there (3 s)
const STOP_PAGES = ["login", "block", "captcha", "verify"];
// The files whose hash tells the bridge's copy (git pull on deploy) from the loaded one: a difference reloads us.
const FILES = ["manifest.json", "background.js", "content/common.js", "content/vinted.js", "content/depop.js",
               "selectors.json", "options.html", "options.js", "idle.html", "idle.js"];

let ws = null;
let wsReady = false;
let attempt = 0;
let refused = false;
let reconnectTimer = null;
let pingTimer = null;
let jobs = {};             // site → the job in hand there: {job_id, site, mode, tabId, windowId, clicked, after, …, job}
let restored = false;
const afterTimers = {};    // site → the after-click timer
let selectorsCache = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const errText = (e) => String(e && e.message ? e.message : e).slice(0, 300);

async function settings() {
  const { token = "" } = await chrome.storage.local.get(["token"]);
  return { token };
}

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(what)), ms); })])
    .finally(() => clearTimeout(timer));
}

let lastState = null;
async function setState(link) {
  if (link === lastState) return;                  // written when it changes (it is asked every few seconds)
  lastState = link;
  await chrome.storage.local.set({ bridge_state: { link, at: new Date().toISOString() } });
}

async function selectors() {
  if (!selectorsCache) selectorsCache = await (await fetch(chrome.runtime.getURL("selectors.json"))).json();
  return selectorsCache;
}

async function remember() {
  await chrome.storage.session.set({ jobs });
}

// The jobs in hand, back after the worker was put to sleep and woken (session storage outlives it).
async function restore() {
  if (restored) return;
  const saved = (await chrome.storage.session.get("jobs")).jobs || {};
  jobs = { ...saved, ...jobs };
  restored = true;
}

const byId = (id) => Object.values(jobs).find((j) => j && j.job_id === id) || null;
const byTab = (tabId) => Object.values(jobs).find((j) => j && j.tabId === tabId) || null;
const anyJob = () => Object.values(jobs).some(Boolean);

async function filesHash() {
  const enc = new TextEncoder();
  const parts = [];
  for (const f of FILES) {
    const bytes = new Uint8Array(await (await fetch(chrome.runtime.getURL(f))).arrayBuffer());
    parts.push(enc.encode(`${f}\n`), bytes, enc.encode("\n"));
  }
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    all.set(p, at);
    at += p.length;
  }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", all));
  return [...digest].map((x) => x.toString(16).padStart(2, "0")).join("");
}

async function noteLoaded() {
  try {
    await chrome.storage.local.set({ loaded_hash: await filesHash() });
  } catch (e) { /* a file being rewritten: the next start records it */ }
}

// The bridge has other files than the ones loaded (a deploy pulled new code): reload, once per version, between jobs.
async function maybeReload(hash) {
  await restore();
  if (!hash || anyJob()) return;
  const { loaded_hash, reloaded_for } = await chrome.storage.local.get(["loaded_hash", "reloaded_for"]);
  if (!loaded_hash || loaded_hash === hash || reloaded_for === hash) return;
  await chrome.storage.local.set({ reloaded_for: hash });
  chrome.runtime.reload();
}

// ---------------------------------------------------------------- the link to the bridge

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const { token } = await settings();
  if (!token) {
    await setState("no token yet: paste it in the extension's options (cat ~/thrift/var/ext_token)");
    return;
  }
  // The files it loaded (their hash): the bridge hands no job for a moment to an extension that is about to reload.
  const { loaded_hash = null } = await chrome.storage.local.get("loaded_hash");
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  let sock;
  try {
    sock = new WebSocket(WS_URL);
  } catch (e) {
    later();
    return;
  }
  ws = sock;
  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token, version: chrome.runtime.getManifest().version,
                                                 loaded: loaded_hash }));
  sock.onmessage = (m) => {
    let msg;
    try {
      msg = JSON.parse(m.data);
    } catch (e) {
      return;
    }
    onBridge(msg);
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    const was = wsReady;
    ws = null;
    wsReady = false;
    clearInterval(pingTimer);
    later();
    if (was) abandon("the bridge went away before the go-ahead (the poster or the CLI stopped)");
  };
  sock.onerror = () => {};
}

function later() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, refused ? REFUSED_MS : BACKOFF[Math.min(attempt, BACKOFF.length - 1)]);
  attempt += 1;
}

async function onBridge(msg) {
  if (msg.type === "welcome") {
    wsReady = true;
    attempt = 0;
    refused = false;
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    }, PING_MS);
    await setState("connected");
    await maybeReload(msg.ext_hash);
  } else if (msg.type === "refused") {
    wsReady = false;
    refused = true;
    await setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
  } else if (msg.type === "job") {
    startJob(msg.job);                               // not awaited: the other site's job may come meanwhile
  } else if (msg.type === "submit" || msg.type === "cancel") {
    await relay(msg);
  }
}

// GET /jobs/next: the way in while the socket is down (the alarm's tick) — one job per free site.
async function poll() {
  if (wsReady) return;
  const { token } = await settings();
  if (!token) return;
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(`${BRIDGE}/jobs/next`, { headers: { "X-Thrift-Token": token } });
      if (r.status === 401) return setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
      if (r.status !== 200) return;
      await setState("polling (the socket is down)");
      startJob(await r.json());
    } catch (e) {
      return setState("the bridge isn't reachable (the poster isn't running, or Depop / Vinted are off)");
    }
  }
}

// To the bridge: over the socket when it is open, else POST /events (its answer carries the job's commands).
async function send(event) {
  if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "event", ...event }));
    return;
  }
  const { token } = await settings();
  for (const wait of [0, 400, 1200]) {   // a refused or reset connection is tried twice more (a busy machine) before
    if (wait) await new Promise((ok) => setTimeout(ok, wait));      // the bridge counts as gone
    try {
      const r = await fetch(`${BRIDGE}/events`, {
        method: "POST", headers: { "X-Thrift-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify(event),
      });
      if (r.ok) {
        const { commands = [] } = await r.json();
        for (const c of commands) await onBridge(c);
      }
      return;
    } catch (e) { /* tried again below */ }
  }
  // nothing listens on the port: the bridge that gave the job is gone
  if (event.job_id) abandon("the bridge went away before the go-ahead (the poster or the CLI stopped)", event.job_id);
}

// The bridge that gave a job went away (a Ctrl+C at the terminal, the poster stopped) before its go-ahead: nothing can
// be published from that form any more, and no one waits for it — the page is told, the tab closed. After the click a
// job stays: its landing page is still reported (the poster looks at the shop when it starts again).
async function abandon(why, jobId = null) {
  await restore();
  for (const j of Object.values(jobs).filter((x) => x && !x.clicked && (!jobId || x.job_id === jobId))) {
    try { await chrome.tabs.sendMessage(j.tabId, { type: "cancel", job_id: j.job_id }); } catch (e) { /* no page yet */ }
    await finish({ event: "error", job_id: j.job_id, stage: "cancelled", page: "unknown", message: why });
  }
}

// While a publish waits for the bridge's go-ahead without a socket: ask every 2 s.
async function waitCommands(jobId) {
  for (let i = 0; i < 180; i++) {
    await sleep(2000);
    await restore();
    const j = byId(jobId);
    if (!j || j.decided || wsReady) return;
    await send({ event: "waiting", job_id: jobId });
  }
}

// ---------------------------------------------------------------- the sites' windows

const idleUrl = (site) => chrome.runtime.getURL(`idle.html?site=${site}`);

// The site's own window (WO33): found by its idle tab (after a browser restart too: the session restores it), else
// created — never focused, so it opens behind what the owner is using. One at a time: two jobs starting together
// would each write the windows map over the other's.
let windowLock = Promise.resolve();
function siteWindow(site) {
  const run = windowLock.then(() => siteWindowNow(site));
  windowLock = run.catch(() => {});
  return run;
}

async function siteWindowNow(site) {
  const idle = idleUrl(site);
  const { windows: saved = {} } = await chrome.storage.session.get("windows");
  const known = saved[site];
  if (known) {
    try {
      const w = await chrome.windows.get(known.windowId);
      if (w && w.type === "normal") return known;
    } catch (e) { /* closed: looked for, else made again */ }
  }
  const found = (await chrome.tabs.query({})).find((t) => t.url === idle);
  const entry = found ? { windowId: found.windowId, idleTabId: found.id }
    : await chrome.windows.create({ url: idle, focused: false }).then((w) => ({ windowId: w.id, idleTabId: w.tabs[0].id }));
  await chrome.storage.session.set({ windows: { ...saved, [site]: entry } });
  return entry;
}

// ---------------------------------------------------------------- one job

const web = (u) => /^https?:/i.test(u || "");

function loaded(tabId, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(on);
      reject(new Error(`the page didn't load in ${ms / 1000} s`));
    }, ms);
    const on = (id, info, tab) => {
      if (id !== tabId || info.status !== "complete" || !web(tab && tab.url)) return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(on);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(on);
    chrome.tabs.get(tabId).then((t) => t.status === "complete" && on(tabId, { status: "complete" }, t)).catch(() => {});
  });
}

// Does the tab start loading another page within `ms`? (A client-side redirect: Vinted's /session-refresh.)
function navigates(tabId, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(on); resolve(false); }, ms);
    const on = (id, info) => {
      if (id !== tabId || info.status !== "loading") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(on);
      resolve(true);
    };
    chrome.tabs.onUpdated.addListener(on);
  });
}

const pathOf = (u) => { try { return new URL(u).pathname.replace(/\/+$/, ""); } catch (e) { return ""; } };

// The page the job is for, loaded: a sell form's job waits through a page that moves on by itself — live, Vinted sent
// /items/new to /session-refresh, which went back to /items/new a second later; the job handed to the refresh page
// died with it, silently (the WO32b stall). Any other page that stays (a login page) is the page script's to name.
async function landed(tabId, target, ms, j) {
  const end = Date.now() + ms;
  for (;;) {
    await loaded(tabId, Math.max(1000, end - Date.now()));
    const tab = await chrome.tabs.get(tabId);
    if (pathOf(tab.url) === pathOf(target) || Date.now() > end) return tab;
    if (!(await navigates(tabId, 2500))) return tab;
    await send({ event: "progress", job_id: j && j.job_id, what: `passed through ${pathOf(tab.url)}` });
  }
}

// The content scripts per site (as the manifest lists them): injected by the worker when the page has none yet.
const SCRIPTS = { vinted: ["content/common.js", "content/vinted.js"], depop: ["content/common.js", "content/depop.js"] };

// What the job's tab is: its load state, address, title, whether Chrome froze or discarded it, and its window
// (minimized?) — said when its page doesn't answer (WO32b: a Vinted page that stalled twice, silently).
async function tabState(tabId) {
  try {
    const t = await chrome.tabs.get(tabId);
    const w = await chrome.windows.get(t.windowId).catch(() => null);
    return { status: t.status, url: t.url, title: t.title, active: t.active, discarded: t.discarded,
             frozen: t.frozen, window: w ? w.state : null, focused: w ? w.focused : null };
  } catch (e) {
    return { gone: errText(e) };
  }
}

// A message to the job's page. Its script comes with the page (the manifest, at document_idle); when it hasn't
// answered after 2 s — a page whose idle comes late — the worker injects it (scripting), once. Each try waits 3 s at
// most: a page whose own code holds its thread (or a dialog) never answers and never refuses — after `ms` the job
// ends with the tab's state and a picture of it (a job asked twice runs once: the page knows its job ids).
async function toTab(tabId, msg, ms = REACH_MS, site = null, j = null) {
  const end = Date.now() + ms;
  let last = "";
  for (let i = 0; ; i++) {
    try {
      return await withTimeout(chrome.tabs.sendMessage(tabId, msg), ANSWER_MS, "no answer in 3 s");
    } catch (e) {
      last = errText(e);
      if (i === 4 && SCRIPTS[site]) {
        try {
          await withTimeout(chrome.scripting.executeScript({ target: { tabId }, files: SCRIPTS[site] }), ANSWER_MS,
                            "the page didn't run the script in 3 s");
        } catch (e2) { last = `${last}; inject: ${errText(e2)}`; }
      }
      if (Date.now() > end) {
        const state = await tabState(tabId);
        if (j) await shootFromWorker("no-answer", j);
        throw new Error(`the page doesn't answer (${last.slice(0, 120)}): ${JSON.stringify(state).slice(0, 400)}`);
      }
      await sleep(500);
    }
  }
}

// A picture of the job's tab taken by the worker itself (the page can't ask for one: it doesn't answer).
async function shootFromWorker(label, j) {
  if (!j || j.tabId == null) return;
  let tab = null;
  try { tab = await chrome.tabs.get(j.tabId); } catch (e) { return; }
  await shoot({ label, html: null, url: tab.url, job_id: j.job_id }, tab);
}

async function b64(blob) {
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(s);
}

async function fetchPhotos(urls) {
  const { token } = await settings();
  const out = [];
  for (const [i, u] of urls.entries()) {
    const r = await fetch(u, { headers: { "X-Thrift-Token": token } });
    if (!r.ok) throw new Error(`photo ${i + 1}: HTTP ${r.status}`);
    const blob = await r.blob();
    out.push({ name: `photo-${i + 1}.jpg`, type: blob.type || "image/jpeg", b64: await b64(blob) });
  }
  return out;
}

function urlFor(job, site) {
  if (job.mode === "verify" || job.mode === "delist" || job.mode === "probe") return job.listing_url;
  if (job.mode === "find") return site.shop_url.replace("{shop}", encodeURIComponent(job.shop || ""));
  return site.sell_url;
}

async function startJob(job) {
  await restore();
  const held = jobs[job.site];
  if (held) {
    if (held.job_id !== job.job_id) {
      await send({ event: "error", job_id: job.job_id, stage: "busy", page: "unknown",
                   message: `another ${job.site} job is running (${held.job_id})` });
    }
    return;
  }
  const sel = await selectors();
  const site = sel[job.site];
  if (!site) {
    await send({ event: "error", job_id: job.job_id, stage: "open", page: "unknown", message: `unknown site ${job.site}` });
    return;
  }
  jobs[job.site] = { job_id: job.job_id, site: job.site, mode: job.mode, tabId: null, windowId: null, clicked: false,
                     after: false, decided: false, delivered: false, started: Date.now(), job };
  await remember();
  // Still this job? (A cancel can come while the tab opens: it is ended before the page has it.)
  const ours = async () => { await restore(); const j = jobs[job.site]; return j && j.job_id === job.job_id ? j : null; };
  try {
    const photos = job.mode === "dry_run" || job.mode === "publish" ? await fetchPhotos(job.photos || []) : [];
    const url = urlFor(job, site);
    const where = await siteWindow(job.site);
    const tab = await chrome.tabs.create({ url, active: true, windowId: where.windowId });
    let j = await ours();
    if (!j) {                                      // ended meanwhile: the tab it opened goes too
      try { await chrome.tabs.remove(tab.id); } catch (e) { /* gone */ }
      return;
    }
    j.tabId = tab.id;
    j.windowId = tab.windowId;
    await remember();
    await send({ event: "progress", job_id: job.job_id, what: "tab opened" });
    await landed(tab.id, url, OPEN_MS, j);
    j = await ours();
    if (j && j.decided) throw new Error("called off before the form opened");
    await send({ event: "progress", job_id: job.job_id, what: "page loaded" });
    await toTab(tab.id, { type: "job", job, selectors: sel, photos, pace: job.pace }, REACH_MS, job.site, j);
    await send({ event: "progress", job_id: job.job_id, what: "page script running" });
    j = await ours();
    if (j) {
      j.delivered = true;
      await remember();
      if (j.decided && !j.clicked) {               // the cancel came while the page was being reached: told now
        try { await chrome.tabs.sendMessage(tab.id, { type: "cancel", job_id: job.job_id }); } catch (e) { /* gone */ }
      }
    }
  } catch (e) {
    await finish({ event: "error", job_id: job.job_id, stage: "open", page: "unknown", message: errText(e) });
  }
}

// The bridge's go-ahead (submit) or no (cancel) for a filled form; after submit, the landing page is watched.
async function relay(msg) {
  await restore();
  const j = byId(msg.job_id);
  if (!j || j.decided) return;
  j.decided = true;
  if (msg.type === "submit") {
    j.clicked = true;
    j.clickedAt = Date.now();
    clearTimeout(afterTimers[j.site]);
    afterTimers[j.site] = setTimeout(() => afterClickTimeout(j.job_id), AFTER_CLICK_MS);
  }
  await remember();
  if (msg.type === "cancel" && !j.delivered) return;   // the page hasn't the job yet: startJob ends it / tells it
  try {
    await toTab(j.tabId, { type: msg.type, job_id: j.job_id }, 6000);
  } catch (e) {
    await finish({ event: "error", job_id: j.job_id, stage: msg.type, page: "unknown", message: errText(e) });
  }
}

// No listing page a minute after the click: whatever the tab shows is reported (the bridge then checks the shop).
async function afterClickTimeout(jobId) {
  await restore();
  const j = byId(jobId);
  if (!j || !j.clicked || j.after) return;
  j.after = true;
  await remember();
  try {
    await toTab(j.tabId, { type: "job", job: { ...j.job, mode: "after_publish" }, selectors: await selectors() },
                REACH_MS, j.site, j);
  } catch (e) {
    await finish({ event: "error", job_id: j.job_id, stage: "after_publish", page: "unknown", message: errText(e) });
  }
}

// A job's page navigated while it was being filled (a session refresh, a reload): its script — and the job with it —
// is gone. Before the go-ahead nothing can have been published: the job is handed to the new page (twice at most),
// except a form already waiting for the owner's POST (never filled again behind them: ended, nothing published) and a
// take-down (its clicks are never repeated).
async function onNavigatedMidJob(j) {
  if (j.readyAt || j.mode === "delist" || (j.reloads || 0) >= 2) {
    const why = j.readyAt ? "the page reloaded while it waited for POST — nothing was published"
      : j.mode === "delist" ? "the page reloaded during the take-down" : "the page keeps reloading";
    return finish({ event: "error", job_id: j.job_id, stage: "reloaded", page: "unknown", message: why });
  }
  j.delivered = false;
  j.reloads = (j.reloads || 0) + 1;
  await remember();
  try {
    const sel = await selectors();
    const tab = await landed(j.tabId, urlFor(j.job, sel[j.site]), OPEN_MS, j);
    await send({ event: "progress", job_id: j.job_id, what: `the page reloaded (${pathOf(tab.url)}) — filling it again` });
    const photos = j.job.mode === "dry_run" || j.job.mode === "publish" ? await fetchPhotos(j.job.photos || []) : [];
    await toTab(j.tabId, { type: "job", job: j.job, selectors: sel, photos, pace: j.job.pace }, REACH_MS, j.site, j);
    await restore();
    const now = byId(j.job_id);
    if (now) {
      now.delivered = true;
      await remember();
    }
  } catch (e) {
    await finish({ event: "error", job_id: j.job_id, stage: "reloaded", page: "unknown", message: errText(e) });
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  await restore();
  const j = byTab(tabId);
  if (!j) return;
  if (j.delivered && !j.clicked && !j.decided && info.status === "loading") return onNavigatedMidJob(j);
  if (!j.clicked || j.after) return;
  if (info.status !== "complete" && !info.url) return;
  const sel = await selectors();
  const conf = sel[j.site] || {};
  const listing = new RegExp(conf.listing_url).test(tab.url || "");
  // WO33: Vinted's Upload lands on the seller's member page ("Item listed": Later is pressed there, then the shop check)
  const landing = !!conf.landing_url && new RegExp(conf.landing_url).test(tab.url || "");
  if (!listing && !(landing && info.status === "complete")) return;
  j.after = true;
  await remember();
  clearTimeout(afterTimers[j.site]);
  await sleep(2500);
  try {
    await toTab(tabId, { type: "job", job: { ...j.job, mode: "after_publish" }, selectors: sel }, REACH_MS, j.site, j);
  } catch (e) {
    await finish({ event: "result", job_id: j.job_id, url: tab.url, live: null, note: errText(e) });
  }
});

// A job's last word: to the bridge, then its tab closed (its window stays, on its idle page) — except a login /
// CAPTCHA / block page, left open for the owner (a human may log in or solve it there; the code never does).
async function finish(ev) {
  await restore();
  const j = byId(ev.job_id);
  if (j) {
    delete jobs[j.site];
    clearTimeout(afterTimers[j.site]);
    await remember();
  }
  await send(ev);
  if (j && j.tabId != null && !(ev.event === "error" && STOP_PAGES.includes(ev.page))) {
    await sleep(1500);
    try {
      await chrome.tabs.remove(j.tabId);
    } catch (e) { /* closed already */ }
  }
  if (!wsReady) await poll();
}

// Chrome allows captureVisibleTab twice a second (MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND): captures are spaced, and
// one refused for it is tried again. Each job's window is captured on its own (WO33: two jobs, two pictures).
let lastCapture = 0;

async function shoot(msg, tab) {
  await restore();
  const j = (msg.job_id && byId(msg.job_id)) || (tab && byTab(tab.id));
  if (!j) return;
  let png_b64 = null;
  let error = null;
  for (let attempt = 0; attempt < 3 && !png_b64; attempt++) {
    const wait = Math.max(0, lastCapture + 600 - Date.now());
    lastCapture = Date.now() + wait;
    await sleep(wait);
    try {
      if (tab && !tab.active) await chrome.tabs.update(tab.id, { active: true });
      const dataUrl = await withTimeout(chrome.tabs.captureVisibleTab(tab ? tab.windowId : j.windowId,
                                                                      { format: "png" }), CAPTURE_MS,
                                        "no picture in 10 s (is the Thrift Chrome window minimized?)");
      png_b64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
      error = null;
    } catch (e) {
      error = errText(e);
      if (!/MAX_CAPTURE|quota/i.test(error)) break;
    }
  }
  await send({ event: "screenshot", job_id: j.job_id, label: msg.label, png_b64, html: msg.html, url: msg.url, error });
}

// The watchdog: a page whose own script holds its thread can't run our step limits (they live in that page) — a job
// whose page has said nothing for 30 s is asked if it is still there; no answer in 3 s ends it, with the tab's state
// (a page busy waiting on its photos still answers).
async function watchdog() {
  await restore();
  const now = Date.now();
  for (const j of Object.values(jobs).filter((x) => x && x.delivered && !x.clicked && !x.readyAt && !x.decided)) {
    if (now - (j.heardAt || j.started || now) < QUIET_MS) continue;
    try {
      await withTimeout(chrome.tabs.sendMessage(j.tabId, { type: "ping" }), ANSWER_MS, "no answer in 3 s");
      j.heardAt = Date.now();
    } catch (e) {
      const state = await tabState(j.tabId);
      await shootFromWorker("no-answer", j);
      await finish({ event: "error", job_id: j.job_id, stage: "hung", page: "unknown",
                     message: `the page doesn't answer (silent ${Math.round((now - (j.heardAt || j.started)) / 1000)} s while filling — `
                       + `its own script holds it?): ${JSON.stringify(state).slice(0, 400)}` });
    }
  }
}
setInterval(() => { watchdog().catch(() => {}); }, 5000);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    const heard = sender && sender.tab ? byTab(sender.tab.id) : null;
    if (heard) heard.heardAt = Date.now();
    if (msg.type === "event") {
      const ev = { ...msg };
      delete ev.type;
      if (ev.event === "result" || ev.event === "error") return finish(ev);
      if (ev.event === "ready") {
        await restore();
        const j = byId(ev.job_id);
        if (j) {
          j.readyAt = Date.now();
          await remember();
        }
      }
      await send(ev);
      if (ev.event === "ready" && !wsReady) waitCommands(ev.job_id);
    } else if (msg.type === "screenshot") {
      await shoot(msg, sender.tab);
    } else if (msg.type === "token-saved") {
      attempt = 0;
      refused = false;
      if (ws) ws.close();
      await connect();
    }
  })().catch(() => {}).finally(() => sendResponse({ ok: true }));
  return true;
});

// ---------------------------------------------------------------- waking up

// The alarm: every 30 s (Chrome's least for an installed extension); the Thrift Chrome loads this one unpacked, which
// Chrome lets wake every 5 s — a worker put to sleep while no bridge ran is then back within ~5 s of one starting
// (live, WO32b: 18 s on the 30 s alarm). `poll_minutes` in storage overrides both.
async function ensureAlarm() {
  const stored = (await chrome.storage.local.get("poll_minutes")).poll_minutes;
  let poll_minutes = stored;
  if (!poll_minutes) {
    let unpacked = false;
    try { unpacked = (await chrome.management.getSelf()).installType === "development"; } catch (e) { /* packed */ }
    poll_minutes = unpacked ? 5 / 60 : 0.5;
  }
  const alarm = await chrome.alarms.get("poll");
  if (!alarm || alarm.periodInMinutes !== poll_minutes) {
    await chrome.alarms.create("poll", { periodInMinutes: poll_minutes, delayInMinutes: poll_minutes });
  }
}

// A job past its time: still filling 4 min after it came, a filled form left 15 min without a go-ahead, or 5 min after
// the click.
function overdue(c, now) {
  if (c.clicked) return now - (c.clickedAt || c.started || 0) > CLICKED_MS ? "5 minutes after the click" : null;
  if (c.readyAt) return now - c.readyAt > READY_MS ? "no go-ahead in 15 minutes" : null;
  return now - (c.started || 0) > FILL_MS ? "the form wasn't filled in 4 minutes" : null;
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "poll") return;
  await restore();
  const now = Date.now();
  for (const j of Object.values(jobs).filter(Boolean)) {
    const late = overdue(j, now);
    if (late) await finish({ event: "error", job_id: j.job_id, stage: "stale", page: "unknown", message: late });
  }
  await connect();
  await poll();
});

// The owner closed a job's tab (or its window): the job ends there (nothing more can happen in it).
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await restore();
  const j = byTab(tabId);
  if (j) {
    await finish({ event: "error", job_id: j.job_id, stage: "tab_closed", page: "unknown",
                   message: "the job's tab was closed" });
  }
});

chrome.runtime.onInstalled.addListener(() => noteLoaded());
chrome.runtime.onStartup.addListener(() => noteLoaded());
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.poll_minutes) ensureAlarm();
});

ensureAlarm();
connect();
