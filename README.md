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
- **The cover is the front of the item** (owner rule, absolute). The item alone, its front, flat lay or on a hanger,
  on a clean background — never the back, a try-on or mirror photo, a label, a tag, a flaw close-up, a box or a
  screenshot. A front lying sideways or upside down still counts (the cover is turned upright for the listing, never
  cropped), and so does a front with a small flaw on it. Front vs back is one comparison: the model sees all the photos
  of the item alone side by side and says which shows the front (print, logo, buttons, zip, pockets, the lower
  neckline); code then makes sure a back or a plain side is never the cover while a front or a printed side exists.
  "cover: no front flat-lay photo" appears only when there really is none. The rest of the photo order stays as the
  model set it, retail screenshots last.
- **"cover 2"** — reply that to a price card, or type it while the card is open: photo 2 of that item (its photos in
  shooting order, counted from 0) becomes the cover and the card comes again. There is no button for it on purpose.
- **Kids clothing sizes from the label.** A label with the height or the age ("4 ans / 104 cm", "110 cm", "4A",
  "5-6 Y") is turned into Poshmark's size by a fixed table (104 cm → 4T, 116 cm → 6, 128 cm → 8 …) and is never a
  question; the label as printed goes into the description too ("Label size: 4 ans / 104 cm."). A department is never
  the category: a kids tee is Kids › Shirts & Tops.
- **Kids gender.** The model reads `kids_gender` (girls / boys / unisex) from the item itself, with how sure it is; it
  picks Poshmark's Girls or Boys size list. At 0.70 or more it is used silently; below that (or unisex, or unread) the
  owner gets "Girls or Boys?" with [Girls] [Boys] before the price card (CLI: `thrift kids <item> girls|boys`); the
  answer is kept. The copy never states the gender.
- **Poshmark's own category names.** Right after extraction the department, category and subcategory are put onto the
  names the create-listing form offers (`data/poshmark_taxonomy.yaml`, every department from the form's own catalog;
  Kids "Tops" becomes "Shirts & Tops", "Booties" becomes "Ankle Boots & Booties", a category given as "Jumpsuits &
  Rompers" becomes Pants & Jumpsuits › Jumpsuits & Rompers). A subcategory Poshmark doesn't have is left out (noted in
  the item's record, not on the card); a department or category it doesn't have is asked like "Other". A two-piece set
  goes under its bottom (top + skirt: Skirts › Skirt Sets) and is a "2-Piece Set" in the title.
- **"Which category?" and "No brand" (WO25).** A category the model is under 0.70 sure of (or one Poshmark doesn't
  have) is one message with 1-3 real paths as buttons, e.g. [Skirts › Skirt Sets] [Shorts]; typed answers still work.
  The brand question has a [No brand] button: Poshmark's brand field stays empty (the form marks it optional) and the
  copy names no brand.
- **Sizes from Poshmark's catalog (WO25).** `data/poshmark_catalog.json` holds every category's size menu per tab
  (Standard / Plus / Petite / Juniors / Maternity, Big & Tall, Baby / Girls / Boys); the size the poster selects is
  exactly one of those values ("Waist 32", "MP", "7.5 (Toddler Girl)", "3 Months"), and a size no menu has is asked.
- **The only questions that reach the owner:** brand or size below 0.70, NWT without a hang-tag photo, a category
  of "Other" (or a department/category not on Poshmark's list), a possible re-share, a pair of shoes in doubt between
  brand new and worn, Girls or Boys below 0.70, and the poster's `needs_owner`. The model's own questions about
  optional facts (material, measurements) are dropped: a missing optional fact is left out of the listing. An unsure
  condition is kept in the item's record, not shown. Anything the owner does from the CLI (`thrift price` / `answer`
  / `confirm` / `condition` / `kids` / `redo`) is echoed to the Telegram group and settles the pending message there.
- **Always a price.** brand tier → the department's category default (the category, then Poshmark's other names for
  it — Kids "Shirts & Tops" finds the table's `Tops` — then the department's `other`) → the old flat table the same way
  → `pricing.default_target` (config/settings.yaml). The price card always has a number. `category_defaults` in
  `brand_tiers.yaml` are keyed by department (Women / Men / Kids / Unisex / Home, each with an `other` fallback).

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
dry-runs (see "Poshmark poster (M2)"), and start the worker as a launchd service:
```bash
bash deploy/services.sh start worker     # also: stop|restart worker|poster, stop all, status, logs [worker|poster] [n]
```
The poster service stays off until the owner turns publishing on (`poster.dry_run: false` and
`poster.autopublish_confirmed: true`). `thrift login` needs the poster's Chrome profile to itself:
`bash deploy/services.sh stop poster` first.

### Deploy over SSH
The PC reaches the Mac with an SSH key (no password): `ssh tatiana_sorokina@192.168.68.57`. From Windows:
```powershell
.\deploy\deploy.ps1          # -MacHost user@host to override, -NoPush to skip the git push
```
It pushes, then on the Mac (`deploy/mac_deploy.sh`) pulls the code and `private/`, runs `deploy/mac_setup.sh` (venv,
folders, the tests, the launchd files), restarts the **worker** service, and shows the services' status, `thrift
status` and the last 30 lines of the worker log. A red test rolls the Mac back to the commit before the pull; any
failure exits non-zero. Only one worker can run: `thrift run` holds a lock next to the DB and a second one refuses
to start ("another worker is already running (pid …)"); `services.sh start worker` refuses while a `thrift run` runs
in a Terminal window. Two workers would take each other's Telegram updates.
When the worker can't read the iCloud inbox (macOS hasn't allowed python3.14 into iCloud Drive yet), it retries
quietly and, after 2 minutes, sends ONE message saying where to allow it (System Settings → Privacy & Security → Files &
Folders → python3.14 → iCloud Drive), then "✓ inbox readable again" once it can. Any other repeated error is sent
once and then at most once a day.
Over SSH Claude deploys and checks status and logs after every work order, and may run status, logs, `requeue`,
`redo` and the tests; never `poster --publish-first`, the poster or anything that touches the live Poshmark account,
`mark-posted`, deleting data, or changes to `settings.local.yaml` / `.env`.

## Commands
```
thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>
thrift condition <item> nwt|like_new|good | kids <item> girls|boys | category <item> "<path>" | redo <batch>
thrift recover <item|batch> [--recheck] | reprocess <item>
thrift requeue <item> [marketplace] | requeue <batch> | mark-posted <item> <marketplace> <url> | status | show <item>
thrift poster [--once] [--dry-run] [--stage form|review] [--publish-first <item>] [--allow-dev-browser]
thrift login --site poshmark | telegram setup|test | harvest | build-style | eval
```
- **`thrift requeue b_…`** sends a failed batch (e.g. an API error during the split) back to the worker, which
  splits it again within ~15 s; a failed batch is never retried on its own. `thrift status` lists the open
  batches under the items: waiting for the worker, waiting for the contact sheet, or failed with the error.
- **`thrift price <item> <amount>`** approves an item's price from the command line — the same effect as replying
  to the Telegram approval message (source `owner`, item becomes `ready`). `thrift confirm` and `thrift answer` are
  the CLI twins of the batch-confirmation reply and of answering a question; `thrift kids <item> girls|boys` of the
  [Girls] [Boys] buttons. Each one sends the next message of the queue.
- **`thrift redo <batch>`** rebuilds a batch's items from the grouping already confirmed (no new contact sheet):
  every item that never reached the site is processed again — new cover, new price, a new card, one at a time — and
  its old Telegram messages are closed. The approved price is asked again; the owner's condition and Girls/Boys
  answers are kept. Items that are posting, posted, drafted, an unconfirmed publish, or dropped as a re-share are left
  as they are and listed. `thrift status` also shows the Telegram queue: the open question and what comes next.
- **`thrift recover <item|batch> [--recheck]`** recomputes only the cover (front, upright), the photo order, the
  category and the size of items that aren't on the marketplace; prices, approved prices, condition and Girls/Boys
  answers and the text stay, and nothing settled is asked again. The item's stored front check is kept, so running it
  twice changes nothing (`--recheck` asks the front and upright checks again). A card still waiting is sent again only
  when what it shows changed.
- **`thrift reprocess <item>`** sends one waiting (or ready) item through the pipeline again in place, with today's
  prompts and copy rules and the owner's answers kept; its card stays open meanwhile and is sent again only when it
  changed. **`thrift category <item> "Skirts › Skirt Sets"`** answers "Which category?"; **`thrift answer <item> "no
  brand"`** is the [No brand] button.
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
  cover; a flaw without a photo shows up as a warning on the price card.
- **Condition grades** (owner rule). Torn between Like New and Good, the item is Like New (the model names the grade
  it weighed against, code picks the higher; Poshmark has no "very good", so excellent goes up as Like New too). The
  shop never lists Fair: a fair reading goes up as Good with the warning "looked well-worn — listed as Good; check
  before approving". NWT still needs the attached hang tag in a photo, or the owner saying NWT.
- **Shoes: brand new or worn?** For shoes only, and only when the photos leave it open (the model is 30–80 % sure
  the pair is unworn, or it wavers between a new and a used grade), the item waits for ONE message before the price:
  the cover and "Brand new or worn? (couldn't tell from the photos)" with [NWT] [Like New] [Good]. The tap is the
  condition (the owner's word: NWT needs no hang-tag photo then; Like New means brand new without tags, "New without
  tags."; Good means worn). The item is repriced and rewritten with it, and the normal price card follows. A box in the
  photos makes an NWT pair "New in box.". CLI twin: `thrift condition <item> nwt|like_new|good`. Clothing, bags and
  accessories are never asked.
- **Item splitting.** The roll is split by the strongest model (`models.segment: claude-opus-5-5`) on 768 px previews
  (384 px when the request would pass `segmentation.max_request_mb`, or the API says it is too large). Visual identity
  decides — fabric, colour, print, shape, hardware, label; time is only a tiebreaker: the model sees "— pause 2 min —"
  where a gap is longer than max(30 s, 4 × this roll's median gap), and no clock times. Retail screenshots stay out of
  the timing. The code doubts an item that spans a pause AND a colour change, and a split with neither; the contact
  sheet marks each pause and the message says why a split is doubtful. Every batch keeps the pauses and colour
  distances, to tune the thresholds on real rolls.
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
The agent owns its Telegram bot and long-polls `getUpdates` from inside the worker (`thrift run`, on its own thread,
so answers are handled at once while items are processed) — no webhook, so it works behind NAT and after the Mac
wakes from sleep. One consumer; the update offset is persisted in SQLite (`kv` table). Only `TELEGRAM_CHAT_ID` and
the senders listed in `TELEGRAM_ALLOWED_USER_IDS` are accepted; everything else is ignored. Replies and buttons work
in a group with BotFather privacy mode ON. A price typed on its own, without replying, reaches the bot only when
privacy mode is OFF (then remove the bot from the group and add it again) or the bot is an admin of the group; in a
private chat with the bot it always does.

### Setup
1. Create the bot with **@BotFather** and copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Add the bot to the owner's group (or open a private chat with it) and send `/start` or any message there. For
   typed prices in a group: make the bot an admin (no rights needed), or `/setprivacy` → Disable in @BotFather and
   add the bot to the group again.
3. `thrift telegram setup` prints the chat ids and user ids seen in recent updates; fill `TELEGRAM_CHAT_ID` and
   `TELEGRAM_ALLOWED_USER_IDS` (comma-separated) in `.env`.
4. `thrift telegram test` sends a test message.
5. Set `telegram.enabled: true` in `config/settings.local.yaml`. Other settings: `telegram.resend_after_hours`
   (default 6) and `telegram.poll_timeout`. On prod, `thrift run` and `thrift poster` refuse to start when
   `telegram.enabled` is on but any of the three env vars is missing.

### Flow
- **The grouping is accepted automatically** (owner decision). After the split no contact sheet is sent: the
  batch's first message is its first item. The sheet is still saved (`work/<batch>/contact_sheet.png`) and the
  code's doubts are kept for `thrift status` ("grouping accepted with doubts"), only where the model itself was
  unsure of an item; a retail screenshot that matches no item is left out. Only a grouping that isn't a partition
  (a photo in no item, or in two) still sends the sheet, since taking it would lose or double a photo.
- **[Wrong photos]** on a price card is the way back when a grouping is wrong: the batch's contact sheet, as its
  items are now, becomes the one open message, answered with `12>2`, `split 7`, `merge 2 3`, `drop 7` or `ok`
  (`thrift confirm <batch> "<fix>"` from the CLI). Items whose photos didn't change keep everything; changed ones are
  rebuilt and their cards come back through the queue; new groups become items. Never for an item already on the
  marketplace, and while the fix is pending none of the batch's items is asked about or posted.
- **The old flow** (`segmentation.auto_confirm: false`, for testing): the worker sends the contact sheet with a
  summary; the owner replies `ok`, `12>2`, `split 7`, `merge 2 3` or `drop 7` — the same parser as `thrift confirm`.
  The sheet marks the photo after each pause ("pause 2 min"); the summary lists the pauses and the doubts.
- **One message at a time.** Everything that waits for the owner is one queue across batches and items: the oldest
  batch first — its contact sheet when one is asked, then its items in photo order (each item's questions, then its price card) — then
  the next batch. Only one message is open; the next is sent when it is answered, with a one-line confirmation first
  ("✓ $28 — 3 of 10 left"). Processing runs ahead, so the next card is usually ready at once; the queue never skips
  an item still being processed. The queue lives in the DB: it survives restarts, and a restart re-sends only the
  open message. Info-only messages (errors, posted confirmations) are not queued and are kept few: a dry-run that
  went fine says nothing unless `poster.notify_dry_runs` is on.
- **The price card, in Poshmark's words.** Cover photo, title, the size as Poshmark's size menu shows it ("7.5
  (Toddler Girl)"), the condition as Poshmark's label (NWT / Like New / Good), a warning only where a look is needed
  (the cover, a well-worn pair, a flaw no photo shows), and only the allowed questions. Buttons: **[✅ $X]** (the
  suggestion), four round prices near it (e.g. X−10, X−5, X+5, X+10; never under the floor), **[Later]** (the item
  goes to the end of the queue) and **[Change]**. A number typed on its own (`28`, `$28`, `28.00`) prices the open
  card (under the floor it is not taken — reply to the card for that); replying to the card with a number still
  works. The approved price is stored with source `owner` and the item becomes `ready`. Nothing publishes without
  price approval.
- **Questions on the card.** If brand or size is truly unreadable (below 0.70), the category is not one Poshmark has,
  or the item looks like a re-share, the question is on the card. The reply may carry both answers and the price
  (`size 8, 45`): the price is stored, the note reprocesses the item, and it comes back only if something is still
  unresolved — the owner's price is kept. A price alone accepts the model's best reading of brand and size.
- **Two answers must be explicit.** *NWT without a hang-tag photo* is listed as **Like New** and the card asks; reply
  `NWT` (with or without the price) if the tag really is attached. *A suspected re-share* stays held even after a
  price: reply `different item` to list it or `same item` to drop it.
- **`needs_owner`.** The poster may ask its own question when it is stuck on a field only the owner can answer (a
  brand, category or size missing from Poshmark's lists); it is asked in its turn. The item waits in `needs_owner`
  while the others continue; the reply is attached to the item and it is reprocessed.
- **Re-send after sleep.** Once the open message has waited longer than `telegram.resend_after_hours`, it is sent
  again (checked when the worker starts and about every hour) — Telegram keeps updates for only 24 h and the Mac may
  have been asleep. A restart (every deploy) never repeats a card the owner has just been sent.
- **CLI equivalents** keep working: `thrift confirm`, `thrift answer`, `thrift price <item> <amount>`, `thrift
  condition`, `thrift kids`.

Item statuses: `new` → (`awaiting_condition` →) `awaiting_price` | `needs_info` → `ready` → `posting` → `posted` |
`drafted` | `failed`; `needs_owner` while the poster's question is open; `dropped` for a re-share of a listed item.

## Roadmap
Where the project is going, and the design decisions already made for each step.

- **Telegram approval flow — implemented (M3).** The agent owns the bot and long-polls `getUpdates` from inside the
  worker — no webhook, so it works after the Mac wakes from sleep (Telegram keeps updates for 24 h; pending
  approvals are re-sent). n8n does *not* attach a Telegram trigger to this bot. One message at a time, one-tap
  prices; nothing publishes without price approval; `needs_owner` for the poster's own questions.
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
