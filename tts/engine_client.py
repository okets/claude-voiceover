# tts/engine_client.py
"""Stdlib client for the shared Kokoro engine (protocol 1).

EngineConnection owns one socket. A reader thread routes audio/end/error events
to the Pending they belong to by id; events without an id (hello, status,
released, id-less errors) go to a reply queue that the synchronous calls wait on;
id-bearing warm replies and ref-bearing errors are dropped (warm is fire and forget). Sync calls (status, release) hold one lock across send and reply and drain stale
replies first, so concurrent or late replies never cross. Sends are serialised by
a lock, so any thread may call any method.
"""

import fcntl
import itertools
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import engine_paths
from engine_protocol import PROTOCOL, encode_line

REPLY_TIMEOUT_S = 5.0
CONNECT_POLL_S = 0.05
_READ_SIZE = 65536
_STUB_ENV = "VOICEOVER_ENGINE_STUB"
_CLOSED = object()      # reply-queue marker: the socket is gone


class EngineError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__("%s: %s" % (code, message) if message else code)
        self.code = code
        self.message = message


class Pending:
    """One submitted utterance. chunks yields PCM pieces, then None."""

    def __init__(self, req_id):
        self.id = req_id
        self.done = threading.Event()
        self.chunks = queue.Queue()
        self.reason = None
        self.error = None
        self._pcm = []

    def pcm(self) -> bytes:
        """All audio received; blocks until the request is done."""
        self.done.wait()
        return b"".join(self._pcm)

    def _audio(self, payload):
        self._pcm.append(payload)
        self.chunks.put(payload)

    def _finish(self, reason=None, error=None):
        if reason is not None and self.reason is None:
            self.reason = reason
        if error is not None and self.error is None:
            self.error = error
        if not self.done.is_set():
            self.done.set()
            self.chunks.put(None)


class _Stream:
    """Buffered reads of lines and exact-length payloads from a socket."""

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""

    def _fill(self):
        data = self._sock.recv(_READ_SIZE)
        if not data:
            raise EOFError("closed")
        self._buf += data

    def line(self):
        while b"\n" not in self._buf:
            self._fill()
        raw, _, self._buf = self._buf.partition(b"\n")
        return raw

    def take(self, count):
        while len(self._buf) < count:
            self._fill()
        data, self._buf = self._buf[:count], self._buf[count:]
        return data


class EngineConnection:
    def __init__(self, sock):
        self._sock = sock
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()       # guards _pending and alive
        self._pending = {}
        self._replies = queue.Queue()
        self._sync_lock = threading.Lock()   # one sync call at a time: send + reply
        self._ids = itertools.count(1)
        self._warm_ids = itertools.count(1)
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, name="engine-client", daemon=True)

    @classmethod
    def connect(cls, path=None, timeout: float = 2.0):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path or engine_paths.socket_path()))
            sock.settimeout(None)
        except BaseException:
            sock.close()
            raise
        conn = cls(sock)
        conn._reader.start()
        try:
            conn._handshake()
        except BaseException:
            conn.close()
            raise
        return conn

    def _handshake(self):
        self._send({"op": "hello", "proto": PROTOCOL})
        reply = self._reply({"hello"})
        if reply.get("proto") != PROTOCOL:
            raise EngineError("unsupported_protocol", "engine speaks %r" % reply.get("proto"))

    # --- reader thread --------------------------------------------------------

    def _read_loop(self):
        stream = _Stream(self._sock)
        try:
            while True:
                event = json.loads(stream.line().decode("utf-8"))
                payload = stream.take(event["bytes"]) if event.get("ev") == "audio" else b""
                self._route(event, payload)
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            pass
        finally:
            self._disconnected()

    def _route(self, event, payload):
        name, req_id = event.get("ev"), event.get("id")
        if "ref" in event:
            return      # an error that ends nothing (misuse, warm): not a reply, not an end
        with self._lock:
            pending = self._pending.get(req_id) if req_id is not None else None
            if pending is not None and name in ("end", "error"):
                del self._pending[req_id]
        if pending is not None:
            self._deliver(pending, name, event, payload)
        elif req_id is None:
            self._replies.put(event)
        # else: an id-bearing event nobody tracks (a warm reply): dropped

    @staticmethod
    def _deliver(pending, name, event, payload):
        if name == "audio":
            pending._audio(payload)
        elif name == "end":
            pending._finish(reason=event.get("reason"))
        elif name == "error":
            pending._finish(reason="error", error=EngineError(event.get("code", "error"),
                                                              event.get("message", "")))

    def _disconnected(self):
        with self._lock:
            self.alive = False
            orphans, self._pending = list(self._pending.values()), {}
        for pending in orphans:
            pending._finish(reason="error", error=EngineError("disconnected"))
        self._replies.put(_CLOSED)

    # --- sending ---------------------------------------------------------------

    def _send(self, obj):
        try:
            with self._send_lock:
                self._sock.sendall(encode_line(obj))
        except OSError:
            raise EngineError("disconnected") from None

    def _reply(self, expected):
        """The next id-less event named in `expected`; an error event raises."""
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while True:
            try:
                event = self._replies.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise EngineError("timeout") from None
            if event is _CLOSED:
                self._replies.put(_CLOSED)   # keep waking any other waiter
                raise EngineError("disconnected")
            if event.get("ev") in expected:
                return event
            if event.get("ev") == "error":
                raise EngineError(event.get("code", "error"), event.get("message", ""))

    def _drain_replies(self):
        """Drop replies left over from earlier calls (late, or from fire-and-forget ops)."""
        while True:
            try:
                event = self._replies.get_nowait()
            except queue.Empty:
                return
            if event is _CLOSED:
                self._replies.put(_CLOSED)
                return

    def _call(self, op, expected):
        with self._sync_lock:
            self._drain_replies()
            self._send(op)
            return self._reply(expected)

    # --- public ops --------------------------------------------------------------

    def submit(self, text: str, voice: str, speed=None, live: bool = False) -> Pending:
        req_id = "c%d" % next(self._ids)
        pending = Pending(req_id)
        with self._lock:
            if not self.alive:
                raise EngineError("disconnected")
            self._pending[req_id] = pending
        msg = {"op": "start", "id": req_id, "voice": voice, "text": text, "end": True}
        if speed is not None:
            msg["speed"] = speed
        if live:
            msg["live"] = True
        try:
            self._send(msg)
        except EngineError:
            with self._lock:
                self._pending.pop(req_id, None)
            raise
        return pending

    def cancel(self, p: Pending) -> None:
        self._send({"op": "cancel", "id": p.id})

    def warm(self, hold_s: float = 0) -> None:
        """Fire and forget: the engine echoes this id, and the router drops the reply."""
        self._send({"op": "warm", "id": "w%d" % next(self._warm_ids), "hold_s": hold_s})

    def status(self) -> dict:
        return self._call({"op": "status"}, {"status"})

    def release(self, keep_off_s: float = 0) -> dict:
        return self._call({"op": "release", "keep_off_s": keep_off_s}, {"released"})

    def stop(self) -> None:
        self._send({"op": "stop"})

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


# --- starting the engine on demand ------------------------------------------------

def _try_connect():
    """A live connection, or None when no engine answers."""
    try:
        return EngineConnection.connect()
    except (OSError, EngineError):
        return None


def _reachable():
    conn = _try_connect()
    if conn is None:
        return False
    conn.close()
    return True


def _serve_command(plugin_root: Path):
    launcher = Path(plugin_root) / "tts" / "engine_launcher.py"
    return [sys.executable, str(launcher), "serve", "--plugin-root", str(plugin_root)]


def _spawn(plugin_root: Path):
    """Start a detached engine; the Popen, or None if it could not be started."""
    command = _serve_command(plugin_root)
    if os.environ.get(_STUB_ENV) == "1":
        command.append("--stub")
    try:
        with open(engine_paths.log_path(), "ab") as log:
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, close_fds=True)
    except OSError:
        return None


def _acquire_start_lock(lock, deadline):
    """flock start.lock without blocking past the deadline; True when held."""
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(CONNECT_POLL_S)


def _start_and_wait(plugin_root, deadline):
    """Under start.lock: spawn unless an engine appeared, then poll until it answers."""
    if _reachable():
        return True
    child = _spawn(plugin_root)
    if child is None:
        return False
    while time.monotonic() < deadline:
        if _reachable():
            return True
        if child.poll() not in (None, 0):   # it failed (exit 0: another engine won the lock, keep polling)
            return _reachable()
        time.sleep(CONNECT_POLL_S)
    return False


def ensure(plugin_root: Path, timeout: float = 10.0) -> bool:
    """True once an engine answers; starts one (single-flight under start.lock) if needed.
    Never blocks past `timeout`, and a last probe settles a deadline that passed mid-wait."""
    deadline = time.monotonic() + timeout
    if _reachable():
        return True
    try:
        lock = open(engine_paths.start_lock_path(), "a+")
    except OSError:
        return False
    with lock:
        if _acquire_start_lock(lock, deadline) and _start_and_wait(plugin_root, deadline):
            return True
    return _reachable()
