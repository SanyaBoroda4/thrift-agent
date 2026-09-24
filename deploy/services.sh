#!/usr/bin/env bash
# Start / stop / restart / status for the two launchd agents on the Mac (worker + poster).
#     bash deploy/services.sh start|stop|restart|status
# mac_setup.sh writes the plists to ~/Library/LaunchAgents but does not load them; `start` does
# (launchctl kickstart on a label that was never bootstrapped fails, so deploy.ps1 calls `restart` here).
# `thrift login` needs the poster's Chrome profile to itself: `stop` first, `start` after.
set -euo pipefail

LABELS="com.thriftagent.worker com.thriftagent.poster"
DOMAIN="gui/$(id -u)"

usage() { echo "usage: bash deploy/services.sh start|stop|restart|status" >&2; exit 2; }
loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

[ $# -eq 1 ] || usage
cmd="$1"
case "$cmd" in start|stop|restart|status) ;; *) usage ;; esac

for label in $LABELS; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  case "$cmd" in
    start)
      if loaded "$label"; then
        echo "$label: already loaded"
      else
        [ -f "$plist" ] || { echo "$label: $plist missing - run bash deploy/mac_setup.sh first" >&2; exit 1; }
        launchctl bootstrap "$DOMAIN" "$plist"
        echo "$label: started"
      fi
      ;;
    stop)
      if loaded "$label"; then
        launchctl bootout "$DOMAIN/$label"
        echo "$label: stopped"
      else
        echo "$label: not loaded"
      fi
      ;;
    restart)
      loaded "$label" || { echo "$label: not loaded - run 'bash deploy/services.sh start' first" >&2; exit 1; }
      launchctl kickstart -k "$DOMAIN/$label"
      echo "$label: restarted"
      ;;
    status)
      echo "== $label"
      if out=$(launchctl print "$DOMAIN/$label" 2>/dev/null); then
        printf '%s\n' "$out" | head -n 12
      else
        echo "not loaded"
      fi
      ;;
  esac
done
