# Human steps in one minute (WO56)

Since WO54 only four things wait for a person: her **price**, a **size** no photo shows (both Telegram buttons), and —
on the sites — a **password or sign-in code** and a **real CAPTCHA**. The agent never solves a CAPTCHA, never uses a
solving service and never types a password. When a site wants a person, you do it in one minute from your PC or
phone, on the Mac's screen, and the agent carries on.

## What counts as a human step
Exactly these:
- a sign-in that needs a password;
- a one-time code (SMS, email, app);
- a real CAPTCHA;
- a "verify you are human" page that **doesn't clear by itself** within 2 minutes and on one retry 15 minutes later
  (Cloudflare's "Just a moment…" usually clears by itself — then you hear nothing at all);
- a site's own "confirm it's you" (identity or phone check).

Everything else the agent decides by itself.

## When one comes
1. The private chat gets ONE message with a screenshot of the page, for example:
   > 🔐 Vinted wants a person — a CAPTCHA (screenshot). 1 minute: connect to the Mac's screen (Screen Sharing,
   > 100.109.116.57), reply **show**, solve it in the window that comes up, and that's it. Depop and Poshmark keep
   > posting meanwhile.

   For a sign-in or a code: "Vinted wants you to sign in — reply **show** and type the password or code yourself on
   the Mac's screen."
2. Connect to the Mac's screen (VNC viewer → `100.109.116.57`, see the setup below).
3. Reply **show** in the private chat: that one window comes to the front on the Mac, titled "Vinted — human step".
4. Solve it / type the password or code yourself. That's all — the agent checks the page every 10 seconds and says
   "✅ Vinted back — posting resumed" when it clears. ("done" is accepted, never needed.)
5. **later** snoozes the reminder for 12 hours. Otherwise a reminder comes every 12 hours while it waits, and once at
   2:05 PM after the quiet hours — never more.

The page is held meanwhile: the agent's Chrome doesn't quit or tidy it away, and every other site keeps posting.
**The quiet hours win:** from 8:30 AM to 2:00 PM the agent's Chrome is closed and the step waits until 2:00 PM (unless
she says "post now").

## The one-time setup

### 1. On her Mac (yours to do — Claude Code never changes system settings)
Remote Management is already on: System Settings says "Screen Sharing … is currently being controlled by the Remote
Management service". So use Remote Management, not Screen Sharing:
1. System Settings → General → Sharing → ⓘ next to **Remote Management**.
2. Allow her user (`tatiana_sorokina`) with **Observe** and **Control**.
3. **Computer Settings…** → **VNC viewers may control screen with password** → a strong password (keep it in your
   password manager).

What the Mac shows now (read-only check, 2026-11-09 8:50 PM):
- Remote Management's agent (ARDAgent) runs; port 5900 listens; the Tailscale address `100.109.116.57:5900` answers
  (from the Mac and from the PC).
- **No user has Remote Management privileges yet** (step 2 above is needed).
- The VNC password setting can't be read without admin rights — set it in step 3.
- Nothing of the agent's turned it on: no file or commit of ours touches it. Its settings file was last changed on
  Oct 4, 1:04 AM (`allowInsecureDH = 1`, the key System Settings writes for VNC viewers) — a hand step that night.

### 2. On the PC
1. Install a VNC viewer (e.g. **RealVNC Viewer**).
2. With Tailscale on, connect to **100.109.116.57**.
3. Log in with the VNC password from step 1.3. Test once: you should see her screen. Close it again — don't click
   anything there unless a step waits.

### 3. Optional, on your iPhone
The **Tailscale** app (the same account) and a VNC viewer app (e.g. RealVNC Viewer) with the address 100.109.116.57.

## Good to know
- VNC shows her whole screen: connect only when a step waits, and tell her it's you.
- The agent logs every human step (`thrift doctor` shows one waiting; the event `human_step` keeps site, page, when,
  how long until it cleared, whether a person was needed). A site that asks more than once a day gets looked into.
- The sites' trust lives in the agent Chrome's profile (cookies are kept across its restarts; only the last session's
  open pages are set aside).
