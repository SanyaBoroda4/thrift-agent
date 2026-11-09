#!/usr/bin/env bash
# WO54 §3 — the owner's quiet hours (8:30 AM – 2:00 PM America/New_York, config/settings.yaml `quiet`): the Mac is her
# working laptop and the agent does NOTHING on it then. The calendar job com.thriftagent.quiet (launchd: every 10
# minutes and at load; a run missed while the Mac slept comes once at wake, coalesced) runs `quiet.sh tick`, which
# decides by the CURRENT time (thrift_agent/quietctl.py — light: no model, no browser):
#   stop   inside the quiet hours (not lifted by her "post now"): the poster first (its SIGTERM: each worker finishes
#          the listing in hand — one not past its final click goes back in line, one past it is "verifying"; up to
#          its ExitTimeOut, 300 s), then the worker, then the Thrift Chrome — launchd unloads them (bootout: KeepAlive
#          can't bring them back) — then nothing of ours may be left: the poster's Playwright Chrome and the Thrift
#          Chrome by their --user-data-dir, our venv's processes. Never her own Chrome or Safari. Then the peek: a
#          "post now" from her in the private chat (getUpdates WITHOUT moving the bot's offset; nothing is ever sent
#          from here) lifts the quiet hours for today and starts everything.
#   start  outside them (or lifted), after the quiet hours stopped the agent (the marker ~/thrift/var/quiet.stopped):
#          the worker and the poster (the owner's design, WO54: the poster starts by itself at 2:00 PM; a deploy still
#          never starts it). The Thrift Chrome is the poster's to open when it has work.
# From 8:20 the processes themselves start nothing new (thrift_agent/quiet.py `closing`); a process that finds itself
# inside the quiet hours asks for this tick itself (quietctl.ask_stop).
#     bash deploy/quiet.sh tick | check | status
# What it does goes to ~/thrift/logs/quiet.log. macOS bash 3.2 and BSD tools: no `timeout`, no GNU options.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
SERVICES="$REPO/deploy/services.sh"
VAR="$HOME/thrift/var"
LOGS="$HOME/thrift/logs"
LOG="$LOGS/quiet.log"
MARK="$VAR/quiet.stopped"          # the quiet hours stopped the agent: the first tick outside them starts it again
LOCK="$VAR/quiet.lock"             # one tick at a time (mkdir is atomic)
DOMAIN="gui/$(id -u)"

mkdir -p "$VAR" "$LOGS"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

trim_log() {   # the log keeps its last 3000 lines once it passes 1 MB
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" | tr -d ' ')" -gt 1048576 ]; then
    tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

ctl() { "$PY" -I -m thrift_agent.quietctl "$@" 2>>"$LOG"; }

label_of() { if [ "$1" = chrome ]; then echo "com.thrift.chrome-cross"; else echo "com.thriftagent.$1"; fi; }
loaded() { launchctl print "$DOMAIN/$(label_of "$1")" >/dev/null 2>&1; }

# Every process of the agent still running, one per line: "<pid> <what> <command…>" — our venv's (the worker, the
# poster, a CLI command, Playwright's driver) and the two Chromes by their exact --user-data-dir (each helper carries
# it too). The patterns travel in the environment, so neither ps nor awk ever matches itself.
POSH_PROFILE=""
load_profiles() {   # in this shell (not a $(…) subshell), once: the poster's Chrome profile from the settings
  if [ -z "$POSH_PROFILE" ]; then
    POSH_PROFILE="$(ctl profiles | sed -n 1p)"
    [ -n "$POSH_PROFILE" ] || POSH_PROFILE="$HOME/thrift/chrome-profile"
  fi
}
ours() {
  ps -axo pid=,args= 2>/dev/null | Q_VENV="$REPO/.venv/" Q_P1="--user-data-dir=$POSH_PROFILE" \
    Q_P2="--user-data-dir=$HOME/thrift/chrome-cross" Q_ME="$$" awk '
    {
      pid = $1
      if (pid == ENVIRON["Q_ME"]) next
      line = $0
      sub(/^[ \t]*[0-9]+[ \t]+/, "", line)
      what = ""
      if (index(line, ENVIRON["Q_VENV"]) == 1 || index(line, " " ENVIRON["Q_VENV"]) > 0) what = "agent"
      n = split(line, a, " ")
      for (i = 1; i <= n; i++) if (a[i] == ENVIRON["Q_P1"] || a[i] == ENVIRON["Q_P2"]) what = "chrome"
      if (what != "") print pid, what, substr(line, 1, 140)
    }'
}

wait_off() {   # <service> <seconds> <process pattern>: until launchd has it unloaded and no such process runs
  local svc="$1" limit="$2" pat="$3" t=0
  while [ "$t" -lt "$limit" ]; do
    if ! loaded "$svc" && ! pgrep -f -- "$pat" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    t=$((t + 2))
  done
  return 1
}

stop_one() {   # <service> <seconds> <process pattern>
  local svc="$1" limit="$2" pat="$3" t0
  t0=$(date +%s)
  if ! loaded "$svc" && ! pgrep -f -- "$pat" >/dev/null 2>&1; then
    return 0
  fi
  bash "$SERVICES" stop "$svc" >> "$LOG" 2>&1 || log "$svc: launchctl bootout said no (still stopping?) — waiting"
  if wait_off "$svc" "$limit" "$pat"; then
    log "stop: $svc off ($(( $(date +%s) - t0 )) s)"
  else
    log "stop: $svc NOT off after $limit s"
  fi
}

sweep() {   # nothing of ours left: TERM, then KILL after 15 s
  local left pids t=0
  load_profiles
  left="$(ours)"
  [ -n "$left" ] || return 0
  printf '%s\n' "$left" | while read -r line; do log "left running: $line"; done
  pids="$(printf '%s\n' "$left" | awk '{print $1}')"
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  while [ "$t" -lt 15 ] && [ -n "$(ours)" ]; do sleep 1; t=$((t + 1)); done
  left="$(ours)"
  if [ -n "$left" ]; then
    pids="$(printf '%s\n' "$left" | awk '{print $1}')"
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
    sleep 1
  fi
  left="$(ours)"
  if [ -n "$left" ]; then
    printf '%s\n' "$left" | while read -r line; do log "STILL running: $line"; done
  else
    log "stop: nothing of the agent is left"
  fi
}

do_stop() {   # <decision>: stop | stop-takedowns
  local first=""
  [ -f "$MARK" ] || first=yes
  touch "$MARK"                          # from here on, the first tick outside the quiet hours starts the agent
  if [ "$1" = stop-takedowns ]; then     # the owner's quiet.allow_takedowns: the poster stays, for take-downs only
    stop_one worker 150 "bin/thrift run"
    [ -n "$first" ] && log "stop: the worker is off; the poster stays for take-downs (quiet.allow_takedowns)"
    return 0
  fi
  stop_one poster 330 "bin/thrift poster"
  if [ -n "$first" ]; then               # one last heartbeat (quiet.beat inside) before the worker goes: thrift-api
    log "stop: $(ctl beat)"              # mirrors the stop at once, never a false "Mac stopped answering"
  fi
  stop_one worker 150 "bin/thrift run"
  stop_one chrome 30 "--user-data-dir=$HOME/thrift/chrome-cross"
  if launchctl print "$DOMAIN/com.thriftagent.once" >/dev/null 2>&1; then     # a one-off GUI job (gui_once.sh)
    launchctl bootout "$DOMAIN/com.thriftagent.once" >/dev/null 2>&1 || true
    log "stop: the one-off job com.thriftagent.once unloaded"
  fi
  sweep
  [ -n "$first" ] && log "stop: quiet hours — $(ctl status)"
  return 0
}

do_start() {
  local ok=yes
  bash "$SERVICES" start worker >> "$LOG" 2>&1 || ok=no
  bash "$SERVICES" start poster >> "$LOG" 2>&1 || ok=no
  if [ "$ok" = yes ]; then
    rm -f "$MARK"
    log "start: the worker and the poster are up — $(ctl status)"
  else
    log "start: not everything started (see above) — tried again at the next tick"
  fi
}

tick() {
  trim_log
  if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +20 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null || true    # left by a tick that was killed
  fi
  if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0                               # another tick is at it
  fi
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
  if [ ! -x "$PY" ]; then
    log "tick: $PY missing — nothing decided (the processes' own quiet-hours gates still hold)"
    exit 0
  fi
  local decision found
  if [ -f "$MARK" ]; then
    decision="$(ctl decide --stopped)"
  else
    decision="$(ctl decide)"
  fi
  case "$decision" in
    stop|stop-takedowns)
      do_stop "$decision"
      if ! loaded worker && ! pgrep -f "bin/thrift run" >/dev/null 2>&1; then
        found="$(ctl peek)"
        if [ "$found" = post-now ]; then
          log "peek: the owner's 'post now' — the quiet hours are lifted for today ($(ctl beat))"
          do_start
        fi
      fi ;;
    start) do_start ;;
    none) ;;
    *) log "tick: no decision (${decision:-nothing printed}) — see the lines above" ;;
  esac
}

check() {   # the 8:31 proof: what of the agent is running now
  local left
  load_profiles
  left="$(ours)"
  for svc in worker poster chrome quiet; do
    if loaded "$svc"; then echo "$svc: loaded"; else echo "$svc: not loaded"; fi
  done
  if [ -n "$left" ]; then
    echo "agent processes running:"
    printf '%s\n' "$left"
  else
    echo "no agent process and no agent Chrome process is running"
  fi
}

case "${1:-}" in
  tick) tick ;;
  check) check ;;
  status) ctl status ;;
  *) echo "usage: bash deploy/quiet.sh tick | check | status" >&2; exit 2 ;;
esac
