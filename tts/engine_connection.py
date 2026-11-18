# tts/engine_connection.py
"""One caller's connection to the shared Kokoro engine. Stdlib only.

Runs on the server's event loop only. It owns the socket I/O, line framing,
the slow-reader watchdog and op dispatch; request state lives in EngineServer.
"""

import asyncio
import sys
import traceback
from itertools import count

from engine_protocol import (AUDIO, BadLine, LIMITS, LineReader, PROTOCOL,
                             audio_header, encode_line)

READ_SIZE = 65536
_connection_ids = count(1)


def _is_protocol(value):
    return value == PROTOCOL and not isinstance(value, bool)


class Connection:
    def __init__(self, server, reader, writer, out_buffer_bytes, slow_reader_s):
        self.id = next(_connection_ids)
        self.requests = {}      # request id -> Request, in start order
        self._server = server
        self._reader = reader
        self._writer = writer
        self._lines = LineReader()
        self._out_limit = out_buffer_bytes
        self._slow_reader_s = slow_reader_s
        self._slow_timer = None
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
            data = await self._reader.read(READ_SIZE)
            if not data:
                return
            for item in self._lines.feed(data):
                self._handle(item)
                if self._closing:
                    return

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

    def _watch_buffer(self, transport):
        if self._slow_timer is None and transport.get_write_buffer_size() > self._out_limit:
            loop = asyncio.get_running_loop()
            self._slow_timer = loop.call_later(self._slow_reader_s, self._drop_if_still_stalled)

    def _drop_if_still_stalled(self):
        self._slow_timer = None
        transport = self._writer.transport
        if not transport.is_closing() and transport.get_write_buffer_size() > self._out_limit:
            transport.abort()

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
