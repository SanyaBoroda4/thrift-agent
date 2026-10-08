"""The extension bridge (WO32): a local HTTP + WebSocket server inside the poster process, on 127.0.0.1:8765 only, that
hands Vinted and Depop jobs to the Thrift Chrome extension (ext/) running in the seller's own, normally opened Chrome —
no DevTools connection, no automation flags.

- Token-checked (~/thrift/var/ext_token, made at deploy): the WebSocket's first message, the X-Thrift-Token header on
  HTTP. A WebSocket must come from an extension origin (chrome-extension://…), a request must name 127.0.0.1 / localhost
  as its host.
- `ws://127.0.0.1:8765/ext`: hello {token} → welcome {ext_hash}; jobs pushed {"type": "job", "job": {…}}; the
  extension's events {"type": "event", "event": "step|screenshot|ready|result|error", "job_id", …}; the go-ahead of a
  filled form {"type": "submit"|"cancel", "job_id"}.
- `GET /jobs/next` the same job object (the extension's alarm, while its socket is down); `POST /events` an event, its
  answer the job's commands; `GET /photos/<item>/<n>.jpg` the item's photos (already rotated and resized, the cover
  first), with Access-Control-Allow-Origin only for https://www.vinted.com and https://www.depop.com; `GET /health`.
- One job at a time per site (the extension runs one at a time in all). Screenshots and the page's HTML are written
  next to the job's evidence path (<shot>-<label>.png / .html). A job's steps and progress ("tab opened", "photos 6/6",
  "category ✓") reach its listener as they happen (WO32b: the CLI prints them). A job lasts 3 minutes at most.
- The pace (WO32b, `ext.pace`): "fast" (the default) fills each field in one go and waits only for the page; "human"
  types in chunks with a person's pauses.
- Why no extension is there (WO32b): it never knocked (no Chrome, or the extension off), it knocked with a token the
  bridge refused, or the port belongs to another process (BridgeError at the start).
- The watch: no extension for 2 minutes inside the window (the lid open) → the Thrift Chrome is started
  (`services.sh start chrome`); still nothing after 5 → one ops line per window.

Python's standard library only (asyncio streams, an RFC 6455 handshake and frames)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from thrift_agent.config import ROOT

HOST, PORT = "127.0.0.1", 8765
EXT_DIR = ROOT / "ext"
EXT_FILES = ("manifest.json", "background.js", "content/common.js", "content/vinted.js", "content/depop.js",
             "selectors.json", "options.html", "options.js")          # = background.js FILES: the reload check
SITE_ORIGINS = ("https://www.vinted.com", "https://www.depop.com")
JOB_TIMEOUT = 180.0          # a job's whole life before its go-ahead (WO32b): 3 minutes
PICKUP_TIMEOUT = 150.0       # no extension took the job (it reconnects within 5 s, polls every 30 s): nothing opened
SEEN_FRESH = 90.0            # an HTTP poll this recent counts as connected
START_CHROME_AFTER = 120.0   # the watch: no extension this long inside the window → start the Thrift Chrome
SAY_AFTER = 300.0            # ... still nothing → one ops line
WATCH_EVERY = 15.0           # the watch's tick
MAX_BODY = 40 * 1024 * 1024  # a screenshot with the page's HTML fits easily
DRAIN = 90.0                 # a job called off: the next one waits this long at most for the extension to let it go
RELOAD_HOLD = 2.0            # an extension told of new files reloads at once: no job for its socket meanwhile
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FINAL = ("result", "error")
MISSING_LINE = ("🧩 The Thrift Chrome extension hasn't checked in for 5 minutes, so Depop and Vinted are waiting. On the "
                "Mac: is the second Chrome (the Thrift one) open and not minimized, the extension loaded at "
                "chrome://extensions, its token saved in its options? (bash ~/thrift-agent/deploy/services.sh status)")


class BridgeError(Exception):
    """The bridge can't run (the port is taken: another poster has it) or can't take a job."""


def token_path(s) -> Path:
    raw = s.get("bridge.token_file") or "~/thrift/var/ext_token"
    return Path(os.path.expanduser(str(raw)))


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise BridgeError(f"no extension token at {path} (deploy/mac_setup.sh makes it)") from None
    if len(token) < 16:
        raise BridgeError(f"the extension token at {path} is too short")
    return token


def new_token() -> str:
    return secrets.token_urlsafe(32)


def files_hash(ext_dir: Path = EXT_DIR) -> str:
    """The extension's files as background.js hashes them: the path, a newline, the bytes, a newline, in FILES order."""
    h = hashlib.sha256()
    for name in EXT_FILES:
        h.update(f"{name}\n".encode())
        try:
            h.update((ext_dir / name).read_bytes())
        except OSError:
            pass
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------- one job

@dataclass
class Job:
    id: str
    site: str
    mode: str
    payload: dict
    shot: Path | None = None              # the evidence path: screenshots go next to it
    created: float = field(default_factory=time.monotonic)
    handed: float | None = None           # when the extension took it
    done: bool = False
    dropped: float | None = None          # when the driver gave up on it
    clicked: bool = False                 # the go-ahead was sent: from here on the listing may be live
    cancelled: bool = False               # its cancel was sent (once is enough)
    sock: object | None = None            # the socket it was pushed to: its go-ahead goes there first
    steps: list[dict] = field(default_factory=list)
    shots: list[dict] = field(default_factory=list)
    final: dict | None = None
    commands: list[dict] = field(default_factory=list)    # not yet delivered over HTTP
    listener: object | None = None        # called with each step / progress event as it comes (the CLI's progress)
    _events: asyncio.Queue = field(default_factory=asyncio.Queue)

    def message(self) -> dict:
        return {"job_id": self.id, "site": self.site, "mode": self.mode, **self.payload}

    async def wait(self, kinds: tuple[str, ...], timeout: float) -> dict:
        """The next event of these kinds (steps and screenshots are recorded on the way). TimeoutError past `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            ev = await asyncio.wait_for(self._events.get(), left)
            if ev.get("event") in kinds:
                return ev


class Bridge:
    """The server. `submit()` queues a job and returns it; the driver waits on its events and sends its go-ahead."""

    def __init__(self, token: str, host: str = HOST, port: int = PORT, ext_dir: Path = EXT_DIR,
                 pace: str | float = "fast", ws_enabled: bool = True):
        self.token, self.host, self.port, self.ext_dir = token, host, port, ext_dir
        self.pace = pace                    # "fast" | "human" (a number: human pauses scaled — the tests)
        self.ws_enabled = ws_enabled        # tests turn the socket off to prove the alarm's poll
        self.server: asyncio.AbstractServer | None = None
        self.jobs: dict[str, Job] = {}
        self.queue: deque[Job] = deque()
        self.sockets: set[_Socket] = set()
        self.photos: dict[str, list[Path]] = {}
        self.started = time.monotonic()
        self.last_seen: float | None = None     # the extension's last authenticated contact
        self.chrome_started: float | None = None
        self.said_missing = False
        self.knocks = 0                         # WebSocket hellos and token-carrying requests, accepted or not
        self.refused = 0                        # ... of them with a token that isn't ours
        self.log: list[str] = []
        self.ext_hash = files_hash(ext_dir)     # what the extension was told; a deploy that changes ext/ is re-announced

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> Bridge:
        try:
            self.server = await asyncio.start_server(self._client, self.host, self.port)
        except OSError as e:
            raise BridgeError(f"port {self.host}:{self.port} is taken ({e.strerror or e}): is the poster service "
                              "running? Stop it first: bash deploy/services.sh stop poster") from None
        if not self.port:
            self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        for sock in list(self.sockets):
            sock.close()
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 3)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self.server = None

    async def __aenter__(self) -> Bridge:
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def connected(self) -> bool:
        """An extension is there: its socket is open, or it polled within the last 90 s."""
        if any(s.ready for s in self.sockets):
            return True
        return self.last_seen is not None and time.monotonic() - self.last_seen < SEEN_FRESH

    def why_missing(self, chrome_running: bool | None = None) -> str:
        """Why no extension is connected, in the owner's words: its token refused, the Thrift Chrome not running, or
        the extension not knocking at all (off, not loaded, or its token never saved)."""
        if self.refused:
            return ("the extension knocked with a token the bridge refused — open its options in the Thrift Chrome and "
                    "paste the token again (cat ~/thrift/var/ext_token)")
        if chrome_running is False:
            return "the Thrift Chrome isn't running — start it: bash ~/thrift-agent/deploy/services.sh start chrome"
        if not self.knocks:
            return ("the Thrift Chrome is open but the extension never knocked — is it on at chrome://extensions, and its "
                    "token saved in its options?")
        return "the extension knocked but isn't connected now — it tries again every 5 s"

    # ------------------------------------------------------------ jobs

    def photo_urls(self, item: str, paths: list[str | Path]) -> list[str]:
        self.photos[item] = [Path(p) for p in paths]
        return [f"{self.base}/photos/{item}/{n}.jpg" for n in range(1, len(paths) + 1)]

    def submit(self, site: str, mode: str, payload: dict, shot: Path | None = None) -> Job:
        now = time.monotonic()
        for jid in [j.id for j in self.jobs.values() if j.done and j.final is not None and now - j.created > 3600]:
            del self.jobs[jid]                    # finished an hour ago: nothing will come for it any more
        job = Job(id=f"{site}-{uuid.uuid4().hex[:10]}", site=site, mode=mode,
                  payload={"pace": self.pace, **payload}, shot=shot)
        self.jobs[job.id] = job
        self.queue.append(job)
        self._push()
        return job

    def command(self, job: Job, kind: str) -> None:
        """The go-ahead for a filled form ("submit": the one publish click) or its end ("cancel")."""
        if kind == "submit":
            job.clicked = True
        elif kind == "cancel":
            if job.cancelled:
                return
            job.cancelled = True
        msg = {"type": kind, "job_id": job.id}
        job.commands.append(msg)
        for sock in sorted(self.sockets, key=lambda x: x is not job.sock):      # the job's own socket first
            if sock.ready and sock.send(msg):
                job.commands.remove(msg)
                break

    def drop(self, job: Job) -> None:
        """A job given up on (timed out, or never taken): its later events are ignored. One the extension has in hand
        is called off there (cancel), and the next job waits for its end — at most DRAIN seconds."""
        if job.dropped is not None:
            return
        job.done, job.dropped = True, time.monotonic()
        try:
            self.queue.remove(job)
        except ValueError:
            pass
        if job.handed is not None and job.final is None:
            self.command(job, "cancel")
        self._push()

    async def settle(self, job: Job, timeout: float = 5.0) -> bool:
        """After a job was called off: wait (at most `timeout`) for the extension's last word on it — its tab closed —
        so a CLI that ends right after doesn't cut the cancel off. True when it came."""
        end = time.monotonic() + timeout
        while job.final is None and time.monotonic() < end and job.handed is not None:
            await asyncio.sleep(0.1)
        return job.final is not None

    def _busy(self) -> bool:
        now = time.monotonic()
        return any(j.handed is not None and j.final is None and (j.dropped is None or now - j.dropped < DRAIN)
                   for j in self.jobs.values())

    def _next(self) -> Job | None:
        """The oldest waiting job, if the extension has none in hand (it runs one at a time, whatever the site)."""
        if self._busy():
            return None
        while self.queue:
            job = self.queue.popleft()
            if not job.done:
                job.handed = time.monotonic()
                return job
        return None

    def _hold(self, sock: _Socket) -> None:
        """This socket's extension is about to reload (its files changed: a deploy): no job is handed to it for
        RELOAD_HOLD seconds — the reload would lose it; one that stays connected gets the jobs after that."""
        sock.hold_until = time.monotonic() + RELOAD_HOLD
        try:
            asyncio.get_running_loop().call_later(RELOAD_HOLD + 0.05, self._push)
        except RuntimeError:
            pass                                    # no loop (a test calling it bare): the next event pushes

    def _push(self) -> None:
        now = time.monotonic()
        for sock in self.sockets:
            if not sock.ready or sock.hold_until > now:
                continue
            job = self._next()
            if job is None:
                return
            if sock.send({"type": "job", "job": job.message()}):
                job.sock = sock
            else:
                job.handed = None
                self.queue.appendleft(job)

    def _event(self, ev: dict) -> list[dict]:
        """One event from the extension: recorded on its job and queued for the driver. Returns the job's commands
        not yet delivered (the HTTP path's answer). An event for a job given up on, or a second last word, is ignored."""
        job = self.jobs.get(str(ev.get("job_id") or ""))
        if job is None:
            return []
        kind = ev.get("event")
        if job.dropped is not None:              # given up on: only its end matters (the extension is free again)
            if kind in FINAL and job.final is None:
                job.final = ev
                self._push()
            out, job.commands = job.commands, []
            return out
        if kind == "screenshot":
            self._keep_shot(job, ev)
        elif kind == "step":
            job.steps.append({k: ev.get(k) for k in ("name", "ok", "detail", "clicked", "ms")})
        if kind in ("step", "progress") and job.listener is not None:
            try:
                job.listener(ev)
            except Exception as e:  # noqa: BLE001 — a listener never breaks the job
                self.log.append(f"listener: {type(e).__name__}: {e}")
        if kind in FINAL:
            if job.final is not None:
                return []
            job.final, job.done = ev, True
        if kind != "waiting":
            job._events.put_nowait(ev)
        out, job.commands = job.commands, []
        if kind in FINAL:
            self._push()
        return out

    def _keep_shot(self, job: Job, ev: dict) -> None:
        label = "".join(c for c in str(ev.get("label") or "page") if c.isalnum() or c in "-_")[:40] or "page"
        record = {"label": label, "url": ev.get("url"), "error": ev.get("error"), "png": None, "html": None}
        if job.shot is not None:      # the filled form is the job's own evidence path; the others are named after it
            base = job.shot if label == "form" else job.shot.with_name(f"{job.shot.stem}-{label}")
            try:
                if ev.get("png_b64"):
                    path = base.with_suffix(".png")
                    path.write_bytes(base64.b64decode(ev["png_b64"]))
                    record["png"] = str(path)
                if ev.get("html"):
                    path = base.with_suffix(".html")
                    path.write_text(str(ev["html"]), encoding="utf-8")
                    record["html"] = str(path)
            except (OSError, ValueError) as e:
                record["error"] = f"{record['error'] or ''} keep: {type(e).__name__}: {e}".strip()
        job.shots.append(record)

    # ------------------------------------------------------------ the watch

    def watch_tick(self, now: float, in_window: bool, start_chrome, say) -> str | None:
        """No extension for 2 minutes inside the window: start the Thrift Chrome; after 5: one ops line (`say`; the
        caller keeps it to once a window). Returns what it did."""
        if self.connected():
            self.chrome_started, self.said_missing = None, False
            return None
        if not in_window:
            return None
        missing = now - (self.last_seen or self.started)
        if missing >= START_CHROME_AFTER and self.chrome_started is None:
            self.chrome_started = now
            start_chrome()
            return "chrome started"
        if missing >= SAY_AFTER and not self.said_missing:
            self.said_missing = True
            say(MISSING_LINE)
            return "said"
        return None

    def announce_files(self) -> bool:
        """ext/ changed on disk (a deploy pulled new extension code): the connected extension is told the new hash, and
        reloads itself between jobs (background.js maybeReload) — no poster restart needed. True when it was told."""
        h = files_hash(self.ext_dir)
        if h == self.ext_hash:
            return False
        self.ext_hash = h
        for sock in self.sockets:
            if sock.ready:
                sock.send({"type": "welcome", "ext_hash": h})
                self._hold(sock)
        return True

    async def watch(self, in_window, start_chrome, say, state=None, every: float | None = None) -> None:
        """Every WATCH_EVERY seconds: watch_tick(), the extension's files (announce_files), and `state(connected)` for
        thrift status and the daily window."""
        while True:
            await asyncio.sleep(WATCH_EVERY if every is None else every)
            try:
                if state is not None:
                    state(self.connected())
                self.announce_files()
                self.watch_tick(time.monotonic(), bool(in_window()), start_chrome, say)
            except Exception as e:  # noqa: BLE001 — the watch never stops the poster
                self.log.append(f"watch: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ the server

    def _authorized(self, headers: dict) -> bool:
        given = headers.get("x-thrift-token", "")
        return bool(given) and secrets.compare_digest(given, self.token)

    def _host_ok(self, headers: dict) -> bool:
        host = headers.get("host", "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(_read_request(reader), 20)
        except (TimeoutError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            writer.close()
            return
        if request is None:
            writer.close()
            return
        method, path, headers, body = request
        try:
            if path == "/ext" and headers.get("upgrade", "").lower() == "websocket":
                await self._websocket(reader, writer, headers)
                return
            status, out_headers, payload = self._http(method, path, headers, body)
        except Exception as e:  # noqa: BLE001 — a bad request never takes the poster down
            status, out_headers, payload = 500, {}, json.dumps({"error": type(e).__name__}).encode()
        try:
            writer.write(_response(status, out_headers, payload))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def _cors(self, headers: dict) -> dict:
        origin = headers.get("origin", "")
        if origin in SITE_ORIGINS:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Headers": "X-Thrift-Token, Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Private-Network": "true"}
        return {"Vary": "Origin"}

    def _http(self, method: str, raw_path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        cors = self._cors(headers)
        path = urlparse(raw_path).path
        if not self._host_ok(headers):
            return 403, {}, b'{"error": "host"}'
        if method == "OPTIONS":
            return 204, cors, b""
        if path == "/health":
            return 200, cors, b'{"ok": true}'
        if headers.get("x-thrift-token"):
            self.knocks += 1
        if not self._authorized(headers):
            if headers.get("x-thrift-token"):
                self.refused += 1
            return 401, cors, b'{"error": "token"}'
        self.last_seen, self.refused = time.monotonic(), 0
        if method == "GET" and path == "/jobs/next":
            job = self._next()
            return (204, cors, b"") if job is None else (200, cors, json.dumps(job.message()).encode())
        if method == "POST" and path == "/events":
            try:
                ev = json.loads(body or b"{}")
            except ValueError:
                return 400, cors, b'{"error": "json"}'
            return 200, cors, json.dumps({"commands": self._event(ev if isinstance(ev, dict) else {})}).encode()
        if method == "GET" and path.startswith("/photos/"):
            parts = [unquote(p) for p in path.split("/")[2:]]
            if len(parts) == 2 and parts[1].endswith(".jpg") and parts[1][:-4].isdigit():
                files = self.photos.get(parts[0]) or []
                n = int(parts[1][:-4])
                if 1 <= n <= len(files) and files[n - 1].is_file():
                    photo = files[n - 1]
                    kind = "image/png" if photo.suffix.lower() == ".png" else "image/jpeg"
                    return 200, {**cors, "Content-Type": kind, "Cache-Control": "no-store"}, photo.read_bytes()
            return 404, cors, b'{"error": "photo"}'
        return 404, cors, b'{"error": "path"}'

    async def _websocket(self, reader, writer, headers: dict) -> None:
        origin = headers.get("origin", "")
        key = headers.get("sec-websocket-key", "")
        if not self.ws_enabled or not self._host_ok(headers) or not origin.startswith("chrome-extension://") or not key:
            writer.write(_response(403, {}, b""))
            await writer.drain()
            writer.close()
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        sock = _Socket(reader, writer)
        try:
            hello = await asyncio.wait_for(sock.receive(), 10)
            self.knocks += 1
            if not isinstance(hello, dict) or hello.get("type") != "hello" or \
                    not secrets.compare_digest(str(hello.get("token") or ""), self.token):
                self.refused += 1
                sock.send({"type": "refused"})
                await sock.flush()
                return
            sock.ready = True
            self.refused = 0                        # the right token now
            self.sockets.add(sock)
            self.last_seen = time.monotonic()
            sock.send({"type": "welcome", "ext_hash": self.ext_hash})
            if hello.get("loaded") != self.ext_hash:    # it loaded other files than ours: it reloads now (a deploy)
                self._hold(sock)
            for job in self.jobs.values():          # a go-ahead its old socket never delivered (it reconnected)
                if job.handed is not None and not job.done and job.commands:
                    for cmd in job.commands:
                        sock.send(cmd)
                    job.commands, job.sock = [], sock
            self._push()
            while True:
                msg = await sock.receive()
                if msg is None:
                    return
                self.last_seen = time.monotonic()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    sock.send({"type": "pong"})
                elif msg.get("type") == "event":
                    for cmd in self._event({k: v for k, v in msg.items() if k != "type"}):
                        sock.send(cmd)
        except (TimeoutError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            return
        finally:
            sock.ready = False
            self.sockets.discard(sock)
            sock.close()


# ---------------------------------------------------------------- HTTP and WebSocket plumbing

async def _read_request(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > 16384:
        raise ValueError("headers too long")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        raise ValueError("request line")
    method, path = parts[0].upper(), parts[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length") or 0)
    if length > MAX_BODY:
        raise ValueError("body too large")
    body = await reader.readexactly(length) if length else b""
    return method, path, headers, body


_REASONS = {101: "Switching Protocols", 200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}


def _response(status: int, headers: dict, body: bytes) -> bytes:
    head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}", "Connection: close", f"Content-Length: {len(body)}"]
    if body and "Content-Type" not in headers:
        headers = {**headers, "Content-Type": "application/json"}
    head += [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


class _Socket:
    """One WebSocket (RFC 6455): text frames of JSON, the client's frames masked, fragments joined, ping answered."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.ready = False
        self.closed = False
        self.hold_until = 0.0                       # no job before this (its extension is reloading)

    def send(self, obj) -> bool:
        if self.closed:
            return False
        try:
            self.writer.write(_frame(json.dumps(obj).encode(), 0x1))
            return True
        except (ConnectionError, RuntimeError):
            self.closed = True
            return False

    async def flush(self) -> None:
        try:
            await self.writer.drain()
        except ConnectionError:
            self.closed = True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.writer.write(_frame(b"", 0x8))
            except (ConnectionError, RuntimeError):
                pass
        try:
            self.writer.close()
        except RuntimeError:
            pass

    async def receive(self):
        """The next message as JSON (None when the socket closes)."""
        data, kind = b"", None
        while True:
            b1, b2 = await self.reader.readexactly(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, n = b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
            if n > MAX_BODY:
                raise ValueError("frame too large")
            mask = await self.reader.readexactly(4) if masked else b""
            payload = await self.reader.readexactly(n)
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload)) if n < 4096 else _unmask(payload, mask)
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.writer.write(_frame(payload, 0xA))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, data = opcode, payload
            elif opcode == 0x0:
                data += payload
            if fin and kind is not None:
                await self.flush()
                try:
                    return json.loads(data.decode("utf-8"))
                except ValueError:
                    return {}


def _unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big")


def _frame(payload: bytes, opcode: int) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    return head + payload
