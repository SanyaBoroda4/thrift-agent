#!/usr/bin/env bash
# MacBook setup: once after cloning, and again on every deploy (deploy/mac_deploy.sh runs it). From ~/thrift-agent:
#     bash deploy/mac_setup.sh
# Assumes: macOS 27, Python 3.14 from python.org, Google Chrome, Apple's Command Line Tools (git).
# No Homebrew needed.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d "/Applications/Google Chrome.app" ] || { echo "Install Google Chrome first"; exit 1; }
# The interpreter: the venv's own when there is one (never rebuilt with another Python); else python.org's. An SSH
# session's PATH is only /usr/bin:/bin:/usr/sbin:/sbin, where python3 is Apple's 3.9, so python.org's is looked for
# where its installer puts it.
PY=""
for c in .venv/bin/python /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$c"
    break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11+ required: install it from python.org"; exit 1; }
if [ "$PY" != .venv/bin/python ]; then
  echo "== creating virtual environment (.venv) with $("$PY" --version)"
  "$PY" -m venv .venv
fi
echo "== packages ($(.venv/bin/python --version))"
.venv/bin/pip install -U pip -q
.venv/bin/pip install -e ".[dev]" -q

echo "== folders"
mkdir -p ~/thrift/logs ~/thrift/var ~/thrift/chrome-cross
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Posh"
mkdir -p "$ICLOUD/inbox" "$ICLOUD/archive"     # archive stays inside iCloud, next to inbox
[ -f config/settings.local.yaml ] || cp config/settings.local.example.yaml config/settings.local.yaml
[ -f .env ] || cp .env.example .env
.venv/bin/thrift init

echo "== tests"
.venv/bin/python -m pytest -q

echo "== the extension's token (WO32: made once, never changed by a deploy; pasted once into the extension's options)"
TOKEN_FILE="$HOME/thrift/var/ext_token"
if [ ! -s "$TOKEN_FILE" ]; then
  (umask 077; .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
  echo "new token: $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

echo "== launchd service files (installed, not loaded: bash deploy/services.sh start worker|chrome does that)"
mkdir -p ~/Library/LaunchAgents
for svc in worker poster; do
  sed "s#__HOME__#$HOME#g" "deploy/com.thriftagent.$svc.plist" > "$HOME/Library/LaunchAgents/com.thriftagent.$svc.plist"
done
sed "s#__HOME__#$HOME#g" deploy/com.thrift.chrome-cross.plist > "$HOME/Library/LaunchAgents/com.thrift.chrome-cross.plist"

[ -n "${THRIFT_DEPLOY:-}" ] && exit 0                 # deploy/mac_deploy.sh goes on from here
cat <<MSG

Setup done. The launchd agents are installed but not running. Next:
  1. .env (ANTHROPIC_API_KEY, Telegram) and config/settings.local.yaml (machine_role: prod, iCloud inbox path)
  2. .venv/bin/thrift login --site poshmark     # needs the Chrome profile to itself: bash deploy/services.sh stop poster
  3. keep poster.dry_run: true for the dry-run week
  4. bash deploy/services.sh start worker       # also: stop | restart worker|poster, status, logs
MSG
