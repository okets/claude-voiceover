# tests/test_engine_server.py
"""Engine server, protocol 1 over a Unix socket, with the stub synthesizer."""

import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_protocol import AUDIO, LIMITS
from engine_server import EngineServer, NoModel
from engine_stub import SineSynth

SOCK_DIR = temp_dir("vo-sock-")
_servers = []


class Event(dict):
    """A server line; audio events carry their PCM bytes in .payload."""
    payload = b""

    @property
    def ev(self):
        return self.get("ev")


class Raw:
    """A blocking test client speaking raw protocol lines."""

    def __init__(self, path, timeout=5.0):
        self.sock = socket.socket(socket.AF_UNIX)
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


def start_server(synth, **kwargs):
    path = str(SOCK_DIR / ("e%d.sock" % len(_servers)))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen(32)
    settings = dict(idle_unload_s=60.0, token=None, plugin_version="9.9.9", plugin_root="/x")
    settings.update(kwargs)
    server = EngineServer(synth, **settings)
    result = []
    thread = threading.Thread(target=lambda: result.append(server.serve_forever([listener])), daemon=True)
    thread.start()
    server.result, server.thread = result, thread
    _servers.append(server)
    return server, path


def stop_server(server, reason="stop"):
    server.request_exit(reason)
    server.thread.join(5)
    return server.result[0] if server.result else None


_ids = iter(range(10000))


def start_line(**fields):
    line = {"op": "start", "id": "r%d" % next(_ids), "voice": "bf_emma"}
    line.update(fields)
    return line


def err_for(c, fields):
    line = start_line(**fields)
    c.send(line)
    code = c.read_error()["code"]
    end = c.read_until_end(line["id"])[-1]
    return code if end["reason"] == "error" and end["samples"] == 0 else "bad end %r" % end


def end_for(c, fields):
    line = start_line(**fields)
    c.send(line)
    end = c.read_until_end(line["id"])[-1]
    return end["reason"], end["samples"]


def fresh_status(path):
    c = Raw(path)
    c.hello()
    c.send({"op": "status"})
    status = c.read_event()
    c.close_abruptly()
    return status


# --- hello, status, nothing loads yet ------------------------------------------------
synth = SineSynth(synth_delay=0.05)
srv, path = start_server(synth)
c = Raw(path)
h = c.hello()
check("hello", h["ev"] == "hello" and h["proto"] == 1 and h["audio"] == AUDIO and h["limits"] == LIMITS
      and h["plugin"] == "9.9.9" and h["loaded"] is False and h["device"] is None, h)
st = fresh_status(path)
check("status does not load", st["ev"] == "status" and st["loaded"] is False and st["device"] is None
      and st["pid"] == os.getpid() and st["proto"] == 1 and st["callers"] == 2 and st["requests"] == 0, st)
check("synth not loaded by hello/status", synth.loaded is False)

# --- whole utterance (T2 shape) ---------------------------------------------------------
c.send({"op": "start", "id": "u2", "voice": "bf_emma", "text": "Hello there.", "end": True})
evs = c.read_until_end("u2")
pcm = b"".join(e.payload for e in evs if e.ev == "audio")
check("bytes == 2*samples", len(pcm) == 2 * evs[-1]["samples"] and evs[-1]["reason"] == "complete"
      and evs[-1]["samples"] == 2880, evs[-1])
check("loaded on first start; hello then reports it", synth.loaded and c.hello()["loaded"] is True)

c.send(start_line(id="big", text=("word " * 39 + "word. ") * 2, end=True))
evs = c.read_until_end("big")
frames = [e for e in evs if e.ev == "audio"]
check("frames are even, capped, seq counts from 0",
      [f["seq"] for f in frames] == list(range(len(frames))) and len(frames) > 2
      and all(f["bytes"] % 2 == 0 and f["bytes"] <= LIMITS["frame"] for f in frames))

# --- fragments + flush (T4 shape) ----------------------------------------------------------
c.send({"op": "start", "id": "u1", "voice": "bf_emma"})
c.send({"op": "text", "id": "u1", "text": "One. Tw"})
check("first sentence audio before rest", c.read_event()["ev"] == "audio")
c.send({"op": "text", "id": "u1", "text": "o"})
c.send({"op": "flush", "id": "u1", "n": 1})
got = c.read_until("flushed")
check("flushed after its audio", [e.ev for e in got] == ["audio", "flushed"] and got[-1]["n"] == 1, got)
c.send({"op": "end", "id": "u1"})
end = c.read_until_end("u1")[-1]
check("fragmented request completes", end["reason"] == "complete" and end["samples"] == 2880, end)
check("start with neither text nor end opens an utterance, text 'ok' alone is spoken",
      (c.send({"op": "start", "id": "ok", "voice": "bf_emma"}), c.send({"op": "text", "id": "ok", "text": "ok"}),
       c.send({"op": "end", "id": "ok"}), c.read_until_end("ok")[-1]["samples"])[-1] == 1440)

# --- cancel timing, then the next request on the same connection (Review Focus 5) --------------
c.send({"op": "start", "id": "long", "voice": "bf_emma", "text": "Word. " * 60, "end": True})
c.read_event()
t = time.time()
c.send({"op": "cancel", "id": "long"})
end = c.read_until_end("long")[-1]
check("cancelled within 0.3 s", end["reason"] == "cancelled" and time.time() - t < 0.3, end)
c.send({"op": "start", "id": "next", "voice": "bf_emma", "text": "Hi.", "end": True})
check("next served, no stray ids", {e["id"] for e in c.read_until_end("next")} == {"next"})

c.send({"op": "start", "id": "a", "voice": "bf_emma", "text": "Word. " * 30})
c.send({"op": "start", "id": "b", "voice": "bf_emma", "text": "Word. " * 30})
c.send({"op": "cancel"})
ends = {c.read_until("end")[-1]["id"] for _ in range(2)}
check("cancel without id cancels everything on the connection", ends == {"a", "b"})

c.send({"op": "start", "id": "dup", "voice": "bf_emma"})
c.send({"op": "start", "id": "dup", "voice": "bf_emma"})
check("active id reuse -> bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "cancel", "id": "dup"})
check("the original dup is still cancellable", c.read_until_end("dup")[-1]["reason"] == "cancelled")

# --- errors -----------------------------------------------------------------------------------------
check("unknown voice", err_for(c, {"voice": "zz_top"}) == "unknown_voice")
check("bad speed", err_for(c, {"speed": 3.0}) == "bad_speed")
check("bad speed, too slow", err_for(c, {"speed": 0.4}) == "bad_speed")
check("bad speed, not a number", err_for(c, {"speed": "fast"}) == "bad_speed")
check("6001 chars", err_for(c, {"text": "a" * 6001, "end": True}) == "text_too_long")
check("whitespace-only completes with 0 samples", end_for(c, {"text": "   ", "end": True}) == ("complete", 0))
check("speed 2.0 accepted", end_for(c, {"speed": 2.0, "text": "Hi.", "end": True}) == ("complete", 1440))
before = synth.synth_calls
end_for(c, {"text": " \n ", "end": True})
check("whitespace-only makes no synthesis call", synth.synth_calls == before)
c.send({"op": "frobnicate"})
check("unknown op -> bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "text", "id": "nobody", "text": "x"})
check("text for an unknown id -> bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "start", "voice": "bf_emma"})
check("start without id -> bad_request", c.read_error()["code"] == "bad_request")
c.sock.sendall(b"{not json\n")
check("invalid JSON -> bad_request, connection survives", c.read_error()["code"] == "bad_request"
      and end_for(c, {"text": "Hi.", "end": True, "future_field": [1]}) == ("complete", 1440))

# --- a caller vanishing mid-stream (Review Focus 3) ----------------------------------------------------------
d = Raw(path)
d.hello()
d.send({"op": "start", "id": "x", "voice": "bf_emma", "text": "Word. " * 40, "end": True})
d.read_event()
d.close_abruptly()
t = time.time()
c.send({"op": "start", "id": "after", "voice": "bf_emma", "text": "Still here.", "end": True})
check("others unaffected by a broken pipe, and the dropped caller's queue is gone",
      c.read_until_end("after")[-1]["reason"] == "complete" and time.time() - t < 1.0)
check("a vanished caller is no longer counted", fresh_status(path)["callers"] == 2)  # c + this probe
check("server exits with 'stop' on request", stop_server(srv, "stop") == "stop")

# --- hello rules ------------------------------------------------------------------------------------
srv, path = start_server(SineSynth())
c = Raw(path)
c.send({"op": "status"})
check("an op before hello -> bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "hello", "proto": 99})
check("unsupported protocol -> error then close", c.read_error()["code"] == "unsupported_protocol"
      and c.closed_by_peer())
stop_server(srv)

# --- failures: no model, synthesis error ---------------------------------------------------------------
class NoModelSynth(SineSynth):
    def load(self):
        raise NoModel("no model files")


srv, path = start_server(NoModelSynth())
c = Raw(path)
c.hello()
check("NoModel -> no_model", err_for(c, {"text": "Hi.", "end": True}) == "no_model")
check("and again (retries the load)", err_for(c, {"text": "Hi.", "end": True}) == "no_model")
stop_server(srv)


class BoomSynth(SineSynth):
    def synthesize(self, text, voice, speed, lang):
        if "boom" in text:
            raise RuntimeError("kaboom")
        return super().synthesize(text, voice, speed, lang)


srv, path = start_server(BoomSynth())
c = Raw(path)
c.hello()
real_stderr, sys.stderr = sys.stderr, io.StringIO()  # the engine logs the traceback; keep the test output clean
failed = err_for(c, {"text": "boom.", "end": True})
logged, sys.stderr = sys.stderr.getvalue(), real_stderr
check("synthesis error -> synthesis_failed, logged", failed == "synthesis_failed" and "kaboom" in logged)
check("engine survives a synthesis error", end_for(c, {"text": "Fine.", "end": True}) == ("complete", 1440))
stop_server(srv)

# --- the loop stays responsive while the model loads --------------------------------------------------------
srv, path = start_server(SineSynth(load_delay=0.6))
c = Raw(path)
c.hello()
c.send(start_line(id="slowload", text="Hi.", end=True))
t = time.time()
status = fresh_status(path)
check("status answers while the model is loading", status["loaded"] is False and time.time() - t < 0.4)
check("request completes after the load", c.read_until_end("slowload")[-1]["reason"] == "complete")
c.send(start_line(id="gone", text="Hi.", end=True))
c.send({"op": "cancel", "id": "gone"})
check("cancel before activation ends the request once", c.read_until_end("gone")[-1]["reason"] == "cancelled")
stop_server(srv)

# --- limits ---------------------------------------------------------------------------------------------------
srv, path = start_server(SineSynth(synth_delay=0.1), max_connections=2, max_requests=2)
a, b = Raw(path), Raw(path)
a.hello(); b.hello()
third = Raw(path)
check("too many connections -> busy, then close", third.read_error()["code"] == "busy" and third.closed_by_peer())
a.send(start_line(id="q1", text="Word. " * 10))
a.send(start_line(id="q2", text="Word. " * 10))
b.send(start_line(id="q3", text="Hi.", end=True))
busy = b.read_error()
check("too many requests -> busy for that request, which then ends in error",
      busy["code"] == "busy" and busy["id"] == "q3" and b.read_until_end("q3")[-1]["reason"] == "error")
a.send({"op": "cancel"})
a.read_until_end("q1"); a.read_until_end("q2")
b.send(start_line(id="q4", text="Hi.", end=True))
check("capacity returns after cancels", b.read_until_end("q4")[-1]["reason"] == "complete")
stop_server(srv)

# --- slow reader (T15) ------------------------------------------------------------------------------------------
srv, path = start_server(SineSynth(), slow_reader_s=1.0, out_buffer_bytes=65536)
slow = Raw(path)
slow.hello()
slow.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
slow.send(start_line(id="flood", text=("word " * 39 + "word. ") * 24, end=True))
t = time.time()
good = Raw(path)
good.hello()
good.send(start_line(id="fine", text="Hi.", end=True))
check("another caller completes while a reader stalls", good.read_until_end("fine")[-1]["reason"] == "complete")
slow.sock.settimeout(0.2)
dropped = False
deadline = t + 2.0
while time.time() < deadline and not dropped:
    time.sleep(0.2)
    dropped = fresh_status(path)["callers"] == 2  # the status caller itself and "good"
check("a reader that never reads is dropped within 2 s", dropped, "elapsed %.2f" % (time.time() - t))
stop_server(srv)

# --- request_exit ---------------------------------------------------------------------------------------------------
srv, path = start_server(SineSynth(synth_delay=0.1))
c = Raw(path)
c.hello()
c.send(start_line(id="mid", text="Word. " * 20, end=True))
c.read_event()
srv.request_exit("idle")
srv.request_exit("stop")
srv.thread.join(5)
check("first exit reason wins, serve_forever returns", srv.result == ["idle"])
tail = c.read_until_end("mid")
check("an exit other than stop ends open requests with restarting",
      tail[-1]["reason"] == "error" and [e["code"] for e in tail if e.ev == "error"] == ["restarting"])
srv, path = start_server(SineSynth(synth_delay=0.1))
c = Raw(path)
c.hello()
c.send(start_line(id="rel", text="Word. " * 20, end=True))
c.read_event()
srv.request_exit("release")
tail = c.read_until_end("rel")
check("release ends open requests with released",
      tail[-1]["reason"] == "error" and [e["code"] for e in tail if e.ev == "error"] == ["released"])
srv.thread.join(5)

srv, path = start_server(SineSynth(synth_delay=0.1))
c = Raw(path)
c.hello()
c.send(start_line(id="halt", text="Word. " * 20, end=True))
c.read_event()
c.send({"op": "stop"})
tail = c.read_until_end("halt")
srv.thread.join(5)
check("stop op: open requests end cancelled and the server exits with 'stop'",
      tail[-1]["reason"] == "cancelled" and srv.result == ["stop"])

report()
