# Thrift mail: the marketplace emails, passed to the API

A small Google Apps Script that lives in the seller's Gmail account. Every 5 minutes it looks for new emails from
Poshmark, Depop and Vinted (sales, offers, shipping) and hands each one to the thrift API, which turns them into
sales, ship-by dates and reminders. It runs on Google's side: the Mac and the PC can be off.

## What it can and cannot do

- **Read only.** It reads mail with Gmail's read-only permission. It cannot send, delete, move, archive, label or mark
  anything as read, and it cannot change a setting: Google doesn't give it those permissions.
- **Only the three marketplaces.** It searches for mail from poshmark.com, depop.com and vinted.com and checks the
  sender's address again before passing anything on. It passes the sender, subject, date and the email's text: no
  attachments, and no other email.
- **One destination.** It sends only to the API address saved in its settings, with the key saved there.
- Google asks you to allow three things: read your email (read-only), connect to an external service (the API), and
  run when you are not present (the 5-minute timer).

## Set it up (once, about 10 minutes, on a computer)

1. Go to script.google.com **signed in as the seller's Gmail** → **New project**. Rename it "Thrift mail".
2. In `Code.gs`, delete everything and paste the whole of `gmail/Code.gs` from this folder. Save (Ctrl+S / Cmd+S).
3. **Project Settings** (the gear) → tick **Show "appsscript.json" manifest file in editor**. Back in the **Editor**,
   open `appsscript.json`, replace everything with `gmail/appsscript.json`, save.
4. **Services** (the + next to it in the Editor) → **Gmail API** → **Add**, keeping the name `Gmail`. If Gmail is
   already listed under Services, step 3 did it: nothing to do.
5. **Project Settings** → **Script Properties** → **Add script property**, twice:
   - `API_URL`: the API's address
   - `API_KEY`: the API's key

   The developer gives you both values from their terminal. Paste them straight in and save; never send them in a
   chat or an email.
6. In the Editor, pick **install** in the function list at the top → **Run**. Google asks for permission:
   **Review permissions** → the seller's account → "Google hasn't verified this app" → **Advanced** →
   **Go to Thrift mail (unsafe)** → **Allow**. The warning is normal: it's your own script, and Google only reviews
   apps that are published for other people. The log ends with "install: poll runs every 5 minutes".
7. Pick **dumpSamples** → **Run**, once. It sends the last 90 days of marketplace emails to the API as examples for
   the developer; the log says how many.

Done. **Triggers** (the clock) shows one `poll` trigger, every 5 minutes, and **Executions** lists each run with a
line like "poll: sent 1, errors 0, left 0".

Leave the `SEEN` property alone (`SEEN_2` and `SEEN_3` appear later): it is the script's memory of what it has already
sent. An email the API didn't take is tried again on the next run; after two days it is no longer looked for.

## Stop it

Pick **uninstall** → **Run**: the 5-minute trigger is removed and nothing runs any more. **install** starts it again.
To also take back the permission: your Google Account → Security → Third-party apps & services → Thrift mail →
remove access.
