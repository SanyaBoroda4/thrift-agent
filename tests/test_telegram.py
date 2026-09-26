"""Bot API client: call() encoding and error handling (httpx mocked), the wrappers, and authorized()."""
import httpx
import pytest

from thrift_agent.telegram import Bot


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post(monkeypatch, resp):
    seen = {}

    def fake_post(url, data=None, files=None, timeout=None):
        seen.update(url=url, data=data, files=files, timeout=timeout)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr("thrift_agent.telegram.httpx.post", fake_post)
    return seen


class FakeBot(Bot):
    """Records every call() and answers with canned results."""
    def __init__(self, chat_id="100", users=(7,)):
        super().__init__("TOK", chat_id, set(users))
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"message_id": 41} if method.startswith("send") else ([] if method == "getUpdates" else True)


def test_call_encodes_json_files_and_drops_none(monkeypatch, tmp_path):
    seen = _post(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 3}}))
    img = tmp_path / "cover.jpg"
    img.write_bytes(b"jpegbytes")
    res = Bot("TOK", "100", {7}).call("sendPhoto", chat_id="100", photo=img, caption="hi",
                                      reply_markup={"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]},
                                      reply_to_message_id=None)
    assert res == {"message_id": 3}
    assert seen["url"] == "https://api.telegram.org/botTOK/sendPhoto"
    assert seen["data"] == {"chat_id": "100", "caption": "hi",
                            "reply_markup": '{"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}'}
    assert seen["files"] == {"photo": ("cover.jpg", b"jpegbytes")}
    assert seen["timeout"] == 20


def test_call_get_updates_uses_long_poll_timeout_plus_ten(monkeypatch):
    seen = _post(monkeypatch, Resp(200, {"ok": True, "result": []}))
    assert Bot("TOK", "100", {7}).get_updates(offset=12, timeout=15) == []
    assert seen["timeout"] == 25 and seen["files"] is None
    assert seen["data"] == {"offset": 12, "timeout": 15, "allowed_updates": '["message", "callback_query"]'}


def test_call_raises_with_description_on_ok_false(monkeypatch):
    _post(monkeypatch, Resp(200, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(RuntimeError, match="chat not found"):
        Bot("TOK", "100", {7}).send_message("hi")


def test_call_raises_on_non_2xx_and_transport_errors(monkeypatch):
    _post(monkeypatch, Resp(502, ValueError("not json")))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        Bot("TOK", "100", {7}).call("getMe")
    _post(monkeypatch, httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="ConnectError"):
        Bot("TOK", "100", {7}).call("getMe")


def test_send_message_builds_reply_markup_and_reply_to():
    b = FakeBot()
    rows = [[{"text": "Approve $85", "callback_data": "approve:i_1:85"}, {"text": "Change", "callback_data": "change:i_1"}]]
    assert b.send_message("hello", buttons=rows, reply_to=9) == 41
    method, p = b.calls[0]
    assert method == "sendMessage" and p["chat_id"] == "100" and p["text"] == "hello"
    assert p["reply_markup"] == {"inline_keyboard": rows} and p["reply_to_message_id"] == 9

    b.send_message("plain")
    assert b.calls[1][1]["reply_markup"] is None and b.calls[1][1]["reply_to_message_id"] is None


def test_send_photo_truncates_caption(tmp_path):
    b = FakeBot()
    img = tmp_path / "c.jpg"
    img.write_bytes(b"x")
    assert b.send_photo(img, "c" * 2000) == 41
    method, p = b.calls[0]
    assert method == "sendPhoto" and p["photo"] == img and len(p["caption"]) == 1024 and p["reply_markup"] is None


def test_answer_callback():
    b = FakeBot()
    b.answer_callback("cb1", "Approved $85")
    b.answer_callback("cb2")
    assert b.calls[0] == ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Approved $85"})
    assert b.calls[1][1]["text"] is None


def _msg(chat, user, **extra):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": chat}, "from": {"id": user}, "text": "ok", **extra}}


def _cb(chat, user):
    return {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": user}, "data": "change:i_1",
                                               "message": {"message_id": 5, "chat": {"id": chat}}}}


@pytest.mark.parametrize("update, ok", [
    (_msg(100, 7), True),
    (_msg(-100999, 7), False),               # another chat
    (_msg(100, 8), False),                   # right chat, stranger
    (_msg(100, "7"), False),                 # ids are ints on the wire; a string never matches
    (_cb(100, 7), True),
    (_cb(100, 9), False),
    (_cb(200, 7), False),
    ({"update_id": 3, "edited_message": {"chat": {"id": 100}, "from": {"id": 7}}}, False),
    ({"update_id": 4}, False),
    ({"update_id": 5, "message": {"chat": {"id": 100}}}, False),     # channel post without a sender
])
def test_authorized(update, ok):
    assert Bot("TOK", "100", {7}).authorized(update) is ok


def test_authorized_matches_chat_id_as_string_and_nobody_when_no_users():
    assert Bot("TOK", -100999, {7}).authorized(_msg(-100999, 7)) is True
    assert Bot("TOK", "100", set()).authorized(_msg(100, 7)) is False
