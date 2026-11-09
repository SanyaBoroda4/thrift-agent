#!/bin/bash
# The Thrift Chrome (WO32; WO54 §4) — what launchd's com.thrift.chrome-cross runs. It is NOT the owner's browser: the
# poster starts it only when its extension has work (a listing, a take-down, a check, an order sweep, an edit, a photo
# read: `services.sh start chrome`, i.e. `launchctl kickstart` — RunAtLoad false, no KeepAlive, nothing at login) and
# quits it after 10 idle minutes (SIGTERM to this job's own pid, matched by its --user-data-dir: Chrome shuts down
# normally and nothing starts it again until the next job). Her own Chrome and Safari are never touched.
#
# Live 2026-11-09: started at 1 AM with --restore-last-session and https://www.vinted.com/, it brought back ~50 old pages
# (hers on eBay, Vinted pages with ad frames) — 9.6 GB on her 8 GB laptop, two pages burning a core each until 9:35 AM.
# So:
#   - no window and no page at start (--no-startup-window): the extension's service worker starts with the profile
#     (its runtime.onStartup listener and its alarm) and connects to the bridge within seconds; it opens one unfocused
#     window per site for its jobs, never brought to the front;
#   - never a restored session, whatever the profile's own "On startup" setting says: the last run's session files are
#     set aside before Chrome opens the profile (one generation kept, Default/Sessions.last);
#   - the two "backgrounding" switches stay (WO28: a window behind others, or the lock screen, keeps full speed);
#   - never --remote-debugging-*, --enable-automation, --load-extension (the sites see a person's Chrome; a test checks).
set -u
PROFILE="$HOME/thrift/chrome-cross"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Only when no Chrome of this profile runs (a second launch hands its arguments to the running one and exits).
if ! /usr/bin/pgrep -f -- "--user-data-dir=$PROFILE( |\$)" >/dev/null 2>&1 && [ -d "$PROFILE/Default/Sessions" ]; then
  rm -rf "$PROFILE/Default/Sessions.last"
  mv "$PROFILE/Default/Sessions" "$PROFILE/Default/Sessions.last" 2>/dev/null || true
fi

exec "$CHROME" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check --no-startup-window \
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disk-cache-size=268435456
