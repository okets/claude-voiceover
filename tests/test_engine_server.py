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
from engine_server import NoModel
from engine_stub import SineSynth
from _engine_test_util import Event, Raw, serve, start_line, stop_server

SOCK_DIR = temp_dir("vo-sock-")
_servers = []


def start_server(synth, **kwargs):
    path = str(SOCK_DIR / ("e%d.sock" % len(_servers)))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen(32)
    server = serve(synth, [listener], **kwargs)
    _servers.append(server)
    return server, path


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
time.sleep(0.2)  # long enough for the sentence that was in flight to finish rendering
c.send({"op": "start", "id": "next", "voice": "bf_emma", "text": "Hi.", "end": True})
after_cancel = c.read_until_end("next")
check("next served, no stray ids", {e["id"] for e in after_cancel} == {"next"})
check("no audio frame for an id after its end(cancelled)",
      not [e for e in after_cancel if e.ev == "audio" and e["id"] == "long"])

c.send({"op": "start", "id": "a", "voice": "bf_emma", "text": "Word. " * 30})
c.send({"op": "start", "id": "b", "voice": "bf_emma", "text": "Word. " * 30})
c.send({"op": "cancel"})
ends = {c.read_until("end")[-1]["id"] for _ in range(2)}
check("cancel without id cancels everything on the connection", ends == {"a", "b"})

c.send({"op": "start", "id": "dup", "voice": "bf_emma"})
c.send({"op": "start", "id": "dup", "voice": "bf_emma"})
dup_err = c.read_error()
check("active id reuse -> bad_request with ref, no id",
      dup_err["code"] == "bad_request" and dup_err.get("ref") == "dup" and "id" not in dup_err, dup_err)
c.send({"op": "text", "id": "dup", "text": "Still mine."})
c.send({"op": "end", "id": "dup"})
dup_events = c.read_until_end("dup")
check("the original request still completes with its own end",
      dup_events[-1]["reason"] == "complete" and not any(e.ev == "error" for e in dup_events), dup_events[-1])

# --- requests on one connection are served in start order -------------------------------------------
c.send({"op": "start", "id": "A", "voice": "bf_emma"})
c.send({"op": "text", "id": "A", "text": "One. Tw"})
c.send({"op": "start", "id": "B", "voice": "bf_emma", "text": "Bee. Bee.", "end": True})
first = c.read_event()
check("the head request renders while a later one waits", first.ev == "audio" and first["id"] == "A")
c.send({"op": "text", "id": "A", "text": "o."})
c.send({"op": "end", "id": "A"})
order = [first] + c.read_until_end("B")
ids = [e["id"] for e in order if e.ev in ("audio", "end")]
check("all of A's audio and its end arrive before any of B's audio",
      ids == ["A", "A", "A", "B", "B", "B"] and order[-1]["reason"] == "complete"
      and [e["reason"] for e in order if e.ev == "end"] == ["complete", "complete"], ids)

c.send({"op": "start", "id": "EA", "voice": "bf_emma", "text": "Real words here.", "end": True})
c.send({"op": "start", "id": "EB", "voice": "bf_emma", "text": "   ", "end": True})
ends = [e for e in c.read_until_end("EB") if e.ev == "end"]
check("a request's end is sent before the next request's, even one that completes at once",
      [e["id"] for e in ends] == ["EA", "EB"] and ends[1]["samples"] == 0, ends)

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
c.send({"op": "start", "id": "fl", "voice": "bf_emma"})
for bad_n in ({}, {"n": "1"}, {"n": True}):
    c.send(dict({"op": "flush", "id": "fl"}, **bad_n))
    check("flush without an integer n -> bad_request %r" % bad_n, c.read_error()["code"] == "bad_request")
c.send({"op": "cancel", "id": "fl"})
c.read_until_end("fl")
c.send({"op": "frobnicate"})
check("unknown op -> bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "text", "id": "nobody", "text": "x"})
nobody = c.read_error()
check("text for an unknown id -> bad_request with ref", nobody["code"] == "bad_request"
      and nobody.get("ref") == "nobody" and "id" not in nobody, nobody)
c.send({"op": "status"})
check("nothing follows a ref error (no end)", c.read_event().ev == "status")
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
stop_server(srv)
srv, path = start_server(SineSynth(load_delay=0.6))  # a fresh model: the request is still waiting for it
c = Raw(path)
c.hello()
c.send(start_line(id="gone", text="Hi.", end=True))
c.send({"op": "cancel", "id": "gone"})
check("cancel before activation ends the request once", c.read_until_end("gone")[-1]["reason"] == "cancelled")
stop_server(srv)

# --- limits: requests per connection (M3); Unix connections uncapped (M4) ------------------------------------
srv, path = start_server(SineSynth(synth_delay=0.1), max_connections=2, max_requests=2)
a, b = Raw(path), Raw(path)
a.hello(); b.hello()
third = Raw(path)
check("M4: Unix connections are not capped: a third over a cap of 2 still gets hello", third.hello().ev == "hello")
a.send(start_line(id="q1", text="Word. " * 10))
a.send(start_line(id="q2", text="Word. " * 10))
a.send(start_line(id="q3", text="Hi.", end=True))
busy = a.read_error()
check("M3: a connection's request over its limit -> busy for that request, which then ends in error",
      busy["code"] == "busy" and busy["id"] == "q3" and a.read_until_end("q3")[-1]["reason"] == "error", busy)
b.send(start_line(id="q4", text="Hi.", end=True))
check("M3: the limit is per connection: another connection still opens requests",
      b.read_until_end("q4")[-1]["reason"] == "complete")
a.send({"op": "cancel"})
a.read_until_end("q1"); a.read_until_end("q2")
a.send(start_line(id="q5", text="Hi.", end=True))
check("capacity returns after cancels", a.read_until_end("q5")[-1]["reason"] == "complete")
stop_server(srv)

# --- M5: cancel with a non-string id is bad_request (ref), and the connection stays ---------------------------
srv, path = start_server(SineSynth())
c = Raw(path)
c.hello()
for bad_id in (["x"], {"k": 1}, 7):
    c.send({"op": "cancel", "id": bad_id})
    error = c.read_event()
    check("M5: cancel with id %r -> bad_request carrying ref" % (bad_id,),
          error.ev == "error" and error["code"] == "bad_request" and error.get("ref") == bad_id and "id" not in error,
          error)
c.send({"op": "status"})
check("M5: the connection is still served after a bad cancel", c.read_event().ev == "status")
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

# --- a stalled reader is not rendered for beyond the buffer limit; a reader that drains is not dropped ----
SENTENCE_BYTES = 40 * 2880
synth = SineSynth()
srv, path = start_server(synth, slow_reader_s=0.6, out_buffer_bytes=65536)
slow = Raw(path)
slow.hello()
slow.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
slow.send(start_line(id="flood", text=("word " * 39 + "word. ") * 24, end=True))
time.sleep(0.3)
conn = next(iter(srv._conns))
check("a non-reading caller's buffer stays bounded: limit + the sentence that tripped it",
      conn.buffered_bytes() <= 65536 + SENTENCE_BYTES and synth.synth_calls < 24,
      (conn.buffered_bytes(), synth.synth_calls))
slow.sock.settimeout(5)
got = slow.read_until_end("flood")
check("a reader that drains in time gets everything and is not dropped",
      got[-1]["reason"] == "complete" and got[-1]["samples"] == 24 * 40 * 1440, got[-1])
time.sleep(1.0)  # well past slow_reader_s since the stall began
check("the drained caller is still connected", srv.snapshot()["callers"] == 1)
slow.send({"op": "status"})
check("and still answers", slow.read_event().ev == "status")
stop_server(srv)

# --- a failure in loop-side work ends that request only (M7) ----------------------------------------------
class FlakyVoiceSynth(SineSynth):
    def default_speed(self, voice):
        if voice == "af_sky":
            raise RuntimeError("no speed for this voice")
        return 1.0


srv, path = start_server(FlakyVoiceSynth(load_delay=0.2))
c, d = Raw(path), Raw(path)
c.hello(); d.hello()
real_stderr, sys.stderr = sys.stderr, io.StringIO()
c.send(start_line(id="bad", voice="af_sky", text="Hi.", end=True))
d.send(start_line(id="good", text="Hi.", end=True))
bad = c.read_until_end("bad")
good = d.read_until_end("good")
logged, sys.stderr = sys.stderr.getvalue(), real_stderr
check("a request whose activation raises ends with synthesis_failed, while loading",
      [e["code"] for e in bad if e.ev == "error"] == ["synthesis_failed"] and bad[-1]["reason"] == "error")
check("the others waiting on the same load still complete", good[-1]["reason"] == "complete")
real_stderr, sys.stderr = sys.stderr, io.StringIO()
c.send(start_line(id="bad2", voice="af_sky", text="Hi.", end=True))
bad2 = c.read_until_end("bad2")
sys.stderr = real_stderr
check("and when the model is already loaded", bad2[-1]["reason"] == "error" and "no speed" in logged)
check("the connection stays usable", end_for(c, {"text": "Hi.", "end": True}) == ("complete", 1440))
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
check("a release exit ends open requests with restarting, like any other exit",
      tail[-1]["reason"] == "error" and [e["code"] for e in tail if e.ev == "error"] == ["restarting"])
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
