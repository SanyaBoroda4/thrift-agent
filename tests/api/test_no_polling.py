"""The API only ever sends to Telegram (WO33): the Mac's worker is the bot's one reader (long polling), so nothing under
api/ may read the bot's updates or set a webhook — a second reader would take the worker's updates away. This file
lives in tests/, so the words below never trip the scan."""
from thrift_agent.config import ROOT

FORBIDDEN = ("getUpdates", "setWebhook")


def test_nothing_under_api_reads_updates_or_sets_a_webhook():
    api = ROOT / "api"
    files = [p for p in api.rglob("*") if p.is_file()
             and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(api).parts[:-1])]
    assert any(p.name == "telegram.py" for p in files)                 # the scan does see the sender
    found = [(p.relative_to(ROOT).as_posix(), word) for p in files for word in FORBIDDEN
             if word.lower() in p.read_text(encoding="utf-8", errors="ignore").lower()]
    assert found == []
