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
  ─► contact sheet → Telegram (always, until eval says otherwise) ─► `thrift confirm <batch> ok|12>2|split 7|merge 2 3`
  ─► items ─► extract Facts (evidence per field) ─► price (brand_tiers.yaml) ─► copy (both marketplaces)
  ─► verify (LLM strip unsupported claims) + lint (deterministic) ─► gate: publish | draft | needs_info
  ─► poster: fill form → read back → diff → dry-run | draft | publish → verify live page → record URL
```

## Invariants — do not break these
1. **Facts before prose.** `extract` never writes copy; `copy` may only restate Facts. Null facts are omitted.
2. **NWT only with a hang-tag photo** (or a seller note). Mislabeled condition is the most common cause of
   "not as described" cancellations and bad ratings. When unsure between two conditions, the lower one.
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
- **UNVERIFIED:** every create-listing selector in `post/poshmark.py:SEL`, the Poshmark condition options, all of
  Depop. Record with `playwright codegen --channel chrome https://poshmark.com/create-listing` on the Mac.

## Seller data (private)
Seller-specific data — brand price tiers, real listing examples, sales findings, account notes, the closet username —
lives in `private/`, a **separate private repo** cloned into this folder and git-ignored here.
When `private/NOTES.md` exists, read it before changing prompts, pricing, or gate rules.
Without `private/`, the code falls back to `config/*.example.yaml` and `data/style_examples/`.

## Commands
`thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | requeue <item> [marketplace]`
`thrift status | show <item> | poster [--once] [--dry-run] [--allow-dev-browser] | login --site poshmark`
`thrift harvest | build-style | eval`
`--allow-dev-browser` lets the poster open a browser on the dev machine for selector work; it stays dry-run and
never logs into or touches the shop.

## Retail screenshots
The owner shares retailer screenshots (product page with price, style name, colour) together with the item photos.
- Detected by code: no camera EXIF + phone aspect ratio. Assigned to items by content.
- Used only for `retail_price`, `style_name`, colour and retailer — never for condition, size or flaws
  (the screenshot shows a new item, not this one).
- Always last in the listing, never the cover. Original Price = retail price.

## Milestones
- **M0 eval** — 20–30 real sessions in `eval/fixtures/<case>/{photos,expected.yaml}` (mostly shoes, some with
  retail screenshots); `thrift eval`. The ~100 harvested sold listings are an extra set for brand/size/category
  only — not condition, historic labels are unreliable.
  Targets before autopublish: segmentation ≥95%, brand/size ≥95%, condition ≥90%.
- **M1 prompt tuning** — tune the segment/extract/copy/verify prompts against M0.
- **M2 Poshmark poster** — record SEL on the Mac, complete `read_back` (category, size, brand, colors), crop dialog,
  draft path; a week of dry-runs.
- **M3 Telegram approval flow** — one message per item, [Approve] [Change], nothing publishes without price
  approval; `needs_owner` for the poster's own questions.
- **M4 Airtable + n8n + My Sales sync** — business view, sale email → sold + delist elsewhere, shipping watchdog
  (`HOLD_UNSHIPPED`), local HTTP API over Tailscale, daily read-only order-status sync.
- **M5 Depop** adapter.
- **M6 sold-comps pricing** — tune `private/brand_tiers.yaml` from new sales.

## Style
Python 3.11+, pydantic v2, pathlib everywhere (Windows + macOS). Pure functions for anything testable
(gate, price, scheduler, segmentation checks, corrections). `pytest -q` must pass before deploy.
