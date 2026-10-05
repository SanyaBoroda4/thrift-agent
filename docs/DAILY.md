# Listing with the Mac — the daily routine

The Mac does the listing for you. You take the photos, tap the prices in Telegram, and it puts the items on Poshmark
by itself. It only works while the Mac is open, so once a day:

1. **Open the Mac.** The charger is optional. You don't have to open any app.
2. **Share your photos** from the iPhone: Share → **New item**. You can do this any time, before or while the Mac is
   open.
3. **Answer in Telegram** on your phone: tap a price on each card (or type a number), and answer the occasional
   question.
4. **Wait for "✓ All done — safe to close the Mac"** (or "✓ Safe to close …").
5. **Close the lid.** That's it.

Nothing is lost if you close the Mac too early. Whatever was left simply continues the next time you open it.

## The messages

**When you open the Mac**
- **"Back online — 2 new shares, 3 items waiting"** — the Mac has woken up and is starting on what is waiting. If
  there is nothing to do, it says nothing.

**The status message.** One message keeps changing while the Mac works, so look at that one.
- **"⏳ Working — 4 items left, about 12 min. Please don't close the Mac yet."** — it is preparing listings or putting
  them on Poshmark. Keep the Mac open.
- **"⏳ 3 listings still to publish, next in ~4 min."** — it puts them up one at a time, with a few minutes in
  between, like a person would. Keep the Mac open.
- **"✓ All done — safe to close the Mac."** — everything is listed.
- **"✓ Safe to close — 2 cards are waiting for your answer in Telegram"** — only your answers are missing. You can
  close the Mac and answer later from your phone. Answers given within 24 hours are kept and applied the next time
  the Mac is open. After that the card simply comes again.
- **"✓ Safe to close — 3 listings will go up after 08:00 next time the Mac is open"** — it only lists during the day.

**Questions and cards**
- **A price card** (photo, title, size, condition, buttons with prices) — tap the price you want, or type a number.
  This is the only thing it needs from you for most items.
- **"Brand new or worn?" / "Girls or Boys?" / "Which category?"** — tap a button. These come only when the photos
  didn't make it clear.
- **"Brand: … not sure"** on a card — reply to the card with the brand, for example "J. Crew" (or tap "No brand").

**After publishing**
- **"Posted ✓ <title>"** — the item is live on Poshmark, and the link follows.
- **"Posted ✓ <title> — check: brand set to 'J. Crew' (from 'J.Crew')"** — it's live, but Poshmark's list didn't
  have exactly what the listing said, so the Mac picked the closest thing. Have a quick look on Poshmark and fix it
  there if it isn't right.
- **"⏸ Not published automatically: <title> …"** — this listing's text needs a human look first. It was not put
  up. Tell Alex, or publish it yourself with the command shown in the message.
- **"⏭ skipped on poshmark: <title> …"** — Poshmark's form had no place for something it needs (for example a
  size far off its list). Nothing was saved. Tell Alex.

**Things to do something about**
- **"🔋 Mac battery low — plug in or I'll pause; nothing will be lost"** — plug the charger in. The Mac finishes
  the listing it is on and waits until it is charging (or above 20%).
- **"⚠️ <title>: the Mac went to sleep while publishing and I can't see it in the closet."** — the lid closed in the
  middle of a listing. Look in your Poshmark closet:
  - If the item **is** there, open it, copy its link, and **reply** to that message with `posted ` and the link
    (for example `posted https://poshmark.com/listing/...`).
  - If it **is not** there, **reply** `retry`. It will be listed again.
  - To reply, swipe left on the message, or press and hold it and choose Reply.
- **"Waiting for iCloud to finish downloading the photos…"** — the photos are still on their way from the iPhone
  through iCloud. Keep the Mac open and connected to Wi-Fi; it continues by itself.
- **"⚠️ Can't read the iCloud inbox …"** — on the Mac: System Settings → Privacy & Security → Files & Folders →
  python3.14 → turn iCloud Drive on. Then it continues by itself.
- **"⛔ Poster paused …"** — Poshmark needs a person: you were logged out, a puzzle (CAPTCHA) appeared, or several
  listings failed in a row. Nothing more is listed until it is fixed. Tell Alex.

## The emergency brake

If something looks wrong on Poshmark and you want the Mac to stop listing **right now**:
- **From the iPhone:** in the Files app, go to iCloud Drive → **Posh** and make a new folder named **PAUSE**. The Mac
  lists nothing while it is there. Delete it to continue.
- **On the Mac:** open Terminal and type `bash ~/thrift-agent/deploy/services.sh stop poster`. It finishes the
  listing it is on, then stops. `bash ~/thrift-agent/deploy/services.sh start poster` starts it again.

Neither one loses anything: the items and your answers stay, and listing picks up where it stopped.
