#!/bin/bash
# One `thrift` command as a one-off launchd job in the owner's GUI session (WO33). What opens the poster's Chrome
# profile (Poshmark's keychain-bound cookies) can't run over plain SSH — it would look logged out. The poster service
# must be stopped first (it holds that profile). Waits until the command has run (≤ 10 min), prints its output and
# exit code, and removes the job.
#     bash deploy/gui_once.sh delist --item <item> --marketplace poshmark
set -euo pipefail

LABEL="com.thriftagent.once"
DOMAIN="gui/$(id -u)"
LOG="$HOME/thrift/logs/once.log"
PLIST="$HOME/thrift/var/$LABEL.plist"

[ $# -gt 0 ] || { echo "usage: bash deploy/gui_once.sh <thrift arguments…>" >&2; exit 2; }
if launchctl print "$DOMAIN/com.thriftagent.poster" 2>/dev/null | grep -q "state = running"; then
  echo "the poster service is running (it holds the Chrome profile): bash deploy/services.sh stop poster" >&2
  exit 1
fi

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
args=""
for a in "$@"; do
  args+="    <string>$(xml "$a")</string>"$'\n'
done

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/thrift-agent/.venv/bin/thrift</string>
$args  </array>
  <key>WorkingDirectory</key><string>$HOME/thrift-agent</string>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
: > "$LOG"
launchctl bootstrap "$DOMAIN" "$PLIST"

# Done only when launchd shows a real exit code: right after the bootstrap the job is "not running" with "last exit
# code = (never exited)" for a moment (the first version stopped there and unloaded a job that hadn't run).
code=""
for _ in $(seq 1 600); do
  info=$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null || true)
  state=$(printf '%s\n' "$info" | awk -F'= ' '/^[[:space:]]*state = /{print $2; exit}')
  last=$(printf '%s\n' "$info" | awk -F'= ' '/^[[:space:]]*last exit code = /{print $2; exit}')
  last="${last%%:*}"
  if [ "$state" != "running" ] && [[ "$last" =~ ^-?[0-9]+$ ]]; then
    code="$last"
    break
  fi
  sleep 1
done

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
cat "$LOG"
if [ -z "$code" ]; then
  echo "== still running after 10 min: stopped (see $LOG)" >&2
  exit 1
fi
echo "== exit code $code"
[ "$code" = "0" ]
