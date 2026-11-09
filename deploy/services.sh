#!/usr/bin/env bash
# The launchd agents on the Mac: the worker (thrift run), the poster (thrift poster) and the Thrift Chrome (WO32: the
# Chrome the Depop / Vinted extension runs in; WO54 §4: not the owner's browser — the poster starts it for work and quits
# it after 10 idle minutes, launchd never starts it by itself).
#     bash deploy/services.sh start worker|poster|chrome
#     bash deploy/services.sh stop worker|poster|chrome|all     (all: the worker and the poster)
#     bash deploy/services.sh restart worker|poster     (starts it when it isn't loaded; the installed plist applies)
#     bash deploy/services.sh load chrome     (its installed plist loaded, the Chrome NOT started: the deploy's step)
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
  echo "usage: bash deploy/services.sh start|stop|restart worker|poster | start|stop|load chrome | stop all | status |" \
       "logs [worker|poster] [lines]" >&2
  exit 2
}
label() { if [ "$1" = chrome ]; then echo "com.thrift.chrome-cross"; else echo "com.thriftagent.$1"; fi; }
loaded() { launchctl print "$DOMAIN/$(label "$1")" >/dev/null 2>&1; }
service_pid() { launchctl print "$DOMAIN/$(label "$1")" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}'; }
plist_of() { echo "$HOME/Library/LaunchAgents/$(label "$1").plist"; }
# A bootout returns before launchd has let go of the label at times: the next bootstrap waits for it (5 s at most).
wait_unloaded() { local i; for i in $(seq 1 20); do loaded "$1" || return 0; sleep 0.25; done; }

# WO54 §4: launchd keeps a job's settings from its bootstrap — a loaded Thrift Chrome job older than the installed
# plist (the one that opened a Vinted page and restored old pages) is replaced before it is started; one that runs is
# left alone (the poster quits it when idle, and the next start replaces it).
chrome_current() { launchctl print "$DOMAIN/$(label chrome)" 2>/dev/null | grep -q "chrome_cross.sh"; }
load_chrome() {
  local plist
  plist="$(plist_of chrome)"
  [ -f "$plist" ] || { echo "chrome: $plist missing - run bash deploy/mac_setup.sh first" >&2; exit 1; }
  if loaded chrome && [ -n "$(service_pid chrome)" ]; then
    echo "chrome: running (left alone: the poster quits it after 10 idle minutes)"
    return
  fi
  if loaded chrome && ! chrome_current; then
    launchctl bootout "$DOMAIN/$(label chrome)" 2>/dev/null || true
    wait_unloaded chrome
  fi
  loaded chrome || launchctl bootstrap "$DOMAIN" "$plist"
  echo "chrome: loaded (not started: the poster opens it when its extension has work)"
}
start_chrome() {
  if loaded chrome && [ -n "$(service_pid chrome)" ]; then
    echo "chrome: already running"
    return
  fi
  load_chrome >/dev/null
  launchctl kickstart "$DOMAIN/$(label chrome)"
  echo "chrome: started"
}

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
  plist="$(plist_of "$svc")"
  if [ "$svc" = chrome ]; then
    start_chrome
    return
  fi
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
  # WO54 §4: bootout + bootstrap, not `kickstart -k` — launchd keeps a job's settings from its bootstrap, and a deploy's
  # new plist (the worker's ProcessType Background, the poster's Nice / LowPriorityIO) must apply
  if loaded "$1"; then
    launchctl bootout "$DOMAIN/$(label "$1")"
    wait_unloaded "$1"
    launchctl bootstrap "$DOMAIN" "$(plist_of "$1")"
    echo "$1: restarted"
  else
    start "$1"
  fi
}

status() {
  local svc pid
  for svc in worker poster chrome; do
    if ! loaded "$svc"; then
      echo "$svc: not loaded (off)"
    elif pid="$(service_pid "$svc")" && [ -n "$pid" ]; then
      echo "$svc: running (pid $pid)"
    elif [ "$svc" = chrome ]; then
      echo "chrome: loaded, not running (the poster opens it when its extension has work; it quits after 10 idle min)"
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
    case "$target" in
      worker|poster) "$cmd" "$target" ;;
      chrome) [ "$cmd" = start ] && start chrome || usage ;;
      *) usage ;;
    esac ;;
  load)
    case "$target" in chrome) load_chrome ;; *) usage ;; esac ;;
  stop)
    case "$target" in
      worker|poster|chrome) stop "$target" ;;
      all) stop worker; stop poster ;;
      *) usage ;;
    esac ;;
  status) status ;;
  logs)
    case "$target" in ""|worker|poster) logs "$target" "${3:-30}" ;; *) usage ;; esac ;;
  *) usage ;;
esac
