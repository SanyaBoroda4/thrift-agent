# Listing with the Mac — the daily routine

The Mac does the listing for you. You take the photos, tap the prices in Telegram, and it puts the items on Poshmark
by itself. It only works while the Mac is open, so once a day:

1. **Open the Mac.** The charger is optional. You don't have to open any app.
2. **Share your photos** from the iPhone: Share → **New item**. You can do this any time, before or while the Mac is
   open.
3. **Answer in Telegram** on your phone: tap a price on each card (or type a number), and answer the occasional
   question. The card then shows your answer, for example "✓ $35 — queued", and the next card comes.
4. **Wait for "✓ All done — safe to close the Mac."** It comes under the last "Posted ✓" of the day.
5. **Close the lid.** That's it.

Nothing is lost if you close the Mac too early. Whatever was left simply continues the next time you open it.

## The messages

The Telegram group stays quiet: it shows only the cards, "Posted ✓", and a message when something needs you. All the
technical messages (errors, progress, the Mac waking up) go to Alex privately.

**Cards and questions**
- **A price card** (photo, title, size, condition, buttons with prices) — tap the price you want, or type a number.
  This is the only thing it needs from you for most items.
- **"Brand new or worn?" / "Girls or Boys?" / "Which category?"** — tap a button. These come only when the photos
  didn't make it clear.
- **"Brand: … not sure"** on a card — reply to the card with the brand, for example "J. Crew" (or tap "No brand").
- **After you answer**, the card's buttons turn into your answer ("✓ $35 — queued", "✓ Girls", "✓ brand: J. Crew").
  No other reply comes; that's how you know it was taken.

**After publishing**
- **"Posted ✓ <title> — $35 · Poshmark <link> · Depop <link> · Vinted <link>"** — the item is live on each site the
  line names (one line per item, once its sites are done). A site that couldn't take it is simply not in the line.
- **"… — check: brand set to 'J. Crew' (from 'J.Crew')"** at the end of the line — it's live, but the site's list
  didn't have exactly what the listing said, so the Mac picked the closest thing (or left the brand empty). Have a
  quick look on that site and fix it there if it isn't right.
- **"✓ All done — safe to close the Mac."** under a "Posted ✓" — that was the last one: everything is listed and no
  card is waiting. Close the lid.

**Things to do something about** (each comes once)
- **"Depop needs you to log in on the Mac."** / **"Vinted needs you to log in on the Mac."** — on the Mac, open the
  **Thrift Chrome** (the second Chrome icon in the Dock, the one Depop and Vinted are listed from — not your own
  Chrome), go to that site and log in the normal way. Poshmark goes on meanwhile; that site is tried again the next
  time the Mac is open.
- **"… shows a CAPTCHA — solve it in its Chrome window on the Mac."** / **"… asks for a check — open it on the
  Mac."** — the same Thrift Chrome window: the site's tab is left open there; do what it asks. The Mac never does
  this part.
- **"… turned the Mac away for now"** — nothing to do; that site is tried again the next time the Mac is open. If it
  keeps coming, tell Alex.
- **"🔋 Mac battery low — plug in or I'll pause; nothing will be lost"** — plug the charger in. The Mac finishes
  the listing it is on and waits until it is charging (or above 20%).
- **"⚠️ <title>: the Mac went to sleep while publishing and I can't see it in the closet."** (or **"… I pressed List
  on Poshmark but can't see it in the closet."**) — look in your Poshmark closet:
  - If the item **is** there, open it, copy its link, and **reply** to that message with `posted ` and the link
    (for example `posted https://poshmark.com/listing/...`).
  - If it **is not** there, **reply** `retry`. It will be listed again.
  - To reply, swipe left on the message, or press and hold it and choose Reply.
- **"⚠️ Can't read the iCloud inbox — …"** (only after 10 minutes of trying) — on the Mac: System Settings → Privacy &
  Security → Files & Folders → python3.14 → turn iCloud Drive on. Then it continues by itself.
- **"⏸ Not published automatically: <title> — its text needs a look first."** — it was not put up. Tell Alex.
- **"⏭ <title> was skipped: Poshmark's form doesn't take one of its details."** — nothing was saved. Tell Alex.

## The Thrift Chrome

The second Chrome on the Mac (its own icon in the Dock) is where Depop and Vinted are listed from. Leave it open: it
can sit behind other windows, but don't minimize it. If you quit it by accident, the Mac opens it again within a few
minutes. Don't use it for your own browsing — use your normal Chrome for that.

## The emergency brake

If something looks wrong on Poshmark and you want the Mac to stop listing **right now**:
- **From the iPhone:** in the Files app, go to iCloud Drive → **Posh** and make a new folder named **PAUSE**. The Mac
  lists nothing while it is there. Delete it to continue.
- **On the Mac:** open Terminal and type `bash ~/thrift-agent/deploy/services.sh stop poster`. It finishes the
  listing it is on, then stops. `bash ~/thrift-agent/deploy/services.sh start poster` starts it again.

Neither one loses anything: the items and your answers stay, and listing picks up where it stopped.
