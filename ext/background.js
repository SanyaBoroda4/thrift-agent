// Thrift crosslister (WO32) — the background service worker. The link to the bridge inside the poster process on this
// Mac: a WebSocket to ws://127.0.0.1:8765/ext (the token in the first message, reconnected with back-off, a message
// every 20 s so the worker stays up while it is open) and, for when it is down (MV3 workers go idle), an alarm every
// minute that asks GET /jobs/next. One job at a time, in its own tab of this window: the job's photos fetched from the
// bridge, the content script started on the page, its events relayed, the visible tab captured on request. After the
// one publish click it watches the tab land on the listing page. It opens nothing a job didn't ask for.
const BRIDGE = "http://127.0.0.1:8765";
const WS_URL = "ws://127.0.0.1:8765/ext";
const PING_MS = 20000;
const OPEN_MS = 45000;
const AFTER_CLICK_MS = 60000;
const STALE_MS = 7 * 60000;     // a job the bridge gave up on long ago (it allows 6 minutes): ours ends too
const STOP_PAGES = ["login", "block", "captcha", "verify"];
// The files whose hash tells the bridge's copy (git pull on deploy) from the loaded one: a difference reloads us.
const FILES = ["manifest.json", "background.js", "content/common.js", "content/vinted.js", "content/depop.js",
               "selectors.json", "options.html", "options.js"];

let ws = null;
let wsReady = false;
let backoff = 1000;
let reconnectTimer = null;
let pingTimer = null;
let afterTimer = null;
let current = null;        // the job in hand: {job_id, site, mode, tabId, windowId, clicked, after, job}
let selectorsCache = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const errText = (e) => String(e && e.message ? e.message : e).slice(0, 300);

async function settings() {
  const { token = "", poll_minutes = 1 } = await chrome.storage.local.get(["token", "poll_minutes"]);
  return { token, poll_minutes };
}

async function setState(link) {
  await chrome.storage.local.set({ bridge_state: { link, at: new Date().toISOString() } });
}

async function selectors() {
  if (!selectorsCache) selectorsCache = await (await fetch(chrome.runtime.getURL("selectors.json"))).json();
  return selectorsCache;
}

async function remember() {
  await chrome.storage.session.set({ current });
}

async function restore() {
  if (!current) current = (await chrome.storage.session.get("current")).current || null;
}

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
  if (!hash || current) return;
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
  let sock;
  try {
    sock = new WebSocket(WS_URL);
  } catch (e) {
    later();
    return;
  }
  ws = sock;
  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token, version: chrome.runtime.getManifest().version }));
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
    ws = null;
    wsReady = false;
    clearInterval(pingTimer);
    later();
  };
  sock.onerror = () => {};
}

function later() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 60000);
}

async function onBridge(msg) {
  if (msg.type === "welcome") {
    wsReady = true;
    backoff = 1000;
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    }, PING_MS);
    await setState("connected");
    await maybeReload(msg.ext_hash);
  } else if (msg.type === "refused") {
    wsReady = false;
    backoff = 60000;
    await setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
  } else if (msg.type === "job") {
    await startJob(msg.job);
  } else if (msg.type === "submit" || msg.type === "cancel") {
    await relay(msg);
  }
}

// GET /jobs/next: the way in while the socket is down (the alarm's tick).
async function poll() {
  await restore();
  if (wsReady || current) return;
  const { token } = await settings();
  if (!token) return;
  try {
    const r = await fetch(`${BRIDGE}/jobs/next`, { headers: { "X-Thrift-Token": token } });
    if (r.status === 401) return setState("the bridge refused the token: paste it again (cat ~/thrift/var/ext_token)");
    if (r.status === 200) {
      await setState("polling (the socket is down)");
      await startJob(await r.json());
    }
  } catch (e) {
    await setState("the bridge isn't reachable (the poster isn't running, or Depop / Vinted are off)");
  }
}

// To the bridge: over the socket when it is open, else POST /events (its answer carries the job's commands).
async function send(event) {
  if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "event", ...event }));
    return;
  }
  const { token } = await settings();
  try {
    const r = await fetch(`${BRIDGE}/events`, {
      method: "POST", headers: { "X-Thrift-Token": token, "Content-Type": "application/json" },
      body: JSON.stringify(event),
    });
    if (r.ok) {
      const { commands = [] } = await r.json();
      for (const c of commands) await onBridge(c);
    }
  } catch (e) { /* the bridge times the job out */ }
}

// While a publish waits for the bridge's go-ahead without a socket: ask every 2 s.
async function waitCommands(jobId) {
  for (let i = 0; i < 180; i++) {
    await sleep(2000);
    await restore();
    if (!current || current.job_id !== jobId || current.decided || wsReady) return;
    await send({ event: "waiting", job_id: jobId });
  }
}

// ---------------------------------------------------------------- one job

async function thriftWindow(url) {
  try {
    const win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    return { windowId: win.id };
  } catch (e) {
    const win = await chrome.windows.create({ url, focused: false });
    return { windowId: win.id, tab: win.tabs[0] };
  }
}

function loaded(tabId, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(on);
      reject(new Error(`the page didn't load in ${ms / 1000} s`));
    }, ms);
    const on = (id, info) => {
      if (id !== tabId || info.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(on);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(on);
    chrome.tabs.get(tabId).then((t) => t.status === "complete" && t.url !== "about:blank" && on(tabId, t)).catch(() => {});
  });
}

async function toTab(tabId, msg, tries = 20) {
  for (let i = 0; ; i++) {
    try {
      return await chrome.tabs.sendMessage(tabId, msg);
    } catch (e) {
      if (i >= tries) {
        let where = "?";
        try { where = (await chrome.tabs.get(tabId)).url; } catch (e2) { /* closed */ }
        throw new Error(`the page has no Thrift script (${where})`);
      }
      await sleep(500);
    }
  }
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
  if (job.mode === "verify" || job.mode === "delist") return job.listing_url;
  if (job.mode === "find") return site.shop_url.replace("{shop}", encodeURIComponent(job.shop || ""));
  return site.sell_url;
}

async function startJob(job) {
  await restore();
  if (current) {
    if (current.job_id !== job.job_id) {
      await send({ event: "error", job_id: job.job_id, stage: "busy", page: "unknown",
                   message: `another job is running (${current.job_id})` });
    }
    return;
  }
  const sel = await selectors();
  const site = sel[job.site];
  if (!site) {
    await send({ event: "error", job_id: job.job_id, stage: "open", page: "unknown", message: `unknown site ${job.site}` });
    return;
  }
  current = { job_id: job.job_id, site: job.site, mode: job.mode, tabId: null, windowId: null, clicked: false,
              after: false, decided: false, started: Date.now(), job };
  await remember();
  try {
    const photos = job.mode === "dry_run" || job.mode === "publish" ? await fetchPhotos(job.photos || []) : [];
    const url = urlFor(job, site);
    const where = await thriftWindow(url);
    const tab = where.tab || (await chrome.tabs.create({ url, active: true, windowId: where.windowId }));
    current.tabId = tab.id;
    current.windowId = tab.windowId;
    await remember();
    await loaded(tab.id, OPEN_MS);
    await toTab(tab.id, { type: "job", job, selectors: sel, photos, pace: job.pace });
  } catch (e) {
    await finish({ event: "error", job_id: job.job_id, stage: "open", page: "unknown", message: errText(e) });
  }
}

// The bridge's go-ahead (submit) or no (cancel) for the filled form; after submit, the landing page is watched.
async function relay(msg) {
  await restore();
  if (!current || current.job_id !== msg.job_id || current.decided) return;
  current.decided = true;
  if (msg.type === "submit") {
    current.clicked = true;
    clearTimeout(afterTimer);
    afterTimer = setTimeout(afterClickTimeout, AFTER_CLICK_MS);
  }
  await remember();
  try {
    await toTab(current.tabId, { type: msg.type }, 4);
  } catch (e) {
    await finish({ event: "error", job_id: current.job_id, stage: msg.type, page: "unknown", message: errText(e) });
  }
}

// No listing page a minute after the click: whatever the tab shows is reported (the bridge then checks the shop).
async function afterClickTimeout() {
  await restore();
  if (!current || !current.clicked || current.after) return;
  current.after = true;
  await remember();
  try {
    await toTab(current.tabId, { type: "job", job: { ...current.job, mode: "after_publish" }, selectors: await selectors() }, 6);
  } catch (e) {
    await finish({ event: "error", job_id: current.job_id, stage: "after_publish", page: "unknown", message: errText(e) });
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  await restore();
  if (!current || tabId !== current.tabId || !current.clicked || current.after) return;
  if (info.status !== "complete" && !info.url) return;
  const sel = await selectors();
  if (!new RegExp(sel[current.site].listing_url).test(tab.url || "")) return;
  current.after = true;
  await remember();
  clearTimeout(afterTimer);
  await sleep(2500);
  try {
    await toTab(tabId, { type: "job", job: { ...current.job, mode: "after_publish" }, selectors: sel });
  } catch (e) {
    await finish({ event: "result", job_id: current.job_id, url: tab.url, live: null, note: errText(e) });
  }
});

// The job's last word: to the bridge, then its tab closed — except a login / CAPTCHA / block page, left open for the
// owner (a human may log in or solve it there; the code never does).
async function finish(ev) {
  await restore();
  const job = current && current.job_id === ev.job_id ? current : null;
  if (job) {
    current = null;
    clearTimeout(afterTimer);
    await remember();
  }
  await send(ev);
  if (job && job.tabId != null && !(ev.event === "error" && STOP_PAGES.includes(ev.page))) {
    await sleep(1500);
    try {
      await chrome.tabs.remove(job.tabId);
    } catch (e) { /* closed already */ }
  }
  if (!wsReady) await poll();
}

// Chrome allows captureVisibleTab twice a second (MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND): captures are spaced, and
// one refused for it is tried again.
let lastCapture = 0;

async function shoot(msg, tab) {
  await restore();
  if (!current) return;
  let png_b64 = null;
  let error = null;
  for (let attempt = 0; attempt < 3 && !png_b64; attempt++) {
    await sleep(Math.max(0, lastCapture + 600 - Date.now()));
    try {
      if (tab && !tab.active) await chrome.tabs.update(tab.id, { active: true });
      lastCapture = Date.now();
      const dataUrl = await chrome.tabs.captureVisibleTab(tab ? tab.windowId : current.windowId, { format: "png" });
      png_b64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
      error = null;
    } catch (e) {
      error = errText(e);
      if (!/MAX_CAPTURE|quota/i.test(error)) break;
    }
  }
  await send({ event: "screenshot", job_id: current.job_id, label: msg.label, png_b64, html: msg.html, url: msg.url,
               error });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.type === "event") {
      const ev = { ...msg };
      delete ev.type;
      if (ev.event === "result" || ev.event === "error") return finish(ev);
      await send(ev);
      if (ev.event === "ready" && !wsReady) waitCommands(ev.job_id);
    } else if (msg.type === "screenshot") {
      await shoot(msg, sender.tab);
    } else if (msg.type === "token-saved") {
      backoff = 1000;
      if (ws) ws.close();
      await connect();
    }
  })().catch(() => {}).finally(() => sendResponse({ ok: true }));
  return true;
});

// ---------------------------------------------------------------- waking up

async function ensureAlarm() {
  const { poll_minutes } = await settings();
  const alarm = await chrome.alarms.get("poll");
  if (!alarm || alarm.periodInMinutes !== poll_minutes) {
    await chrome.alarms.create("poll", { periodInMinutes: poll_minutes, delayInMinutes: poll_minutes });
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "poll") return;
  await restore();
  if (current && Date.now() - (current.started || 0) > STALE_MS) {
    await finish({ event: "error", job_id: current.job_id, stage: "stale", page: "unknown",
                   message: "the job ran past 7 minutes" });
  }
  await connect();
  await poll();
});

// The owner closed the job's tab: the job ends there (nothing more can happen in it).
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await restore();
  if (current && current.tabId === tabId) {
    await finish({ event: "error", job_id: current.job_id, stage: "tab_closed", page: "unknown",
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
