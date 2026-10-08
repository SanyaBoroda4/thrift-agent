# Thrift Agent — spec for Claude Code

Photos shot on an iPhone become live Poshmark (then Depop) listings with no typing. The seller shoots,
taps Share → "New item", and gets a Telegram ping when it's listed or when something needs an answer.

## Machines
- **Windows PC (dev)** — code, tests, eval on fixture photos. `machine_role: dev` forces dry-run;
  the dev machine never logs into or posts to a marketplace.
- **MacBook (prod)** — MacBook Pro 13" M2, 8 GB, macOS 27 Golden Gate, Python 3.14 (python.org), bash shell, no Homebrew. Runs `thrift run` (worker) and `thrift poster` (Chrome) as launchd agents.
  The poster's Chrome profile is created and logged in *on the Mac only* (cookies are keychain-bound).
  **The Thrift Chrome (WO32):** a second, ordinary Google Chrome (`~/thrift/chrome-cross`, LaunchAgent
  `com.thrift.chrome-cross`, its own Dock icon) where the Thrift crosslister extension (`ext/`) lists on Vinted and
  Depop; the owner logs in there by hand. See "The extension driver".
- **iPhone (dedicated)** — same Apple ID as the Mac; saves photos to iCloud Drive `Posh/inbox/<ts>/`.
- Deploy: `git push` from PyCharm → `deploy\deploy.ps1` (over SSH: pull, private pull, `mac_setup.sh` + tests,
  restart the worker service, show status + log). See "Mac over SSH".

## Mac over SSH (WO21)
- The PC reaches the Mac with an SSH key, no password: `ssh tatiana_sorokina@192.168.68.57` (MacBook-Pro-5.local;
  its host key is known under the IP). Repo `~/thrift-agent`, venv `.venv`, logs `~/thrift/logs`, DB `~/thrift/var`.
- **Find the Mac first (WO28):** it travels (the owner's work) and DHCP moves addresses — on 2026-10-05 the PC itself
  had 192.168.68.57. Try `MacBook-Pro-5.local`, then 192.168.68.57; if neither answers, ask the user for the IP and
  wait — never assume. Reach a new address with `-o HostKeyAlias=192.168.68.57` so the known host key must match.
- `deploy\deploy.ps1` (`-MacHost` to override, `-HostKeyAlias 192.168.68.57` for a new address, `-NoPush`): `git push`,
  then on the Mac `deploy/mac_deploy.sh`: `git pull`, `git -C private pull`, `bash deploy/mac_setup.sh` (venv,
  folders, the tests, the launchd files), the WORKER
  (re)started as the launchd service, then `services.sh status`, `thrift status` and the worker log's last 30 lines. A
  failed setup or test run rolls the Mac back to the commit before the pull; any failure (incl. a worker that isn't
  running afterwards) exits non-zero.
- `bash deploy/services.sh start|stop|restart worker|poster`, `start|stop chrome` (the Thrift Chrome, WO32), `stop
  all` (worker + poster), `status`, `logs [worker|poster] [lines]`. The deploy starts the Thrift Chrome when it isn't
  running (never restarts it) and makes the extension's token once.
  The worker runs as the launchd service (`com.thriftagent.worker`, KeepAlive). **The poster service stays off** until
  the owner turns publishing on (the two keys, unchanged); deploy never starts it, and there is no bare
  `start`/`restart` that would.
- **Worker trouble is told once (WO22, WO29, `alerts.py`).** The iCloud inbox that can't be read — a read macOS
  refuses or interrupts (EPERM / EACCES / EINTR: python3.14 waiting for its iCloud Drive permission), or a look at the
  inbox stuck in that wait (noticed by the Telegram thread) — is retried quietly; after 10 min ONE plain line in the
  group ("⚠️ Can't read the iCloud inbox — on the Mac, open System Settings → Privacy & Security → Files & Folders →
  python3.14 and turn iCloud Drive on; I continue by myself."), nothing more until it recovers, then "✓ inbox readable
  again" in the ops chat. **iCloud still downloading or coordinating a file is no error** (WO29; live: "OSError:
  [Errno 11] Resource deadlock avoided" on the first automatic share — EDEADLK is 11 on macOS; also EAGAIN,
  ETIMEDOUT: `alerts.icloud_busy`): the inbox or the batch is simply taken again on the next tick (a batch stays
  `new`), the ops chat hears once only past 10 min (`alerts.busy`), "✓ iCloud free again" after. Items already split
  keep being processed. Any other worker error (a tick, a batch, an item) goes to the **ops chat** once and then at
  most once a day per identical error (item/batch ids ignored: ten items failing on one bad key are one message) —
  never to the group; every occurrence is in the event log and `thrift status`.
- **One worker, ever:** `thrift run` holds `worker.lock` next to the DB (an OS lock, dropped when the process ends; the
  holder is in `worker.lock.pid`); a second one exits at once with "another worker is already running (pid …)".
  `services.sh start worker` refuses while a `thrift run` runs outside launchd (a Terminal window). Two workers would
  take each other's Telegram updates.
- **After every work order** Claude deploys to the Mac itself, checks `services.sh status`, `thrift status` and the
  logs, and puts the result in the report.
- **Allowed over SSH without asking:** the deploy, status, logs, `thrift requeue`, `thrift redo`, the tests,
  read-only commands, `thrift telegram test`. **Never over SSH:** `thrift poster --publish-first`, starting the poster
  or anything else that publishes or touches the live Poshmark account (`thrift login`, `thrift poster`),
  `thrift mark-posted`, deleting data, changing `config/settings.local.yaml` or `.env` — those are the owner's.
- **Publishing is LIVE since 2026-10-05** (the owner's three keys; never switched to dry-run or changed by Claude).
  A work order that changes the poster says so and the poster service is restarted with the deploy, only when it is
  idle (never mid-item): `services.sh stop poster` → wait until `status` says it is off (it finishes the item in hand,
  ExitTimeOut 300 s) → deploy → `services.sh start poster` → confirm "Poster started (LIVE)" (the `poster_started`
  event, `{"live": true}`, the poster log's first line, and — WO29 — the line in the ops chat, never the group).

## Flow
```
iCloud Posh/inbox/<ts>/ (+ _done) ─► register batch ─► prep (HEIC→JPEG, EXIF rotate, burst dedupe)
  ─► segment (one Opus vision call on 768 px previews → groups; visual identity first, "— pause 2 min —" markers
     relative to the roll as the only timing; code checks partition, full shot, sizes, confidence, and — only for an
     item the model was unsure of — non-contiguous photos, a pause AND a colour change inside it / neither between two)
  ─► the grouping is accepted at once (owner decision, WO20b: `segmentation.auto_confirm`; the contact sheet is saved,
     not sent; the doubts stay in `thrift status`; a grouping that isn't a partition still asks with the sheet)
     ([Wrong photos] on a price card reopens it: the sheet as the open message, 12>2|split 7|merge 2 3|drop 7|ok,
      only the items whose photos changed are rebuilt; never for an item on the marketplace)
  ─► items ─► extract Facts (evidence per field) ─► labels read closely (premium details, WO26)
  ─► price (brand_tiers.yaml, × one premium factor) ─► copy (both marketplaces)
  ─► verify (LLM strip unsupported claims) + lint (deterministic) ─► gate: publish | draft | needs_info
  ─► (shoes in doubt, brand new vs worn: awaiting_condition ─► "Brand new or worn?" [NWT] [Like New] [Good] ─►
      reprocessed with the answer)
  ─► (a kids item, under 0.70 sure of Girls/Boys: "Girls or Boys?" [Girls] [Boys])
  ─► awaiting_price ─► the price card [$X-10] [⭐$X] [$X+10 … +60] [Later] [Change] (allowed questions folded in) ─► ready
     (Telegram is ONE queue: one open message at a time, the oldest batch first — see "Telegram")
  ─► poster: fill form → read back → diff → dry-run | draft | publish → verify live page → record URL
     (publish: List This Item once → Poshmark's closet ?created_listing_id=<id> → the closet reloaded until that
      listing shows, ≤ 90 s → its /listing/<slug>-<id> address; not found → "unconfirmed publish", `mark-posted`)
     (dry-run stage form | review: never the publish button; the form is left through Poshmark's Discard;
      after the upload Poshmark's cover dialog is applied with its default crop, any other dialog fails the item;
      each dry-run compares Poshmark's Drafts count before and after — more drafts → Telegram warning + Outcome note)
     (never a question, WO27: a value the form doesn't offer exactly → the closest one, listed in "Posted ✓ <title>
      — check: …"; a required field nothing comes close to → the item skipped, nothing saved, reported)
     (the daily window, WO28: no new listing with the lid closed or below 15% on battery; the Mac kept from idle
      sleep while listings remain; a listing the Mac slept through is looked for in the closet before "unconfirmed")
  ─► cross-list (WO30): Poshmark, Depop and Vinted AT THE SAME TIME (WO33: three site workers; the cross rows queued at
     the price's approval), the same approved price; every value mapped from the saved catalogs (data/*_catalog.json);
     a dry run unless the marketplace's autopublish is on; then ONE "Posted ✓ <title> — $X · Poshmark <url> · Depop
     <url> · Vinted <url>" line for the item, once all its sites are settled
  ─► sold anywhere (WO33): thrift-api (Azure) reads the marketplace emails (a Gmail Apps Script) → "💰 Sold on <Site>"
     → take-down tasks → the Mac's site workers take the item down elsewhere (reversible) before any new listing →
     ship-by reminders until shipped; a dashboard on the phone
```
Item statuses: `new` → (`awaiting_condition` →) `awaiting_price` | `needs_info` → `ready` → `posting` → `posted` |
`drafted` | `failed`; `needs_owner` only for an item a poster from before WO27 parked (`thrift requeue` takes it
back); `dropped` (a re-share the owner called the same item). Nothing publishes without price approval. The item is
`posted` once Poshmark is (WO30); each marketplace is a row of `listings` (item_id, marketplace, status, url,
listing_id, price, fields_json, posted_at, error, attempts, updated_at): queued → posting → posted | failed | skipped
(later delisted | sold), plus dryrun / drafted; unique per (item, marketplace). It replaced the `posts` table, whose
rows were copied in once (kv `listings_migrated`); `posts` is left as it was.

## Invariants — do not break these
1. **Facts before prose.** `extract` never writes copy; `copy` may only restate Facts. Null facts are omitted.
2. **NWT only with a hang-tag photo** (or a seller note). Mislabeled condition is the most common cause of
   "not as described" cancellations and bad ratings.
   - **Grades (owner decision, WO17 — replaces "when unsure, the lower one").** Torn between Like New and Good →
     Like New: the model names the other grade it weighed (`condition_alternative`) and `pipeline.settle_condition`
     takes the higher of good / excellent / like_new. Poshmark has no "very good": excellent goes up as Like New.
     **Never Fair:** a fair reading (or good weighed against fair) is listed as Good, with the Note "looked well-worn —
     listed as Good; check before approving"; the form never selects Fair (`CONDITION_TO_POSH`, `POSH_FAIR` recorded
     only). NWT stays strict (`settle_nwt`); NWOT / like_new for "new, no tags". Excellent → Like New on Poshmark:
     confirmed by the owner (WO18).
   - **Shoes: brand new or worn? (owner rule, WO18 — ask only in doubt, never routinely).** For shoes (any
     department) the model reads `unworn` (yes/no + confidence + photos: soles, insoles, toe-box creasing, box, tags,
     sole stickers) and `box_photo`. `pipeline.shoe_condition_doubt` — code, on the model's own reading — asks when
     0.30 ≤ the confidence that the pair is unworn ≤ 0.80, or when `condition` and `condition_alternative` straddle new
     (NWT / NWOT / like_new) and used. Not when the owner already priced or answered; never for clothing, bags or
     accessories. The item waits in `awaiting_condition` with ONE message before the price card: cover + "Brand new or
     worn? (couldn't tell from the photos)" + [NWT] [Like New] [Good] (or `thrift condition <item> nwt|like_new|good`).
     The answer is `items.owner_condition`, the condition with source `owner` (NWT then needs no hang-tag photo): NWT;
     Like New = brand new without tags = NWOT ("New without tags."); Good = worn. The item is reprocessed (new price,
     copy, listing) and the price card follows; it is never asked again. Never Fair.
   - **Owner rule (first run).** `gate.min_confidence` is 0.70 for brand, size and condition; the price is the
     owner's only routine input (Telegram approval or `thrift price`). NWT stays strict — `hang_tag_photo` or a
     seller note, never confidence. **Always a price (WO20):** brand tier → the department's category default (the
     category, then Poshmark's other names for it — Kids "Shirts & Tops" finds `Tops` — then `other`) → the flat
     legacy table the same way → `pricing.default_target` (source `default`). The card always has a number; it no
     longer says "no price history". (The live miss: a Kids tee, Poshmark's "Shirts & Tops", against the Mac's
     committed flat table — no key matched and price() had nothing after the tables.)
   - **Explicit answers.** A price alone settles brand/size (the model's best reading stands) but never these two:
     NWT without `hang_tag_photo` is listed as **like new** unless the owner's reply says "NWT" (the note then
     becomes the evidence); a suspected **re-share stays held** (the price is recorded, the item stays
     `awaiting_price`) until the reply says "different item" (list it) or "same item" (drop it, status `dropped`).
   - **Condition wording (owner rule, WO16).** Wear and flaws are never put in words — no dirt, dirty, stain, scuff,
     worn, wear and tear, fraying, pilling, hole, tear, smell… in the title, the descriptions or the tags. A used item
     (like_new, excellent, good, fair) says ONE neutral line, "Gently pre-loved, please see photos for condition.", and
     is never "no flaws", "like new" or "excellent". Every flaw has a photo that is in the listing (`fit_photos`); a
     flaw close-up is never the cover, but the FRONT stays the cover even when a small flaw shows on it (WO23: the first
     photo is the front, and that photo discloses the flaw); a flaw without a photo is a warning on the price card. The copy
     writer never sees the flaws or the condition evidence; `copy.condition_rule` runs after the verifier (drops a
     sentence with wear words, puts the line in) and `verify.lint` checks the result (wear words, used-item claims,
     the line and the flaw photos).
   - **The only questions.** Brand or size below 0.70 (the brand question carries a [No brand] button, WO25), a size
     none of Poshmark's size menus for the category has (WO25), NWT without a hang-tag photo, "Which category?" — the
     model under 0.70 sure of its category, or one Poshmark doesn't have / "Other": its own message with 1-3 real paths
     as buttons (WO25; with no real path to offer, the old typed question on the card) — a possible re-share, a pair of
     shoes in doubt between brand new and worn (its own message, before the price), Girls or Boys for a kids item the
     model is under 0.70 sure of (its own message, WO20). **Never the poster** (WO27: it takes the closest value the
     form offers and lists it in "Posted ✓"); **never a set's category** (WO27: its bottom decides). **Never the
     grouping** (owner decision, WO20b): it is accepted without asking; [Wrong photos] on a card is the owner's way
     back, not a routine question; only a grouping that isn't a partition (a photo in no item, or in two) still sends
     the contact sheet, since taking it would lose or double a photo. The model's own `questions` about
     optional facts (material, measurements) are dropped — a missing optional fact is left out of the listing; an
     unsure condition is kept in the item's record (`gate.info`), never on the card. CLI actions (`thrift price` /
     `answer` / `confirm` / `condition` / `kids` / `category` / `redo`) are echoed to the ops chat (WO29) and settle the
     pending message.
3. **The model never clicks publish.** Deterministic code fills, reads back, diffs, then publishes.
   An LLM fallback (Playwright MCP) may *fill* a form when a selector breaks; code still verifies and submits.
4. **Idempotent posting.** Row → `posting` before the form opens (Poshmark's Playwright poster); on the extension
   driver (Vinted, Depop) the moment before its ONE go-ahead is sent, after the owner's POST when supervised (WO32b) —
   before it nothing can be published, so a Ctrl+C, a failure or a timeout leaves the row `queued` / `failed`, never
   `posting`. A `posting` row after a crash is never
   retried automatically; reconcile against the closet first. One item ID posts once per marketplace, ever.
   - A publish that pressed List This Item but found no listing address is `failed` with `last_error`
     "unconfirmed publish: …" — it may be live. `thrift requeue` and `--publish-first` refuse it; check the closet,
     then `thrift mark-posted <item> <marketplace> <url>` (only for such a row: the URL must be a listing page no
     other item holds, showing the item's title and price; then `posted` + URL, "✅ confirmed live" in Telegram).
   - WO28: the owner gets ONE reply-able message for such a row ("⚠️ <title>: … I can't see it in the closet. Check
     Poshmark: if it's there, reply 'posted <url>'; if not, reply 'retry'.", outbox kind `unconfirmed`). "posted <url>"
     is queued for the running poster, which opens the page (title, price) between listings (`thrift mark-posted`
     queues it too while the poster runs); "retry" (`thrift retry <item>`) is the owner saying it is not there: the
     row goes back to `queued`. The code never decides either by itself.
5. **Stop, don't guess.** Logged out / CAPTCHA / restricted account → write `PAUSE`, ping, exit. Unknown modal
   → fail the item with a screenshot. Never solve CAPTCHAs, never type credentials.
6. **Human pacing, human hours.** `schedule` limits; no sharing/following/offers/relisting from this code.
7. **All selectors live in `post/<marketplace>.py:SEL`.** Role/label/placeholder/text locators or the form's own hooks
   (data-vv-name, data-et-name, data-test, ids); a CSS class only where it was verified on the live form. Real clicks
   only (a JS `element.click()` on Poshmark's subcategory `<li>` does not register). Unverified entries are listed
   in `UNVERIFIED`; `submit()` refuses to publish or draft while a step it needs is in it.
8. **Shipping guard.** `HOLD_UNSHIPPED` flag pauses posting. Marketplaces restrict accounts with late shipments;
   listing faster without shipping discipline makes that worse.
   - `HOLD_UNSHIPPED` blocks publish only; drafts and dry-runs still run.

## What's verified vs not
- Verified against the live site (Sep 2026): order list pagination button `button[data-et-name="pagination_next"]`,
  order links `/order/sales/<id>`, order page `__INITIAL_STATE__.$_order_details.order` (fields in harvest.py),
  listing page `__INITIAL_STATE__.$_listing_details.listingDetails`, create page `https://poshmark.com/create-listing`,
  restricted-account banner text.
- Verified on the live create-listing form (2026-09-29, WO9 — every `post/poshmark.py:SEL` entry not in `UNVERIFIED`):
  `input#img-file-input`; `data-vv-name` title / description / style-tag-input / listingPrice / originalPrice / sku;
  the category menu (department links `a.dropdown__link[data-et-name=women|men|kids|home|pets|electronics]`, the
  `all` back link, categories `li.dropdown__menu__item`, subcategories `a.dropdown__link[data-et-name=sub_category]`
  incl. "None"); the size menu `[data-test=size]`, tabs `a.navigation--horizontal__link`, buttons
  `button[id="size-<label>"]`, Done `button[data-et-name=apply]`, the kids shoe labels ("7.5 (Toddler Girl)" on the
  Girls/Boys tabs, `KIDS_SIZE_OPTIONS`); the condition labels (`CONDITION_TO_POSH`; Fair `uf` is recorded but never
  selected); brand suggestions; colour tiles;
  the Listing Price dialog (inputs, Smart Sell checkbox, Done); Next / Save Draft / Discard by `data-et-name`; the Women
  and Kids category lists with their Shoes subcategories (`data/poshmark_taxonomy.yaml`).
- Poshmark's catalog (WO24): every department's categories and subcategories — Women, Kids, Men, Home — as the listing
  form's own catalog lists them (read from the logged-in form 2026-10-04; it agrees with the Women Shoes and Kids lists
  recorded live on 2026-09-29), in `data/poshmark_taxonomy.yaml`, all `verified: true`; the Women entries after
  "Global & Traditional Wear" (Ao Dais … Treggings) are its subcategories. The raw catalog, `data/poshmark_catalog.json`
  (in git since WO25: Poshmark's public taxonomy), also gives each category's **size menu per tab** — Women Standard /
  Plus / Petite / Juniors / Maternity, Men Standard / Big & Tall, Kids Baby / Girls / Boys, shoes Standard (Kids shoes
  Baby "0"-"7", Girls / Boys "7.5 (Toddler Girl)" …) — the menus every size is checked against and selected from (see
  "Sizes (WO25)"). And the form marks **Brand "Optional"** (the 2026-09-30 snapshot): a listing without a brand leaves
  it empty.
- Verified from the DOM snapshot of the first Mac dry-run (2026-09-30, WO11): the **"Select a Covershot." dialog**
  Poshmark opens after the upload (`div.image-edit-modal > [data-test=modal-container]`: one `.image-edit-modal__thumb`
  per photo, the first preselected with `svg.icon-green-checkmark`; a croppie crop frame, 3:4 portrait (viewport
  225×300), zoom slider, rotate buttons, "Replace Photo"; Cancel `[data-et-name=cancel]` / Apply `[data-et-name=apply]`)
  — the poster keeps Poshmark's default crop and presses Apply; any other dialog after the upload fails the item with
  the evidence. Also: Poshmark's dropdown component (`[data-test=dropdown].dropdown`, `dropdown_root`); the department
  links as `li > a.dropdown__link.dropdown__menu__item` with "All Categories" on top; condition items carrying
  Poshmark's code (`[data-et-name=listing_condition][data-et-prop-content=nwt|uln|ug|uf]`) with a description line
  under the label; the 130 curated style tags (`[data-et-on-name=style_tag]`); the SKU inside the collapsed Additional
  Details (`a.listing-editor-toggle-link` "show details"); the form's `originalPrice` input hidden
  (`.listing-editor__original-price--hidden`); the price dialog's Smart Sell toggle (`[data-test=toggle-input]`) and
  Shipping Discount showing "Optional" (nothing chosen); the form's Cancel `a[data-et-name=discard]` and the "Save
  Draft" dialog ("Do you want to save this listing as a draft?": "Discard Changes" `[data-et-name=discard]` / "Save
  Draft" `[data-et-name=save_draft]`, then "Saved" / Ok); the Drafts panel `[data-et-name=draftsSection]` with its
  count.
- Seen in Mac dry-run #2 (2026-10-01, WO13): photos, the cover dialog (Apply), title, description and Kids > Shoes >
  Sneakers all went through; the Kids shoe size menu **closes itself** when a size is picked (no Done) and the size
  field `[data-test=size]` then reads the label, e.g. "7.5 (Toddler Girl)". The poster accepts either path for any
  size — Done to press, or a menu that closed with the expected size on the form — and fails on anything else; the
  read-back JSON records which one (`size_menu`).
- Mac dry-run #3 (2026-10-02, WO14) filled the whole form (photos + cover, title, description, Kids > Shoes >
  Sneakers, "7.5 (Toddler Girl)", Good, Naturino, Pink + Green, price 50, SKU) and its read-back matched everything
  but one field: an Original Price left empty reads back as **"0"**. The diff now treats "", "0", "0.00" and no price as
  one for `original_price` only (a planned 120 against 0 still fails). Every other read-back — the photo count and the
  category, size, condition and colour texts — passed on the live form; their exact texts are in that run's JSON.
- Mac form + review stages (2026-10-02, WO15). Form: every read-back matched with these exact texts — photos **6**
  (`photo_thumbs`), category **"Kids Shoes"**, subcategory **"Sneakers"**, size **"7.5 (Toddler Girl)"** (size menu
  "closed by itself"), condition **"Good"**, colours **"Pink Green"**, Smart Sell off — and it left through Cancel →
  "Save Draft" → Discard Changes (`leave`). Review: Next keeps the URL `/create-listing` and slides up a **"Share
  Listing" panel** over the form, behind a backdrop (`share_panel`: a `[role=dialog]`/`.modal` with that text):
  "‹ Back" (`review_back`, text — not a button or a tracked link), the cover and the full title (an h-heading, cut
  with CSS ellipsis), "Promote My Closet" with an Off toggle, Pinterest / Facebook "Connect Now"
  (`a[data-et-name=pn_v2_connect|fb_connect]`, never clicked) and **List This Item** `button[data-et-name=list]`
  (`list_item`). None of the panel is in the DOM before Next. The old back-out (a Back/Edit/Cancel role, else browser
  Back) timed out under the backdrop; it is now ‹ Back → the panel gone → Cancel → Discard Changes.
- The first live listing (2026-10-03, WO16, supervised `--publish-first`): after List This Item Poshmark goes to
  `/closet/<user>?created_listing_id=<24 hex>` (`created_id`), then drops the query; 5 s later that closet did NOT show
  the new listing yet — the poster now reloads it every 10 s for up to 90 s. Closet tiles: `a.tile__covershot
  [data-et-name=listing][data-et-prop-listing_id=<id>]` with the title as the image alt, and a title link whose first
  line is the title; both `href="/listing/<slug>-<id>"`. The listing's address (`listing_url`):
  `https://poshmark.com/listing/<slug>-<24 hex id>`, the slug = the title with everything but letters, digits and
  spaces dropped and the words joined by "-" (48 of 48 closet listings: "Toddler size 7.5" → "Toddler-size-75",
  "One-Shoulder" → "OneShoulder"); the id's first 8 hex digits are its creation time (when the form opened). Matching:
  new since the post started, the tile's title AND the slug are this title's, and it is the id Poshmark named (else an
  id created since the post started); several → never a guess.
- Seen in the first cross-list dry runs on the Mac (2026-10-06, WO30, the poster's own Chrome profile): neither Depop
  nor Vinted is logged in there. Depop's create page answered first with its login page (title "Log in", "Sign up or
  log in", "Continue with email"), 35 minutes later with its block page (HTTP 403, title "Forbidden - Depop", "Sorry,
  not authorized … let us know you were blocked") — the site turning the automated browser away; Vinted's /items/new
  showed "Join and sell pre-loved clothes with no fees" with `[data-testid=header--login-button]`. All three now stop
  that marketplace for the window (AccountBlocked: the group's one plain line); the code never works around a block.
- **UNVERIFIED:** the Promote My Closet toggle's markup (`promote_toggle`: one checkbox → must be unchecked; none →
  the panel must read "Promote My Closet Off"; anything else stops before List — the first publish passed through the
  one-checkbox path); where Save Draft lands
  (`draft_saved`); the size field and Done for adult sizes (the values and tabs are the catalog's, WO25); the
  CAPTCHA wording; every `SEL` entry of Depop's and Vinted's posters (WO30, `post/depop.py`, `post/vinted.py`:
  only Depop's combobox ids came from its logged-in form); the extension's steps that no dry run can show
  (`ext/selectors.json`: Vinted's listing page, Vinted's Hide / Depop's Mark as sold, Depop's shop page links,
  Vinted's skirt length and promotion close) — recorded by the owner's supervised publishes and WO33's probes. Record them from the
  evidence in `failed/shots/` (`.png`/`.html`/`.json` per run, `<item>-review.json`, `…-after-list.*`) or with
  `playwright codegen --channel chrome https://poshmark.com/create-listing` on the Mac.
- **Recorded live by the extension (2026-10-06, WO32, five dry runs of one item in the Thrift Chrome, nothing
  published; `ext/selectors.json` "seen"):**
  - **Vinted:**
    - photos: the hidden `[data-testid=add-photos-input].u-hidden`, thumbnails in `[data-testid=media-upload-grid]`;
    - text: `title--input`, `description--input`, the price `price-input--input`;
    - every detail is a readonly input opening a `*-dropdown/-grid/-list-content` panel:
      - category: the tree of `div#catalog-<id>`, a branch `[role=button]`, a leaf `[role=radio]`;
      - brand: `brand-select-dropdown-input` with a search;
      - size: `category-size-single-grid-input`, a grid of `[role=checkbox][aria-label]`;
      - condition: `category-condition-single-list-input`, `[role=radio]` rows;
      - colour: `color-select-dropdown-input`; material: `category-material-multi-list-input` — both
        `[role=checkbox]` rows;
      - each panel lists a "Suggested" group first; the inputs show the values chosen;
    - the package size: `div#package-size-<n>` cells with radios; Vinted chooses a "Recommended" one by itself;
    - one Upload button `[data-testid=upload-form-save-button]`.
  - **Depop:**
    - photos: `[data-testid=upload-input__input]`, each photo a sortable tile with a media-photos.depop.com image. The
      class `thumbnailError` is only its style while it loads; 6 of 6 displayed.
    - the comboboxes by id; the category menu lists a name once per department under a heading ("Men > Bottoms",
      "Women > Bottoms", "Kids > Bottoms");
    - the brand search matches the text as written ("J. Crew" → only "Other"; "J.Crew" → J.Crew);
    - a multi-select's choices are chips `button[aria-label='Remove <value>']`;
    - **Depop fills fields by itself once the photos are up:** colour chips Cream + Grey, the brand, a package size.
      They are read back and Depop's extras are removed;
    - price `priceAmount__input`; Depop Shipping is the USPS radio; `shippingMethods-input` is the package-size menu;
    - Boost is the switch "Pay an extra 12% fee": kept off;
    - one Post button.
  - The supervised publish may go; the unattended loop waits for `after_publish`.
- **Recorded from the first supervised Depop publish (2026-10-07, WO32b; it is live):** after the one Post click the tab
  goes to `/products/<slug>/manage/` — the seller's view of the new listing (Edit listing, Boost listing, Copy listing;
  "Listed 0 minutes ago"); the listing's own address is `/products/<slug>/` (`listing_url` takes both, the address is
  the bare one; `post/depop.py:listing_address`, `db.listing_id_from`). The page's JSON-LD
  (`script[data-testid=meta__jsonLd]`, schema.org Product) carries our description (the title its first line), the price
  (`offers.price`) and one image per photo; the price shows as `p[aria-description=Price]`, "Size … • … condition •
  <brand>" as `[data-testid=productPrimaryAttributes]`; Depop's own h1 is a name it makes up (brand, department,
  colour, category) — never compared. The shop link `a[aria-label$="'s shop"]` → `/<shop>/` (the shop page the check
  after an unrecognised landing opens: `marketplaces.depop.shop`, set in `private/settings.yaml`; the slug's first word
  is not the shop's name). Depop's `after_publish` is verified: its unattended loop needs only the owner's
  `marketplaces.depop.autopublish: true`. Live misses fixed: the `/manage/` landing read as "not a listing page", and
  the shop check had no shop to open.
- **Recorded from the first supervised Vinted publish (2026-10-08, WO33; it is live):** after the one Upload click the
  tab lands on the seller's member page `/member/<id>?promo_shown=true` (`landing_url`) with an **"Item listed"**
  dialog — the item card, "List another" `[data-testid=list-promotion-submit-cta]` and "Later"
  `[data-testid=list-promotion-cancel-cta]`; the page under it shows Bump buttons `[data-testid=bump-button]`. The
  extension presses **Later** at once (its id AND its text — `listed_later`), never List another or Bump
  (`never_click`), and the driver's shop check finds the new listing among the member page's items (tiles
  `a[href='/items/<id>']` whose `title` reads "<title>, brand: …, condition: …, size: …, $35.00"; `shop_links`
  verified). Vinted's `after_publish` is verified: its unattended loop needs only the owner's
  `marketplaces.vinted.autopublish: true`. Before this the landing waited 60 s as an "unknown page".
- **Recorded by the take-down probes (2026-10-08, WO33, `thrift delist --verify`, nothing clicked):** Poshmark's edit
  page `/edit-listing/<id>` — "Availability *": `[data-et-name=listingEditorAvailabilitySection] [data-test=dropdown]`
  (items "available,not_for_sale"), options `a.dropdown__link[data-et-on-name=availability][data-et-name=available |
  not_for_sale]`, `button[data-et-name=update]` (Delete Listing `a[data-et-name=delete]` never touched); after Update
  the public listing is read until it is no longer for sale. Vinted's listing page: Hide
  `button[data-testid=item-hide-button]` (next to Mark as sold, Mark as reserved, Edit listing, Delete
  `item-delete-button` — never); a take-down counts only when Hide has gone afterwards. Depop's listing page has NO
  Mark as sold — Edit (to the edit page), Boost listing, Copy listing, Delete listing (never): `mark_sold` stays
  UNVERIFIED (the edit page to probe next), so a Depop take-down asks the owner.
- Seen in Chromium (WO32 tests): `chrome.tabs.captureVisibleTab` needs `<all_urls>` or `activeTab`; an unpacked
  extension's `chrome.alarms` may tick every 3 s (`poll_minutes` 0.05); a tab opened by the extension can escape a
  Playwright route on its first request — the tests make every host but 127.0.0.1 unresolvable.

## Seller data (private)
Seller-specific data — brand price tiers, real listing examples, sales findings, account notes, the closet username —
lives in `private/`, a **separate private repo** cloned into this folder and git-ignored here.
When `private/NOTES.md` exists, read it before changing prompts, pricing, or gate rules.
Without `private/`, the code falls back to `config/*.example.yaml` and `data/style_examples/`.

## Commands
`thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>`
`thrift condition <item> nwt|like_new|good` (the CLI twin of the shoe question's buttons)
`thrift kids <item> girls|boys` (the twin of [Girls] [Boys]) | `thrift redo <batch>` (rebuild a batch's unposted items)
`thrift category <item> "<category › subcategory>"` (WO25, the twin of "Which category?"; "Kids > Matching Sets" for
another department) | `thrift answer <item> "no brand"` (the twin of [No brand])
`thrift edit <item> --title "<exact title>" --brand "<brand>"` (WO27, either or both: the owner's words, kept)
`thrift reprocess <item>` (WO25): an item that waits for the owner, or is ready, goes through the pipeline again IN
PLACE — today's prompts and copy rules, the owner's answers kept (price, condition, Girls/Boys, cover, category, no
brand). It keeps waiting meanwhile (its card stays open) and the card is sent again only when what it shows changed, as
`recover`; an answer that lands meanwhile wins (nothing written, "run it again"). Model calls.
`thrift recover <item|batch> [--recheck]` (WO23): recompute ONLY the cover (the front check, upright), the photo order,
the category and the size of items not on the marketplace — price, approved price, condition and Girls/Boys answers and
the copy stay; a question the new category/size settles goes, an item then waiting only for a price it has is ready.
WO24: the front check stored with the item is kept (and the cover's turn while the cover is the same photo), so a second
run changes nothing — live, a second look swapped a skirt's cover between two look-alike sides on every run; `--recheck`
asks both checks again (model calls; for an item processed before WO23, with none stored, they are asked anyway). The
item's open card is sent again only when what it shows changed (text, price, cover picture: `approve.card` + the
cover's hash, before vs after); otherwise it stays as the owner has it. The line printed says which ("card sent again"
/ "card changed (goes out when its turn comes)" / "card unchanged (not sent again)").
`thrift requeue <item> [marketplace] | requeue <batch> | mark-posted <item> <marketplace> <url> | status | show <item>`
`thrift crosslist <item> | crosslist --dry-run <item> | crosslist --backfill [--dry-run] | crosslist --check-login`
(WO30, WO32: see "Cross-listing" and "The extension driver")
`thrift crosslist --hand-listed` (WO33, the poster stopped): her Depop and Vinted shops read through the extension,
each of our items scored against her listings (`handlisted.py`: brand — a near spelling counts —, the item's words, the
size, the price), the numbered list (sure and unsure) to the ops chat, nothing recorded; `--hand-listed --apply 1,2`
records the numbers the owner confirmed (the row `posted` with her listing's address: the backfill skips it, a sale
elsewhere takes it down).
`thrift catalogs check | catalogs refresh [--depop] [--vinted]`; `--marketplace poshmark|depop|vinted` on
`poster --publish-first`, `retry`, `mark-posted` and `requeue`
`thrift retry <item> [marketplace]` (WO28): an "unconfirmed publish" the owner checked and is NOT on the marketplace goes
back in line (the twin of the reply 'retry'); `thrift status` shows the poster's state and the window's status message.
`thrift poster [--once] [--dry-run] [--stage form|review] [--publish-first <item>] [--allow-dev-browser]`
`thrift login --site poshmark | telegram setup|test [--ops] | harvest | build-style | eval`
`thrift requeue b_…` sends a failed batch back to the worker (failed batches are never retried on their own);
`thrift status` lists the open batches (waiting for the worker, the contact sheet, or failed with their error) and the
Telegram queue (the open question, what comes next).
`thrift redo <batch>` rebuilds a split batch's items from the grouping already confirmed (no new contact sheet): every
item that never reached the site goes back to `new` — its price and approved price, dry-run post rows and Telegram
messages dropped; the owner's condition and Girls/Boys answers kept — and the worker processes it again; new cards
follow, one at a time. Left alone, and listed: posting / posted / drafted, a post with a URL or an unconfirmed publish,
and an item the owner dropped as a re-share. Refuses a batch with nothing to rebuild.
`thrift requeue` also takes back an item a poster from before WO27 parked in `needs_owner` as it is (no reprocessing,
the question closed), and an item the poster skipped (post `failed` "skipped: …") once the listing is fixed.
`thrift edit <item> [--title "<exact title>"] [--brand "<brand>"]` (WO27): the owner's own words, kept through any
reprocessing (`items.owner_title`, `items.owner_brand`), no model call, the price kept; another spelling of the same
brand ("J.Crew" → "J. Crew") is learned for the poster. `thrift recover <item> --relabel` reads the labels again (one
call) and replaces the feature lines the old reading gave. `confirm`, `answer` and `price` are the CLI twins of the Telegram replies
(`confirm <batch> "<fix>"` also answers a [Wrong photos] sheet);
`telegram setup` prints the chat/user ids seen in recent updates, `telegram test` sends a test message.
`poster --publish-first <item>` is the supervised first publish: on the Mac only (poster service stopped), one
`ready` item at its owner-approved price, `poster.dry_run` ignored for this one call; a listing whose stored text
predates the condition rule is refused (`thrift answer <item> "recheck"` reprocesses it, price kept). It fills,
reads back and diffs,
checks the Share Listing panel (our title, Promote My Closet off), asks "Type LIST to publish" in the terminal, presses
List This Item exactly once, records everything after the click (`<shot>-after-list.png/.html/.json`), finds the
listing's address (the closet Poshmark lands on, reloaded until the listing shows — see "What's verified"), checks
the live page (title, price) and records the post `posted` with the URL. Anything unrecognized after the click: never
clicked again, `failed` with the evidence, a Telegram ping. Never retried automatically.
The poster loop (`thrift poster`) publishes only with `poster.dry_run: false` AND `poster.autopublish_confirmed:
true` (both off by default) AND `marketplaces.<mp>.autopublish: true`. It takes `ready` items whose listing carries the
owner-approved price (`runner.approved`; WO27), the oldest first, one at a time, inside `schedule.hours`,
`per_hour_max` / `daily_cap`, with a human `gap_seconds` pause after each (now and then a 2–4× longer break). A
gate-"draft" item (the copy needs one look) never publishes on its own, and Save Draft is UNVERIFIED, so the live loop
leaves it alone — never filled, never failed — and says once "⏸ Not published automatically (<item>) … thrift poster
--publish-first <item>" (`runner.held`). A skipped item never trips the circuit breaker. `mark-posted <item> <marketplace> <url>` records a listing found by hand for
an "unconfirmed publish" row (Mac only, poster service stopped).
`--allow-dev-browser` lets the poster open a browser on the dev machine for selector work; it stays dry-run and
never logs into or touches the shop. `--stage` overrides `poster.dry_run_stage` for one run: `form` (fill, read back,
evidence, Discard) or `review` (also Next, record the page after it to `failed/shots/<item>-review.json`, back out,
Discard).

## Retail screenshots
The owner shares retailer screenshots (product page with price, style name, colour) together with the item photos.
- Detected by code: no camera EXIF + phone aspect ratio. Assigned to items by content.
- Used only for `retail_price`, `style_name`, colour and retailer — never for condition, size or flaws
  (the screenshot shows a new item, not this one).
- Always last in the listing, never the cover. Original Price = retail price.

## Copy rules
- **Condition is shown, not told** (owner rule, WO16 — see invariant 2): no wear or flaw words anywhere; a used item's
  condition line is "Gently pre-loved, please see photos for condition."; NWT/NWOT say "New with tags." / "New without
  tags." ("New in box." for NWT shoes whose box is in the photos, `box_photo`); flaw photos are in the listing, never
  the cover.
- **A cutoff's frayed / raw hem is its style** (WO27): "frayed hem", "raw hem", "distressed edges" are allowed on
  cutoff shorts, jean shorts and jeans only (`copy.cutoff`, `copy.STYLE_HEMS`: the condition rule, lint and the poster's
  check mask them there); anywhere else they are wear words.
- **Materials need evidence.** `item_type`/`features` may not name a material (leather, suede, wool, …) unless
  `facts.material` has label/stamp evidence; texture words (woven, quilted, ribbed, glitter) are fine. The verifier
  treats material words as claims; lint flags any material word in title/description/tags that `facts.material`
  doesn't support — except "denim" on a visibly denim item (jeans, jean shorts, a jean or denim jacket; WO27,
  `verify.denim_item`).
- **Titles: the brand FIRST** (owner rule, WO27; `copy.title_order`, run inside `premium.title_with_feature` on every
  title): brand → the one premium feature → item → colour → US size ("J. Crew 100% Merino Wide Leg Sweater Pants
  Blue size M", never "100% Merino New J.Crew …"). "New" only for NWT / NWOT, right after the brand; lint flags
  "New" anywhere in a used item's title (the brand's own "New" — New Balance — aside). The owner's own title
  (`thrift edit --title`) is kept as it is.
- **A set goes under its bottom** (owner rule, WO27; `pipeline.settle_set`, code, at confidence 1.0, so "Which
  category?" never comes for a set): pants → Pants & Jumpsuits (flared → Boot Cut & Flare, wide → Wide Leg, joggers,
  leggings…), skirt → Skirts › Skirt Sets, shorts → Shorts; Kids → Matching Sets. Pajama, swim and lingerie sets are
  left to the model; the owner's category wins.
- **The verifier's edits** (WO27): `copy.changed_fields` compares word by word — case, punctuation, emoji, hashtags,
  line breaks and the Depop tag line aside — and words it only took out are no rewrite (live: "verifier rewrote
  depop_description without reporting a claim" for an emoji or a wear phrase it stripped, six items).
- **Style tags are Poshmark's own.** Only the 130 curated tags its form offers (`data/poshmark_taxonomy.yaml:
  style_tags`, recorded 2026-09-30), spelled Poshmark's way, at most 3. The copy prompt lists them; `fit_style_tags`
  drops anything else before lint, including a material tag (Leather, Suede, Wool, …) that `facts.material` doesn't
  back — tags are optional, so an unusable one is left out, never a question or a draft.
- **Two-piece sets (WO24, WO25):** two garments sold together are a "2-Piece Set" in the title ("… Corset Top & Bubble
  Skirt 2-Piece Set size M"). The extraction reads `set_pieces` (2 or 3; never a bikini, shoes or jewelry) and
  `copy.ensure_set_title` puts "<n>-Piece Set" in when the copy left it out (replacing "Set", else before the size; never
  past 80 characters). Their category is the bottom's (see Categories), never Dresses.
- **No brand (WO25):** the brand question's [No brand] (or a reply "no brand" / "unbranded", or `thrift answer <item>
  "no brand"`) is `items.owner_brand` = "none": the brand source owner (the gate asks no more), Poshmark's brand field
  left empty, the copy names no brand (prompt rule; `verify.lint` flags a brand of the price table in a listing whose
  facts have none). The card stays open for its price; a listing that named the model's unsure guess is rewritten first
  (reprocessed). "no brand, 25" sets the price too.
- **Premium details (WO26, owner rule):** stated exactly, ONLY what a label or the photos show — see "Premium details".
  The retail price line is "Original retail $128." (was "Retail $128."), from a retailer screenshot, a seller note or
  a price printed on a hang tag in the seller's own photo.
- **Line breaks are line breaks (WO24):** a break the model writes as the two characters backslash + n (live: the
  verifier's rewrite, in two listings, which also counted as "rewrote without reporting a claim") is made a real one by
  `copy.clean` / `copy.unescape_breaks`, ignored by `changed_fields`, and mended in stored listings by `relist`
  (`thrift recover`).
- **The cover is the FRONT of the item (owner rule, absolute — WO20, WO23):** the item alone, its front, flat lay or on
  a hanger, clean background — never the back, never worn / try-on / mirror, never a label, tag, flaw close-up, box or
  screenshot. A garment photographed sideways or upside down is still a valid front flat lay; a front that shows a
  small flaw stays the cover (WO23: that rule had made the live Lacoste tee's plain back the cover).
  1. The extraction names every photo's role (`facts.photo_roles`: front, back, side, detail, label, tag, flaw, worn,
     box, other).
  2. **The front check** (`brain/cover.py`, `models.cover` = Opus 5.5 — live, the skirt 3/3 right vs Sonnet 2/3; one call): the item-alone photos (front/side/back), side by
     side in shooting order at `images.cover_check_long_edge`, with the try-on / mirror photos as a labelled reference
     for how the front looks when worn (never a candidate), ONE question — which shows the front? — with per photo the
     side, the printed design (none/some/strong) and where the item's top lies. Cues: print, graphic, text, logo,
     buttons, pockets, the lower neckline = front; a fly zip = front, but a zip down the middle of a skirt or dress =
     back (live: a floral skirt); a plain side when another photo shows a print = back; pants: back pockets / yoke =
     back; shoes: the side profile is a front; equal → more printed design. Stored in `items.views`.
  3. **Code check** (`pipeline.choose_cover`): a photo called the back is never the cover while another candidate
     isn't; a plain photo never while another candidate (not called the back — jeans carry a logo patch there) shows
     printed design. "cover: no front flat-lay photo" only when no item-alone photo could be the front (backs only, or
     none). The decision is `facts.cover_photo` + `facts.cover_upright`; `photo_order` trusts it (the card, the renders
     and the poster all use it).
  4. **Upright** (`cover.upright_check`, `models.upright` = Sonnet 5, one small call): the cover photo shown turned four ways, the model picks the
     upright picture — steadier than naming where a collar lies, which varied from run to run live (probed: the same
     answer twice on all ten live items). The cover is turned by that exact quarter turn before the 3:4 padding
     (`prep.portrait_cover`), never cropped; if the call fails, the comparison's reading. EXIF orientation is applied
     to every photo at prep (`prep.normalize`), before any model sees it.
  5. **"cover N"** — a reply to the card, or typed while it is open (no button): photo N of the item (its photos in
     shooting order, from 0) becomes the cover (`items.owner_cover`, kept through reprocessing) and the card is sent
     again. Never for an item on the marketplace.

  Facts from before WO20 (no roles) keep the model's pick and the old rule (no flaw photo as cover).
- **The cover is 3:4 portrait** (`images.cover_size: [1200, 1600]`), padded with the photo's own edge colour, never
  cropped: the frame of Poshmark's cover dialog, so its default crop takes the whole picture. The re-share check keeps
  hashing the square cover the photo would have made (`prep.cover_hash`), so it compares with older items bit for
  bit.
- **Sizes in the title are US only, never EU.** Adults: "size 7.5". Kids shoes: "Toddler size 7.5" /
  "Little Kid size 13" / "Big Kid size 4", never a bare "size 7.5" for kids. The groups are Poshmark's (owner decision,
  WO10): **Toddler up to 12C** (0–7C included — the form's Baby tab, but "Toddler" is what buyers search), **Little Kid
  12.5–13.5C and 1–3Y**, **Big Kid 3.5–7Y** (`brain/sizes.py`), for the title, the description label and `Render.size`
  alike. The EU size and the full label "EU 24 / US Toddler 7.5" go in the description; `Render.size` keeps the full
  label for the form. Lint accepts the US-only forms ("size 7.5", "US 7.5", "(US 7.5)", "Toddler size 7.5",
  "US Toddler 7.5"), never requires EU, flags any EU or non-US size token in the title, and flags a kids group word
  that isn't Poshmark's for that size (a brand chart's "Little Kid" on 11C).
- **On Poshmark's form** a kids shoe goes on the Girls or Boys tab (`facts.kids_gender` with
  `kids_gender_confidence`: at 0.70 or more used silently; below, or unisex / unread, the owner is asked "Girls or
  Boys?" [Girls] [Boys] before the price card — `items.owner_kids_gender`, never asked again) as Poshmark's own label
  (`KIDS_SIZE_OPTIONS`, verified): Toddler 7.5–12, Little 12.5–13.5 and 1–3, Big 3.5–7, e.g. "7.5 (Toddler Girl)";
  0–7 C sits on the Baby tab (labels UNVERIFIED). The title names the same group, except 0–7C ("Toddler size 5",
  form Baby tab). The copy never states kids_gender.

## Telegram (M3)
- The agent owns the bot: long polling (`getUpdates`) on the worker's own Telegram thread (`thrift run`; its own DB
  connection, so answers are handled at once while the main thread processes), one consumer, offset persisted in
  SQLite (`kv`). Only `TELEGRAM_CHAT_ID` and senders in `TELEGRAM_ALLOWED_USER_IDS` (`.env`, comma-separated) are
  accepted; everything else is ignored. Replies and buttons work with BotFather privacy mode ON; a number typed
  without a reply reaches the bot only with privacy mode OFF (then remove and re-add the bot) or the bot an admin.
- **ONE message at a time (owner rule, WO20).** Everything that waits for an answer — contact sheet, "Brand new or
  worn?", "Girls or Boys?", "Which category?", price card — is one queue read from the DB (`approve.queue`): the
  oldest batch first, its contact sheet when one is asked, then its items in photo order (each item's questions, then its card), then
  the next batch. At most one message is open; `approve.pump` sends the next when it is answered (a send lock in
  `kv` keeps the worker's threads, the poster and a CLI command from both sending). The worker processes in the
  same order and runs ahead, so the next card is usually ready at once; the queue never skips an item still being
  processed (an item sent back by an answer is processed first; an answer that lands while its item is being
  processed wins: that result is dropped and the item processed again). After an answer: no message (WO29) — the
  answered card's buttons become ONE inert button with the answer (`approve._ack`, `editMessageReplyMarkup`: "✓ $35 —
  queued", "✓ Girls", "✓ Like New (brand new, no tags)", "✓ cover: photo 2", "✓ brand: J. Crew"; callback `noop`), then
  the next message. [Later] puts the item behind everything queued so far (`items.deferred_at`). The queue is the DB: a
  restart re-sends only the open message; older unanswered copies are closed (their buttons still work). Info-only
  messages are not queued; a successful dry-run says nothing unless `poster.notify_dry_runs`.
- **A quiet group (owner rule, WO29).** The GROUP (`TELEGRAM_CHAT_ID`) gets only: (a) the cards and the allowed
  questions; (b) ONE line per item, ONCE — the first time its listings go out, when its marketplaces are done (WO30,
  WO32b, `crosslist.announce`): "Posted ✓ <title> — $X · Poshmark <url> · Depop <url> · Vinted <url>" (+ " — check: …"
  when a poster guessed; a marketplace that failed or was skipped is left out); while one of its sites is unconfirmed
  the ⚠️ question is the item's only group message (the line waits for the answer); a site added to an item already
  announced (a supervised publish, the backfill, a retry, a 'posted <url>' reply) is an ops line only, "Added: <title> ·
  Depop <url>" (the items live before WO32b counted as announced once: kv `announced_migrated`), and on the window's
  LAST one (`daily.all_done`: nothing to process, no card waiting,
  nothing left to publish on ANY marketplace) a second line "✓ All done — safe to close the Mac."; (c) action-needed
  alerts, one plain sentence each, once per episode: "Depop needs you to log in on the Mac." / "Vinted asks for a
  check — open it on the Mac." (WO30); 🔋 battery low; the
  Mac slept while publishing / "I pressed List but can't see it" (reply `posted <url>` / `retry`); "Can't read the
  iCloud inbox" (past 10 min); "⏸ Not published automatically: <title> — its text needs a look first."; "⏭ <title> was
  skipped: Poshmark's form doesn't take one of its details." — plus plain replies to the owner's own messages ("No
  price card is open", the hints, "Sorry, that didn't go through — please try again."). No item or batch ids, no
  errno, no traceback. Everything else goes to the **ops chat** (`notify.ops`/`notify.say`): the status message
  (WO28; also in `thrift status`), "Back online", "Poster started (LIVE)", dry-run notes, CLI echoes, "batch …
  rebuilt", the details of a hold or a skip, every ❌ error. The ops chat is the owner's private chat with the bot:
  `TELEGRAM_OPS_CHAT_ID` (`.env`), else `telegram.ops_chat_id` (set in `private/settings.yaml`), never the group's id;
  it works once the owner has sent the bot /start. A send there that fails is logged (event `ops_chat_down`), dropped and
  not tried for 10 min (kv `ops_down_until`) — **never sent to the group instead**. The same text again: once, then
  at most once a day (kv `ops_sent`, ids ignored), except the status message (edited in place) and the CLI echoes.
  On the dev machine both print (`[notify]` / `[ops]`). `notify.group*` is called only for (a)-(c).
- Batch (WO20b): the grouping is accepted at once (`segmentation.auto_confirm: true`) — no contact sheet, the
  batch's first message is its first item. The sheet is still drawn (`work/<batch>/contact_sheet.png`); the code's
  doubts stay in `batches.reasons` and `thrift status` ("grouping accepted with doubts"); a retail screenshot that
  matches no item is left out (and recorded). A grouping that isn't a partition still sends the sheet. **[Wrong
  photos]** on a price card (`pipeline.start_regroup`): the batch goes to `regroup`, the sheet as its items are now
  (`regroup_sheet.png`) is the one open message (outbox kind `regroup`), its items wait unasked, unprocessed and
  unposted; the reply `12>2 | split 7 | merge 2 3 | drop 7 | ok` (or `thrift confirm <batch> "<fix>"`) is applied by
  `pipeline.regroup`: unchanged items keep everything, changed ones are rebuilt like `thrift redo` (same id, paired
  by the most shared photos), new groups become items, emptied ones are removed; refused when it would change an
  item on the marketplace. `auto_confirm: false` is the old flow: the sheet + summary, reply `ok | 12>2 | …` (same
  parser as `thrift confirm`), the sheet marks the photo after each pause ("pause 2 min"), the message lists them and
  the doubts; `segmentation.always_confirm` then decides whether a clean split also waits.
- Condition question (WO18, shoes in doubt only): ONE message in `awaiting_condition`, before the price card — cover,
  title, "Brand new or worn? (couldn't tell from the photos)", [NWT] [Like New] [Good]; a typed reply works too (nwt /
  like new / good). Outbox kind `condition`, re-sent while pending; the tap reprocesses the item and the price card
  follows ("Condition: NWT (your answer)").
- The price card, in Poshmark's words (WO20): cover, title, the size as Poshmark's size menu shows it ("7.5 (Toddler
  Girl)"), the condition as Poshmark's label (NWT / Like New / Good — excellent is Like New), a warning only where a
  look is needed (the cover, a well-worn pair, a flaw no photo shows) and only the allowed questions; no flaw count,
  no basis, no retail. Buttons (WO32b, the owner's request — mostly up): eight prices in two rows of four, $10 below
  the suggestion, the suggestion marked ⭐, six $10 steps above it, the steps from the suggestion ($35: $25 · ⭐$35 · $45
  · $55 / $65 · $75 · $85 · $95); no low button when it would be under $5 (then seven steps up;
  `approve.price_options`) — a button is the owner's deliberate tap, so the floor rule below is the typed number's
  only; [Later] [Change]. A number typed on its own (`28`,
  `$28`, `28.00`) prices the open card (under the floor it is not taken: reply to the card for that); a reply with a
  number works as before and sets the price (source `owner`) → `ready`. Unreadable brand/size (< 0.70), a
  department/category Poshmark doesn't have, or a suspected re-share is a question on the card; a reply like `size 8,
  45` stores the price and reprocesses with the note; it comes back only if still unresolved (price kept). Two
  questions are never settled by a price alone: NWT without a hang-tag photo is listed as Like New (the card asks)
  unless the reply says `NWT`; a re-share hold needs `different item` (list it) or `same item` (drop it).
- **The poster never asks (WO27).** Brand: ours as Poshmark spells it (`brands.Aliases`: `data/brand_aliases.yaml`,
  written by the poster on the Mac, git-ignored; seed "J.Crew" → "J. Crew"), typed (then its longest word if the list
  offers nothing), then `brands.pick`: the same name normalised (case, spaces, dots, hyphens, apostrophes, & = and),
  else the name without a qualifier ours lacks (Factory, Outlet, Kids, Baby, Home, Collection, Sport… — never "J. Crew
  Factory" for J.Crew), else the longest name all of whose words are ours ("Zara" for "Zara Basic"), else the most
  similar (≥ 0.85), else Brand left empty (it is optional) — each resolved name learned. Size: the menu's value, else
  the nearest of the same system, one size away at most (`post/fallback.nearest_size`: 8.5 → 9, XL → L, never a
  Petite or a kids label for a plain size). Category: the closest (≥ 0.75), else the item is skipped; subcategory: the
  closest (≥ 0.6), else None; colours and style tags not offered are left out. Every guess goes in the "Posted ✓
  <title> — check: brand set to 'J. Crew' (from 'J.Crew')" message (none → no extra words) and the event log. A
  required field nothing comes close to: `skipped` — nothing saved, the post `failed` with "skipped: …", one Telegram
  line, `thrift requeue <item>` once the listing is fixed. The outbox kind `owner_q` is no longer sent; a reply to an
  old one still works (to a brand question, the reply is the brand).
- **A plain reply is the answer to THAT question** (WO27): to a card that asks the brand, "J. Crew" is the brand
  (`approve.brand_reply` → `pipeline.set_brand`: no model call; the old name replaced in the copy, the title brand-first,
  the card sent again when it changed); "brand X" works on any card; a size, NWT, a number… keep their meaning.
- Re-send: once the open message — only that one — has waited longer than `telegram.resend_after_hours` (default 6),
  it is sent again; checked when the worker starts and about hourly (Telegram keeps updates 24 h; the Mac sleeps). A
  restart — every deploy — never repeats a card sent less than that ago (WO24: it used to, at every start; one skirt's
  card went out 13 times in a day between deploys and `thrift recover` runs).
- Settings: `telegram.enabled`, `telegram.resend_after_hours`, `telegram.poll_timeout`, `telegram.ops_chat_id`. On
  prod, `thrift run` and `thrift poster` refuse to start when `telegram.enabled` but any of the three env vars is
  missing (the ops chat is optional: without one the technical messages are only logged).

## Milestones
- **M0 eval** — 20–30 real sessions in `eval/fixtures/<case>/{photos,expected.yaml}` (mostly shoes, some with
  retail screenshots); `thrift eval`. The ~100 harvested sold listings are an extra set for brand/size/category
  only — not condition, historic labels are unreliable.
  Targets before autopublish: segmentation ≥95%, brand/size ≥95%, condition ≥90%.
- **M1 prompt tuning** — tune the segment/extract/copy/verify prompts against M0.
  Done (WO17): segmentation on `claude-opus-5-5` with 768 px previews (384 px fallback above `max_request_mb` or on a
  413), a visual-identity-first prompt, pauses relative to the roll (> max(30 s, 4 × the median gap); screenshots out
  of the timing), and the code's doubt check (a pause AND a colour change inside an item, neither between two). The
  batch record keeps the pauses, colour distances and changes: tune `segmentation.pause_*` / `visual_change_*` on
  real rolls (M0).
- **M2 Poshmark poster — in progress.** Done (WO9): the create-listing structure verified 2026-09-29 is in `SEL`; fill
  order photos → title → description → category → subcategory → size → condition → brand → colors → tags → price dialog
  → SKU; `read_back` of every field incl. the dropdowns' text; dry-run stages `form` | `review` leaving through
  Discard; curated style tags only; Smart Sell asserted off; `data/poshmark_taxonomy.yaml` + `taxonomy.fit()`;
  `kids_gender` → the Girls/Boys size tab; a static-HTML fixture test of the form (`tests/test_poshmark_form.py`).
  Done (WO11, from the first Mac dry-run's snapshot): the cover dialog is recorded and applied; condition picked by
  code; curated tags, the SKU behind "show details", Cancel → "Save Draft" dialog → "Discard Changes" recorded.
  Done (WO12): 3:4 cover; style tags only from the 130 curated; the Drafts count checked after every dry-run
  (the create page reopened in a fresh tab; `[data-et-name=draftsSection]`).
  Done (WO13–WO14): a size menu that closes itself; an empty Original Price reading back "0".
  Done (WO15): the Share Listing panel after Next recorded; the review back-out fixed (‹ Back → Cancel → Discard);
  `thrift poster --publish-first <item>`, the supervised first publish.
  Done (WO16): the first live listing (supervised); `listing_url` pinned from its evidence; the closet polled after
  List (`created_listing_id`, title AND slug, ≤ 90 s); `thrift mark-posted`; the owner's condition-wording rule;
  `poster.autopublish_confirmed` as the second key for the poster loop.
  Done (WO24): every department's categories and subcategories from the form's catalog; a subcategory given as the
  category put under its category.
  Done (WO27): the poster never stops to ask — Poshmark's brand spelling learned, the nearest size, the closest
  (sub)category, colours / tags left out, each guess listed in "Posted ✓"; a required field it can't fill skips the
  item; the loop takes only owner-approved prices and holds draft-gated items; `thrift edit`.
  Left: where Save Draft lands (`draft_saved`); the Promote toggle's markup.
  LIVE since 2026-10-05 (the owner's three keys). Done (WO28): the daily window — no listing started with the lid
  closed or on a low battery, the Mac kept awake while listings remain, the closet checked after a sleep, a stale
  `posting` row reconciled at start, the owner's 'posted <url>' / 'retry' replies.
- **M3 Telegram approval flow — done (v1):** long polling in the worker, one approval message per item
  ([Approve $P] [Change], questions folded in), re-send of pending
  messages after sleep. Nothing left code-wise; the owner creates the bot with @BotFather and fills
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` in `.env` (`thrift telegram setup|test`).
  Done (WO20, after the first live test with two batches): one message at a time across batches and items, one-tap
  prices, Later, a typed number, always a price, the cover = the front of the item, the card in Poshmark's words,
  Girls/Boys asked below 0.70, a quieter contact sheet, `thrift redo <batch>`.
- **M4 Airtable + n8n + My Sales sync** — business view, sale email → sold + delist elsewhere, shipping watchdog
  (`HOLD_UNSHIPPED`), local HTTP API over Tailscale, daily read-only order-status sync.
- **M5 Depop** — WO30: Depop and Vinted cross-listing built (catalogs, mapping, posters, the loop); their forms'
  selectors are recorded from the Mac's dry runs, then the owner's supervised `--publish-first` per marketplace.
  WO32: both posted from the Thrift Chrome through the extension (the driven browser is turned away); the extension's
  selectors start UNVERIFIED and are recorded from its first dry run; `delist` ready for WO31; Depop's API a stub.
- **M6 sold-comps pricing** — tune `private/brand_tiers.yaml` from new sales.

## The daily window (WO28)
The Mac is mostly closed (asleep). About once a day the owner opens it, logged in, often on battery, for ~30 minutes;
photos are shared from the iPhone any time, prices approved in Telegram, then the lid closes. The owner's guide:
`docs/DAILY.md`. Nothing here changes the publish-safety rules or the two-key autopublish rule.
- **Wake** (`power.WakeWatch`: `kern.waketime` moved, or the wall clock ran 90 s longer than the process — a gap in the
  tick clock) or worker start, **with the lid open** (`power.lid_closed`, ioreg's AppleClamshellState): a new window
  (`daily.Window.step`/`begin`) — the inbox looked at, "Back online — N new shares, M items waiting" (ops chat, WO29)
  only when there is work, the open card re-sent only if older than `telegram.resend_after_hours` (`approve.resend_pending`; answers
  given while asleep, < 24 h, arrive in the next poll in order — the offset is persisted; older ones are lost and the
  card simply comes again). With the lid closed (the night's maintenance wakes) the worker starts nothing, says
  nothing, and the poster starts no listing; the Telegram thread still applies answers.
- **The status message** (`daily.status_text`, `Window.show`) — in the **ops chat** and `thrift status` since WO29;
  the group gets "✓ All done — safe to close the Mac." only as the last "Posted ✓"'s second line: ONE per window, edited
  (`Bot.edit_message`) when its
  text changes, sent anew only if it can't be edited; a window where nothing happened sends none; a new window deletes
  the last window's message (`Bot.delete_message`; Telegram allows it for 48 h). "⏳ Working — 4
  items left, about 12 min. Please don't close the Mac yet." (processing; approved listings the poster will publish
  count too) / "⏳ 3 listings still to publish, next in ~4 min. …" / "✓ All done — safe to close the Mac." / "✓ Safe
  to close — 2 cards are waiting for your answer in Telegram (answers within 24 h are kept)" / "✓ Safe to close — 3
  listings will go up after 08:00 next time the Mac is open" (outside `schedule.hours`, or the daily cap) / "… wait:
  <PAUSE, unshipped hold, battery low>". The estimate is the median of the last runs (`daily.timings`): the worker's
  `worked` events (seconds per batch / item, monotonic: sleep excluded), the poster's `post_posted` seconds, and the
  real gaps between listings. The poster's own state is a heartbeat in kv (`poster_state`: live, busy, next_at,
  paused; stale after 3 min = not running).
- **Sleep while working.** A batch or item whose model call is cut off by the lid closing stays `new` and is taken
  again (`cli._guard`, event `interrupted_by_sleep`) — never failed for it. A listing the Mac slept through: before
  its final click nothing was submitted, it goes back to `queued` (≤ 3 attempts, `SLEPT`), never a failure for the
  circuit breaker; after it without an address, the closet is looked at once more (`PoshmarkPoster.find_live`: the
  created_listing_id, else exactly this title — tile and slug — created since the listing started; several → never
  a guess) — found: posted with its URL and the normal "Posted ✓"; not found: unconfirmed and the ONE ⚠️ message. A
  row still `posting` when the poster starts (the Mac slept for good, shut down) gets the same closet check
  (`runner.reconcile_stale`; the Chrome profile lock means no other poster can be mid-listing).
- **Battery** (`power.Awake`: `caffeinate -i -w <pid>`, on battery too): the worker holds it while it has processing to
  do, the poster while it has listings to publish (through the human pause only when one follows); released when the
  work is done, so the Mac sleeps normally. Below 15% on battery with work left: ONE line "🔋 Mac battery low — plug
  in or I'll pause; nothing will be lost"; the listing in hand is finished, none started until charging or ≥ 20%
  (`power.publish_paused`, kv `battery_low_since`, shared by both processes).
- **iCloud** (`prep.icloud_placeholders`): a share is ready only when every file is really downloaded — the old
  `.Name.icloud` placeholders and current macOS's dataless files (`SF_DATALESS`) are asked for (`brctl download`, the
  folder and each file) — and `_done` is there; past 5 minutes, ONE line "Waiting for iCloud to finish downloading
  the photos…" per share (kv `icloud_waiting`).

## Cross-listing on Depop and Vinted (WO30)
Every item live on Poshmark goes on Depop, then Vinted — the same approved price (no markup), no extra questions.
- **Catalogs** (`thrift_agent/catalogs/`, read-only reference data): `data/depop_catalog.json` (322 categories with
  their attributes, the value lists, the 13 US size sets and the product type → size set map, the form's combobox ids),
  `data/vinted_catalog.json` (573 leaves id → path → fields with required flags and option libraries, 29 colours, the
  package sizes), next to `data/poshmark_catalog.json`. Validated when loaded (`catalogs.check()` at the poster's
  start; `thrift catalogs check`): a file that doesn't validate turns only that marketplace off (an ops line).
- **Mapping** (`catalogs/depop.py:map_depop`, `catalogs/vinted.py:map_vinted`; every value one of the catalog's):
  - Category: a deterministic table (`catalogs/categories.py:TABLE`, Poshmark department / category / subcategory →
    Depop path + Vinted leaf; REFINE splits one by the item's words, e.g. romper / jumpsuit; Kids rows name the Girls
    and the Boys leaf — unisex goes on Girls) covering every Poshmark clothing, shoe, bag and accessory subcategory;
    no row → Opus picks from the ENUMERATED leaves of the department (`models.crosslist`; the tool's input is an enum)
    and the answer is cached in `private/category_map_learned.json`.
  - Size (`catalogs/sizes.py`): from Poshmark's menu value. Vinted: the leaf's library — women's numeric 00→XXXS, 0→XXS,
    2→XS, 4/6→S, 8/10→M, 12/14→L, 16/18→XL, 20/22→XXL; women's denim waist → US first (24→00 … 32→14, 33→16, 34→18);
    men's waist `W<n>` (size_9), neck (size_11); shoes the US number; kids size_16; kids shoes size_17 (a half size the
    menu lacks goes up one and is reported). Depop: the size set of the product type (letters stay letters, numbers
    numbers, waists `NN"`, shoes "US 8.5", kids "N years" / "N-M months"). One size / Other only when the item is one
    size; never an invented size — a required size that doesn't fit skips that marketplace only.
  - Condition (never Fair): Vinted NWT→6, NWOT→1, like new / excellent→2 (Very good), good (and a fair reading)→3;
    Depop NWT→Brand new, NWOT / like new / excellent→Like new, good→Used - Good. Colours ≤ 2 by a fixed table
    (Gray→Grey on Depop, Tan→Beige on Vinted, olive→Khaki, teal→Turquoise on Vinted / Green on Depop, navy→Navy).
    Material only from a read label (Vinted ≤ 3, Depop ≤ 4). Vinted's Skirts need a skirt length (the facts, else the
    model with the enum). Depop: Source Preloved (Vintage only with the label's vintage cue), Age Modern unless a
    decade, optional attributes only the facts support (material, dress-length, bottom-style, body-fit). Package size
    from a weight class (X_SMALL only where the Vinted leaf offers it).
  - Brand at fill time: Vinted's brands API / Depop's brand menu, exact or normalised only (`brands.strict_pick`);
    none → empty, said in "Posted ✓ … — check:"; a Vinted brand flagged for authenticity / luxury: listed, ops note.
  - Copy: Depop — the Poshmark title as the first line, a blank line, the Poshmark description, ≤ 5 hashtags (brand,
    item, style, colour, era), ≤ 1000 characters (only the body is trimmed). Vinted — Poshmark's title and
    description. Photos: Depop ≤ 8 (the front cover first, every flaw photo kept), Vinted ≤ 20 (Poshmark's order).
  - The mapped fields are saved to `listings.fields_json` before the form opens; a mapping failure skips only that
    marketplace (status skipped, ops note).
- **Posters** (`post/depop.py`, `post/vinted.py` on `post/cross.py`; WO32: `driver: playwright` — the default is the
  extension, see "The extension driver"): the same browser and profile, a tab each; a dry
  run fills every field (one that won't fill is recorded and the read-back fails it, the whole form kept as evidence)
  and closes the tab — never the publish button; a publish presses it once, records everything after it, takes the
  address of the page it lands on (else the shop's one new listing with this title; several → never a guess), and
  compares the live page with the fields (a difference → ops note, the listing stays up). Every selector is
  UNVERIFIED until a Mac dry run records it: `submit()` publishes only when the publish button is recorded (the
  supervised `--publish-first`); the unattended loop also needs the landing page (AUTOPUBLISH_NEEDS).
- **The loop** (`runner.run`, `crosslist`): per item Poshmark → Depop → Vinted, 5–15 s apart (WO32b; was 30–90 s;
  `crosslist.gap_seconds`), then the human gap; within the hours; `marketplaces.<m>.daily_cap` (25) each, dry runs
  included. Safety switches: both poster keys AND `marketplaces.<m>.autopublish` (default false = dry run: the
  screenshot and the mapped fields to the ops chat). A logged-out / CAPTCHA / verification page stops that marketplace
  for the window (the group's one plain line; kv `crosslist_blocked`); 3 failures in a row too. A failure before the
  publish button: `failed`, retried next window, 3 attempts, then `skipped` with an ops note. Interrupted after it:
  the shop is looked at (WO28's way), never published again blind; else "unconfirmed" and the owner's 'posted <url>' /
  'retry' (outbox ref `<item>:<marketplace>`).
- **`thrift crosslist`**: `<item>` queues one already live on Poshmark; `--dry-run <item>` asks the running poster to
  fill both forms (screenshots + fields to the ops chat, nothing saved); `--backfill` queues every item still for sale
  on Poshmark (its public page checked first), oldest first (`--dry-run`: lists them only).
- **Catalog refresh** (`catalogs/refresh.py`): `thrift catalogs refresh` and once a week in the daily window, the poster
  re-reads the same read-only APIs from its logged-in Chrome (WO32: Playwright-driven marketplaces only); a catalog is rewritten only when the new one validates;
  the diff, and a ❗ line for a table row whose target has gone (that row then goes to the model), go to the ops chat.
  The raw answers are kept in `data/catalog_raw/` (git-ignored).

## The extension driver: Vinted and Depop from the seller's own Chrome (WO32)
Vinted (DataDome) and Depop turn away any browser driven over the DevTools protocol (WO30's poster profile got Depop's
403 page and a logged-out Vinted; a human login inside that window changes nothing). The commercial crosslisters'
answer: a Chrome extension in the seller's own, normally opened Chrome — no DevTools connection, no automation flags,
the real Chrome, the real cookies. `marketplaces.<m>.driver: extension` (the default for both; `playwright` = WO30's
posters, still there; Depop's `api` = a stub). Everything from WO30 stays: catalogs, mapping, `listings`, the queue,
the pacing, the Telegram lines, the dry-run gate. Poshmark is untouched (its Playwright poster, LIVE).
- **The Thrift Chrome** (`deploy/com.thrift.chrome-cross.plist`, LaunchAgent `com.thrift.chrome-cross`): an ordinary
  Google Chrome with its own profile `~/thrift/chrome-cross` and Dock icon, next to the owner's own Chrome and the
  poster's profile: `--user-data-dir=… --no-first-run --no-default-browser-check --restore-last-session
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding https://www.vinted.com/` (the two
  "backgrounding" switches keep it at full speed behind other windows and the lock screen; pages can't see them).
  Never `--remote-debugging-*`, `--enable-automation`, `--load-extension` (a test checks). KeepAlive restarts it after a
  crash only (`SuccessfulExit: false`): a plain KeepAlive would relaunch on every normal quit (Cmd-Q, Chrome's
  relaunch to update) a second copy that just hands its address to the running one, again and again — the bridge
  starts it when needed instead. `services.sh start|stop chrome` (`start` kickstarts one that quit), `status` lists it;
  `mac_deploy.sh` starts it when it isn't running, never restarts it. The owner loads the extension unpacked once
  (`chrome://extensions` → Developer mode → Load unpacked → `~/thrift-agent/ext`), pastes the token, logs in to both
  sites there by hand. The window can sit behind others, never minimized (the screenshots capture the visible tab; a
  job's page reports "the tab is hidden" when it starts in a minimized window).
- **The extension** (`ext/`: Manifest V3, plain JS, no build step, no third-party code): `manifest.json` — permissions
  storage, alarms, tabs, scripting; host permissions the two sites, the bridge, and `<all_urls>` (the owner's choice,
  WO32: `chrome.tabs.captureVisibleTab` refuses without `<all_urls>` or `activeTab` — "Either the '<all_urls>' or
  'activeTab' permission is required", seen in Chromium — and activeTab needs a person's click; the content scripts
  still run on the two sites only, nothing else is opened or read). `background.js` — the WebSocket to
  `ws://127.0.0.1:8765/ext` (the token first; tried again 1 s, 2 s, 5 s after a drop, then every 5 s — WO32b: a bridge
  that starts is seen within ~5 s; 30 s after a refused token; a ping every 20 s keeps the MV3 worker up), a
  `chrome.alarms` tick (`GET /jobs/next` while the socket is down) every 30 s — every 5 s when loaded unpacked, as in
  the Thrift Chrome (Chrome allows it there; live, a worker put to sleep while no bridge ran took 18 s to answer the
  CLI on the 30 s alarm); `poll_minutes` in storage overrides, tests 0.05; one job at a time in its own new tab of the Thrift window, opened as its active tab (the window itself
  never focused or raised; closed after, except a login / block / CAPTCHA page, left for the owner), the page's script
  injected by the worker when it hasn't answered in 2 s (a late document_idle; `common.js` answers once per page), each
  message to the page answered (or refused) within 3 s or asked again, 20 s in all — a page that never answers ends the
  job with the tab's state (load status, address, title, frozen / discarded, its window minimized or not) and a picture
  taken by the worker (a job asked twice runs once), "tab opened" / "page loaded" / "page script running" progress
  events. **The live Vinted stall (WO32b), from the Thrift Chrome's history:** both stalled runs went /items/new →
  (server redirect) `/session-refresh?ref_url=/items/new` → (a client redirect ~1 s later) /items/new; the worker took
  the refresh page for the loaded form, handed it the job, and the job died with that page — silently. Now a sell job's
  tab counts as loaded on the job's own page, or on another one that stays 2.5 s (a login page: the page script names
  it) — a page that moves on is waited through ("passed through /session-refresh"); and a page that navigates during
  the fill gets the job again (twice at most; "the page reloaded … — filling it again"), except a form already waiting
  for POST (ended: nothing published) and a take-down; the photos fetched from the bridge by the worker (the extension's origin: no page CORS,
  no local network prompt) and handed to the page as bytes, `captureVisibleTab` (10 s at most) + the page's HTML on
  request, the landing page watched after the one click (`tabs.onUpdated`, ≤ 60 s), a job ended when its bridge goes
  away before the go-ahead (a Ctrl+C, the poster stopped: the tab closed — nothing can be published from it), or called
  off while its tab was opening (ended before the page has it; a cancel reaching the page first is kept per job id —
  the job's start no longer clears it), when not filled in 4 min, a filled form left 15 min without a go-ahead, 5 min after the click, or its tab closed; the reload check — the bridge's `files_hash` of `EXT_FILES` vs the hash taken when it loaded: a deploy that
  changed `ext/` reloads it (`chrome.runtime.reload()`, between jobs, once per version). `content/common.js` + `vinted.js`
  / `depop.js` (isolated world): text through the native value setter + input / change — WO32b's fast pace (the
  default, `ext.pace: fast | human`): each value set in one go, each wait on the page itself (`T.until` / `waitFor`:
  a MutationObserver, a light poll re-armed from a message task, never a chain of timers that a hidden tab runs once a
  second, then once a minute — the live stall), fixed settles ≤ 150 ms counted in MessageChannel ticks, no pause between
  steps; every step ends the job after 20 s without progress (photos 120 s) with its name and a `stalled-<step>`
  screenshot; step events carry their time (`ms`), the read-back the fill's (`fill_ms`). `human`: typed in chunks with
  40–140 ms pauses, 250–900 ms between steps, 2–5 s before the first. Real clicks (pointer / mouse events, then click)
  on the real options — Depop's Downshift menus `<id>-menu`
  cleared then typed, the size menu read until it shows the size, Vinted's category walker (the leaf by id, else the
  path), brand search (ours or its clean form only, `strictPick`), size, condition, colours ≤ 2, materials ≤ 3, the
  skirt length, the package radio; photos as Files on `input.files`, all in one DataTransfer (fallback: a drop);
  `scrollIntoView`; Depop's Boost unchecked if it is on, Vinted's bump / promote offers closed; Depop's menus waited on
  until they offer the value (an older list may still show), its size menu reopened every 0.8 s while it lags, its
  comboboxes waited for (the size and the attributes come after the category); **Depop's own suggestions** (colours,
  brand, package — they come once the photos are up) waited for, 8 s at most, and the page to settle (`T.quiet`) before
  our picks go over them (live, WO32b: at the fast pace its package "Medium" landed after our "Large"); the tidy step
  sets back a single choice Depop replaced and adds back a value of ours a multi-select lost.
  Pages classified block → captcha → login → verify → listing → form (`selectors.json` pages; jsdom has no innerText:
  the text without scripts). `selectors.json`: site → pages / steps → selector(s) → how verified; every step starts
  UNVERIFIED (`"verified": false`); WO30's live evidence recorded the login pages of both and Depop's block page.
  `options.html`: the token, and the link's state ("connected", "the bridge refused the token", …).
- **The bridge** (`thrift_agent/bridge.py`, in the poster process; stdlib asyncio, a hand-written RFC 6455): only
  127.0.0.1:8765; the token (`bridge.token_file`, `~/thrift/var/ext_token`, made once by `mac_setup.sh`, chmod 600,
  never rotated by a deploy) on every request, the WebSocket from a `chrome-extension://` origin only, a request whose
  Host isn't 127.0.0.1 / localhost refused (DNS rebinding). Jobs one at a time in all (the extension runs one); a job
  called off (the driver gave up) gets ONE `cancel` and the next waits for its end (≤ 90 s, `DRAIN`). An extension
  whose hello names other files than the bridge's (`loaded`: it reloads now — a deploy changed `ext/`) gets no job for
  2 s (`RELOAD_HOLD`, WO32b: a job handed to it then would go down with the reload); `announce_files` holds the same way. `GET /photos/<item>
  /<n>.jpg` (the listing's own prepared photos, cover first) with `Access-Control-Allow-Origin` only for the two sites;
  `GET /health`. Screenshots and HTML kept next to the job's evidence path (`failed/shots/<item>-<mp>-<time>.png` is the
  filled form, `…-after-publish.png`, `…-login.png` …). The watch (`watch_tick`, every 15 s): no extension for 2 min
  with the lid open → `services.sh start chrome` (event `thrift_chrome_started`); 5 min → ONE ops line a window
  (`MISSING_LINE`, kv `ext_missing_said`); kv `ext_link` {connected, at} for `thrift status` and the daily window
  (`crosslist.reachable`: an extension marketplace counts as "still to publish" only while it is connected). No token
  / the port taken → the extension marketplaces off for that run (an ops line), Poshmark goes on.
- **The driver** (`post/ext_driver.py:ExtensionPoster`): WO30's poster interface (`post`, `find_live`, `verify_live`,
  `listing_address`, `available`) + the WO32 names (`check_login`, `dry_run`, `publish`, `delist`). post(): the job
  (§2 below) → `ready` / `result` → the read-back diffed with WO30's plan (`DepopPoster` / `VintedPoster.expected`) → a
  dry run ends (nothing saved) → a publish needs `PUBLISH_NEEDS` {submit} (supervised) / `AUTOPUBLISH_NEEDS` {submit,
  after_publish} verified in `ext/selectors.json`, exactly one Post / Upload button seen, the owner's POST when
  supervised → the row taken (`on_go_ahead`: `db.claim_listing`, `posting` — WO32b; refused → no go-ahead) → ONE
  `submit` → the listing page → its title / price / size / photos compared (the page's own title and price when it
  gives them — Depop's JSON-LD; a difference: an ops note). A form that went away before the go-ahead (the owner
  closed the tab) is never clicked. Progress (`progress` callback, `lines`): "tab opened", "photos 6/6", "category ✓",
  "size ✗ <why>", "filled in 21.3 s" — the CLI prints it, a dry run's ops message carries it. A job given up on (a
  timeout, a Ctrl+C) is cancelled once and its end awaited (`Bridge.settle`, ≤ 5 s: the tab closed). Not taken in 150 s
  (`PICKUP_TIMEOUT`) → PosterError before anything opened; past 3 min (`JOB_TIMEOUT`, WO32b) before the go-ahead →
  failed, nothing submitted; after the click without a listing page → the shop (`find` job on
  `marketplaces.<m>.shop`, else the shop a listing page showed; the one listing with the title's first words;
  several → never a guess) → else unconfirmed. The attempt is counted when the job starts (`db.begin_attempt`).
  The loop takes an extension marketplace's jobs only while the extension is connected (`runner.ready_cross`).
  `delist`: Vinted's Hide / Depop's Mark as sold (never delete), refused until `hide` / `mark_sold` is verified — for
  WO31. `post/depop_api.py:DepopApiPoster` (driver `api`): `product_request` builds the PUT
  `/api/v1/products/{sku}` from `data/depop_catalog.json` (department, group, product type, size set; photos by URL);
  it sends nothing (no `DEPOP_API_KEY` yet), and the poster says once that its listings wait.
- **The job (WO32 §2):** bridge → extension `{"job_id", "site", "mode": check_login | dry_run | publish | verify | find
  | delist, "fields", "copy": {"title", "description"}, "price", "photos": [bridge URLs], "listing_url", "shop",
  "pace": "fast" | "human"}`; extension → bridge `{"event": "step", "name", "ok", "detail", "ms"}`, `{"event":
  "progress", "what"}` ("tab opened", "the tab is hidden …"), `{"event": "screenshot", "label", "png_b64", "html"}`,
  `{"event": "ready", "seen", …}` (publish: the filled form, waiting), `{"event": "result", "url", "live" | "seen" |
  "listings"}`, `{"event": "error", "stage", "message", "page": login | block | captcha | verify | form | unknown}`;
  bridge → extension `{"type": "submit" | "cancel", "job_id"}`. `GET /jobs/next` returns the same job; `POST /events`
  answers with the job's commands.
- **Stops:** a login / CAPTCHA / block / verification page stops that site for the window with ONE plain group line by
  what it showed (`crosslist.ALERTS`, `AccountBlocked.page`): "Vinted needs you to log in on the Mac." / "… shows a
  CAPTCHA — solve it in its Chrome window on the Mac." / "… turned the Mac away for now — I'll try again next time the
  Mac is open." / "… asks for a check — open it on the Mac." The code never solves a CAPTCHA; a person may, there.
- **Around it:** `thrift crosslist --check-login` (the sell pages opened in the Thrift Chrome: an ops line each, or the
  stop); `thrift crosslist --dry-run` without the poster running starts its own bridge; `--publish-first --marketplace
  vinted|depop` runs its bridge (the poster service stopped: the port — a taken port is said at once) and sees the
  extension within ~5 s (WO32b; past 30 s it says why: its token refused, the Thrift Chrome not running, the extension
  never knocking — `Bridge.why_missing`; it gives up at 90 s), prints the progress line by line ("extension connected
  (1.2 s)" … "filled in 21.3 s"), the summary, "Type POST to publish" (read on a daemon thread: a Ctrl+C ends the CLI
  at once); a Ctrl+C before POST cancels the job, the tab closes, the row stays `queued` (event `publish_cancelled`);
  after the go-ahead it is the unconfirmed flow;
  `mark-posted` reads the page in the Thrift Chrome; `thrift login --site vinted|depop` says to log in in the Thrift
  Chrome; the catalog refresh never takes the poster profile to an extension site (WO30's weekly refresh and `thrift
  catalogs refresh` skip it: the saved catalogs stay); `thrift status` prints "extension: connected / not connected".
- **Tests:** `tests/js/content.test.mjs` — jsdom on the stand-in forms (`npm ci --prefix tests/js`; Node ≥ 22.22;
  `tests/test_ext_js.py` runs it, skipped without Node — the Mac has none); `tests/test_ext_integration.py` —
  Playwright's own Chromium with the unpacked extension (`--disable-extensions-except` / `--load-extension`), the real
  bridge on 8765, the stand-ins answered by the router and every other host unresolvable (`--host-resolver-rules`:
  nothing can reach a real site), skipped without `python -m playwright install chromium`: a dry run with a real
  capture, publishes that click once, login / block / CAPTCHA, the timeout called off, a refused token, the alarm's
  poll with the socket down; WO32b: the fill times (fast), the job tab in the background, the extension back within
  ~5 s of a bridge restart, a Ctrl+C or a vanished bridge before POST closing the tab with nothing clicked
  (Playwright keeps every page "visible": Chrome's hidden-tab throttling is the jsdom test's — every timer forced to
  ≥ 1 s, the fill still takes seconds); `tests/test_bridge.py` — the bridge and the driver with a scripted stand-in extension
  (`tests/ext_fake.py`); `tests/test_crosslist_flow.py` — every WO30 loop test runs with both drivers.
- **Owner steps (once, at the Mac):** load `~/thrift-agent/ext` unpacked in the Thrift Chrome (Developer mode; Cancel a
  "Disable developer mode extensions" prompt), paste `cat ~/thrift/var/ext_token` in its options, log in to
  vinted.com and depop.com there (the iPhone hotspot if Vinted still flags the home Wi-Fi), turn both sites on in
  `settings.local.yaml` (`enabled: true`) and restart the poster; then CC runs `thrift crosslist --dry-run <item>` (the
  two filled-form screenshots in the ops chat) and records the selectors from that evidence; then WO30's supervised
  `--publish-first --marketplace vinted|depop` and `marketplaces.<m>.autopublish: true`.

## Parallel posting (WO33 Part A)
`thrift poster` runs three site workers (`post/parallel.py`; `poster.parallel: true` by default, `false` = the
sequential loop): an approved item goes to every site at once — about its slowest site's minute, not the sum.
- **Poshmark** in its own thread (own event loop, Playwright instance, SQLite connection); **Depop and Vinted** as
  asyncio workers beside the bridge. Each, between its own listings: its pending take-downs first (Part D), then the
  next approved item with a `queued` row for it, in approval order (ties: queue order), within the hours, under its
  own daily cap, the human gap after each listing. Unchanged per site: one job at a time, one click per publish,
  `posting` at the go-ahead (extension) / before the form (Poshmark), never retried blind, the shop check, 3 attempts.
- **Isolation:** a failure, a timeout, a login wall or a CAPTCHA stops only that site for the window
  (`crosslist.block`, Poshmark too in parallel mode — never the owner's PAUSE, which stays the brake for all).
- **The cross rows are queued at the price's approval** (`parallel.feed`: ready, the owner's price, the gate's
  publish), not after Poshmark is live. Parallel needs a cross site and no cross site on the Playwright driver (it
  would want Poshmark's Chrome profile); a one-shot run (`--once`) is sequential.
- **The group line (A3):** once every enabled site of the item is settled (`crosslist.settled`): posted, failed (an
  unconfirmed publish too — its ⚠️ question waits), skipped, drafted, dry-run; or queued on a site stopped for the
  window, at its daily cap, or whose extension has been away 10 minutes since the item's first live listing (a
  moment's disconnect never sends it early; the housekeeping sweeps such items). Links Poshmark · Depop · Vinted; a
  site confirmed later is an ops "Added:" line. "✓ All done" only when nothing is left to list and no take-down is
  pending (`daily.all_done`).
- **The extension (A2):** one job per site (`Bridge._busy(site)`, `_next`), each site in its own window of the Thrift
  Chrome (`siteWindow`: found by its idle page `ext/idle.html?site=…`, made once if missing, never focused), each job
  in a tab of its own that is its window's active tab, captured per window; a job's photos are served under its site
  (`/photos/<site>/<item>/<n>.jpg`: Depop's ≤ 8 and Vinted's ≤ 20 differ). A watchdog pings a page that has been
  silent 30 s while filling: no answer in 3 s (the page's own script holds its thread) ends the job with its state.
- **Depop's SKU (A4):** our item id in `SKU__input` (only the seller sees it; sales are matched by it; Depop's Selling
  API keys products by it) — compared in the read-back once a Mac dry run records it (`selectors.json` sku).
- The supervised `--publish-first … --marketplace X` still needs the poster stopped (the bridge's port), said clearly.

## Sales tracking (WO33 Parts B–G)
**thrift-api** — an Azure Function (`api/`: Python 3.12, Flex Consumption, no always-ready instance, App Insights
capped at 0.1 GB a day, a $5/month budget alert on `thrift-rg`), its data in the database `thrift` (login
`thrift_app`, owner of `thrift` only) on the owner's existing Postgres server; `deploy/azure/provision.py up|deploy|
mode|cleanup|keys` makes and deploys it idempotently from the PC (only `thrift-*` resources, tag
`project=thrift-agent`; the function keys and `thrift_app`'s password stay in the PC's git-ignored `var/azure/`; the
Telegram token is read from the Mac's .env, never printed). The same code runs on SQLite in the tests.
- **Gmail:** a Google Apps Script in the seller's Gmail (`gmail/Code.gs`, read-only Advanced Gmail service, scopes
  gmail.readonly + external_request + scriptapp) posts each new email from poshmark.com / depop.com / vinted.com
  (`from:(…) newer_than:2d`, every 5 min, the seen ids in Script Properties, sender domains checked) to `POST /email`,
  then `POST /heartbeat`; `dumpSamples()` sends 90 days to `/samples` for the parsers. Apps Script hands a body over
  as bytes, not base64 text (WO33, live: the first 110 samples came empty). Reads paced (200 ms) and Gmail's
  per-minute quota waited out (2 s, 6 s); in the 5-minute poll an email is done only once its FULL text went — one
  Gmail won't give is tried 3 runs (`TRIES`), then sent by its headers with `unreadable` and `tries`, and the API
  tells the ops chat (📭); a large body kept as an attachment is fetched (`messages.attachments.get`, read-only).
- **Endpoints** (function keys `mac`, `gmail`, `dashboard`, each revocable; `x-functions-key`, the dashboard `?code=`):
  `POST /email`, `/samples`, `/heartbeat`, `/sync` (the Mac's items and listings), `GET /tasks` (take-downs, a
  10-minute lease) and `POST /tasks/{id}`, `POST /mac-event` (`shipped …`), `GET /sales`, `POST /sales/{id}/match`,
  `GET /dashboard`, `GET /health`, `POST /test-message`. Telegram from Azure is send-only (`sendMessage`; never
  getUpdates / setWebhook — the Mac's long polling owns the updates; a test greps); quiet hours 23:00–08:00 send
  silently; never delayed.
- **Modes** (`SALES_MODE`): `replay` — sales recorded, every would-be message to the ops chat as "[replay] …", never a
  take-down; `live` — only emails after `go_live_at` act (older ones are history); `off` — stored, nothing else.
- **A sale** (`💰 Sold on <Site>: <title> — $X. Ship by Thu Oct 9.`): matched by listing URL / id / Depop SKU, else the
  site's posted listings by title (≥ 0.9), else unmatched (ops; `thrift sales match`); take-down tasks for every
  other site where it is posted; sold twice → `⚠️ Sold twice …`. SHIPPED stops the reminders, DELIVERED closes it,
  CANCELLED says so (no automatic relisting).
- **Ship-by** (`api/thrift_api/deadlines.py`): a date the email states wins; else Poshmark +7 days, Vinted +5 business
  days (US federal holidays skipped), Depop +5 days. Reminders after 09:00 local: the day before, the morning it is
  due, then one ops line if overdue — never twice, none once shipped. `shipped <title words>` in the group marks it.
- **Take-downs on the Mac** (`thrift_agent/sales.py`, `post/takedown.py`): in any hour (a sold item must not sell
  twice) but never with PAUSE, the lid closed, or the site stopped for the window (`parallel.may_take_down`); fetched
  before each worker's next listing
  (GET /tasks also names the items sold since going live: their rows still queued anywhere become `skipped: sold`,
  never listed after the sale); Poshmark Availability Not for Sale (`set_availability`: the edit page's
  Availability, Update, then the public listing read until it's no longer for sale — the WO30 checker), Vinted Hide
  (`ExtensionPoster.delist`: Hide and its confirmation; it counts only when Hide has gone), both reversible; **Depop:
  DELETE** (the owner's decision, 2026-10-08 — it replaces "Mark as sold" and "never delete" for Depop, whose page
  offers no Mark as sold): when a sale on Poshmark or Vinted is matched, on `/products/<slug>/manage/` the bin next to
  Copy listing (`button[aria-label='Delete listing']`), the "Delete listing" window ("Are you sure you want to delete
  this listing? You will lose all the likes." — Cancel / Delete listing), Delete listing pressed once, then the address
  opened again: gone = taken down. A Depop listing that shows sold already (its JSON-LD SoldOut) is never deleted:
  the API is told `sold` and the group hears "⚠️ Sold twice …" (once per item). The window is recorded by the owner's
  practice run (`thrift delist --practice --marketplace depop --url <listing>`: the bin, the window, Cancel, the
  listing checked still live — never Delete listing); until then a Depop take-down asks the owner; a site whose control is UNVERIFIED is
  never tried (the API is told `manual`: the group asks the owner to mark it sold there); 3 failures → the same line;
  all done → `✓ <title> taken down on …`. `thrift delist --verify --marketplace m --url <listing>` records a control
  WITHOUT using it (the page opened, the control found, a picture to the ops chat, nothing clicked).
- **Going live** (D4): the first heartbeat that sees `mode: live` with a new `go_live_at` runs the check once
  (`sales.golive_check`): every item on Depop or Vinted whose Poshmark page (the WO30 backfill checker) is no longer
  for sale is told to the API (`/mac-event` `not_for_sale` → its take-downs), unreadable pages are only counted; ONE
  ops summary.
- **The Mac's link** (`THRIFT_API_URL`, `THRIFT_API_KEY` in .env — the owner's; missing → all of this off, posting
  as before, one ops line a day): every change to an item or a listing is marked by SQLite triggers (`api_dirty`) and
  pushed to `/sync` by the worker each minute, in order, after an offline spell too (once, at the first start, every
  item and listing made before the triggers is marked too — kv `api_dirty_seeded`; `thrift sync --push --all` again); other calls wait in
  `api_outbox`; the heartbeat (`mac`) every 15 min and on every wake. SQLite stays the posting's source of truth,
  Postgres the sales'.
- **Commands:** `thrift sales [--open|--unmatched|--all]`, `thrift sales match <sale> <item>`, `thrift delist --run`,
  `thrift delist --verify …`, `thrift relist <item> [--marketplace m]` (Poshmark For Sale again; Depop/Vinted by hand
  until recorded), `thrift sync --push`, `thrift api ping`; `thrift status` adds the API, its mode, open sales,
  pending take-downs and Gmail's last heartbeat.
- **Dashboard** (`GET /dashboard?code=…`, `api/thrift_api/dashboard.py`): cards (active listings per site, sold this
  month, awaiting shipment with the nearest ship-by, unmatched), a table newest first with each site's status and
  links, filters All / Active / Sold / To ship; no buyer data; inline CSS/JS only (a CSP), light and dark.

## Kids clothing sizes (WO23)
A kids label that gives the child's height ("104 cm", "Gr. 104", "104") or age ("4 ans", "4A", "5-6 Y", "18 mois") is
Poshmark's size by a fixed table (`brain/sizes.kids_clothing_size`): 92 → 2T, 98 → 3T, 104 → 4T, 110 → 5T, 116 → 6,
122 → 7, 128 → 8, 134/140 → 10, 146/152 → 12, 158/164 → 14, 170/176 → 16; babies 50 → Newborn, 56 → 0-3 Months, 62 →
3 Months … 86 → 18 Months; ages the same way (9 → 10, 11 → 12: no 9/11/13 on Poshmark, so up). The labels are Poshmark's
own: its public Kids size filter (Shirts & Tops, read 2026-10-04) and the catalog of its listing form (read the same
day: Kids Shirts & Tops / Bottoms / Dresses — Girls 2T-5T, 4, 5, 6, 6X, 7, 8, 10, 12, 14, 16, XS-XXL; Boys the same plus
7X, 18, 20; Baby Preemie, Newborn, 0-3 … 18-24 Months, 3 … 24 Months) agree. A baby size goes on the form's Baby tab
(`size_choice`); the size buttons themselves are still to be seen in a dry-run. When the table maps the label, the size
is settled (derived, 0.95) — no question — and the description
gets "Label size: 4 ans / 104 cm." (`copy.ensure_label_size`). A bare "4" is not mapped (4T or kids 4?): the label photos
are read once more for the units (`cover.read_size_label`), else it is asked as before.

## Premium details (WO26)
Owner rule: premium details are selling points, stated exactly — but ONLY when a label or the photos show them (the WO4
no-hallucination rule stays).
- **The labels read closely** (`brain/labels.py`, `models.labels` = Opus 5.5, one call an item that has label or tag
  photos): the label/tag photos at `images.label_long_edge` (2048, the full work size; any orientation), the detail
  photos at the usual size. Out: `facts.premium` — composition exact ([{fiber, pct, part}], one entry per fiber in
  English: "100% SILK / 100% SOIE / 100% SEDA" is silk 100), made_in, a premium line / sub-label, vintage only with a
  concrete cue (union label, old tag, single stitch, Big E, a dated care tag) and the era when clear, collab / limited
  edition / sample (with its type as the tag prints it: "Sample: 1st Proto Fit"), technical (Gore-Tex, waterproof,
  down fill, Primaloft, UPF), construction (fully lined, silk lining,
  hand-knit, handmade, beading, Goodyear welt; never a negative), a retail price printed on an attached hang tag —
  every value with its photos; `premium.merge` keeps only values a photo of this item shows. The main fabric's
  composition becomes `facts.material` (the materials rule's evidence); the hang tag's price the retail price when no
  screenshot gave one. A failed read is logged and retried by `recover`; no label photo = an empty read.
- **Config** (`config/premium.yaml`, editable; `private/premium.yaml` replaces it): premium fibers (silk, cashmere,
  merino, wool, alpaca, mohair, camel, angora, linen, genuine leather / suede, shearling, down, organic / Pima / Supima
  cotton) with the title's word; "Made in" countries worth saying, as "what a label prints → what the listing says"
  (Italy, France, Japan, USA, UK incl. England / Scotland / Wales, Portugal, Spain) — **any other country (China,
  Bangladesh, Vietnam…) is never mentioned**; premium lines (J.Crew Collection, Purple Label, BR Heritage, Levi's Made &
  Crafted, Zara Studio / Limited Edition, We The Free, Maeve, Pilcro); the price multipliers.
- **Title** (`premium.title_with_feature`, after the copy and `ensure_set_title`): Brand → the ONE strongest feature →
  item → color → US size. Strongest: a premium fiber from 90% of the main fabric ("100% Silk" at 100, else "Cashmere")
  > a premium line (after the brand: "J.Crew Collection …") > "Vintage" with its era ("Vintage 90s") > "Made in Italy"
  > a collab > a premium blend 50–89% ("Silk Blend"). Its weaker wording elsewhere goes ("White Silk Pants" → "100% Silk
  White Pants"). Over 80 characters (`premium.fit_title`): the words just before the size go first, then backwards — a
  detail, a hanging connector — never the brand, the feature, the US size, the set phrase, the item's last two type
  words or its colours. Idempotent.
- **Description** (`premium.ensure_feature_lines`, both marketplaces): every confirmed feature in a plain line of its
  own, before the condition line — "Material: 100% silk." (every fiber of the main fabric), "Lining: 100% silk." (a
  premium lining), "Made in Italy.", "J.Crew Collection.", "Vintage 90s.", "H&M x Erdem.", a sample as whose and which
  ("J. Crew Sample (1st Proto Fit).", WO27, `premium.collab_line`), "Gore-Tex, waterproof.",
  "Fully lined." — then the condition line, then "Original retail $128.". The copy writer never sees `premium`: code
  states it, so it's never doubled or embellished.
- **Price** (`premium.price_factor` → `price(…, premium=)`): the suggestion × ONE multiplier, the largest that applies
  (fiber 1.3, blend 1.1, line 1.2, vintage 1.2 — never stacked); never a seller-note price, never the owner's price.
- **Existing items:** `thrift recover` reads the labels once for an item without `facts.premium` (`--recheck` reads
  again), applies the title and description rules and the Original Price (`relist`), reprices a suggestion the owner
  hasn't approved, and compares the card as in WO24; its output line gives the new title and what the labels gave.

## Sizes (WO25)
`sizes.poshmark_size` puts the item's size on Poshmark's own menus (the catalog): the size the poster selects is exactly
one value of one tab's menu (`Render.size_tab` / `size_value`; `post.size_choice` takes it as it is). One-size
categories (bags, jewelry, most accessories, Home): "One Size", and no size question (`gate.evaluate(sized=False)`).
Kids shoes by their C/Y size: 0-7C on the Baby tab as the bare number, else Girls / Boys "7.5 (Toddler Girl)" (Toddler
7.5-12C, Little 12.5-13.5C and 1-3Y, Big 3.5-7Y). Kids clothing on the Girls or Boys tab by kids_gender (unisex or
unread: Girls), else the Baby tab ("3 Months", "Newborn"); a size only one gender's menu has is not moved to the other.
Adults: the tab the label asks for first — Maternity (the item type or label says so), Petite ("8P", "M Petite"),
Juniors ("Jrs") — then Standard, Plus, Petite, Juniors, Big & Tall in that order, the value spelled as the menu spells it
("3XL" → "XXXL" for Women, a men's "32x30" → "Waist 32", "15.5" → "Neck 15.5", "34DD" → "34E (DD)", "8 1/2" and "8.5M" →
"8.5"). A size read well (≥ 0.70) that no menu of the category has is a question on the card ("Size: “38” isn't on
Poshmark's Women Shoes size list (Standard) — reply 'size …'"). The card shows the menu's words ("Waist 32", "4T (Boys)",
"14 (Plus)"). Renders made before WO25 keep the old mapping (the Baby tab's and kids clothing labels now marked verified:
they are the catalog's) until `recover` / `reprocess` rebuilds them.

## Categories (WO23, WO24)
`taxonomy.fit` puts the model's department / category / subcategory on Poshmark's names right after extraction (the
lists: `data/poshmark_taxonomy.yaml`, the form's catalog; the extraction prompt shows them all).
- A department is never a category (WO23; live: category "Kids", subcategory "Shirts & Tops"): `fit` takes the
  subcategory, else the item type's noun ("graphic tee" → Shirts & Tops, "ankle boots" → Shoes), else asks with a real
  example; the extraction prompt says so too.
- A subcategory given as the category is put under its category (WO24; live: category "Jumpsuits & Rompers" was asked
  as "not one of Poshmark's Women categories" → Pants & Jumpsuits › Jumpsuits & Rompers, no question): its whole name,
  else one part of a two-part name ("Hoodies" → Men Shirts › Sweatshirts & Hoodies; "Sweatshirts & Hoodies", "Tees -
  Short Sleeve", "A-Line or Full"), when exactly one category has it — a whole name wins over another's part ("Wide
  Leg" is Pants', not Jeans' "Flare & Wide Leg"); a name two categories have ("Maxi", "Mini", "Skinny") is still asked.
  A subcategory matches one part of Poshmark's name the same way ("Jumpsuits" → "Jumpsuits & Rompers").
- "Which category?" (WO25): the model gives `category_confidence` and, under 0.70, up to 2 `category_alternatives`.
  Under 0.70 — or a category Poshmark doesn't have, or "Other" — `pipeline.category_question` offers 1-3 real paths
  (`taxonomy.category_options`: the model's own pick if Poshmark has it, its alternatives, every category that has its
  word as a subcategory; never "Other") as ONE message before Girls/Boys and the price card ([Skirts › Skirt Sets]
  [Shorts]; `cat:<item>:<n>`). A typed reply works: a number, an option's words, any real path ("Shorts", "Pants &
  Jumpsuits / Wide Leg"); anything else is a note, as before. The answer is `items.owner_category`, applied after every
  extraction and passed to the model as a seller-note line. The model's own pick settles at once (no model call);
  another path reprocesses the item (price, size menu, copy follow it). A price alone doesn't settle it (like Girls/Boys).
- The extraction prompt (WO24): a two-piece set is never a dress — Women top + skirt = Skirts › Skirt Sets, top +
  shorts = Shorts, top + pants = Pants & Jumpsuits (the bottom decides), Kids = Matching Sets; a catsuit / jumpsuit /
  romper = Pants & Jumpsuits › Jumpsuits & Rompers (Kids: Bottoms › Jumpsuits & Rompers); pants, sheer or flowy ones too,
  are Pants & Jumpsuits (wide-leg = Wide Leg) unless the photos clearly show swimwear (then Swim › Coverups).

## Model calls
Every call goes through `brain/llm.ask`: one tool, the answer validated by pydantic (one repair round). A forced
`tool_choice` (`tool`) is used where the model takes it; Claude Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 answer it
with a 400 (docs: Define tools > Forcing tool use), so they — and any model whose 400 says the same, remembered for
the process — get `tool_choice` auto (one call at most), an explicit instruction, and one reminder (WO19). Strict
tools / structured outputs are not used: our schemas use `minimum`/`maximum`/`minLength` and more than 24 optional
fields, which they reject. `tests/test_llm.py` has a fake client that answers forced tool use like the API.

## Style
Python 3.11+, pydantic v2, pathlib everywhere (Windows + macOS). Pure functions for anything testable
(gate, price, scheduler, segmentation checks, corrections). `pytest -q` must pass before deploy.
Tests never read `config/settings.local.yaml`, `private/` or `.env` (tests/conftest.py isolates them); a test that
needs prod behaviour or private data opts in with the `settings_override` fixture.
