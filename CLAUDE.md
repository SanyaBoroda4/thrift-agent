# Thrift Agent — spec for Claude Code

Photos shot on an iPhone become live Poshmark, Depop and Vinted listings with no typing. The seller shoots, taps Share
→ "New item"; the Mac groups the photos into items, writes the listings, asks her only for the price in Telegram, posts
on all three sites at once, and takes an item down everywhere when it sells.
**History and evidence (the per-WO sections, live incidents, superseded rules, the full verified-selector log):
`docs/history.md`.** This file is the rules in force and what the code does now.

## Machines
- **Windows PC (dev)** — code, tests, eval on fixture photos. `machine_role: dev` forces dry-run; the dev machine never
  logs into or posts to a marketplace.
- **MacBook (prod)** — MacBook Pro 13" M2, 8 GB, macOS 27 Golden Gate, Python 3.14 (python.org), bash, no Homebrew, no
  Node. Repo `~/thrift-agent`, venv `.venv`, logs `~/thrift/logs`, DB `~/thrift/var`. launchd agents: the worker
  (`thrift run`, `com.thriftagent.worker`, KeepAlive), the poster (`thrift poster`: Poshmark's Playwright Chrome profile,
  created and logged in on the Mac only — cookies are keychain-bound) and **the Thrift Chrome** (`com.thrift.chrome-cross`,
  profile `~/thrift/chrome-cross`, its own Dock icon): an ordinary Chrome with the crosslister extension (`ext/`) that
  lists on Depop and Vinted; the owner logs in there by hand — WO54 §4: not her browser; launchd never starts it, the
  poster opens it for work and quits it after 10 idle minutes. Since WO51 the Mac stays on. **The quiet hours**
  (WO54 §3): 8:30 AM – 2:00 PM the calendar job `com.thriftagent.quiet` unloads all of it (The quiet hours, below).
- **iPhone (dedicated)** — same Apple ID; the Shortcut saves each share to iCloud Drive `Posh/inbox/<share id>/` and
  uploads it to thrift-api (`docs/SHORTCUT_UPLOAD.md`).
- **thrift-api** — an Azure Function (`api/`) + Postgres: sale emails, take-down tasks, watchdogs, the dashboard.
- Seller data (brand tiers, listing examples, sales findings, account notes, the closet username) lives in `private/`, a
  separate private repo cloned here and git-ignored. Read `private/NOTES.md` before changing prompts, pricing or gate
  rules. Without `private/` the code falls back to `config/*.example.yaml` and `data/style_examples/`.

## Invariants — do not break these
1. **Facts before prose.** `extract` never writes copy; `copy` only restates Facts; null facts are omitted. Materials need
   label evidence (`facts.material`); premium details only what a label or photo shows; retail screenshots give price,
   style name, colour, retailer only — never condition, size or flaws.
2. **Condition.** NWT only with a hang-tag photo (`hang_tag_photo`) or the owner's word ("NWT" reply / `condition nwt`).
   Never Fair (a fair reading lists as Good, with a note). Torn between grades → the higher of good / excellent /
   like_new (`pipeline.settle_condition`); excellent = Like New on Poshmark. **No wear words** anywhere (dirt, stain,
   scuff, worn, wear and tear, fraying, pilling, hole, tear, smell…; a cutoff's frayed / raw hem is its style, on cutoffs
   and jeans only); a used item says ONE line, "Gently pre-loved, please see photos for condition."; every flaw has its
   photo in the listing, never as the cover.
3. **The model never clicks publish.** Code fills, reads back, diffs, then presses Publish / Save / Delete once. The
   model may only help FIND a fill field (WO52 heal) — never a publish, save, delete, submit, discard, boost, bump, hide
   or confirm control.
4. **Idempotent posting, never retry blind.** Row → `posting` before Poshmark's form opens / at the extension's ONE
   go-ahead. A `posting` row after a crash is never retried; it is reconciled against the closet / the site's own data.
   One item posts once per marketplace, ever. An unconfirmed publish is "verifying" (WO53): reposted only with proof —
   two reads of the site's own data that show it absent, 10+ minutes apart, the last 30+ minutes after the click
   (unreadable is never absent) — once, after one last look; never a question, never a failure (no pause).
5. **Stop, don't guess.** Logged out / CAPTCHA / block / verification page → that site stops, never a workaround; a
   sign-in, a one-time code, a real CAPTCHA and a "verify you are human" page that stayed are HUMAN STEPS (WO56). Unknown modal → fail the item with a screenshot. Never solve CAPTCHAs, never type credentials.
6. **Pacing and hours.** No posting hours (WO54 §3): every hour outside the owner's quiet hours (8:30 AM – 2:00 PM
   New York, `quiet:`), posting included. Caps (25 a site), PAUSE and stops stay; no sharing / following / offers /
   relisting from this code; gaps 0 (WO37). WO53 rule 6 (`scheduler.outside_hours`) is moot.
7. **All selectors live in `post/<marketplace>.py:SEL` and `ext/selectors.json`.** Role/label/placeholder/text locators
   or the form's own hooks (data-vv-name, data-et-name, data-test, data-testid, ids); a CSS class only where verified
   live. Real clicks only. Unverified entries in `UNVERIFIED` / `"verified": false`; `submit()` refuses while a step it
   needs is unverified. The one other place: a FILL field's selector learned when the site moved it
   (`learned_selectors.json` beside the DB, said once privately, to be copied into SEL / selectors.json).
8. **Shipping guard.** `HOLD_UNSHIPPED` blocks publishing (not drafts or dry runs).

**The owner's standing rules:**
- **Only four things wait for a person (WO54 §2,** the owner 2026-11-09: "take your own decisions in order to post it
  other than just hold off till human will intrude"): her price (a button, a typed number, `thrift price`); a size
  no photo shows (the card's size buttons); a password or sign-in code; a real CAPTCHA — for the last two a HUMAN
  STEP (WO56, below): ONE private message with the page's screenshot and "reply show" — never the group, never a 🚨
  — reminders every 12 h and once at 2:05 PM after the quiet hours. The agent never types a password, never solves a
  CAPTCHA, never uses a solving service. WO56 §6 adds one human step no code can take: a share whose upload didn't
  finish (only her phone has the photos) — ONE group line asks her to share them again, no reply. **Everything else is decided**: the most reasonable choice, the post, and ONE line in
  the item's private "Check —" message (`crosslist.item_checks`) — a brand not read surely is the reading when it
  is a big brand, else no brand (`pipeline.settle_brand`); NWT without a tag photo is Like New; a category Poshmark
  lacks becomes the closest real one (`taxonomy.closest`); a title nothing fixes is built from the facts
  (`titles.fallback`); a possible re-share is settled by the photo rules at her price; a grouping that isn't a
  partition is put right by code (`repair_grouping`); a category of another family becomes the closest of the
  item's own (WO54 §1.4). The card states decisions (⚠️), never asks (❓), and is corrected by a reply (`size … ·
  brand … · no brand · condition … · category … · men / women · boys / girls · cover N`). Every failure is tried
  again by itself another way (rule 4 below; a refused form: once 30 min later, then once per code update with ONE
  ⛔ and ONE 🚨). **Nobody is asked to check a site, ever** (WO53). `tests/test_wo54_decide.py` lists every string
  that asks a question or prompts a reply (ALLOWED); only the price and the size may wait.
- **Never leave an item unposted (WO52).** No field ever stops a listing: brands, sizes, categories fall back to the
  closest real value (never a made-up one; a big brand never "no brand"); site changes are healed or found by the form
  check; dead browsers are reopened; what still needs code is ONE 🚨 alarm within 20 minutes.
- **No message twice; a quiet group.** The group gets only cards, the allowed questions, the ✅ cards and plain
  action-needed lines — no ids, errno, tracebacks; everything technical goes to the private (ops) chat. Every reply of
  hers gets an answer at once. No message ever needs a terminal command.
- **Copy:** brand first in titles; shop-window titles (no shouting caps, codes, odd characters); US sizes only in titles;
  no style tags on any site; the cover is the FRONT of the whole item; a set goes under its bottom; no trademark that
  isn't the item's own.
- **Her hand edits are never undone** (price, title, brand changed on a site: recorded, left as they are).

## Working on the Mac (over SSH) and the live sites
- Find the Mac with `tailscale status` and SSH to its Tailscale address with `-o HostKeyAlias=192.168.x.x`; fallback
  `<mac-host>.local`, then the LAN IP; none answers → ask the user for the IP and wait, never assume.
- **Deploy:** `deploy\deploy.ps1` (defaults from git-ignored `deploy/mac.local.json`; `-MacHost`, `-HostKeyAlias`,
  `-NoPush`): `git push`, then on the Mac `deploy/mac_deploy.sh` — `git pull`, `git -C private pull`, `bash
  deploy/mac_setup.sh` (venv, folders, the tests, the launchd files; the extension token once), the worker restarted,
  the Thrift Chrome started if not running (never restarted), then `services.sh status`, `thrift status`, the worker
  log. A failed setup or test run rolls back to the previous commit; any failure exits non-zero. Deploy never starts the
  poster.
- **After every work order** Claude deploys, checks `services.sh status`, `thrift status` and the logs, and reports them.
  The golden replays run in the Mac's pre-deploy tests: re-record them (`thrift replay --golden --record`) after a
  prompt / model change BEFORE deploying.
- **Publishing is LIVE** (since 2026-10-05; the owner's keys — never switched to dry-run by Claude). A WO that changes
  the poster restarts it only when idle: `services.sh stop poster` → wait until `status` says off (ExitTimeOut 300 s) →
  deploy → `services.sh start poster` → confirm "Poster started (LIVE)" (event `poster_started` `{"live": true}`, the
  poster log's first line, the private chat).
- `bash deploy/services.sh start|stop|restart worker|poster`, `start|stop chrome`, `stop all`, `status`, `logs
  [worker|poster] [lines]`. One worker ever (`worker.lock` beside the DB; a second exits; `start worker` refuses while a
  Terminal `thrift run` runs). One-off GUI jobs: `bash deploy/gui_once.sh <thrift args>`.
- **Never on the Mac 8:30 AM – 2:00 PM** (the owner's quiet hours) beyond light read-only SSH. `mac_deploy.sh` refuses
  from 8:20 to 2:00 PM (the pulled code rolled back, exit 1; `THRIFT_DEPLOY_ANYWAY=1` — an emergency with her
  go-ahead — sets up and tests and starts nothing). The quiet hours' job starting the poster at 2:00 PM is the
  owner's design; a deploy still never starts it. The deploy wrapper on the Mac (`~/thrift/deploy_wrapper.sh
  <rollback commit>`, run detached; the private bundle shipped first): `START_ON_FAIL=0` leaves the poster stopped
  when the deploy fails (WO54, live 14:09: a rollback had started the morning's poster code). No test may read the
  Mac's own state (`tests/conftest.py` patches `runner.thrift_chrome_running` too).
- **Allowed over SSH without asking:** `bash deploy/quiet.sh check|status`, `thrift quiet --status`, the deploy, status, logs, `thrift requeue`, `thrift redo`, the tests, read-only
  commands (`doctor`, `shares`, `sales`, `show`), `thrift telegram test`, restarting the poster as above.
- **Never over SSH (the owner's):** `thrift poster --publish-first`, `thrift login`, `thrift quiet --post-now|--quiet-now` (hers), starting the poster outside a
  deploy restart or anything else that publishes or touches the live accounts by hand, `thrift mark-posted`, deleting
  data (a Vinted listing only through `thrift vinted-repost`, per item, with the owner's go-ahead), changing `config/settings.local.yaml` or `.env`, switching autopublish or dry-run keys. Never click a site's
  publish / save / delete control from a probe; `--verify` / `--practice` / `--dry-run` paths only. Before any edit of a
  live listing compare live vs posted; never undo her change.

## Flow (now)
```
share ─► iCloud Posh/inbox/<id>/ (+ _done)  and/or  thrift-api upload (POST /share/<id>/photo … /end)
  ─► the Mac takes the share once it is whole (or a deadline) ─► the share ledger (one row per share id)
  ─► prep (HEIC→JPEG, EXIF rotate, burst dedupe) ─► photo memory (a repeat share is skipped)
  ─► segment (Opus vision on 768 px previews; ≤ 40 photos one call, bigger in overlapping parts) ─► accepted at once
  ─► items ─► extract Facts (evidence per field) ─► labels read closely (premium) ─► second look for a size
  ─► price (brand tiers × one premium factor) ─► copy ─► verify + lint (fixed in code, never holds) ─► gate
  ─► the card: [sizes] [8 prices ⭐] [‹ lower · higher ›] [Later] [Change] [Wrong photos]  (ONE open message a lane)
  ─► her price (+ size / brand when needed) ─► ready ─► listing rows queued on all three sites
  ─► three site workers post at once (Poshmark: Playwright; Depop / Vinted: the extension) ─► read back, diff, ONE click
  ─► confirmed by address ─► the group's ✅ card (edited as sites land) ─► read back again (photos, price) every 6 h
  ─► sold anywhere: thrift-api (emails, the sites' orders, the listings' own state) ─► take-downs elsewhere ─► ship-by
```
Item statuses: `new` → (`awaiting_condition`, legacy) → `awaiting_price` | `needs_info` → `ready` → `posting` →
`posted` | `drafted` | `failed`; `needs_owner` (legacy park, `thrift requeue`), `dropped` (a twin / re-share), `canary`
(the form check's item). Nothing publishes without her price. The item is `posted` once Poshmark is. Each marketplace is
a row of `listings` (item_id, marketplace, status, url, listing_id, price, fields_json, posted_at, error, attempts,
photos_live, price_live, updated_at): queued → posting → posted | failed | skipped (later delisted | sold), plus dryrun /
drafted; unique per (item, marketplace). (The old `posts` table was copied in once, kv `listings_migrated`.)

## Verified on the live sites (the essentials; the full log with dates: docs/history.md)
**Poshmark** (`post/poshmark.py:SEL`; UNVERIFIED: `promote_toggle`, `draft_saved`, `captcha`, `availability_label`):
- Create `https://poshmark.com/create-listing`: `input#img-file-input`; `data-vv-name` title / description /
  style-tag-input / listingPrice / originalPrice / sku (SKU behind `a.listing-editor-toggle-link` "show details";
  originalPrice hidden `.listing-editor__original-price--hidden`; an empty one reads back "0" — "", "0", "0.00" equal).
- Category: dropdown `[data-test=dropdown].dropdown`; departments `a.dropdown__link[data-et-name=women|men|kids|home|
  pets|electronics]` ("All Categories" / `all` on top); categories `li.dropdown__menu__item`; subcategories
  `a.dropdown__link[data-et-name=sub_category]` incl. "None". Every department's list in `data/poshmark_taxonomy.yaml`
  (all verified), the raw catalog with each category's size menu per tab in `data/poshmark_catalog.json`.
- WO54 §1.1: the size field's own short forms are read by MEANING (`poshmark.size_meaning` / `size_same` in
  `SizeShown`): "12 Months" = "12M", "0-3 Months" = "0-3M", "Newborn" = "NB", "One Size" = "OS"; label words may be
  left out ("Waist 32" = "32") but never contradicted (Toddler Girl ≠ Toddler Boy); never "9-12M" for "12 Months".
  WO55: a kids' shoe size with its mark finds the tab's bare number (`fallback.kids_bare`: "11.5 (kids)" / "11.5C" →
  "11.5", "4Y" → "4", only where it can't be another size).
- Size `[data-test=size]`, tabs `a.navigation--horizontal__link`, tiles `button[id="size-<label>"]`, Done
  `button[data-et-name=apply]`; kids shoe labels "7.5 (Toddler Girl)" (`KIDS_SIZE_OPTIONS`). A menu may close itself on
  a pick (no Done) — either path accepted, recorded as `size_menu`. Bags preselect "One Size" (field "OS"): a click on a
  selected tile toggles it OFF — a size already shown is kept ("preselected"); `SizeShown` maps "One Size" ↔ "OS".
- Condition `[data-et-name=listing_condition][data-et-prop-content=nwt|uln|ug|uf]` (`CONDITION_TO_POSH`; `uf` never
  selected). Brand is Optional. Colour tiles. 130 curated style tags (never used: `ALLOW_STYLE_TAGS = False`).
- Cover dialog after upload: `div.image-edit-modal > [data-test=modal-container]`, thumbs `.image-edit-modal__thumb`,
  first preselected `svg.icon-green-checkmark`, croppie 3:4 frame, Cancel `[data-et-name=cancel]` / Apply
  `[data-et-name=apply]` — default crop, Apply; with ONE photo no title and no thumbs (same dialog). Any other dialog
  fails the item.
- Price dialog: inputs, Smart Sell `[data-test=toggle-input]` (asserted off), Done. Leaving: Cancel
  `a[data-et-name=discard]` → "Save Draft" dialog → Discard Changes `[data-et-name=discard]` (Save Draft
  `[data-et-name=save_draft]`); Drafts panel `[data-et-name=draftsSection]` with its count.
- Next → the "Share Listing" panel (`share_panel`, same URL): "‹ Back" (`review_back`, text), title, Promote My Closet
  (Off), Pinterest / Facebook connect (never), **List This Item** `button[data-et-name=list]` (`list_item`). Read once
  Promote is drawn (≤ 8 s, `PANEL_WAIT_MS`).
- After List: `/closet/<user>?created_listing_id=<24 hex>`; closet tiles `a.tile__covershot[data-et-name=listing]
  [data-et-prop-listing_id=<id>]` (title = img alt), `href="/listing/<slug>-<id>"`; slug = the title's letters / digits /
  spaces, words joined by "-"; the id's first 8 hex = creation time. Listing page `__INITIAL_STATE__.$_listing_details.
  listingDetails`; the public JSON (`crosslist.poshmark_listing`): department / category_v2 / category_features display,
  size, brand, photos, price, `inventory.status` (sold_out, not_for_sale).
- Edit page `/edit-listing/<id>`: Availability `[data-et-name=listingEditorAvailabilitySection] [data-test=dropdown]`,
  options `a.dropdown__link[data-et-on-name=availability][data-et-name=available|not_for_sale]`; category field in
  `.listing-editor__category-container` (`category_open_edit`); photo input `input.listing-editor__file-list`
  (`edit_photo_input`); Update `button[data-et-name=update]` only slides up the Share Listing panel — **the edit is saved
  by its List This Item** (`SEL["edit_save"]`: `data-et-prop-flow_type="edit"` + the listing id). Delete
  `a[data-et-name=delete]` never touched. Edit Covershot → Replace Photo `input[name=replaceImage]` + croppie. A brand
  change asks "You are allowed to edit the brand on your listing 3 times…" [No] [Yes] (at the click, a keystroke or the
  pick) — provoked before the search and answered after the pick (`_brand_warning`).
- Orders: `/order/sales` → `/seller-hub/sales`, JSON `GET /vm-rest/users/<seller>/orders/sales?request=…` →
  `data.sales_summary[]` (display_status; a fresh sale is "Sold"); order page `__INITIAL_STATE__.$_order_details.order`.
  Never open `/news/order` (it marks notifications seen). Pagination `button[data-et-name="pagination_next"]`.
**Depop** (`ext/selectors.json`; verified: photos, combos, price, shipping, submit, after_publish, view_listing, sku,
edit_photos, edit_save, delete_*, listing_*; UNVERIFIED: captcha, mark_sold (Depop has none), confirm, shop_links):
- Photos `[data-testid=upload-input__input]`; comboboxes by id, Downshift menus `<id>-menu`; the category menu lists a
  name once per department under its heading; multi-select chips `button[aria-label='Remove <value>']`; price
  `priceAmount__input`; SKU `SKU__input` (our item id); "Shipping" section (`national-shipping-section`) naming "Depop
  Shipping"; `shippingMethods-input` = package size (Extra small < Small < Medium < Large < Extra large); Boost ("Pay an
  extra 12% fee") off; one Post button. Depop fills colours, brand and package itself once the photos are up.
- Brand required (empty → "This field is required"); no brand → **Other**. The brand search matches text as written.
- Post → `/products/create/success/?productId=<n>`, **View listing** `a[href*='/products/'][href$='/manage/']` →
  `/products/<slug>/manage/` (shows "SKU: <item id>"); the listing is `/products/<slug>/`; JSON-LD
  `script[data-testid=meta__jsonLd]` (description, `offers.price`, images, SoldOut); `[data-testid=
  productPrimaryAttributes]`; Depop's h1 is made up — never compared. Shop link `a[aria-label$="'s shop"]`
  (`marketplaces.depop.shop`). Depop scrambles one character of the username in product addresses: match on the title's
  words after it (`ext_driver.slug_words`), confirm by the SKU. Edit page `/products/edit/<slug>/` (Save changes;
  can come up with fields blank — refilled from `fields_json`). Delete: `button[aria-label='Delete listing']` → "Delete
  listing" window → Delete listing once → the address gone = done. Receipts: `webapi.depop.com/presentation/api/v1/
  receipts/?role=seller` with the page's Bearer token, without cookies.
**Vinted** (verified: photos, title, description, price, category, brand, empty_brand, size, condition, color,
material, package, submit, after_publish, listed_later, never_click, hide, wardrobe, shop_links, edit_photos,
edit_save; UNVERIFIED: block, captcha, verify, skirt_length, promo_close, listing_title / price / photos, confirm):
- Photos `[data-testid=add-photos-input]`, grid `[data-testid=media-upload-grid]`; its error dialog
  `item-upload-critical-error-dialog`. `title--input`, `description--input`, `price-input--input`. Since late Oct every
  detail is a `<button>` opening a panel (category tree `div#catalog-<id>`, branch `[role=button]`, leaf `[role=radio]`;
  brand search; size `[role=checkbox][aria-label]`; condition / colour / material rows); "Suggested" first. Package
  `div#package-size-<n>` radios, a server "Recommended" one applied after the price. Upload
  `[data-testid=upload-form-save-button]`.
- Brand required ("Fill in brand to continue"); the menu ends "No brand indicated › List without brand"
  (`div#empty-brand[role=radio]`); a 'Use "<brand>" as brand' row on the create form. A brand change on an existing
  listing to many brands is refused ("Create new listing to change to luxury brand").
- Upload → `/member/<id>?promo_shown=true` with "Item listed": press **Later** `[data-testid=list-promotion-cancel-cta]`
  only (never List another / Bump). Confirm by `/api/v2/wardrobe/<member id>/items?order=newest_first` (exact title).
  Listing `/items/<id>`; Hide `button[data-testid=item-hide-button]` (never Delete `item-delete-button`; "Unhide" =
  hidden); edit `/items/<id>/edit`. Orders: `/api/v2/my_orders?type=sold&status=all&page=1&per_page=20`. Pages may
  pass through `/session-refresh?ref_url=…`. Refused titles: "Title contains too many capital letters" (limit between
  29% and 38%).
**Blocks** seen: Depop's login page and 403 "Forbidden - Depop" in a driven browser; Vinted logged-out
`[data-testid=header--login-button]` — why Depop / Vinted go through the extension. Chromium: `captureVisibleTab` needs
`<all_urls>` or `activeTab`.

## Shares and intake
- **The Shortcut's new order (WO56 §6, `docs/SHORTCUT_UPLOAD.md`):** the upload first; the iCloud folder only when the
  phone has no internet. **Whole** = every photo its count names (`uploads.whole`): taken at once, never waits for or
  mentions an iCloud copy. **Held** (`uploads.short` → `uploads.hold`): a share that ended short or never ended, with
  no iCloud copy, is never taken short — its photos wait in `work/_uploads/held/<id>`; 15 min after its last photo
  ONE group line "📸 The photos shared at 7:42 PM didn't finish uploading — please share them again." (ledger
  `held`); a newer share carrying any of its photos replaces it (ledger `replaced`; held photos are never in the
  photo memory, so the re-share is no repeat); 7 days → its folder goes. The 30-min partial rule is for iCloud copies
  only.
- **Two copies, one share id** (the Shortcut's `yyyy-MM-dd_HHmmss_<random>`): the iCloud folder and the direct upload.
  **The upload** (`api/thrift_api/uploads.py`, `thrift_agent/uploads.py`): `POST /share/<id>/photo?n=&of=&name=` (raw or
  one multipart file; HEIC + EXIF kept; images only, ≤ 30 MB, ≤ 200 a share — `uploads.MAX_PHOTOS`; same bytes stored
  once) then `POST /share/<id>/end?count=N`, behind a key for those two functions only (`provision.py uploads`); blobs
  in container `shares`. The Mac polls `GET /shares/pending` every 30 s while awake (`uploads.fetch_new`), downloads
  (SHA-256 checked) into `paths.work/_uploads/<id>` (the Mac's own disk, `pipeline.uploads_dir`), writes `_done.txt`,
  `_source.txt` ("azure"), `_test` for `?test=1`, tells the API (`POST /shares/<id>/fetched` → its blobs deleted).
  Uploads are taken BEFORE the iCloud inbox is listed and whether or not it can be (`cli._scan_inbox`), archived to
  `_uploads/archive/`. Whichever copy arrives first is the share; the other is the same share (`pipeline._taken_as`,
  `register`) and is deleted from the inbox at once (`cleanup.delete_copy`, event `share_copy_deleted`; iCloud holding
  it → the next look). An iCloud copy not whole gives way to a whole upload (`uploads.icloud_behind`).
- **When a share is taken** (`pipeline.share_state` / `take_share`, clock = the folder's arrival on the Mac): whole =
  the `_done` mark ≥ `inbox.min_wait_seconds` (90) old, nothing new for `inbox.settle_seconds` (60), the count `done N`
  covered by photos PLUS companions (`MEDIA_EXT`: Live Photo .mov, video, raw). Cloud-only files are pulled
  (`pipeline.pull_from_icloud`: `brctl download`, every third ask a first-byte read in a subprocess; back-off per folder,
  kv `icloud_pull`). **30 min** (`PARTIAL_AFTER`) short, with ≥ 1 camera photo → taken with those; the card says "📷 1 of
  20 photos — the rest join when they arrive" (`items.photos_expected`, `approve.photo_line`; every card "📷 N photos").
  No camera photo for 30 min → ONE group line "📭 Photos from your 7:31 PM share are still on the iPhone — unlock it on
  Wi-Fi and they'll come through. No need to share again." (`say_waiting`, kv `share_waiting_said`); 3 h → one private
  line; the folder waits 24 h (`EMPTY_ARCHIVE_AFTER`) then is archived with a `_thrift_note.txt`. Folders are judged ONE
  AT A TIME (`_folder_unreadable`, `cli._take_one`): one stuck folder never blocks another.
- **iCloud trouble** (`alerts.py`): a read refused / interrupted (EPERM / EACCES / EINTR) → retried quietly, after
  10 min ONE group line "⚠️ Can't read the iCloud inbox — on the Mac, open System Settings → Privacy & Security → Files &
  Folders → python3.14 and turn iCloud Drive on; I continue by myself.", "✓ inbox readable again" privately. Busy
  (EDEADLK 11, EAGAIN, ETIMEDOUT: `alerts.icloud_busy`) is no error: taken next tick, private line only past 10 min
  (`alerts.busy`). Other worker errors: the private chat once (ids ignored), every one in the event log.
- **A vanished share folder** → the batch `failed` "the share folder vanished…", one private line (`vanished_share`).
- **The share ledger** (`shares.py`, table `shares`, synced): source (icloud | azure | both), expected, received, times
  of received → complete → grouped → carded; outcomes `carded`, `repeat`, `empty`, `same_share`, `joined`, `failed`
  (reason). `shares.refresh_open` (each minute) keeps `items`, `cards`, `state` (work | her | send | orphan | done),
  `waiting`, `moved_at`. thrift-api's watchdog (`watch.shares_watch`, 5 min): `her` is never stuck — after 2 h without
  her answer (08:00–22:00) ONE "⏳ Waiting for her price on "…" — 8 more cards behind it."; work / send / orphan unmoved
  15 min (30 past 20 photos) → ONE "⏳ Share 12:39 PM (70 photos) stuck since 12:48 PM: waiting for …"; an upload not
  fetched 10 min after its last photo while the Mac beats → "⏳ Share … is in Azure but the Mac hasn't taken it"
  (`watch.uploads_watch`). `thrift shares [--day | --all]`; the dashboard's Shares panel.
- **Test shares** (`?test=1`, `_test`): batch and items carry `test`; cards go to the PRIVATE chat marked 🧪
  (`approve.card_bot`) in their own lane; the poster, `feed` and `crosslist.pending` never take a test item.
- **The photo memory** (`photomem.py`, table `photos`, 30 days): every photo remembered with its item at the split, known
  again by SHA-256, its capture (EXIF DateTimeOriginal + SubsecTimeOriginal + Model + pixel size) or, without EXIF, a
  near-identical picture (phash ≤ 2 and average colours within 10). Checked in `process_batch` before grouping: all known
  → repeat share (batch `failed` "repeat share: …", archived, ONE private "🧹 Repeat share skipped (7:42 PM): same
  photos as <title>." — `photomem.say_skipped`); some known → dropped (the new ones are that item's late photos when all
  known ones are one item's, else a share of their own). Outbox guard `photomem.card_blocked`: no card for photos that
  had one in 30 days or for a retired item. `thrift photos --forget <item>`.
- **Late photos join their item** (`late_photos`, every 2 min; also photos landing in a SPLIT share's archive folder — a
  batch of its own in place, `_in_place_batch`, src_dir `<archive dir>#hhmmss`, `late_of`; `batch_dir` strips the
  suffix): `process_batch` first asks the model to attach them to the share's items (`reference_items`, `seg.attach`,
  one vision call, `ATTACH_MIN` 0.7). A photo of an item is ADDED (`add_photos_to_item`): not processed yet → taken then;
  processed, not on a site → cover, order, card recomputed (`recover_item`); on a site → cover and copy kept, the photos
  appended and ONE "photos" edit per live site. The rest go on as a share; nothing left → `joined`. A small follow-up
  share (≤ 3 photos within 15 min) is compared with the previous share's items the same way. Private lines "📸 N photos
  of the 12:11 PM share came late — taken now: they join their item or become one" / "📸 2 late photos joined <title>:
  its listings get them". `thrift late --take <batch> | --add-to <item> --file … | --dry-run`.
- **Grouping** (`ingest/segment`; Opus, 768 px previews, 384 px fallback above `max_request_mb` or on a 413): visual
  identity first; one roll is one item unless the photos clearly show different things (never split for background,
  light, angle, hanger / flat lay / worn); a photo in doubt joins the most likely item. Pauses relative to the roll (>
  max(30 s, 4 × the median gap), screenshots out of the timing) as "— pause 2 min —" markers. Code checks partition,
  full shot, sizes, confidence, and for an unsure item non-contiguous photos, a pause AND a colour change inside it.
  **Parts** (`segment_roll`, `plan_parts`, `stitch`): ≤ `segmentation.part_photos` (40) is one call; bigger rolls in
  windows of ≤ 40 overlapping `part_overlap` (8), each photo trusted to the part it lies nearer the middle of; a seam
  disagreement is a doubt in `batches.reasons`, never a question. `merge_singletons` checks a one-photo group against its
  nearest 12 groups. **Accepted at once** (`segmentation.auto_confirm: true`): the contact sheet saved
  (`work/<batch>/contact_sheet.png`), the doubts in `thrift status`; only a non-partition sends the sheet. Photos left out
  (a screenshot matching no item) → ONE private line (`pipeline.say_left_out`). `auto_confirm: false` = the old flow
  (sheet + reply `ok | 12>2 | …`; `segmentation.always_confirm`).
- **[Wrong photos]** on a card (`pipeline.start_regroup`): batch → `regroup`, `regroup_sheet.png` the open message
  (outbox kind `regroup`); reply `12>2 | split 7 | merge 2 3 | drop 7 | ok` (or `thrift confirm <batch> "<fix>"`) →
  `pipeline.regroup`: unchanged items keep everything, changed ones rebuilt like `redo`; refused for an item on a site.
- **Retail screenshots**: detected by no camera EXIF + phone aspect ratio, assigned by content, always last, never the
  cover; Original Price = retail price ("Original retail $128.").

## Processing an item
- `extract` → Facts with evidence per field and `photo_roles`; `brain/labels.py` (Opus, one call when there are label /
  tag photos, at `images.label_long_edge` 2048) → `facts.premium`; `second_look_size` (`cover.read_size_label`) reads a
  printed size on label / tag / box photos before any buttons; with nothing printed the model may give `size_guess` (the
  ⭐ only, never listed by itself). Then price, copy, verify (LLM strips unsupported claims; `copy.changed_fields`
  compares word by word, removals are no rewrite), `copy.condition_rule`, lint (`verify.lint`), gate.
- **The gate never holds** (`runner.publishable`, `parallel.feed`): an approved `ready` item publishes whatever the gate
  said. `gate.min_confidence` 0.70: below it brand → the brand rule; size / condition → notes in `gate.info`.
  Optional-fact questions from the model are dropped. Lint findings are fixed in code (`copy.ensure_title_size`,
  `copy.fix_title`, `titles.fix`).
- **Corrections** (no model call unless said): `size …` (`approve.size_reply` → `pipeline.set_size`, `items.owner_size`),
  `brand X` (`pipeline.set_brand`), `no brand` (`set_no_brand`, `items.owner_brand = "none"`), `condition nwt|like
  new|good` (`pipeline.correct_condition`), `category …` / a button (`items.owner_category`; another path reprocesses),
  `men` / `women` (`approve._set_department` → `pipeline.set_category`), `boys` / `girls` (`items.owner_kids_gender`),
  `cover N` (her Nth photo from 1, `items.owner_cover`, card edited "✓ Cover: photo 3"), `NWT`, `same item` /
  `different item`. `thrift edit --title/--brand/--sizes` keeps her words (`owner_title`, `owner_brand`, `owner_sizes`).
  Never for an item on a site, except brand / price / department / photos, which go through live edits.

### Condition
- Shoes: the model reads `unworn` + confidence + photos and `box_photo`; a pair in doubt is listed **Like New** (NWT
  only with a tag photo), shown on the card. NWT / NWOT say "New with tags." / "New without tags." ("New in box." for NWT
  shoes with `box_photo`). `pipeline.settle_nwt` keeps NWT strict; NWT without a tag photo lists as Like New, the card
  says so, a reply "NWT" makes her note the evidence. Fair never selected (`CONDITION_TO_POSH`, `POSH_FAIR` recorded
  only): "looked well-worn — listed as Good; check before approving".
- **Wording** (`copy.condition_rule` after the verifier: drops a sentence with wear words, puts the line in;
  `verify.lint` checks wear words, used-item claims — never "no flaws", "like new", "excellent" for a used item —, the
  line and the flaw photos). The copy writer never sees the flaws or the condition evidence. Every flaw's photo is in
  the listing (`fit_photos`); a flaw without a photo is a card warning. Frayed / raw / distressed hems allowed only on
  cutoffs, jean shorts and jeans (`copy.cutoff`, `copy.STYLE_HEMS`, masked for the condition rule, lint and the poster).
- Legacy: `awaiting_condition` + [NWT] [Like New] [Good] (outbox kind `condition`, `thrift condition`) still answered
  for items asked before WO37.

### Covers and photo order
- The cover is the **front of the whole item**: shoes the pair from the side / ¾ / front, whole; clothing and sets a
  front flat lay or hanger shot, whole (worn only when there is none); bags and the rest a whole outside view. Never a
  label, close-up, look into shoes, sole, flaw, box, hand-held back shot, screenshot or a cut-off photo. A front showing a
  small flaw stays the cover.
- **The front check** (`brain/cover.py`, `models.cover` Opus): labels EVERY photo — view (front, three_quarter, side,
  back, top, worn, label, detail, inside, sole, flaw, box), `whole`, `cut_off`, `fill`, `held` — and names a pick;
  code decides (`pipeline.cover_rule`): several fit → the model's pick, then the printed side (clothing), then the most
  of the frame; none fits → the photo showing the most of the item + ONE private line (`NO_FULL_COVER`). Cues: print /
  logo / buttons / lower neckline = front; fly zip = front, a zip down the middle of a skirt or dress = back; pants' back
  pockets = back; plain vs printed → printed. The stored check is reused while it covers every photo (`items.views`).
- Order (`labelled_order`): cover, other fronts, back, side, top, worn, details, labels, flaws, screenshots last.
  `facts.cover_photo` + `cover_upright`; `photo_order` is used by the card, the renders and every poster.
- **Upright** (`cover.upright_check`, `models.upright` Sonnet): the cover shown four ways, the model picks the upright
  one; turned by that quarter turn. EXIF orientation applied at prep (`prep.normalize`).
- **3:4 portrait** (`images.cover_size: [1200, 1600]`), never cropped: the whole photo over a blurred enlarged copy of
  itself (`prep.portrait_cover`); an exact 3:4 photo only resized. The re-share hash stays the square cover
  (`prep.cover_hash`).
- Facts from before WO20 (no roles) keep the model's pick.

### Titles and copy
- **Order** (`copy.title_order` inside `premium.title_with_feature`): brand → the ONE premium feature → item → colour →
  US size ("J. Crew 100% Merino Wide Leg Sweater Pants Blue size M"). "New" only for NWOT, right after the brand; NWT ends
  ", New with Tags" when it fits (`titles.fix` keeps one New; a brand's own New — `NEW_BRANDS` — aside). Her own title is
  kept as is.
- **Shop-window titles** (`thrift_agent/titles.py`, rules per site in `data/site_rules.json`): `check` names what breaks
  a rule (an all-caps word of 4+ letters not an acronym; capitals over 35% — `caps_share_max`; codes: letters+digits
  mixed, "RN 54867", SKU, our ids, 5+ digit numbers — sizes like 34DD / W32 / 2-Piece fine; repeated words; "size
  size"; condition words but New; HTML artefacts; emoji / odd characters; a slash; over length); `fix` repairs it (brand
  as a shop sign — `data/brand_display.json`: BCBGMAXAZRIA → BCBG Max Azria, FARM Rio → Farm Rio, MNG (Mango) → Mango;
  aims at ≤ 29% capitals, `caps_share_safe`). `titles.shop_window` ends every title; still failing → ONE model rewrite
  (`pipeline.shop_window_title`, `titles.rewrite`); checked again at mapping (`catalogs.vinted.vinted_title`,
  `map_depop`, `crosslist.strict_title`) and before Poshmark's form (`runner.shop_window_render`); a title that still
  breaks never reaches a site (the item waits, one private line). Descriptions get the shop-sign brand
  (`titles.display_in_text`).
- **Length** (`premium.fit_title`, 80): the words before the size go first, then backwards — never the brand, the
  feature, the US size, the set phrase, the item's last two type words or the colours. A collab that is the brand itself
  is never repeated (`premium.collab`).
- **Sizes in titles: US only.** Adults "size 7.5"; kids shoes "Toddler size 7.5" (up to 12C; 0–7C too, form Baby tab) /
  "Little Kid size 13" (12.5–13.5C, 1–3Y) / "Big Kid size 4" (3.5–7Y) (`brain/sizes.py`). The EU size and the full label
  ("EU 24 / US Toddler 7.5") go in the description. A size no photo shows never goes in a title.
- **Sets**: "2-Piece Set" / "3-Piece Set" (`facts.set_pieces`, `copy.ensure_set_title`; never bikinis, shoes,
  jewelry); different piece sizes → listed under the BIGGER, the description says each ("Cardigan: size S. Pants: size
  XS."; `pipeline.settle_set_size`, `copy.ensure_piece_sizes`). Dual labels ("S/M", "M-L", "0/2", "One Size (S–L)") →
  the bigger (`pipeline.settle_dual_size`), "Size S/M (fits S–M)." / "Label size: S/M." in the description
  (`copy.ensure_label_size`), the card "Size M (label S/M)".
- **Descriptions**: premium feature lines (below), the condition line, "Original retail $128."; line breaks real
  (`copy.unescape_breaks`). Depop: the title as the first line, a blank line, the description, ≤ 5 hashtags, ≤ 1000
  characters. Vinted: title + description.
- **Materials** only with label evidence; texture words fine; "denim" on visibly denim items (`verify.denim_item`).
- **No style tags** anywhere: the prompt asks none, `copy.clean` drops any, Poshmark never types one, Depop `style: []`
  and its own Style chip removed.
- **The trademark guard** (`data/trademarks.json`, `brands.guard`; `pipeline.guard_marks`, `runner._guard_render`,
  `crosslist.strict_title`): a licensed character or much-counterfeited mark (Disney, Nike, Barbie, Gucci…) that isn't
  the item's own brand, label text, style name or a collab its label shows never reaches a title, description or
  hashtag.
- `copy.short_name` (brand + item, ≤ 6 words, no size / colour) names the item in the group's lines.

### Premium details (`premium.py`, `config/premium.yaml`, `private/premium.yaml` replaces it)
- From the labels only: composition ([{fiber, pct, part}], in English), made_in (only Italy, France, Japan, USA, UK incl.
  England / Scotland / Wales, Portugal, Spain — any other country never mentioned), a premium line (J.Crew Collection,
  Purple Label, BR Heritage, Levi's Made & Crafted, Zara Studio, We The Free, Maeve, Pilcro…), vintage only with a
  concrete cue (+ era), collab / limited / sample ("J. Crew Sample (1st Proto Fit)."), technical (Gore-Tex, waterproof,
  down, Primaloft, UPF), construction (fully lined, silk lining, hand-knit, Goodyear welt…), a hang-tag retail price.
  `premium.merge` keeps only values a photo shows; the main fabric becomes `facts.material`.
- Title feature (strongest one): premium fiber ≥ 90% ("100% Silk" / "Cashmere") > premium line > "Vintage 90s" > "Made
  in Italy" > collab > blend 50–89% ("Silk Blend"). Description lines (`premium.ensure_feature_lines`): "Material: 100%
  silk.", "Lining: …", "Made in Italy.", "Vintage 90s.", "H&M x Erdem.", "Fully lined." — code states them, the copy
  writer never sees `premium`.
- Price × ONE multiplier, the largest (fiber 1.3, line 1.2, vintage 1.2, blend 1.1); never on her price.
- `thrift recover <item> --relabel` reads the labels again.

### Sizes
- `sizes.poshmark_size` puts the size on ONE value of one tab of Poshmark's menu for the category (`Render.size_tab` /
  `size_value`). One-size categories (bags, jewelry, most accessories, Home): "One Size", no size needed. Kids shoes:
  0–7C Baby tab bare number, else Girls / Boys "7.5 (Toddler Girl)"; kids clothing Girls / Boys by `kids_gender`
  (unisex / unread: Girls), Baby tab for baby sizes. Adults: Maternity / Petite ("8P") / Juniors first when the label
  says so, then Standard, Plus, Petite, Juniors, Big & Tall; spelled as the menu ("3XL" → "XXXL", "32x30" → "Waist 32",
  "15.5" → "Neck 15.5", "34DD" → "34E (DD)", "8 1/2" → "8.5").
- Kids clothing labels in cm / age (`sizes.kids_clothing_size`): 92 → 2T, 98 → 3T, 104 → 4T, 110 → 5T, 116 → 6, 122 → 7,
  128 → 8, 134/140 → 10, 146/152 → 12, 158/164 → 14, 170/176 → 16; 50 → Newborn, 56 → 0-3 Months, 62 → 3 Months … 86 →
  18 Months; ages alike (9 → 10, 11 → 12). Settled (0.95), "Label size: 4 ans / 104 cm." A bare "4" → the labels read again.
- A label's waist 23–34 on jeans / pants / shorts is the size (`settle_waist`). Loafers, moccasins, flats → Flats &
  Loafers (Men: Loafers & Slip-Ons), never Heels (`settle_shoe_kind`).
- An unsure size or one off the menu is listed as best read, a card note (`SIZE_NOTE`, `pipeline.size_menu_note`).
- **No size on the photos → size buttons** (`sizebuttons.py`, `approve.card_buttons`): "📏 No size on the photos — tap it
  below (⭐ = my guess)"; Poshmark's menu for the category kept to what Depop and Vinted take (`crosslist_ok`), eight in
  two rows (shoes in halves; letters one row), ⭐ on the guess (her shop's history → the category's usual → the middle),
  "‹ smaller · bigger ›" (`sizes:<item>:±1`, kv `size_page`), EU brands (`EU_BRANDS`) "8 · EU 38.5", "S/M → M". A tap
  (`size:<item>:<value>`, `approve._tap_size`) sets that item's size, edits the card ("📏 Size 8 ✓"), changeable until it
  starts posting. A price first → "✓ $25 — tap a size to list it"; the item is queued when it has both
  (`approve.needs_size`). A size typed without a reply → "Which item? Reply 'size …' to its card". An item waiting for a
  size gets ONE group "Waiting for a size: <title> — tap one on its card (or reply 'size …' …)" (`approve.ask_sizes`,
  outbox kind `size`); its reply sets the size and puts every site back in line. Every site skips an item with no size
  with ONE private line (`crosslist.say_no_size`).
- **On any site's menu** (`post/fallback.menu_size`, one order): the exact value → the bigger of a dual label
  (`dual_parts`, `bigger_of`) → the nearest in the same system, one step at most (`nearest_size`: 8.5 → 9, XL → L; never
  Petite or a kids label for a plain size) → One Size only for a one-size item. Depop / Vinted (`catalogs/sizes.py`
  `depop_size` / `vinted_size`): also the size without its fit mark ("27P" → 27"), then the menu's neutral choice (One
  size / Other) — each fallback in the note; kids letters on Depop's ages (XS → 5 … XL → 16 years). Only "no size at
  all" skips.

### Categories and departments
- `taxonomy.fit` maps the model's department / category / subcategory onto Poshmark's names
  (`data/poshmark_taxonomy.yaml`, all shown in the extraction prompt): a department is never a category (the
  subcategory, else the item type's noun); a subcategory given as the category goes under its category when exactly one
  has it (whole name before a part of a two-part name; "Maxi", "Mini", "Skinny" are ambiguous); one part of a name matches
  ("Jumpsuits" → "Jumpsuits & Rompers").
- The model gives `category_confidence` (+ `category_alternatives`) and `department_confidence` (+
  `department_alternative`). Unsure → its own pick, shown on the card ("Category: Men › Shoes › Sneakers — my best
  reading; reply 'men' or 'women' …"); `taxonomy.category_options` offers real paths; legacy "Which category?" buttons
  (`cat:<item>:<n>`) still work. The card names the department unless plainly Women. Her category
  (`items.owner_category`) is applied after every extraction and passed to the model as a seller note.
- **Kids**: `facts.kids_gender` + `kids_gender_confidence`; ≥ 0.70 used silently, else Girls (unisex too), shown on the
  card, corrected by 'boys' / 'girls' (`items.owner_kids_gender`; legacy [Girls] [Boys] message and `thrift kids`). The
  copy never states it. Kids shoes on the Girls / Boys tab: Toddler 7.5–12, Little 12.5–13.5 and 1–3, Big 3.5–7 ("7.5
  (Toddler Girl)", `KIDS_SIZE_OPTIONS`); 0–7C on the Baby tab.
- **A set goes under its bottom** (`pipeline.settle_set`, confidence 1.0): pants → Pants & Jumpsuits (flared → Boot Cut &
  Flare, wide → Wide Leg…), skirt → Skirts › Skirt Sets, shorts → Shorts; Kids → Matching Sets; never Dresses; pajama,
  swim, lingerie sets left to the model; her category wins. Catsuit / jumpsuit / romper → Jumpsuits & Rompers.
- **WO54 §1.4 — never refused for its family:** a pet item (`families.is_pet_item`: "dog coat", "cat bed",
  "harness for dogs"; never "dog print dress") is family `pet`, at home under Accessories / Other (no site has a pet
  category in our catalogs); a Depop / Vinted pick of another family becomes the closest category of the item's own
  family in that department (`categories.in_family` → `families.closest`, Pick source "family"); Poshmark's
  (`runner.refamily`) the closest Poshmark category of its family — the facts and EVERY render follow, one check
  line; none of that family → posted where it is, ONE check line (`crosslist.family_check`; `hold_family` unused).
- **Families** (`thrift_agent/families.py`): a site category of another family than the item's type words (a dress under
  Sweaters) → that site `skipped` "… isn't the item's family" (both safety nets keep it), ONE private line
  (`crosslist.family_problem` / `hold_family`).

### Brands
- **The rule** (WO54 §2; `facts.brand_seen`: read | unreadable | logo | none): no brand to be seen → no brand
  (`NO_BRAND_NOTE`); read ≥ 0.70 → used; there but not read surely → decided, never asked: the reading when it is a
  big brand, else no brand (Poshmark empty, Depop "Other", Vinted "List without brand"; `pipeline.settle_brand`).
  The card states it (⚠️ "Brand: none — …" / "Brand: Nike — read with doubt …"); `gate.brand_unsure` gives
  [No brand] when a reading is kept and makes a plain reply her brand. `approve.needs_brand` never holds;
  `ask_brands` asks nothing and returns parked rows to the line; a reply to an old 🏷️ message still sets her brand.
- **The shop's name** (`pipeline.shop_brand`): the copy step returns `brand` (the name shops list, "Alo Yoga" for "alo")
  and `brand_label`; taken only when it is the same brand, else the alias file's, else the label's. Title,
  descriptions, short name use the shop's name (`named_as` / `shop_named`); `Render.brand` / `Render.brand_label`.
- **On every site** (`brands.candidates`, `brands.choose` / `pick_any`, the extension's `T.brandChoose`): our names in
  order — the brand, the label text, the alias file's names, the two parts of "MNG (Mango)", then a sub-line's PARENT
  (`data/brand_parents.json`: "Zara TRF Collection" → "Zara TRF" → "Zara"; `keep`: "Lauren Ralph Lauren" stays) — each
  typed as written and in its other spellings, picked when the site lists it EXACTLY (case, spaces, punctuation, accents
  aside, & = and; a company suffix is one word: "Co" = "Company", `brands.SUFFIX`); only after all, the ONE option that
  is a name of ours followed by more words ("Alo" → "ALO Yoga"; whole words; two → no guess). Then the fallback: Depop
  "Other", Vinted "List without brand", Poshmark empty, with the site's top suggestions in the note — **never for a big
  brand** (`data/big_brands.json`, ~330 brands: the read-back refuses it, `BrandOf(names, [])`; Poshmark's `_brand`
  raises — a site failure, the 🚨 path). A site's own fill-in is kept only when it is one of our names. Vinted's 'Use
  "<brand>" as brand' row lists our exact brand when its list lacks it (never a big brand).
- **The alias file**: `data/brand_aliases.json` (curated) under `data/brand_aliases.learned.json` on the Mac
  (`paths.brand_aliases`; old `.yaml` still read). Grows by a site's other spelling of the same name (`Aliases.learn`)
  and her `brand <name>` reply (`learn(…, owner=True)`).
- **Live brand fixes**: `thrift brand-fix <item> [--marketplace m] [--brand B] [--dry-run]` (edit kind "brand",
  `edits.queue_brand`): Poshmark `PoshmarkPoster.set_brand` (box cleared, names tried, read back, the 3-edits warning
  answered, Update → List This Item for this edit, the public JSON's brand); Depop / Vinted the extension's "fields" job
  with `fields.brand` (`_fields_match`). Vinted refuses many brand changes on an existing listing.

### Prices
- **Always a price** (`price()`): brand tier (`private/brand_tiers.yaml`) → the department's category default (then
  Poshmark's other names for it, then `other`) → the flat legacy table → `pricing.default_target` (source `default`); ×
  one premium factor.
- **The card's buttons** (`approve.price_options`, `item_buttons`): eight in two rows of four, $10 below the suggestion,
  ⭐ the suggestion, six $10 steps up (no button under $5: then seven up). The pager row "‹ lower · higher ›"
  (`prices:<item>:-1|1`, `PRICE_SHIFT` $80, kv `price_page`, `can_lower`) edits the card, never prices. A number typed
  alone (`28`, `$28`) prices the open card (under the floor only as a reply); a reply with a number sets the price.
  Source `owner` → `ready`.
- **Her price change on a live item**: a reply "price 80" (or a number) to its card or ✅ card →
  `pricedrop.owner_price`: one `price_changes` row per live site (stage `owner`, no floor), `items.owner_price` and
  `listings.price` follow, ONE private line when the sites show it. A site price that differs from ours is her hand
  change: event `price_differs`, never undone.
- **Automatic drops** (`pricedrop.py`, `price_drop.enabled`, `price_drop.floor`): 30 days after the item is live on ALL
  its sites (`items.all_live_at`; a site never live → 7 days after the first) and unsold → 10% off on every site; 30
  days later 10% again; never a third (`items.drop_stage`, `last_drop_at`, `next_drop_at`; a relist restarts the clock).
  Not for an item with a listing sold / delisted, a take-down, or her 'no drop' (`items.no_drop`,
  `approve._reply_no_drop`). New price per site from that site's live price (`read_price`), whole dollars down from 90%
  ($25 → $22, $95 → $85); under the floor or `SITE_MIN` (Poshmark $3, Depop / Vinted $1) → skipped. Done by each site
  worker on an idle turn inside the hours (`parallel.price_drop`, `run_one`): Poshmark `set_price` (the edit page's price
  dialog, Smart Sell off, Update → List This Item for this edit, the public page until it shows it); Depop / Vinted the
  extension's "price" job (`T.priceSet`; the page's price must be the one read; Depop's blank fields refilled from
  `fields_json`, `depop.js refill`), Save once. Right before Save, GET /tasks once more (a sale stops it). `saving` →
  `done` | `unconfirmed` (read again 5 min later; never saved twice blind) | `failed` (retried 6 h later, 3 → one private
  line). ONE group line a day: "🔻 Prices down 10% today: … $25 → $22 · … (last drop)." (kv `price_drop_said`).
  Records: `price_changes` (stage auto_1 | auto_2 | owner | dry; dry never synced). `thrift price-drop --list | --skip
  <item> | --dry-run <item> | --now <item>`.

## Telegram
- **The bot** (`telegram.py`, `approve.py`, `notify.py`): long polling (`getUpdates`) on the worker's own Telegram thread
  (its own DB connection; one consumer; offset in kv). Only `TELEGRAM_CHAT_ID` (the group) and senders in
  `TELEGRAM_ALLOWED_USER_IDS` (`.env`) count. The private (ops) chat: `TELEGRAM_OPS_CHAT_ID`, else
  `telegram.ops_chat_id` (`private/settings.yaml`) — never the group's id; works once she has sent /start. Replies and
  buttons work with privacy mode ON; a bare typed number needs it OFF (or the bot an admin). On prod `run` / `poster`
  refuse to start when `telegram.enabled` and an env var is missing. Settings: `telegram.enabled`, `poll_timeout` (15;
  capped at 25, `cli.POLL_TIMEOUT_MAX`), `ops_chat_id`. Azure only sends (`sendMessage`; never getUpdates / setWebhook).
- **One queue, two lanes** (`approve.queue`, `next_up`, `open_message(db, lane=)`, `pump`, `lane_of`): everything that
  waits for an answer is one queue read from the DB — the oldest batch first, its items in photo order (questions, then
  the card). At most ONE open message per lane: "real" (the group) and "test" (🧪 cards in the private chat). A send lock
  in kv. The worker processes in queue order and runs ahead; an answer that lands while its item is processed wins (the
  item processed again). [Later] puts the item behind everything (`items.deferred_at`). A queue that can't send for 10
  min says so once privately (`approve.note_send_failure` / `note_send_ok`). `daily.work` / `thrift status` count the
  real lane.
- **Never twice** (`approve.once`): a placeholder outbox row (negative message id) BEFORE the send; a send that may have
  arrived (`telegram.maybe_delivered`: ReadTimeout, WriteTimeout, RemoteProtocolError…) counts as sent; only a connection
  that never opened is retried. A changed card is EDITED in place (`approve.refresh_card`, `Bot.edit_card`:
  editMessageMedia / Caption / Text); `resend_pending` sends nothing new. Uploads get 90 s (`UPLOAD_TIMEOUT`).
- **The other keyboard layout** (WO54 §1.3, `thrift_agent/layout.py`, `approve._relaid` on every reply and ops-chat
  text): a reply in Cyrillic that reads, key for key on QWERTY, as one of our commands or a brand we know is
  understood as that ("тщ икфтв" = "no brand", "ышяу Ь" = "size M", "зщые тщц" = "post now"; event
  `reply_relaid`), the reverse too. A brand reply that is no brand we know and no plausible shop name
  (`layout.plausible_brand`) is 'no brand' with ONE check line — never typed into a form (`approve._plausible`,
  `pipeline.set_brand`); the extension: two brands are the same only with a non-empty key (`T.sameBrand`).
  `Bot.answer_callback` never raises: a tap answered late ("query is too old") loses only its toast.
- **Answers**: after an answer the card's buttons become ONE inert button with it (`approve._ack`, callback `noop`: "✓ $35
  — queued", "✓ Girls", "✓ cover: photo 2"); every typed reply gets an answer at once (`approve._confirm`, `next_step`,
  `when_posting`: "✓ Size S · $100 — it goes up on Poshmark, Depop and Vinted"); a handler that said nothing →
  "✓ Got it — …" (`_fallback_answer`, `_Answered`). A plain reply answers THAT message's question (`approve.brand_reply`,
  `_reply_size`, `_reply_brand`); a reply to a dropped twin is refused (`DROPPED_SAID`). Replies in the private chat are
  routed by `approve._route_ops` (unconfirmed 'posted <url>' / 'retry', refused 'retry', "go titles", "doctor", an
  unmatched sale's words). `approve.CORRECT_HINT` on every card.
- **The card** (`approve.card`): cover, title, the size as Poshmark's menu shows it, the condition as Poshmark's label,
  "📷 N photos", "Category: …" when not plainly Women, warnings only where a look is needed (cover, well-worn, a flaw
  without photo, NWT without tag photo, a possible re-share "looks like item X", the trademark / family notes); buttons:
  [size rows] [eight prices] [‹ lower · higher ›] [Later] [Change] [Wrong photos].
- **Twins** (`pipeline.settle_twin` from `set_price`; `parallel.feed` backstop; `thrift twins [--apply]`): a card flagged
  "looks like item X" that gets its price — X not live anywhere: X `dropped` (rows skipped "shared again as …", card
  closed); X live on a site: this item's row there `skipped: already live as X`. One private ♻️ line (event
  `twin_settled`). 'same item' drops it unless the earlier twin is already retired (`TWIN_KEPT`).
- **The group hears only**: the cards and allowed questions (🏷️ brand, "Waiting for a size", legacy condition / kids /
  category); ONE ✅ card per item (`crosslist.card_text`, HTML, previews off): "✅ <short name> · $X", then "🟣 Poshmark",
  "🔴 Depop", "🟢 Vinted" each on its own line (the name is the link); a site not yet up "⏳ Vinted — later" or "⏳ Vinted
  — photos failed, next try 4:45 PM" (`step_name`, `crosslist.next_try`); a failed-for-good / skipped site left out; a
  site landing later EDITS it (outbox kind `posted`); the 💤 line on the newest card (kv `posted_done_carrier`):
  "💤 All done — nothing is waiting." / "💤 Done for now. Vinted catches up by itself." / "💤 Done for now — waiting for a
  size: …" (`crosslist.done_line`, `daily.done_state`, `daily.owner_waits`, `BRAND_WAIT`). WO56: a sign-in / CAPTCHA / check page is a
  human step (private, above) — the group hears only the block line and "📸 … please share them again". Before WO56:
  plain action lines when the
  pause begins and again at most every 12 h while items wait (WO53, `alarm.account_line`; live 11-07: said once, then 31
  hours of silence): "Depop needs you to log in on the Mac." / "… shows a CAPTCHA — solve it in its Chrome window on the
  Mac." / "… turned the Mac away for now — I'll try again by myself later." / "… asks for a check — open it on the
  Mac." (`crosslist.ALERTS`);
  "⚠️ Can't read the iCloud inbox …"; 🔋 battery low; 📭 photos still on the iPhone; "⏸ Not published automatically:
  <title> — <site> publishes by itself only once its autopublish is on." (WO53: no "⏭ … Reply 'retry'" line any more —
  a listing the poster couldn't make is ONE private 🚨, `runner.ask_skipped`); 🔻 the day's drops; 💰 sales, ⚠️ sold twice,
  📦 overdue, "Couldn't take … down on <Site> — please mark it sold there.", "⚠️ <title> sold on <Site> but is still
  listed on <Sites> …"; replies to her own messages. Never ids, errno or tracebacks. No "safe to close" / "close the Mac"
  wording anywhere (a grep test).
- **Everything else is private** (`notify.ops` / `notify.say`): the status message, "Back online", "Poster started
  (LIVE)", dry-run notes, CLI echoes (price / answer / confirm / condition / kids / category / redo), "Check — <title>:"
  (ONE per item, each note once, event `checks_said`; routine notes filtered by `crosslist.noteworthy`), ❌ errors, ⛔
  pauses, 🩹 learned selectors, 🔎 form changes, 🚨 alarms. The same text again: never (kv `ops_sent`), except the edited
  status message and CLI echoes. A failed private send → event `ops_chat_down`, not tried for 10 min (kv
  `ops_down_until`), **never sent to the group instead**. On dev both print (`[notify]` / `[ops]`).
- **The link** (`telegram.client()`: one httpx client per process, connections kept 15 s, TCP keepalive —
  `keepalive_options`; the one network seam `telegram.http_post`, every call through `telegram.post`): a request that
  never left (ConnectError, ConnectTimeout, PoolTimeout, a failed write) is retried 1, 2, 4 s later (`RETRY_PAUSES`); one
  that may have arrived never is. DNS failure (`dns_failed`) → api.telegram.org's last known address (`_ADDRESSES`,
  refreshed every 10 min; seeded 149.154.166.110 / 149.154.167.220) with `Host` + `sni_hostname`, for a minute
  (`DNS_DOWN_FOR`). The poll backs off 5, 10, 20, 40, 60 s (`cli._safe_poll`, `poll_backoff`); events `telegram_down`
  {kind, why} / `telegram_up` {down_s, failed_polls, kinds} (`telegram.failure_kind`: dns, offline, tls, reset, timeout,
  http). A line whose send never left is HELD (kv `telegram_held:<when>:<pid>`, ≤ 30, 48 h) and sent "(held back since
  3:12 PM: Telegram couldn't be reached)" by `notify.flush_held`; claimed before its send. A ✅ card not sent is not
  "announced" (event `card_not_sent`; the sweep sends it). A line said inside a write transaction waits for its commit
  (`db.in_transaction`, `db.after_commit`, `notify._later`; dropped on rollback); notify keeps one connection per thread
  (`notify._db`). Tailscale's MagicDNS stays first on the Mac (the pinning covers it).
- `thrift telegram setup` prints chat / user ids; `telegram test [--ops]` sends a test line.

## The daily window, power, status
- A **window** (`daily.Window.step` / `begin`) starts at worker start or a wake (`power.WakeWatch`: `kern.waketime`, or
  the wall clock outrunning the process) with the lid open (`power.lid_closed`, AppleClamshellState): the inbox looked
  at, "Back online — N new shares, M items waiting" privately when there is work. With the lid closed the worker starts
  nothing and says nothing, the poster starts no listing; the Telegram thread still applies answers.
- **The status message** (`daily.status_text`, `Window.show`; private chat + `thrift status`): ONE per window, edited
  (`Bot.edit_message`), the last window's deleted: "⏳ Working — 4 items left, about 12 min." / "⏳ 3 listings still to
  publish, next in ~4 min." / "⏸ Waiting — …" / "💤 All done — nothing is waiting."; the daily cap counts Poshmark's
  own listings. Estimates: medians of the last runs (`daily.timings`: `worked` events, `post_posted` seconds, gaps).
  Poster heartbeat kv `poster_state` (stale after 3 min).
- **Sleep**: a model call cut by sleep leaves the batch / item `new` (`cli._guard`, event `interrupted_by_sleep`); a
  listing slept through before its final click goes back to `queued` (≤ 3, `SLEPT`); after it, the closet is looked at
  (`PoshmarkPoster.find_live`); a `posting` row at poster start gets the same check (`runner.reconcile_stale`), and so
  does one left by a failed Poshmark turn (`parallel._poshmark_worker`). The first listing after a wake waits
  `parallel.AWAKE_GRACE` (45 s) (`settle_after_wake`, event `awake_grace`).
- **Battery** (`power.Awake`: `caffeinate -i -w <pid>`): held while there is processing / listings; below 15% on battery
  ONE group line "🔋 Mac battery low — plug in or I'll pause; nothing will be lost", none started until charging or ≥ 20%
  (`power.publish_paused`, kv `battery_low_since`).
- **The worker watch**: `alerts.beat` (kv `worker_inbox_beat`, `worker_progress`) in awake time (mach_absolute_time;
  progress = a finished look `scan_done`, a job worked, an upload fetched, or an idle lid-closed turn; `look_ended`);
  the Telegram thread restarts the worker (`alerts.restart_watcher`, `os._exit(3)`, ≤ one private line an hour, event
  `watcher_restarted`) after 15 min without progress with the lid open (never in its first 15 min); the poster's
  housekeeping (`alerts.WorkerCheck`) kickstarts it through launchd after 20 min.
- **The Mac offline** (thrift-api `timers.mac_watch`): no heartbeat for 30 minutes of the Mac's working time (the
  quiet hours don't count) between 08:00 and 23:00 → ONE private line per outage, "The Mac stopped answering at
  5:19 PM. Is it plugged in, with the lid open? …" — after the quiet hours "The Mac didn't come back after its quiet
  hours …"; nothing in the quiet hours or the 30 minutes after; ONE "Mac back at …" only for an outage whose line
  was said (settings `mac_down_since`).

## Human steps (WO56, `thrift_agent/humansteps.py`; the owner's setup: `docs/HUMAN_STEPS.md`)
- **What:** a sign-in (login); a one-time code or the site's own "confirm it's you" (verify); a real CAPTCHA; a "Just a
  moment… verifying you are human" page (class `challenge`, checked before the CAPTCHA) that didn't pass within 2 min
  (`T.challengeWait`) on two loads 15 min apart — the first is a quiet pause retried by the self-test
  (`quiet_challenge`); one that passes says nothing. A block page ("turned the Mac away") is none: its group line and
  the 20-minute 🚨.
- **Held:** `crosslist.block` → an account pause (no ⛔ line) + `humansteps.begin`: ONE private message with the
  page's screenshot — "🔐 Vinted wants a person — a CAPTCHA (screenshot), since 7:12 PM. 1 minute: connect to the Mac's
  screen (Screen Sharing, <`human_steps.vnc_address`>), reply show, solve it in the window that comes up, and that's
  it. Depop and Poshmark keep posting meanwhile." (a sign-in / code: "… type the password or code yourself …"). The
  extension moves the page into a window of its own titled "<Site> — human step" (`T.stepTitle`), never tidied,
  read every 10 s (`step_page` → `humansteps.seen`); no idle quit while a step is pending (`Bridge.quit_reason`).
- **Her words** (the private chat, also on the Russian layout): "show [site]" → that window to the front — the only
  window ever focused — for 5 min, then minimized (its own window, never reused for a job); "later" → no reminder
  for 12 h; "done" → looked at again now. Poshmark's step: its own browser (`parallel._poshmark_step`).
- **Clear:** a form / listing page at once, any other on the second reading in a row → ONE "✅ <Site> back — posting
  resumed …", `health.resume`, the step's tab closed (window and all). **Reminders:** every 12 h and once at 2:05 PM
  after the quiet hours, never more. **Record:** event `human_step` {site, kind, page, at, cleared_at, seconds,
  person_needed}, kv `human_step_log`, `thrift doctor` "🔐 Human step: …". The Thrift Chrome's cookies survive its
  restarts (`chrome_cross.sh` moves only `Default/Sessions` aside).

## The quiet hours (WO54 §3)
- **The rule** (the owner, 2026-11-09: the Mac is her working laptop): 8:30 AM – 2:00 PM America/New_York every day
  (`quiet:` in settings, `thrift_agent/quiet.py`, DST-safe) the agent does NOTHING on the Mac — no processing, model
  calls, posting, checks, cleanup, Telegram sends or browsers. Azure keeps taking her shares and the sales emails.
  Every other hour everything runs, posting included.
- **From 8:20** (`quiet.closing`) nothing new starts: the worker (`cli._quiet_turn`) only syncs (its Telegram thread
  applies her replies until 8:30); the poster (`parallel.quiet_hold`) starts no listing, form check, test, verifying
  look, edit, price drop, order sweep, photo check, reconciler or request; take-downs only with
  `quiet.allow_takedowns`. Work in hand finishes.
- **8:30 — the calendar job** `com.thriftagent.quiet` (`deploy/quiet.sh tick`, every 10 minutes and at load; a run
  missed in sleep comes once at wake; decided by `quietctl decide` in New York time): the poster stopped (the
  listing in hand finishes, ≤ 300 s), one last heartbeat, the worker, the Thrift Chrome and the one-off job
  stopped, nothing of ours left (our venv's processes and both Chromes by their exact `--user-data-dir`; never her
  Chrome or Safari), marker `~/thrift/var/quiet.stopped`; inside the window each tick peeks for her "post now" of
  today (the bot's offset never moved, nothing sent). **2:00 PM:** the first tick with the marker starts the worker
  and the poster. Log `~/thrift/logs/quiet.log`; the 8:31 proof: `bash deploy/quiet.sh check`.
- **A process inside the window** (the job missed) starts nothing and asks for its own stop (`quietctl.ask_stop`).
- **Active time** (`quiet.active_seconds` / `advance`): the verifying schedule, rule 3's proof and its 2-hour 🚨, the
  20-minute pause alarm, job health. **Telegram:** nothing sent or re-sent in the window (held, sent at 2 PM
  "(held back since …: the quiet hours)"); 10 PM – 7 AM silent (`disable_notification`, `telegram.silent_hours`);
  a tap's "query is too old" is no error. **Her words** in the private chat (also on the Russian layout): "post
  now" lifts them for that day, "quiet now" brings them back — one heartbeat each.
- **thrift-api** (`api/thrift_api/quiet.py`, from the heartbeat's `quiet`; defaults when none): every net that judges
  the Mac rests while it is quiet or starting back (30 min, `GRACE`) and counts its working time — `mac_watch`,
  `watch.not_live` (priced at 8:00 → 2 h old at 3:30 PM), `shares_watch`, `uploads_watch`, `unposted` (any hour but
  the quiet hours), `core.late_takedowns`; the 9 PM line never calls a quiet Mac offline; night sends silent
  22:00–07:00 (`deadlines.night`); shares, sales emails and 💰 lines go on. `/health` shows `quiet`.
- Seen in `thrift status` / `doctor` ("Quiet hours 8:30 AM – 2:00 PM: next stop …"), `services.sh status`. Tests:
  the quiet hours and the silent nights are off unless `@pytest.mark.quiet_on`; `no_real_quiet_tick`.

## Posting
### The poster loop
- `thrift poster` publishes only with `poster.dry_run: false` AND `poster.autopublish_confirmed: true` AND
  `marketplaces.<m>.autopublish: true`; otherwise dry runs (`poster.notify_dry_runs` to say them) or held
  (`runner.held`: "⏸ Not published automatically"). It takes `ready` items carrying her approved price
  (`runner.approved`; a brand question open → false), approval order, inside `schedule.hours`, under `daily_cap` (25 a
  site) and `per_hour_max` (60); gaps 0 (`schedule.gap_seconds`, `marketplaces.<m>.gap_seconds`,
  `crosslist.gap_seconds` all [0, 0]; `scheduler.next_gap`). PAUSE stops all; `HOLD_UNSHIPPED` blocks publish.
- **Three site workers** (`post/parallel.py`, `poster.parallel: true`): Poshmark in its own thread (own loop, Playwright,
  SQLite connection); Depop and Vinted as asyncio workers beside the bridge. Each turn, in order: take-downs (any hour),
  listing edits (any hour), the form check when due, the next approved item with a `queued` row (in the hours, under the
  cap), price drops, the order sweep. The cross rows are queued at the price's approval (`parallel.feed`). `--once` is
  sequential. A site stopped / paused stops only that site.
- **The ✅ card at the first live site** (WO54 §1.5; `crosslist.announce`, event `posted_announced`; live 2026-11-09
  it waited for every site to settle — a paused Poshmark never did — and 19 items live on Depop and Vinted got no
  card): sent the first time any site of the item is posted, a "⏳ <Site> — …" line for each site still to come,
  the same message EDITED as each other site lands, fails for good (left out) or changes state (`_update_card`).
  Never twice (`_card`: the outbox row taken in ONE statement before the send; a send that never left goes with the
  next sweep, one that may have arrived is never re-sent). The housekeeping's sweep (`waiting_lines(db, s)`, ≤ 5 a
  turn) sends cards still due and edits open cards whose text changed. `thrift cards --backfill [--dry-run]`.
- **The housekeeping** (every few minutes in the poster): announcements, `crosslist.retry_failed` (an item failure tried
  again 30 min later), the photo check, the sale net, the price-drop plan, `pricedrop.announce`, the reconciler, the
  worker check, the memory watch, `jobs` health, alarms, `sales.repair_gone` at start.
- **The reconciler** (WO53 §7, `thrift_agent/reconcile.py`, every 30 min — `EVERY_S` — so also before the 9 PM line;
  only with the real Poshmark poster): Poshmark's closet newest first (`postcheck.posh_closet`, plain HTTP) against our
  records. A live listing of ours that isn't recorded — made after OUR click (a row posting / verifying / unconfirmed;
  a merely queued row never adopts one), our title key, our price, our cover (phash) — is recorded
  (`runner.confirm_found`: posted, the ✅ card, the other sites queued) with ONE private "🔗 Poshmark: <title> was live
  but not recorded — recorded now: <url>"; two live listings for one item (or two new ones) → ONE private "⚠️ Two live
  Poshmark listings for <title>: <url> and <url> — nothing deleted." (kv `reconcile_said`, event `reconcile`). A
  recorded listing that is gone is the photo check's sale net; Depop's / Vinted's unrecorded publishes the verifying
  re-check's.

### Failures, pauses, self-healing (WO51, WO52)
- **Rule 4 — one item never stops its site** (WO54; live 2026-11-09 the snowsuit's "12M" and the sneakers' brand
  paused Poshmark and Vinted for every item): an error about one item's VALUE ("after picking '…'", "the size field
  shows", "Mismatch: …", "brand shown", "isn't the item's family" — `health.VALUE_RE`) is the item's; an item that
  fails twice on a site (any kind but infra) is SET ASIDE there (`health.aside`: row 'failed' "set aside: …", ONE
  private ⏸ line), tried again after 3 h / 12 h / 24 h (`ASIDE_HOLD`, `crosslist.retry_failed` — Poshmark's rows
  too) or at once after a poster start (`release_asides`: a fix), the third time also ONE 🚨, never skipped for good;
  a site pauses only when DIFFERENT items fail — the same failure on two items in a row (`non_item`), or 3 different
  items in a row with nothing through (`health.brake`) — or the canary fails. A paused site's self-test runs on the
  CANARY (`parallel._poshmark_test` / `_cross_test`), never on the item that failed.
- **Every failure has a kind** (`thrift_agent/health.py`, `kind_of`): item (the site refused this listing, a field it
  can't satisfy) | site (an element missing, the form changed, an unknown page; the extension's `SiteChanged`) | infra
  (browser / context / page closed, extension away, network, our code throwing — `T.errInfo` names it). Only item
  failures use attempts (`MAX_ATTEMPTS` 3, then `skipped` + ONE private 🚨) and count toward "3 in a row" — an
  unconfirmed publish ("verifying") never does (WO53 rule 4). A site /
  infra failure (`health.non_item`): row back to `queued`, attempt given back, the item behind others 10 min; the same
  failure on the next item pauses the site at once; one item meeting the same failure 3 times is the item's.
- **Pauses** (`health.pause`, kv `site_health`; `crosslist.blocked` / `block` read and write it): ONE private line per
  site per problem, true and whole (WO53, `health.pause_text`, `health.cut` at a word) — "⛔ Depop paused at 10:58 PM:
  its sell form changed (shipping section not found). Live today: 3. Waiting: 6. Next try 11:28 PM." (never "nothing
  was published"); after 30 min (doubling to 4 h) the next item is filled as a quiet dry run (`parallel._poshmark_test`
  / `_cross_test`; a test that meets the account page again is a failed test — its wait doubles, live 11-08 it ran every
  ~5 s): passing → "✅ Depop back to normal — posting resumed, 3 items going up." (`health.going_text`: true about the
  hours). `thrift pause <site>` / `thrift resume <site> [--dry]` — a resume whose test fails becomes THAT problem's
  pause, said once (live 11-09: Depop read "PAUSED (owner)" while its form lacked the shipping section), so the 🚨
  sees it. A login / CAPTCHA / block / check page
  (`crosslist.account_blocked`) stops the site with the group's plain line; take-downs run on a site paused for
  failures, not on one with such a page.
- **Browsers**: Poshmark's context (`parallel.Browser`, `Browser.ensure`, probed every 5 min `PROBE_EVERY`; its "close"
  event, `browser_closed`) reopened at once, then after 30 s / 2 min / 10 min, the login checked, the same item next with
  no attempt used; three failed reopenings pause Poshmark. Closed by hand → "Poshmark's Chrome was closed on the Mac —
  reopened. Please don't quit the Chrome windows on the Mac." (`runner.closed_line`, ≤ once / 6 h); the same for the
  Thrift Chrome. Only the poster's own profile is ever opened or closed.
- **Healing a moved field** (`thrift_agent/heal.py`): learned selector (sent with every job as `learned`) → the recorded
  one → the field's label / accessible name (`T.byLabel`: aria-label, aria-labelledby, label[for], a label in its field
  box, placeholder; Poshmark `get_by_label` / `get_by_placeholder`, `HEAL_LABELS`) → the model (`models.heal` Sonnet,
  `heal.pick`: the page's control snapshot — `T.snapshot` / `heal.SNAPSHOT_JS`, ids `data-thrift-ai`, never an input's
  value, ≤ 150 — + a screenshot, ONE tool call, an enum of ids or "none"). The extension asks through the bridge
  (`{"event": "ask", ask_id, kind, field, want, candidates}` → `answer_ask` → `{"type": "answer", job_id, ask_id,
  pick}`); Poshmark in process (`PoshmarkPoster._find`). `kind: "option"`: an option in the site's new wording only when
  it IS our value reworded (`seen.reworded`, `ext_driver.reworded`). Forbidden controls (publish, post, upload, list,
  save, delete, submit, discard, boost, bump, hide, confirm; submit buttons; recorded submit / save / delete /
  never_click; Poshmark `NEVER`) are out of the snapshot and refused (`T.forbidden`, `heal.allowed`, `T.aiFind`). The
  read-back decides as before. Learned (`{"event": "learned", field, selector, how}` → `heal.Learned`,
  `learned_selectors.json`) → ONE private "🩹 Vinted's brand field moved — learned the new one.".
- **The form check** (`thrift_agent/formcheck.py`, `formcheck.enabled`, prod only; `formcheck.anywhere` for tests): at
  02:00 local (WO54: `formcheck.slots: ["02:00"]`; a slot slept through let go after 4 h) and never beside real work —
  a site with real items queued or posting is proven by them (`form_check_skipped`), a real listing since the check
  fell due is its pass, and a check without a verdict beside real work is no failure (live 8:40: two false 🚨);
  never in the quiet hours, after every poster start and
  after a browser reopen (kv `formcheck_request`); never with PAUSE, the lid closed or a login / CAPTCHA page. **The
  canary**: a copy of an old sold item (else the oldest live one) — status `canary`, `test` 1, batch `b_canary`, photos
  under `<var>/canary/<id>/`, no listing row, never carded, never synced. The check = the poster's own dry run of it,
  every field filled and read back, the form left (Poshmark Discard + Drafts count, `Outcome.draft_left`). The
  structure (`post/base.STRUCTURE_JS`, `capture_structure`; `T.structure()`), kept as the last good one in
  `<var>/formcheck/<site>.json`; a difference → ONE "🔎 Vinted's form changed: Brand is now a button; …" (kv
  `formcheck_said`). A failure → `degraded` (kv `formcheck_state`); second in a row, or at once with items waiting →
  `formcheck.failed()` → the 🚨 alarm; passing again "✅ Vinted's form check passed again.". Rides the heartbeat
  (`formcheck.beat`) into the 9 PM line ("Sites: Poshmark ✓ Depop ✓ Vinted ✓ — form checks 8:30, 2:30 passed.",
  `watch.sites_line`). `thrift canary [--check <site|all> | --make [<item>]]`.
- **The alarm** (`thrift_agent/alarm.py`, kv `alarms`): a site paused 20 min with items waiting (not her PAUSE) → ONE
  private "🚨 <Site> posting is blocked since … — N items waiting. Step …: … Evidence: …" with a ready-to-paste Claude
  Code request; again only after 4 h, ≤ twice a day; cleared by "✅ <Site> back to normal — posting resumed, N items
  going up." WO53: a login / CAPTCHA / check page too, inside the posting hours ("🚨 Depop has shown a login page since …
  Only you can act on it, on the Mac.", why "account") + the group's plain line again (≤ every 12 h); a listing still
  unconfirmed 2 h after its click, a skipped listing, a refused form: one 🚨 each (keys "<mp>:verify|skipped|refused:
  <item>"). `alarm.raise_for`, `alarm.form_check_failed`. thrift-api's net
  (`watch.not_live`): an item priced ≥ 2 h ago (within 3 days; `items.priced_at`) not live on all three → ONE 🚨 line
  naming each missing site's reason — a queued row on a site the Mac's heartbeat shows paused names the pause ("Depop:
  waiting — Depop paused: … since 1:04 AM", `watch.pause_words`; live 11-09 it said "waiting in line").
- **Jobs** (`thrift_agent/jobs.py`, kv `job_health`; `jobs.ok` / errors): order sweeps, photo checks, take-downs, price
  drops, sync, heartbeat, cleanup, form check — no success for 12 h (24 for daily ones) → ONE private line with the last
  error, "✅ … works again" after.
- **Memory** (`thrift_agent/memory.py`): `top` grouped by browser (ours vs Safari / her Chrome); pressure warning+ with
  the Thrift Chrome over 3 GB and sites idle → restarted through its LaunchAgent (≤ every 12 h, one line); critical for
  30 min → one line naming the biggest users. The extension tidies its Chrome every 30 min with no job (`background.js
  tidyTabs`): pages on the three sites unseen 6 h (`lastAccessed`) and a site's second idle page closed — never a job's
  tab or a window's active tab.

### Poshmark (`post/poshmark.py`, Playwright, its own Chrome profile)
- Fill order: photos → cover dialog (Apply; checkmark waited for) → title → description → category → subcategory → size
  → condition → brand → colours → price dialog (Smart Sell off) → SKU; `read_back` of every field; the diff; `fix` once
  (`PoshmarkPoster.fix`: a brand not ours cleared, the rest filled again) then read back again. Still different →
  nothing published, the row `failed` "Mismatch: …" (MAX_ATTEMPTS windows, then skipped with the ⏭ line,
  `crosslist.step_text`, `ext_driver.describe_diff`).
- Never asks (`post/fallback`): brand by the brand rules (none → empty, a big brand never); size `menu_size`; category
  and subcategory exact, else the ONE name that is ours followed by more words or of whose " & " parts ours is one
  (`fallback.matching`; a category nothing matches skips the item, a subcategory → None); colours not offered left out.
  Every guess in the item's "Check —" line. A required field nothing fits → `skipped` ("skipped: …"), nothing saved;
  `thrift requeue <item>` once fixed. An item with no size is skipped before Next. A size pick that didn't register is
  picked again four ways (`SIZE_REPICKS`: plain click, keyboard, the tile's container, the button's own click).
- Publish: Next → the Share Listing panel (our title; Promote off — one checkbox unchecked, or the panel reads "Promote
  My Closet Off"; unreadable → stop before List, `…-panel.png`) → List This Item once (Poshmark's JSON answers to the
  click recorded, `after-list.json` "answers") → **WO53 §1, Poshmark's own data** (`thrift_agent/postcheck.py`, plain
  HTTP of public pages, no browser): the page itself when it is a listing; else the id Poshmark named
  (`?created_listing_id=` in its landing address — the landing is still the closet, as since 2026-10-03) → the public
  `/listing/<id>` (it redirects to `/listing/<slug>-<id>`; its page state: title, price) → posted; else the closet
  newest first (`?sort_by=added_desc`, its page state `$_closet.listingsPostData.data`): the named id, else our title
  (`postcheck.title_key`: letters and digits, as the slug) at our price created after the click (the id's own UTC
  timestamp); two → the cover photo (phash) decides; never a guess. Looked at 10 s, 30 s, 1 min after the click
  (`postcheck.INLINE_S`, `<shot>-verify.json`). Live 2026-11-08: the closet's default order ("Just Shared",
  `best_match`) didn't show three new listings for 100 s while `/listing/<id>` showed each at once (each went public
  ~40 s after the click).
- **Not confirmed at once → "submitted — verifying"** (`failed` + "unconfirmed publish: …" internally; kv
  `unconfirmed_state`: clicked_at, due, reads, created, round): looked at again at 2, 3, 5, 10 min after the click,
  then every 10 minutes (`postcheck.SCHEDULE_S`, `runner.recheck_unconfirmed` from each site's worker turn — Poshmark
  too, any hour, even paused); the next item never waits. Found → posted (posted_at = the click), Depop / Vinted
  queued, the ✅ card. Proven absent (two absent reads 10+ min apart, the last 30+ min after the click) → back in line
  once, one last look right before List (`runner.last_look`); unreadable never counts; still unknown 2 h after the
  click, or a second post unconfirmed too → ONE 🚨 (`runner.unconfirmed_alarm`), never a third post; a row older than 6 h
  with no state (from before WO53) → the 🚨, never re-posted. Never asked, never a failure (it never counts toward
  a pause). Her own 'posted <url>' / 'retry' / `thrift mark-posted` / `thrift retry` still work — nothing prompts for
  them. A new landing kind is said once privately ("🩹 Poshmark now lands on … — handled.", `postcheck.landed`).
- Dry-run stages (`poster.dry_run_stage`, `--stage`): `form` (fill, read back, evidence, Cancel → Discard Changes) |
  `review` (also Next, the panel recorded to `failed/shots/<item>-review.json`, ‹ Back → Cancel → Discard). Each dry run
  compares the Drafts count before / after (more → a warning). A cover dialog still open when leaving is cancelled. A
  dry run traces the size menu (`_trace`: `<sku>-poshmark-trace-<nn>-<label>.png/.json`).
- **Supervised `--publish-first <item>`** (Mac, poster stopped): one `ready` item at her price, `poster.dry_run` ignored
  once; refuses a listing whose text predates the condition rule (`thrift answer <item> "recheck"`); fills, reads back,
  checks the panel, "Type LIST to publish", List once, records `<shot>-after-list.png/.html/.json`, finds the address,
  checks the page, `posted`. Anything unrecognised after the click → never clicked again, `failed` with evidence.
- `--allow-dev-browser`: a browser on the dev machine for selector work, dry-run only, never logs in.
- Edits on the edit page all save by the panel's List This Item for THIS edit (`_save_edit`, `SEL["edit_save"]`), once,
  then the public page decides: `set_availability` (take-down / relist), `set_price`, `set_category` (+ size), `set_photos`
  (crop dialogs applied, tiles counted), `set_text`, `set_brand`, `set_cover` (Replace Photo with the right photo's 3:4
  cover, the old cover onto its tile; **never saves** — reports "nothing saved"; a dry run is what it does).
- Evidence: `failed/shots/` (`.png` / `.html` / `.json` per run). `playwright codegen --channel chrome
  https://poshmark.com/create-listing` on the Mac to record selectors.

### Cross-listing on Depop and Vinted
- **Catalogs** (`thrift_agent/catalogs/`): `data/depop_catalog.json` (322 categories, attributes, values, 13 US size
  sets, product type → size set, combobox ids), `data/vinted_catalog.json` (573 leaves with required fields and option
  libraries, 29 colours, package sizes), `data/poshmark_catalog.json`; validated when loaded (`catalogs.check()`; a bad
  file turns that site off, one private line). `thrift catalogs check | refresh [--depop] [--vinted]` (Playwright-driven
  sites only; the extension sites keep their saved catalogs; rewritten only when the new one validates; raw answers in
  `data/catalog_raw/`).
- **Mapping** (`catalogs/depop.py:map_depop`, `catalogs/vinted.py:map_vinted`; every value one of the catalog's; saved
  to `listings.fields_json` before the form opens; a mapping failure skips only that site):
  - Category: `catalogs/categories.py:TABLE` (Poshmark department / category / subcategory → Depop path + Vinted leaf;
    REFINE by the item's words; Kids rows name Girls and Boys leaves, unisex → Girls); no row → Opus picks from the
    enumerated leaves (`models.crosslist`), cached in `private/category_map_learned.json`. Depop (depop.js): our leaf
    under our heading; a name under two headings by the heading our path names; none → that heading's "Other"
    (`seen.category_fallback`, a check line).
  - Size: from Poshmark's menu value (`catalogs/sizes.py`, `undual`). Vinted women's numeric 00→XXXS, 0→XXS, 2→XS, 4/6→S,
    8/10→M, 12/14→L, 16/18→XL, 20/22→XXL; denim waist 24→00 … 32→14, 33→16, 34→18; men's waist `W<n>`, neck; shoes the
    US number; kids size_16, kids shoes size_17 (a missing half size up one). Depop: the product type's size set
    (letters, numbers, waists `NN"`, "US 8.5", "N years", "N-M months").
  - Condition (never Fair): Vinted NWT→6, NWOT→1, like new / excellent→2, good→3; Depop NWT→Brand new, NWOT / like new
    / excellent→Like new, good→Used - Good. Colours ≤ 2 by a table (Gray→Grey on Depop, Tan→Beige on Vinted,
    olive→Khaki, teal→Turquoise / Green, navy→Navy). Material only from a read label (Vinted ≤ 3, Depop ≤ 4). Vinted's
    skirts need a length (facts, else the model with the enum). Depop: Source Preloved (Vintage only with a vintage
    cue), Age Modern unless a decade, optional attributes only from facts. Package from a weight class (X_SMALL only
    where the Vinted leaf offers it).
  - Copy: Depop the title + blank line + description, ≤ 5 hashtags (brand, item, style, colour, era), ≤ 1000 chars; Vinted
    title + description. Photos: Depop ≤ 8 (cover first, every flaw photo kept), Vinted ≤ 20.
- **Optional values never made up**: an option the menu doesn't offer exactly is left empty and noted (`seen.left`; the
  driver expects it absent); required fields keep their defined fallback.
- **The read-back before publishing**: Depop's tidy step — every multi-select holds only ours (Depop's own colours
  removed first, two at most; `typed_left` empty; Style empty), category / size / condition chosen again once, then
  brand and package; the page settled (`T.quiet`) and read again (`chipsOurs`, `addBack`). Vinted's `check` step —
  size, condition, skirt length chosen again once, colours / materials set to ours (`setMulti`), the brand again, the
  package. The driver's diff (`ExtensionPoster.expected(r, seen)`): brand one of ours or the allowed fallback
  (`BrandOf`), multi-selects ours minus `left`. Still different → nothing published (the job cancelled before the click),
  `failed` "Mismatch: …".
- **Depop's package**: Depop's own choice is KEPT when at least ours by its order (`depop.js packageAtLeast` /
  `packageBelow`, `ext_driver.PackageShown`); ours goes in only when empty or Depop's is smaller (the exact size, else
  the smallest offered ≥ ours, else the largest); the tidy and `submit`'s last look set back only a size below ours
  (twice at most). The read-back takes any of the five sizes; still below at the click → posted with a `note` event in
  the "Check —" line. An empty package that can't be set is the one error.
- **Depop shipping**: the "Shipping" section naming "Depop Shipping", a "Shipping from" address checked; "Other" never.
- **Vinted's package**: its server's "Recommended" lands after the price; the panel settles (`T.quiet`), our pick watched
  and chosen again once, `check` compares it, `submit` looks once more before Upload — never published as Vinted's.
- **Photos**: Vinted's error dialog instead of thumbnails → a fresh page after `T.photosRetryPauseMs` (25 s), the form
  filled again (`job.reloads`, twice at most), then the step fails with the dialog's words and the upload calls seen
  (`T.watchRequests`); a short count → the missing ones attached once more, then the step fails (a publish ends there).
  Depop the same: re-attach once, then fail.
- **A refused form** (`T.watchAfterClick`, `T.refusal`: alerts, toasts, `aria-invalid` fields by label, the site's
  `/api/` calls with statuses): still the sell form after the click = "refused: …"; the driver takes any after-publish
  "form" page as a refusal (one look at the site's data; found → posted). A field error → `failed` "Vinted refused the
  form: …" + ONE private "⛔ … refused the form — <error>. It isn't on Vinted (its own data agrees); not posted again by
  itself until the code is fixed." (outbox kind `refused`: her own 'retry' still works, nothing asks for it) + ONE 🚨
  with the fix request (WO53); never requeued by itself (`crosslist.refused`). A silent refusal
  (`crosslist.silent_refusal`) goes to the 10-min re-check (`refused: true`): found → posted; absent → posted ONCE more by
  itself; refused again → the ⛔ line. **Fixable refusals** (`crosslist.autofix_refusal`, `data/site_rules.json`: too many
  capitals, too long, forbidden characters) → fixed (`strict_level`) and retried once, "✓ Vinted had refused … the title
  was fixed and it is live: …".
- **Unconfirmed on Depop / Vinted** (`runner.recheck_unconfirmed`, kv `unconfirmed_state`) — WO53, the same as
  Poshmark's: "verifying", nothing said; the site's own data — Vinted's newest-items JSON (exact title), Depop's shop
  (title words after the username) + the SKU on /manage/ — on postcheck's schedule. Live → posted; absent only with
  rule 3's proof → `queued` once (never a failure), looked for once more right before the second post (`run_cross`);
  a second unconfirmed → ONE 🚨, never a third post, never a question. Unreadable ≠ absent; unknown 2 h after the
  click → the 🚨.
- **The size skip**: a size no menu value fits → ONE private line naming the size and menu (`crosslist.say_size_skip`),
  the other sites go on.
- `thrift crosslist <item> | --dry-run <item> | --backfill [--dry-run] | --check-login | --hand-listed [--apply 1,2]`.
  Backfill: every item still for sale on Poshmark, at its price on Poshmark that day (a differing price becomes
  `owner_price`, ONE private list). Hand-listed (`handlisted.py`): her shops read, our items scored (brand, words, size,
  price), the numbered list privately; `--apply` records the confirmed ones `posted` with her address.

### The extension driver (Depop and Vinted from the Thrift Chrome)
Both sites turn away any browser driven over DevTools; the extension runs in the seller's own normally opened Chrome.
`marketplaces.<m>.driver: extension` (default; `playwright` = WO30's posters `post/depop.py` / `post/vinted.py` on
`post/cross.py`, kept; Depop's `api` = a stub, `post/depop_api.py`). Poshmark stays on Playwright.
- **The Thrift Chrome**: see "Her laptop comes first (WO54 §4)" — `deploy/chrome_cross.sh`, `--no-startup-window`,
  opened on demand, quit after 10 idle minutes, never a restored session or a start URL; the two backgrounding
  switches keep a window behind others at full speed (WO28). Owner steps once: load `~/thrift-agent/ext` unpacked (Developer mode), paste `cat
  ~/thrift/var/ext_token` in its options, log in to vinted.com and depop.com there, `enabled: true` per site.
- **The extension** (`ext/`, MV3, plain JS, no build, no third-party code): permissions storage, alarms, tabs, scripting;
  hosts the two sites, the bridge, `<all_urls>` (for `captureVisibleTab`; content scripts on the two sites only).
  `background.js`: WebSocket to `ws://127.0.0.1:8765/ext` (token first; retries 1, 2, 5 s then every 5 s; 30 s after a
  refused token; ping every 20 s), `chrome.alarms` poll of `GET /jobs/next` while the socket is down (30 s; 5 s
  unpacked; `poll_minutes` overrides). One job per site, each site in its own window (`siteWindow`, idle page
  `ext/idle.html?site=…`, never focused; an "Orders" window for sweeps), each job in a new active tab of it, closed after
  (except a login / block / CAPTCHA page, left for her). The page script injected when silent 2 s; each message answered
  within 3 s or asked again (20 s in all); a page that never answers ends the job with the tab's state and a picture. A
  sell job's tab counts as loaded on the job's page or another that stays 2.5 s (passes through `/session-refresh`); a
  page that navigates during the fill gets the job again (twice at most), except a form waiting for POST. Photos fetched
  by the worker from the bridge and handed over as bytes. After the one click the landing is watched (`tabs.onUpdated`,
  ≤ 3 min). A job ends when its bridge goes away before the go-ahead (tab closed), when not filled in 4 min, a filled form
  left 15 min, 5 min after the click, or its tab closed. Watchdog: a page silent 30 s while filling is pinged (3 s).
  Reload check: the bridge's `files_hash` of `EXT_FILES` differs → `chrome.runtime.reload()` between jobs.
- **Content scripts** (`content/common.js` + `vinted.js` / `depop.js`, isolated world): text by the native value setter +
  input / change; pace `ext.pace: fast` (values in one go, waits on the page via MutationObserver — `T.until` /
  `waitFor`, settles ≤ 150 ms in MessageChannel ticks) | `human` (typed in chunks, pauses). Each step ends the job after
  20 s without progress (photos 120 s) with a `stalled-<step>` screenshot; step events carry `ms`, the read-back
  `fill_ms`. Real clicks (pointer / mouse events, then click) on real options (`T.matchOption`); Depop's menus cleared
  then typed and waited on until they offer the value, comboboxes waited for; Vinted's category walker (leaf by id, else
  path), brand search, size, condition, colours ≤ 2, materials ≤ 3, skirt length, package radio; photos as Files in one
  DataTransfer (fallback a drop); Depop's Boost unchecked; Vinted's bump / promote closed. Depop's own suggestions
  waited for (≤ 8 s) and the page settled before our picks. Pages classified block → captcha → login → verify →
  listing → form (`selectors.json` pages; jsdom: text without scripts); WO53: a page element counts only when shown
  (`T.shown`: not display:none / hidden, not 0×0 — live 2026-11-08 Google's invisible reCAPTCHA helper frame on an
  ordinary Vinted page read as "a CAPTCHA is shown" and paused Vinted). `options.html`: token + link state.
- **The bridge** (`thrift_agent/bridge.py`, in the poster process; asyncio, hand-written RFC 6455): 127.0.0.1:8765 only;
  the token (`bridge.token_file`, `~/thrift/var/ext_token`, made once by `mac_setup.sh`, chmod 600, never rotated) on
  every request; the WebSocket from `chrome-extension://` only; a Host other than 127.0.0.1 / localhost refused. One job
  per site (`Bridge._busy(site)`, `_next`); a job called off gets ONE `cancel`, the next waits ≤ 90 s (`DRAIN`); after a
  reload hello no job for 2 s (`RELOAD_HOLD`). `GET /photos/<site>/<item>/<n>.jpg` (CORS only for the two sites), `GET
  /health`. Screenshots / HTML beside the job's evidence (`failed/shots/<item>-<mp>-<time>.png`, `…-after-publish.png`,
  `…-login.png`). **The watch** (`watch_tick`, 15 s, lid-open time only): no extension 2 min → `services.sh start chrome`
  (event `thrift_chrome_started`); 5 min → restart it (`runner.restart_thrift_chrome`); 10 min → ONE private line
  (`MISSING_LINE`, kv `ext_missing_said`). kv `ext_link` {connected, at}; `crosslist.reachable`. No token / port taken →
  the extension sites off for that run (one private line), Poshmark goes on.
- **The job**: bridge → extension `{"job_id", "site", "mode": check_login | dry_run | publish | verify | find | delist |
  price | fields | photos | text | cover | orders | read_only, "fields", "copy": {"title", "description"}, "price",
  "photos", "listing_url", "shop", "pace", "learned"}`; extension → bridge `step` {name, ok, detail, ms}, `progress`
  {what}, `screenshot` {label, png_b64, html}, `note`, `ask`, `learned`, `ready` {seen} (publish: the filled form
  waiting), `result` {url, live | seen | listings}, `error` {stage, message, page: login | block | captcha | verify | form
  | unknown}; bridge → extension `submit` | `cancel` | `answer`. `GET /jobs/next`; `POST /events` answers with the job's
  commands.
- **The driver** (`post/ext_driver.py:ExtensionPoster`: `post`, `find_live`, `verify_live`, `listing_address`,
  `available`, `check_login`, `dry_run`, `publish`, `delist`, `set_price`, `set_fields`, `set_text`, `add_photos`,
  `read_photos`, `read_listing`, `read_fields`, `read_text`, `set_cover`): the job → `ready` → the read-back diffed
  with the plan (`DepopPoster` / `VintedPoster.expected`) → a dry run ends → a publish needs `PUBLISH_NEEDS` {submit}
  (supervised) / `AUTOPUBLISH_NEEDS` {submit, after_publish} verified, exactly one Post / Upload button seen, her POST
  when supervised → the row claimed (`on_go_ahead`, `db.claim_listing` → `posting`; refused → no go-ahead) → ONE
  `submit` → the address (Depop's success page → /manage/ whose SKU must be ours; else the site's data every 30 s for 5
  min, `confirm_by_address` → `find_live`; an address another item holds never taken; several → never a guess) → the
  page compared (title / price / size / photos; a difference is a note, the listing stays). `confirmed_in` seconds.
  Attempts counted at the job's start (`db.begin_attempt`). Not taken in 150 s (`PICKUP_TIMEOUT`) → error before
  anything opened; past 3 min (`JOB_TIMEOUT`) before the go-ahead → failed, nothing submitted. A job given up is
  cancelled once and awaited (`Bridge.settle`, ≤ 5 s). Progress lines ("tab opened", "photos 6/6", "category ✓",
  "filled in 21.3 s"). The loop takes extension jobs only while connected (`runner.ready_cross`).
- **Around it**: `thrift crosslist --check-login`; `--dry-run` without the poster starts its own bridge; `poster
  --publish-first <item> --marketplace vinted|depop` (poster stopped — the port) sees the extension within ~5 s (past 30 s
  `Bridge.why_missing`; gives up at 90 s), prints progress, "Type POST to publish" (a Ctrl+C before it cancels the job,
  the row stays `queued`, event `publish_cancelled`); `mark-posted` reads the page in the Thrift Chrome; `thrift login
  --site vinted|depop` says to log in there; `thrift status` "extension: connected / not connected".

## Live listing edits and read-backs
- **`listing_edits`** (`edits.py`; one row per item, site and kind — `category`, `photos`, `text`, `brand`; pending →
  saving → done | unconfirmed | failed | skipped): taken by each site worker on an idle turn in any hour
  (`parallel.listing_edit`). One Save per site; saved and not confirmed → READ an hour later (`_shows`), never saved again
  blind; a failure retried an hour later, three in all, then ONE private line. Dry runs fill and never save (a private
  line + event `listing_edit_dry`). The records follow (`edits._mirror`). Payloads carry what the site should show now
  (`expect_title` / `expect_first_line` / `expect_description`): a field she changed by hand is left as it is.
  - category (`thrift recategorize <item> "Men > Shoes > Sneakers" --dry-run | --now`; a `men` / `women` reply on a
    listed item): the record follows at once (facts, renders, `map_depop` / `map_vinted`); Poshmark `set_category`;
    Depop / Vinted "fields" job (`T.fieldsSet` → `site.setFields`) — the page scripts read the listing's OWN brand,
    condition and package before the change and set them back (Vinted empties them on a category change); edit pages
    waited for up to 30 s more.
  - photos (`edits.queue_photos`; late photos, the photo check): Poshmark `set_photos`; Depop / Vinted the "photos" job
    (`T.photosAdd`: the tiles must be the `had` recorded), Save once, `read_photos`.
  - text (`edits.queue_text`; `thrift titles --review --send` → ONE private list old → new; her reply "go titles" or
    `thrift titles --apply` → `titles.apply_review`): Poshmark `set_text`; Depop / Vinted "text" job (`T.textSet`; Depop's
    title is its description's first line; blank fields refilled first).
  - brand: see Brands.
- **Covers on live listings** (`thrift cover-fix <item> [--marketplace m] [--photo N] [--dry-run]`, `coverfix.py`; the
  poster stopped, Poshmark via `gui_once.sh`): the right photo by `--photo N` or the cover rule; Depop / Vinted the
  "cover" job (`T.coverFirst`: keyboard move on the dnd-kit grid — works on Depop, ignored by Vinted), the driver's one
  go-ahead only when the order is the old one with that photo first, Save once, the listing's first photo checked
  (`photo_key`); Poshmark replaces the cover but never saves.
- **The photo check** (`photocheck.py`, housekeeping every 5 min, 6 listings a run): a live listing 10 min after it posts,
  then by bulk reads every 6 h (`photocheck.bulk_reads`: Vinted's wardrobe JSON `vinted.js wardrobeAll`, 96 a page;
  Depop's products JSON with the page's token `depop.js shopAll`, the shop tiles as fallback; first record's keys logged
  once, `photocheck_shape`), Poshmark's public JSON every 2 h (`POSHMARK_EVERY`); a listing's own page only when the bulk
  data has no count or fewer than sent (≤ once a day per listing, 6 a run). `listings.photos_live` / `price_live`. Fewer
  photos → ONE "photos" edit with the missing ones (kv `photocheck_asked`); a different price → `price_differs`.

- **A Vinted listing posted again with its brand** (`thrift_agent/vinted_repost.py`, `thrift vinted-repost <item>
  --check | --practice | --go`, served by the running poster; the owner's decision 2026-11-01 — Vinted refuses a brand
  change on a live listing, "Create new listing to change to luxury brand"): only with no likes, no conversations (offers
  live there) and not reserved — a count Vinted didn't give is never "none". The delete dialog is recorded once by a
  practice run (Cancel), its other button pressed ONCE (`ExtensionPoster.vinted_delete`; a reload ends the job, never a
  second press); gone from Vinted's own data before the row goes back to `queued`; the new listing keeps the ORIGINAL
  `posted_at` (the price-drop clock), its fields carry `reposted_at` / `reposted_from`, the ✅ card is edited
  (`crosslist.refresh_card`), one private line. Recorded live 2026-11-01: Vinted's inbox entries carry NO item id
  (id, item_count, description, unread, updated_at, opposite_user, item_photos) — a conversation is matched by the
  listing's own photo ids / file names (validated on two sold listings: 1 and 2); the delete dialog is "Delete item? By
  deleting this item, you confirm you have not sold it on Vinted." [Cancel `item-delete-cancelation-button`] [Delete
  `item-delete-confirmation-button`]; `--probe-delete` proves the job reaches the listing with nothing pressed.
- **The extension reloads itself after a deploy** only when its loaded files' hash differs from the bridge's
  (`background.js maybeReload`); a restarted service worker hashes the files on disk while Chrome may still run the old
  background script (live 2026-11-01: a new job mode opened the sell page). `bridge.HASH_SALT` / background.js
  `HASH_SALT` (the same value) is changed to force every loaded copy to reload once.
## Sales, take-downs, thrift-api
- **thrift-api** (`api/`: Python 3.12 Azure Function, Flex Consumption, App Insights capped 0.1 GB/day, $5 budget alert
  on `thrift-rg`; database `thrift` on the owner's Postgres, login `thrift_app`; SQLite in tests).
  `deploy/azure/provision.py up | deploy | uploads | mode | cleanup | keys | open` — only `thrift-*` resources (tag
  `project=thrift-agent`); keys and the DB password in the PC's git-ignored `var/azure/`; the Telegram token read from
  the Mac's .env, never printed; `keys` needs a TTY. Log (`api/host.json`): Warning by default, `Host.Results`
  Information, our lines `Function.<name>.User`, sampling on; a failure logged as its type and place (`util.where`), never
  its message, a body, a key or a token.
- **Endpoints** (function keys `mac`, `gmail`, `dashboard`, `phone` — the phone's scoped to `get_dashboard` only — plus
  the upload key; `x-functions-key`, the dashboard `?code=`): `POST /email`, `/samples`, `/heartbeat`, `/sync`,
  `/orders`, `/mac-event` (`shipped …`, `not_for_sale`, `sold_found`), `/test-message`; `GET /tasks` + `POST
  /tasks/{id}`; `GET /sales`, `POST /sales/{id}/match`; `GET /dashboard`, `/health`; the share endpoints. Quiet hours
  23:00–08:00 send silently.
- **Modes** (`SALES_MODE`): `replay` (every would-be message to the private chat as "[replay] …", no take-down) | `live`
  (emails after `go_live_at` act) | `off`. Going live: the first heartbeat seeing a new `go_live_at` runs
  `sales.golive_check` once (Depop / Vinted items whose Poshmark page is no longer for sale → `not_for_sale`).
- **Gmail** (`gmail/Code.gs`, read-only, every 5 min): each new email from poshmark.com / depop.com / vinted.com
  (`newer_than:2d`, seen ids in Script Properties) → `POST /email`, then `/heartbeat`; bodies as bytes; paced reads,
  quota waited out; an unreadable email tried 3 runs (`TRIES`) then sent by headers (`unreadable`) → 📭 privately; large
  bodies fetched as attachments. `dumpSamples()` → `/samples`.
- **Recognising a sale** (`emails.py`): Poshmark's `<title> just sold to @x on Poshmark!` with or without quotes
  (`REAL`, `TITLES`, "just sold to" in `SOLD`); "Reminder to ship your Poshmark order: …" (`REMINDER_SUBJECT`) is the
  sale itself unless one is known (`core._reminded_sale`, same site + title ≤ 14 days) — "💰 Found by Poshmark's
  reminder to ship it: …"; an email still saying "sold" that no rule took (`emails.maybe_sale`) → ONE private "❓ … may
  be a sale I didn't recognise"; ignored emails re-read every 15 min for 7 days (`core.reclassify`) → "💰 Found late …".
- **A sale**: "💰 Sold on <Site>: <title> — $X. Ship by Thu Oct 9." Matched by listing URL / id / Depop SKU, else the site's
  posted listings by title (≥ 0.9), else by the title's words (≥ 60%, `match.WORDS_SHARE`) AND its price, exactly one
  item (`match.item_for_words_and_price`); else unmatched — never a question (WO53 §6): ONE quiet private "Sold on
  Poshmark: Ralph Lauren Home Leopard-Print 4 Dinner Plates… — not one of the agent's listings, nothing to take down."
  (`core.NOT_OURS`, whole words; `thrift sales match` and a reply with title words still work). Take-down
  tasks for every other posted site (`core.ensure_takedowns` on every path: a listing synced after the sale, the order
  sweep, a task left to her reopened once); sold twice → "⚠️ Sold twice …". SHIPPED stops reminders, DELIVERED closes,
  CANCELLED is said (no automatic relist). GET /tasks also answers `sold` (items whose queued rows become `skipped:
  sold`) and `sold_on` (`core.sold_sites` → `sales.mark_sold_here`, the site's own row `sold`).
- **Ship-by** (`api/thrift_api/deadlines.py`): the email's date wins; else Poshmark +7 days, Vinted +5 business days (US
  federal holidays skipped), Depop +5 days. Reminders after 09:00: the day before, the morning it is due; none for a
  sale its site shows shipped / delivered / completed / cancelled. Overdue → "📦 Overdue: <title> (<Site>) — ship today"
  in the group once (kind `overdue_site`) only when the site, read after the day ended, still shows it to ship; else the
  old private line. Her `shipped <title words>` marks it.
- **Take-downs on the Mac** (`thrift_agent/sales.py`, `post/takedown.py`): in any hour, never with PAUSE, the lid closed,
  or a site showing a login / CAPTCHA / block / check page (`parallel.may_take_down`, `crosslist.account_blocked`). ONE
  GET /tasks at most every 2 min for all sites (`sales.TASKS_EVERY`). Poshmark: Availability Not for Sale
  (`set_availability` → `_save_edit`), the public listing read until not for sale. Vinted: Hide (waited ≤ 15 s; counts
  only when Hide has gone; a reload hands it over with `pressed`, never clicked twice). Depop: DELETE (her decision) on
  `/products/<slug>/manage/` — the bin, the window, Delete listing once, the address gone = done (judged by the address
  even when the job errors on Depop's reload); a listing already SoldOut is never deleted (`sold`, "⚠️ Sold twice").
  A take-down finding the listing gone marks it `delisted` (`sales.repair_gone` at start). A failure is `waiting` until
  GET /tasks leases it again (the API counts attempts on leased tasks only); three leased failures → the group's
  "Couldn't take … down on <Site> — please mark it sold there."; a site whose control is UNVERIFIED → `manual`. All
  done → "✓ <title> taken down on …". Evidence to `failed/shots/`. `thrift delist --verify --marketplace m --url …`
  (records a control without using it), `--practice --marketplace depop --url …` (the window, Cancel), `--run`, `--item
  <item> --marketplace poshmark` (her one-off Not For Sale, via `gui_once.sh`); `thrift relist <item> [--marketplace m]`.
- **The API's nets**: `core.late_takedowns` (15-min timer) — a live sale 2 h old whose item is still up elsewhere → ONE
  group "⚠️ <title> sold on <Site> but is still listed on <Sites> — please mark it sold there." (settings
  `takedown_late:<sale>`; a sale found late counts from when it was recorded, `FOUND_LATE`) and the dashboard's "Sold,
  still listed"; `watch.unposted` (08:00–22:00) — an item priced an hour ago whose row is failed / skipped for a reason the
  Mac doesn't settle itself → ONE `repost` task (`sales.repost`: back to `queued` once; KEEP_REASONS: no size, a twin,
  sold, autopublish off, unconfirmed, mismatch, refused the form, waiting for the brand, not the item's family, held) →
  still not live an hour later → ONE "⚠️ <title> isn't on Poshmark: <reason>"; `watch.not_live` (the 2-hour 🚨).
- **WO56 §0.3 — the bulk read reaches every listing:** Vinted's wardrobe to its last page (`vinted.js wardrobePages`,
  96 a page), Depop's products JSON by its own cursor (`depop.js shopAll`: the address the shop page fetched, else
  `selectors.json depop.shop_products`, unverified; else the tiles scrolled to the end); `photocheck_bulk` logs pages,
  complete, `base` (record it in selectors.json). A listing the bulk data lacks is flagged ONCE and read by its OWN
  data (`photocheck.confirm_absent` → `ExtensionPoster.listing_states`; a listing known live read first — a 404
  counts only when it answers): live → cleared; sold → the sale net; 404 → gone when seen again 10+ min later.
  `thrift photocheck --recheck-not-in-bulk [--since …] [--dry-run]`.
- **Gone vs sold (WO56):** only a SALE acts on the other sites (`sold_found`); Poshmark's own Not for Sale keeps WO49's
  rule (her signal: `not_for_sale`, the other sites come down). A listing merely GONE — its own page / data 404,
  removed, closed without a sale — is that row only: `delisted` "gone from <Site> (its own data: …) — not
  relisted", ONE private line "Vinted no longer has <title> (deleted by you, or removed by Vinted — check the Vinted
  app's notifications). Still live on Poshmark and Depop." (`photocheck.gone_line`); never relisted; its drops go on
  elsewhere.
- **The sale net without email** (photo check): a `posted` listing its site shows sold or no longer for sale — Poshmark's
  public JSON (sold_out → sold; not_for_sale / 404), Depop's / Vinted's shop data (sold / closed; hidden never acted on)
  → `sales.listing_gone` (`sold_found` / `not_for_sale`) → take-downs elsewhere; the row `sold` / `delisted`. SOLD acts at
  once; not-for-sale or 404 only when seen again 10+ min later (kv `photocheck_gone_seen`). A take-down of ours = only
  the row. `runner.next_job` never posts a `delisted` / `sold` row again.
- **The sites' sold orders, read only** (`orders.py`, `parallel._sweep`): nothing on an order page is clicked, typed or
  submitted; no buyer written to, no label downloaded; never stored: buyer names, addresses, tracking (only `orders.SEND`
  fields leave the page); order pages never pictured (`T.noShots`). Poshmark: the seller-hub JSON (a response listener)
  + ≤ 4 open orders' pages (sku, inventory_units status, shipped_on…); "Sold" → to_ship (`orders.POSHMARK_STATUS`,
  `MORE_ANSWERS`). Vinted: `/api/v2/my_orders` (status sentences; an unknown one told once "🔎 …"). Depop: receipts JSON
  with the Bearer token, no cookies (`orders.DEPOP_CODES`), else the drawn list; Depop's emails carry no order id → the
  one open Depop sale within 3 days. Sweep: each site worker on an idle turn or inside a pause with ≥ 60 s left
  (`orders.BUDGET`), one site at a time (`SWEEP_LOCK`), from 08:00 then every 6 h; a login / CAPTCHA page → "⏸ Can't read
  <Site>'s orders today — …"; failure retried after 30 min. `POST /orders` → `api/thrift_api/orders.py`: SHIPPED /
  DELIVERED / COMPLETED quietly, CANCELLED said; an order our records lack (sold ≤ 45 days, not cancelled) is recorded
  as a sale. kv `orders_swept` / `orders_stopped` / `orders_failed`. `thrift orders --sweep [--marketplace m] |
  --probe`.
- **The Mac's link** (`THRIFT_API_URL`, `THRIFT_API_KEY` in .env; missing → all of this off, one private line a day):
  SQLite triggers mark changed items / listings / price_changes / shares (`api_dirty`), pushed to `/sync` each minute in
  order (kv `api_dirty_seeded`); other calls wait in `api_outbox`; the heartbeat every 15 min and on wake carries
  `cli.beat_info` (last look, `worker_progress`, upload poll, cleanup, form checks). SQLite is the posting's truth,
  Postgres the sales'. `thrift sync --push [--all]`, `thrift api ping`, `thrift sales [--open|--unmatched|--all]`.
- **The dashboard** (`GET /dashboard?code=…`, `api/thrift_api/dashboard.py`): cards (active per site, sold this month,
  to ship with the nearest ship-by, unmatched, sold-still-listed, drops due this week), Shares today, a table newest first
  with each site's status / links, the Shipping column (`core.shipping`), "Orders read on the sites: …", per item "live
  since … · drop …"; filters All / Active / Sold / To ship; no buyer data; inline CSS / JS (CSP), light and dark.
- **The 9 PM line** (`watch.evening_line`): "📊 Today: 7 shares → 8 items, 7 live on all 3 sites with all photos, 1
  waiting for a size." + the sites line + "🧹 Cleanup freed 270 MB." (from the heartbeat).

## Her laptop comes first (WO54 §4)
- **The Thrift Chrome** (`deploy/com.thrift.chrome-cross.plist` → `deploy/chrome_cross.sh`; live 2026-11-09: 9.6 GB,
  51 pages on her 8 GB laptop, two pages burning a core each since 1 AM): RunAtLoad false, no KeepAlive, Nice 10,
  LowPriorityIO; the script sets the last session aside (`Default/Sessions.last`) and execs Chrome with
  `--no-startup-window` (no window, no page, never a restored session, no start URL; never `--remote-debugging-*`,
  `--enable-automation`, `--load-extension`, `--restore-last-session`). **On demand:** a job with no extension starts
  it (`Bridge.want`, event `thrift_chrome_started` {on_demand}); **idle quit:** nothing for 10 min (`IDLE_QUIT`) →
  SIGTERM to its own main pid (`runner.quit_thrift_chrome`, verified by `--user-data-dir`; never her Chrome, never
  SIGKILL; event `thrift_chrome_quit`) — not while one of its windows had her focus in 10 min or a login / CAPTCHA
  page waits for her (≤ 6 h); at once at pressure critical. A Chrome quit on purpose is no missing extension. Site
  windows `focused: false`, never focused; a job's tab is active inside its own window only. The tidy (every
  minute, during and after jobs) closes every page no running job holds; event `ext_tidied` each run.
- **Poshmark's Chrome** sleeps after 10 idle minutes (`browser_slept`, no loss, no line) and wakes on the next page.
- **Priority:** worker `ProcessType Background`; poster and Thrift Chrome `Nice 10` + `LowPriorityIO`; HEIC on one core.
- **Memory budget** (`memory.py`, 2 GB): `memory.may_start(db, site)` → no NEW listing at pressure warning/critical
  (edits and take-downs go on); critical → the idle browsers quit. `thrift doctor`: Agent / CPU now / Pages open /
  In front.
- **Nothing in iCloud Drive or Spotlight:** `paths.archive` = `~/thrift/var/local.noindex/archive` (the owner's OK,
  2026-11-09; moved at 3:17 PM by `deploy/move_archive.py`: copied, checked, then deleted from iCloud); a share
  taken short or with cloud-only files waits in iCloud's `Posh/archive` for its late photos. The owner's step for
  the rest: System Settings → Spotlight → Search Privacy → + `~/thrift`.

## Storage and cleanup (`thrift_agent/cleanup.py`, `thrift cleanup [--dry-run | --sizes]`)
- No duplicate share copy ever (deleted at once, see Shares). Azure blobs deleted once fetched (30-day lifecycle rule as
  backstop). Items keep ONE copy, the work copy (`work/<batch>/item_NN/photos` + cover): the archive copy of a split
  share goes 7 days (`KEEP_DAYS`) after the share once every item has its work copy; `work/<batch>/all` once split;
  sold items' work photos once the sale is shipped or done (never with a listing queued / posting); repeat / empty /
  failed / test shares after 7 days. WO53 §5: a share whose items are ALL live on all three sites (or sold and shipped,
  or never listed — `cleanup.live_everywhere`) lets its archive copy go a day after it came (`SETTLED_AFTER`, kind
  `settled`) and its contact / regroup sheet at once (kind `sheet`). **Evidence** (`failed/shots`, 2.2 GB on 11-08):
  after 14 days (`EVIDENCE_DAYS`); an item's an hour after it is live on all three; the folder kept under 300 MB
  (`EVIDENCE_CAP`, the oldest first); pages (HTML) older than an hour gzipped (`<name>.html.gz`, `gzip -dc`) — hourly
  (`trim_evidence`, kv `evidence_trim_at`, event `evidence_trimmed`) and at once before a low-disk line; an item with an
  open problem (`cleanup.open_problems`) keeps every file, even over the cap. **Chrome caches**: both of our Chromes
  `--disk-cache-size=268435456` (the Thrift Chrome's plist from its next launchd bootstrap — `kickstart -k` keeps the
  loaded arguments; Poshmark's at launch), Poshmark's profile caches (Cache, Code Cache, GPUCache; never cookies or
  storage) emptied before the poster opens it when over 256 MB (`base.clear_cache`). The photo memory and the DB are
  never touched, nor a share in flight or a work copy still needed. Once a day from the worker (`cleanup.tick`), event
  `cleanup`, kv `cleanup_last`; no line of its own (the 9 PM summary). **Disk** (`cleanup.disk_watch`, 10 min): under
  5 GB free → the evidence trimmed first, then ONE private "⚠️ The Mac has 3.6 GB free (under 5 GB) — please free space
  on the Mac. The biggest: iPhone backups (Finder → the iPhone → Manage Backups) 35.0 GB, … The agent already freed
  1.6 GB today; its own folders take 900 MB." (`cleanup.biggest`: `du -sk` of `BIG_USERS` + the swap, time-limited;
  never iCloud or failing-share wording); back above 6 GB silently (event `disk_ok`); `thrift status` "disk: X free
  of Y".

## Commands
- Intake / items: `thrift init | run | process <dir> | status | show <item> | doctor` (`doctor.py`; also a reply "doctor"
  privately: worker progress, inbox / intake / Azure queues, poster, extension, each site, open take-downs, today's
  shares, jobs, memory by app) `| shares [--day|--all] | late --take <batch> | late --add-to <item> --file … [--dry-run]
  | photos --forget <item> | twins [--apply] | replay <case> | replay --golden [--record] [--card] | eval | harvest |
  build-style`.
- Answers (the CLI twins of replies, echoed privately, settling the pending message): `price <item> <amount>`, `answer
  <item> "<note>"` (`"no brand"`, `"recheck"`), `confirm <batch> "<fix>"`, `condition <item> nwt|like_new|good`, `kids
  <item> girls|boys`, `category <item> "<category › subcategory>"`, `edit <item> [--title …] [--brand …] [--sizes
  'Cardigan=S,Pants=XS']`.
- Rebuilding: `redo <batch>` (a split batch's unposted items back to `new` from the accepted grouping; price, dry-run rows
  and messages dropped, her condition / kids answers kept; posting / posted / drafted / unconfirmed / dropped left;
  refuses a batch with nothing to rebuild) · `reprocess <item>` (in place, today's prompts, her answers kept; the card
  re-sent only if it changed; an answer meanwhile wins) · `recover <item|batch> [--recheck] [--relabel]` (only cover,
  order, category, size — stored checks reused unless `--recheck`; prints "card sent again" / "card changed (goes out
  when its turn comes)" / "card unchanged (not sent again)") · `requeue <item> [marketplace] | requeue <batch>` (a failed
  batch back to the worker — failed batches never retry by themselves; a legacy `needs_owner` item; a skipped / failed
  row once fixed; refuses an unconfirmed publish) · `retry <item> [marketplace]`.
- Posting: `poster [--once] [--dry-run] [--stage form|review] [--publish-first <item>] [--marketplace m]
  [--allow-dev-browser]` · `mark-posted <item> <marketplace> <url>` (only an unconfirmed row; a listing page no other
  item holds, our title and price; queued for the running poster) · `pause <site>` / `resume <site> [--dry]` · `canary
  [--check <site|all> | --make [<item>]]` · `crosslist …` (see Cross-listing) · `catalogs check | refresh` ·
  `recategorize <item> "<path>" --dry-run|--now` · `titles --review [--send] | --apply` · `brand-fix <item> …` ·
  `cover-fix <item> …` · `price-drop --list | --skip | --dry-run | --now <item>` · `login --site poshmark|vinted|depop`.
- Sales: `sales [--open|--unmatched|--all] | sales match <sale> <item> | delist --run | --verify … | --practice … | --item
  <item> --marketplace poshmark | relist <item> | orders --sweep | --probe | sync --push [--all] | api ping | cleanup`.
- WO54: `cards --backfill [--dry-run]` (the ✅ cards never sent, oldest first; refused in the quiet hours) · `quiet
  [--status | --tick | --post-now | --quiet-now]` · `text <item> --title … | --add-line …` (WO53).
- Telegram: `telegram setup | test [--ops]`. `--marketplace poshmark|depop|vinted` on `poster --publish-first`, `retry`,
  `mark-posted`, `requeue`.

## Model calls
Every call through `brain/llm.ask`: one tool, the answer validated by pydantic (one repair round). Forced `tool_choice`
where the model takes it; Opus 5.5, Sonnet 5.5, Fable 5.1, Mythos 5.1 (and any model whose 400 says so, remembered for
the process) get `tool_choice` auto, an explicit instruction and one reminder. No strict tools / structured outputs (our
schemas use minimum / maximum / minLength and > 24 optional fields). Models: segmentation, extraction, labels, cover =
Opus 5.5; upright, heal = Sonnet; crosslist category = Opus. `brain/llm.Answers` records / replays answers for the
replay harness (keyed by model, prompt, tool, images' bytes; a replay never asks — `MissingAnswer`).

## Tests and style
- Python 3.11+, pydantic v2, pathlib everywhere (Windows + macOS). Pure functions for anything testable. `pytest -q` must
  pass before deploy. Tests never read `config/settings.local.yaml`, `private/` or `.env` (`tests/conftest.py`; opt in
  with the `settings_override` fixture), nor the machine's own disk, clock or network (faked, or guarded by `is_prod`).
- JS: `tests/js/content.test.mjs` (jsdom on the stand-in forms `tests/fixtures/ext_*.html`, with hooks per incident;
  `npm ci --prefix tests/js`, Node ≥ 22.22; run by `tests/test_ext_js.py`, skipped without Node — the Mac has none);
  `tests/test_ext_integration.py` (Playwright's Chromium + the unpacked extension + the real bridge, every host but
  127.0.0.1 unresolvable; skipped without `python -m playwright install chromium`); `tests/test_bridge.py` with
  `tests/ext_fake.py`; `tests/test_crosslist_flow.py` runs every loop test with both drivers; the Poshmark stand-ins
  (`tests/test_poshmark_form.py`, `tests/fixtures/poshmark_*.html`).
- **The replay harness** (`replay.py`, `thrift replay`, `tests/test_golden.py`): a saved share through the whole intake in
  a scratch workspace and DB with recorded model answers, compared with `expected.yaml` (`items`, `outcomes`, `each`,
  `said_contains`). Golden cases in `private/tests/golden/<case>/` (the Mac only; skipped in CI); a prompt change needs
  a new recording.
- Eval (M0): `eval/fixtures/<case>/{photos,expected.yaml}`, `thrift eval`; targets segmentation ≥ 95%, brand / size ≥
  95%, condition ≥ 90%. Tune `segmentation.pause_*` / `visual_change_*` on real rolls.
- Still open: Poshmark's `draft_saved`, the Promote toggle's markup, the CAPTCHA wording; Depop's API (`DEPOP_API_KEY`);
  M4 Airtable / n8n (not built); M6 sold-comps pricing (tune `private/brand_tiers.yaml` from sales).
