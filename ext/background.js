// Thrift crosslister (WO32, WO32b, WO33, WO54) — the background service worker. The link to the bridge inside the poster
// process on this Mac: a WebSocket to ws://127.0.0.1:8765/ext (the token in the first message, a message every 20 s so
// the worker stays up while it is open), tried again 1 s, 2 s, 5 s after it drops and then every 5 s — a bridge that
// starts is seen within seconds — and, for when the worker was put to sleep (MV3 workers go idle), an alarm that asks
// GET /jobs/next. One job at a time PER SITE (WO33: a Depop job and a Vinted job run together), each in its site's own
// window of the Thrift Chrome — created once per start of it, kept with a small idle page, created unfocused and never
// focused or raised over what the owner is using — in a tab of its own that is that window's active tab, so no job tab
// is ever hidden: the job's photos fetched from the bridge, the content script started on the page, its events
// relayed, that window captured on request (10 s at most). After the one publish click it watches the tab land on the
// listing page. A job whose bridge goes away before the go-ahead can't be published any more: it is called off and its
// tab closed. It opens nothing a job didn't ask for.
//
// WO54 §4 (live 2026-11-09: started at 1 AM with --restore-last-session and a Vinted start page, the Thrift Chrome held
// 9.6 GB in 51 pages on her 8 GB laptop — her old eBay pages, Vinted pages with ad frames — two of them burning a core
// each until 9:35 AM; the tidy kept every window's active tab and every page off the three sites):
// - The Thrift Chrome is not her browser. launchd starts it with NO window (--no-startup-window) only when the poster
//   has a job for it, and the poster quits it after 10 idle minutes. This worker starts with the profile — its
//   runtime.onStartup listener and its alarm, both below — and connects at once (the connect() at the end).
// - A job's tab stays its window's ACTIVE tab (the window itself is never focused): an inactive tab is a hidden page —
//   its timers throttled to one a second, then one a minute, its animation frames stopped, nothing for
//   captureVisibleTab — and a hidden page stalled a live run (WO32b). The window opens behind what she is using.
// - The tidy (every minute, also while jobs run, and after each job) closes every page no running job holds, except
//   each window's idle page, the one page per site left for her to log in or solve a CAPTCHA (6 h at most), and a page
//   she looked at in the last 15 minutes (the active tab of a focused Thrift Chrome window).
// - Each ping says whether a Thrift Chrome window has her focus (the poster never quits it under her hands) and how
//   many tabs and windows it holds (thrift doctor).
//
// WO56, human steps (a sign-in, a one-time code, a real CAPTCHA, a "verify you are human" page that stayed — the agent
// never types a password and never solves a CAPTCHA): the bridge says which sites have one pending ({type: "steps"}).
// Each one's page — the page left for the owner when its job met it — is HELD: moved into a window of its own (never a
// site's window, so no job's tab ever opens in it), titled "<Site> — human step" (the page's title prefixed, re-applied
// by the content script), never tidied, read every 10 s ({event: "step_page", site, page} to the bridge, which decides
// when it cleared). The owner's "show" ({type: "show"}) brings THAT window to the front — the only window this worker
// ever focuses — for 5 minutes, then it is minimized (a window can't be sent behind others from here: focused: false
// raises the next Chrome window instead); once the step cleared its tab is closed, window and all, and the next job
// opens its tab in the site's own unfocused window as before. A minimized window is never a job's: its page would be a
// hidden one (throttled timers, nothing to capture — the WO32b stall), and siteWindow() only ever reuses a window by
// its idle page, which a step's window never has. A job's page that meets a "Just a moment" waits it out there (2 min
// at most, the content script); it gets that much more time, and a navigation as it passes is no reload.
const BRIDGE = "http://127.0.0.1:8765";
const WS_URL = "ws://127.0.0.1:8765/ext";
const PING_MS = 20000;
const OPEN_MS = 45000;
const AFTER_CLICK_MS = 180000;      // WO35: up to 3 min for the landing (live: a Depop listing showed up late)
const BACKOFF = [1000, 2000, 5000];  // reconnect: 1 s, 2 s, 5 s, then every 5 s
const REFUSED_MS = 30000;            // after the bridge refused our token: every 30 s (or at once when it is saved)
const FILL_MS = 4 * 60000;           // a form not filled 4 min after its job came (the bridge gives up at 3): ended
const READY_MS = 15 * 60000;         // a filled form waits this long for the owner's go-ahead
const CLICKED_MS = 5 * 60000;        // after the click: the landing page is reported within a minute; this is the end
const CAPTURE_MS = 10000;            // a capture that doesn't come back (a minimized window isn't drawn): skipped
const ANSWER_MS = 3000;              // one message to the page: answered (or refused) within 3 s, else asked again
const REACH_MS = 20000;              // ... for 20 s in all: a page that never answers ends the job, with what it shows
const QUIET_MS = 30000;              // a page filling a form, silent this long, is asked if it is still there (3 s)
const STOP_PAGES = ["login", "block", "captcha", "verify", "challenge"];   // WO56: a "Just a moment" that stayed
const CHALLENGE_MS = 120000;         // WO56: a job's page waits out a "Just a moment" this long: the job gets as much more
const STEP_EVERY = 10000;            // WO56: a human step's held page is read this often
const SHOW_MS = 5 * 60000;           // WO56: "show": the step's window in front this long at most, then minimized
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
// WO56b: the job ids just handed to us that startJob hasn't put in `jobs` yet (it does after two awaits) — they hold a
// reload off like a job in hand (live 2026-11-09 10:32 PM: the reload check saw "no job" with two just handed: lost)
const pending = new Set();
const holdingIds = () => [...new Set([...Object.values(jobs).filter(Boolean).map((j) => j.job_id), ...pending])];
let bridgeHash = null;      // WO56b: the bridge's files hash from its last word (a reload a job held off waits for it)

const HASH_SALT = "wo56-reload-2\n";      // WO56b: changed with bridge.HASH_SALT — every loaded copy reloads once
async function filesHash() {
  const enc = new TextEncoder();
  const parts = [enc.encode(HASH_SALT)];      // WO52: the same salt as bridge.files_hash
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

// The hash of the files this copy loaded, recorded ONCE per load — a Chrome start, an install, a reload (session storage
// starts empty then) — before this worker's first hello, poll or reload check: they all wait for it (WO56b). Live
// 2026-11-09 10:32 PM: the Chrome started for two jobs, its hello and its reload check read the LAST session's hash
// (onStartup wrote the new one a moment later), and it reloaded itself with both jobs just handed — lost, 3 minutes
// each. A wake of this worker (MV3 puts it to sleep) keeps what its load recorded: the content scripts are that load's.
const noting = (async () => {
  try {
    if ((await chrome.storage.session.get("noted")).noted) return;
    // WO53: a fresh load (a deploy's reload, a Chrome start) tidies at the next alarm — the pages a restored session
    // brought back go at once, not 30 minutes later
    await chrome.storage.local.set({ loaded_hash: await filesHash(), tidied_at: 0 });
    await chrome.storage.session.set({ noted: true });
  } catch (e) { /* a file being rewritten: the next start records it */ }
})();

async function noteLoaded() {
  await noting;
}

// The bridge has other files than the ones loaded (a deploy pulled new code): reload, once per version, between jobs —
// never with a job in hand or just handed (WO56b); one a job held off comes when that job ends (finish).
async function maybeReload(hash) {
  if (hash) bridgeHash = hash;
  await noting;
  await restore();
  if (!hash || anyJob() || pending.size) return;
  const { loaded_hash, reloaded_for = null } = await chrome.storage.local.get(["loaded_hash", "reloaded_for"]);
  if (!loaded_hash || loaded_hash === hash || reloaded_for === hash) return;
  await chrome.storage.local.set({ reloaded_for: hash });
  if (anyJob() || pending.size) {                  // a job came while the storage answered: it holds the reload off
    await chrome.storage.local.set({ reloaded_for });
    return;
  }
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
  await noting;                                    // WO56b: this load's own hash, never the last session's
  await restore();                                 // the jobs it holds: the hello names them
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
  // WO56b: `holding` — a job handed to an earlier copy of us (one that reloaded) that this one doesn't hold is lost: the
  // bridge hands it again
  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token, version: chrome.runtime.getManifest().version,
                                                 loaded: loaded_hash, holding: holdingIds() }));
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
    if (was) abandon("the bridge went away before the go-ahead (the poster stopped, or the Mac slept)");
  };
  sock.onerror = () => {};
}

function later() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, refused ? REFUSED_MS : BACKOFF[Math.min(attempt, BACKOFF.length - 1)]);
  attempt += 1;
}

// WO54 §4: what each ping tells the poster — whether one of this Chrome's windows has the owner's focus (she is using
// it: it isn't quit under her hands) and how many tabs and windows it holds.
async function linkInfo() {
  const out = { using: false, tabs: null, windows: null };
  try {
    const all = await chrome.windows.getAll({});
    out.windows = all.length;
    out.using = all.some((w) => w.focused);
  } catch (e) { /* no window at all */ }
  try { out.tabs = (await chrome.tabs.query({})).length; } catch (e) { /* none */ }
  if (out.using) await lookedAt();
  return out;
}

async function ping() {
  let info = {};
  try { info = await linkInfo(); } catch (e) { /* the ping goes without it */ }
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping", ...info }));
}

async function onBridge(msg) {
  if (msg.type === "welcome") {
    wsReady = true;
    attempt = 0;
    refused = false;
    clearInterval(pingTimer);
    pingTimer = setInterval(() => { ping().catch(() => {}); }, PING_MS);
    await setState("connected");
    ping().catch(() => {});                          // WO54 §4: its tabs and the owner's focus, at once
    await maybeReload(msg.ext_hash);
  } else if (msg.type === "refused") {
    wsReady = false;
    refused = true;
    await setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
  } else if (msg.type === "job") {
    take(msg.job);                                   // not awaited: the other site's job may come meanwhile
  } else if (msg.type === "submit" || msg.type === "cancel") {
    await relay(msg);
  } else if (msg.type === "answer") {                // WO52 §2: the Mac named a field the page couldn't find
    await restore();
    const j = byId(msg.job_id);
    if (j && j.tabId) {
      try { await toTab(j.tabId, msg, 6000); } catch (e) { /* the page asks again or its step fails as before */ }
    }
  } else if (msg.type === "steps") {                 // WO56: the human steps pending now — their pages held
    await setSteps(msg.steps || {});
  } else if (msg.type === "show") {                  // WO56: the owner's "show" — that step's window to the front
    await showStep(msg.site, msg.ms || SHOW_MS);
  } else if (msg.type === "check_steps") {
    await checkSteps(Date.now(), true);
  }
}

// A job handed to us (the socket, or GET /jobs/next): held from this moment — before startJob's first await — so no
// reload loses it (WO56b).
function take(job) {
  const id = job && job.job_id;
  if (id) pending.add(id);
  startJob(job).catch(() => {}).finally(() => { if (id) pending.delete(id); });
}

// GET /jobs/next: the way in while the socket is down (the alarm's tick) — one job per free site. WO56b: it says the
// files this copy loaded (X-Thrift-Loaded): the bridge hands no job to a copy about to reload — it reloads instead.
async function poll() {
  if (wsReady) return;
  const { token } = await settings();
  if (!token) return;
  await noting;
  const { loaded_hash = "" } = await chrome.storage.local.get("loaded_hash");
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(`${BRIDGE}/jobs/next`, { headers: { "X-Thrift-Token": token,
                                                                 "X-Thrift-Loaded": loaded_hash || "" } });
      if (r.status === 401) return setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
      const theirs = r.headers && r.headers.get ? r.headers.get("X-Thrift-Hash") : null;
      if (theirs && theirs !== loaded_hash) return maybeReload(theirs);     // new files: reload, no job meanwhile
      if (r.status !== 200) return;
      await setState("polling (the socket is down)");
      take(await r.json());
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
  if (event.job_id) abandon("the bridge went away before the go-ahead (the poster stopped, or the Mac slept)", event.job_id);
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

// The site's own window (WO33): found by its idle tab, else created — unfocused (WO54 §4: `focused: false` and nothing
// here ever focuses a window, so it opens behind what the owner is using and stays there), a normal window (never
// minimized: its job tab would be a hidden page). The idle page keeps the window between this Chrome's jobs; the Chrome
// itself quits 10 idle minutes after the last one, and nothing restores a session. One at a time: two jobs starting
// together would each write the windows map over the other's.
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
    : await chrome.windows.create({ url: idle, focused: false, state: "normal" })
      .then((w) => ({ windowId: w.id, idleTabId: w.tabs[0].id }));
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
  if (["cover", "price", "fields", "photos", "text"].includes(job.mode)) return job.edit_url;   // WO37/41/43/45/50: edit page
  if (job.mode === "orders") return job.orders_url;                  // WO38: the sold orders, read only
  if (["verify", "delist", "probe", "practice", "delete"].includes(job.mode)) return job.listing_url;
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
  pending.delete(job.job_id);                      // WO56b: `jobs` holds it from here (a reload waits for its end)
  await remember();
  // Still this job? (A cancel can come while the tab opens: it is ended before the page has it.)
  const ours = async () => { await restore(); const j = jobs[job.site]; return j && j.job_id === job.job_id ? j : null; };
  try {
    const photos = ["dry_run", "publish", "photos"].includes(job.mode) ? await fetchPhotos(job.photos || []) : [];
    const url = urlFor(job, site);
    const where = await siteWindow(job.mode === "orders" ? "orders" : job.site);   // WO38: the "Orders" window
    // WO54 §4: active in ITS OWN window (that window is never focused): an inactive tab is a hidden page — throttled
    // timers, no animation frames, nothing to capture — which stalled a live run (WO32b)
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
// except a form already waiting for the owner's POST (never filled again behind them: ended, nothing published). A
// Vinted take-down's (WO40 — the UGG's died on Vinted's /session-refresh; Depop's delete never) is handed again, with
// `pressed` when its Hide was already clicked: then the new page only says whether it took (a click never repeated).
async function onNavigatedMidJob(j) {
  // WO56: a "Just a moment" that passed moves its page on by itself (the site's own page now): no reload counted,
  // nothing was pressed yet — the job is handed to it
  const passed = !!j.challengeAt && !j.readyAt;
  if (passed) await challengePassed(j);
  if (!passed && (j.readyAt || (j.mode === "delist" && j.site !== "vinted") || j.mode === "delete"
                  || (j.reloads || 0) >= 2)) {
    // WO52 follow-up: a delete is never handed again (no second press) — the driver reads the listing's address
    const why = j.readyAt ? "the page reloaded while it waited for POST — nothing was published"
      : j.mode === "delete" ? "the page reloaded during the delete"
      : j.mode === "delist" ? "the page reloaded during the take-down" : "the page keeps reloading";
    return finish({ event: "error", job_id: j.job_id, stage: "reloaded", page: "unknown", message: why });
  }
  j.delivered = false;
  if (!passed) j.reloads = (j.reloads || 0) + 1;
  await remember();
  try {
    const sel = await selectors();
    const tab = await landed(j.tabId, urlFor(j.job, sel[j.site]), OPEN_MS, j);
    await send({ event: "progress", job_id: j.job_id, what: `the page reloaded (${pathOf(tab.url)}) — ` +
                 (j.mode === "delist" ? "the take-down handed to it again" : "filling it again") });
    const photos = ["dry_run", "publish", "photos"].includes(j.job.mode) ? await fetchPhotos(j.job.photos || []) : [];
    // the page learns how many reloads it has had: Vinted's photo-upload error asks for a fresh page twice at most
    const job = j.mode === "delist" ? { ...j.job, pressed: !!j.pressed } : { ...j.job, reloads: j.reloads };
    await toTab(j.tabId, { type: "job", job, selectors: sel, photos, pace: j.job.pace }, REACH_MS, j.site, j);
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
  } else if (j && j.tabId != null) {
    await leaveForOwner(j.site, j.tabId);
  }
  if (!wsReady) await poll();
  tidyTabs(Date.now(), true).catch(() => {});      // WO54 §4: whatever the job's pages left behind goes now
  // WO56b: a reload this job held off (the bridge has new files) comes now, between jobs — the job's last word sent
  if (bridgeHash) setTimeout(() => { maybeReload(bridgeHash).catch(() => {}); }, 1500);
}

// WO53 (live 2026-11-08): a paused site's test run met the login page every ~5 s and each left a new login tab open
// for the owner — ~80 pages, the Thrift Chrome at 5.7 GB, the Mac swapping until its watchdog panicked at 11:16 PM.
// A site keeps ONE page left for the owner: the newer replaces the older — never the tab she is on. WO54 §4: kept with
// the time it was left (the tidy closes it after LEFT_FOR), in session storage (tab ids don't outlive a Chrome).
const leftTab = (e) => (e && typeof e === "object" ? e.tab : e);
const leftAt = (e) => (e && typeof e === "object" ? e.at || 0 : 0);
async function leaveForOwner(site, tabId) {
  const { left_open: left = {} } = await chrome.storage.session.get("left_open");
  const old = leftTab(left[site]);
  if (old != null && old !== tabId) {
    try {
      const t = await chrome.tabs.get(old);
      if (t && !t.active) await chrome.tabs.remove(old);
    } catch (e) { /* gone already */ }
  }
  left[site] = { tab: tabId, at: Date.now() };
  await chrome.storage.session.set({ left_open: left });
}

// ---------------------------------------------------------------- WO56: human steps

async function challengePassed(j) {
  const seconds = Math.round((Date.now() - (j.challengeAt || Date.now())) / 1000);
  j.challengeAt = null;
  await remember();
  await send({ event: "progress", job_id: j.job_id, what: `the security check passed by itself in ${seconds} s`,
               challenge: "cleared", seconds });
  await send({ event: "challenge", site: j.site, state: "cleared", seconds, mode: j.mode });
}

async function stepState() {
  const { steps = {}, step_tabs: tabs = {}, left_open: left = {} } =
    await chrome.storage.session.get(["steps", "step_tabs", "left_open"]);
  return { steps, tabs, left };
}

// The bridge's word on the pending steps: a site no longer listed has cleared — its page goes (window and all).
async function setSteps(steps) {
  await chrome.storage.session.set({ steps });
  const { tabs } = await stepState();
  for (const site of Object.keys(tabs)) {
    if (!steps[site]) await releaseStep(site);
  }
  await adoptSteps();
  await checkSteps(Date.now(), true);
}

// Each pending step's page: the page left for the owner on that site, moved into a window of its own — never a site's
// window (one with an idle page, or recorded as one), so no job's tab ever opens beside it, even while it is minimized.
async function adoptSteps() {
  const { steps, tabs, left } = await stepState();
  const { windows: saved = {} } = await chrome.storage.session.get("windows");
  let changed = false;
  for (const site of Object.keys(steps)) {
    const tabId = leftTab(left[site]);
    if (tabId == null || (tabs[site] && tabs[site].tab === tabId)) continue;
    let tab = null;
    try { tab = await chrome.tabs.get(tabId); } catch (e) { continue; }       // gone: reported as such
    if (tabs[site] && tabs[site].tab !== tabId) {
      // a newer page of the same step (another job met it) replaces the older one — never the one in front of the
      // owner now (shown, or its window focused): then the newer one goes
      let shownNow = tabs[site].shown_until > Date.now();
      try {
        const old = await chrome.tabs.get(tabs[site].tab);
        const w = await chrome.windows.get(old.windowId).catch(() => null);
        shownNow = shownNow || !!(w && w.focused);
      } catch (e) { shownNow = false; }                                       // the older one is gone
      if (shownNow) {
        try { await chrome.tabs.remove(tabId); } catch (e) { /* gone already */ }
        left[site] = { tab: tabs[site].tab, at: Date.now(), step: true };
        changed = true;
        continue;
      }
      try { await chrome.tabs.remove(tabs[site].tab); } catch (e) { /* gone already */ }
    }
    let windowId = tab.windowId;
    const beside = (await chrome.tabs.query({ windowId })).filter((t) => t.id !== tabId);
    const siteWin = Object.values(saved).some((e) => e && e.windowId === windowId);
    if (beside.length || siteWin) {
      const w = await chrome.windows.create({ tabId, focused: false, state: "normal" });
      windowId = w.id;
    }
    tabs[site] = { tab: tabId, window: windowId, shown_until: 0, at: Date.now() };
    left[site] = { tab: tabId, at: Date.now(), step: true };
    changed = true;
  }
  if (changed) await chrome.storage.session.set({ step_tabs: tabs, left_open: left });
}

// Every STEP_EVERY: each held page read (its kind, by the page's own classifier; the window's title re-applied) and
// reported — the bridge decides when it cleared. A shown window past its 5 minutes is minimized again.
let lastStepCheck = 0;
async function checkSteps(now = Date.now(), force = false) {
  if (!force && now - lastStepCheck < STEP_EVERY - 500) return;
  lastStepCheck = now;
  const { steps } = await stepState();
  if (!Object.keys(steps).length) return;
  await adoptSteps();
  const { tabs } = await stepState();
  const sel = await selectors();
  for (const site of Object.keys(steps)) {
    const mine = tabs[site];
    const report = { event: "step_page", site, page: "gone", url: null, held: false, shown: false, window: null };
    let tab = null;
    if (mine) {
      try { tab = await chrome.tabs.get(mine.tab); } catch (e) { tab = null; }
    }
    if (tab) {
      const w = await chrome.windows.get(tab.windowId).catch(() => null);
      Object.assign(report, { held: true, window: w ? w.state : null, shown: !!(w && w.focused) });
      try {
        const r = await withTimeout(chrome.tabs.sendMessage(tab.id, { type: "classify", site, selectors: sel,
                                                                      title: steps[site].label }),
                                    ANSWER_MS, "no answer in 3 s");
        report.page = (r && r.page) || "unreadable";
        report.url = pathOf(r && r.url);
      } catch (e) {
        report.page = tab.status === "loading" ? "loading" : "unreadable";
        if (tab.status !== "loading" && SCRIPTS[site]) {         // a page with no script of ours yet: given one
          try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: SCRIPTS[site] }); } catch (e2) {}
        }
      }
      if (mine.shown_until && now > mine.shown_until) await hideStep(site);
    } else if (mine) {
      delete tabs[site];
      await chrome.storage.session.set({ step_tabs: tabs });
    }
    await send(report);
  }
}

// The owner's "show": THAT step's window to the front on the Mac (the only window this worker ever focuses), for `ms`.
// Its page gone (the Chrome restarted, the tab closed): the site's page opened again for the person at the screen.
async function showStep(site, ms = SHOW_MS) {
  await adoptSteps();
  const { steps, tabs, left } = await stepState();
  if (!site || !steps[site]) return;
  let mine = tabs[site];
  let tab = null;
  if (mine) {
    try { tab = await chrome.tabs.get(mine.tab); } catch (e) { tab = null; }
  }
  if (!tab) {
    const sel = await selectors();
    const w = await chrome.windows.create({ url: sel[site].sell_url, focused: true, state: "normal" });
    mine = { tab: w.tabs[0].id, window: w.id, at: Date.now() };
    left[site] = { tab: mine.tab, at: Date.now(), step: true };
  } else {
    await chrome.windows.update(tab.windowId, { focused: true, state: "normal" });
    if (!tab.active) await chrome.tabs.update(tab.id, { active: true });
    mine.window = tab.windowId;
  }
  mine.shown_until = Date.now() + ms;
  tabs[site] = mine;
  await chrome.storage.session.set({ step_tabs: tabs, left_open: left });
  await send({ event: "step_shown", site, until: mine.shown_until });
}

// Back to hidden after "show": minimized (its own window — never a job's).
async function hideStep(site) {
  const { tabs } = await stepState();
  const mine = tabs[site];
  if (!mine) return;
  mine.shown_until = 0;
  await chrome.storage.session.set({ step_tabs: tabs });
  try {
    const tab = await chrome.tabs.get(mine.tab);
    await chrome.windows.update(tab.windowId, { state: "minimized" });
  } catch (e) { /* gone already */ }
}

// The step cleared: its page goes, window and all (the next job opens its tab in the site's own unfocused window).
async function releaseStep(site) {
  const { tabs, left } = await stepState();
  const mine = tabs[site];
  delete tabs[site];
  if (mine && leftTab(left[site]) === mine.tab) delete left[site];
  await chrome.storage.session.set({ step_tabs: tabs, left_open: left });
  if (mine) {
    try { await chrome.tabs.remove(mine.tab); } catch (e) { /* closed already */ }
  }
}
setInterval(() => { checkSteps().catch(() => {}); }, STEP_EVERY);

// Chrome allows captureVisibleTab twice a second (MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND): captures are spaced, and
// one refused for it is tried again. Each job's window is captured on its own (WO33: two jobs, two pictures).
let lastCapture = 0;

async function shoot(msg, tab) {
  await restore();
  const j = (msg.job_id && byId(msg.job_id)) || (tab && byTab(tab.id));
  if (!j || j.mode === "orders") return;          // WO38: an order page is never pictured (buyer names, addresses)
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
      if (ev.event === "progress" && ev.challenge) {      // WO56: the page waits out a "Just a moment" (2 min at most)
        await restore();
        const j = byId(ev.job_id);
        if (j && ev.challenge === "waiting") {
          j.challengeAt = Date.now();
          j.extraMs = CHALLENGE_MS;                       // the job's 4 minutes to fill, plus the wait
          await remember();
        } else if (j && ev.challenge === "cleared" && j.challengeAt) {
          j.challengeAt = null;
          await remember();
          await send({ event: "challenge", site: j.site, state: "cleared", seconds: ev.seconds, mode: j.mode });
        }
      }
      if (ev.event === "step" && ev.clicked) {             // WO40: a take-down's click, never repeated after a reload
        await restore();
        const j = byId(ev.job_id);
        if (j && j.mode === "delist") {
          j.pressed = true;
          await remember();
        }
      }
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
  return now - (c.started || 0) > FILL_MS + (c.extraMs || 0) ? "the form wasn't filled in 4 minutes" : null;
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
  await tidyTabs(now);
  await checkSteps(now);                           // WO56: also when this worker was asleep between the alarm's ticks
});

// The tidy. WO52 §7 (live 2026-11-01: ~27 old pages, 8.4 GB) and WO53 closed old pages of the three sites, every
// 30 minutes, with no job running — never the active tab of a window, never another site's page; WO54 §4 (live
// 2026-11-09: two such pages — a window's active tab and pages off the three sites — burned a core each for 8½ hours):
// every TIDY_EVERY, while jobs run too and right after each job, EVERY page no running job holds is closed, whatever
// its site, unless it is
// - a running job's tab, or a page that tab opened (a site's popup), until the job ends;
// - the idle page of a site window (one per site; a second one goes);
// - the page left for the owner on a site (a login / CAPTCHA / check page, one per site), for LEFT_FOR at most —
//   WO56: a human step's page for as long as the step is pending;
// - a page born in the last BORN_GRACE (a job's tab between its creation and its job);
// - a page the owner looked at in the last UNSEEN — the active tab of a focused Thrift Chrome window (she may be
//   logging in there, or fetching a code from her mail).
const TIDY_EVERY = 60 * 1000, UNSEEN = 15 * 60 * 1000, LEFT_FOR = 6 * 3600 * 1000, BORN_GRACE = 60 * 1000;
const seenAt = {};           // tab → when the owner last looked at it (kept in session storage too: the worker sleeps)
const bornAt = {};           // tab → when it was created

async function marks() {
  const { seen = {}, born = {} } = await chrome.storage.session.get(["seen", "born"]);
  return { seen: { ...seen, ...seenAt }, born: { ...born, ...bornAt } };
}

// The page the owner looks at now: the active tab of the focused window (none when this Chrome isn't in front).
async function lookedAt(now = Date.now()) {
  let tab = null;
  try {
    const w = await chrome.windows.getLastFocused({ populate: true });
    if (w && w.focused) tab = (w.tabs || []).find((t) => t.active) || null;
  } catch (e) { return null; }          // no window at all
  if (!tab) return null;
  seenAt[tab.id] = now;
  const { seen = {} } = await chrome.storage.session.get("seen");
  await chrome.storage.session.set({ seen: { ...seen, [tab.id]: now } });
  return tab.id;
}

async function tidyTabs(now, force = false) {
  const { tidied_at: last = 0 } = await chrome.storage.local.get("tidied_at");
  if (!force && now - last < TIDY_EVERY) return;
  await restore();
  const held = Object.values(jobs).filter(Boolean);
  if (held.some((j) => j.tabId == null)) return;           // a job's tab is being opened: the next minute
  await chrome.storage.local.set({ tidied_at: now });
  await lookedAt(now);
  const { seen, born } = await marks();
  const { left_open: left = {}, step_tabs: stepTabs = {} } = await chrome.storage.session.get(["left_open", "step_tabs"]);
  const jobTabs = new Set(held.map((j) => j.tabId));
  // WO56: a human step's page is kept while the step is pending — no 6-hour cap
  const leftTabs = new Set([...Object.values(left).filter((e) => (e && e.step) || now - leftAt(e) < LEFT_FOR).map(leftTab),
                            ...Object.values(stepTabs).map((e) => e && e.tab)]);
  const tabs = await chrome.tabs.query({});
  const idleSeen = new Set(), close = [];
  for (const tab of tabs) {
    if (jobTabs.has(tab.id) || (tab.openerTabId != null && jobTabs.has(tab.openerTabId))) continue;
    const idle = /idle\.html\?site=([a-z]+)/.exec(tab.url || "");
    if (idle && (tab.url || "").startsWith(chrome.runtime.getURL(""))) {
      if (idleSeen.has(idle[1])) close.push(tab.id); else idleSeen.add(idle[1]);
      continue;
    }
    if (leftTabs.has(tab.id)) continue;
    if (now - (born[tab.id] || 0) < BORN_GRACE) continue;
    if (now - (seen[tab.id] || 0) < UNSEEN) continue;
    close.push(tab.id);
  }
  const open = new Set(tabs.map((t) => String(t.id)));     // forget the tabs that are gone
  const keep = (m) => Object.fromEntries(Object.entries(m).filter(([id]) => open.has(id) && !close.includes(+id)));
  await chrome.storage.session.set({ seen: keep(seen), born: keep(born) });
  if (close.length) {
    try { await chrome.tabs.remove(close); } catch (e) { /* gone already */ }
  }
  // WO54 (left from WO53: the tidy couldn't be proven from the logs): every run is told, even one that closed nothing
  // (event ext_tidied) — the pages closed, kept and all of this Chrome's tabs; why: every page no running job holds
  const why = { unheld: close.length };
  await send({ event: "tidied", closed: close.length, kept: tabs.length - close.length, tabs: tabs.length, why });
}

chrome.tabs.onCreated.addListener((tab) => {
  bornAt[tab.id] = Date.now();
  chrome.storage.session.get("born").then(({ born = {} }) =>
    chrome.storage.session.set({ born: { ...born, [tab.id]: bornAt[tab.id] } })).catch(() => {});
});
chrome.tabs.onActivated.addListener(() => { lookedAt().catch(() => {}); });
chrome.windows.onFocusChanged.addListener(() => { lookedAt().catch(() => {}); });

// The owner closed a job's tab (or its window): the job ends there (nothing more can happen in it).
chrome.tabs.onRemoved.addListener(async (tabId) => {
  delete seenAt[tabId];
  delete bornAt[tabId];
  await restore();
  const j = byTab(tabId);
  if (j) {
    await finish({ event: "error", job_id: j.job_id, stage: "tab_closed", page: "unknown",
                   message: "the job's tab was closed" });
  }
});

chrome.runtime.onInstalled.addListener(() => noteLoaded());
// WO54 §4: the Thrift Chrome starts with no window (--no-startup-window) when the poster has a job for it — this
// listener is what wakes this worker with the profile (the "poll" alarm is the second way), and it connects at once.
chrome.runtime.onStartup.addListener(() => { noteLoaded(); connect(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.poll_minutes) ensureAlarm();
});

ensureAlarm();
connect();
