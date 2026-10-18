# The upload Shortcut — "New item" (WO45 §2)

What it does: saves the photos to iCloud Drive `Posh/inbox/<share id>/` exactly as the current Shortcut does (the
safety copy, saved first), then sends each photo straight to thrift-api, then the end of the share. The Mac takes
whichever arrives first; the share id names both, so nothing is listed twice.

Before you start, you need two things:
- `<API_URL>` — the API's address. On the PC it is the `API_URL` line of `var/azure/keys.txt` (looks like
  `https://thrift-api-xxxxxx.azurewebsites.net`).
- the upload key — the ONE message in your private chat with the bot: "🔑 The upload key for the Shortcut: …".
  It opens only the two upload routes. Never put it anywhere else.

## The Shortcut (Shortcuts app, iOS 26)

Make a new Shortcut named **New item**. In its details (ⓘ): **Show in Share Sheet** on, Share Sheet Types: **Images**
only. The first line of the Shortcut is then "Receive **Images** from Share Sheet — If there's no input: **Stop and
Respond**".

1. **Set Low Power Mode** — turn **Off**. (Low Power Mode holds iCloud and background uploads — the 4½-hour share of
   Oct 17.)
2. **Count** — Items — **Shortcut Input** → rename the result **Count**.
3. **Format Date** — Current Date — Date Format **Custom**, Format String `yyyy-MM-dd_HHmmss` → rename **Stamp**.
4. **Random Number** — Minimum `1000`, Maximum `9999` → rename **Rand**.
5. **Text** — `Stamp_Rand` (the variable Stamp, an underscore, the variable Rand) → rename **ShareID**.
   It reads like `2026-10-18_153045_4821`.
6. **Repeat with Each** — **Shortcut Input**:
   - **Save File** — **Repeat Item** — Service **iCloud Drive**, Ask Where to Save **off**, Destination Path
     `Posh/inbox/ShareID/` (the variable ShareID inside the path), Overwrite If File Exists **off**.
   - End Repeat.
7. **Text** — `done Count` (the word done, a space, the variable Count).
8. **Save File** — the Text — iCloud Drive, Ask Where to Save off, Destination Path `Posh/inbox/ShareID/_done.txt`.
   (Steps 6–8 are the current Shortcut's save steps. They run BEFORE any upload: iCloud stays the safety copy.)
9. **Get Network Details** — **Current Network** — **Network Name** → rename **Network**. (Cellular only: this is
   empty and the uploads still work; the check below catches the phone being fully offline.)
10. **Get Current IP Address** — External → rename **IP**.
11. **If** — **IP** — **does not have any value**:
    - **Show Notification** — `Saved — it will go through when the phone is on Wi-Fi.`
    - **Stop This Shortcut**.
    - End If.
12. **Repeat with Each** — **Shortcut Input**:
    - **Get Details of Files** — **Name** — of **Repeat Item** → rename **Name**.
    - **URL** — `<API_URL>/share/ShareID/photo?n=Repeat Index&of=Count&name=Name`
      (ShareID, Repeat Index, Count and Name are variables; Repeat Index is offered inside the Repeat).
    - **Get Contents of URL** — the URL — Show More → Method **POST**; Headers: one header, Key `x-functions-key`,
      Text: the upload key; Request Body **File** → **Repeat Item**.
      (If "File" isn't offered: Request Body **Form**, one field, Key `photo`, Type **File**, Value **Repeat Item**.
      Both work.)
    - End Repeat.
13. **URL** — `<API_URL>/share/ShareID/end?count=Count`.
14. **Get Contents of URL** — the URL — Method **POST**; the same `x-functions-key` header; Request Body **JSON** with
    no fields → rename **End**.
15. **Get Dictionary Value** — **Value** for **received** in **End** → rename **Received**.
16. **If** — **Received** — **is** — **Count**:
    - **Show Notification** — `✓ Sent Count photos`
    - Otherwise: **Show Notification** — `Sent Received of Count — the rest come through iCloud.`
    - End If.

Offline or an error mid-upload: Shortcuts has no per-action "continue on error" — a failed Get Contents of URL stops
the Shortcut with its error. That is why the iCloud save (steps 6–8) comes first and the network check (steps 9–11)
comes before any upload: the photos are saved either way, and the Mac takes the iCloud copy when the API copy never
came. (If you'd rather see no error at all, put steps 12–16 in a second Shortcut and call it with **Run Shortcut** —
the error then shows in the helper's name, and the first Shortcut has already saved everything.)

The key can also go in the address instead of a header (`…&code=<the key>` on both URLs) if a header is awkward to
fill in; the header is tidier.

## The test copy

Duplicate the Shortcut, name it **New item (TEST)**, and add `&test=1` to BOTH addresses: the photo one
(`…&name=Name&test=1`) and the end one (`…/end?count=Count&test=1`). Its shares are test shares on the Mac: the cards
go to your private chat marked 🧪 and nothing is posted.

**Its iCloud copy:** in the test copy, change the two Save File paths from `Posh/inbox/…` to `Posh/test/…` — a folder
the Mac doesn't watch. The iCloud copy carries no test mark of its own: if it were saved into `Posh/inbox` and reached
the Mac before the API's copy was asked for, it could make a real card. (The Mac also marks an inbox copy as a test
the moment the API says so, and tells you if a copy was taken before that — but the separate folder is the sure way.)

## Checking it

After the first share: `thrift status` on the Mac, or the private chat — the share's card as usual. On the PC,
`python deploy/azure/provision.py uploads` makes the container, the 30-day rule and the key again when anything is
missing (it never prints the key; it tells it to the private chat only once).
