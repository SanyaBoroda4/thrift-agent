/*
 * Thrift mail: the seller's marketplace emails (Poshmark, Depop, Vinted), forwarded to the thrift API.
 *
 * It runs inside the seller's Gmail account (Apps Script, V8): install() starts poll() every 5 minutes. It only
 * READS mail, through the Advanced Gmail service under the gmail.readonly scope, so it can't send, label, move, mark
 * or delete anything. Each new email whose From address is at poshmark.com, depop.com or vinted.com goes to the API's
 * /email as {message_id, thread_id, from, subject, date, text}: the text only, never an attachment.
 *
 * Script properties (Project Settings > Script properties) are the only configuration:
 *   API_URL  the API's base address, https only (a trailing slash is fine)
 *   API_KEY  the API's key, sent as the x-functions-key header and never written to the log
 *   SEEN     the script's own memory: a JSON array of the last 1,000 message ids already delivered, oldest first.
 *            One property holds at most 9 KB (about 420 ids), so a longer list goes on in SEEN_2 and SEEN_3.
 *
 * The owner's steps are in gmail/README.md: install() once, dumpSamples() once, uninstall() to stop.
 */

var DOMAINS = ['poshmark.com', 'depop.com', 'vinted.com'];
var POLL_DAYS = 2;                  // poll() looks two days back
var SAMPLE_DAYS = 90;               // dumpSamples() looks 90 days back
var POLL_MINUTES = 5;               // the trigger's interval
var PER_RUN = 50;                   // emails one poll() run sends at most; the rest wait for the next run
var SAMPLE_BATCH = 50;              // samples per POST to /samples
var PAGE_SIZE = 100;                // message ids per list call
var MAX_PAGES = 200;                // a guard against a listing that never ends (20,000 messages)
var POLL_BUDGET_MS = 240000;        // Apps Script ends a run at 6 minutes: poll() starts no email after 4
var SAMPLES_BUDGET_MS = 270000;     // dumpSamples() stops reading after 4.5 and sends what it has
var SEEN_LIMIT = 1000;              // message ids remembered
var SEEN_KEYS = ['SEEN', 'SEEN_2', 'SEEN_3', 'SEEN_4'];   // 1,000 Gmail ids are ~19 KB of JSON: three of these
var SEEN_VALUE_MAX = 8000;          // characters per property: Apps Script refuses a value over 9 KB

// The named entities email HTML uses; numeric ones (&#8217; &#x2019;) are decoded by number.
var ENTITIES_ = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', AMP: '&', LT: '<', GT: '>', QUOT: '"',
  ndash: '\u2013', mdash: '\u2014', lsquo: '\u2018', rsquo: '\u2019', ldquo: '\u201C', rdquo: '\u201D',
  hellip: '\u2026', bull: '\u2022', middot: '\u00B7', copy: '\u00A9', reg: '\u00AE', trade: '\u2122',
  shy: '', zwnj: '', zwj: ''
};

// ---------------------------------------------------------------------------------------------------------------
// What the owner runs (the editor's function list shows these first)

/** Starts the 5-minute poll. A poll trigger already there is replaced, so running it again leaves exactly one. */
function install() {
  config_();                        // no trigger without the API's address and key
  const removed = removeTriggers_('poll');
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(POLL_MINUTES).create();
  log_(`install: poll runs every ${POLL_MINUTES} minutes` + (removed ? ` (${removed} old trigger(s) replaced)` : ''));
}

/**
 * The trigger's work: the last two days' marketplace emails, oldest first; each one not delivered yet is POSTed to
 * /email, at most PER_RUN a run. A 2xx answer marks it delivered; any other answer or an error leaves it for the next
 * run. SEEN is saved once at the end, then /heartbeat gets {source: 'gmail', info: {sent, errors, left}}.
 */
function poll() {
  const started = Date.now();
  config_();
  const props = PropertiesService.getScriptProperties();
  const seen = loadSeen_(props);
  const todo = unseen(listIds_(buildQuery(POLL_DAYS)).reverse(), seen);    // Gmail lists the newest first
  const done = [];
  const problems = [];
  let sent = 0;
  let errors = 0;
  let skipped = 0;
  for (let i = 0; i < todo.length && i < PER_RUN; i++) {
    if (Date.now() - started > POLL_BUDGET_MS) break;
    const id = todo[i];
    try {
      const email = buildPayload(Gmail.Users.Messages.get('me', id, { format: 'full' }));
      if (!fromMarketplace_(email.from)) {      // Gmail's from: also matches a display name or a look-alike domain
        skipped++;
        done.push(id);                          // remembered, so it isn't fetched again; never sent
        continue;
      }
      const status = post_('/email', email);
      if (ok_(status)) {
        sent++;
        done.push(id);
      } else {
        errors++;
        problems.push(`${id}: HTTP ${status}`);
      }
    } catch (e) {
      errors++;
      problems.push(`${id}: ${errorText_(e)}`);
    }
  }
  if (done.length) saveSeen_(props, rememberSeen(seen, done));
  const left = todo.length - sent - skipped;    // failed this run, or past the run's limit: tried next run
  let beat;
  try {
    beat = 'heartbeat HTTP ' + post_('/heartbeat', { source: 'gmail', info: { sent: sent, errors: errors, left: left } });
  } catch (e) {
    beat = 'heartbeat failed: ' + errorText_(e);
  }
  log_(`poll: sent ${sent}, errors ${errors}, left ${left}` +
       (skipped ? `, skipped ${skipped} (not from a marketplace address)` : '') + `; ${beat}` +
       (problems.length ? ' | ' + problems.slice(0, 3).join('; ') : ''));
  return { sent: sent, errors: errors, left: left, skipped: skipped };
}

/**
 * Once, for the developer: every marketplace email of the last 90 days, POSTed to /samples as {samples: [...]} in
 * batches of 50 (the same payloads poll() sends): the examples the API's email parsers are written from.
 */
function dumpSamples() {
  const started = Date.now();
  config_();
  const ids = listIds_(buildQuery(SAMPLE_DAYS));
  const counts = { found: ids.length, read: 0, sent: 0, batches: 0, refused: 0, skipped: 0, unreadable: 0 };
  let batch = [];
  let stopped = false;
  const send = () => {
    if (!batch.length) return;
    counts.batches++;
    let status;
    try {
      status = post_('/samples', { samples: batch });
    } catch (e) {
      status = errorText_(e);
    }
    if (ok_(status)) {
      counts.sent += batch.length;
    } else {
      counts.refused += batch.length;
      log_(`dumpSamples: a batch of ${batch.length} was not taken: ` + (typeof status === 'number' ? `HTTP ${status}` : status));
    }
    batch = [];
  };
  for (let i = 0; i < ids.length; i++) {
    if (Date.now() - started > SAMPLES_BUDGET_MS) {
      stopped = true;
      break;
    }
    let email;
    try {
      email = buildPayload(Gmail.Users.Messages.get('me', ids[i], { format: 'full' }));
    } catch (e) {
      counts.unreadable++;
      continue;
    }
    counts.read++;
    if (!fromMarketplace_(email.from)) {
      counts.skipped++;
      continue;
    }
    batch.push(email);
    if (batch.length >= SAMPLE_BATCH) send();
  }
  send();
  log_(`dumpSamples: ${counts.found} emails in the last ${SAMPLE_DAYS} days, ${counts.sent} sent in ${counts.batches} batches` +
       (counts.refused ? `, ${counts.refused} not taken by the API` : '') +
       (counts.skipped ? `, ${counts.skipped} skipped (not from a marketplace address)` : '') +
       (counts.unreadable ? `, ${counts.unreadable} unreadable` : '') +
       (stopped ? `; stopped at the time limit after reading ${counts.read} (the newest first)` : ''));
  return counts;
}

/** Stops it: the poll triggers are removed. The script and its properties stay; install() starts it again. */
function uninstall() {
  const removed = removeTriggers_('poll');
  log_(`uninstall: ${removed} poll trigger(s) removed; nothing runs on its own now`);
}

// ---------------------------------------------------------------------------------------------------------------
// The parts (pure where they can be: the Node tests call them directly)

/** Gmail's search for the three marketplaces' mail of the last `days` days. */
function buildQuery(days) {
  return `from:(${DOMAINS.join(' OR ')}) newer_than:${days}d`;
}

/** An HTML body as plain text: what a reader sees, one block per line. */
function htmlToText(html) {
  let s = String(html == null ? '' : html);
  s = s.replace(/<!--[\s\S]*?-->/g, '');                                    // comments (Outlook's blocks too)
  s = s.replace(/<(head|style|script)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');      // never shown
  s = s.replace(/\s+/g, ' ');                                               // a line break in HTML source is a space
  s = s.replace(/<br\b[^>]*>/gi, '\n');
  s = s.replace(/<\/(p|div|tr|li|h[1-6])\s*>/gi, '\n');
  s = s.replace(/<\/t[dh]\s*>/gi, ' ');                                     // table cells side by side
  s = s.replace(/<[^>]*>/g, '');
  s = decodeEntities_(s);
  s = s.replace(/\r\n?/g, '\n');
  s = s.replace(/[\u00AD\u034F\u200B-\u200D\u2060\uFEFF]/g, '');           // invisible fillers (preheader padding)
  s = s.replace(/[ \t\u00A0]+/g, ' ');
  s = s.replace(/ ?\n ?/g, '\n');
  s = s.replace(/\n{3,}/g, '\n\n');
  return s.trim();
}

/** The message's text: the first text/plain part of the MIME tree, else the first text/html one as text, else "". */
function extractText(payload) {
  const plain = partText_(payload, 'text/plain');
  if (plain) return plain.replace(/\r\n?/g, '\n').trim();
  const html = partText_(payload, 'text/html');
  return html ? htmlToText(html) : '';
}

/** A header's value (the name in any case), "" when the message has none. */
function header(payload, name) {
  const want = String(name).toLowerCase();
  const headers = (payload && payload.headers) || [];
  for (let i = 0; i < headers.length; i++) {
    if (headers[i] && String(headers[i].name).toLowerCase() === want) return String(headers[i].value || '');
  }
  return '';
}

/** What the API gets for one Gmail message (format 'full'). */
function buildPayload(msg) {
  const payload = msg.payload || {};
  return {
    message_id: msg.id,
    thread_id: msg.threadId,
    from: header(payload, 'From'),
    subject: header(payload, 'Subject'),
    date: isoDate_(msg.internalDate, header(payload, 'Date')),
    text: extractText(payload)
  };
}

/**
 * The delivered ids to keep: the old ones, then the new, each once (an id seen again moves to the newest end), the
 * last `limit` (default 1,000) of them. The inputs are not changed.
 */
function rememberSeen(seen, ids, limit) {
  const max = limit > 0 ? Math.floor(limit) : SEEN_LIMIT;
  const all = (seen || []).concat(ids || []);
  const kept = new Set();
  const out = [];
  for (let i = all.length - 1; i >= 0 && out.length < max; i--) {
    const id = all[i];
    if (!id || kept.has(id)) continue;
    kept.add(id);
    out.push(id);
  }
  return out.reverse();
}

/** The ids not in `seen`, in their order, each once. */
function unseen(ids, seen) {
  const known = new Set(seen || []);
  const out = [];
  (ids || []).forEach((id) => {
    if (!id || known.has(id)) return;
    known.add(id);
    out.push(id);
  });
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Private (a trailing underscore keeps a function out of the editor's Run list)

/** POSTs `body` as JSON to API_URL + path with the key; the HTTP status (an error status doesn't throw). */
function post_(path, body) {
  const cfg = config_();
  const response = UrlFetchApp.fetch(cfg.url + path, {
    method: 'post',
    contentType: 'application/json',
    payload: json_(body),
    headers: { 'x-functions-key': cfg.key },
    muteHttpExceptions: true
  });
  return response.getResponseCode();
}

/** API_URL (no trailing slash) and API_KEY from the Script properties; an error that says what to set. */
function config_() {
  const props = PropertiesService.getScriptProperties();
  const url = String(props.getProperty('API_URL') || '').trim().replace(/\/+$/, '');
  const key = String(props.getProperty('API_KEY') || '').trim();
  if (!url || !key) throw new Error('Set the Script properties API_URL and API_KEY first (Project Settings > Script properties).');
  if (!/^https:\/\/[^\s/]+/i.test(url)) throw new Error('API_URL must be an https:// address.');
  return { url: url, key: key };
}

/** JSON in plain ASCII (other characters as \u escapes), so no charset on the way can garble a title. */
function json_(value) {
  return JSON.stringify(value).replace(/[\u007F-\uFFFF]/g, (c) => '\\u' + ('000' + c.charCodeAt(0).toString(16)).slice(-4));
}

function ok_(status) {
  return typeof status === 'number' && status >= 200 && status < 300;
}

/** Every message id the search finds, page by page, newest first (Gmail's order). */
function listIds_(query) {
  const ids = [];
  let pageToken = '';
  for (let page = 0; page < MAX_PAGES; page++) {
    const options = { q: query, maxResults: PAGE_SIZE };
    if (pageToken) options.pageToken = pageToken;
    const result = Gmail.Users.Messages.list('me', options) || {};
    (result.messages || []).forEach((m) => {
      if (m && m.id) ids.push(m.id);
    });
    if (!result.nextPageToken || result.nextPageToken === pageToken) break;
    pageToken = result.nextPageToken;
  }
  return ids;
}

/** True when the From address itself is at one of the marketplaces or a subdomain of one. */
function fromMarketplace_(from) {
  const text = String(from || '').replace(/\([^()]*\)/g, ' ').trim();     // old-style comments aside
  const angle = text.match(/<([^<>]*)>\s*$/);
  const address = (angle ? angle[1] : text).trim().toLowerCase();
  const at = address.lastIndexOf('@');
  if (at < 0) return false;
  const domain = address.slice(at + 1).replace(/\.+$/, '');
  return DOMAINS.some((d) => domain === d || domain.slice(-d.length - 1) === '.' + d);
}

/** SEEN, SEEN_2 ... read back as one list, oldest first. */
function loadSeen_(props) {
  let ids = [];
  SEEN_KEYS.forEach((key) => {
    const raw = props.getProperty(key);
    if (!raw) return;
    try {
      const list = JSON.parse(raw);
      if (Array.isArray(list)) ids = ids.concat(list.filter((id) => typeof id === 'string' && id));
    } catch (e) {
      log_(`${key} is not a JSON list; ignored`);
    }
  });
  return ids;
}

/** Saves the list over SEEN, SEEN_2 ..., each value under SEEN_VALUE_MAX; a key no longer needed is removed. */
function saveSeen_(props, ids) {
  const chunks = [[]];                  // filled from the newest id back: chunks[0] is the one being filled
  let size = 2;                         // "[]"
  for (let i = ids.length - 1; i >= 0; i--) {
    const cost = JSON.stringify(ids[i]).length + 1;     // the quoted id and its comma
    if (chunks[0].length && size + cost > SEEN_VALUE_MAX) {
      if (chunks.length === SEEN_KEYS.length) break;    // every key full: the oldest ids are forgotten
      chunks.unshift([]);
      size = 2;
    }
    chunks[0].unshift(ids[i]);
    size += cost;
  }
  SEEN_KEYS.forEach((key, n) => {
    if (n < chunks.length) props.setProperty(key, JSON.stringify(chunks[n]));
    else if (props.getProperty(key) !== null) props.deleteProperty(key);
  });
}

function removeTriggers_(handler) {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach((trigger) => {
    if (trigger.getHandlerFunction() === handler) {
      ScriptApp.deleteTrigger(trigger);
      removed++;
    }
  });
  return removed;
}

/** One line to the execution log; the API's key and address never appear in it. */
function log_(line) {
  let text = String(line);
  const props = PropertiesService.getScriptProperties();
  ['API_KEY', 'API_URL'].forEach((name) => {
    const value = String(props.getProperty(name) || '').trim().replace(/\/+$/, '');
    if (value.length >= 8) text = text.split(value).join(`<${name}>`);
  });
  console.log(text);
}

function errorText_(e) {
  return String(e && e.message ? e.message : e).replace(/\s+/g, ' ').trim();
}

/** The first part of this MIME type (depth first, in order) that has text in it; attachments never count. */
function partText_(part, mimeType) {
  if (!part) return '';
  if (String(part.mimeType || '').toLowerCase() === mimeType && !part.filename && part.body && part.body.data) {
    const text = decodeBody_(part.body.data);
    if (text.trim()) return text;
  }
  const parts = part.parts || [];
  for (let i = 0; i < parts.length; i++) {
    const text = partText_(parts[i], mimeType);
    if (text) return text;
  }
  return '';
}

/** A body's base64url data as UTF-8 text ("" when it can't be decoded). */
function decodeBody_(data) {
  let s = String(data);
  if (s.length % 4) s += '===='.slice(s.length % 4);
  try {
    return Utilities.newBlob(Utilities.base64DecodeWebSafe(s)).getDataAsString('UTF-8');
  } catch (e) {
    return '';
  }
}

function decodeEntities_(text) {
  return text.replace(/&(#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]{1,31});/g, (whole, name) => {
    if (name.charAt(0) !== '#') return Object.prototype.hasOwnProperty.call(ENTITIES_, name) ? ENTITIES_[name] : whole;
    const hex = name.charAt(1) === 'x' || name.charAt(1) === 'X';
    const code = parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10);
    if (!(code > 0 && code <= 0x10FFFF) || (code >= 0xD800 && code <= 0xDFFF)) return whole;
    return String.fromCodePoint(code);
  });
}

/** Gmail's internalDate (epoch ms) as ISO 8601, else the Date header's; null when neither reads. */
function isoDate_(internalDate, dateHeader) {
  let d = new Date(internalDate === undefined || internalDate === null || internalDate === '' ? NaN : Number(internalDate));
  if (isNaN(d.getTime())) d = new Date(dateHeader ? Date.parse(dateHeader) : NaN);
  return isNaN(d.getTime()) ? null : d.toISOString();
}

if (typeof module !== 'undefined') { module.exports = { buildQuery, htmlToText, extractText, header, buildPayload, rememberSeen, unseen, poll, install, uninstall, dumpSamples }; }
