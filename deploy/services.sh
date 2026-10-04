#!/usr/bin/env bash
# The two launchd agents on the Mac: the worker (thrift run) and the poster (thrift poster).
#     bash deploy/services.sh start worker|poster
#     bash deploy/services.sh stop worker|poster|all
#     bash deploy/services.sh restart worker|poster     (starts it when it isn't loaded)
#     bash deploy/services.sh status
#     bash deploy/services.sh logs [worker|poster] [lines]
# mac_setup.sh writes the plists to ~/Library/LaunchAgents; `start` loads them. deploy.ps1 restarts the WORKER only:
# the poster service stays off until the owner turns publishing on (poster.dry_run false AND
# poster.autopublish_confirmed true — the two keys). `thrift login` needs the poster's Chrome profile to itself:
# `stop poster` first. One worker at a time: `thrift run` holds worker.lock next to the DB, and `start worker` here
# refuses while a `thrift run` runs outside launchd (a Terminal window).
set -euo pipefail

DOMAIN="gui/$(id -u)"
LOGS="$HOME/thrift/logs"

usage() {
  echo "usage: bash deploy/services.sh start|stop|restart worker|poster | stop all | status | logs [worker|poster] [lines]" >&2
  exit 2
}
label() { echo "com.thriftagent.$1"; }
loaded() { launchctl print "$DOMAIN/$(label "$1")" >/dev/null 2>&1; }
service_pid() { launchctl print "$DOMAIN/$(label "$1")" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}'; }

# `thrift run` processes that launchd didn't start (e.g. a Terminal window): a second worker would take the Telegram
# updates the service needs.
stray_workers() {
  local svc
  svc="$(service_pid worker || true)"
  { pgrep -f 'bin/thrift run' || true; } | while read -r pid; do
    [ "$pid" = "$svc" ] || ps -o pid=,command= -p "$pid" || true
  done
}

start() {
  local svc="$1" plist
  plist="$HOME/Library/LaunchAgents/$(label "$svc").plist"
  if loaded "$svc"; then
    echo "$svc: already running"
    return
  fi
  if [ "$svc" = worker ] && [ -n "$(stray_workers)" ]; then
    echo "worker: NOT started - a thrift run is already running outside launchd:" >&2
    stray_workers >&2
    echo "Stop it first (Control+C in its Terminal window), then run this again." >&2
    exit 1
  fi
  [ -f "$plist" ] || { echo "$svc: $plist missing - run bash deploy/mac_setup.sh first" >&2; exit 1; }
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "$svc: started"
}

stop() {
  if loaded "$1"; then
    launchctl bootout "$DOMAIN/$(label "$1")"
    echo "$1: stopped"
  else
    echo "$1: not running"
  fi
}

restart() {
  if loaded "$1"; then
    launchctl kickstart -k "$DOMAIN/$(label "$1")"
    echo "$1: restarted"
  else
    start "$1"
  fi
}

status() {
  local svc pid
  for svc in worker poster; do
    if ! loaded "$svc"; then
      echo "$svc: not loaded (off)"
    elif pid="$(service_pid "$svc")" && [ -n "$pid" ]; then
      echo "$svc: running (pid $pid)"
    else
      echo "$svc: loaded, not running right now (launchd restarts it; see: bash deploy/services.sh logs $svc)"
    fi
  done
  if [ -n "$(stray_workers)" ]; then
    echo "WARNING: thrift run outside launchd:"
    stray_workers
  fi
}

logs() {
  local svc="${1:-worker}" n="${2:-30}" f
  for f in "$LOGS/$svc.log" "$LOGS/$svc.err"; do
    echo "== $f (last $n lines)"
    if [ -f "$f" ]; then tail -n "$n" "$f"; else echo "(none yet)"; fi
  done
}

[ $# -ge 1 ] || usage
cmd="$1"
target="${2:-}"
case "$cmd" in
  start|restart)
    case "$target" in worker|poster) "$cmd" "$target" ;; *) usage ;; esac ;;
  stop)
    case "$target" in
      worker|poster) stop "$target" ;;
      all) stop worker; stop poster ;;
      *) usage ;;
    esac ;;
  status) status ;;
  logs)
    case "$target" in ""|worker|poster) logs "$target" "${3:-30}" ;; *) usage ;; esac ;;
  *) usage ;;
esac
