"""A stand-in for the Thrift Chrome extension (WO32), in Python: a WebSocket client of the bridge that answers each job
the way ext/background.js + the content scripts do — the events, the read-back, the one click after the go-ahead —
from a script of results, so the poster loop and the driver can be tested without a browser. Plus the raw WebSocket
helpers the bridge tests use."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct

from thrift_agent.post.base import AccountBlocked, Outcome

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


async def ws_open(port: int, origin: str = "chrome-extension://thrifttest", host: str = "127.0.0.1"):
    """(reader, writer, status line) after the HTTP upgrade."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET /ext HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, head.split(b"\r\n")[0].decode()


def ws_send(writer, obj) -> None:
    data = json.dumps(obj).encode()
    mask = os.urandom(4)
    n = len(data)
    head = struct.pack(">BB", 0x81, 0x80 | n) if n < 126 else (
        struct.pack(">BBH", 0x81, 0x80 | 126, n) if n < 65536 else struct.pack(">BBQ", 0x81, 0x80 | 127, n))
    writer.write(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))


async def ws_recv(reader, timeout: float = 5.0):
    """The next message from the bridge (None when it closes)."""
    async def one():
        b1, b2 = await reader.readexactly(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await reader.readexactly(8))[0]
        payload = await reader.readexactly(n)
        return None if b1 & 0x0F == 0x8 else json.loads(payload)
    return await asyncio.wait_for(one(), timeout)


def seen_for(job: dict) -> dict:
    """The read-back the real form would give for this job's fields (what the content scripts report)."""
    f, copy = job["fields"], job["copy"]
    photos = str(len(job.get("photos") or []))
    if job["site"] == "vinted":
        seen = {"title": copy["title"], "description": copy["description"], "price": str(job["price"]),
                "photos": photos, "category": [f["category_path"]], "condition": [f["condition"]]}
        for key, field in (("size", "size"), ("skirt_length", "skirt_length")):
            if f.get(field):
                seen[key] = [f[field]]
        if f.get("colors"):
            seen["color"] = list(f["colors"])
        if f.get("materials"):
            seen["material"] = list(f["materials"])
    else:
        seen = {"description": copy["description"], "price": str(job["price"]), "photos": photos,
                "group-input": [f["category"]], "condition-input": [f["condition"]], "boost": False,
                "package": f"{f['package_size']} (up to 12 oz)"}
        if f.get("size"):
            seen["variants-input"] = [f["size"]]
        if f.get("colors"):
            seen["colour-input"] = list(f["colors"])
    seen["submit_buttons"] = 1
    seen["photos_loaded"] = photos
    if job["site"] == "vinted" and f.get("package_sizes"):
        seen["package"] = f["package_sizes"][0]
    return seen


def item_of(job: dict) -> str:
    """The item id, from the photo addresses (/photos/<item>/<n>.jpg)."""
    for u in job.get("photos") or []:
        return u.split("/photos/")[1].split("/")[0]
    return "i_0"


class FakeExtension:
    """ONE extension, as in Chrome: connects to the bridge and answers each job from its site's script (`sites`:
    {site: object with .results and .calls}): one result per job, the last one repeating — an Outcome, or an Exception
    (AccountBlocked → its page). `calls` records (item, dry run?) like the WO30 stubs."""

    def __init__(self, port: int, token: str, results: list | None = None, sites: dict | None = None):
        self.port, self.token = port, token
        self.results = list(results or [])
        self.sites = sites if sites is not None else {}
        self.calls: list[tuple[str, bool]] = []
        self.jobs: list[dict] = []
        self.clicks = 0
        self.connected = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.writer = None

    def start(self) -> asyncio.Task:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.run())
        return self.task

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.writer is not None:
            self.writer.close()

    def next_result(self, site: str | None = None):
        script = self.sites.get(site)
        results = script.results if script is not None else self.results
        return results.pop(0) if len(results) > 1 else results[0]

    def record(self, site: str, call: tuple[str, bool]) -> None:
        script = self.sites.get(site)
        (script.calls if script is not None else self.calls).append(call)

    async def run(self) -> None:
        try:
            await self._run()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            return                      # the bridge closed (the poster stopped): as the real one, it just goes quiet

    async def _run(self) -> None:
        reader, writer, status = await ws_open(self.port)
        self.writer = writer
        assert " 101 " in status, status
        ws_send(writer, {"type": "hello", "token": self.token})
        commands: asyncio.Queue = asyncio.Queue()
        while True:
            msg = await ws_recv(reader, timeout=3600)
            if msg is None:
                return
            if msg.get("type") == "welcome":
                self.connected.set()
            elif msg.get("type") == "job":
                asyncio.get_running_loop().create_task(self.answer(writer, msg["job"], commands))
            elif msg.get("type") in ("submit", "cancel"):
                commands.put_nowait(msg)

    def event(self, writer, **ev) -> None:
        ws_send(writer, {"type": "event", **ev})

    async def answer(self, writer, job: dict, commands: asyncio.Queue) -> None:
        self.jobs.append(job)
        jid, mode, site = job["job_id"], job["mode"], job["site"]
        if mode in ("find", "verify", "check_login", "delist"):
            res = self.next_result(site) if (self.results or site in self.sites) else Outcome("posted")
            if mode == "find":
                self.event(writer, event="result", job_id=jid, listings=getattr(res, "listings", []))
            elif mode == "check_login":
                if isinstance(res, Exception):
                    self.event(writer, event="error", job_id=jid, stage="check_login", page="login", message=str(res))
                else:
                    self.event(writer, event="result", job_id=jid, page="form")
            else:
                self.event(writer, event="result", job_id=jid, url=job.get("listing_url"), delisted=True,
                           live={"body": getattr(res, "body", ""), "photos": 0})
            await writer.drain()
            return
        self.record(site, (item_of(job), mode == "dry_run"))
        res = self.next_result(site)
        if isinstance(res, AccountBlocked):
            self.event(writer, event="error", job_id=jid, stage="open", page=res.page or "login", message=str(res))
            await writer.drain()
            return
        if isinstance(res, Exception):
            self.event(writer, event="error", job_id=jid, stage="open", page="unknown", message=str(res))
            await writer.drain()
            return
        for name in ("photos", "title", "description", "category", "price"):
            self.event(writer, event="step", job_id=jid, name=name, ok=True)
        self.event(writer, event="screenshot", job_id=jid, label="form", png_b64=base64.b64encode(PNG).decode(),
                   html="<html><body>form</body></html>", url="https://example.invalid/form")
        seen = seen_for(job)
        guesses = list(getattr(res, "guesses", []) or [])
        if res.status == "failed" and not res.clicked:
            self.event(writer, event="error", job_id=jid, stage="form", page="form", message=res.error or "failed")
            await writer.drain()
            return
        if mode == "dry_run":
            self.event(writer, event="result", job_id=jid, dry_run=True, seen=seen, failed=[], guesses=guesses,
                       notes=[])
            await writer.drain()
            return
        self.event(writer, event="ready", job_id=jid, seen=seen, failed=[], guesses=guesses, notes=[])
        await writer.drain()
        cmd = await asyncio.wait_for(commands.get(), 30)
        if cmd["type"] != "submit":
            self.event(writer, event="error", job_id=jid, stage="cancelled", page="form", message="cancelled")
            await writer.drain()
            return
        self.clicks += 1
        self.event(writer, event="step", job_id=jid, name="submit", ok=True, clicked=True)
        iid = item_of(job)
        url = (res.url or "").replace("{iid}", iid).replace("{slug}", iid.replace("_", "-"))
        if res.status == "posted" and url:
            title = job["copy"]["title"]
            self.event(writer, event="screenshot", job_id=jid, label="after-publish",
                       png_b64=base64.b64encode(PNG).decode(), url=url)
            self.event(writer, event="result", job_id=jid, url=url,
                       live={"body": f"{title}\n${job['price']}.00\n{job['fields'].get('size') or ''}",
                             "photos": len(job.get("photos") or [])})
        else:
            self.event(writer, event="error", job_id=jid, stage="after_publish", page="unknown",
                       message=res.error or "no listing page")
        await writer.drain()
