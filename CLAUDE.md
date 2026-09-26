# Thrift Agent — spec for Claude Code

Photos shot on an iPhone become live Poshmark (then Depop) listings with no typing. The seller shoots,
taps Share → "New item", and gets a Telegram ping when it's listed or when something needs an answer.

## Machines
- **Windows PC (dev)** — code, tests, eval on fixture photos. `machine_role: dev` forces dry-run;
  the dev machine never logs into or posts to a marketplace.
- **MacBook (prod)** — MacBook Pro 13" M2, 8 GB, macOS 27 Golden Gate, Python 3.14 (python.org), bash shell, no Homebrew. Runs `thrift run` (worker) and `thrift poster` (Chrome) as launchd agents.
  The poster's Chrome profile is created and logged in *on the Mac only* (cookies are keychain-bound).
- **iPhone (dedicated)** — same Apple ID as the Mac; saves photos to iCloud Drive `Posh/inbox/<ts>/`.
- Deploy: `git push` from PyCharm → `deploy\deploy.ps1` (ssh, pull, test, restart services).

## Flow
```
iCloud Posh/inbox/<ts>/ (+ _done) ─► register batch ─► prep (HEIC→JPEG, EXIF rotate, burst dedupe)
  ─► segment (one vision call on thumbnails → groups; code checks partition, full shot, sizes, confidence)
  ─► contact sheet → Telegram; owner replies ok|12>2|split 7|merge 2 3|drop 7 (or `thrift confirm <batch> …`)
  ─► items ─► extract Facts (evidence per field) ─► price (brand_tiers.yaml) ─► copy (both marketplaces)
  ─► verify (LLM strip unsupported claims) + lint (deterministic) ─► gate: publish | draft | needs_info
  ─► awaiting_price ─► Telegram: ONE message [Approve $P] [Change] (open questions folded in) ─► ready
  ─► poster: fill form → read back → diff → dry-run | draft | publish → verify live page → record URL
     (stuck on an owner-only field → separate question, item waits in `needs_owner`, the others continue)
```
Item statuses: `new` → `awaiting_price` | `needs_info` → `ready` → `posting` → `posted` | `drafted` | `failed`;
`needs_owner` while the poster's question is open. Nothing publishes without price approval.

## Invariants — do not break these
1. **Facts before prose.** `extract` never writes copy; `copy` may only restate Facts. Null facts are omitted.
2. **NWT only with a hang-tag photo** (or a seller note). Mislabeled condition is the most common cause of
   "not as described" cancellations and bad ratings. When unsure between two conditions, the lower one.
   - **Owner rule (first run).** `gate.min_confidence` is 0.70 for brand, size and condition; the price is the
     owner's only routine input (Telegram approval or `thrift price`). NWT stays strict — `hang_tag_photo` or a
     seller note, never confidence. A price-table miss no longer blocks the gate: the per-department
     `category_defaults` (Women/Men/Kids/Unisex/Home, each with `other`) become the suggestion in the owner's
     message, marked "no price history for <brand>".
3. **The model never clicks publish.** Deterministic code fills, reads back, diffs, then publishes.
   An LLM fallback (Playwright MCP) may *fill* a form when a selector breaks; code still verifies and submits.
4. **Idempotent posting.** Row → `posting` before the form opens. A `posting` row after a crash is never
   retried automatically; reconcile against the closet first. One item ID posts once per marketplace, ever.
5. **Stop, don't guess.** Logged out / CAPTCHA / restricted account → write `PAUSE`, ping, exit. Unknown modal
   → fail the item with a screenshot. Never solve CAPTCHAs, never type credentials.
6. **Human pacing, human hours.** `schedule` limits; no sharing/following/offers/relisting from this code.
7. **All selectors live in `post/<marketplace>.py:SEL`.** Role/label/placeholder locators, not CSS classes.
8. **Shipping guard.** `HOLD_UNSHIPPED` flag pauses posting. Marketplaces restrict accounts with late shipments;
   listing faster without shipping discipline makes that worse.
   - `HOLD_UNSHIPPED` blocks publish only; drafts and dry-runs still run.

## What's verified vs not
- Verified against the live site (Sep 2026): order list pagination button `button[data-et-name="pagination_next"]`,
  order links `/order/sales/<id>`, order page `__INITIAL_STATE__.$_order_details.order` (fields in harvest.py),
  listing page `__INITIAL_STATE__.$_listing_details.listingDetails`, create page `https://poshmark.com/create-listing`,
  restricted-account banner text.
- **UNVERIFIED:** every create-listing selector in `post/poshmark.py:SEL`, the Poshmark condition options, the kids
  size-option mapping (`post/poshmark.py:KIDS_SIZE_OPTIONS`), all of Depop. Record with
  `playwright codegen --channel chrome https://poshmark.com/create-listing` on the Mac.

## Seller data (private)
Seller-specific data — brand price tiers, real listing examples, sales findings, account notes, the closet username —
lives in `private/`, a **separate private repo** cloned into this folder and git-ignored here.
When `private/NOTES.md` exists, read it before changing prompts, pricing, or gate rules.
Without `private/`, the code falls back to `config/*.example.yaml` and `data/style_examples/`.

## Commands
`thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>`
`thrift requeue <item> [marketplace] | status | show <item> | poster [--once] [--dry-run] [--allow-dev-browser]`
`thrift login --site poshmark | telegram setup|test | harvest | build-style | eval`
`confirm`, `answer` and `price` are the CLI twins of the Telegram replies; `telegram setup` prints the chat/user ids
seen in recent updates, `telegram test` sends a test message.
`--allow-dev-browser` lets the poster open a browser on the dev machine for selector work; it stays dry-run and
never logs into or touches the shop.

## Retail screenshots
The owner shares retailer screenshots (product page with price, style name, colour) together with the item photos.
- Detected by code: no camera EXIF + phone aspect ratio. Assigned to items by content.
- Used only for `retail_price`, `style_name`, colour and retailer — never for condition, size or flaws
  (the screenshot shows a new item, not this one).
- Always last in the listing, never the cover. Original Price = retail price.

## Copy rules
- **Materials need evidence.** `item_type`/`features` may not name a material (leather, suede, wool, …) unless
  `facts.material` has label/stamp evidence; texture words (woven, quilted, ribbed, glitter) are fine. The verifier
  treats material words as claims; lint flags any material word in title/description/tags that `facts.material`
  doesn't support.
- **Kids sizes carry their system** in title, description and the listing's size field: "EU 24 / US Toddler 7.5"
  (C sizes = Toddler / Little Kid, Y = Big Kid); never a bare "US 7.5" for kids. Lint accepts the size token in every
  form the copy rules produce ("size 7.5", "US 7.5", "(US 7.5)", "EU 24 / US Toddler 7.5"). The Poshmark kids
  size-option mapping (`KIDS_SIZE_OPTIONS`) is UNVERIFIED — record it in M2.

## Telegram (M3)
- The agent owns the bot: long polling (`getUpdates`) inside the worker (`thrift run`), one consumer, offset persisted
  in SQLite (`kv`). Only `TELEGRAM_CHAT_ID` and senders in `TELEGRAM_ALLOWED_USER_IDS` (`.env`, comma-separated) are
  accepted; everything else is ignored. Works in a group with BotFather privacy mode ON — the owner only replies to
  the bot's messages or presses its buttons.
- Batch: contact sheet + summary; reply `ok | 12>2 | split 7 | merge 2 3 | drop 7` (same parser as `thrift confirm`);
  `segmentation.always_confirm` stays configurable.
- Item: ONE message in `awaiting_price` — cover, title, size (with system), condition + flaw count, suggested price +
  basis, "Retail $X" if known, "no price history for <brand>" for a category default; [Approve $P] [Change]. A reply
  with a number (`85`, `$85`, `85.00`, `85 dollars`) sets the price (source `owner`) → `ready`. Unreadable brand/size
  (< 0.70), NWT without hang-tag photo, or a suspected re-share is folded into the same message; a reply like
  `size 8, 45` stores the price and reprocesses with the note; it comes back only if still unresolved (price kept).
- `needs_owner`: the poster's separate question (brand missing from Poshmark's list, ambiguous category); other items
  continue; the reply is attached and the item reprocessed.
- Re-send: on worker start and about hourly, anything waiting longer than `telegram.resend_after_hours` (default 6)
  is sent again (Telegram keeps updates 24 h; the Mac sleeps).
- Settings: `telegram.enabled`, `telegram.resend_after_hours`, `telegram.poll_timeout`. On prod, `thrift run` and
  `thrift poster` refuse to start when `telegram.enabled` but any of the three env vars is missing.

## Milestones
- **M0 eval** — 20–30 real sessions in `eval/fixtures/<case>/{photos,expected.yaml}` (mostly shoes, some with
  retail screenshots); `thrift eval`. The ~100 harvested sold listings are an extra set for brand/size/category
  only — not condition, historic labels are unreliable.
  Targets before autopublish: segmentation ≥95%, brand/size ≥95%, condition ≥90%.
- **M1 prompt tuning** — tune the segment/extract/copy/verify prompts against M0.
- **M2 Poshmark poster** — record SEL on the Mac, complete `read_back` (category, size, brand, colors), crop dialog,
  draft path; a week of dry-runs.
- **M3 Telegram approval flow — done (v1):** long polling in the worker, one approval message per item
  ([Approve $P] [Change], questions folded in), `needs_owner` for the poster's own questions, re-send of pending
  messages after sleep. Nothing left code-wise; the owner creates the bot with @BotFather and fills
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` in `.env` (`thrift telegram setup|test`).
- **M4 Airtable + n8n + My Sales sync** — business view, sale email → sold + delist elsewhere, shipping watchdog
  (`HOLD_UNSHIPPED`), local HTTP API over Tailscale, daily read-only order-status sync.
- **M5 Depop** adapter.
- **M6 sold-comps pricing** — tune `private/brand_tiers.yaml` from new sales.

## Style
Python 3.11+, pydantic v2, pathlib everywhere (Windows + macOS). Pure functions for anything testable
(gate, price, scheduler, segmentation checks, corrections). `pytest -q` must pass before deploy.
Tests never read `config/settings.local.yaml`, `private/` or `.env` (tests/conftest.py isolates them); a test that
needs prod behaviour or private data opts in with the `settings_override` fixture.
