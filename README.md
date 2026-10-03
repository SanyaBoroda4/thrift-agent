# Thrift Agent

![tests](https://github.com/SanyaBoroda4/thrift-agent/actions/workflows/tests.yml/badge.svg)

An AI listing agent for a resale closet: shoot items on an iPhone, tap **Share → New Item**, and the agent
splits the photo roll into items, reads brand/size/condition from the photos with evidence for every fact,
prices from the seller's own sales history, writes the listing in the seller's style, checks it for
unsupported claims, and posts it to Poshmark (Depop next) through a real, paced browser session.

- **Pipeline:** iCloud Drive inbox → segmentation → vision extraction → pricing → copy → verifier + lint → gate → Telegram price approval → poster
- **Stack:** Python 3.14, Claude API (vision + tool use), Pydantic, Playwright (real Chrome), SQLite, launchd
- **Design rules:** facts before prose, the model never presses publish, idempotent posting, stop-don't-guess

Full spec and invariants: **CLAUDE.md**.

## First-run rules
Defaults for the first weeks of live posting, until the eval numbers justify loosening them.
- **The owner's only routine input is the price.** Brand, size and condition are accepted at `gate.min_confidence`
  0.70; below that the question goes to the owner. NWT stays strict: it needs a hang-tag photo (`hang_tag_photo`) or a
  seller note — never a confidence score.
- **Materials need evidence.** The item type and features may not name a material (leather, suede, wool, ...) unless
  `facts.material` is backed by a label or stamp; texture words (woven, quilted, ribbed, glitter) are fine. The verifier
  treats material words as claims, and lint flags any material word in the title, description or tags that
  `facts.material` does not support.
- **Titles show the US size only, never EU.** Adults: "size 7.5". Kids shoes: "Toddler size 7.5" / "Little Kid
  size 13" / "Big Kid size 4", never a bare "size 7.5" for kids. The groups are Poshmark's: Toddler up to 12C (0-7C
  included, since that's what buyers search), Little Kid 12.5-13.5C and 1-3Y, Big Kid 3.5-7Y. The EU size and the
  full label "EU 24 / US Toddler 7.5" go in the description; the listing's size field keeps the full label for the
  form. Lint accepts the US-only title forms, never requires EU, and flags any EU or non-US size token in the title,
  and a kids group word that isn't Poshmark's for the size (a brand chart's "Little Kid" on 11C). On Poshmark's form
  the kids shoe size is picked on the **Girls** or **Boys** tab with Poshmark's own label (`KIDS_SIZE_OPTIONS` in
  `post/poshmark.py`, verified): "7.5 (Toddler Girl)" for 7.5-12, "13 (Little Girl)" for 12.5-13.5 and 1-3,
  "4 (Big Girl)" for 3.5-7 — the same groups as the title. The Baby tab (0-7) labels are still unverified.
- **Style tags and the cover are Poshmark's.** Style tags come only from the 130 curated tags Poshmark's form offers
  (a material tag such as Leather only when a label backs it); anything else is left out. The cover is 3:4 portrait,
  1200x1600, padded with the photo's own edge colour and never cropped, which is the frame of Poshmark's cover crop,
  so its default crop keeps the whole picture.
- **Kids gender.** The model reads `kids_gender` (girls / boys / unisex) from the item itself; it only picks the size
  tab and is never a question. A unisex (or unread) kids item goes under Girls, and the approval message says so in a
  `Note:` line (reply `boys` to change it). The copy never states the gender.
- **Poshmark's own category names.** Right after extraction the department, category and subcategory are put onto the
  names the create-listing form offers (`data/poshmark_taxonomy.yaml`; Kids "Tops" becomes "Shirts & Tops",
  "Booties" becomes "Ankle Boots & Booties"). A subcategory Poshmark doesn't have is left out with a `Note:`; a
  department or category it doesn't have is asked like "Other".
- **Only five questions ever reach the owner:** brand or size below 0.70, NWT without a hang-tag photo, a category
  of "Other" (or a department/category not on Poshmark's list), a possible re-share, and the poster's
  `needs_owner`. The model's own questions about optional facts
  (material, measurements) are dropped: a missing optional fact is left out of the listing. An unsure condition is a
  `Note:` line, not a question. Anything the owner does from the CLI (`thrift price` / `answer` / `confirm`) is
  echoed to the Telegram group and settles the pending message there.
- **Per-department price defaults.** `category_defaults` in `brand_tiers.yaml` are keyed by department (Women / Men /
  Kids / Unisex / Home, each with an `other` fallback). A brand missing from the price table no longer blocks the gate:
  the default becomes the suggestion in the owner's message, marked "no price history for <brand>".

## Public code, private data
Seller data (price table from real sales, real listings, account notes, username) lives in `private/`, a separate
**private** repo cloned into this folder and git-ignored here. Without it, the code runs on the example files in
`config/*.example.yaml` and `data/style_examples/`.
```
git clone git@github.com:SanyaBoroda4/thrift-agent-private.git private
```

## Windows (development)
```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env            # add ANTHROPIC_API_KEY
thrift init
pytest -q
thrift process C:\path\to\some\photos     # full pipeline, dry, no marketplace contact
thrift status
```
In PyCharm: set `.venv` as the interpreter and add a pytest run configuration.

## MacBook (production)
macOS 27, Python 3.14 (python.org installer), Google Chrome, Apple Command Line Tools (`xcode-select --install`).
No Homebrew. Shell is bash.
```bash
git clone git@github.com:SanyaBoroda4/thrift-agent.git ~/thrift-agent && cd ~/thrift-agent
bash deploy/mac_setup.sh
```
Then: `.env`, `config/settings.local.yaml` (`machine_role: prod`), `thrift login --site poshmark`, the first manual
dry-runs (see "Poshmark poster (M2)"), and start the two launchd services (worker + poster) for the dry-run week:
```bash
bash deploy/services.sh start      # also: stop | restart | status
```
`thrift login` needs the poster's Chrome profile to itself: `bash deploy/services.sh stop` first, `start` afterwards.
Deploy updates from Windows with `deploy\deploy.ps1` (pull, test, `services.sh restart`; a red test rolls the Mac back
to the previous commit).

## Commands
```
thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>
thrift requeue <item> [marketplace] | mark-posted <item> <marketplace> <url> | status | show <item>
thrift poster [--once] [--dry-run] [--stage form|review] [--publish-first <item>] [--allow-dev-browser]
thrift login --site poshmark | telegram setup|test | harvest | build-style | eval
```
- **`thrift price <item> <amount>`** approves an item's price from the command line — the same effect as replying
  to the Telegram approval message (source `owner`, item becomes `ready`). `thrift confirm` and `thrift answer` are
  the CLI twins of the batch-confirmation reply and of answering a question.
- **`thrift telegram setup | test`** — `setup` prints the chat ids and user ids seen in the bot's recent updates so
  the owner can fill `TELEGRAM_CHAT_ID` and `TELEGRAM_ALLOWED_USER_IDS`; `test` sends a test message. See
  "Telegram approval (M3)".
- **`thrift requeue <item> [marketplace]`** puts an item back in the posting queue — after a failed attempt, a fix,
  or an owner answer — for one marketplace or all of them. It also takes back an item the poster parked with a
  question (`needs_owner`) as it is, without reprocessing, and closes that question — the way to retry once the poster
  itself was fixed. Posting stays idempotent: an item that already has a live URL on a marketplace is never posted
  there again.
- **`thrift poster --allow-dev-browser`** lets the poster open a browser on the Windows dev machine to work on
  selectors. It stays a dry-run: the dev machine never logs into or touches the shop, and without the flag the dev
  poster does not launch a browser at all.
- **`thrift poster --stage form|review`** picks the dry-run stage for this run (default `poster.dry_run_stage`,
  `form`). See "Poshmark poster (M2)".
- **`thrift poster --publish-first <item>`** — the supervised first publish of one item on the Mac. See "Poshmark
  poster (M2)".
- **`thrift mark-posted <item> <marketplace> <url>`** records a listing that went live while the poster couldn't find
  its address (a post marked "unconfirmed publish"): the address must be a listing page (`https://poshmark.com/
  listing/<title-words>-<24 hex id>`) that no other item holds and that shows the item's title and price. Then the
  post is `posted` with that address and Telegram says "✅ confirmed live". Anything else is refused and nothing
  changes. Mac only, with the poster service stopped (it opens the poster's Chrome profile to look at the page).
- **Condition is shown, not told** (owner rule). No listing text names wear or flaws (dirt, stain, scuff, worn, wear
  and tear, fraying, pilling, hole, tear, smell…); a used item says "Gently pre-loved, please see photos for
  condition." and is never "like new", "excellent" or "no flaws". Every flaw has a photo in the listing, never the
  cover; a flaw without a photo shows up as a Note in the approval message.
- **`HOLD_UNSHIPPED`** is a flag file in `paths.control` (set by the shipping watchdog or by hand). It blocks
  *publish* only: drafts and dry-runs keep running, so the queue is ready the moment the late order ships. `PAUSE`
  in the same folder stops the poster entirely.
- **Retail screenshots.** Share the retailer's product-page screenshot together with the item photos. The agent
  detects it (no camera EXIF, phone aspect ratio), assigns it to the item by content, and uses it only for the
  retail price, style name, colour and retailer — never for condition, size or flaws. Own photos first: the
  screenshot always goes last in the listing and is never the cover.

## Poshmark poster (M2)
`post/poshmark.py` fills https://poshmark.com/create-listing in the poster's own Chrome. The form's structure was
verified on the live site on 2026-09-29 (logged in, nothing saved): photo input, title, description, the category
menu (department links, category items, the subcategory menu whose `<a>` must be clicked, not its `<li>`), the size
menu (tabs, `size-<label>` buttons, Done), condition labels, brand suggestions, colour tiles, style-tag input, the
Listing Price dialog (listing and original price, Smart Sell, Shipping Discount, Done), SKU, Next / Save Draft /
Discard. The first Mac dry-run's page snapshot (2026-09-30) added the cover dialog after the upload, Poshmark's
dropdown component, the condition items' codes, the curated style tags, the SKU behind "show details" and the Cancel
link's "Save Draft" dialog; the Mac's review stage (2026-10-02) the "Share Listing" panel after Next. Every
locator lives in `SEL`; the ones not verified yet are listed in `UNVERIFIED` and in
the module docstring.

- **Fill order:** photos, title, description, category, subcategory, size, condition, brand, colours, style tags,
  price dialog, SKU. Then every field is read back (including the text each closed dropdown shows) and diffed
  against the approved listing before anything else happens.
- **The cover dialog.** After the upload Poshmark opens "Select a Covershot." (every photo listed, the first one — our
  cover — preselected, a 3:4 crop frame with a zoom slider). The poster checks that it is the recorded dialog, keeps
  Poshmark's default crop (never touches the frame, the slider or rotate), presses **Apply**, waits for it to close
  and for every photo to show. A dialog that differs from the recording, or any other dialog, fails the item with a
  screenshot and the page.
- **The size menu** may wait for **Done** or close by itself when a size is picked (Kids shoes do, seen on the Mac).
  Either is fine as long as the form's size field then shows the size; anything else fails the item with the
  evidence.
- **Poshmark's labels.** Condition, picked by Poshmark's code: NWT = "New With Tags (NWT)" (`nwt`), NWOT and like new
  = "Like New" (`uln`), excellent and good = "Good" (`ug`), fair = "Fair" (`uf`). Brand: the exact
  (case-insensitive) suggestion, else the owner is asked. Colours: the 15-colour palette's tiles. Style tags: only
  Poshmark's 130 curated tags (a tag it doesn't offer is left out and the owner is told). Smart Sell must be off
  (checked, never switched); Shipping Discount is left at its default, "Optional" (no discount). The SKU is in the
  collapsed Additional Details: the poster opens "show details" first.
- **Owner questions (`needs_owner`)** only where the form is fine but lacks our value: a brand, category, subcategory
  or size Poshmark doesn't offer, or a department it doesn't have (Unisex). A form that looks different from the
  recording fails the item with a screenshot instead.
- **Dry-run stages** (`poster.dry_run_stage`, or `--stage` for one run). `form`: fill, read back, keep the evidence,
  then leave through the form's **Cancel** and the "Save Draft" dialog's **Discard Changes**, so no draft is left
  behind. Every dry-run also counts Poshmark's Drafts before the form and again on a freshly opened create page
  after it: a higher count means a draft was left behind, which is a Telegram warning and a note on the dry-run.
  `review`: also press **Next** — the **Share Listing** panel slides up over the form (‹ Back, the cover and title,
  Promote My Closet, Pinterest / Facebook Connect Now, **List This Item**) — screenshot it, save its HTML
  (`<item>-review.html`) and record its buttons and labels to `failed/shots/<item>-review.json`, then **‹ Back**, wait
  for the panel to slide away, Cancel, Discard Changes. No dry-run ever presses List This Item.
- **Evidence** for every dry-run and failure in `failed/shots/`: `<item>-poshmark-<time>.png` (full page), `.html`
  (the DOM, to record selectors from) and `.json` (what was read back, what was expected, the diff). The HTML can
  contain account details: keep it on the Mac, never in the public repo.
- **Publishing: two keys.** The poster service publishes only with `poster.dry_run: false` AND
  `poster.autopublish_confirmed: true` in the Mac's settings (both off by default; publish-gated items also need
  `marketplaces.poshmark.autopublish: true`, else they would be drafts, which still refuse until where Save Draft
  lands is recorded). Until then it dry-runs, and one item at a time can be published supervised on the Mac:

  ```bash
  thrift poster --publish-first <item>
  ```

  It runs only on prod (stop the poster service first), only for a `ready` item whose price the owner approved, and
  ignores `poster.dry_run` for this one call. An item processed before the condition rule (its text still names
  wear) is refused: `thrift answer <item> "recheck"` reprocesses it, the price kept. It fills the form, reads back and diffs as usual, presses Next, checks the
  Share Listing panel (it shows our title; Promote My Closet is off and never touched; Connect Now is never clicked),
  then asks **"Type LIST to publish"** in the terminal — anything else cancels, discards the form and puts the item
  back in the queue. After LIST it presses **List This Item exactly once** and records everything after the click
  (`failed/shots/<item>-poshmark-<time>-after-list.png/.html/.json`: URL before/after, navigations, dialogs, buttons).
  After List, Poshmark goes to the closet (`?created_listing_id=<id>`), which shows the new listing only after a
  while: the poster reloads it every 10 s for up to 90 s until the listing Poshmark named appears with this title and a
  URL slug made of this title (`failed/shots/…-closet.json` records every reload). The live page must show the title
  and the price; then the post is `posted` with the URL. If anything after the click is unrecognized, nothing is
  clicked again: the post is `failed` with "unconfirmed publish: …" and the evidence, Telegram is pinged, and
  `thrift requeue` refuses it — check the closet, then `thrift mark-posted`.

## iPhone Shortcut — "New item"
Same Apple ID as the Mac, iCloud Drive on, folders `iCloud Drive/Posh/inbox`. Finished shares are moved to
`Posh/archive` next to it — the files never leave the iCloud container, so the phone can see what was processed.
1. Share Sheet input: Images only; if no input → Stop.
2. **Format Date**: Current Date, custom `yyyy-MM-dd_HHmmss`.
3. **Save File**: Shortcut Input → iCloud Drive/Posh/inbox, Ask Where to Save OFF, Subpath `Formatted Date/`.
   (No conversion — originals keep the capture time the splitter relies on; the Mac converts HEIC.)
4. **Text** `done` → **Set Name** `_done` → **Save File** to the same place and subpath. Must be last.
5. **Show Notification** "Sent to Posh ✅".

Shooting rule that makes splitting reliable: finish one item before starting the next, and include a clear
shot of the size label / insole stamp.

## Telegram approval (M3)
The agent owns its Telegram bot and long-polls `getUpdates` from inside the worker (`thrift run`) — no webhook, so
it works behind NAT and after the Mac wakes from sleep. One consumer; the update offset is persisted in SQLite (`kv`
table). Only `TELEGRAM_CHAT_ID` and the senders listed in `TELEGRAM_ALLOWED_USER_IDS` are accepted; everything else
is ignored. It works in a group with BotFather privacy mode ON, because the owner only ever replies to the bot's
messages or presses its buttons.

### Setup
1. Create the bot with **@BotFather** and copy the token into `.env` as `TELEGRAM_BOT_TOKEN`. Keep privacy mode ON.
2. Add the bot to the owner's group (or open a private chat with it) and send `/start` or any message there.
3. `thrift telegram setup` prints the chat ids and user ids seen in recent updates; fill `TELEGRAM_CHAT_ID` and
   `TELEGRAM_ALLOWED_USER_IDS` (comma-separated) in `.env`.
4. `thrift telegram test` sends a test message.
5. Set `telegram.enabled: true` in `config/settings.local.yaml`. Other settings: `telegram.resend_after_hours`
   (default 6) and `telegram.poll_timeout`. On prod, `thrift run` and `thrift poster` refuse to start when
   `telegram.enabled` is on but any of the three env vars is missing.

### Flow
- **Batch confirmation.** The worker sends the contact sheet with a summary; the owner replies to it with `ok`,
  `12>2`, `split 7`, `merge 2 3` or `drop 7` — the same parser as `thrift confirm`. `segmentation.always_confirm`
  stays configurable.
- **One message per item.** After extraction the item waits in `awaiting_price` and gets a single message: cover
  photo, title, size (with its system), condition and flaw count, the suggested price with a short basis, "Retail $X"
  when known, and "no price history for <brand>" when the price came from a category default. Inline buttons
  **[Approve $P]** and **[Change]**. Replying to the message with a number (`85`, `$85`, `85.00`, `85 dollars`) sets
  the price; [Change] asks "reply with the price". The approved price is stored with source `owner` and the item
  becomes `ready`. Nothing publishes without price approval.
- **Questions folded in.** If brand or size is truly unreadable (below 0.70), the category is not one Poshmark has,
  or the item looks like a re-share, the question is part of the *same* message. The reply may carry both answers and the price (`size 8, 45`): the price is
  stored, the note reprocesses the item, and it comes back for approval only if something is still unresolved — the
  owner's price is kept. A price alone accepts the model's best reading of brand and size.
- **Two answers must be explicit.** *NWT without a hang-tag photo* is listed as **like new** and the message says so
  in a `Note:` line; reply `NWT` (with or without the price) if the tag really is attached. *A suspected re-share*
  stays held even after a price: reply `different item` to list it or `same item` to drop it.
- **`needs_owner`.** The poster may send a *separate* question when it is stuck on a field only the owner can answer
  (a brand, category or size missing from Poshmark's lists). The item waits in `needs_owner` while the others
  continue; the reply is attached to the item and it is reprocessed. No other questions go to the owner.
- **Re-send after sleep.** On worker start and about every hour, anything still waiting longer than
  `telegram.resend_after_hours` is sent again — Telegram keeps updates for only 24 h and the Mac may have been asleep.
- **CLI equivalents** keep working: `thrift confirm`, `thrift answer`, `thrift price <item> <amount>`.

Item statuses: `new` → `awaiting_price` | `needs_info` → `ready` → `posting` → `posted` | `drafted` | `failed`;
`needs_owner` while the poster's question is open.

## Roadmap
Where the project is going, and the design decisions already made for each step.

- **Telegram approval flow — implemented (M3).** The agent owns the bot and long-polls `getUpdates` from inside the
  worker — no webhook, so it works after the Mac wakes from sleep (Telegram keeps updates for 24 h; pending
  approvals are re-sent). n8n does *not* attach a Telegram trigger to this bot. One message per item with
  **[Approve $P] [Change]**; nothing publishes without price approval; `needs_owner` for the poster's own questions.
  Details in "Telegram approval (M3)" above. What remains is on the owner's side: create the bot and fill `.env`.
- **Airtable as the business view.** One row per item: photos, title, prices, platform links, status
  (listed / sold / shipped / delivered / complete), sale price, earnings, days to sell. SQLite stays the source of
  truth for posting state; the agent pushes updates, and field ownership is documented so the two never fight
  over a column.
- **n8n automations.** Gmail sale email → Airtable `sold` + a call to the agent to delist the item elsewhere.
  Shipping watchdog: sold more than 24 h ago and not shipped → Telegram reminder + `HOLD_UNSHIPPED`. The agent
  exposes a small local HTTP API (mark-sold, hold, release) reachable over Tailscale. Workflows are exported as
  JSON into `n8n/`.
- **Daily read-only sync of My Sales** — the true order status (shipped / delivered / complete) → Airtable.
- **Eval.** Shoot 20–30 real items (mostly shoes, some with retail screenshots). In addition, the photos of the
  ~100 sold listings harvested from the closet form an extra set for brand / size / category only — *not*
  condition, because historic condition labels are unreliable.
- **A laptop that is closed most of the day.** Everything queues and catches up when the Mac opens; listing
  hours are set to cover when it is usually open.
- **Milestones:** M0 eval → M1 prompt tuning → M2 Poshmark poster (form structure verified 2026-09-29; the Mac
  dry-runs recorded the rest up to the Share Listing panel; next the supervised first publish) → M3 Telegram
  approval flow (implemented: long polling, one message per item) → M4 Airtable + n8n + My Sales sync → M5 Depop
  → M6 sold-comps pricing.
