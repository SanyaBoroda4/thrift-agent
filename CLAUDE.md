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
- The PC reaches the Mac with an SSH key, no password: `ssh <user>@192.168.x.x` (<mac-host>.local;
  its host key is known under the IP). Repo `~/thrift-agent`, venv `.venv`, logs `~/thrift/logs`, DB `~/thrift/var`.
- **Find the Mac over Tailscale first (owner rule, 2026-10-10):** the PC and the Mac are on Tailscale (same account);
  `tailscale status` shows the Mac (`<mac-host>`, <tailscale-ip>) — SSH to that address with
  `-o HostKeyAlias=192.168.x.x` (the known host key must match). It works from any network: a hotel's guest Wi-Fi
  keeps devices from reaching each other (2026-10-10). Fallback, the home LAN (WO28): it travels and DHCP moves
  addresses — on 2026-10-05 the PC itself had the Mac's usual IP. Try `<mac-host>.local`, then its LAN IP; if neither
  answers, ask the user for the IP and wait — never assume.
- `deploy\deploy.ps1` (default: the Mac's Tailscale address and HostKeyAlias from the git-ignored
  `deploy/mac.local.json`; `-MacHost` /
  `-HostKeyAlias` to override, `-NoPush`): `git push`,
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
  ─► verify (LLM strip unsupported claims) + lint (deterministic; what it flags is fixed in code where it can be —
     the title's size, "New" on a used item, an EU size — and never holds the item, owner rule 2026-10-09)
  ─► gate: publish | needs_info (the allowed questions only)
  ─► (WO37: nothing but the price waits — a shoe in doubt is Like New, an unsure kids item Girls, an unsure category the
      model's own pick; each shown on the card, a reply 'condition …' / 'boys' / 'category …' corrects it)
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
   - **WO37 (the owner, 2026-10-12): never asked any more** — a pair in doubt is listed Like New (NWT only with a tag
     photo), shown on the card; a reply 'condition nwt|like new|good' (`pipeline.correct_condition`, no model call: the
     size and the copy stay) corrects it. What follows is the WO18 rule as it was, for items asked before.
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
   - **Owner rule (first run).** `gate.min_confidence` is 0.70 for brand (below: a question), size and condition
     (below: a note, never a question — 2026-10-09); the price is the
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
   - **Never a size question, never a hold (owner rule, 2026-10-09 — replaces WO25's size questions and the draft
     hold).** An unsure size (below 0.70) or one off Poshmark's menus is listed as best read; the card says so ("Size S —
     my best reading; reply 'size …' to change it"; a gate note under `SIZE_NOTE`, kept in `gate.info`) and the poster
     takes the nearest menu value. A set whose pieces have different sizes is listed under the BIGGER one — the size
     field and the title — and the description says which piece is which ("Cardigan: size S. Pants: size XS.";
     `facts.piece_sizes`, `pipeline.settle_set_size`, `copy.ensure_piece_sizes`; the owner's `thrift edit --sizes
     'Cardigan=S,Pants=XS'`, kept as `items.owner_sizes`). The copy check fixes what it can (the title's size,
     `copy.ensure_title_size`; "New" on a used item and an EU size, `copy.fix_title`) and never holds: a `ready` item with
     the owner's approved price always publishes, whatever its gate said (`runner.publishable`, `parallel.feed`).
   - **WO37: only the price waits.** The brand below 0.70 and NWT without a tag photo stay lines on the price card; a
     possible re-share is a warning on the card, never a hold ('same item' drops it, a price lists it); the category,
     Girls/Boys and the condition are decided and shown ("Category: …"), corrected by a reply (size … · brand … ·
     condition … · category … · cover N — `approve.CORRECT_HINT` on every card). The list below is how it was.
   - **The only questions.** Brand below 0.70 (the brand question carries a [No brand] button, WO25), NWT without a
     hang-tag photo, "Which category?" — the
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
     Poshmark: if it's there, reply 'posted <url>'; if not, reply 'retry'.", outbox kind `unconfirmed`) — in the OPS
     chat since WO35 (the group never gets a "can't see it" question; a reply there works the same, `_route_ops`).
     "posted <url>" is queued for the running poster, which opens the page (title, price; Depop also the SKU on
     /manage/) between listings (`thrift mark-posted` queues it too while the poster runs); "retry" (`thrift retry
     <item>`) is the owner saying it is not there: the row goes back to `queued`. A reply to a row settled meanwhile
     gets "✓ Already settled — it's live". For Poshmark the code never decides either by itself.
   - **Depop and Vinted settle it by the site's own data (WO35 §4, `runner.recheck_unconfirmed`):** nothing is said
     when the publish is unconfirmed; 10 minutes later (`crosslist.RECHECK_AFTER`, kv `unconfirmed_state`) the poster
     looks again — Vinted's JSON of the newest items (the exact title), Depop's shop (the title's words after the
     username part of each address) confirmed by the SKU on /manage/. Live → posted (the card follows). Absent → it
     wasn't posted: it counts toward "three in a row" only now, goes back to `queued`, and is looked for once more
     right before the second post (`run_cross`: found then → posted, never twice). A second post that can't be
     confirmed either → the ops chat only (`second_miss_text`, the screenshot; a reply 'posted <link>' / 'retry'
     settles it), never a third post. Unreadable data is never "absent": looked at again later (6 times, then the ops
     chat). "Never retry blind" stands: a repost needs the site's own data saying the listing is absent.
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
- **Recorded from the 2026-10-09 backfill (WO35, from its evidence and the architect's read-only look):**
  - Depop's Post now lands on `/products/create/success/?productId=<n>` ("Nice! It's listed", title "Done - Depop") —
    its **View listing** link `a[href*='/products/'][href$='/manage/']` is the new listing (`landing_url`,
    `view_listing`); the old code took that page for an unknown one and looked in the shop, where its first-four-words
    match missed all three Levi's ("Levi's" → "levi", "s"; the address says "levis"). The seller's /manage/ page shows
    "SKU: <our item id>".
  - Depop scrambles one character of the username in every product address (`example9hopname`, `exampl3shopname`,
    `exampleshopnam0`, …): a listing is matched on the title's words after the username part, never on it
    (`ext_driver.slug_words`), and confirmed by the SKU.
  - Depop requires a brand: Post with it empty is refused — "This field is required" under Brand, the form stays (the
    silk pants, no brand). No brand, or one Depop doesn't list → Depop's own **Other**.
  - Vinted requires a brand too: Upload refused "Fill in brand to continue" (the Solid & Striped set; the MNG set had
    gone through only because Vinted filled "Mango" itself). Its brand menu ends with "No brand indicated" › **List
    without brand** (`div#empty-brand[role=radio]`, `empty_brand`); ours is searched by the exact name and its own
    spellings only (the owner, 2026-10-11: never a word of it — see "Brands").
  - Depop's package size (live 2026-10-11, the silk pants' second try): Depop had put "Small" in by itself and its
    menu answered "No option" to "Small" typed again — a single choice already ours is now kept as it is, and a menu
    that offers nothing for the typed text is read whole.
  - Vinted's package sizes come from its server after the details: none drawn yet when the price is typed (Seven
    Wonders, twice: "package MEDIUM not offered ()") — waited for, 10 s.
  - Vinted's own JSON of the seller's newest items, same origin from the member page:
    `/api/v2/wardrobe/<member id>/items?order=newest_first` (`wardrobe`: items[].id, .title, .url) — the confirmation
    after Upload, by the exact title.
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
`per_hour_max` / `daily_cap`, with a human `gap_seconds` pause after each (now and then a 2–4× longer break). The
gate never holds an item (owner rule, 2026-10-09): an approved `ready` item publishes whatever the gate said; only a
marketplace whose autopublish is off is held (`runner.held`: "⏸ Not published automatically", no command). A skipped
item never trips the circuit breaker; its group message takes a reply ('retry', `runner.ask_skipped`). `mark-posted <item> <marketplace> <url>` records a listing found by hand for
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
- **No style tags (the owner, 2026-10-12 — WO36: the Alo set's "Coord Sets" and "Athleisure" deleted by hand).**
  Poshmark's Style Tags and Depop's Style stay empty: the copy prompt asks for none, `copy.clean` and the renders drop
  any, the Poshmark poster never types one (`poshmark.ALLOW_STYLE_TAGS = False`: WO12's curated-tag step stays behind
  it), Depop's job carries `style: []` and its page removes a Style chip Depop put in by itself; the read-back expects
  Depop's Style empty. Depop's hashtags are unchanged.
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
  5. **"cover N"** — a reply to the card, or typed while it is open (no button): her Nth photo of the item (shooting
     order, counting from 1 since WO37 — `approve.cover_index`) becomes the cover (`items.owner_cover`, kept through
     reprocessing) and the card is EDITED to show it, answered at once ("✓ Cover: photo 3"). Never for an item on the
     marketplace.

  Facts from before WO20 (no roles) keep the model's pick and the old rule (no flaw photo as cover).
- **The cover is 3:4 portrait** (`images.cover_size: [1200, 1600]`), never cropped: the frame of Poshmark's cover
  dialog, so its default crop takes the whole picture. WO37 (live: flat grey bars above and below a sideways photo):
  the photo, whole, over a blurred enlarged copy of itself (`prep.portrait_cover`); an exact 3:4 photo is only resized. The re-share check keeps
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
  processed wins: that result is dropped and the item processed again). After an answer the answered card's
  buttons become ONE inert button with the answer (`approve._ack`, `editMessageReplyMarkup`: "✓ $35 — queued",
  "✓ Girls", "✓ Like New (brand new, no tags)", "✓ cover: photo 2", "✓ brand: J. Crew"; callback `noop`), then the next
  message. **Every REPLY gets an answer at once (owner rule, 2026-10-09 — live: "Size S" replied to a card already
  closed by its price went nowhere she could see):** a reply to her message saying what was understood and what happens
  next — "✓ Size S · $100 — it goes up at 8:00 on Poshmark, Depop and Vinted" (`approve._confirm`, `next_step`,
  `when_posting`); a handler that said nothing gets "✓ Got it — …" (`_fallback_answer`, the bot wrapped in
  `_Answered`); a button tap keeps its "✓ …" label only. "Size S", "size s", "S", "size 8.5" (with or without a price)
  set the size at once, no model call (`approve.size_reply` → `pipeline.set_size`, kept as `items.owner_size`). **No
  message may need a terminal command (owner rule):** a reply in Telegram is always enough — the ⏭ skip line takes
  'retry', the API's "Unmatched sale" line in the ops chat takes words from the item's title (`approve._route_ops`:
  the bot also hears the owner's private chat; it matches the sale through thrift-api). [Later] puts the item behind everything queued so far (`items.deferred_at`). The queue is the DB: a
  restart re-sends only the open message; older unanswered copies are closed (their buttons still work). Info-only
  messages are not queued; a successful dry-run says nothing unless `poster.notify_dry_runs`.
- **A quiet group (owner rule, WO29).** The GROUP (`TELEGRAM_CHAT_ID`) gets only: (a) the cards and the allowed
  questions; (b) ONE card per item (WO34, the owner: separate lines, little text, some emoji; `crosslist.card_text`,
  HTML, link previews off): "✅ <short name> · $X", an empty line, then "🟣 Poshmark", "🔴 Depop", "🟢 Vinted" each on
  its own line with an empty line between — the site's name is the link (`<a href>`); a site that hasn't posted yet
  "⏳ Vinted — later" (no link); a site that failed for good or was skipped is left out. Sent once the item's sites are
  settled (WO33 A3); a site that posts later EDITS that same message (outbox kind `posted`) — never a new one. The short
  name is the copy step's `short_name` (brand + item, ≤ 6 words, no size, no colour; `copy.short_name`), for older items
  the title cut at a word near 40 characters. Nothing technical: the posters' notes ("brand left empty …") go to the ops
  chat as ONE "Check — <title>:" message per item, each note once (event `checks_said`). While one of its sites is
  unconfirmed the ⚠️ question waits as before; an item announced before WO34 (the old one-liner) gets an ops "Added:"
  line. The window's last card ends with "💤 All done — you can close the Mac." when nothing is left on ANY site, or
  "💤 Done for now — you can close the Mac. Vinted catches up next time." when a site stopped for the window, at its cap
  or without its Chrome still has listings (`daily.done_state`; WO34 — live, those had counted as done and the group
  heard "safe to close the Mac" while three sets waited for Vinted); the 💤 line rides on the newest card (kv
  `posted_done_carrier`: the card that had it is edited without it, and its wording follows the state); (c) action-needed
  alerts, one plain sentence each, once per episode: "Depop needs you to log in on the Mac." / "Vinted asks for a
  check — open it on the Mac." (WO30); 🔋 battery low; the
  Mac slept while publishing (the ⚠️ "can't see it" question itself is the ops chat's since WO35); "Can't read the
  iCloud inbox" (past 10 min); "⏸ Not published automatically: <title> — <site> publishes by itself only once its autopublish
  is on."; "⏭ <title> wasn't listed on <Site>: <why>. Reply 'retry' to try it again." — plus plain replies to the owner's own messages ("No
  price card is open", the hints, "Sorry, that didn't go through — please try again."). No item or batch ids, no
  errno, no traceback. Everything else goes to the **ops chat** (`notify.ops`/`notify.say`): the status message
  (WO28; also in `thrift status`), "Back online", "Poster started (LIVE)", dry-run notes, CLI echoes, "batch …
  rebuilt", the details of a hold or a skip, every ❌ error. The ops chat is the owner's private chat with the bot:
  `TELEGRAM_OPS_CHAT_ID` (`.env`), else `telegram.ops_chat_id` (set in `private/settings.yaml`), never the group's id;
  it works once the owner has sent the bot /start. A send there that fails is logged (event `ops_chat_down`), dropped and
  not tried for 10 min (kv `ops_down_until`) — **never sent to the group instead**. The same text again: never (WO37;
  was once a day — kv `ops_sent`, ids ignored, kept 30 days), except the status message (edited in place) and the CLI
  echoes. Routine poster notes ("category menu: …", Depop's "refused … looked up" of a listing found live, "removed
  'X' … (Depop's own suggestion)") stay out of it (`crosslist.noteworthy`).
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
- **The poster never asks (WO27).** Brand — the names and rules of "Brands (WO36)" below, the same on all three
  sites; on Poshmark none → Brand left empty (it is optional), the note listing what it offered. Size: the menu's value,
  else the nearest of the same system, one size away at most (`post/fallback.nearest_size`: 8.5 → 9, XL → L, never a
  Petite or a kids label for a plain size) — the owner's defined fallback for a required field (2026-10-09). Category
  and subcategory (WO36, `post/fallback.matching`): the exact name, else the ONE name that is ours followed by more
  words ("Jumpsuits" → "Jumpsuits & Rompers") or of whose " & " parts ours is one — never a merely similar one (WO27's
  difflib took "Over the Knee Boots" for knee-high boots); a category nothing matches skips the item (required); a
  subcategory, None. Colours not offered are left out. Every guess goes in the "Posted ✓
  <title> — check: brand set to 'J. Crew' (from 'J.Crew')" message (none → no extra words) and the event log. A
  required field nothing comes close to: `skipped` — nothing saved, the post `failed` with "skipped: …", one Telegram
  line, `thrift requeue <item>` once the listing is fixed. The outbox kind `owner_q` is no longer sent; a reply to an
  old one still works (to a brand question, the reply is the brand).
- **A plain reply is the answer to THAT question** (WO27): to a card that asks the brand, "J. Crew" is the brand
  (`approve.brand_reply` → `pipeline.set_brand`: no model call; the old name replaced in the copy, the title brand-first,
  the card sent again when it changed); "brand X" works on any card; a size, NWT, a number… keep their meaning.
- **WO37: no message is ever sent twice** (the owner; live 2026-10-12: one question came 4 times). `approve.once`
  records a placeholder row in the outbox (a negative message id) BEFORE the send: a send that timed out after the
  request went out (`telegram.maybe_delivered`: ReadTimeout, WriteTimeout, RemoteProtocolError…) counts as sent —
  never sent again; only a connection that never opened is taken back and sent on the next pump. A card that changes
  (a correction, a reprocessing, "cover N", Later) is EDITED in place (`approve.refresh_card`, `Bot.edit_card`:
  editMessageMedia / Caption / Text), never sent anew; `resend_pending` sends nothing — the wake, a restart, a deploy
  never repeat a message (`telegram.resend_after_hours` is no longer used). Uploads get 90 s (`UPLOAD_TIMEOUT`).
- Before WO37: once the open message had waited longer than `telegram.resend_after_hours` (default 6), it was sent
  again (WO24: a restart never repeated a card sent less than that ago).
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
  - Brand at fill time: see "Brands (WO36)" — the same names and rules as Poshmark (the job's `brand_candidates`,
    the extension's `T.brandChoose`). None → Depop's "Other", Vinted's "List without brand" (both require a brand,
    WO35); a Vinted brand flagged for authenticity / luxury: listed, ops note.
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
  on Poshmark (its public page checked first), oldest first (`--dry-run`: lists them only), at each item's price on
  Poshmark that day (WO33, the owner: some were changed there by hand — a price that differs becomes the item's
  `owner_price`, the one Depop and Vinted get, and ONE ops message lists the differences).
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
  request, the landing page watched after the one click (`tabs.onUpdated`, ≤ 3 min since WO35: Depop's success page
  gives the new listing's address, Vinted's member page its JSON of the newest items; a form the site refuses — its
  error shown — is said at once, "refused: …"), a job ended when its bridge goes
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
  path), brand search (our names exactly, then the one longer name — `T.brandChoose`, WO36), size, condition,
  colours ≤ 2, materials ≤ 3, the skirt length, the package radio; photos as Files on `input.files`, all in one DataTransfer (fallback: a drop);
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
  failed, nothing submitted; after the click (WO35 §3) the listing is confirmed by its ADDRESS: the page it landed on
  names it (Depop's success page → /manage/, whose SKU must be our item id; a listing page), else the site's own data
  every 30 s for 5 minutes (`confirm_by_address` → `find_live`: Vinted's JSON of the newest items, the exact title;
  Depop's shop by the title's words after the username, then the SKU; an address another item holds is never taken;
  several → never a guess) → else unconfirmed, settled later by `recheck_unconfirmed` (invariant 4). A form the site
  refused is looked up once: absent → a plain failure ("Vinted refused the form: …"). `confirmed_in`: seconds from the
  click to the address (WO35 §5). The attempt is counted when the job starts (`db.begin_attempt`).
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
  own daily cap, a pause after each listing — WO37 (the owner, 2026-10-12): **none** on any site
  (`schedule.gap_seconds`, `marketplaces.<m>.gap_seconds`, `crosslist.gap_seconds` all [0, 0]; `scheduler.next_gap`
  gives 0 and no occasional longer break; `schedule.per_hour_max` 60 so the hour never stops a site); the daily cap
  (25 a site), the hours, PAUSE and the login / CAPTCHA / block stops stay. Before: Poshmark 150–420 s (now and then
  2–4× longer), Depop and Vinted 30–90 s (`crosslist.listing_gap`). Unchanged per site: one job at a time, one click per publish,
  `posting` at the go-ahead (extension) / before the form (Poshmark), never retried blind, the shop check, 3 attempts.
- **Isolation:** a failure, a timeout, a login wall or a CAPTCHA stops only that site for the window
  (`crosslist.block`, Poshmark too in parallel mode — never the owner's PAUSE, which stays the brake for all).
- **The cross rows are queued at the price's approval** (`parallel.feed`: ready, the owner's price, the gate's
  publish), not after Poshmark is live. Parallel needs a cross site and no cross site on the Playwright driver (it
  would want Poshmark's Chrome profile); a one-shot run (`--once`) is sequential.
- **The group line (A3):** once every enabled site of the item is settled (`crosslist.settled`): posted, failed (an
  unconfirmed publish too — its ⚠️ question waits), skipped, drafted, dry-run; or queued on a site stopped for the
  window, at its daily cap, or whose extension has been away 10 minutes since the item's first live listing (a
  moment's disconnect never sends it early; the housekeeping sweeps such items). The card (WO34, see Telegram): a
  site confirmed later edits it. "💤 All done" only when nothing is left to list on any site and no take-down is pending;
  "💤 Done for now … <Site> catches up next time." when only a stopped site waits (`daily.done_state`).
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
- **Its log (WO35 §6):** `api/host.json` — the host from Warning, `Host.Results` Information (one request row per call),
  `Host.Aggregator` None, `Function` Warning, our own lines `Function.<name>.User` Information (one entry per function:
  the host's App Insights filter has no `*` in a category; a test reads the names from `function_app.py`); sampling on
  (≤ 1 item/s, exceptions never sampled); no live metrics, dependency tracking, performance counters or extended HTTP
  details. A failure is logged as its type and place (`util.where`), never its message, a body, a key or a token. Before
  (2026-10-09, the 0.1 GB cap hit): the Mac's GET /tasks loop, the host's per-call and start-up Information lines,
  per-function aggregates and performance counters. The host's `azure.functions.health_check.reports` metric (~9 MB a
  day) can't be turned off from host.json — only by a workspace ingestion filter (not set up).
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
  for all the poster's sites in ONE GET /tasks at most every 2 minutes (WO35, `sales.TASKS_EVERY`: each worker had
  asked on every idle turn — ~5,600 calls on 2026-10-09 — and the API's log hit its 0.1 GB daily cap)
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
  `thrift delist --verify …`, `thrift delist --item <item> --marketplace poshmark` (the owner's take-down of one
  listing, Not For Sale — for an item sold where no sale email could make the task; the poster stopped, as a one-off
  job in the GUI session: `bash deploy/gui_once.sh delist --item <item> --marketplace poshmark`), `thrift relist <item> [--marketplace m]` (Poshmark For Sale again; Depop/Vinted by hand
  until recorded), `thrift sync --push`, `thrift api ping`; `thrift status` adds the API, its mode, open sales,
  pending take-downs and Gmail's last heartbeat.
- **Dashboard** (`GET /dashboard?code=…`, `api/thrift_api/dashboard.py`): cards (active listings per site, sold this
  month, awaiting shipment with the nearest ship-by, unmatched), a table newest first with each site's status and
  links, filters All / Active / Sold / To ship; no buyer data; inline CSS/JS only (a CSP), light and dark.

## Oct 16: twins, sizes, a refused form (WO39, 2026-10-16)
Three shares; two of them were the Oct 12 Alo bras' very photos again (IMG_1293–1298 — the Oct 13 "-late" items
i_261013_71458e / i_261013_b325a8, priced, waiting for a size). The brown bralette got $25 but no size reply; the
Sam Edelman's Vinted form was kept twice after Upload with nothing on the page, the third identical form went live.
- **The same item is never listed twice** (`pipeline.settle_twin`, from `set_price`; `parallel.feed` as a backstop;
  `thrift twins [--apply]` for items priced before): a card flagged "looks like item X" that gets its price — X not
  live anywhere: X `dropped`, its rows skipped "shared again as …", its card closed, a late reply to it refused
  (`DROPPED_SAID`); X live on a site (posted, posting, an unconfirmed publish): this item's row there `skipped:
  already live as X` (her size later never requeues it). One private ♻️ line; once (event `twin_settled`).
- **A size reply applies to the card it answers only**; "size S" typed without a reply is answered "Which item? Reply
  'size …' to its card", never guessed. A price on a card with no size: "✓ $25 — needs a size: reply 'size …' to this
  card" (never "queued"), `next_step` "it goes up once you reply 'size …' to its card" (`approve.needs_size`).
- **The 💤 line names what waits for her** (`daily.owner_waits`: items priced but skipped everywhere for no size;
  listings waiting for her 'retry' — refused, skipped, unconfirmed): "💤 Done for now — waiting for a size: Alo Yoga
  Sports Bralette Mauve Taupe." (`crosslist.done_line(state, waits)`, `daily.done_text`, names `crosslist.item_name`);
  "💤 All done — you can close the Mac." only when nothing waits; the ops status message says the same. The old "✓ All
  done — safe to close the Mac." (the status message, and two dead one-liners) is gone.
- **A refused form** (extension + driver): from the click on, every error is kept — alerts, snackbars / toasts that go
  away, `aria-invalid` fields anywhere (by their label), the site's own `/api/` requests after the click with their HTTP
  status (resource timing) — `T.watchAfterClick`, `T.refusal`; still the sell form at the after-publish look = "refused:
  …" (`common.js`), and the driver takes ANY after-publish "form" page as a refusal (one look at the site's data; found →
  posted). The row `failed` "Vinted refused the form: …", ONE reply-able private line "⛔ … refused the form — <error>.
  Nothing was published (its own data agrees). Reply 'retry' to post it again." (outbox kind `refused`, `_route_ops`);
  never requeued by a new window (`crosslist.refused`) — the same form is never posted again by itself.
- **Follow-ups (the owner, 2026-10-16):** a refusal that names no field (`crosslist.silent_refusal`: nothing on the
  form, or only "Something went wrong" / "try again") says nothing at first; WO35's re-check takes it (kv
  `unconfirmed_state` with `refused: true`, `rechecks_due`): 10 minutes later the site's own data — found → posted;
  absent → queued, one last look right before, posted ONCE more by itself; refused again → the ⛔ 'retry' line. A field
  error asks at once, as above. **An item waiting for a size** gets ONE group message, "Waiting for a size: <title> —
  reply 'size …' to this message" (`approve.ask_sizes`, from the worker's window tick; outbox kind `size`, `once` —
  never repeated); a size replied to it is that item's (`_reply_size`), every site back in line. **'same item' on a
  card whose earlier twin is retired already** is answered `TWIN_KEPT` and the item stays (live 2026-10-17: the reply
  came three minutes after the retirement and both brown bralettes were dropped; the owner brought the new one back).

## Take-downs that happen (WO40, 2026-10-17)
The UGG Chestnut sold on Depop (2026-10-16 15:34 UTC), was matched at once and both take-downs were made — and it stayed
live on Poshmark and Vinted overnight with no word to anyone. Three causes, all fixed:
- **Poshmark's edit page saves with List This Item, not Update** (as WO37 had seen for the cover): Update only slides up
  the Share Listing panel. `set_availability` → `_save_edit` presses that panel's List This Item once, only when it is
  THIS edit's (`SEL["edit_save"]`: `data-et-prop-flow_type="edit"` and the listing's id), then the public page decides.
  The stand-in edit page (`tests/test_poshmark_form.py`) now behaves like the live one.
- **Vinted's Hide is waited for** (15 s; it had been read the instant the page script started: "no Hide button"), and a
  take-down whose page reloads (`/session-refresh`) is handed to the new page — with `pressed` once Hide was clicked,
  then never clicked again (the page only says whether it took). "Unhide" on the page = hidden already. Depop's delete
  is never handed again after a reload.
- **The API counts the attempts; the Mac waits for its next lease.** The API takes a failure only for a task it leased
  (`running`); the Mac had retried three times on one lease, so the API counted one, the Mac marked its row `failed`
  for good, and the API re-leased it every 10 minutes, never reaching the third failure that asks the owner. Now a
  failure is `waiting` on the Mac until GET /tasks hands it out again (then `pending`, the API's attempt count); three
  leased failures → "Couldn't take … down on <Site> — please mark it sold there." in the group.
- Take-downs run on a site stopped for the window by **posting failures** (`crosslist.account_blocked`: only a login /
  CAPTCHA / block / check page holds them — a take-down meeting one stops the site, the usual plain line, no attempt
  counted). Their evidence goes to `failed/shots/` (it had landed in the repo folder).
- **The API's own safety net** (`core.late_takedowns`, the 15-minute timer): a live sale 2 hours old whose item is
  still up elsewhere (a take-down not done, or a site the mirror shows posted with none) → ONE group line, "⚠️ <title>
  sold on <Site> but is still listed on <Sites> — please mark it sold there." (settings `takedown_late:<sale>`), and the
  dashboard's "Sold, still listed" card and a row flag until it's down.
- **Every path makes the missing take-down** (`core.ensure_takedowns`): a listing the sync brings after its item sold;
  the order sweep (a sold order on a site → every other posted site taken down; a take-down left to the owner whose
  listing is still posted handed to the Mac once more, `takedown_reopened:<task>`). Live mode only; a read never starts
  the live clock.

## The whole path from share to listing (WO45, 2026-10-18 — replaces WO43's and WO44's scope; WO43's code stays)
Live (Oct 12–18): 4 of 23 shares never became a card and 11 came short or late; the Ralph Lauren shirt (a 20-photo
share) was taken with ONE photo at minute 17 (WO42's 15-minute rule), went up on Vinted and Depop with it, failed on
Poshmark (its cover dialog has no title for one photo), and the other 19 photos — its own three and three more items —
were told in a private line and never processed. The rules: every share reaches the Mac within 2 minutes when the
phone has internet (§2); every share ends in exactly one visible outcome (§3); the same photos never make a second
card (§4); photos of one item stay one item (§5); every photo she shares is on every site's listing (§6); every priced
item goes up on all three sites or she is told why (§7); she only taps (§8); every listing is read back (§6).
- **The direct upload (§2, `api/thrift_api/uploads.py`, `thrift_agent/uploads.py`, `docs/SHORTCUT_UPLOAD.md`):** the
  Shortcut POSTs each photo to `POST /share/<share id>/photo?n=&of=&name=` (a raw image or one multipart file; HEIC and
  EXIF kept; images only, ≤ 30 MB, ≤ 60 a share; the same bytes stored once) and then `POST /share/<id>/end?count=N`
  — behind a key for those two functions only (`provision.py uploads` makes it and sends it ONCE to the private
  chat); the photos are blobs in the function app's storage (container `shares`, gone after 30 days by Azure's rule;
  the rows by `timers.daily`). The Mac asks `GET /shares/pending` every 30 s while awake (`uploads.fetch_new` from
  the inbox scan), downloads each photo (SHA-256 checked), writes `_done.txt`, `_source.txt` ("azure") and `_test`
  for a test share into `inbox/<share id>/` and tells the API (`POST /shares/<id>/fetched`). **One share id names
  both copies** (the Shortcut's `yyyy-MM-dd_HHmmss_<random>`): whichever arrives first is taken; the other is the
  same share (`register`: a folder of a batch already taken is archived without a word, ledger outcome
  `same_share`; `process_batch`: known photos whose items came from that share id are never a "repeat"). `?test=1`
  = a test share: the batch and its items carry `test`; the cards go to the PRIVATE chat marked 🧪 (`approve.card_bot`,
  a tap there works), and the poster, `feed` and `crosslist.pending` never take a test item.
- **The share ledger (§3, `shares.py`, table `shares`, synced to thrift-api's `shares`):** one row per share id —
  source (icloud | azure | both), expected (`done N`) and received, and the time of each step: received → complete
  (taken) → grouped (split) → carded (the first card; the outcome `carded` once every item has its card); outcomes
  `repeat`, `empty` (a day without photos), `same_share`, `joined` (every photo joined an item), `failed` with its
  reason. Recorded before the next step (`_share_seen` on every look at the inbox, `take_share`, `split`,
  `send_item`). The API's **watchdog** (`watch.shares_watch`, a 5-minute timer): a complete share with no outcome after
  15 minutes (30 past 20 photos) while the Mac's heartbeat is fresh → ONE private line, "⏳ Share 12:31 PM (4 photos)
  stuck at grouped since 12:33"; the dashboard's **Shares today** panel; `thrift shares [--day | --all]`.
- **No card from part of a share in its first 30 minutes (§6):** `PARTIAL_AFTER` is 30 minutes (was 15); the card of an
  item made from part of a share says "📷 1 of 20 photos — the rest join when they arrive" (`items.photos_expected`,
  `approve.photo_line`), every card "📷 N photos".
- **Late photos join their item or become items (§5/§6, replaces the private line):** `late_photos` takes the photos
  that came into a SPLIT share's archive folder too — a batch of their own on that folder (`_in_place_batch`: src_dir
  `<archive dir>#hhmmss`, only those names, `late_of` the share's batch; `batch_dir` strips the suffix) — and
  `process_batch` first puts them to the model against the share's items (`reference_items`, `seg.attach`: the items'
  covers and a few photos as A, B, C; one vision call; ATTACH_MIN 0.7): a photo of an item already made is ADDED to it
  (`add_photos_to_item`: normalized into its photos/, the manifest's `late` entries, remembered; an item not yet
  processed takes them when it is; one processed but not on a site has its cover, order and card recomputed
  (`recover_item` asks the front check again for late photos); one on a site keeps its cover and copy, the photos go
  at the end of its lists and ONE "photos" edit per live site puts them on the listing); the rest go on as a share
  (their own cards). Nothing left → the batch `joined`. **A small follow-up share** (≤ 3 photos within 15 minutes of
  the previous share) is compared with that share's items the same way. **A one-photo group** is checked against the
  share's other groups before any card (`merge_singletons`). The grouping prompt: one roll is one item unless the
  photos clearly show different things — never split for the background, the light, the angle, hanger vs flat lay
  vs worn; a photo in doubt joins the most likely item, never a one-photo item. `thrift late --take <batch>`
  (the photos told in a line before WO45, taken now), `thrift late --add-to <item> --file …`, `--dry-run` (the photo
  edits as dry runs first — §11).
- **Photos added to a live listing (§6, `edits.py` kind "photos", `edits.queue_photos`):** Poshmark
  `PoshmarkPoster.set_photos` — the edit page's file input after its tiles (`SEL["edit_photo_input"]`,
  `input.listing-editor__file-list`, UNVERIFIED until the first dry run), each crop dialog applied, the tiles counted,
  Update → List This Item for THIS edit (WO40) once, the public JSON's photo count (`poshmark_listing` now gives
  `photos` and `price`); Depop / Vinted `ExtensionPoster.add_photos` — the extension's "photos" job on the edit page
  (`T.photosAdd`: the tiles must be the `had` recorded, the files set on the page's photo input, the tiles counted
  again), Save once, then `read_photos` (a read-only "photos" job) for the count that stuck. Unconfirmed → read an
  hour later, never saved again blind; 3 failures → one private line (WO43's edit rules).
- **Every listing read back (§6/§7, `photocheck.py`, the poster's housekeeping every 5 minutes, 6 listings a run):**
  a live listing 10 minutes after it posts and then every 6 hours — Poshmark's public JSON, Depop's / Vinted's
  listing page through the extension's verify job (`read_listing`) — `listings.photos_live` / `price_live` recorded;
  fewer photos than sent → ONE "photos" edit with the missing ones (once per count, kv `photocheck_asked`); a price
  that differs (her hand change) → event `price_differs`, never undone.
- **Poshmark's cover dialog for ONE photo** has no "Select a Covershot." title and no thumbnail strip (live, the
  shirt): accepted as the same dialog (`_apply_cover`: the crop frame and Apply still required).
- **Depop's package size** (live: the Polo tee failed three times — this seller's menu offers Medium, Large and
  Extra large only, and "offered: " came out empty because the typed filter had emptied the list): a size the menu
  doesn't offer becomes the smallest offered one that is at least ours, else the largest (`depop.js packageSize`,
  in the "check:" line); the tidy sets back the size chosen, never one the menu lacks; the whole list is reported.
- **The API's safety net for an item priced but not live (§7, `watch.unposted`, the 15-minute timer, 08:00–22:00
  local):** an item priced an hour ago (the mirror's `items.priced`, `status`, `test`; `listings.error`) whose row
  on a site is failed / skipped with no reason the Mac settles itself (no size, a twin, sold, autopublish off, an
  unconfirmed publish, a mismatch) → ONE `repost` task the Mac takes with its take-downs (`delist_tasks.kind`;
  `sales.repost`: the row back to `queued` once, with the same reasons kept) → still not live an hour after the Mac
  answered → ONE private line per item and site, "⚠️ <title> isn't on Poshmark: <reason>".
- **The size (§8):** before any buttons, ONE more look for a printed size on the label, tag, detail, box and other
  photos at the label resolution (`second_look_size` → `cover.read_size_label`; "US 8.5" / "M" is the size,
  "EU 38" the EU size); the extraction prompt looks at the insole, tongue, outsole, box, neck label, waistband and
  care tag and, with nothing printed, gives `size_guess` from the item's proportions next to her hand — the ⭐ only
  (`sizebuttons.model_guess`), never listed by itself; men's and the other women's categories have defaults.
- **Messages (§9):** the private chat gets "📸 N photos of the 12:11 PM share came late — taken now: they join their
  item or become one", "📸 2 late photos joined <title>: its listings get them", the ⏳ and ⚠️ lines above, and ONE
  line a day in the 21:00 hour from the API: "📊 Today: 7 shares → 8 items, 7 live on all 3 sites with all photos, 1
  waiting for a size." (`watch.evening_line`). The group's list is WO29's + WO43's 📭 line; nothing else.
- **The replay harness (§10, `replay.py`, `thrift replay <case> | --golden [--record] [--card]`,
  `tests/test_golden.py`):** a saved share (or several, in order, plus a `late/` folder) through the whole intake in a
  scratch workspace and database — never the live one — with the model's answers recorded once (`brain/llm.Answers`,
  `answers.json`, keyed by the model, the prompt, the tool and the images' bytes: a prompt change needs a new
  recording) and replayed after (a replay never asks the model: `MissingAnswer`), compared with `expected.yaml`
  (`items`, `outcomes`, `each: [{photos, department, size, cover_role, title_contains, size_buttons}]`,
  `said_contains`). The golden cases live in `private/tests/golden/<case>/` (the owner's photos, never the public
  repo): the test runs where that folder is (the Mac's pre-deploy test run) and is skipped in GitHub CI.
- Tests: `tests/test_wo45.py`, `tests/api/test_api_wo45.py`, `tests/api/test_api_uploads.py`, `tests/test_uploads.py`;
  the jsdom tests for the package fallback and the photos job; WO42's and WO37's updated to the 30-minute rule and
  the in-place late batches.
- **Follow-up (the owner, 2026-10-18 evening):** (1) a reply **"price 80"** (or a bare number) to a LIVE item's card —
  its price card or its ✅ card — changes the price on every site it is on through WO41's price-edit path
  (`pricedrop.owner_price`: one `price_changes` row per live site, stage `owner`, the site's price read before the
  change as always, no floor, no 'no drop'; `items.owner_price` and `listings.price` follow; ONE private line when
  the sites show it, never the 🔻 group line); a price that differs on a site is her hand change: recorded
  (`price_differs`), never undone, no message. (2) **The photo check reads the shops in BULK** (`photocheck.bulk_reads`,
  every 6 h): Vinted's wardrobe JSON (`vinted.js wardrobeAll`, 96 a page), Depop's products JSON with the page's
  token (`depop.js shopAll`, the receipts' way; the shop page's tiles when it can't be read) — each listing's price,
  state and photo count when the JSON carries the photos; the first record's keys are logged once
  (`photocheck_shape`). A listing's own edit page is opened only when the bulk data has no count for it or shows
  fewer than were sent (to confirm before any edit), at most once a day per listing and 6 a run; Poshmark's public
  JSON once a day. (3) A share folder gone while the Mac reads it (live 2026-10-12, 6:02 PM) → the batch `failed`
  "the share folder vanished…", the ledger's outcome, ONE private line — never a traceback (`vanished_share`). (4) The
  iCloud copy of a `?test=1` share: the API's word marks the inbox copy `_test` (`uploads._settle_known`); a copy
  taken before that is told; the doc has the test Shortcut save its iCloud copy under `Posh/test/`. (5) **Poshmark's
  Bags size menu, recorded live 2026-10-18 (the Marc Jacobs bag, four failures, then a step-by-step trace):** Women ›
  Bags PRESELECTS its one tile — the menu opens with "One Size" selected (`btn--secondary--burgundy-v2`) and the
  size field reading **"OS"** — and a click on a selected tile toggles it OFF (the menu closes, the field says
  "Select Size"); every repick toggled it back and forth. A size the field shows already is kept (`_size`: Done
  pressed, the tile never touched, `size_menu` "preselected"); a pick that didn't register is picked again four ways
  at most — a plain click, the keyboard (focus + Space), the tile's container, the button's own click
  (`SIZE_REPICKS`; CI's macOS runners: the keyboard press on a tile just redrawn doesn't always register, so the
  plain click comes first) — and the read-back takes the form's abbreviation
  (`SizeShown`, `SIZE_SHOWN`: "One Size" ↔ "OS"). A DRY RUN traces the size menu after each step (`_trace`:
  `<sku>-poshmark-trace-<nn>-<label>.png/.json` in failed/shots — the field, the tiles' classes, the Done buttons,
  the focus); the cover dialog's checkmark on the first thumb is waited for (it came a moment after the 11th
  thumbnail), and a cover dialog still open when the form is left is cancelled first. Stand-in: `oneSizeMenu`.

## The price pager (WO48, the owner, 2026-10-20)
Under the eight price buttons, above [Later] [Change] [Wrong photos]: ONE row "‹ lower · higher ›"
(`approve.item_buttons`, callbacks `prices:<item>:-1|1`), like the size pager. "higher ›" moves all eight buttons up by
$80 on the same card ($20–$90 → $100–$170; `PRICE_SHIFT`, `price_options(price, shift=)`), "‹ lower" back down; $10
steps stay; ⭐ stays on the suggestion whenever it is in view (only the first window has it); "‹ lower" is hidden
when the window below would hold a button under $5 (`can_lower`; the first window never goes under $5 as before). A
shift tap never prices anything (the card is EDITED in place, `refresh_card`; the window kept in kv `price_page` until
the price is set — `forget_price_page` in `_set_price`); a tap on any shown price lists it as before; a typed price
still works. Tests: the WO48 part of `tests/test_wo43.py`.

## Vinted, 2026-10-20: the photo upload, the package size, a silent Upload (the incident)
Six items priced after 23:00 went up on Poshmark and Depop at 08:00; Vinted's first three jobs of the day failed three
different ways and the "3 in a row" brake stopped it for the window; the other three were never tried. The whole
investigation, the timeline and the evidence index: `incidents/2026-10-20-vinted/README.md` (the raw evidence beside
it, git-ignored: `incidents/*/shots/`, `incidents/*/logs/`). What the code does since:
- **Photos (vinted.js):** Vinted's own "Sorry, something went wrong … system error" dialog over the photo area
  (`item-upload-critical-error-dialog`, `selectors.json` photos.error; live: 0 of 8 photos, the fill went on behind it
  and only the read-back caught it) instead of the thumbnails → a fresh page after `T.photosRetryPauseMs` (25 s) and
  the form filled again (background.js hands the job to the new page and passes `job.reloads`: twice at most), the
  last time the step fails with the dialog's words and the upload calls seen; a count short of the files has the
  missing ones attached once more, then the step fails ("N of M photos shown after a second attach") — a publish ends
  there, nothing else filled; a dry run records it and goes on. Before, "0/8 shown" passed as a step.
- **Package (vinted.js `packageSize`):** Vinted's "Recommended" size comes from its server a moment after the price and
  is applied over whatever is chosen then (live: our Medium, clicked 43 ms after the price, read back Large with the
  badge on Large; the same item at 10:23 got Medium recommended and preselected) — the panel settles before the pick
  (`T.quiet`), the pick is watched for a moment after and chosen again once (a note), `check` lets the panel settle
  (≤ 3 s) and compares the package whatever else was fixed (it had only after another fix), and `submit` looks once
  more right before the Upload click — a size Vinted moved meanwhile is chosen again, never published as Vinted's
  (CI's Linux runners showed the recommendation landing after the check).
- **Evidence (common.js `T.watchRequests`):** the site's /api/ calls with their HTTP statuses from a
  PerformanceObserver — the refusal of 2026-10-20 came back with no request at all: `getEntriesByType` reads a
  250-entry buffer a sell page fills before the form is touched — in the photo step's error and the refusal's.
- **Retries and words:** a "Mismatch" row (the form read back other than planned) gets `MAX_ATTEMPTS` windows like any
  failure (WO36 §4 had it wait for ever, one try a window), then `skipped` and the group's ⏭ line naming the step
  (`crosslist.step_text`); the ❌ private line says the step and "attempt N of 3 — the next try the next time the Mac
  wakes or restarts"; the group card says "⏳ Vinted — photos failed, next try when the Mac is next open"
  (`step_name`) for a failed site, "— later" for one still queued; `ext_driver.describe_diff` writes the diff in
  words ("photos 0 of 8 on the form; package LARGE (plan MEDIUM)"). A refused form (WO39) is left to the Mac's own
  re-check by the API's safety net and `sales.repost` ("refused the form" in both KEEP_REASONS; live: the net
  re-queued the Tory Burch before the re-check could run), and the re-check runs on a site stopped by failures in a
  row (`parallel._cross_turn`: `account_blocked`, not `blocked` — it is a read).
- **Brands:** a company suffix is one word however the site writes it (`brands.SUFFIX`, the same in `common.js`):
  "Thursday Boot Co" = "Thursday Boot Company" (live: Poshmark's Brand left empty, Depop "Other"); the alias too.
- **The pause:** `marketplaces.vinted.enabled: false` in the Mac's `config/settings.local.yaml` (the owner's OK,
  2026-10-20, a backup in `~/thrift/var/`) until the backlog went (lifted 11:35 the same day: the six Vinted rows
  held — `skipped: not published automatically - held for the Vinted backlog…`, a reason both safety nets keep — and
  freed one at a time with `thrift requeue <item> --marketplace vinted`, 2–5 min apart; all six live by 12:07); the
  Thrift Chrome relaunched that morning (Chrome 155; the Mac's memory is the owner's own Safari tabs, not ours).
- **Depop, the same evening (the leggings i_261020_88d284, 2 photos):** two Depop failures on one item. 13:01 —
  Depop's own package choice (Medium) landed AFTER the tidy's set-back and the form read back Medium for our Small
  (`Mismatch: package Medium (plan starts with Small)`): the tidy looks at the package once more after its settle,
  and `submit` takes one last look right before the Post click (as Vinted's) — a size Depop moved is chosen again.
  18:20 — the job began 20 s after the Mac woke (the worker's Telegram poll had just failed with a DNS error) and 0
  of 2 photos showed in 110 s while the fill went on for 137 s: Depop's photo step re-attaches the missing files once
  and then FAILS the attempt with the upload calls seen (`T.watchRequests`), nothing else filled; and the first
  listing after a wake waits until `parallel.AWAKE_GRACE` (45 s) have passed since it (`settle_after_wake`, event
  `awake_grace`, every site). Stand-in hooks `loseFirstAttach` / `loseAttaches: N` in both fixtures.
- **Two lanes in the Telegram queue (the same day, 11:37–12:55 live):** a 🧪 test share's card (WO45 §2, the private
  chat — the WO48 pager test's) left open counted as THE open message, and her two real shares (9 items, priced by
  11:46) got no card for an hour and a quarter (the API's ⏳ line said "stuck at grouped"). `approve.lane_of`: a
  test batch's or item's messages are the "test" lane, everything else the "real" lane (the group's); `queue(db,
  lane)`, `next_up(db, lane)`, `open_message(db, lane=)` look at one lane only, `pump` / `resend_pending` serve each
  lane on its own (one open message per lane), `processing_order` takes both; `daily.work` and `thrift status` count
  the group's lane. **A queue that can't send says so** (`approve.note_send_failure` from `cli._safe_pump`: past 10
  minutes of failed sends — Telegram unreachable, a DNS failure — ONE private line "⚠️ The next card couldn't be sent
  for N min (…) — M waiting; I keep trying every turn.", cleared by the next send, `note_send_ok`). The owner, that
  day: never a silent queue he has to notice himself. Tests: the 2026-10-20 part of `tests/test_queue.py`. Stand-in hooks `photosError`, `loseFirstAttach`,
  `recommendLate`, `recommendAfterPick` in `tests/fixtures/ext_vinted_new_item.html`; tests: the 2026-10-20 part of
  `tests/js/content.test.mjs`, `tests/test_crosslist_flow.py`, `tests/test_never_ask.py`, `tests/test_wo45.py`,
  `tests/api/test_api_wo45.py`, `tests/test_catalog_answers.py`.

## The brand is asked, the database never waits on a line, one New (Alex, 2026-10-27, the afternoon's shares)
Five shares, 16 items: 9 live on all three sites, 4 waiting for her price; three went wrong, all fixed:
- **The brand rule** (Alex, the same evening — it narrows the question below to case 2):
  1. **No brand to be seen** — no photo of a brand label, tag or logo and the model sees no brand, or the brand
     unknown or unreadable with nothing suggesting a known brand → listed with NO brand, never asked: Poshmark's field
     empty, Depop "Other", Vinted "List without brand"; the gate's note `NO_BRAND_NOTE`, and the item's "Check —" line
     (private) says "listed with no brand (no brand label, tag or logo in the photos)" once (`crosslist.LISTED_NO_BRAND`;
     Depop's "(no brand)" and Vinted's "listed without a brand" notes folded into it).
  2. **A brand there but unreadable** — a brand label, tag or logo that is blurry, cut off or ambiguous, or a
     recognizable logo or design of a known brand without a readable label → the one question (below).
  3. **A brand read confidently** (≥ 0.70) → used, never asked.
  The model says which (`facts.brand_seen`: read | unreadable | logo | none — the extraction prompt's "brand_seen,
  always"; a size or care label without a brand name is no brand label); facts read before it are judged by what they
  hold (`gate.brand_seen`: a reading → read, its confidence decides; a photo cited for the brand without one →
  unreadable; nothing → none). A low-confidence reading always asks (something suggests a brand). Applied once to the
  items waiting then (`pipeline.apply_brand_rule`, kv `brand_rule_applied`, at the worker's start). Tests:
  `tests/test_brand_rule.py`.
- **The brand question** (case 2 above; Alex: "if the agent struggles with the brand and a platform needs one, ask her
  to type it"). An item whose gate asked the brand (`approve.asks_brand`) and
  that she hasn't answered (items.owner_brand: a name, or "none") goes up NOWHERE once priced (`approve.needs_brand`;
  `runner.approved` is false, `crosslist.pending` skips a row a safety net re-queued): the price's button says "✓ $95 —
  needs the brand: reply with it (or 'no brand')", and ONE group message asks — "🏷️ What brand is <title>? I couldn't
  read it, and Depop and Vinted need one — reply to this message with the brand, or 'no brand'." (`ask_brands`, outbox
  kind `brand`, once; also from the window's tick). Her reply (`_reply_brand`): a name → `pipeline.set_brand`, 'no
  brand' → `set_no_brand` — the item lists on all three. An item already live on a site (before this rule) gets her
  answer for the sites still waiting only (`pipeline.brand_for_waiting`); its rows that failed, skipped or were queued
  meanwhile say "waiting for the brand (was: …)" — both safety nets keep that reason — and go back in line
  (`release_brand_wait`). The 💤 line names it ("waiting for a brand: …", `daily.BRAND_WAIT`). A brand read well, or a
  label the model is sure isn't there, never asks. Live: the pink biker shorts (no brand) went up as Depop's "Other"
  and failed on Vinted.
- **Vinted's "List without brand"**: its brand panel had stayed shut after the one click (the pink shorts: the field
  "Select a brand", no rows), so the option never showed though Vinted still has it (the Denim Forum's open menu, the
  same afternoon: "No brand indicated › List without brand"). `vinted.js open` clicks again (3 times at most) until the
  panel shows; `noBrandOption` re-opens it if needed, scrolls the list to its end and searches "" then a word no brand
  has (Vinted ends a search's rows with the option); a miss keeps a `menu-no-brand` picture. Stand-in hook
  `brandOpensLate`.
- **"database is locked"** (14:03 EDT: the Poshmark worker while recording the Reformation jeans it had just
  published — the row stayed `posting`, the listing live — and the worker's Telegram thread at 14:04):
  `process_item` said its private "No full photo …" line INSIDE its write transaction; notify opened a NEW connection
  per line, whose opening runs the schema and the migrations (writes), and waited 30 s on its own caller's lock while
  the item's transaction held the database — 155 s for that item. Now: the line is said after the commit; a Telegram
  line said inside ANY write transaction waits for its commit (`db.in_transaction`, `db.after_commit`, `notify._later`;
  dropped on a rollback); notify keeps one connection per thread (`notify._db`); a Poshmark turn that failed looks for
  a row it left `posting` in the closet at once (`parallel._poshmark_worker` → `runner.reconcile_stale`), not only at
  the poster's next start.
- **", New with Tags" says New once** (`titles.fix`): every other standalone "New" outside the brand goes, an existing
  tail isn't doubled (live: the Levi's 501 "… Jeans New, size 26, New with Tags" — 'repeated: New' — was held on every
  site); a brand's own "New" (New Balance, New Look, New York & Company, New Era) is never a repeat (`NEW_BRANDS`).
- Tests: `tests/test_brand_wait.py`; the jsdom "brand panel that doesn't open at the first click".

## The Telegram link (Alex, 2026-10-27)
Measured on the Mac over 14 days (`events` kind `error`, the logs, a live probe): 4,868 failed `getUpdates` polls —
DNS 3,144 (the Mac's first resolver is Tailscale's MagicDNS 100.100.100.100; it stops answering around wakes and
network changes, in bursts on 10-13, 10-14, 10-18, 10-23), no network 457 (the night's dark wakes), a captive portal's
self-signed certificate 707 (one travel day, 10-18), read timeouts 336 (a poll in flight when the Mac sleeps), resets
in the TLS handshake 204 — the path to Telegram itself: a live probe from the Mac had api.telegram.org reset 1 time in
60 and ~3× slower (Amsterdam) while Apple and Anthropic, through the same router, never were. So: the Mac (DNS, sleep),
not the router; Telegram's path for the resets. Lost before this: 9 private / plain lines (4 dropped while the ops chat
was marked down, 5 failed outright — 2 of those timed out after going out and may have arrived); cards (the outbox,
WO37) were never lost; nothing went twice because of the network (two "item" rows were the [Change] prompt by design;
the robe's ⏭ line went twice on 10-24 because the API's repost re-queued it — now said once).
- **One client per process** (`telegram.client()`: httpx, connections kept 15 s — the ~15 s poll reuses its TLS
  connection, no DNS look per call — with TCP keepalive: SO_KEEPALIVE, idle 10 s, interval 5 s, 3 probes, each where
  the OS takes it, `keepalive_options`). The one network seam: `telegram.http_post`; every call goes through
  `telegram.post` (the Bot's `call`, `notify._send`).
- **Retry with back-off:** a request that never left the Mac (ConnectError — DNS, no network, a reset handshake —,
  ConnectTimeout, PoolTimeout, a failed write) is tried again 1, 2, 4 s later (`RETRY_PAUSES`); one that went out with
  no answer (ReadTimeout, RemoteProtocolError, ReadError — WO37's `maybe_delivered`) never is.
- **DNS pinned while it fails:** a DNS failure (`dns_failed`: socket.gaierror or its words) sends the next try to
  api.telegram.org's last known address (`_ADDRESSES`, learned from DNS every 10 min after a good call; seeded with
  149.154.166.110 / 149.154.167.220) with `Host: api.telegram.org` and the TLS name check on that name
  (`sni_hostname`); for a minute after, calls go there first (`DNS_DOWN_FOR`).
- **The long poll stays under 30 s:** `telegram.poll_timeout` (15 on the Mac) capped at 25 (`cli.POLL_TIMEOUT_MAX`);
  the HTTP read waits the poll + 10 s.
- **The poll's back-off and one event per outage** (`cli._safe_poll`): a failed poll waits 5, 10, 20, 40, then 60 s
  (`poll_backoff`; was 5 s every time, all night); `telegram_down` {kind, why} at the outage's first failure,
  `telegram_up` {down_s, failed_polls, kinds} when a poll works (`telegram.failure_kind`: dns, offline, tls, reset,
  timeout, http) — no traceback event per failed poll any more; a non-network error in the poll is still a traceback.
- **Nothing lost:** a private or group line (`notify.ops` / `group` / the photos) whose send never left the Mac is
  HELD (kv `telegram_held:<when>:<pid>`, one row each, 30 at most, 48 h) and sent — "(held back since 3:12 PM:
  Telegram couldn't be reached)" — by `notify.flush_held` after the next good poll or the next line that goes out; a
  row is claimed (deleted) before its send, so the worker and the poster never send one twice; one that may have
  arrived is never held. A refusal (Telegram answered no: the ops chat not /started) is dropped as before. A ✅ card
  that never left is not "announced" (event `card_not_sent`): the poster's sweep sends it (`crosslist.announce` logs
  `posted_announced` only after the send; one that may have arrived counts, outbox message id 0). The same ⏭ "wasn't
  listed" line for an item and site is said once while it is open (`runner.ask_skipped`).
- Not ours to change: Tailscale's DNS on the Mac (MagicDNS first) — `tailscale set --accept-dns=false` would make the
  router's resolver first; left as it is (the pinning covers it).
- Tests: `tests/test_telegram_link.py`; `tests/test_telegram.py` and `tests/test_notify.py` on the new seam.

## Shop-window titles every site accepts; dual sizes always map (WO50, 2026-10-24)
The owner's rule: "imagine a shop window downtown: people walking past read the label" — short, natural, nice to read;
no shouting capitals, no codes or SKU-like strings, no odd letters; never dropping what matters (brand, what it is, the
one feature that sells it, colour, size). Live: Vinted refused "BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted
Strap" ("Title contains too many capital letters", POST /api/v2/item_upload/items 400); a robe labelled "S/M" was
skipped on all three sites though every menu had M.
- **One checker, one fixer** (`thrift_agent/titles.py`; every site's rules in `data/site_rules.json`, §3): `check`
  names what breaks a rule — an all-caps word of 4+ letters that isn't an acronym (the allow-list: UGG, H&M, DKNY,
  NYDJ, BCBG, ASOS, XL…), capitals above 35% of the letters, a code (letters and digits mixed, "RN 54867", "SKU…", our
  item ids, 5+ digit numbers; sizes like 34DD / W32 / 2-Piece stay), a repeated word, "size size", condition words but
  "New", HTML artefacts, emoji / odd characters, a slash, over the site's length. `fix` repairs all of it
  deterministically, the brand written as a shop sign writes it (`data/brand_display.json`: BCBGMAXAZRIA → BCBG Max
  Azria, FARM Rio → Farm Rio, MNG (Mango) → Mango; else an all-caps word → a capital first), fitted to 80 without
  dropping the brand, the size, the set phrase or the colours (premium.fit_title). **NWT** (the owner's example):
  "BCBG Max Azria Black Slouchy Shoulder Bag Knotted Strap, New with Tags" — ", New with Tags" at the end when it
  fits; NWOT keeps "New" after the brand.
- **Vinted's capital limit, from its own answers:** it accepted all 70 of our titles up to 29% capitals ("FRAME Le
  High Straight Jeans …") and refused 38%: the limit lies above 29% and at or below 38%. The fixer aims for 29% or less
  (`caps_share_safe`: connecting words lower-case); the checker blocks only above 35% (`caps_share_max`), so real
  acronyms are never held; after Vinted's own refusal the retry goes further (sentence case beyond the brand and
  acronyms).
- **Where it runs:** `titles.shop_window` ends `premium.title_with_feature` (every title, every path); the copy
  prompt carries the shop-window rules and examples from this closet (golden answers re-recorded); a title still
  failing gets ONE model rewrite in the pipeline (`pipeline.shop_window_title`, `titles.rewrite`); Vinted's title and
  Depop's description first line (`catalogs.vinted.vinted_title`, `map_depop`, `crosslist.strict_title` at mapping)
  and Poshmark's title before the form opens (`runner.shop_window_render`: an item processed before WO50 fixed there)
  — a title that still breaks the rules never reaches a site (the item waits; one private line). Descriptions get the
  shop-sign brand too (`titles.display_in_text`). The owner's own title (WO27) stays hers.
- **A site's fixable refusal is fixed and retried once by itself** (`crosslist.autofix_refusal`, the refusals in
  site_rules.json: Vinted's "too many capital letters", "title too long", forbidden characters): the row back in line,
  its title stricter at the next mapping (`strict_level`), no 'retry' asked; posted → ONE private line "✓ Vinted had
  refused … the title was fixed and it is live: …"; refused again → the usual ⛔ line, never a loop.
- **The live titles reviewed once** (`thrift titles --review --send`): ONE private message, each title that breaks a
  rule old → new; the owner's reply **"go titles"** (`approve._route_ops`, `titles.apply_review`; `thrift titles
  --apply` its twin) queues a **"text" edit** per site (`edits.queue_text`, kind `text`): Poshmark `PoshmarkPoster.
  set_text` (the edit page's title / description, Update → List This Item for THIS edit, the public JSON read until it
  shows the title), Depop / Vinted `ExtensionPoster.set_text` (the extension's "text" job, `T.textSet`: Depop's title
  is its description's first line; its blank edit-page fields refilled first), Save once, read back (`read_text`).
  Each payload carries the text the site should show now (`expect_title` / `expect_first_line`, and
  `expect_description` wherever a description is typed — on Depop always, its whole description is typed again): a
  title or description she changed by hand is left as it is ("changed by hand? Left as it is."). The records follow
  (`edits._mirror`). A dry run is kept in the event log too (`listing_edit_dry`, what the page showed): the private
  chat may be out of reach. §0 ran as dry runs on all three sites first (2026-10-24), then for real.
- **Dual sizes always map** (`post/fallback.menu_size`, one order for every site): 1. the exact value; 2. the bigger of
  a dual label ("S/M", "M/L", "XS/S", "0/2", "7/8", "8-10", "One Size (S–L)" — `dual_parts`, `bigger_of`); 3. the
  nearest in the same system, one step at most; 4. One Size only for a one-size item (robes, scarves, bags, hats…).
  At processing `pipeline.settle_dual_size` lists the bigger size; the description says "Size S/M (fits S–M)."
  (`copy.ensure_label_size`), the card "Size M (label S/M)", the size buttons "S/M → M" as one choice; Depop's and
  Vinted's mappers take the bigger too (`catalogs.sizes.undual`). A size no menu value fits: ONE private line naming
  the size and the menu (`crosslist.say_size_skip`), the item posting on the other sites — never the group's 'retry'.
- Tests: `tests/test_wo50.py`, the WO50 part of `tests/test_poshmark_form.py` and `tests/js/content.test.mjs`.

## A missed sale, a stuck share, a false stall (WO49, 2026-10-24)
Live: the FARM Rio dress sold on POSHMARK on Oct 23 and stayed on Depop and Vinted for a day; her two shares of the
morning waited 71 minutes; the worker was "restarted for a 21-hour stall" the moment the lid opened. Causes and fixes:
- **Poshmark changed its sale subject** (since Oct 21: `<title> just sold to @x on Poshmark!`, no quotes) and the
  REAL rule demanded the quotes: three Poshmark sales filed as OTHER (the dress; two hand-listed items). Now either
  form is a SALE, the title read from the unquoted subject (`emails.REAL`, `TITLES`); "just sold to" is in `SOLD`.
  **Poshmark's "Reminder to ship your Poshmark order: "<title>""** is a SALE too (`REMINDER_SUBJECT`): deduped
  against the sale it is about (`core._reminded_sale`: same site and title, ≤ 14 days), else the sale itself — ship by
  its own day, the private chat told ("💰 Found by Poshmark's reminder to ship it: …"), take-downs as usual.
  **An email that still says "sold"** and that no rule took (not a like, an offer, a drop, a promotion:
  `emails.maybe_sale`) → ONE private line per email ("❓ A Poshmark email may be a sale I didn't recognise: …").
  **The ignored emails are read again** by today's rules every 15 minutes (`core.reclassify`, the last 7 days, by
  their subject — an OTHER email's text is never kept): processed as LATE — take-downs as for any sale, "💰 Found late
  (its email wasn't recognised): …" to the private chat, never a days-late group line. WO40's 2-hour net runs from when
  a sale found late (recorded ≥ 1 h after it sold, `FOUND_LATE`) was recorded, so its take-downs get their chance.
- **The sale net that needs no email** (`photocheck`, the poster's housekeeping): a listing we hold `posted` that its
  site shows sold or no longer for sale — Poshmark's public JSON (`inventory.status`: sold_out → sold; not_for_sale or
  404 → not for sale), Depop's / Vinted's shop data (sold / closed; hidden is never acted on) — goes to thrift-api
  (`sales.listing_gone`: `sold_found` with THAT site, or `not_for_sale` as WO33's go-live check did) → take-downs
  everywhere else; the row becomes `sold` / `delisted`. A take-down of ours for the item means it was us: the row only.
  Poshmark's listings are read every 2 hours (`POSHMARK_EVERY`, was a day). The site saying SOLD acts at once; "not for
  sale" or a 404 only when seen again 10+ minutes later (kv `photocheck_gone_seen`; a for-sale reading between forgets
  it): one odd answer never takes anything down. And a listing row `delisted` or `sold` is never posted again by the
  queue (`runner.next_job` skips them, as it skips failed and skipped ones — found while testing this WO).
- **The Poshmark order sweep never saw a new sale**: the sales JSON calls a just-sold order **"Sold"** (the page
  draws "Awaiting Shipment"; recorded by the read-only probe of 2026-10-24) and that word wasn't in
  `orders.POSHMARK_STATUS` — every fresh sale was dropped as "no known status"; only shipped / delivered / complete /
  cancelled orders ever reached the API (the Alo set's order appeared only once it shipped). "Sold" → to_ship now.
  The reader also takes every sales answer the page loads (`MORE_ANSWERS`), rows merged by id.
- **The site it sold ON holds it as sold** (2026-10-27): GET /tasks also answers `sold_on` — each sale's item and
  site (`core.sold_sites`, the same 30-day live window as `sold`) — and the Mac marks that site's own `posted` row
  `sold` (`sales.mark_sold_here`, event `listing_sold_here`). Live: a sold Depop listing just leaves Depop's shop data,
  so the photo check never saw it sold — the UGG Chestnut's (sold 10-16) and the Nike tee's (10-22) Depop rows had
  stayed `posted`, and "go titles" tried to edit the Nike's sold listing (Depop: "There was a problem editing your
  product"). Poshmark's and Vinted's sold listings were already marked by the photo check.
- **"Gone already" keeps the rows true:** a take-down that finds the listing gone marks it `delisted` here too (the
  dress's and the Alo set's Depop rows had stayed `posted`); `sales.repair_gone` fixes such rows at the poster's start.
  **Depop's delete judged by the address:** a job that ends in an error (Depop reloads the page as it deletes — "the
  page reloaded during the take-down") is done when the address, live before, is gone now (`ExtensionPoster.delist`).
- **One stuck iCloud folder never blocks anything** (the morning: `2026-10-24_093927_7777` in EDEADLK made every look
  fail; `ready_folders` dropped the share it had already found ready with it): `ready_folders` and the takes go ONE
  FOLDER AT A TIME (`_folder_unreadable`, `cli._take_one`: busy → `alerts.busy` per folder, quiet, the ops chat once
  after 10 minutes; anything else logged), retried on the next looks.
- **The direct uploads never touch iCloud:** fetched into `paths.work/_uploads/<share id>` (the Mac's own disk,
  `pipeline.uploads_dir`, `upload_folders`), taken BEFORE the iCloud inbox is listed and whether or not it can be
  (`cli._scan_inbox`), ready at once (whole by construction), archived to `_uploads/archive/` (WO47's 7 days, the
  cleanup's archive rule); the iCloud copy is deleted at once when it can be — when iCloud holds it, the upload never
  waits (`share_icloud_copy_left`), and the copy, once readable, is the same share by its id (`pipeline._taken_as`
  in `register`: never a second batch).
- **The watcher watches progress in awake time** (`alerts`): the Mac's monotonic clock (mach_absolute_time — it
  stops while the Mac sleeps); progress = a FINISHED look (`scan_done`), a job worked, an upload fetched, or a turn
  with the lid closed (`idle`: nothing is owed then); a look that iCloud cut short for the whole inbox keeps the loop
  alive (`look_ended`) without moving `thrift status`'s last scan. The night is never a stall. **From outside too:**
  the poster's housekeeping (`alerts.WorkerCheck`) restarts the worker through launchd (`kickstart -k`) when kv
  `worker_progress` hasn't moved for 20 minutes of its own lid-open time — one private line an hour.
- **thrift-api watches the uploads**: an upload the Mac hasn't fetched 10 minutes after its last photo while the Mac's
  heartbeat is fresh → ONE private line, "⏳ Share 9:39 AM (20 photos) is in Azure but the Mac hasn't taken it (since
  9:40 AM)" (`watch.uploads_watch`, the 5-minute timer); the heartbeat carries progress (`cli.beat_info`: the last
  finished look, `worker_progress`, the upload poll, the cleanup).
- **The extension watch counts lid-open time only** (`Bridge.watch_tick`: the clock starts again when the window
  opens): 2 min → start the Thrift Chrome, 5 → restart it (`runner.restart_thrift_chrome`: running with its extension
  asleep), 10 → one private line.
- **Messages:** the daily cleanup sends no line of its own — the 9 PM summary ends "🧹 Cleanup freed 270 MB." (from the
  Mac's heartbeat). The 8:57 burst was the lid opening after the night: the false restart and the extension line are
  gone; "⏳ … don't close the Mac" is the ops status message, as WO29 has it.
- **Two more stuck items of the day:** a label's two sizes ("S/M", "M-L" — the Bath & Body Works robe, skipped on all
  three sites) are listed under the bigger one, as a set's (`pipeline.settle_dual_size`), "Label size: S/M." in the
  description (`copy.ensure_label_size`); Vinted's title loses a shouting word when capitals are more than a quarter
  of its letters (`catalogs.vinted.vinted_title`: "BCBGMAXAZRIA" → "Bcbgmaxazria" — Vinted refused "Title contains too
  many capital letters").
- **`thrift doctor`** (`doctor.py`; a reply "doctor" in the private chat): the worker's progress, the iCloud inbox /
  local intake / Azure queues, the poster and the extension, each site (listings, stopped for the window), the
  take-downs still open and their age, today's shares and their outcomes — one screen, read only.
- Tests: `tests/test_wo49.py`, `tests/api/test_api_wo49.py`; WO42's watcher test rewritten for awake time; the upload
  tests land in the local intake; the extension watch's timings.

## Depop's package size is a rule, not a timing (2026-10-22, the owner: "fix it finally")
The dress i_261021_93bd13 was the third Depop listing in five days stopped by the package race (the Polo tee ×3 on
10-17/18, the leggings 10-20, the dress 10-21: `Mismatch: package Medium (plan starts with Small)`, nothing published,
the ❌ line). Depop suggests a package size by itself once the photos are up and AGAIN after any change to the chips or
the brand (the tidy's own), and its choice can land after any look of ours — ea91fb7's three timed looks didn't win.
- **The rule** (`depop.js packageAtLeast` / `packageBelow`, `ext_driver.PackageShown`): Depop's own choice is KEPT
  when it is at least ours by Depop's order (Extra small < Small < Medium < Large < Extra large) — its shipping estimate
  for the item; a maxi dress isn't under 8 oz (our "light" class → Small); ours goes in only when the field is empty or
  Depop's is smaller (then as WO45: the exact size, else the smallest offered that is at least ours, else the largest).
  The tidy's two looks and `submit`'s last look set back only a size BELOW ours (twice at most before the click, a
  settle between), never a bigger one of Depop's.
- **Never a failure:** the driver's read-back takes ANY of Depop's five sizes (`PackageShown.matches`); the plan (or
  WO45's `package_choice`) is only the floor. A size still below the plan at the click goes as Depop's own, said in the
  row's note through the page's `note` event ("package 'Medium' at the click, below the plan (Large) — Depop's own
  choice; check the label when it sells" → the owner's "Check — <title>:" line); a dry run notes it from the read-back.
  An EMPTY package before the click that can't be set is the one error left (Depop would refuse the form anyway).
  Routine (never in the "Check" line): "package: Depop's 'Medium' kept (ours would be Small)", "Depop's package size
  'X' before ours is below ours: Y chosen", "Depop changed the package size to 'X' (below Y): set back".
- The bridge hands `note` events to the job's listener like steps and progress (`ExtensionPoster._listen`: the row's
  notes, once). Vinted's package rule (10-20: ours over its Recommended, set back before the click) is unchanged.
- Tests: the 2026-10-22 part of `tests/js/content.test.mjs` (kept, late before the read-back, below ours before the
  click — chosen again / posted as Depop's with the note), `tests/test_bridge.py` (the read-back, the note event).

## Photo storage (WO47, the owner's policy, 2026-10-19 — `thrift_agent/cleanup.py`, `thrift cleanup`)
Where a share's photos live: her iCloud Drive inbox (the Shortcut's copy), thrift-api's blob container `shares` (the
upload), the Mac's work folder (`work/<batch>/item_NN/photos` + cover — THE copy kept; `work/<batch>/all` the prep
copy), and the archive `Posh/archive/<date>_<share id>` (the share folder moved out of the inbox — inside iCloud Drive,
so it counts against her iCloud storage; it stays there because iCloud lands a share's late photos in the renamed
folder, WO43). The rules: (1) **no duplicates, ever** — the second copy of a share (the iCloud folder after the upload,
or the other way round) is DELETED from the iCloud inbox the moment it is recognised (`cleanup.delete_copy` from
`uploads.take`, `pipeline.register`, `pipeline.repeat_share`; event `share_copy_deleted`; iCloud still holding it →
the next look), never archived (WO46's `set_aside` is gone; the daily run sweeps iCloud's "<id> 2" folders, a folder
that came back, and the "-icloud" folders set aside before); (2) **Azure**: a share's blobs are deleted when the Mac
says it has the share (`POST /shares/<id>/fetched` → `uploads.mark_fetched(…, blobs)`; the rows stay; the 30-day
lifecycle rule is the backstop); (3) **items for sale keep ONE copy**, the work copy: the archive copy of a split share
goes KEEP_DAYS (7) after the share once every item has its work copy (late photos land within 2 days; `redo`, [Wrong
photos] and `requeue` need the folder — a week covers them), `work/<batch>/all` goes as soon as the share is split;
(4) **sold items**: the work photos (photos/, cover.jpg) go as soon as thrift-api's sale shows shipped (`shipped_at`)
or done (`GET /sales?state=all`; never with a listing still queued / posting); (5) **repeat, empty, failed and test
shares**: archive and work folders after 7 days; (6) the photo memory (fingerprints) is untouched; (7) `cleanup.run`
once a day from the worker's inbox look (`cleanup.tick`: `sales._claim` 24 h), by hand `thrift cleanup [--dry-run |
--sizes]`; everything removed is in the event `cleanup` (path, kind, why, bytes) and ONE private line "🧹 Cleanup:
freed 1.6 GB — 9 share folders older than 7 days (archive), 2 duplicates, 1 sold item's photos, 12 prep copies.";
kv `cleanup_last` in `thrift status` with the disk; (8) **the evidence** (`failed/shots`, Alex, 2026-10-20): files
older than 14 days (`EVIDENCE_DAYS`) go — except an item's while a problem of it is still open (`cleanup.open_problems`:
a listing queued / posting / failed, or skipped for a reason that still waits — never "shared again", "already live",
"sold", "not published automatically"; an item failed or parked; a price change, a listing edit or a take-down not
done; an unconfirmed publish) — one Removal per item (its files; a file is tied to an item by its name, `<item id>-…`
or a Poshmark listing id of ours), the kept ones said in the plan's notes, the line "… 123 evidence files older than
14 days (11 items)". Never touched: a share in flight, a work copy an item still needs (the archive copy is then
kept, said in the plan's notes), the database. **The
disk** (`cleanup.disk_watch`, every 10 min from the same tick): under 5 GB free → ONE private line naming thrift's
own folders; nothing is said above it (Alex, 2026-10-27: was 20 GB with a "free again" line — the Mac lives at ~13 GB);
back above 6 GB the episode ends silently (event `disk_ok`); `thrift status` shows "disk: X free of Y". Tests:
`tests/test_cleanup.py`.

## Her first real upload shares (WO46, 2026-10-19)
The upload Shortcut went live on her phone that evening (no test run). It saves the iCloud copy FIRST, then uploads, so
the iCloud folder reaches the Mac before the upload ends — usually with its photos cloud-only or short (live
2026-10-17: the folder and its `_done` at once, the photos never). Before this, `uploads.take` saw any folder of that
share id and threw the upload away (`_settle_known`): the upload never helped. Now (`uploads.icloud_behind`): an iCloud
copy in the inbox, not taken yet (no batch on it), and not whole — cloud-only files, no end mark, or fewer photos (with
their companions) than the Shortcut counted — gives way to a WHOLE upload (ended, received ≥ count): it is moved aside
to `archive/<date>_<id>-icloud` (`set_aside`, a rename inside iCloud Drive; event `share_icloud_set_aside`; never
taken, never watched by `late_photos`, skipped by `known_folder`) and the upload goes into the inbox (beside a folder
iCloud put back, as `<id> 2` — `free_name`; `shares.share_id_of` reads "<id> 2" and "-icloud" as the share). A whole
iCloud copy, one already taken, or a short upload: the iCloud copy is the share, as before. A move refused (iCloud
coordinating) leaves the download staged and the share pending for the next poll. Tests in `tests/test_uploads.py`.

## Size buttons, the photo memory, no "share it again", a listing's department changed (WO43, 2026-10-17)
That evening: three items shared at 3:18 PM stayed on the iPhone (5G); WO42's "share it again" line went out; the
re-share at 7:26 PM and the phone's late upload of the first share both landed at 7:47 PM — six folders holding three
pairs of byte-identical photos (SHA-256), six cards, three repeats (the On sneakers' first card was retired by the
twin rule when the second got its price; the tee's and the jacket's repeats were retired and deleted by hand, §0).
And the On Cloudsurfer Max sneakers — men's — went up under Women › Shoes › Sneakers on all three sites.
- **Size buttons** (`sizebuttons.py`; the owner: shoes almost never show a size, she taps, never types). An item that
  takes a size with none read: the card's line is "📏 No size on the photos — tap it below (⭐ = my guess)" and its
  buttons start with the size rows — Poshmark's own menu for the category (`taxonomy.size_menus`), kept to what Depop
  and Vinted take too (`crosslist_ok`: their catalogs' mappers; WO36: no made-up value) — eight in two rows of four
  (shoes in half sizes), letters all in one row (a mixed menu: the letters first, the numbers behind "bigger ›"),
  ⭐ on the guess (the shop's history for the brand and category, else the category's usual size, else the menu's
  middle), "‹ smaller · bigger ›" when there are more (`sizes:<item>:±1`, the window kept in kv `size_page`), a shoe of
  a brand that marks sizes in EU (`EU_BRANDS`) shows "8 · EU 38.5"; right above the price rows (`approve.card_buttons`).
  A tap (`size:<item>:<value>`, `approve._tap_size`): the size of THAT item, no model call (`pipeline.set_size`, noted
  in the gate doc as `size_tapped`), the card EDITED in place — "📏 Size 8 ✓", the button "✓ 8", the title's size, the
  rows kept so another tap changes it until the item starts posting (a tap on a posting / retired item changes
  nothing; the same size twice does nothing twice). Size and price in either order: the item is queued when it has
  both; a price first leaves the card with its size rows and one inert "✓ $25 — tap a size to list it" (the queue moves
  on). The typed `size …` still works; the private line says "once a size is tapped on its card"; WO39's "Waiting for a
  size" line says "tap one on its card (or reply 'size …' …)". The guess never goes on a listing by itself.
- **The photo memory** (`photomem.py`, table `photos`, 30 days): every photo of a share is remembered with its item as
  the share is taken (`pipeline.split`; burst photos dropped at dedupe with the batch), known again by its bytes
  (SHA-256), its capture (EXIF DateTimeOriginal + SubsecTimeOriginal + Model + pixel size — checked on the archived
  shares: all of it survives the Shortcut, a HEIC→JPEG, a rename, a new folder) or, with no EXIF, a near-identical
  picture (phash ≤ 2 and the average colours within 10 — two different items on the same carpet never match). The
  check runs in `process_batch` before grouping and before any model call: every photo known → a repeat share (the
  batch `failed` "repeat share: same photos as …", archived, nothing to the group, ONE private line per look "🧹
  Repeat share skipped (7:42 PM): same photos as <title>." — `photomem.say_skipped` from the scan); some known → they
  are dropped, and when they all belong to one item the new ones are its late photos (WO37's line, no card), else the
  new ones go on as a share of their own. A second guard in the outbox (`photomem.card_blocked`, from `send_item`): no
  card for photos that had one in the last 30 days or for a retired item — the item is retired, one private line.
  `thrift photos --forget <item>` clears an item's photos (a re-share is then processed again).
- **Late photos, never "share it again"** (replaces WO42's lost line): an empty share folder means the Shortcut ran and
  the photos are still on the iPhone. 30 minutes with no camera photo (`share_state` "empty") → ONE group line, once
  per share: "📭 Photos from your 7:31 PM share are still on the iPhone — unlock it on Wi-Fi and they'll come through.
  No need to share again." (several in one look: one line naming the times; `say_waiting`, kv `share_waiting_said`);
  still nothing after 3 hours → ONE private line, never a second group line (a folder first seen that old gets the
  private line only). Cloud-only placeholders are pulled as before (WO42), private lines only. The folder is NOT moved:
  it waits where it is for 24 hours (`EMPTY_ARCHIVE_AFTER`, "stale" → archived with a note, the failed batch), never
  holding up other shares. A folder that fills after being archived is taken where it is — a new batch on the archive
  folder itself (`late_photos`; the old batch learns the names so nothing is "late" twice), never copied back into the
  inbox. A share whose count is covered waits only for its mark's `min_wait_seconds` — never "partial" (live: "5 of 5
  photos arrived").
- **The department** (live: the On sneakers). The extraction prompt reads men's/women's from the item (the last's shape
  and width, the size system on the label, the model's line and colourway, a garment's cut) and gives
  `department_confidence` (+ `department_alternative` below 0.70); the card names the department unless it is plainly
  Women ("Category: Men › Shoes › Sneakers"; unsure: "… — my best reading; reply 'men' or 'women' to change it";
  CORRECT_HINT has "men / women"); a reply `men` / `women` (`approve._set_department`) puts the item under that
  department (`pipeline.set_category`: reprocessed, the size menu and copy follow). **A listed item** is changed on the
  sites by the running poster (`edits.py`, table `listing_edits`, one row per item and site, pending → saving → done |
  unconfirmed | failed | skipped; `thrift recategorize <item> "Men > Shoes > Sneakers" --dry-run | --now`): the item's
  record follows at once (facts, renders — the size on the new department's menu —, the sites' mapped fields through
  `map_depop` / `map_vinted`); each site worker takes its due row on an idle turn, before a price drop
  (`parallel.listing_edit`, in any hour — before the cross workers' posting-hours gate, like a take-down): Poshmark
  `PoshmarkPoster.set_category` — the edit page's own category field (`SEL["category_open_edit"]`: the dropdown in
  `.listing-editor__category-container`, showing the listing's current value — live 2026-10-18, the create form's
  "Select Category" text isn't there), then the same menu (`_category`: department, category, subcategory) and size
  menu (`_size`), read back, Update → List This Item for THIS edit (WO40)
  once, then the public listing's JSON (`crosslist.poshmark_listing`: department.display, category_v2.display,
  category_features[0].display, size) until it shows the new department; Depop / Vinted `ExtensionPoster.set_fields`
  — the extension's "fields" job on the edit page (`T.fieldsSet` → `site.setFields`: Depop's category chosen under
  the new department's heading and its size on the menu that follows; Vinted's leaf by id down the men's tree and
  its size), read back, Save once, then the edit page read again (`read_only` job, `read_fields`) for what stuck.
  **Live 2026-10-18 (the On sneakers, done on all three):** a new category on Vinted EMPTIES the brand, the condition
  and the package size (Vinted refused the Save: "Fill in brand to continue, Certain brands can only be changed if you
  delete and re-upload this item.") and on Depop can empty the condition — the page scripts read the listing's OWN
  values before the change and set those back (never our mapping over the owner's choices: the dry run had put
  "Very good" / medium over her "Good" / large); Depop's edit page came up once as a header and a footer with no
  form for 15 s — the edit modes wait up to 30 s longer for the form. One Save per site; an edit saved once and not
  confirmed is READ an hour later (`_shows`), never saved again blind; a failed attempt is tried again an hour later,
  three in all, then ONE private line. Dry runs fill and never save (a private line with the screenshot's name and
  what the page read back).
- Tests: `tests/test_wo43.py` (22); WO42's and WO39's updated to the new lines.

## Shares that end in a card or a clear message (WO42, 2026-10-17)
Live: a share at 3:18 PM whose 11 photos never reached iCloud — the folder and its 7-byte `_done.txt` came within two
seconds, no photo ever (iCloud's own log: three pushes, one tiny document, nothing else; `brctl status` caught up); two
more runs of the Shortcut made two empty folders with no `_done`. The worker waited WO37's 30 minutes, said one technical
line in the private chat ("batch …: no usable photos") and the group heard nothing for four hours; the markerless
folders were skipped on every tick, forever. Since Oct 12, 4 of 23 shares never became a card and 11 came short or late.
- **Pull, don't wait** (`pipeline.pull_from_icloud`): a share's cloud-only files (`.icloud` placeholders, dataless
  files) are asked for — `brctl download` on the folder and each file, and on every third ask the file's first byte read
  through the File Provider (what makes macOS fetch it; a subprocess with a 20 s timeout, never on the worker's thread)
  — with back-off per folder (15 s, 30 s, 1, 2, 5 min; kv `icloud_pull`).
- **The count is what she shared** (`share_state`): the Shortcut's `done N` is matched against the photos PLUS their
  companions (a Live Photo's .mov, a video, a raw — `MEDIA_EXT`), never waited on for a number the Mac can't reach.
- **A deadline, never silence** (`share_state` / `take_share`, from when the folder appeared on the Mac — its birth
  time, `appeared`): ready → the card as before (the mark here and `inbox.min_wait_seconds` old, quiet for
  `settle_seconds`, the count covered); **15 minutes** (`PARTIAL_AFTER`) short of the count or without the mark, with at
  least one camera photo (`camera_photos`: not a retail screenshot — the stand-in for "a label or front shot", the
  model's roles come only after) → taken with those, ONE private line "📸 3 of 4 photos of your 3:18 PM share arrived —
  the card is made from those; the rest are told if they come." (event `share_partial`); **30 minutes**
  (`EMPTY_SHARE_WAIT`) with no camera photo (nothing, or only screenshots) → **replaced the same night by WO43** (the
  "share it again" line made three duplicates): the photos are still on the iPhone — ONE group line says so, the
  folder waits where it is for a day, then is archived with a `_thrift_note.txt` and the failed batch; photos that
  land in it later are taken where they are. Empty and leftover folders are shares too: judged by the same clock,
  never blocking the next share.
- **The watcher's heartbeat** (`alerts.beat`, kv `worker_inbox_beat`: every look at the inbox, whatever iCloud
  answered): the Telegram thread restarts the worker (`alerts.restart_watcher`: one private line an hour at most, event
  `watcher_restarted`, `os._exit(3)` — launchd's KeepAlive starts it again) when no look finished for 15 minutes
  (`WATCH_STALL`) and nothing was processed either (no `worked` event), or a look started 15 minutes ago never came
  back — with the lid open only, and never in the process's first 15 minutes (`alerts.STARTED`: the beat is the last
  run's until the first look).
- **The Mac asleep or closed in the daytime** (`timers.mac_watch`, the API's 15-minute timer): its heartbeat (every 15
  minutes while open, and on a wake) missing for 2 hours between 08:00 and 23:00 local → ONE private line per outage,
  "The Mac has been offline since Sat Oct 17, 2:10 PM EDT — shares and posting wait until it's open." (settings
  `mac_down_since`, cleared by the next beat; nothing at night — said in the morning if it is still silent; nothing when
  it is back).
- **The phone side:** the Shortcut saves the photos and `_done` into the same folder, one folder per run (the Oct 16
  shares, each with its photos and `done N`); today's three folders are three runs. The Mac can't make the phone
  upload: the owner checks Low Power Mode, iCloud Drive's cellular data, the phone's iCloud storage, and that the
  Shortcut's "Save" steps really write to the same Posh/inbox subpath (report `reports/2026-10-17_45_wo42-shares.md` §4).
- Tests: `tests/test_wo42.py`, `tests/api/test_api_wo42.py`; the WO37 share test's "30 minutes, no photo" case is now
  `share_state == "lost"`.

## Automatic price drops (WO41, 2026-10-17)
The owner's rule: 30 days after an item is live on ALL its sites, if it hasn't sold, 10% off on every site; 30 days
after that, 10% again; then never again. `thrift_agent/pricedrop.py`; settings `price_drop.enabled` (true),
`price_drop.floor` (off).
- **The clock** (`pricedrop.clock`, `items.all_live_at`): the last site's posted_at (hand-listed rows by their recorded
  date; a backfilled item from when it reached all three); a site that never goes live (failed, skipped, a dry run) →
  7 days after the first live site. Stage 1 when `all_live_at` + 30 days passed, stage 2 when stage 1 + 30 days
  (`items.drop_stage` 0/1/2, `last_drop_at`, `next_drop_at`; never a third). A site posted again after a drop was made
  there (a relist) → a new clock (stage 0). `pricedrop.plan` (the poster's housekeeping, every 10 min) keeps them and
  makes one `price_changes` row per live site when a stage falls due.
- **Who drops** (`why_not`): item `posted`; no listing sold / delisted (Hidden, Not For Sale); no take-down for it ever;
  not the owner's 'no drop'. On the site, read right before the change (`read_price`): Poshmark's public page for sale
  (`crosslist.poshmark_page`), Vinted's own JSON of the seller's items by the listing's id (not reserved, hidden,
  closed or a draft — `vinted.js wardrobeItem`), Depop's listing JSON-LD (not SoldOut).
- **The price** (`dropped`): per site from that site's live price (her hand changes count), whole dollars rounded down
  from 90% ($25 → $22, $40 → $36, $95 → $85, $110 → $99, $20 → $18): every cut ≥ 10% (Poshmark tells the likers). Under
  the floor or the site's minimum (`SITE_MIN`: Poshmark $3, Depop / Vinted $1) → that site skipped for the stage, never
  a smaller cut. Nothing else on the listing changes.
- **Doing it** (`run_one` from each site worker when it has no take-down and no listing — `parallel.price_drop`; inside
  the posting hours, LIVE poster, never with PAUSE, the lid closed or the site stopped): Poshmark (`PoshmarkPoster.
  set_price`): the edit page's Listing Price (its dialog: the listing price only; Smart Sell must be off), read back,
  Update → the Share Listing panel's List This Item for THIS edit (WO40) once, the public page read until it shows the
  new price. Depop / Vinted (`ExtensionPoster.set_price`, the extension's "price" job on the edit page — `T.priceSet`):
  the page's price must be the one read (else nothing typed: `mismatch`, read again next turn); Depop's fields its edit
  page left blank (category, size, brand, condition, colours — the Tory loafers, 2026-10-13) filled from the listing's
  `fields_json` first (`depop.js refill`); the price typed, read back, Save once; then the site's own data read. Right
  before the Save `go()` asks GET /tasks once more: a sale that arrived meanwhile stops it (row `skipped`); `sales.
  skip_sold` calls off an item's open drops.
- **One Save, never two blind:** `saving` at the go-ahead; the site showing the new price → `done`; not yet →
  `unconfirmed`, read again 5 min later (the new price → done; lower than the old → she changed it by hand → done; the
  old price → one failed attempt, the next window — never saved again in the same turn). A failure is retried 6 hours
  later; 3 → ONE private line "Price drop on <Site> failed for <title>: $X → $Y. Please change it by hand."
- **The group** (`announce`, housekeeping): ONE line a day once nothing is left to try now — "🔻 Prices down 10% today:
  Levi's 501 Cutoff Shorts $25 → $22 · Zara Mini Skirt $25 → $22 (last drop)." (the card's short name; sites at
  different prices listed per site); nothing on a day without drops (kv `price_drop_said`).
- **Controls:** a reply `no drop` to any of the item's messages (its card, its ✅ line) → `items.no_drop`, open drops
  called off, answered at once (`approve._reply_no_drop`); `thrift price-drop --list | --skip <item> | --dry-run <item>
  (the edit pages filled, never saved; screenshots in failed/shots, a line to the ops chat) | --now <item>` — the dry run
  and --now are made by the running poster.
- **Records:** `price_changes` (item, site, stage auto_1 | auto_2 | dry, old / new price, pct, status pending | saving |
  done | unconfirmed | failed | skipped, attempts, error, evidence, due / retry / done / announced at; dry rows never
  synced), `listings.price_live` / `price_read_at`; all synced to thrift-api (`/sync` takes `price_changes`; the API's
  schema adds the columns where missing — `schema.COLUMNS`). A sale keeps how many drops its item had (`sales.drops`,
  set when its take-downs are made); its price is the sale's. The dashboard: per item "live since … · N days · drop Oct 30
  $25 → $22 · next drop …", the price now (the lowest live site price), and a "Drops due this week" card.

## The sites' sold orders, read only (WO38, 2026-10-14)
None of the three sites emails the seller when an order ships (Poshmark's "Thank you for shipping" aside): the shipping
state lives on each site. The poster reads each site's sold orders itself, and the API's reminders, overdue line and
dashboard follow what the sites show.
- **Read only, always:** nothing on an order page is clicked, typed or submitted (no ship, label, mark, accept, decline,
  rate, offer or message control); no buyer is ever written to; no label is downloaded. **Never stored:** buyer names,
  addresses, tracking numbers — only order id, status, the site's status words, title, price, dates and the listing /
  SKU leave a page (`orders.SEND`); an order page is never pictured (`T.noShots`; background.js `shoot` refuses an
  orders job). The §1 probe scrubbed what it kept in the page (`T.scrub` / `orders.scrub`: a key cut into its words, a
  value under a DENY word "<scrubbed>", a string kept only under a SAFE word; a row's title only when it is ours).
- **What each site gives (§1, the probes of 2026-10-14, `thrift orders --probe`):**
  - **Poshmark:** /order/sales opens /seller-hub/sales, whose page loads its own JSON `GET /vm-rest/users/<seller>/
    orders/sales?request=…` → `data.sales_summary[]`: id (the sale email's order id), title, total_price_amount.val,
    inventory_booked_at, display_status (Awaiting Shipment | Shipped | In Transit | Delivered | Order Complete | Order
    Cancelled) — taken from the page's own answer (a response listener: nothing asked again). Each open order's own
    page (`$_order_details.order`, ≤ 4, same-origin GET) adds line_items[].sku (our item id) / product_id,
    inventory_units[].status (shipped | delivered | received) and shipped_on / delivered_on / received_at.
    **`/news/order` is never opened:** its page marks the order notifications as seen (`last_viewed_notification`).
  - **Vinted:** /my_orders?order_type=sold is drawn by its server; its own JSON `GET /api/v2/my_orders?type=sold&
    status=all&page=1&per_page=20` (same origin) → my_orders[]: transaction_id (the label email's order id), title,
    price.amount, date, status — a sentence: "Order shipped and on its way!", "Package delivered.", "Order completed
    successfully. The buyer accepted the item.", a cancellation. Its "to ship" sentence isn't recorded yet: an unknown
    sentence leaves the order as it is and is told once in the private chat ("🔎 Vinted shows an order status I don't
    know: …").
  - **Depop:** /sellinghub/sold-items/ loads `webapi.depop.com/presentation/api/v1/receipts/?role=seller` with the
    page's own sign-in token (Authorization: Bearer, from its access_token cookie) — read the same way and without
    cookies (with them the browser refused it; without: 200, live 2026-10-14); the token never leaves the page. Its
    shape: `objects[]` — purchase_id (the id in the list's /sellinghub/sold-items/<id>/), status
    ("delivered_pending_feedback"), sold_timestamp, items_amount, line_items[].product_id, parcels[].parcel_status
    ("DELIVERED") and shipped_at — both status words read together (`orders.DEPOP_CODES`). Else the list as drawn:
    each receipt's link and, BESIDE it in its row, the status word (To ship | In transit | Shipped | Completed |
    Canceled) and "20 days ago". Depop's emails carry no order id: an order is the one Depop sale without one sold
    within 3 days (the first sweep tied the Madewell sale that way), then its purchase id for good.
- **The sweep (§2, `orders.py`, `parallel._sweep`):** each of the poster's site workers, between its own listings — on
  an idle turn, or inside the pause after a listing when ≥ 60 s of it is left (`orders.BUDGET`): a posting or take-down
  job of the same site always goes first (Poshmark's read also steps aside between its loads, `Yielded`) and no
  listing waits for it (measured in `tests/test_wo38_sweep.py`). One site at a time (`SWEEP_LOCK`); never with PAUSE,
  the lid closed or the site's extension away. Due: from 08:00 the day's first, then every 6 hours while the Mac is
  open (and so on a wake when the last is older). Depop / Vinted by the extension's `orders` job (`sweep: true`,
  vinted.js / depop.js `orders`) in the Thrift Chrome's own **"Orders" window** (`siteWindow("orders")`); Poshmark in
  a new page of its browser. A login / CAPTCHA / block / check page stops that site's sweep for the day with ONE private
  line ("⏸ Can't read <Site>'s orders today — …"); a failure is tried again after 30 min. The orders go to the API
  (`POST /orders`; its outbox while it doesn't answer). kv `orders_swept` / `orders_stopped` / `orders_failed`; events
  `orders_swept` / `orders_failed` / `orders_stopped`; `thrift status` shows each site's last sweep; `thrift orders
  --sweep [--marketplace m]` reads now (the poster stopped; Poshmark in the GUI session: `bash deploy/gui_once.sh
  orders --sweep`).
- **The API (`api/thrift_api/orders.py`):** each order kept in `orders` (site, order id, sale, item, status, the site's
  words, the first time each status was seen); the same events the emails give, keyed by the order id
  (`mac:order:<site>:<order>:<KIND>`, so once each): SHIPPED / DELIVERED / COMPLETED recorded **quietly** (no message),
  CANCELLED said as the email's is. A sale is found by the order id, then our SKU / the listing, then the title (sold
  within 3 days), then Depop's one open sale. An order our records lack (its email missed) is recorded once
  (`mac:order:<site>:<order>`) — the usual path: take-downs elsewhere, one 💰 line — when sold in the last 45 days and
  not cancelled, and, if sold before going live, still on its way (finished history is left alone).
- **Reminders (§3, `timers.reminders`):** none for a sale its site shows shipped / delivered / completed (or cancelled,
  returned). Overdue = `📦 Overdue: <title> (<Site>) — ship today` in the GROUP, once (kind `overdue_site`), only when
  the site — read after the ship-by day ended — still shows it to ship; a sale on a swept site that no order was tied
  to, or on a site never swept, keeps the old ops line. Her `shipped <words>` reply still overrides.
- **Dashboard (§4):** the "Shipping" column — To ship / Shipped · day / Delivered / Completed (the furthest of our
  records and the site's, `core.shipping`) — and "Orders read on the sites: Poshmark … · Depop … · Vinted …" under
  it; a sale of something listed by hand (none of our items) is a row of its own, so every sale shows and "to ship"
  counts it. The phone's link uses a key scoped to the dashboard function only (key `phone` on `get_dashboard`): the host-wide
  `dashboard` key also opens the write routes.

## Brands, dropdowns, the read-back (WO36 — the Alo listing, 2026-10-12)
The live miss: the label read "alo"; the copy's title said "Alo Yoga" but the brand field stayed "Alo"; Poshmark offered
ALO Yoga, ALOHA Collection, ALOHAS, Aloha Island, Alorna and the exact-name rule left Brand empty; Depop had filled its
own right "Alo Yoga", which "Alo" typed replaced with "Other"; Vinted took its separate "alo". The owner fixed the live
listings by hand (never changed back). The rules since:
- **Brands.** The copy step returns `brand` — the name shops list ("Alo Yoga" for "alo", "Mango" for "MNG") — and
  `brand_label`, the label's text (`CopyOut`); `pipeline.shop_brand` takes the copy's name only when it is the same
  brand (the label's words followed by more, the alias file's name, the label as its initials), else the alias file's,
  else the label's: never another brand. The facts keep the label's reading; the title, both descriptions and the short
  name use the shop's name (`pipeline.named_as` / `shop_named`: "MNG Knit Cardigan" → "Mango Knit Cardigan", never "Alo
  Yoga Yoga"); `Render.brand` = the shop's name, `Render.brand_label` = the label's when it differs (the cross fields
  carry it). On every site the names are tried in order (`brands.candidates`): the brand, the label's text, the alias
  file's name for each, then the two parts of "MNG (Mango)" — each typed as written and in the other spellings of the
  same name (never a word of it) and picked when the site lists it EXACTLY (case, spaces, punctuation, accents aside,
  & = and); only once every name was searched, the ONE option that is a name of ours followed by more words ("Alo" →
  "ALO Yoga"; whole words — never ALOHAS; two such options → no guess) (`brands.choose` / `pick_any`, the extension's
  `T.brandChoose`); else the fallback — Depop "Other", Vinted "List without brand", Poshmark empty — with the site's top
  suggestions in the note ("… (it offers: …)") so the alias can be added. Never the other way round: "Solid & Striped"
  never lands on "Solid". Depop's own fill-in is kept when it IS one of our names.
- **The alias file:** `data/brand_aliases.json` (tracked, curated: alo → Alo Yoga, MNG → Mango, TNF → The North Face,
  A&F, AE / AEO, BR, CK, DVF, WHBM, NY&Co, J.Crew → J. Crew, …) under `data/brand_aliases.learned.json` on the Mac
  (`paths.brand_aliases`, git-ignored; the old `data/brand_aliases.yaml` still read). It grows two ways: a site's other
  spelling of the same name ("J.Crew" → "J. Crew", "MNG (Mango)" → "Mango"; `Aliases.learn`), and the owner's card reply
  **`brand <name>`** (`approve.brand_reply` → `pipeline.set_brand`): that item's brand on all three sites (the renders,
  so Depop's and Vinted's mapped fields follow), the title and descriptions renamed once, her reply answered at once
  ("✓ Brand: …"), and the label's text saved as an alias for her name (`learn(…, owner=True)`).
- **No made-up values in any dropdown (§3).** Every pick is the option whose text IS the wanted value (Depop's
  `T.matchOption`, Vinted's rows, Poshmark's `_is`) or, for a brand / a Poshmark category, the rules above — never the
  first option a filter shows. An optional value a menu doesn't offer is left empty, never another option, and said in
  the notes ("colour-input: no option 'Teal' … — left empty"; Depop's colours, source, age, attributes; Vinted's colours
  and materials — optional in its catalog); the page reports it (`seen.left`) and the driver expects it absent. A
  required field keeps its defined fallback (a Poshmark size one step away, a category → the item skipped, Depop's
  brand "Other", Vinted's "List without brand"), with a note.
- **The read-back before publishing (§4).** Depop's page (the tidy step): every multi-select holds only ours (Style,
  an occasion, Depop's own colours removed), the category, size and condition read back and chosen again ONCE when they
  aren't ours, then the brand and the package set back. Vinted's page (`check` step): size, condition and skirt length
  chosen again once, colours and materials set to exactly ours once (`setMulti`), the brand set again, the package after
  a fix. Poshmark (`PoshmarkPoster.fix`, called once by `Poster.post` when the diff isn't empty): a brand other than
  ours cleared (optional), the title, description, category (+ size), size, condition and price filled again; then read
  back again. The driver's diff (`ExtensionPoster.expected(r, seen)`): the brand one of ours or the fallback
  (`BrandOf`; live 2026-10-09 the silk pants read Vinted's own "H&M"), Depop's Style and any other multi-select empty
  unless ours, colours / materials ours minus `left`. Still different → nothing published (a publish is cancelled
  before the click): the row `failed` with "Mismatch: …", the ops chat says why, and it waits — tried again each window
  whatever its attempts, never the group's "⏭" (`crosslist.MISMATCH`). The group hears nothing.

## Tonight's fixes (WO37, 2026-10-12)
The first long evening at gap 0 (17 items, 15 shares). What went wrong, and what the code does now:
- **Covers: the whole item, from the front** (the owner's rule). The front check (`brain/cover.py`, Opus) labels EVERY
  photo of the item — view (front, three_quarter, side, back, top, worn, label, detail, inside, sole, flaw, box),
  `whole`, `cut_off`, `fill`, `held` — and names the one it would make the cover; code decides (`pipeline.cover_rule`):
  shoes the pair from the side, 3/4 or front, whole; clothing and sets a front flat lay / hanger shot, whole (worn only
  when there is none); bags and the rest a whole outside view; never a label, a close-up, a look into shoes, a sole, a
  flaw, a box, a hand-held shot from behind (`back`) or a photo cut off. Several fit: the model's pick, then the
  printed side (clothing), then the most of the frame. None fits: the photo showing the most of the item, and ONE line
  to the private chat, never a question ("No full photo of … — a full front shot next time"; `NO_FULL_COVER`). The
  rest follow in her order of kinds (`labelled_order`: other fronts, back, side, top, worn, details, labels, flaws). The
  stored check is reused while it covers every photo (the cover never changes between runs). Tonight's five kinds are
  fixtures: `tests/test_wo37.py` (labels) and `private/tests/fixtures/wo37_covers/` (the photos, the expected covers).
- **Shares whose photos come late** (live: the alo bras taken with 0 photos; the leggings' and the vest's full shots a
  minute after their labels). A share is taken only when it is whole (`pipeline.share_complete`): nothing new in the
  folder for `inbox.settle_seconds` (60 s) by ARRIVAL on this Mac (`arrived`: ctime / birthtime — the photos' own
  dates are the phone's), the `_done` file at least `inbox.min_wait_seconds` (90 s) old, the count `_done` may carry
  ("done 7") waited for up to 30 minutes, a share with no photo waited 30 minutes. Photos that still come late
  (`late_photos`, every 2 minutes, kv `late_photos_seen`; the names each share had are in its `batch_registered`
  event): a share failed for having none goes back into the inbox as "<name>-late" and is taken again ("📸 … taken again
  now"); otherwise one private line names the photos the listing doesn't have.
- **No message twice** (see Telegram), **no pauses** (see Parallel posting), **only the price waits** (invariant 2).
- **Poshmark** (`post/poshmark.py`): the Share Listing panel is read once its Promote My Closet part is drawn (≤ 8 s,
  `PANEL_WAIT_MS`; live: read too early, the UGG Tazz failed "can't tell whether Promote … is off"), and when it still
  can't be read the panel's text is in the error and its picture kept (`…-panel.png`) before ‹ Back; a size pick that
  didn't register — the menu closed, or Done pressed, with no size on the form — is picked again, twice at most
  (`SIZE_REPICKS`; live: the Marc Jacobs bag's "One Size"); an item with no size is skipped before Next ("no size on
  the item"; live: the Clarks went to Next and Poshmark only showed "Required").
- **No size at all**: the title never carries a size no photo shows (`copy.ensure_title_size` takes a made-up "size 8"
  out — live: the Clarks); the card says "⚠️ No size on the photos — reply 'size …' (it can't be listed without one)";
  every site skips it with ONE private line for the item (`crosslist.say_no_size`, the same text on Poshmark, Depop and
  Vinted, so said once — never the group's "⏭ … Reply 'retry'", which would only skip again); a reply 'size 8' puts every site
  that skipped or failed it before any publish click back in line — never an unconfirmed publish (`pipeline.set_size`).
- **Depop's colours**: Depop put its own colour back AFTER our tidy (live: the jacket, the UGG Tazz — the form check
  stopped both before Post): the tidy lets the page settle and reads the multi-selects once more (`chipsOurs`,
  `addBack`; "Depop changed the form again after the tidy: set back once more").
- **Sizes and categories**: a label's waist (23-34) on jeans, pants and shorts is the size listed (`settle_waist`; live:
  FRAME "24" went up as "00"); loafers, moccasins and flats are Flats & Loafers (Men: Loafers & Slip-Ons), never Heels
  (`settle_shoe_kind`; live: the Tory Burch loafers).
- **Titles**: a collab that is the brand itself is never said ("adidas by Stella McCartney adidas by Stella McCartney
  …", `premium.collab`); the 80-character cut never takes the brand or the size (`premium.fit_title`); a fiber's weaker
  wording goes with its last word ("Leather … Leather").
- **The diagnostic** (2026-10-12, read only, sent to the owner as a file): `reports/2026-10-12_37_wo37-diagnostic.md`.
- **A live listing's cover fixed on its edit page** (`thrift cover-fix <item> [--marketplace m] [--photo N]
  [--dry-run]`, `thrift_agent/coverfix.py`; the owner's authorization: "put the right photo first and change nothing
  else"; the poster stopped — the bridge's port, Poshmark's Chrome profile —, Poshmark's part in the GUI session:
  `bash deploy/gui_once.sh cover-fix …`). The right photo: `--photo N` (her Nth, from 1), else the WO37 rule on the
  item's labelled check. Each site's photos as posted (Poshmark's render, Depop's / Vinted's `fields_json`) say where
  it is; a listing without it is left for the owner. Recorded by WO37's read-only probes of the UGG Chestnut
  (2026-10-12, nothing saved): Depop `/products/edit/<slug>/` and Vinted `/items/<id>/edit` are forms whose photo
  grids are dnd-kit sortables (`edit_photos`; Depop's "Cover", Vinted's "Main" badge on the first; Vinted: "To
  reorder photos, press the space bar") with one Save (`edit_save`: Depop "Save changes", Vinted
  `upload-form-save-button`, disabled until something changes). The extension's "cover" job (`T.coverFirst`) moves the
  photo with the keyboard (focus, Space, ArrowUp to the first row, ArrowLeft, Space), reads the order back, and the
  driver (`ExtensionPoster.set_cover`, `cover_moved`) gives its one go-ahead only when the order is the old one with
  that photo first; Save once; the listing page's first photo must be that photo (`photo_key`: Depop's
  `…/<id>/P2.jpg` = the JSON-LD's `…/<id>/P0.jpg`, Vinted's `/tc/<id>/` = `/t/<id>/`). Poshmark's edit page
  (`/edit-listing/<id>`) has no order to change: its cover's "Edit Covershot" (shown on hover) opens a dialog with only
  the photo, "Replace Photo" (`input[name=replaceImage]`), Cancel and Apply; after a file the dialog shows its crop
  editor (`.croppie-container`, a 3:4 frame — a plain photo's "Delete | Replace" dialog too) — so
  `PoshmarkPoster.set_cover` replaces the cover by the right photo's 3:4 cover and puts the old cover's own photo
  (fitted to 3:4) on the tile the right one had, and checks that exactly those two tiles changed. **It never saves:**
  recorded live on 2026-10-13, Update only slides up the Share Listing panel (the new cover, Promote My Closet Off,
  List This Item) and the edit is saved by List This Item — that press needs the owner's go-ahead (the session's
  permission check refused it), so a real run reports "nothing saved" and a dry run is all it does today.
  Live results (2026-10-13): Depop's keyboard move took — the UGG Chestnut's and the Schutz's Depop covers saved (the
  owner's go-ahead); the Tory loafers' Depop edit page came up with its Category, Brand, Condition and Colour empty
  (its "Pumps" category no longer loads there) and refused the Save: nothing saved, now reported so (it had said "it WAS
  saved"). Vinted's grid ignored the keyboard both on the sortable and on the element inside it (dnd-kit's live region
  stayed empty). The owner fixes Poshmark's and Vinted's covers, the late photos on live listings and the smaller
  issues by hand, from one list in her private chat (each listing, site, change, and the photo — by its IMG name —
  that goes first). The UGG Tazz and the Marc Jacobs, not yet on Poshmark, got their late photos before posting (a
  one-off: the photos normalized into the item, the front check asked again, the cover file and photo lists rebuilt —
  titles, category, size and price untouched). Stand-ins:
  `tests/fixtures/ext_edit_photos.html`, `tests/fixtures/poshmark_edit_listing.html`.

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
"8.5"). A size that no menu of the category has is NOT a question (owner rule, 2026-10-09): the card shows it as a
note ("… not on Poshmark's … size list: the nearest is used", `pipeline.size_menu_note`). Before that rule it was a
question on the card ("Size: “38” isn't on
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
needs prod behaviour or private data opts in with the `settings_override` fixture. Nor the machine's own disk,
clock or network: the Mac's pre-deploy run is on a laptop with 10 GB free (WO47's disk watch fired inside an alerts
test there and rolled the deploy back) — anything that reads them is faked in the test or guarded by `is_prod`.
