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
mkdir -p "$ICLOUD/inbox"
[ -f config/settings.local.yaml ] || cp config/settings.local.example.yaml config/settings.local.yaml
[ -f .env ] || cp .env.example .env
.venv/bin/thrift init

echo "== tests"
.venv/bin/python -m pytest -q

echo "== launchd service files (not started yet)"
mkdir -p ~/Library/LaunchAgents
for svc in worker poster; do
  sed "s#__HOME__#$HOME#g" "deploy/com.thriftagent.$svc.plist" > "$HOME/Library/LaunchAgents/com.thriftagent.$svc.plist"
done

cat <<MSG

Setup done. Next steps are in the chat — don't start the services yet.
MSG
