"""CI only: put a dummy .env with real-looking values at the repo root, then the suite must still pass.

Refuses to run outside GitHub Actions so it can never overwrite a real machine's .env."""
import os
import sys
from pathlib import Path

if os.environ.get("GITHUB_ACTIONS") != "true":
    sys.exit("dummy_env.py only runs in CI (it overwrites .env)")

root = Path(__file__).resolve().parent.parent
LINES = [
    "TELEGRAM_BOT_TOKEN=123:dummy-token",
    "TELEGRAM_CHAT_ID=-1001234567890",
    "TELEGRAM_ALLOWED_USER_IDS=111,222",
    "ANTHROPIC_API_KEY=sk-ant-dummy",
]
(root / ".env").write_text(chr(10).join(LINES) + chr(10), encoding="utf-8")
print("dummy .env in place")
