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
  ─► segment (one Opus vision call on 768 px previews → groups; visual identity first, "— pause 2 min —" markers
     relative to the roll as the only timing; code checks partition, full shot, sizes, confidence, and — only for an
     item the model was unsure of — non-contiguous photos, a pause AND a colour change inside it / neither between two)
  ─► contact sheet → Telegram; owner replies ok|12>2|split 7|merge 2 3|drop 7 (or `thrift confirm <batch> …`)
  ─► items ─► extract Facts (evidence per field) ─► price (brand_tiers.yaml) ─► copy (both marketplaces)
  ─► verify (LLM strip unsupported claims) + lint (deterministic) ─► gate: publish | draft | needs_info
  ─► (shoes in doubt, brand new vs worn: awaiting_condition ─► "Brand new or worn?" [NWT] [Like New] [Good] ─►
      reprocessed with the answer)
  ─► (a kids item, under 0.70 sure of Girls/Boys: "Girls or Boys?" [Girls] [Boys])
  ─► awaiting_price ─► the price card [✅ $X] [4 nearby prices] [Later] [Change] (allowed questions folded in) ─► ready
     (Telegram is ONE queue: one open message at a time, the oldest batch first — see "Telegram")
  ─► poster: fill form → read back → diff → dry-run | draft | publish → verify live page → record URL
     (publish: List This Item once → Poshmark's closet ?created_listing_id=<id> → the closet reloaded until that
      listing shows, ≤ 90 s → its /listing/<slug>-<id> address; not found → "unconfirmed publish", `mark-posted`)
     (dry-run stage form | review: never the publish button; the form is left through Poshmark's Discard;
      after the upload Poshmark's cover dialog is applied with its default crop, any other dialog fails the item;
      each dry-run compares Poshmark's Drafts count before and after — more drafts → Telegram warning + Outcome note)
     (stuck on an owner-only field → separate question, item waits in `needs_owner`, the others continue)
```
Item statuses: `new` → (`awaiting_condition` →) `awaiting_price` | `needs_info` → `ready` → `posting` → `posted` |
`drafted` | `failed`; `needs_owner` while the poster's question is open; `dropped` (a re-share the owner called the
same item). Nothing publishes without price approval.

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
     is never "no flaws", "like new" or "excellent". Every flaw has a photo that is in the listing, never the cover
     (`pipeline.photo_order` / `fit_photos`); a flaw without a photo is a warning on the price card. The copy
     writer never sees the flaws or the condition evidence; `copy.condition_rule` runs after the verifier (drops a
     sentence with wear words, puts the line in) and `verify.lint` checks the result (wear words, used-item claims,
     the line and the flaw photos).
   - **The only questions.** Brand or size below 0.70, NWT without a hang-tag photo, category "Other" (or a
     department/category not on Poshmark's list, `data/poshmark_taxonomy.yaml`), a possible re-share, a pair of shoes in
     doubt between brand new and worn (its own message, before the price), Girls or Boys for a kids item the model is
     under 0.70 sure of (its own message, WO20), and the poster's `needs_owner`. The model's own `questions` about
     optional facts (material, measurements) are dropped — a missing optional fact is left out of the listing; an
     unsure condition is kept in the item's record (`gate.info`), never on the card. CLI actions (`thrift price` /
     `answer` / `confirm` / `condition` / `kids` / `redo`) are echoed to the Telegram group and settle the pending
     message.
3. **The model never clicks publish.** Deterministic code fills, reads back, diffs, then publishes.
   An LLM fallback (Playwright MCP) may *fill* a form when a selector breaks; code still verifies and submits.
4. **Idempotent posting.** Row → `posting` before the form opens. A `posting` row after a crash is never
   retried automatically; reconcile against the closet first. One item ID posts once per marketplace, ever.
   - A publish that pressed List This Item but found no listing address is `failed` with `last_error`
     "unconfirmed publish: …" — it may be live. `thrift requeue` and `--publish-first` refuse it; check the closet,
     then `thrift mark-posted <item> <marketplace> <url>` (only for such a row: the URL must be a listing page no
     other item holds, showing the item's title and price; then `posted` + URL, "✅ confirmed live" in Telegram).
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
- **UNVERIFIED:** the Promote My Closet toggle's markup (`promote_toggle`: one checkbox → must be unchecked; none →
  the panel must read "Promote My Closet Off"; anything else stops before List — the first publish passed through the
  one-checkbox path); where Save Draft lands
  (`draft_saved`); the size field and Done for adult sizes; the CAPTCHA wording; the Baby-tab size labels, kids
  clothing and Plus size tabs; the Men/Home category lists; all of Depop. Record them from the evidence in
  `failed/shots/` (`.png`/`.html`/`.json` per run, `<item>-review.json`, `…-after-list.*`) or with
  `playwright codegen --channel chrome https://poshmark.com/create-listing` on the Mac.

## Seller data (private)
Seller-specific data — brand price tiers, real listing examples, sales findings, account notes, the closet username —
lives in `private/`, a **separate private repo** cloned into this folder and git-ignored here.
When `private/NOTES.md` exists, read it before changing prompts, pricing, or gate rules.
Without `private/`, the code falls back to `config/*.example.yaml` and `data/style_examples/`.

## Commands
`thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | price <item> <amount>`
`thrift condition <item> nwt|like_new|good` (the CLI twin of the shoe question's buttons)
`thrift kids <item> girls|boys` (the twin of [Girls] [Boys]) | `thrift redo <batch>` (rebuild a batch's unposted items)
`thrift requeue <item> [marketplace] | requeue <batch> | mark-posted <item> <marketplace> <url> | status | show <item>`
`thrift poster [--once] [--dry-run] [--stage form|review] [--publish-first <item>] [--allow-dev-browser]`
`thrift login --site poshmark | telegram setup|test | harvest | build-style | eval`
`thrift requeue b_…` sends a failed batch back to the worker (failed batches are never retried on their own);
`thrift status` lists the open batches (waiting for the worker, the contact sheet, or failed with their error) and the
Telegram queue (the open question, what comes next).
`thrift redo <batch>` rebuilds a split batch's items from the grouping already confirmed (no new contact sheet): every
item that never reached the site goes back to `new` — its price and approved price, dry-run post rows and Telegram
messages dropped; the owner's condition and Girls/Boys answers kept — and the worker processes it again; new cards
follow, one at a time. Left alone, and listed: posting / posted / drafted, a post with a URL or an unconfirmed publish,
and an item the owner dropped as a re-share. Refuses a batch with nothing to rebuild.
`thrift requeue` also takes back an item the poster parked in `needs_owner` as it is (no reprocessing, the question
closed) — the retry after a poster fix. `confirm`, `answer` and `price` are the CLI twins of the Telegram replies;
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
true` (both off by default; plus `marketplaces.<mp>.autopublish: true`, else a publish-gated item is a draft, which
still refuses: `draft_saved` UNVERIFIED). `mark-posted <item> <marketplace> <url>` records a listing found by hand for
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
- **Materials need evidence.** `item_type`/`features` may not name a material (leather, suede, wool, …) unless
  `facts.material` has label/stamp evidence; texture words (woven, quilted, ribbed, glitter) are fine. The verifier
  treats material words as claims; lint flags any material word in title/description/tags that `facts.material`
  doesn't support.
- **Style tags are Poshmark's own.** Only the 130 curated tags its form offers (`data/poshmark_taxonomy.yaml:
  style_tags`, recorded 2026-09-30), spelled Poshmark's way, at most 3. The copy prompt lists them; `fit_style_tags`
  drops anything else before lint, including a material tag (Leather, Suede, Wool, …) that `facts.material` doesn't
  back — tags are optional, so an unusable one is left out, never a question or a draft.
- **The cover is the front of the item (owner rule, WO20):** the item alone, its front, flat lay or on a hanger, on a
  clean background — never the back, never worn / try-on / mirror, never a label, tag, flaw, box or screenshot. The
  model names every photo's role (`facts.photo_roles`: front, back, side, detail, label, tag, flaw, worn, box, other)
  and picks `cover_photo` by that rule; code checks it (`pipeline.choose_cover`): only a photo of the item alone
  (front, else side, else back; the model's pick first among equals), never a flaw photo or a screenshot. No front
  photo → the best item-alone photo and the card says "cover: no front flat-lay photo". The rest of the photo order is
  the model's (screenshots last). Facts from before WO20 (no roles) keep the model's pick.
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
  worn?", "Girls or Boys?", price card, the poster's question — is one queue read from the DB (`approve.queue`): the
  oldest batch first, its contact sheet, then its items in photo order (each item's questions, then its card), then
  the next batch. At most one message is open; `approve.pump` sends the next when it is answered (a send lock in
  `kv` keeps the worker's threads, the poster and a CLI command from both sending). The worker processes in the
  same order and runs ahead, so the next card is usually ready at once; the queue never skips an item still being
  processed (an item sent back by an answer is processed first; an answer that lands while its item is being
  processed wins: that result is dropped and the item processed again). After an answer: one line, "✓ $28 — 3 of 10 left"
  (the items queued since the queue was last empty), then the next message. [Later] puts the item behind everything
  queued so far (`items.deferred_at`). The queue is the DB: a restart re-sends only the open message; older unanswered
  copies are closed (their buttons still work). Info-only messages (errors, posted confirmations, the Drafts warning,
  CLI echoes) are not queued; a successful dry-run says nothing unless `poster.notify_dry_runs`.
- Batch: contact sheet + summary; reply `ok | 12>2 | split 7 | merge 2 3 | drop 7` (same parser as `thrift confirm`);
  the sheet marks the photo after each pause ("pause 2 min"), the message lists them and says why a split is doubtful;
  `segmentation.always_confirm` stays configurable.
- Condition question (WO18, shoes in doubt only): ONE message in `awaiting_condition`, before the price card — cover,
  title, "Brand new or worn? (couldn't tell from the photos)", [NWT] [Like New] [Good]; a typed reply works too (nwt /
  like new / good). Outbox kind `condition`, re-sent while pending; the tap reprocesses the item and the price card
  follows ("Condition: NWT (your answer)").
- The price card, in Poshmark's words (WO20): cover, title, the size as Poshmark's size menu shows it ("7.5 (Toddler
  Girl)"), the condition as Poshmark's label (NWT / Like New / Good — excellent is Like New), a warning only where a
  look is needed (the cover, a well-worn pair, a flaw no photo shows) and only the allowed questions; no flaw count,
  no basis, no retail. Buttons: [✅ $X] (the suggestion), four round prices near it ($5 apart under $100, $10 to $250,
  $25 above; never under the floor; `approve.price_options`), [Later] [Change]. A number typed on its own (`28`,
  `$28`, `28.00`) prices the open card (under the floor it is not taken: reply to the card for that); a reply with a
  number works as before and sets the price (source `owner`) → `ready`. Unreadable brand/size (< 0.70), a
  department/category Poshmark doesn't have, or a suspected re-share is a question on the card; a reply like `size 8,
  45` stores the price and reprocesses with the note; it comes back only if still unresolved (price kept). Two
  questions are never settled by a price alone: NWT without a hang-tag photo is listed as Like New (the card asks)
  unless the reply says `NWT`; a re-share hold needs `different item` (list it) or `same item` (drop it).
- `needs_owner`: the poster's question (brand missing from Poshmark's list, ambiguous category), kept with the item
  (`items.owner_question`) and asked in its turn; other items continue; the reply is attached and the item
  reprocessed.
- Re-send: on worker start, and about hourly once it has waited longer than `telegram.resend_after_hours` (default 6),
  the open message — only that one — is sent again (Telegram keeps updates 24 h; the Mac sleeps).
- Settings: `telegram.enabled`, `telegram.resend_after_hours`, `telegram.poll_timeout`. On prod, `thrift run` and
  `thrift poster` refuse to start when `telegram.enabled` but any of the three env vars is missing.

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
  Left: where Save Draft lands (`draft_saved`); the Promote toggle's markup; a week of dry-runs, then the two keys.
- **M3 Telegram approval flow — done (v1):** long polling in the worker, one approval message per item
  ([Approve $P] [Change], questions folded in), `needs_owner` for the poster's own questions, re-send of pending
  messages after sleep. Nothing left code-wise; the owner creates the bot with @BotFather and fills
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` in `.env` (`thrift telegram setup|test`).
  Done (WO20, after the first live test with two batches): one message at a time across batches and items, one-tap
  prices, Later, a typed number, always a price, the cover = the front of the item, the card in Poshmark's words,
  Girls/Boys asked below 0.70, a quieter contact sheet, `thrift redo <batch>`.
- **M4 Airtable + n8n + My Sales sync** — business view, sale email → sold + delist elsewhere, shipping watchdog
  (`HOLD_UNSHIPPED`), local HTTP API over Tailscale, daily read-only order-status sync.
- **M5 Depop** adapter.
- **M6 sold-comps pricing** — tune `private/brand_tiers.yaml` from new sales.

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
