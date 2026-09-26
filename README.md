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
- **Kids sizes carry their system** in the title, the description and the listing's size field: "EU 24 / US Toddler
  7.5" (C sizes = Toddler / Little Kid, Y = Big Kid), never a bare "US 7.5" for kids. Lint accepts the size token in
  every form the copy rules produce ("size 7.5", "US 7.5", "(US 7.5)", "EU 24 / US Toddler 7.5"). The Poshmark kids
  size-option mapping (`KIDS_SIZE_OPTIONS` in `post/poshmark.py`) is unverified until M2.
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
Then: `.env`, `config/settings.local.yaml` (`machine_role: prod`), `thrift login --site poshmark`, record selectors,
and start the two launchd services (worker + poster) for the dry-run week:
```bash
bash deploy/services.sh start      # also: stop | restart | status
```
`thrift login` needs the poster's Chrome profile to itself: `bash deploy/services.sh stop` first, `start` afterwards.
Deploy updates from Windows with `deploy\deploy.ps1` (pull, test, `services.sh restart`; a red test rolls the Mac back
to the previous commit).

## Commands
```
thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>
thrift requeue <item> [marketplace] | status | show <item> | poster [--once] [--dry-run] [--allow-dev-browser]
thrift login --site poshmark | telegram setup|test | harvest | build-style | eval
```
- **`thrift price <item> <amount>`** approves an item's price from the command line — the same effect as replying
  to the Telegram approval message (source `owner`, item becomes `ready`). `thrift confirm` and `thrift answer` are
  the CLI twins of the batch-confirmation reply and of answering a question.
- **`thrift telegram setup | test`** — `setup` prints the chat ids and user ids seen in the bot's recent updates so
  the owner can fill `TELEGRAM_CHAT_ID` and `TELEGRAM_ALLOWED_USER_IDS`; `test` sends a test message. See
  "Telegram approval (M3)".
- **`thrift requeue <item> [marketplace]`** puts an item back in the posting queue — after a failed attempt, a fix,
  or an owner answer — for one marketplace or all of them. Posting stays idempotent: an item that already has a
  live URL on a marketplace is never posted there again.
- **`thrift poster --allow-dev-browser`** lets the poster open a browser on the Windows dev machine to work on
  selectors. It stays a dry-run: the dev machine never logs into or touches the shop, and without the flag the dev
  poster does not launch a browser at all.
- **`HOLD_UNSHIPPED`** is a flag file in `paths.control` (set by the shipping watchdog or by hand). It blocks
  *publish* only: drafts and dry-runs keep running, so the queue is ready the moment the late order ships. `PAUSE`
  in the same folder stops the poster entirely.
- **Retail screenshots.** Share the retailer's product-page screenshot together with the item photos. The agent
  detects it (no camera EXIF, phone aspect ratio), assigns it to the item by content, and uses it only for the
  retail price, style name, colour and retailer — never for condition, size or flaws. Own photos first: the
  screenshot always goes last in the listing and is never the cover.

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
- **Questions folded in.** If brand or size is truly unreadable (below 0.70), NWT lacks a hang-tag photo, or the item
  looks like a re-share, the question is part of the *same* message. The reply may carry both answers and the price
  (`size 8, 45`): the price is stored, the note reprocesses the item, and it comes back for approval only if something
  is still unresolved — the owner's price is kept.
- **`needs_owner`.** The poster may send a *separate* question when it is stuck on a field only the owner can answer
  (a brand missing from Poshmark's list, an ambiguous category). The item waits in `needs_owner` while the others
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
- **Milestones:** M0 eval → M1 prompt tuning → M2 Poshmark poster (record selectors on the Mac) → M3 Telegram
  approval flow (implemented: long polling, one message per item) → M4 Airtable + n8n + My Sales sync → M5 Depop
  → M6 sold-comps pricing.
