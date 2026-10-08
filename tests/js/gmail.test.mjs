// The Gmail Apps Script (gmail/Code.gs) under Node's own test runner. Code.gs is loaded with node:vm into a context
// whose globals stand in for Apps Script: Gmail.Users.Messages (list / get over an in-memory inbox), UrlFetchApp
// (recorded, scripted status codes), the Script properties (in memory, refusing a value over 9 KB as Apps Script
// does), ScriptApp's triggers, Utilities' base64 / blob, Logger and console. No npm dependencies, no network.
// Run: node --test tests/js/gmail.test.mjs (pytest runs it: tests/test_gmail_js.py).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const GMAIL = fileURLToPath(new URL('../../gmail/', import.meta.url));
const SOURCE = readFileSync(join(GMAIL, 'Code.gs'), 'utf8');
const API_URL = 'https://api.example.test/api/';          // a trailing slash: the script drops it
const BASE = 'https://api.example.test/api';
const KEY = 'test-function-key-0123456789';
const Q2 = 'from:(poshmark.com OR depop.com OR vinted.com) newer_than:2d';
const Q90 = 'from:(poshmark.com OR depop.com OR vinted.com) newer_than:90d';
const T0 = Date.UTC(2026, 9, 5, 12, 0, 0);
const PUBLIC = ['buildQuery', 'htmlToText', 'extractText', 'header', 'buildPayload', 'rememberSeen', 'unseen', 'poll',
                'install', 'uninstall', 'dumpSamples'];
const plain = (x) => JSON.parse(JSON.stringify(x));       // values from the script's realm, compared as data
const addr = (user, domain) => [user, domain].join('@');  // no address literal in this file
const b64url = (text) => Buffer.from(text, 'utf8').toString('base64url');   // unpadded: the script pads it
const pad = (n) => String(n).padStart(4, '0');

// A Gmail API message resource (format 'full') with a text/plain body; n orders them in time.
function mail(n, { id = `m${pad(n)}`, from = `Poshmark <${addr('notify', 'poshmark.com')}>`, subject = `Sale ${n}`,
                   text = `Item ${n} sold` } = {}) {
  const at = T0 + n * 60000;
  return {
    id, threadId: `t${pad(n)}`, internalDate: String(at), labelIds: ['INBOX'], snippet: text.slice(0, 40),
    payload: {
      partId: '', mimeType: 'text/plain', filename: '',
      headers: [{ name: 'From', value: from }, { name: 'Subject', value: subject },
                { name: 'Date', value: new Date(at).toUTCString() }],
      body: { size: Buffer.byteLength(text), data: b64url(text) },
    },
  };
}
const newestFirst = (msgs) => msgs.slice().sort((a, b) => Number(b.internalDate) - Number(a.internalDate));
const many = (count, from = 1) => newestFirst(Array.from({ length: count }, (_, i) => mail(from + i)));
const idsOf = (first, last) => Array.from({ length: last - first + 1 }, (_, i) => `m${pad(first + i)}`);

// Apps Script around Code.gs. `inbox` is newest first, as Gmail lists; a test may change w.inbox, w.status, w.onGet.
function world({ inbox = [], props = {}, status = 200, clock = false } = {}) {
  const w = {
    inbox, status, onGet: null, now: T0,
    store: new Map(Object.entries({ API_URL, API_KEY: KEY, ...props })),
    triggers: [], lists: [], gets: [], posts: [], logs: [], charsets: [], writes: 0, attachments: new Map(),
    attachmentGets: [],
  };
  const scriptProperties = {
    getProperty: (k) => (w.store.has(k) ? w.store.get(k) : null),
    setProperty(k, v) {
      const value = String(v);
      if (Buffer.byteLength(value, 'utf8') > 9 * 1024) throw new Error('Exception: Argument too large: value');
      w.writes++;
      w.store.set(k, value);
      return scriptProperties;
    },
    deleteProperty(k) { w.store.delete(k); return scriptProperties; },
  };
  let nextTrigger = 1;
  const trigger = (handler, minutes) => {
    const uid = String(nextTrigger++);
    return { handler, minutes, getHandlerFunction: () => handler, getUniqueId: () => uid };
  };
  w.addTrigger = (handler, minutes) => w.triggers.push(trigger(handler, minutes));
  const log = (...args) => w.logs.push(args.map(String).join(' '));
  const sandbox = {
    Gmail: { Users: { Messages: {
      list(user, options = {}) {
        w.lists.push({ user, ...options });
        const size = options.maxResults || 100;
        const start = options.pageToken ? Number(options.pageToken) : 0;
        const page = w.inbox.slice(start, start + size);
        const result = { resultSizeEstimate: page.length };
        if (page.length) result.messages = page.map((m) => ({ id: m.id, threadId: m.threadId }));
        if (start + size < w.inbox.length) result.nextPageToken = String(start + size);
        return result;
      },
      get(user, id, options = {}) {
        w.gets.push({ user, id, ...options });
        if (w.onGet) w.onGet(id, options);
        const m = w.inbox.find((x) => x.id === id);
        if (!m) throw new Error('GoogleJsonResponseException: API call to gmail.users.messages.get failed: Not Found');
        return structuredClone(m);
      },
      Attachments: {
        get(user, messageId, id) {
          w.attachmentGets.push({ user, messageId, id });
          if (!w.attachments.has(id)) throw new Error('GoogleJsonResponseException: attachments.get failed: Not Found');
          return { size: w.attachments.get(id).length, data: w.attachments.get(id) };
        },
      },
    } } },
    UrlFetchApp: {
      fetch(url, params) {
        const body = JSON.parse(params.payload);
        w.posts.push({ url, params, body });
        const code = typeof w.status === 'function' ? w.status(url, body) : w.status;
        if (code instanceof Error) throw code;
        return { getResponseCode: () => code, getContentText: () => '' };
      },
    },
    PropertiesService: { getScriptProperties: () => scriptProperties },
    ScriptApp: {
      getProjectTriggers: () => w.triggers.slice(),
      deleteTrigger(t) { w.triggers = w.triggers.filter((x) => x.getUniqueId() !== t.getUniqueId()); },
      newTrigger: (handler) => ({ timeBased: () => ({ everyMinutes: (n) => ({ create() {
        const t = trigger(handler, n);
        w.triggers.push(t);
        return t;
      } }) }) }),
    },
    Utilities: {
      sleep() {},
      base64DecodeWebSafe(data) {               // strict, like Java's decoder: padded base64url only
        if (data.length % 4 !== 0 || /[^A-Za-z0-9_=-]/.test(data)) throw new Error('Exception: Could not decode string.');
        return Array.from(Buffer.from(data, 'base64url'), (b) => (b > 127 ? b - 256 : b));   // Java bytes are signed
      },
      newBlob: (bytes) => ({ getDataAsString(charset) {
        w.charsets.push(charset);
        return Buffer.from(Array.from(bytes, (b) => b & 255)).toString('utf8');
      } }),
    },
    Logger: { log },
    console: { log, info: log, warn: log, error: log },
    module: { exports: {} },
  };
  if (clock) sandbox.__now = () => w.now;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: 'gmail/Code.gs' });
  if (clock) vm.runInContext('Date.now = function () { return __now(); };', sandbox);
  w.api = sandbox.module.exports;
  w.ctx = sandbox;
  return w;
}

// SEEN as the script keeps it: SEEN, then SEEN_2, SEEN_3 … (one property holds at most 9 KB).
function seenIn(w) {
  const ids = [];
  for (const key of ['SEEN', 'SEEN_2', 'SEEN_3', 'SEEN_4']) if (w.store.has(key)) ids.push(...JSON.parse(w.store.get(key)));
  return ids;
}
const emailIds = (w) => w.posts.filter((p) => p.url === `${BASE}/email`).map((p) => p.body.message_id);
function heartbeat(w) {                         // the run's last POST, and it is the heartbeat
  const last = w.posts.at(-1);
  assert.equal(last.url, `${BASE}/heartbeat`);
  return last.body;
}
function fresh(w) {                             // forget what the last run did (the inbox and properties stay)
  w.posts.length = 0;
  w.gets.length = 0;
  w.lists.length = 0;
  w.logs.length = 0;
}
const noSecretsLogged = (w) => {
  for (const line of w.logs) {
    assert.ok(!line.includes(KEY), `the key was logged: ${line}`);
    assert.ok(!line.includes('api.example.test'), `the API address was logged: ${line}`);
  }
};

test('buildQuery: mail from the three marketplaces, so many days back', () => {
  const { api } = world();
  assert.equal(api.buildQuery(2), Q2);
  assert.equal(api.buildQuery(90), Q90);
});

test('htmlToText: head, styles, scripts and comments gone; breaks kept; entities decoded; whitespace tidied', () => {
  const { api } = world();
  const html = `<!DOCTYPE html><html><head><title>Ignore me</title><style>p { color: red; }</style></head>
  <body><!-- a comment --><script type="text/javascript">var x = "<p>no</p>";</script>
  <h1>You  made\ta sale!</h1>
  <div>Nike &amp; Co &lt;Air&gt; &quot;Max&quot; isn&#39;t &#8220;new&#x201D; &#x2019;&nbsp;&nbsp;size&nbsp;8 &#127881;</div>
  <p>Line one<br>Line two<BR/>Line three<br /></p>
  <table><tr><td>Price</td><td>$25.00</td></tr><tr><th>Ship by</th><td>Oct 9</td></tr></table>
  <ul><li>One</li><li>Two</li></ul>
  <div></div><div></div><div></div><div></div>
  <h3>The end &amp;lt;kept&amp;gt; &unknown; &#0;</h3>
  </body></html>`;
  assert.equal(api.htmlToText(html), [
    'You made a sale!',
    'Nike & Co <Air> "Max" isn\'t “new” ’ size 8 🎉',
    'Line one', 'Line two', 'Line three', '',
    'Price $25.00', 'Ship by Oct 9',
    'One', 'Two', '',
    'The end &lt;kept&gt; &unknown; &#0;',
  ].join('\n'));
  assert.equal(api.htmlToText('<p>a</p>\r\n\r\n<div></div><div></div><div></div><p>b</p>'), 'a\n\nb');   // 3+ → 2
  assert.equal(api.htmlToText('  <span>x</span>  '), 'x');
  assert.equal(api.htmlToText(''), '');
  assert.equal(api.htmlToText(null), '');
});

test('extractText: a text/plain-only email, base64url and UTF-8 decoded', () => {
  const w = world();
  const text = 'Congrats! You made a sale — Nike ’Air’ été 🎉\r\nShip by Oct 9 ~~~ ??? >>>\r\n\r\n';
  const data = b64url(text);
  assert.match(data, /[-_]/);                   // the web-safe alphabet is in play
  assert.notEqual(data.length % 4, 0);          // and the padding is left to the script
  assert.equal(w.api.extractText({ mimeType: 'text/plain', headers: [], body: { size: text.length, data } }),
               'Congrats! You made a sale — Nike ’Air’ été 🎉\nShip by Oct 9 ~~~ ??? >>>');
  assert.deepEqual(w.charsets, ['UTF-8']);
});

test('extractText: an HTML-only email goes through htmlToText', () => {
  const { api } = world();
  const html = '<html><head><style>.x{}</style></head><body><p>Your item sold!</p><p>Total: $25.00 &amp; free shipping</p></body></html>';
  assert.equal(api.extractText({ mimeType: 'text/html', body: { data: b64url(html) } }),
               'Your item sold!\nTotal: $25.00 & free shipping');
});

test('extractText: multipart/alternative inside multipart/mixed: text/plain first, attachments never, "" for none', () => {
  const { api } = world();
  const nested = {
    mimeType: 'multipart/mixed', body: { size: 0 },
    parts: [
      { partId: '0', mimeType: 'multipart/alternative', body: { size: 0 }, parts: [
        { partId: '0.0', mimeType: 'text/html', body: { data: b64url('<p>HTML comes second</p>') } },
        { partId: '0.1', mimeType: 'text/plain', body: { data: b64url('Plain text wins\r\nSecond line') } },
      ] },
      { partId: '1', mimeType: 'application/pdf', filename: 'label.pdf', body: { attachmentId: 'ATT1', size: 2048 } },
    ],
  };
  assert.equal(api.extractText(nested), 'Plain text wins\nSecond line');

  const htmlOnly = {                            // a text/plain ATTACHMENT is not the body; an empty plain part neither
    mimeType: 'multipart/mixed', body: { size: 0 },
    parts: [
      { partId: '0', mimeType: 'multipart/alternative', parts: [
        { partId: '0.0', mimeType: 'text/plain', body: { data: b64url(' \r\n ') } },
        { partId: '0.1', mimeType: 'text/html', body: { data: b64url('<div>Sold: <b>Zara</b> skirt</div><div>$18</div>') } },
      ] },
      { partId: '1', mimeType: 'text/plain', filename: 'notes.txt', body: { data: b64url('an attachment'), size: 13 } },
    ],
  };
  assert.equal(api.extractText(htmlOnly), 'Sold: Zara skirt\n$18');

  const nothing = { mimeType: 'multipart/mixed', parts: [
    { mimeType: 'image/png', filename: 'logo.png', body: { attachmentId: 'ATT2', size: 100 } }] };
  assert.equal(api.extractText(nothing), '');
  assert.equal(api.extractText(undefined), '');
});

test('header: any case, the first match, "" when missing', () => {
  const { api } = world();
  const payload = { headers: [{ name: 'FROM', value: 'A' }, { name: 'from', value: 'B' }, { name: 'Subject', value: 'S' }] };
  assert.equal(api.header(payload, 'From'), 'A');
  assert.equal(api.header(payload, 'subject'), 'S');
  assert.equal(api.header(payload, 'Date'), '');
  assert.equal(api.header({}, 'From'), '');
});

test('buildPayload: exactly the six fields, the date in ISO 8601 from internalDate', () => {
  const { api } = world();
  const from = `Poshmark <${addr('notify', 'poshmark.com')}>`;
  const msg = {
    id: '18b0c5e3f2a1d4e7', threadId: '18b0c5e3f2a10000', internalDate: String(Date.UTC(2026, 9, 7, 14, 30, 5, 120)),
    labelIds: ['INBOX', 'UNREAD'], snippet: 'You made a sale', sizeEstimate: 1234,
    payload: {
      mimeType: 'multipart/alternative',
      headers: [{ name: 'from', value: from }, { name: 'SUBJECT', value: 'You made a sale!' },
                { name: 'Date', value: 'Wed, 07 Oct 2026 10:30:05 -0400' }],
      parts: [{ mimeType: 'text/plain', body: { data: b64url('Sold: Nike Air Max, $25') } },
              { mimeType: 'text/html', body: { data: b64url('<p>Sold</p>') } }],
    },
  };
  assert.deepEqual(plain(api.buildPayload(msg)), {
    message_id: '18b0c5e3f2a1d4e7', thread_id: '18b0c5e3f2a10000', from, subject: 'You made a sale!',
    date: '2026-10-07T14:30:05.120Z', text: 'Sold: Nike Air Max, $25',
  });
  delete msg.internalDate;                      // none: the Date header's time
  assert.equal(api.buildPayload(msg).date, '2026-10-07T14:30:05.000Z');
});

test('rememberSeen: old + new, each once, the newest 1,000 kept, the inputs untouched', () => {
  const { api } = world();
  const old = Array.from({ length: 995 }, (_, i) => `a${i}`);
  const before = old.slice();
  const out = plain(api.rememberSeen(old, ['b1', 'a3', 'b2', 'b2', 'b3', 'b4', 'b5', 'b6']));
  assert.equal(out.length, 1000);
  assert.equal(new Set(out).size, 1000);
  assert.deepEqual(out.slice(-7), ['b1', 'a3', 'b2', 'b3', 'b4', 'b5', 'b6']);   // an id seen again moves to the end
  assert.deepEqual(out.slice(0, 3), ['a1', 'a2', 'a4']);                         // the oldest, a0, went
  assert.deepEqual(old, before);
  assert.deepEqual(plain(api.rememberSeen(['x', 'y'], ['y', 'z'])), ['x', 'y', 'z']);
  assert.deepEqual(plain(api.rememberSeen(['x', 'y'], ['z'], 2)), ['y', 'z']);
  assert.deepEqual(plain(api.rememberSeen([], [])), []);
  const big = Array.from({ length: 1500 }, (_, i) => `c${i}`);
  assert.deepEqual(plain(api.rememberSeen(big, [])), big.slice(500));            // the default limit is 1,000
});

test('unseen: the ids not delivered yet, in their order, once each', () => {
  const { api } = world();
  assert.deepEqual(plain(api.unseen(['a', 'b', 'c', 'b', 'd', 'a'], ['b', 'x'])), ['a', 'c', 'd']);
  assert.deepEqual(plain(api.unseen(['a'], [])), ['a']);
  assert.deepEqual(plain(api.unseen([], ['a'])), []);
});

test('poll(): only unseen mail, oldest first, with the key; only a 2xx is remembered; the heartbeat comes last', () => {
  const w = world({ inbox: many(4), props: { SEEN: JSON.stringify(['m0002']) },
                    status: (url, body) => (body.message_id === 'm0003' ? 500 : 200) });
  w.api.poll();
  assert.deepEqual(w.posts.map((p) => p.url), [`${BASE}/email`, `${BASE}/email`, `${BASE}/email`, `${BASE}/heartbeat`]);
  assert.deepEqual(emailIds(w), ['m0001', 'm0003', 'm0004']);
  assert.deepEqual(w.gets.map((g) => [g.user, g.id, g.format]),
                   [['me', 'm0001', 'full'], ['me', 'm0003', 'full'], ['me', 'm0004', 'full']]);
  for (const p of w.posts) {
    assert.equal(p.params.method, 'post');
    assert.equal(p.params.contentType, 'application/json');
    assert.equal(p.params.muteHttpExceptions, true);
    assert.deepEqual(plain(p.params.headers), { 'x-functions-key': KEY });
  }
  const first = w.posts[0].body;
  assert.deepEqual(Object.keys(first).sort(), ['date', 'from', 'message_id', 'subject', 'text', 'thread_id']);
  assert.equal(first.text, 'Item 1 sold');
  assert.equal(first.thread_id, 't0001');
  assert.equal(first.date, new Date(T0 + 60000).toISOString());
  assert.deepEqual(heartbeat(w), { source: 'gmail', info: { sent: 2, errors: 1, left: 1 } });
  assert.deepEqual(seenIn(w), ['m0002', 'm0001', 'm0004']);                     // the 500 is not in it
  assert.match(w.logs.at(-1), /^poll: sent 2, errors 1, left 1; heartbeat HTTP 200 \| m0003: HTTP 500$/);
  noSecretsLogged(w);

  fresh(w);                                     // the next run: the 500 is tried again, alone
  w.status = 200;
  w.api.poll();
  assert.deepEqual(emailIds(w), ['m0003']);
  assert.deepEqual(heartbeat(w), { source: 'gmail', info: { sent: 1, errors: 0, left: 0 } });
  assert.deepEqual(seenIn(w), ['m0002', 'm0001', 'm0004', 'm0003']);

  fresh(w);                                     // nothing new: only the heartbeat, no property written
  const writes = w.writes;
  w.api.poll();
  assert.deepEqual(w.posts.map((p) => p.url), [`${BASE}/heartbeat`]);
  assert.deepEqual(heartbeat(w), { source: 'gmail', info: { sent: 0, errors: 0, left: 0 } });
  assert.equal(w.writes, writes);
});

test('poll(): a message that can\'t be read or posted stays unseen, counts as an error and comes back next run', () => {
  const w = world({ inbox: many(3) });
  w.onGet = (id) => { if (id === 'm0002') throw new Error('Exception: Service unavailable: Gmail'); };
  w.status = (url, body) => (body.message_id === 'm0003'
    ? new Error(`Exception: Address unavailable: ${BASE}/email (sent with ${KEY})`) : 200);
  w.api.poll();
  assert.deepEqual(emailIds(w), ['m0001', 'm0003']);              // m0002 never got as far as a POST
  assert.deepEqual(heartbeat(w), { source: 'gmail', info: { sent: 1, errors: 2, left: 2 } });
  assert.deepEqual(seenIn(w), ['m0001']);
  assert.match(w.logs.at(-1), /m0002: Exception: Service unavailable: Gmail; m0003: Exception: Address unavailable: <API_URL>\/email \(sent with <API_KEY>\)/);
  noSecretsLogged(w);

  fresh(w);
  w.onGet = null;
  w.status = 200;
  w.api.poll();
  assert.deepEqual(emailIds(w), ['m0002', 'm0003']);
  assert.deepEqual(heartbeat(w), { source: 'gmail', info: { sent: 2, errors: 0, left: 0 } });
  assert.deepEqual(seenIn(w), ['m0001', 'm0002', 'm0003']);
});

test('poll(): every page is listed (100 a page), at most 50 go out a run, the oldest first', () => {
  const w = world({ inbox: many(120) });
  w.api.poll();
  assert.deepEqual(w.lists.map((l) => [l.user, l.q, l.maxResults, l.pageToken]),
                   [['me', Q2, 100, undefined], ['me', Q2, 100, '100']]);
  assert.deepEqual(emailIds(w), idsOf(1, 50));
  assert.deepEqual(heartbeat(w).info, { sent: 50, errors: 0, left: 70 });

  fresh(w);
  w.api.poll();
  assert.deepEqual(emailIds(w), idsOf(51, 100));
  assert.deepEqual(heartbeat(w).info, { sent: 50, errors: 0, left: 20 });

  fresh(w);
  w.api.poll();
  assert.deepEqual(emailIds(w), idsOf(101, 120));
  assert.deepEqual(heartbeat(w).info, { sent: 20, errors: 0, left: 0 });
  assert.deepEqual(seenIn(w), idsOf(1, 120));
});

test('poll(): SEEN holds 1,000 real-length Gmail ids though one property takes at most 9 KB', () => {
  const hex = (i) => `18b0c5e3f2a1${i.toString(16).padStart(4, '0')}`;   // 16 hex digits, like Gmail's ids
  const w = world({ inbox: newestFirst(Array.from({ length: 1000 }, (_, i) => mail(i + 1, { id: hex(i + 1) }))) });
  for (let run = 0; run < 20; run++) {
    fresh(w);
    w.api.poll();                               // the stand-in refuses a value over 9 KB, as Apps Script does
    assert.equal(emailIds(w).length, 50);
  }
  const all = Array.from({ length: 1000 }, (_, i) => hex(i + 1));
  assert.deepEqual(seenIn(w), all);
  assert.deepEqual([...w.store.keys()].filter((k) => k.startsWith('SEEN')).sort(), ['SEEN', 'SEEN_2', 'SEEN_3']);
  for (const [key, value] of w.store) assert.ok(value.length <= 9 * 1024, `${key}: ${value.length} characters`);

  fresh(w);                                     // all known: only the heartbeat
  w.api.poll();
  assert.deepEqual(w.posts.map((p) => p.url), [`${BASE}/heartbeat`]);

  fresh(w);                                     // 10 more: the newest 1,000 stay, the 10 oldest are forgotten
  w.inbox = newestFirst(w.inbox.concat(Array.from({ length: 10 }, (_, i) => mail(1001 + i, { id: hex(1001 + i) }))));
  w.api.poll();
  assert.deepEqual(emailIds(w), Array.from({ length: 10 }, (_, i) => hex(1001 + i)));
  assert.deepEqual(seenIn(w), Array.from({ length: 1000 }, (_, i) => hex(i + 11)));
});

test('poll(): only mail whose From address is at a marketplace is sent; a look-alike is remembered, never sent', () => {
  const w = world({ inbox: newestFirst([
    mail(1, { from: `"poshmark.com" <${addr('someone', 'example.test')}>` }),
    mail(2, { from: `Poshmark <${addr('notify', 'poshmark.com.example.test')}>` }),
    mail(3, { from: `Depop <${addr('noreply', 'mail.depop.com')}>` }),
    mail(4, { from: addr('no-reply', 'VINTED.COM') }),
    mail(5, { from: `Vinted <${addr('x', 'notvinted.com')}>` }),
  ]) });
  w.api.poll();
  assert.deepEqual(emailIds(w), ['m0003', 'm0004']);
  assert.deepEqual(heartbeat(w).info, { sent: 2, errors: 0, left: 0 });
  assert.deepEqual(seenIn(w), ['m0001', 'm0002', 'm0003', 'm0004', 'm0005']);
  assert.match(w.logs.at(-1), /^poll: sent 2, errors 0, left 0, skipped 3 \(not from a marketplace address\)/);

  fresh(w);                                     // none of them is fetched again
  w.api.poll();
  assert.equal(w.gets.length, 0);
});

test('poll(): curly quotes, accents and emoji reach the API intact in ASCII-only JSON', () => {
  const text = 'Sold: “Nike” Air Max — été 🎉';
  const w = world({ inbox: [mail(1, { subject: 'You made a sale 🎉', text })] });
  w.api.poll();
  const [email] = w.posts;
  assert.match(email.params.payload, /^[\x20-\x7e]*$/);
  assert.equal(email.body.text, text);
  assert.equal(email.body.subject, 'You made a sale 🎉');
  assert.deepEqual(heartbeat(w).info, { sent: 1, errors: 0, left: 0 });
});

test('poll(): no new email is started after 4 minutes; the rest wait, unseen, for the next run', () => {
  const w = world({ inbox: many(60), clock: true });
  w.onGet = () => { w.now += 10000; };          // every message takes 10 s
  w.api.poll();
  const expected = Math.floor(w.ctx.POLL_BUDGET_MS / 10000) + 1;   // 25
  assert.ok(expected < 50);
  assert.deepEqual(emailIds(w), idsOf(1, expected));
  assert.deepEqual(heartbeat(w).info, { sent: expected, errors: 0, left: 60 - expected });
  assert.deepEqual(seenIn(w), idsOf(1, expected));
});

test('poll(), install() and dumpSamples() refuse to start without API_URL and API_KEY, or with an http:// address', () => {
  for (const props of [{ API_URL: '' }, { API_KEY: '' }, { API_URL: 'http://api.example.test/api' }]) {
    const w = world({ inbox: [mail(1)], props });
    assert.throws(() => w.api.poll(), /API_URL|API_KEY/);
    assert.throws(() => w.api.install(), /API_URL|API_KEY/);
    assert.throws(() => w.api.dumpSamples(), /API_URL|API_KEY/);
    assert.equal(w.lists.length + w.gets.length + w.posts.length + w.triggers.length, 0);
  }
});

test('install() leaves exactly one 5-minute poll trigger, whatever was there; uninstall() removes it', () => {
  const w = world();
  w.addTrigger('poll', 10);
  w.addTrigger('poll', 5);
  w.addTrigger('somethingElse', 60);
  const polls = () => w.triggers.filter((t) => t.handler === 'poll');
  w.api.install();
  assert.deepEqual(polls().map((t) => t.minutes), [5]);
  assert.deepEqual(w.triggers.filter((t) => t.handler !== 'poll').map((t) => t.handler), ['somethingElse']);
  assert.match(w.logs.at(-1), /^install: poll runs every 5 minutes \(2 old trigger\(s\) replaced\)$/);
  w.api.install();
  assert.deepEqual(polls().map((t) => t.minutes), [5]);
  w.api.uninstall();
  assert.equal(polls().length, 0);
  assert.deepEqual(w.triggers.map((t) => t.handler), ['somethingElse']);
  assert.match(w.logs.at(-1), /^uninstall: 1 poll trigger\(s\) removed/);
  assert.equal(w.posts.length, 0);
  noSecretsLogged(w);
});

test('dumpSamples(): 90 days, every page, POSTed to /samples in batches of 50 (120 → 50, 50, 20)', () => {
  const inbox = many(120);
  const w = world({ inbox, props: { SEEN: JSON.stringify(['m0001']) } });
  w.api.dumpSamples();
  assert.deepEqual(w.lists.map((l) => [l.q, l.maxResults, l.pageToken]), [[Q90, 100, undefined], [Q90, 100, '100']]);
  assert.ok(w.posts.every((p) => p.url === `${BASE}/samples` && p.params.headers['x-functions-key'] === KEY));
  const batches = w.posts.map((p) => p.body.samples);
  assert.deepEqual(batches.map((b) => b.length), [50, 50, 20]);
  const samples = batches.flat();
  assert.deepEqual(samples.map((s) => s.message_id).sort(), inbox.map((m) => m.id).sort());
  assert.deepEqual(Object.keys(samples[0]).sort(), ['date', 'from', 'message_id', 'subject', 'text', 'thread_id']);
  assert.equal(samples[0].text, 'Item 120 sold');
  assert.equal(w.store.get('SEEN'), JSON.stringify(['m0001']));   // the poll's memory is not touched
  assert.match(w.logs.at(-1), /^dumpSamples: 120 emails in the last 90 days, 120 sent in 3 batches$/);
  noSecretsLogged(w);
});

test('dumpSamples(): stops reading before Apps Script\'s 6-minute limit and still sends what it read', () => {
  const w = world({ inbox: many(200), clock: true });
  w.onGet = () => { w.now += 3000; };           // every message takes 3 s
  w.api.dumpSamples();
  const read = Math.floor(w.ctx.SAMPLES_BUDGET_MS / 3000) + 1;    // 91
  assert.ok(read < 200);
  assert.deepEqual(w.posts.map((p) => p.body.samples.length), [50, read - 50]);
  assert.match(w.logs.at(-1), new RegExp(`stopped at the time limit after reading ${read}`));
});

// ---------------------------------------------------------------------------------------------------------------
// The files themselves

function filesUnder(dir) {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((e) => (e.isDirectory() ? filesUnder(join(dir, e.name)) : [join(dir, e.name)]));
}

test('nothing under gmail/ looks like a secret: no bot token, no Function address or ?code=, no email address', () => {
  const SECRET = {
    'a Telegram bot token': /\d{6,}:[A-Za-z0-9_-]{30,}/,
    'an Azure Functions address': /azurewebsites\.net/i,
    'a key in a URL': /\?code=/i,
    'an email address': /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/,
  };
  assert.match(`123456789:${'A'.repeat(35)}`, SECRET['a Telegram bot token']);    // the patterns do catch what
  assert.match(addr('seller', 'example.test'), SECRET['an email address']);       // they are there for
  const files = filesUnder(GMAIL);
  const names = files.map((f) => f.slice(GMAIL.length).replace(/\\/g, '/'));
  for (const name of ['Code.gs', 'README.md', 'appsscript.json']) assert.ok(names.includes(name), names.join(', '));
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    for (const [what, re] of Object.entries(SECRET)) assert.doesNotMatch(text, re, `${file} contains ${what}`);
  }
});

test('read-only: the manifest asks for exactly the three scopes; the only Gmail calls are messages.list, .get and attachments.get', () => {
  assert.deepEqual(JSON.parse(readFileSync(join(GMAIL, 'appsscript.json'), 'utf8')), {
    timeZone: 'America/New_York',
    runtimeVersion: 'V8',
    exceptionLogging: 'STACKDRIVER',
    dependencies: { enabledAdvancedServices: [{ userSymbol: 'Gmail', serviceId: 'gmail', version: 'v1' }] },
    oauthScopes: [
      'https://www.googleapis.com/auth/gmail.readonly',
      'https://www.googleapis.com/auth/script.external_request',
      'https://www.googleapis.com/auth/script.scriptapp',
    ],
  });
  assert.deepEqual([...new Set(SOURCE.match(/\bGmail\.Users\.[\w.]+(?=\s*\()/g))].sort(),
                   ['Gmail.Users.Messages.Attachments.get', 'Gmail.Users.Messages.get', 'Gmail.Users.Messages.list']);
  assert.doesNotMatch(SOURCE, /\b(GmailApp|MailApp)\b/);
});

test('Code.gs is plain Apps Script: ASCII only, no modules, no ES2020 syntax, the Node export guarded', () => {
  // pasted into a browser editor: an invisible or odd character could be dropped there, so other characters are escapes
  const odd = [...SOURCE].filter((c) => c.codePointAt(0) > 126).map((c) => c.codePointAt(0).toString(16));
  assert.deepEqual(odd, []);
  assert.doesNotMatch(SOURCE, /^\s*(import|export)\b/m);
  assert.doesNotMatch(SOURCE, /\?\.(?!\d)|\?\?/);              // optional chaining / nullish coalescing
  assert.match(SOURCE, /^if \(typeof module !== 'undefined'\) \{ module\.exports = \{ buildQuery, htmlToText, extractText, header, buildPayload, rememberSeen, unseen, poll, install, uninstall, dumpSamples \}; \}\s*$/m);
  for (const name of PUBLIC) assert.match(SOURCE, new RegExp(`^function ${name}\\(`, 'm'));
  const bare = vm.createContext({});            // as in Apps Script: no `module`, the functions are globals
  vm.runInContext(SOURCE, bare);
  for (const name of PUBLIC) assert.equal(typeof bare[name], 'function', name);
  assert.equal(typeof bare.module, 'undefined');
});

// ---------------------------------------------------------------------------------------------------------------
// WO33: the 51 "unreadable" of the first dumpSamples() -- why, and never a lost sale

test('readEmail_: a message Gmail refuses once is asked for again and read in full', () => {
  const w = world({ inbox: many(2) });
  let refused = 0;
  w.onGet = (id, opts) => { if (id === 'm0002' && opts.format === 'full' && !refused++) throw new Error('Exception: Too many concurrent requests'); };
  w.api.poll();
  assert.deepEqual(emailIds(w), ['m0001', 'm0002']);
  const sent = w.posts.find((p) => p.body.message_id === 'm0002').body;
  assert.equal(sent.text, 'Item 2 sold');
  assert.equal(sent.unreadable, undefined);
});

test('poll(): a message Gmail never gives in full still reaches the API by its headers -- a sale is never lost', () => {
  const w = world({ inbox: many(2) });
  w.onGet = (id, opts) => { if (id === 'm0002' && opts.format === 'full') throw new Error('Exception: Internal error'); };
  w.api.poll();
  const sent = w.posts.find((p) => p.body.message_id === 'm0002').body;
  assert.equal(sent.subject, 'Sale 2');
  assert.equal(sent.text, '');
  assert.match(sent.unreadable, /Internal error/);
  assert.deepEqual(seenIn(w), ['m0001', 'm0002']);            // delivered: not fetched again and again
  assert.match(w.logs.at(-1), /m0002: read by its headers only \(Exception: Internal error\)/);
  noSecretsLogged(w);
});

test('dumpSamples(): says why for the first five it reads by headers only, and still sends them', () => {
  const w = world({ inbox: many(8) });
  w.onGet = (id, opts) => { if (Number(id.slice(1)) % 2 === 0 && opts.format === 'full') throw new Error(`Exception: refused ${id}`); };
  w.api.dumpSamples();
  const samples = w.posts.flatMap((p) => p.body.samples);
  assert.equal(samples.length, 8);
  assert.equal(samples.filter((x) => x.unreadable).length, 4);
  const why = w.logs.filter((l) => /read by its headers only/.test(l));
  assert.equal(why.length, 4);
  assert.match(why[0], /"Sale \d" from Poshmark/);
  assert.match(w.logs.at(-1), /8 sent in 1 batches, 4 sent by their headers only \(their text unreadable\)/);
});

test('a body Gmail keeps as an attachment (a large HTML email) is fetched and read', () => {
  const m = mail(1, { subject: 'You made a sale!' });
  m.payload = { partId: '', mimeType: 'multipart/alternative', filename: '', headers: m.payload.headers, body: { size: 0 },
                parts: [{ partId: '0', mimeType: 'text/html', filename: '', headers: [],
                          body: { size: 900000, attachmentId: 'att-1' } }] };
  const w = world({ inbox: [m] });
  w.attachments.set('att-1', b64url('<html><body><p>You sold Naturino sneakers</p><p>$40.00</p></body></html>'));
  w.api.poll();
  const sent = w.posts.find((p) => p.body.message_id === 'm0001').body;
  assert.equal(sent.text, 'You sold Naturino sneakers\n$40.00');
  assert.deepEqual(w.attachmentGets.map((a) => [a.messageId, a.id]), [['m0001', 'att-1']]);
});

test('Apps Script hands a body over as bytes (a Byte[]), not base64 text: read as UTF-8 (WO33, live: 110 empty samples)', () => {
  const m = mail(1, { subject: 'You sold an item on Vinted', text: 'You sold J. Crew pants \u2014 $35.00' });
  const bytes = Array.from(Buffer.from('You sold J. Crew pants \u2014 $35.00', 'utf8'), (b) => (b > 127 ? b - 256 : b));
  m.payload.body = { size: bytes.length, data: bytes };       // what Gmail.Users.Messages.get gives inside Apps Script
  const w = world({ inbox: [m] });
  w.api.poll();
  const sent = w.posts.find((p) => p.body.message_id === 'm0001').body;
  assert.equal(sent.text, 'You sold J. Crew pants \u2014 $35.00');
});
