# tests/_engine_test_util.py
"""Shared helpers for the engine server tests: a raw line client and server runner."""

import json
import socket
import threading

from engine_server import EngineServer


class Event(dict):
    """A server line; audio events carry their PCM bytes in .payload."""
    payload = b""

    @property
    def ev(self):
        return self.get("ev")


class Raw:
    """A blocking test client speaking raw protocol lines."""

    def __init__(self, path, timeout=5.0, tcp=False):
        """`path` is a Unix socket path, or with tcp=True a ("127.0.0.1", port) or ("::1", port) address."""
        family = (socket.AF_INET6 if ":" in path[0] else socket.AF_INET) if tcp else socket.AF_UNIX
        self.sock = socket.socket(family)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def hello(self):
        self.send({"op": "hello", "proto": 1})
        return self.read_event()

    def _take(self, n=None):
        while (n is None and b"\n" not in self.buf) or (n is not None and len(self.buf) < n):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("closed")
            self.buf += chunk
        if n is None:
            line, _, self.buf = self.buf.partition(b"\n")
            return line
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_event(self):
        event = Event(json.loads(self._take()))
        if event.ev == "audio":
            event.payload = self._take(event["bytes"])
        return event

    def read_until(self, name, req_id=None):
        events = []
        while True:
            event = self.read_event()
            events.append(event)
            if event.ev == name and (req_id is None or event.get("id") == req_id):
                return events

    def read_until_end(self, req_id):
        return self.read_until("end", req_id)

    def read_error(self):
        return self.read_until("error")[-1]

    def closed_by_peer(self):
        try:
            while self.sock.recv(65536):
                pass
            return True
        except (socket.timeout, OSError):
            return False

    def close_abruptly(self):
        self.sock.close()


def serve(synth, listeners, **kwargs):
    """Run an EngineServer on `listeners` in a thread; returns the server."""
    settings = dict(idle_unload_s=60.0, token=None, plugin_version="9.9.9", plugin_root="/x")
    settings.update(kwargs)
    server = EngineServer(synth, **settings)
    result = []
    thread = threading.Thread(target=lambda: result.append(server.serve_forever(listeners)), daemon=True)
    thread.start()
    server.result, server.thread = result, thread
    return server


def stop_server(server, reason="stop"):
    server.request_exit(reason)
    server.thread.join(5)
    return server.result[0] if server.result else None


_ids = iter(range(10000))


def start_line(**fields):
    line = {"op": "start", "id": "r%d" % next(_ids), "voice": "bf_emma"}
    line.update(fields)
    return line


