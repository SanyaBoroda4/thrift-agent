# eBay — the owner's steps (WO57)

eBay is the fourth site. The agent lists there through **eBay's official API**: plain HTTPS calls, no browser, nothing
opens on the Mac. You do four small things, once. Nothing here needs the Mac.

**Never put a key, a secret or a token in a chat.** They live in two places only: the file `private/.env` on the PC,
and Azure's settings (Claude Code copies them there with one command). The agent never prints them.

What you need at hand:
- `<API_URL>` — the API's address. On the PC it is the `API_URL` line of `var/azure/keys.txt` (it looks like
  `https://thrift-api-xxxxxx.azurewebsites.net`).
- The three addresses and the token below. To see them exactly as they must be typed, run this **in your own
  terminal** on the PC (PowerShell, in the project folder) — it prints only on a real terminal, never in a log:

  ```
  .\.venv\Scripts\python.exe deploy\azure\provision.py ebay show
  ```

  It prints: the **Notification Endpoint URL** (`<API_URL>/ebay/account-deletion`), the **Verification token**, the
  **Auth Accepted URL** (`<API_URL>/ebay/oauth/callback`), the **Auth Declined URL** (`<API_URL>/ebay/oauth/declined`)
  and the **Privacy Policy URL** (`<API_URL>/ebay/privacy`).

---

## Steps 1–4 ✅ done (Nov 11, 2026: keys in Azure, consent given — valid until May 12, 2028)

## Step 1 — the developer account ✅ done
Created Nov 10, 11:00 AM. eBay approves it in about a day (an email comes).

## Step 2 — when it is approved: the keys and the account-deletion notice

1. Sign in at **developer.ebay.com** → **Application Keys** (https://developer.ebay.com/my/keys).
2. Under **Sandbox**, select **Create a keyset**. Then under **Production**, select **Create a keyset**
   (application title: anything, e.g. `thrift agent`).
   Each keyset shows three values: the **App ID (Client ID)**, the **Dev ID**, the **Cert ID (Client Secret)**.
3. The Production keyset says **"Your Keyset is currently disabled"** until the next part is done — that is normal.
   Next to the Production App ID click **Notifications** (the page "Alerts and Notifications").
   - Under **Event Notification Delivery Method** choose **Marketplace Account Deletion**.
   - **Email address**: yours → **Save**.
   - **Notification Endpoint URL**: the one `ebay show` printed (it ends in `/ebay/account-deletion`).
   - **Verification token**: the one `ebay show` printed.
   - **Save**. eBay calls the address at once; the agent's API answers its check, and the page says it is saved.
     (The endpoint is live already — it was put up on Nov 10 so this step can't fail for our part.)
   - Click **Send Test Notification** once. It should say the notification was received.
   - Do **not** use the "Not persisting eBay data" switch: the endpoint way is the one set up.
4. Put the keys where the agent reads them — the file `private\.env` on the PC (make it if it isn't there; it is in
   the private repo's ignore list and never leaves the PC except into Azure's settings). The Production values while
   the agent lists for real; the Sandbox values only for the one-day test before that (Claude Code says when):

   ```
   EBAY_ENV=production
   EBAY_CLIENT_ID=<App ID (Client ID)>
   EBAY_CLIENT_SECRET=<Cert ID (Client Secret)>
   EBAY_RUNAME=<the RuName from step 3>
   ```

   (The Dev ID isn't needed. The shop's eBay username is read from `private/settings.yaml` — `marketplaces.ebay.shop`
   — so that a consent given while signed in to another eBay account is refused; an `EBAY_SELLER=…` line here would
   win over it.)

## Step 3 — the redirect name (RuName)

1. Application Keys → next to the Client ID click **User Tokens**.
2. Open **Get a Token from eBay via Your Application**.
3. Click **"You have no Redirect URLs. Click here to add one."**, confirm your contact details, then
   **Continue to create RuName**.
4. Fill the form:
   - **Display Title**: `Thrift agent` (it is what the consent page shows you).
   - **Privacy Policy URL**: the one `ebay show` printed (`…/ebay/privacy`).
   - **Auth Accepted URL**: the one `ebay show` printed (`…/ebay/oauth/callback`).
   - **Auth Declined URL**: the one `ebay show` printed (`…/ebay/oauth/declined`).
   - If the form has an **OAuth Enabled** box, tick it.
   - **Save**.
5. Copy the **RuName** value the page shows (it looks like `Your_Name-YourName-thrift-abcdefgh`) into
   `private\.env` as `EBAY_RUNAME=…` (step 2.4).
6. Tell Claude Code: **"the eBay keys are in private/.env"**. It runs
   `provision.py ebay` (the keys go into Azure's settings — only when all three are there; it prints the settings'
   names, never a value) and `provision.py ebay status` (which names are set, and what the API says of eBay).

Do this once for the Sandbox keyset too if Claude Code asks for the one-day test there (the Sandbox has its own
RuName; the same three addresses).

## Step 4 — once, one minute: Agree

Within 15 minutes of step 3.6 the API sends ONE message to your **private Telegram chat** (never the group):
"🔗 eBay consent link — 1 minute, once: …". The link is the agent's own API; its key opens nothing but this one page.
(`provision.py ebay show` in your own terminal prints the same link.) On the PC, in a browser where you are **signed
in to eBay as the shop's account**:
1. Open the link. It sends you on to eBay's page **Grant Application Access**.
2. Click **Agree and Continue**.
3. The page says "eBay is connected. You can close this page." and the private chat gets
   "✅ eBay connected — the agent can list on eBay and read its orders. Valid until …".
   Signed in as another eBay account by mistake → the page and the chat say so and nothing is kept: sign in as the
   shop's account and open the link again.

That permission lasts about 18 months. A month before it ends — or at once if eBay ever cancels it (it does when the
account's password or username changes) — the private chat gets ONE message with the same kind of link:
"🔐 eBay wants a person … click Agree and Continue". Until you click, nothing new goes up on eBay **and a sale on
another site can't end its eBay listing by itself** — so that one is worth a minute the same day.

---

## What happens after that (nothing for you to do)

- **Checks on eBay itself that create nothing** (done Nov 11, 2026): eBay's own "validate only" mode on test items
  with eBay's real categories, and your listings read (never changed) so the agent's match them: calculated USPS
  Ground Advantage, 2 business days handling, Best Offer. No sandbox day was needed.
- **One real listing**: a low-priced, non-designer item. Its link comes to the private chat. If it reads back right,
  eBay goes on for new shares and the backfill starts: every item posted since Oct 13 that is still for sale, 25 a day.
- **Prices**: you tap one price in Telegram as always. The eBay price is worked out from it so that a sale on eBay
  leaves you the same profit as the same sale on Poshmark (eBay's fee is smaller, so its price is often a dollar or
  two lower). The ✅ card shows each site's price when eBay's differs. "price 35" as a reply changes all four sites.
- **Shipping and returns**: the buyer pays the shipping; returns 30 days, the buyer pays return shipping; Best Offer on.
- **Your ten hand-made eBay listings are never touched.** The agent acts only on listings it made itself (they carry
  its own item id as eBay's "custom label / SKU").
- **A sale on eBay** shows as "💰 Sold on eBay: …" like every sale; the item comes down on the other sites by itself.
  **A sale elsewhere** ends the eBay listing at once — from Azure, also in the quiet hours and with the Mac asleep.

## If something says no

- **The Notifications page won't save the endpoint** — check the URL and the token are exactly the two `ebay show`
  printed (no space at the end). Then tell Claude Code what the page said.
- **"Your Keyset is currently disabled"** stays after step 2.3 — reload the Application Keys page; it clears once the
  endpoint is saved.
- **The consent page says the redirect is invalid** — the RuName in `private\.env` isn't the Production one, or its
  Auth Accepted URL isn't the one above.
- **The scope `sell.listing` isn't offered for the keyset** (Application Keys → **OAuth Scopes**) — tell Claude Code:
  the agent then uses eBay's other API (the Inventory API), which needs one more yes from you (business policies).
