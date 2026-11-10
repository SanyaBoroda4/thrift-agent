# The upload Shortcut — "New item" (WO45 §2, WO56 §6)

What it does (since WO56 §6, 2026-11-09): sends each photo straight to thrift-api, then the end of the share — and
saves the photos to iCloud Drive `Posh/inbox/<share id>/` **only when the phone has no internet** at the start. Until
WO56 every share went up twice (the iCloud copy first, then the upload) and the Mac downloaded both: the 127-photo
share of Nov 8 was about 530 MB each way, and its iCloud copy reached the Mac 73 minutes after the upload. Now there is
one copy of each share. The Mac takes the share the moment its upload is whole; the upload's photos are deleted from
Azure as soon as the Mac has them; the Mac keeps one copy of each item's photos while it is for sale.

Before you start, you need two things:
- `<API_URL>` — the API's address. On the PC it is the `API_URL` line of `var/azure/keys.txt` (looks like
  `https://thrift-api-xxxxxx.azurewebsites.net`).
- the upload key — the ONE message in your private chat with the bot: "🔑 The upload key for the Shortcut: …".
  It opens only the two upload routes. Never put it anywhere else.

## The Shortcut (Shortcuts app, iOS 26)

Make a new Shortcut named **New item** (or edit the old one: its first steps stay, the iCloud save moves into step 7's
branch). In its details (ⓘ): **Show in Share Sheet** on, Share Sheet Types: **Images** only. The first line of the
Shortcut is then "Receive **Images** from Share Sheet — If there's no input: **Stop and Respond**".

1. **Set Low Power Mode** — turn **Off**. (Low Power Mode holds background uploads — the 4½-hour share of Oct 17.)
2. **Count** — Items — **Shortcut Input** → rename the result **Count**.
3. **Format Date** — Current Date — Date Format **Custom**, Format String `yyyy-MM-dd_HHmmss` → rename **Stamp**.
4. **Random Number** — Minimum `1000`, Maximum `9999` → rename **Rand**.
5. **Text** — `Stamp_Rand` (the variable Stamp, an underscore, the variable Rand) → rename **ShareID**.
   It reads like `2026-10-18_153045_4821`.
6. **Get Current IP Address** — External → rename **IP**. (On cellular this still has a value and the uploads work;
   it is empty only when the phone is fully offline.)
7. **If** — **IP** — **does not have any value** — the no-internet branch, the ONLY place the iCloud copy is made:
   - **Repeat with Each** — **Shortcut Input**:
     - **Save File** — **Repeat Item** — Service **iCloud Drive**, Ask Where to Save **off**, Destination Path
       `Posh/inbox/ShareID/` (the variable ShareID inside the path), Overwrite If File Exists **off**.
     - End Repeat.
   - **Text** — `done Count` (the word done, a space, the variable Count).
   - **Save File** — the Text — iCloud Drive, Ask Where to Save off, Destination Path `Posh/inbox/ShareID/_done.txt`.
   - **Show Notification** — `No internet — saved to iCloud. It goes through by itself once the phone is online.`
   - **Stop This Shortcut**.
   - End If.
8. **Repeat with Each** — **Shortcut Input** — the upload:
   - **Get Details of Files** — **Name** — of **Repeat Item** → rename **Name**.
   - **URL** — `<API_URL>/share/ShareID/photo?n=Repeat Index&of=Count&name=Name`
     (ShareID, Repeat Index, Count and Name are variables; Repeat Index is offered inside the Repeat). The `of=Count`
     matters: it tells the Mac how many photos the share has from its first photo on.
   - **Get Contents of URL** — the URL — Show More → Method **POST**; Headers: one header, Key `x-functions-key`,
     Text: the upload key; Request Body **File** → **Repeat Item**.
     (If "File" isn't offered: Request Body **Form**, one field, Key `photo`, Type **File**, Value **Repeat Item**.
     Both work.)
   - End Repeat.
9. **URL** — `<API_URL>/share/ShareID/end?count=Count`.
10. **Get Contents of URL** — the URL — Method **POST**; the same `x-functions-key` header; Request Body **JSON** with
    no fields → rename **End**.
11. **Get Dictionary Value** — **Value** for **received** in **End** → rename **Received**.
12. **If** — **Received** — **is** — **Count**:
    - **Show Notification** — `✓ Sent Count photos`
    - Otherwise: **Show Notification** — `Only Received of Count photos went through — please share them again.`
    - End If.

Offline or an error mid-upload: Shortcuts has no per-action "continue on error" — a connection that drops during a
Get Contents of URL stops the Shortcut with its error (the photos sent until then are in Azure; the end never comes).
The Mac handles that share as "never ended" (below): it is held and, 15 minutes later, the group is asked to share the
photos again. A photo the API refuses (not an image, over 30 MB, past the 200th) does not stop the Shortcut: the end
answers with fewer photos than Count and step 12 says "Only X of N". Such a photo is refused every time — share it as a
smaller copy, or share fewer at once.

How many photos: up to **200** a share (WO51 §6). A photo past 200 is refused by the API.

The key can also go in the address instead of a header (`…&code=<the key>` on both URLs) if a header is awkward to
fill in; the header is tidier.

## What the Mac does with each share

| The share | The Mac |
|---|---|
| **Uploaded whole** (every photo its Count names reached Azure; the end call too, or not) | Taken at once from Azure and made into its card — it never waits for, or mentions, an iCloud copy. No 📭. |
| **Ended short** ("Only 7 of 12 photos went through") with no iCloud copy | Never posted with photos missing. The 7 photos are fetched into a held folder on the Mac (Azure's copy is deleted). If she hasn't shared them again 15 minutes after the last photo arrived, the group hears ONE line: "📸 The photos shared at 7:42 PM didn't finish uploading — please share them again." The share ledger says `held`. |
| **Never ended** (the Shortcut stopped mid-upload: no end call) with no iCloud copy | The same: held from 10 minutes after its first photo with 5 quiet minutes (it is never taken while photos still arrive), the one line 15 minutes after its last photo, never posted with 7. |
| **Shared again** after a held share | The new share replaces the held one: as soon as the new share's photos reach Azure (the same photos, by their SHA-256) — or once the Mac knows them from its card — the held share is dropped (ledger `replaced`) and nothing is said about it. The new share is an ordinary share: one item, all its photos, nothing skipped as a duplicate (a held share's photos are never in the photo memory). If the new share also stops short, it is held in its turn and the older one goes. |
| **No internet** (step 7: the iCloud copy only) | As before WO56: the iCloud copy is taken when its end mark is there and its photos are whole; if only some came after 30 minutes, the card is made from those ("📷 7 of 12 photos — the rest join when they arrive") and the rest join the item as they arrive; if none came in 30 minutes, ONE group line "📭 Photos from your 7:31 PM share are still on the iPhone — unlock it on Wi-Fi and they'll come through. No need to share again." (the 30-minute rule stays for iCloud copies only: their missing photos do come, late — an upload's never do). |
| **Both copies** (the old Shortcut, iCloud first) | As before WO56: the whole copy wins (a whole upload at once); a held upload whose iCloud copy turns up is that share's other copy — the iCloud copy is taken by its own rules and the held copy goes. |
| **A held share never shared again** | Its folder is deleted after 7 days; the ledger keeps `held`. |

`thrift shares` lists the day's shares with their outcome (`held: 7 of 12 photos came …`, `replaced: … shared again at
7:50 PM`); `thrift doctor`'s "Waiting:" line counts the held ones; the dashboard's Shares panel shows "didn't finish
uploading — to share again".

## The test copy

Duplicate the Shortcut, name it **New item (TEST)**, and add `&test=1` to BOTH addresses: the photo one
(`…&name=Name&test=1`) and the end one (`…/end?count=Count&test=1`). Its shares are test shares on the Mac: the cards
go to your private chat marked 🧪 and nothing is posted; a held test share's line goes to the private chat too.

**Its iCloud copy (the no-internet branch):** in the test copy, change the two Save File paths from `Posh/inbox/…` to
`Posh/test/…` — a folder the Mac doesn't watch. The iCloud copy carries no test mark of its own: if it were saved into
`Posh/inbox` it could make a real card. The separate folder is the sure way.

## Checking it

After the first share: `thrift status` / `thrift shares --day` on the Mac, or the private chat — the share's card as
usual. On the PC, `python deploy/azure/provision.py uploads` makes the container, the 30-day rule and the key again when
anything is missing (it never prints the key; it tells it to the private chat only once).
