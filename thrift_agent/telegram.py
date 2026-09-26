"""Thin Telegram Bot API client (httpx). Skeleton: another agent fills in the bodies; keep the signatures."""
from __future__ import annotations

from pathlib import Path


class Bot:
    def __init__(self, token: str, chat_id: str, allowed_users: set[int]):
        self.token, self.chat_id, self.allowed_users = token, str(chat_id), set(allowed_users)

    def call(self, method: str, **params) -> dict:
        """POST https://api.telegram.org/bot<token>/<method>; returns the 'result' object; raises on error."""
        raise NotImplementedError

    def send_message(self, text: str, buttons: list[list[dict]] | None = None, reply_to: int | None = None) -> int:
        """Returns the sent message_id. `buttons` = rows of {"text": ..., "callback_data": ...}."""
        raise NotImplementedError

    def send_photo(self, path: Path, caption: str, buttons: list[list[dict]] | None = None) -> int:
        raise NotImplementedError

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        raise NotImplementedError

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        raise NotImplementedError

    def authorized(self, update: dict) -> bool:
        """True only for updates from self.chat_id sent by a user in self.allowed_users."""
        raise NotImplementedError
