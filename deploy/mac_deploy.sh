#!/usr/bin/env bash
# Run on the Mac by deploy/deploy.ps1, right after its `git pull`:
#     bash deploy/mac_deploy.sh <the commit before the pull>
# private/ pull → mac_setup.sh (venv, folders, tests, launchd files, the extension's token) → the WORKER (re)started as
# the launchd service → the Thrift Chrome started if it isn't running (WO32; never restarted) → services status,
# `thrift status` and the last 30 lines of the worker log. A failed setup or test run rolls the
# checkout back to the commit before the pull, so launchd never runs untested code. Any failure exits non-zero.
# The poster service is never started here: it stays off until the owner turns publishing on (the two keys).
set -euo pipefail
cd "$(dirname "$0")/.."
before="${1:-}"

echo "== code $(git log --oneline -1)"
if [ -d private/.git ]; then
  if [ -f .private.bundle ]; then
    # deploy.ps1 ships the PC's private repo as a bundle (an SSH session can't use the keychain's GitHub credential).
    # Fast-forward only: private commits made on the Mac and not on the PC stop the deploy rather than vanish.
    git -C private pull --ff-only ../.private.bundle main
    rm -f .private.bundle
  else
    git -C private pull --ff-only
  fi
  echo "== private $(git -C private log --oneline -1)"
fi

if ! THRIFT_DEPLOY=1 bash deploy/mac_setup.sh; then
  if [ -n "$before" ] && [ "$before" != "$(git rev-parse HEAD)" ]; then
    echo "== setup or tests failed: rolling back to $before"
    git reset --hard "$before"
    .venv/bin/pip install -q -e .
  fi
  exit 1
fi

scan() { .venv/bin/thrift status 2>/dev/null | sed -n 's/^worker inbox scan: //p'; }
last_scan="$(scan)"
bash deploy/services.sh restart worker
# A worker that started and then waits (silently) is no deploy either: wait for its first pass over the inbox.
scanned=""
for _ in $(seq 1 12); do
  sleep 5
  now_scan="$(scan)"
  if [ -n "$now_scan" ] && [ "$now_scan" != never ] && [ "$now_scan" != "$last_scan" ]; then scanned=yes; break; fi
done
bash deploy/services.sh start chrome || echo "== the Thrift Chrome didn't start (see above)" >&2
bash deploy/services.sh status
echo "== thrift status"
.venv/bin/thrift status
bash deploy/services.sh logs worker 30
if ! launchctl print "gui/$(id -u)/com.thriftagent.worker" 2>/dev/null | grep -q "state = running"; then
  echo "== the worker is not running: see the log above" >&2
  exit 1
fi
if [ -z "$scanned" ]; then
  echo "== the worker runs but hasn't finished a pass over the inbox in 60 s. If the Mac's screen shows" >&2
  echo "   \"python3.14 would like to access files in your iCloud Drive\", click Allow there (once)." >&2
  exit 1
fi
echo "== deployed"
