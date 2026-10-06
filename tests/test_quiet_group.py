"""WO29: a quiet group. During a whole share -> price -> publish run the GROUP sees only the card, the card's "✓ $X —
queued" (an edit, not a message) and "Posted ✓ … ✓ All done — safe to close the Mac."; "Back online", the status
message, "Poster started (LIVE)" and every error go to the OPS chat. The model, Telegram and the browser are stubbed."""
import asyncio
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import alerts, approve, cli, daily, notify, pipeline
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.post import runner
from thrift_agent.post.base import Outcome
from thrift_agent.schema import CopyOut, Ev, VerifyOut
from thrift_agent.telegram import Bot

CHAT, OPS, OWNER = 100, 5550001, 7
LINE = "Gently pre-loved, please see photos for condition."
TITLE = "Tory Burch Red Ballet Flats size 7.5"


class FakeBot(Bot):
    """Records (method, params) instead of calling Telegram; hands out message ids."""

    def __init__(self, chat):
        super().__init__("TOK", chat, {OWNER})
        self.calls, self.next_id = [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True

    def sends(self):
        return [(m, p.get("text") or p.get("caption")) for m, p in self.calls if m in ("sendMessage", "sendPhoto")]


def _model(model, system, content, out, tool, description, **kw):
    if out.__name__ == "Facts":
        from thrift_agent.schema import Facts
        return Facts(item_type="suede ballet flats", department="Women", category="Shoes",
                     subcategory="Flats & Loafers", brand=Ev(value="Tory Burch", photos=[1], source="photo",
                                                            confidence=0.97),
                     size_printed=Ev(value="7.5M", photos=[1], source="photo", confidence=0.95),
                     size_us=Ev(value="7.5", photos=[1], source="photo", confidence=0.95), colors=["Red"],
                     condition="excellent", condition_evidence=Ev(value="light wear", photos=[2], source="photo",
                                                                  confidence=0.9), cover_photo=0,
                     photo_order=[0, 1, 2])
    if out is CopyOut:
        return CopyOut(poshmark_title=TITLE, poshmark_description=f"Red flats.\n{LINE}", poshmark_style_tags=[],
                       depop_description=f"red flats. {LINE}", depop_hashtags=["a", "b", "c", "d", "e"])
    if out is VerifyOut:
        return VerifyOut(poshmark_title=TITLE, poshmark_description=f"Red flats.\n{LINE}",
                         depop_description=f"red flats. {LINE}")
    if out.__name__ == "FrontOut":
        return out(views=[], front=-1)
    if out.__name__ == "UprightOut":
        return out(upright="A")
    if out.__name__ == "SizeLabel":
        return out(printed=None)
    raise AssertionError(out)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """The Mac, LIVE (the three keys on), with a group bot and an ops bot; every message recorded by where it went."""
    base = settings().data
    data = {**base, "machine_role": "prod", "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    data["poster"] = {**base["poster"], "dry_run": False, "autopublish_confirmed": True}
    data["marketplaces"] = {"poshmark": {**base["marketplaces"]["poshmark"], "enabled": True, "username": "closet",
                                         "autopublish": True},
                            "depop": {**base["marketplaces"]["depop"], "enabled": False}}
    data["schedule"] = {**base["schedule"], "hours": ["00:00", "23:59"]}
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    group, ops = FakeBot(CHAT), FakeBot(OPS)
    said = {"group": [], "ops": []}
    monkeypatch.setattr(approve, "bot_for", lambda _s: group)
    monkeypatch.setattr(approve, "ops_bot_for", lambda _s: ops)
    monkeypatch.setattr(notify, "group", lambda text: said["group"].append(text))
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: said["group"].append(caption))
    monkeypatch.setattr(notify, "say", lambda text: said["ops"].append(text))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said["ops"].append(caption))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model)
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda s_, db_=None: [])
    for name in ("lid_closed", "battery", "last_wake"):
        monkeypatch.setattr(f"thrift_agent.power.{name}", lambda: None)
    return s, db, group, ops, said


def _new_item(tmp_path, db, seq=1) -> str:
    bid = db.add_batch(f"share_{seq}", 3)
    db.set_batch(bid, status="split")
    d = tmp_path / f"item{seq}"
    for i in range(3):                                                # noise: each item its own photos (a solid
        (d / "photos").mkdir(parents=True, exist_ok=True)              # colour hashes alike: a "re-share")
        Image.effect_noise((60, 80), 90).convert("RGB").save(d / "photos" / f"{i:02d}.jpg")
    return db.add_item(bid, seq, str(d))


class Poster:
    name = "poshmark"

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        return Outcome("posted", url=f"https://poshmark.com/listing/{r.sku}", screenshot=str(shots / "x.png"))

    async def find_live(self, ctx, r, since, created=None):
        return None, {}


def _publish(monkeypatch, s, db, once=True):
    class Ctx:
        async def close(self):
            pass

    class PW:
        async def stop(self):
            pass

    async def browser(profile, tz):
        return PW(), Ctx()

    async def no_pause(stop, seconds):
        stop.set()

    monkeypatch.setattr(runner, "open_browser", browser)
    monkeypatch.setattr(runner, "posters", lambda s_: {"poshmark": Poster()})
    monkeypatch.setattr(runner, "_pause", no_pause)
    asyncio.run(runner.run(s, db, once=once))


def _cb(data, mid):
    return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": OWNER}, "data": data,
                                               "message": {"message_id": mid, "chat": {"id": CHAT}}}}


def test_a_whole_run_shows_the_group_only_the_card_and_posted(mac, tmp_path, monkeypatch):
    s, db, group, ops, said = mac
    window = daily.Window(s, db, approve.ops_bot_for(s), lid=lambda: False, battery=lambda: None)
    iid = _new_item(tmp_path, db)
    window.step()                                                      # the worker starts: a window
    cli._tick(s, db)                                                   # the share processed: its card goes out
    approve.pump(s, db)
    window.update()                                                    # the status message: the ops chat
    [(kind, card)] = group.sends()
    assert kind == "sendPhoto" and card.startswith(TITLE)              # the card, in the group
    approve.handle_update(s, db, group, _cb(f"approve:{iid}:85", group.next_id))   # she taps $85
    assert group.sends() == [(kind, card)]                             # no "✓ $85 — N left" message
    assert [p["reply_markup"]["inline_keyboard"][0][0]["text"] for m, p in group.calls
            if m == "editMessageReplyMarkup"] == ["✓ $85 — queued"]    # the card itself says it
    _publish(monkeypatch, s, db)                                       # the poster lists it
    window.update()
    assert said["group"] == [f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{iid}\n"
                             "✓ All done — safe to close the Mac."]     # the group: the card and this, nothing else
    assert any(t.startswith("Poster started (LIVE)") for t in said["ops"])          # the ops chat: the rest
    status = [p["text"] for m, p in ops.calls if m in ("sendMessage", "editMessageText")]
    assert status and status[-1] == "✓ All done — safe to close the Mac."          # the status message: ops
    assert not any(t.startswith(("Back online", "⏳", "Poster started")) for t in said["group"])


def test_all_done_comes_only_with_the_windows_last_listing(mac, tmp_path, monkeypatch):
    s, db, group, ops, said = mac
    a, b = _new_item(tmp_path, db, 1), _new_item(tmp_path, db, 2)
    cli._tick(s, db)
    for iid in (a, b):
        pipeline.set_price(s, db, iid, 85)
    _publish(monkeypatch, s, db)                                       # the first listing: one more to go
    _publish(monkeypatch, s, db)                                       # the second: the window's last
    assert said["group"] == [f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{a}",
                             f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{b}\n"
                             "✓ All done — safe to close the Mac."]


def test_a_card_still_waiting_keeps_all_done_back(mac, tmp_path, monkeypatch):
    s, db, group, ops, said = mac
    a, _b = _new_item(tmp_path, db, 1), _new_item(tmp_path, db, 2)
    cli._tick(s, db)
    pipeline.set_price(s, db, a, 85)                                   # b still waits for its price
    _publish(monkeypatch, s, db)
    assert said["group"] == [f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{a}"]


def test_no_traceback_or_errno_ever_reaches_the_group(mac, tmp_path, monkeypatch):
    """The live miss: "❌ worker tick: OSError: [Errno 11] Resource deadlock avoided" in the group. Any error now goes
    to the ops chat; iCloud still downloading says nothing at all."""
    s, db, group, ops, said = mac
    import errno

    def busy(s_, db_=None):
        raise OSError(errno.EDEADLK, "Resource deadlock avoided", "/Posh/inbox/2026-01-02_120000")
    monkeypatch.setattr(cli.pipeline, "ready_folders", busy)
    cli._safe_tick(s, db)
    assert said == {"group": [], "ops": []} and group.sends() == []     # quiet: just retried

    def broken(s_, db_=None):
        raise RuntimeError("something else")
    monkeypatch.setattr(cli.pipeline, "ready_folders", broken)
    cli._safe_tick(s, db)
    assert said["group"] == [] and said["ops"] == ["❌ worker tick: RuntimeError: something else"]
    iid = _new_item(tmp_path, db)
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda s_, db_=None: [])
    monkeypatch.setattr(cli.pipeline, "process_item", lambda *a: (_ for _ in ()).throw(KeyError("facts")))
    cli._tick(s, db)
    assert said["group"] == [] and any(t.startswith(f"❌ {iid}: KeyError") for t in said["ops"])
    assert group.sends() == []
    assert alerts.INBOX_GRACE == 600                                   # the inbox line: only after 10 minutes


def test_cli_echoes_go_to_the_ops_chat(mac, monkeypatch):
    s, db, group, ops, said = mac
    sent = []
    monkeypatch.setattr(notify, "ops", lambda text, once=True: sent.append(text) or True)
    assert approve.announce(s, "i_x: price $85 set from the CLI -> ready") is True
    assert sent == ["i_x: price $85 set from the CLI -> ready"] and group.sends() == []


def test_the_battery_line_is_the_groups_but_back_online_and_the_status_are_ops(mac, tmp_path):
    s, db, group, ops, said = mac
    from thrift_agent import power
    _new_item(tmp_path, db)
    window = daily.Window(s, db, approve.ops_bot_for(s), lid=lambda: False,
                          battery=lambda: power.Battery(9, False))
    window.step()
    window.update()
    assert said["group"] == [power.BATTERY_LOW]
    assert said["ops"] and said["ops"][0].startswith("Back online") and ops.sends()   # "Back online", the status
    assert group.sends() == []


def test_an_ops_chat_that_cant_be_reached_drops_the_status_message_never_the_group(mac, tmp_path):
    s, db, group, ops, said = mac

    class Refused(FakeBot):
        def call(self, method, **params):
            raise RuntimeError("telegram sendMessage failed: Forbidden: bot can't initiate conversation with a user")

    _new_item(tmp_path, db)
    window = daily.Window(s, db, Refused(OPS), lid=lambda: False, battery=lambda: None)
    window.step()
    window.update()
    window.update()
    assert group.sends() == [] and said["group"] == []
    assert db.kv_get(notify.OPS_DOWN)                                  # left alone for a while, nothing raised


def test_the_card_of_a_tap_shows_the_answer_and_its_button_does_nothing(mac, tmp_path):
    s, db, group, ops, said = mac
    iid = _new_item(tmp_path, db)
    cli._tick(s, db)
    approve.pump(s, db)
    card = group.next_id
    approve.handle_update(s, db, group, _cb(f"approve:{iid}:85", card))
    [edit] = [p for m, p in group.calls if m == "editMessageReplyMarkup"]
    assert edit["message_id"] == card and edit["reply_markup"]["inline_keyboard"] == [
        [{"text": "✓ $85 — queued", "callback_data": "noop"}]]
    assert approve.handle_update(s, db, group, _cb("noop", card)) == "ignored: answered card"
    assert Path(db.item(iid)["dir"]).exists()
