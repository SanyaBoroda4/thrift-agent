# Incident: Vinted listings not posting — 2026-10-20

Status: **investigation done (read-only), nothing changed on the Mac or on any site; waiting for Alex's OK** on the
pause, the reproduction runs and the fixes. Branch `incident/2026-10-20-vinted`. Evidence copied into `shots/` and
`logs/` next to this note (git-ignored: they hold the seller's account pages).

Times below are Eastern (the owner's). The Mac's event log is UTC (ET + 4 h); the screenshot names are ET.

## 1. Summary

- Six items priced after 23:00 on Oct 19 went up at 08:00–08:06 on Poshmark and Depop (all six, with all photos). On
  Vinted the poster made **three attempts in total**, one per item, and all three failed in different ways; at
  08:06:37 the "3 failures in a row" brake stopped Vinted for the rest of the window. The other three items were
  never tried. **Nothing has been retried since**, and nothing was published on Vinted: the pre-publish check
  cancelled two forms before any click; the third form got exactly one Upload click that Vinted did not act on.
- The failures: (a) Vinted's photo upload failed on the first job of the day — its own "Sorry, something went wrong /
  system error" dialog, 0 of 8 photos; (b) Vinted put its "Recommended" package size (Large) over our Medium a moment
  after our click, and our form check doesn't re-read the package unless something else was fixed; (c) one Upload
  click produced nothing — no listing, no error — the same shape as the Oct 16 Sam Edelman case.
- Since the last successful Vinted listing (the Zara blouse, Oct 19 22:43) **no extension, poster or Vinted code
  changed**; the same Chrome process has been running since Oct 6. What did change: 18 jobs on three sites in six
  minutes at 08:00, on a Mac that is out of memory (122 MB unused, 10.8 of 12 GB swap in use, load 4, up 26 days).
- The Vinted account looks healthy: no login, CAPTCHA, block or verification page at any point today; Vinted's own
  read endpoints (the wardrobe JSON, the orders JSON) answered normally at 08:03–08:06; the session refreshed itself.
- No public Vinted outage report for this morning was found (sources in §6). Vinted's HTTP answers to our upload
  and to the Upload click were **not recorded** — our evidence capture reads a buffer that was already full.
- Whose problem: (a) and (c) are Vinted's write calls failing with no usable feedback, on a starved Mac — cause not
  provable from the evidence; (b) is our code (no settle-and-verify after the package pick). The retry rules are ours
  too: a "Mismatch" row is retried every window with no cap, and the card only ever says "⏳ Vinted — later".
- There is no Vinted-only pause switch that survives the next window (§4). Vinted is stopped now; the next window
  (the Mac's next wake with the lid open, or a worker restart) would try again, up to three failures.

## 2. Root cause per failure type

### (a) Photos 0 of 8 + "Sorry, something went wrong" — i_261019_09053e, Thursday Boot Co combat boots, 08:00:11

How photos go up on Vinted: the extension fetches the item's prepared JPEGs from the local bridge, builds **one**
DataTransfer with all of them (8 here, 20 at most) and sets it on Vinted's hidden file input
(`[data-testid=add-photos-input]`); Vinted's own page script uploads them. The step then waits up to 60 s for 8
thumbnails and up to 50 s for them to load, takes the "photos" screenshot and reports `N/8 shown` — **a short count is
not a failure of that step** (`ext/content/vinted.js` lines 311–319, `common.js` `T.attach` / `T.waitCount` /
`T.waitLoaded`). The rest of the form is then filled, and the driver's pre-publish diff (`ext_driver.py` line 419)
catches the missing photos and cancels the job before any click. That is what happened:

- `shots/i_261019_09053e-vinted-20261020-080011.json`: step `photos` ok, detail "0/8 shown", 110,265 ms; every other
  step ✓; `seen.photos` "0" vs expected "8"; the diff `{'photos': ('8','0'), 'photos_loaded': ('8','0')}`; nothing
  clicked (`clicked: false`, the job cancelled).
- `shots/…-080011-photos.png` (taken when the step ended): Vinted's dialog **"Sorry, something went wrong — We're
  working to solve this system error now. Try refreshing the page. [Refresh page]"** over an empty photo area. In the
  HTML it is `data-testid="item-upload-critical-error-dialog"`; the upload grid has 0 images. The dialog is still there
  in the final form HTML (`…-080011.html`): the other fields were filled behind it.
- The job's progress (event `post_failed` 12:02:39 UTC): "tab opened · passed through /session-refresh · page loaded ·
  page script running · **the page reloaded (/items/new) — filling it again** · photos 0/8 · …": the first fill was
  interrupted by a navigation about 10 s into the job (148.7 s in all, 137.6 s for the second fill); the second fill's
  upload got the system error. What triggered the reload is not recorded (the extension records the fact only); the
  Depop jobs for the Hunter boots (08:00:48) and the H&M coat (08:02:05) reloaded once the same way and went on fine.
- The files are fine: the same 8 JPEGs were served by the same bridge to Depop at 08:00:11 ("photos 8/8", posted
  08:00:47) and Poshmark posted them too; the prep is the one every earlier Vinted listing went through (JPEG, EXIF
  applied). WO47's cleanup at 05:32 touched prep copies and sold items' photos only; these items' work copies are intact.
- Session: valid. The first page load went through Vinted's routine `/session-refresh`; no login / CAPTCHA / block /
  verification page was classified at any time today (such a page stops the site with a group line; none came). The
  wardrobe JSON was read at 08:03:40–08:06:37 (40 listings) and the orders JSON at 08:06:44 (17 orders).
- Anti-bot, IP, proxy, user agent: no proxy; Chrome's own user agent; the same Chrome process (pid 11547, 154.0.8037.98)
  since Oct 6; the home network (its IP last night vs this morning: **could not check**, not recorded).
- Load: 6 items × 3 sites between 08:00:11 and 08:06:37 — 18 jobs, Vinted's one at a time (3 in total). The failing one
  was the first Vinted job of the day; two later Vinted jobs uploaded 6/6 (4.6 s) and 7/7 (3.1 s) photos fine. No 429
  or other status is known (below).
- Vinted outage: **no report found** — sitedown.co.uk "no Vinted problems reported in the last 24 hours" (checked
  14:00 UK), the web search found nothing for Oct 20 (the nearest: an April 9 "something went wrong" wave); Downdetector
  **could not be fetched** from here.
- Network evidence: **none** — the extension records the site's request statuses only after the publish click, and
  even then from `performance.getEntriesByType("resource")`, a 250-entry buffer that a Vinted sell page fills before
  the form is even touched (the refusal in (c) came back with no requests at all). Chrome's own stderr around 08:00
  has only the routine "chrome://newtab for incorrect profile type" lines (the job tabs opening).

### (b) Package size LARGE instead of MEDIUM — i_261019_51cd24, Larroudé slide sandals, 08:02:50

- Our mapping: slide sandals → weight class "medium" → Vinted `MEDIUM` (`catalogs/common.py` `package_class`,
  `VINTED_PACKAGES`); shoes/boots → `LARGE` for the Thursday boots.
- How it is set (`vinted.js` `packageSize`, lines 250–264): after the price, among the cells
  `[data-testid$='-package-size--cell']` the one whose title span reads "medium" is clicked; the step then waits for the
  cell's radio to read checked (3 s) and returns. Today it took **1 ms**: the click landed and the radio read checked at
  once. The `check` step (lines 269–304) re-reads the package **only when another field had to be fixed** — nothing had
  to be — so the package wasn't looked at again. The read-back a second later: `package: "LARGE"`; the diff
  `{'package': ("one of ['MEDIUM']", 'LARGE')}`; cancelled before any click.
- Vinted's page did it: the saved form (`shots/…-080250.html`) shows the Large cell as the chosen one
  (`role="presentation"`) carrying Vinted's **"Recommended"** badge (`package-size-suggestion-badge-id-3`); Small and
  Medium are plain buttons. Vinted computes that recommendation server-side after the details are in and applies it —
  recorded on 2026-10-06 already (`ext/selectors.json` package → seen: "Vinted moved its own choice Small → Medium").
  Our click came 43 ms after the price was typed; the recommendation landed after it and won.
- Not a UI change: the DOM is identical to last night's successful Zara form (`shots/i_261019_a81526-…-224304.html`:
  cells `package-size-<n>`, radios `package_type_selector_<n>`, the same badge), where the recommendation was X-Small
  and our Small stayed (the package step took 25 ms there). Nothing of ours touches the category after the package
  (order: category → brand → size → condition → colours → materials → price → package → check). The two other jobs
  today wanted the size Vinted had preselected (LARGE for the boots, MEDIUM for the espadrilles): 0 ms, kept.
- The stand-in tests cover "our size over the one Vinted recommends" and "sizes drawn late are waited for", but not
  "Vinted re-selects its recommendation after our click" (`tests/js/content.test.mjs` 141, 744). That is the gap.

### (c) One Upload click, nothing listed, no error — i_261019_3ba524, Tory Burch espadrille flats, 08:03:21

- The form read back exactly as planned (7/7 photos, Tory Burch as an exact brand, size 6, Very good, Beige + Gold,
  Leather, $115, MEDIUM — Vinted's own preselection) and the driver sent its one go-ahead at about 08:03:35 (`steps`:
  `submit` clicked once). The page stayed the sell form: no alert, no snackbar, no field marked invalid
  (`shots/…-080321-refused.png/.html`: the form with its 7 photos, no error markers at all). The extension then
  reported "refused: no error shown on the form"; the driver looked Vinted's own data up (the wardrobe's 40 newest
  items, by the exact title) from 08:03:40 to 08:06:37: absent → `Vinted refused the form: no error shown on the form`
  (event `post_failed` 12:06:37 UTC, `clicked: false` because the site's data agreed nothing went up).
- Same shape as 2026-10-16 (WO39: the Sam Edelman sandals' form kept twice after Upload with nothing on the page; the
  third identical form went live) — Vinted's item-create request failing with no UI feedback. Its HTTP status: **could
  not check** (the request log after the click came back empty — the buffer problem above).
- The silent-refusal path then did not run: its 10-minute re-check (due 08:16:37) is skipped while the site is stopped
  for the window (`parallel.py` line 348), and at 09:15:16 the API's safety net (`watch.unposted`, 15-minute timer)
  re-queued the row because its reason list doesn't know "refused" — kv `unconfirmed_state` still says `stage: wait,
  refused: true` and the row is `queued` with "retried by the safety net (was: Vinted refused the form…)".

### For all three: why "later" doesn't recover, and the brakes

- Retry rules (`crosslist.py` `new_window` lines 166–192, `failure` 222–231; `parallel.py` 340–373): at every new
  window the blocks and streaks are reset; a failed row with fewer than 3 attempts goes back to `queued`, a **"Mismatch"
  row at any number of attempts** (by design, WO36 §4: nothing was published, it waits); the 3rd failure in a row in
  one window stops the site until the next window. There is no backoff between windows, no cap for Mismatch rows, no
  message naming the failing step (the private ❌ line quotes the diff; the group card says "⏳ Vinted — later").
- A "window" begins at a worker start, or a wake with the lid open, or the lid opening (`daily.py` `Window.step`);
  today's began at 05:32 (the deploy's restart). Vinted has been stopped in it since 08:06:37 (kv `crosslist_blocked`:
  "failures: 3 in a row"). So: **no retry is scheduled by time**; the next window is whenever the Mac next wakes
  open or the worker restarts, inside 08:00–23:00. It would then: re-check the Tory Burch by Vinted's data (absent →
  counts as failure 1), then post the queue in order (Thursday boots → Hunter → Larroudé → Tory Burch → AllSaints →
  H&M), stopping again at the 3rd failure.

### Brand matching (secondary)

- "Thursday Boot Co" vs "Thursday Boot Company": `brands.key` compares names with case, spaces, punctuation and
  accents dropped and "&" = "and" — `thursdaybootco` ≠ `thursdaybootcompany`; the only other rule, `extends`, takes an
  option that is our whole words **followed by more words** ("Alo" → "Alo Yoga"); "Company" replaces "Co" rather than
  following it, so no match → Poshmark's Brand left empty, Depop "Other", Vinted "List without brand" (Vinted doesn't
  list the brand at all: it offered Dr. Martens, Zara, UGG, Timberland, Tamaris, New Look). Co / Co. / Company / Corp /
  Inc / Ltd are not normalised anywhere (`thrift_agent/brands.py`, `ext/content/common.js` `T.brandKey`).
- "AllSaints" → Poshmark "All Saints": matched by the same key (`allsaints`) and reported as a spelling — correct.
- "Larroude": Depop's note says it offers nothing for it; Vinted offered Birkenstock, Havaianas, Primark, Shein,
  Crocs, Zara. The accent (Larroudé) would not have mattered (the key drops accents): the brand is simply not listed on
  either site. "Other" / "List without brand" is the right outcome there.

## 3. Whose problem

| | ours | Vinted | the Mac | account/session |
|---|---|---|---|---|
| (a) photos 0/8 + system error | the step passes a 0-photo upload on and doesn't read the dialog; no request log | the upload failed with its "system error" dialog | a reload mid-fill on a swapping Mac (18 jobs in 6 min) — possible, not proven | healthy |
| (b) package Large | yes: no settle-and-verify after the pick; the check skips the package | its late "Recommended" overrides a pick made just before | — | — |
| (c) silent Upload | evidence capture too weak to show Vinted's answer | the item-create request did nothing, no feedback (as on Oct 16) | possible, not proven | healthy |
| retries / messages | yes: Mismatch retried every window without cap; "later" never names the step | | | |
| brands | yes: no Co/Company normalisation, no alias | | | |

## 4. Hammering: current state and how to pause Vinted only

- Today: 3 attempts, then the brake at 08:06:37; nothing on Vinted since (the orders sweep at 08:06:44 and the bulk
  photo check at 06:40 are reads). **No automatic retry until the next window.**
- There is **no Vinted-only pause switch** that outlives the window and can be flipped without the owner's config or a
  restart: the window block is per window id; `PAUSE` stops all three sites; `marketplaces.vinted.enabled` is read at
  the poster's start from `config/settings.local.yaml` on the Mac (the owner's file, mtime Oct 9).
- How I would pause Vinted only (needs Alex's OK, twice over — the owner's file and a restart): set
  `marketplaces.vinted.enabled: false` in `config/settings.local.yaml` on the Mac, then
  `services.sh stop poster` → wait for "off" (idle: `busy null`) → `services.sh start poster`. Poshmark and Depop keep
  posting; the queued Vinted rows wait untouched; take-downs on Vinted would wait too (a sale elsewhere then needs the
  owner's hand on Vinted — one more reason to keep the pause short). Undo: the same line back to `true` + the same
  idle restart.
- Lower-impact alternative if the Mac is left open: do nothing — the brake holds until the lid is closed and opened
  again, or the worker restarts. With the fix deployed the point is moot.

## 5. Items shared since 2026-10-19 22:00 and the backlog plan

| item | title | price | photos | Poshmark | Depop | Vinted |
|---|---|---|---|---|---|---|
| i_261019_a81526 | Zara Kelly Green Satin V-Neck Ruffle Blouse Long Sleeve size S | $15 | 5 | posted 22:43 ([link](https://poshmark.com/listing/Zara-Kelly-Green-Satin-VNeck-Ruffle-Blouse-Long-Sleeve-size-S-6ad6d53a105c58b9598c5efe)) | posted 22:43 ([link](https://www.depop.com/products/tetbanasorokina-zara-kelly-green-satin-v-neck-4262/)) | **posted 22:43:34** ([10452462240](https://www.vinted.com/items/10452462240)) — the last successful Vinted listing |
| i_261019_09053e | Thursday Boot Co Black Lug Sole Front-Zip Combat Boots size 7 | $75 | 8 | posted 08:01:37 ([link](https://poshmark.com/listing/Thursday-Boot-Co-Black-Lug-Sole-FrontZip-Combat-Boots-size-7-6ad757d62fc9f88521bdf82e)) | posted 08:00:47 ([link](https://www.depop.com/products/teti2nasorokina-thursday-boot-co-black-lug-ae66/)) | failed, 1 attempt 08:00:11–08:02:39: Mismatch photos 8 vs 0 (Vinted's system-error dialog); next: next window |
| i_261019_5499b2 | Hunter Black Chelsea Rain Boots Lug Sole Pull Tab size 8 | $100 | 9 | posted 08:02:25 ([link](https://poshmark.com/listing/Hunter-Black-Chelsea-Rain-Boots-Lug-Sole-Pull-Tab-size-8-6ad758261a166d79c90506f4)) | posted 08:01:10 ([link](https://www.depop.com/products/tet3anasorokina-hunter-black-chelsea-rain-boots-dd28/)) | queued, 0 attempts (Vinted stopped before its turn); next: next window |
| i_261019_51cd24 | Larroude Denim Blue Floral Embroidered Slide Sandals size 8.5 | $90 | 6 | posted 08:03:40 ([link](https://poshmark.com/listing/Larroude-Denim-Blue-Floral-Embroidered-Slide-Sandals-size-85-6ad75858eeedbea8c6672e78)) | posted 08:01:30 ([link](https://www.depop.com/products/tytianasorokina-larroude-denim-blue-floral-embroidered-cd78/)) | failed, 1 attempt 08:02:50–08:03:17: Mismatch package MEDIUM vs LARGE; next: next window |
| i_261019_3ba524 | Tory Burch 100% Genuine Leather Taupe Suede Espadrille Flats Gold Logo size 6 | $115 | 7 | posted 08:04:29 ([link](https://poshmark.com/listing/Tory-Burch-100-Genuine-Leather-Taupe-Suede-Espadrille-Flats-Gold-Logo-size-6-6ad758a1353534dc7027ff3c)) | posted 08:01:49 ([link](https://www.depop.com/products/tetianasorwkina-tory-burch-100-genuine-leather-76df/)) | queued again (the API's safety net, 09:15:16), 1 attempt 08:03:21–08:06:37: one Upload click, form kept, "refused: no error shown on the form"; next: re-checked by Vinted's data, then posted again, next window |
| i_261019_def149 | AllSaints 100% Genuine Leather Bales Biker Black Lamb Moto Jacket size 6 | $260 | 15 | posted 08:05:27 ([link](https://poshmark.com/listing/AllSaints-100-Genuine-Leather-Bales-Biker-Black-Lamb-Moto-Jacket-size-6-6ad758d15ad4be6a9a7bf33f)) | posted 08:02:04 ([link](https://www.depop.com/products/tetianasorokinr-allsaints-100-genuine-leather-bales-39d0/)) | queued, 0 attempts; next: next window |
| i_261019_77057e | H&M Houndstooth Wool-Blend Pea Coat, Black & White Notched Lapel size XS | $30 | 5 | posted 08:06:16 ([link](https://poshmark.com/listing/HM-Houndstooth-WoolBlend-Pea-Coat-Black-White-Notched-Lapel-size-XS-6ad7590a875d651d68d15a8e)) | posted 08:02:28 ([link](https://www.depop.com/products/tetianasorokinb-hm-houndstooth-wool-blend-pea-coat-0cc4/)) | queued, 0 attempts; next: next window |

(Depop takes 8 photos at most and Vinted 20; Poshmark all. Vinted rows all time: 46 `posted` — two of them listed by
the owner's hand — the newest the Zara blouse above, the one before it Oct 18 17:42; 2 delisted, 3 skipped, 2 failed
and 4 queued, the last two groups being today's.)

Backlog plan (Phase 7, after the fix and Alex's OK): the poster posts the six in queue order, one at a time, with
Vinted's listing gap set to 2–5 minutes for that run; before each one the existing last look by Vinted's own data
(the wardrobe JSON, exact title) so nothing is listed twice — extended to every queued Vinted row for this run; after
each, the confirmation by address edits the item's ✅ card with the Vinted link and a private line names it; I watch
it live and stop the poster at the first new failure.

## 6. What changed between the last success and the failures — and what didn't

- Code deployed to the Mac at 23:03 (fd47048) and 05:32 (00b4712 … 9b636a0): `cleanup.py` (new), `approve.py`,
  `uploads.py`, `pipeline.py`, `cli.py`, `post/poshmark.py`, the API and tests. **Not touched:** `ext/` (last change
  f6d7ede, Oct 18 19:43, deployed before the Zara success), `bridge.py`, `post/ext_driver.py`, `crosslist.py`,
  `post/parallel.py`, `catalogs/`. No dependency change (the venv's newest entries are the editable install of
  05:25).
- Browser: the Thrift Chrome is the same process since Oct 6 18:54 (154.0.8037.98). Google Chrome on disk was updated
  to 155.0.8059.85 on Oct 19 19:09 (the 154.0.8037.98 framework is still on disk, so the running copy is intact) — a
  relaunch has been pending since Oct 16. Not changed between 22:43 and 08:00.
- Config: `config/settings.local.yaml` Oct 9, `.env` Oct 8, `private/settings.yaml` Oct 7 — unchanged.
- The Mac at 10:09 today: 8 GB RAM with **122 MB unused, 3.0 GB compressed, swap 10.8 of 12 GB used**, load 3.8–4.2,
  up 26 days, 11.9 GB disk free; renderer processes from Sep 25, Sep 30, Oct 6 and Oct 8 still alive in the Thrift
  Chrome. At 08:00 the three site posters, the worker and two Chromes were all busy at once. A factor for the mid-fill
  reloads and the failed upload: plausible, **not proven** (no memory record from 08:00 exists).
- Outage sources: [sitedown.co.uk Vinted status](https://sitedown.co.uk/status/vinted.co.uk) (no problems in the last
  24 h, checked 14:00 UK, Oct 20); the web search surfaced only [an April 9, 2026 report of "something went wrong"
  pop-ups](https://www.nationalworld.com/business/consumer/is-vinted-down-users-report-issues-with-second-hand-selling-platform-6569469)
  and [updownradar's September page](https://updownradar.com/status/vinted.co.uk); Downdetector could not be fetched.

## 7. Proposed fixes (Phase 6, after approval) — minimal, with risks and tests

1. **Photos (vinted.js, common.js).** The photo step fails the attempt when the count is short: if Vinted's
   `item-upload-critical-error-dialog` is on the page, the step ends as "Vinted: system error during the photo upload";
   the job is then retried on a fresh page at most twice, 20–30 s apart (the extension already re-fills after a
   reload; we would trigger the reload ourselves — never Vinted's "Refresh page" button, a plain `location.reload()`),
   else the attempt ends there with the dialog's text as the error and nothing else filled. A short count with no
   dialog: the missing files attached once more (Vinted keeps the loaded ones), then the same rule. Risk: a slow upload
   on a swapping Mac now fails faster instead of filling a form it can't publish — acceptable, nothing was publishable
   anyway. Tests: jsdom cases for the dialog (retry then fail), the short count re-attached, the unchanged happy path.
2. **Package (vinted.js).** After the price, wait for the panel to settle (`T.quiet`, and the Recommended badge when it
   is coming, ≤ 2 s); after the pick, watch 800 ms and re-pick once if Vinted changed it; the `check` step always
   compares the package (today only after another fix). Tests: the stand-in's recommendation arriving 300 ms after our
   click. Risk: none beyond ~1 s per listing.
3. **Evidence (common.js).** Request statuses via a `PerformanceObserver` started at the beginning of the photo step and
   again at the click (not the 250-entry buffer), so the next refusal and the next failed upload carry Vinted's HTTP
   answers; the dialog's text and the reload trigger (`tabs.onUpdated` details) recorded with the job. Risk: none.
4. **Retries and messages (crosslist.py, runner.py, parallel.py, the API's watch.py and sales.py).** A Mismatch row
   gets three windows, then `skipped` with the group's ⏭ 'retry' line; every ❌ line names the step and the next try
   ("photos: 0 of 8 — Vinted's system error; next try when the Mac is next open, from 08:00"); the card's
   "⏳ Vinted — later" becomes "⏳ Vinted — <step> failed, next try <when>" for a failed row; "refused the form" added
   to both safety-net reason lists so the Mac's own re-check flow isn't pre-empted; the silent-refusal re-check allowed
   to run while the site is stopped by *failures* (it is a read). Tests: unit tests on the window transitions and the
   texts.
5. **Brands (brands.py, common.js, data/brand_aliases.json).** Co / Co. / Company / Corp / Inc / Ltd normalised in
   `key` on both sides (so "Thursday Boot Co" = "Thursday Boot Company"), plus the alias; unit tests for Thursday Boot
   Co → Thursday Boot Company and AllSaints → All Saints; Larroude stays "Other" where unlisted.
6. **One dry run per failure type on the Mac** (Phase 4, stage form, never the Upload click): the Thursday boots and the
   Larroudé, with the new evidence capture on, to see Vinted's answers; a headed run is what the extension always is.
7. **The Mac (Alex's call, not code):** relaunch the Thrift Chrome while the poster is idle (`services.sh stop chrome`
   / `start chrome`; the profile and the login stay) to drop two weeks of renderers and pick up Chrome 155; consider a
   reboot (26 days up, 10.8 GB in swap).

## 8. What could not be checked, and why

- Vinted's HTTP responses to the photo upload and to the Upload click (not recorded; the buffer problem).
- What navigated the Vinted tab (and the two Depop tabs) once during the fill at 08:00–08:02 (the extension keeps the
  fact, not the trigger; Chrome's stderr has nothing but routine lines then).
- Whether Vinted had an incident at 08:00 ET (no public report found; Downdetector unreachable from here).
- The home network's public IP at 22:43 vs 08:00 (not recorded; no proxy or VPN is used).
- The Mac's memory at 08:00 (no record; the 10:09 figures are the nearest).

## 9. Evidence index

- `logs/db-state-2026-10-20_0953ET.txt` — the listings, items and kv at 09:53 ET (read-only dump).
- `logs/db-events-since-2026-10-19_22ET.txt` — every post_posted / post_failed / blocked / window / orders / telegram
  event since 22:00 ET, with each job's progress lines.
- `logs/worker.log`, `logs/poster.log`, `logs/poster.err`, `logs/chrome-cross.err` — the Mac's logs as of 09:56 ET.
- `shots/i_261019_09053e-vinted-20261020-080011*` — (a): the job JSON, the form, the photo-step and photos-end
  screenshots with their HTML, the brand menu.
- `shots/i_261019_51cd24-vinted-20261020-080250*` — (b): the job JSON (package step 1 ms), the form HTML with the
  Recommended badge on Large.
- `shots/i_261019_3ba524-vinted-20261020-080321*` — (c): the job JSON (submit clicked once, `after`: refused), the
  form before the click, the "refused" page after it.
- `shots/i_261019_a81526-vinted-20261019-224304*` — the last successful Vinted listing (22:43), for comparison, and
  its photo-read of 22:56.

## 10. Phase 1 map (for reference)

- **Where it runs:** the owner's MacBook Pro (M2, 8 GB, macOS 27), two launchd services — `com.thriftagent.worker`
  (`thrift run`: the iCloud inbox and the Azure uploads, the pipeline, the Telegram bot) and the poster
  (`thrift poster`, LIVE since Oct 5): three site workers in one process. Started by launchd; redeployed by
  `deploy/deploy.ps1` from the PC (git pull, tests, worker restart; the poster only by hand when idle).
- **Scheduler:** the daily "window" (above); posting hours 08:00–23:00 ET; no pause between listings (the owner's
  rule); 25 listings a day per site; the per-site brake of 3 failures in a row; the housekeeping loop every 5 min
  (new windows, feeds, photo checks, price drops).
- **Queue and state:** SQLite `~/thrift/var/state.db` — `items`, `listings` (one row per item and site: queued →
  posting → posted | failed | skipped), `events`, `kv` (the window, the blocks, the streaks, the poster heartbeat);
  mirrored every minute to thrift-api on Azure (Postgres) for the dashboard and the safety nets.
- **Logs and screenshots:** `~/thrift/logs/` (worker.log; poster.log only start/stop lines; chrome-cross.err is the
  Thrift Chrome's stderr); the poster's detail is the `events` table (step progress per job) and
  `~/thrift/var/failed/shots/<item>-<site>-<ET stamp>[-label].{png,html,json}` — the JSON is the job trace (steps
  with ms, seen vs expected, diff, after-click event).
- **Telegram:** the worker's bot — the group gets the cards (✅ … 🟣 Poshmark / 🔴 Depop / 🟢 Vinted, or "⏳ Vinted —
  later"), the private chat gets every ❌ with its screenshot and the "⛔ … refused … reply 'retry'" lines.
- **The Vinted posting module:** no automation library — a Chrome extension (`ext/`, Manifest V3, plain JS content
  scripts) in a second ordinary Google Chrome ("the Thrift Chrome", profile `~/thrift/chrome-cross`, LaunchAgent
  `com.thrift.chrome-cross`), driven by the poster over a local WebSocket bridge (127.0.0.1:8765, a token). Headed —
  a real window that is never focused or minimized; no proxy; Chrome's own user agent; the cookies of the owner's hand
  login in that profile, refreshed by Vinted itself (`/session-refresh`); nothing is ever typed into a login or a
  CAPTCHA. Photos: the extension fetches the prepared JPEGs from the bridge and sets them on the form's file input as
  Files, all at once; Vinted's page uploads them. The form is read back and diffed against the plan before the one
  Upload click; the new listing is confirmed by the landing page or Vinted's wardrobe JSON.
