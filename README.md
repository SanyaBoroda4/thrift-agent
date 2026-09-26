# Thrift Agent

![tests](https://github.com/SanyaBoroda4/thrift-agent/actions/workflows/tests.yml/badge.svg)

An AI listing agent for a resale closet: shoot items on an iPhone, tap **Share → New Item**, and the agent
splits the photo roll into items, reads brand/size/condition from the photos with evidence for every fact,
prices from the seller's own sales history, writes the listing in the seller's style, checks it for
unsupported claims, and posts it to Poshmark (Depop next) through a real, paced browser session.

- **Pipeline:** iCloud Drive inbox → segmentation → vision extraction → pricing → copy → verifier + lint → gate → poster
- **Stack:** Python 3.14, Claude API (vision + tool use), Pydantic, Playwright (real Chrome), SQLite, launchd
- **Design rules:** facts before prose, the model never presses publish, idempotent posting, stop-don't-guess

Full spec and invariants: **CLAUDE.md**.

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
thrift init | run | process <dir> | confirm <batch> <cmd> | answer <item> "<note>" | requeue <item> [marketplace]
thrift status | show <item> | poster [--once] [--dry-run] [--allow-dev-browser] | login --site poshmark
thrift harvest | build-style | eval
```
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

## Roadmap
Where the project is going, and the design decisions already made for each step.

- **Telegram approval flow.** The agent owns the bot and long-polls `getUpdates` — no webhook, so it works after
  the Mac wakes from sleep (Telegram keeps updates for 24 h; on wake, any approvals still pending are re-sent).
  n8n does *not* attach a Telegram trigger to this bot. **One message per item:** a price approval with
  **[Approve] [Change]**; replying with a number changes the price; "Retail $X" is shown when known. Nothing
  publishes without price approval. If brand or size is truly unreadable, that question is folded into the same
  message. The poster may send a *separate* question only when it is stuck on a field only the owner can answer
  (a brand missing from Poshmark's list, an ambiguous category); the item waits in `needs_owner` while the others
  continue. No other questions go to the owner.
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
  approval flow → M4 Airtable + n8n + My Sales sync → M5 Depop → M6 sold-comps pricing.
