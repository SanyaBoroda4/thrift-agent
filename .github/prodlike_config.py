"""CI only: make the checkout look like the Mac before .env is filled in, then the suite must still pass.

Writes config/settings.local.yaml (machine_role: prod, telegram.enabled: true), private/settings.yaml (a username)
and an empty-token .env. Refuses to run outside GitHub Actions so it can never clobber a real machine's files."""
import os
import sys
from pathlib import Path

if os.environ.get("GITHUB_ACTIONS") != "true":
    sys.exit("prodlike_config.py only runs in CI (it overwrites config/settings.local.yaml, private/settings.yaml and .env)")

root = Path(__file__).resolve().parent.parent
(root / "config" / "settings.local.yaml").write_text(
    "machine_role: prod\ntelegram:\n  enabled: true\npaths:\n  inbox: ./var/prod-inbox\n", encoding="utf-8")
(root / "private").mkdir(exist_ok=True)
(root / "private" / "settings.yaml").write_text("marketplaces:\n  poshmark:\n    username: someone\n", encoding="utf-8")
(root / ".env").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nANTHROPIC_API_KEY=\n", encoding="utf-8")
print("prod-like config in place")
