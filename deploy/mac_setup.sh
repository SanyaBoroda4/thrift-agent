#!/usr/bin/env bash
# One-time MacBook setup. Run from ~/thrift-agent after cloning:
#     bash deploy/mac_setup.sh
# Assumes: macOS 27, Python 3.14 from python.org, Google Chrome, Apple's Command Line Tools (git).
# No Homebrew needed.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null || { echo "Install Python from python.org first"; exit 1; }
[ -d "/Applications/Google Chrome.app" ] || { echo "Install Google Chrome first"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
  || { echo "Python 3.11+ required"; exit 1; }

echo "== creating virtual environment (.venv) with $(python3 --version)"
python3 -m venv .venv
.venv/bin/pip install -U pip -q
.venv/bin/pip install -e ".[dev]" -q

echo "== folders"
mkdir -p ~/thrift/logs ~/thrift/var
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Posh"
mkdir -p "$ICLOUD/inbox" "$ICLOUD/archive"     # archive stays inside iCloud, next to inbox
[ -f config/settings.local.yaml ] || cp config/settings.local.example.yaml config/settings.local.yaml
[ -f .env ] || cp .env.example .env
.venv/bin/thrift init

echo "== tests"
.venv/bin/python -m pytest -q

echo "== launchd service files (installed, not loaded: bash deploy/services.sh start does that)"
mkdir -p ~/Library/LaunchAgents
for svc in worker poster; do
  sed "s#__HOME__#$HOME#g" "deploy/com.thriftagent.$svc.plist" > "$HOME/Library/LaunchAgents/com.thriftagent.$svc.plist"
done

cat <<MSG

Setup done. The launchd agents are installed but not running. Next:
  1. .env (ANTHROPIC_API_KEY, Telegram) and config/settings.local.yaml (machine_role: prod, iCloud inbox path)
  2. .venv/bin/thrift login --site poshmark     # needs the Chrome profile to itself: bash deploy/services.sh stop first
  3. keep poster.dry_run: true for the dry-run week
  4. bash deploy/services.sh start              # also: stop | restart | status
MSG
