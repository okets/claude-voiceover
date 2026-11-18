# tts/engine_connection.py
"""One caller's connection to the shared Kokoro engine. Stdlib only.

Runs on the server's event loop only. It owns the socket I/O, line framing,
the slow-reader watchdog and op dispatch; request state lives in EngineServer.
"""

import asyncio
import hmac
import sys
import time
import traceback
from itertools import count

from engine_protocol import (AUDIO, BadLine, LIMITS, LineReader, PROTOCOL,
                             audio_header, encode_line)

READ_SIZE = 65536
WATCH_INTERVAL_S = 0.1
_connection_ids = count(1)


def _is_protocol(value):
    return value == PROTOCOL and not isinstance(value, bool)


class Connection:
    def __init__(self, server, reader, writer, out_buffer_bytes, slow_reader_s,
                 token=None, needs_auth=False, auth_timeout_s=5.0):
        self.id = next(_connection_ids)
        self.requests = {}      # request id -> Request, in start order
        self._server = server
        self._reader = reader
        self._writer = writer
        self._lines = LineReader()
        self._out_limit = out_buffer_bytes
        self._slow_reader_s = slow_reader_s
        self._slow_timer = None
        self._over_since = None  # when the output buffer last went over its limit
        self._token = token
        self._auth_deadline = time.monotonic() + auth_timeout_s
        self._needs_auth = needs_auth   # TCP callers must send a valid auth line first
        self._said_hello = False
        self._closing = False
        self._ops = {
            "hello": self._op_hello, "auth": self._op_ignored, "status": self._op_status,
            "stop": self._op_stop, "start": self._op_start, "text": self._op_text,
            "flush": self._op_flush, "end": self._op_end, "cancel": self._op_cancel,
        }

    @staticmethod
    def busy_line():
        return encode_line({"ev": "error", "code": "busy", "message": "too many connections"})

    # --- lifecycle ----------------------------------------------------------

    async def run(self):
        try:
            await self._read_loop()
        except OSError:
            pass  # reset by peer, or dropped by the slow-reader watchdog
        except Exception:
            traceback.print_exc(file=sys.stderr)  # never let one caller take the engine down
        finally:
            self._server.drop_connection(self)
            self._release_transport()

    async def _read_loop(self):
        while not self._closing:
            data = await self._read()
            if not data:
                return
            for item in self._lines.feed(data):
                self._handle(item)
                if self._closing:
                    return

    async def _read(self):
        """One read; until the caller has authenticated it must arrive before the auth deadline.
        Returns b"" (closing quietly) when that deadline passes."""
        if not self._needs_auth:
            return await self._reader.read(READ_SIZE)
        try:
            return await asyncio.wait_for(self._reader.read(READ_SIZE),
                                          max(0.0, self._auth_deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return b""

    def close(self):
        """Flush what is queued, then close."""
        self._closing = True
        self._writer.close()

    def _release_transport(self):
        if self._slow_timer is not None:
            self._slow_timer.cancel()
        self._writer.close()

    async def wait_closed(self):
        try:
            await self._writer.wait_closed()
        except OSError:
            pass

    # --- writing: never blocks, never awaits ----------------------------------

    def write(self, data):
        transport = self._writer.transport
        if transport.is_closing():
            return
        transport.write(data)
        self._watch_buffer(transport)

    def buffered_bytes(self):
        return self._writer.transport.get_write_buffer_size()

    def _watch_buffer(self, transport):
        """Over the limit: stop rendering for this caller, and time how long it stays over."""
        if self._over_since is None and self.buffered_bytes() > self._out_limit:
            self._over_since = time.monotonic()
            self._server.set_blocked(self.id, True)
            self._schedule_tick()

    def _schedule_tick(self):
        interval = min(WATCH_INTERVAL_S, self._slow_reader_s / 10)
        self._slow_timer = asyncio.get_running_loop().call_later(interval, self._tick)

    def _tick(self):
        self._slow_timer = None
        transport = self._writer.transport
        if transport.is_closing():
            return
        if self.buffered_bytes() <= self._out_limit:
            self._over_since = None  # drained: the stall period is over
            self._server.set_blocked(self.id, False)
        elif time.monotonic() - self._over_since >= self._slow_reader_s:
            transport.abort()
        else:
            self._schedule_tick()

    def send(self, obj):
        self.write(encode_line(obj))

    def send_error(self, code, message, req_id=None):
        line = {"ev": "error", "code": code, "message": message}
        if req_id is not None:
            line["id"] = req_id
        self.send(line)

    def send_end(self, req_id, reason, samples):
        self.send({"ev": "end", "id": req_id, "reason": reason, "samples": samples})

    def send_audio(self, req_id, seq, frame):
        self.write(audio_header(req_id, seq, len(frame)) + frame)

    # --- dispatch --------------------------------------------------------------

    def _handle(self, item):
        if self._needs_auth and not self._authenticate(item):
            return
        if isinstance(item, BadLine):
            self.send_error(item.code, item.message)
            return
        op = item.get("op")
        handler = self._ops.get(op) if isinstance(op, str) else None
        if handler is None:
            self.send_error("bad_request", "unknown op")
        elif not self._said_hello and handler not in (self._op_hello, self._op_ignored):
            self.send_error("bad_request", "hello comes first")
        else:
            handler(item)

    def _authenticate(self, item):
        """The first line on a TCP connection must carry the token; else refuse and close."""
        if isinstance(item, BadLine) or item.get("op") != "auth" or not self._token_matches(item.get("token")):
            self.send_error("unauthorized", "a valid auth token is required")
            self.close()
            return False
        self._needs_auth = False
        return True  # the auth line itself is then handled (and ignored) like any other

    def _token_matches(self, given):
        if not isinstance(self._token, str) or not isinstance(given, str):
            return False
        return hmac.compare_digest(given.encode(), self._token.encode())

    def _op_ignored(self, msg):
        pass

    def _op_hello(self, msg):
        proto = msg.get("proto")
        if proto is None:
            self.send_error("bad_request", "hello needs proto")
        elif not _is_protocol(proto):
            self.send_error("unsupported_protocol", "this engine speaks protocol %d" % PROTOCOL)
            self.close()
        else:
            self._said_hello = True
            snapshot = self._server.snapshot()
            self.send({"ev": "hello", "proto": PROTOCOL, "plugin": snapshot["plugin"], "audio": AUDIO,
                       "limits": LIMITS, "loaded": snapshot["loaded"], "device": snapshot["device"]})

    def _op_status(self, msg):
        self.send(dict(self._server.snapshot(), ev="status"))

    def _op_stop(self, msg):
        self._server.stop()

    def _op_start(self, msg):
        self._server.open_request(self, msg)

    def _op_text(self, msg):
        self._server.add_text(self, msg)

    def _op_flush(self, msg):
        self._server.flush_request(self, msg)

    def _op_end(self, msg):
        self._server.end_request(self, msg)

    def _op_cancel(self, msg):
        self._server.cancel_requests(self, msg.get("id"))
