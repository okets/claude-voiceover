# tts/engine_server.py
"""The shared Kokoro engine's server: protocol 1 over listening sockets. Stdlib only.

Threads. Everything touching sockets and request state runs on one asyncio loop
(serve_forever). One worker thread renders sentences and loads the model; it
only reads the shared scheduler and request registry under `_lock` and reports
back with loop.call_soon_threadsafe. The worker never writes to a socket.

The Synth interface (duck-typed; `engine_stub.SineSynth` is the reference):

    loaded: bool                      True once load() has finished
    load() -> None                    load the model; may raise NoModel
    voices() -> set[str]              voice names usable after load()
    default_speed(voice) -> float     speed when a request gives none
    synthesize(text, voice, speed, lang) -> bytes    pcm_s16le, 24 kHz mono
    status() -> dict                  keys device, requested, fallback, vram_mib
"""

import asyncio
import os
import socket
import sys
import threading
import traceback

from engine_connection import Connection
from engine_protocol import LIMITS, PROTOCOL, split_frames
from engine_scheduler import Job, Scheduler
from engine_text import UtteranceText

SPEED_RANGE = (0.5, 2.0)
DEFAULT_LANG = "en-us"
SHUTDOWN_WAIT_S = 2.0

# Exit reason -> how a request still open at that moment ends: (error code or None, end reason).
_EXIT_ENDINGS = {"stop": (None, "cancelled"), "release": ("released", "error")}
_DEFAULT_EXIT_ENDING = ("restarting", "error")


class NoModel(Exception):
    """The model files are not available (Synth.load)."""


class Request:
    """One started utterance. Mutated on the loop thread only."""

    def __init__(self, conn, req_id, voice, speed, lang, live):
        self.conn = conn
        self.id = req_id
        self.voice = voice
        self.speed = speed          # None until the voice's default is looked up
        self.lang = lang
        self.live = live
        self.text = UtteranceText(live=live)
        self.ready = False          # voice validated: sentences may be queued
        self.input_ended = False
        self.ended = False
        self.held = []              # sentences produced before the request was ready
        self.queued = 0             # sentences produced so far
        self.done = 0               # sentences rendered and sent
        self.markers = []           # (sentences that must be done, flush n)
        self.samples = 0
        self.seq = 0


def _valid_speed(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and SPEED_RANGE[0] <= value <= SPEED_RANGE[1])


def _start_problem(msg):
    """(code, message) for the first thing wrong with a start line, else None."""
    if not isinstance(msg.get("voice"), str) or not msg["voice"]:
        return "bad_request", "start needs a voice"
    if msg.get("speed") is not None and not _valid_speed(msg["speed"]):
        return "bad_speed", "speed must be a number from %s to %s" % SPEED_RANGE
    if not isinstance(msg.get("lang", DEFAULT_LANG), str):
        return "bad_request", "lang must be a string"
    return _text_problem(msg)


def _text_problem(msg):
    if msg.get("text") is not None and not isinstance(msg["text"], str):
        return "bad_request", "text must be a string"
    return None


class EngineServer:
    def __init__(self, synth, *, idle_unload_s, token, plugin_version, plugin_root,
                 max_connections=16, max_requests=64, out_buffer_bytes=2 * 1024 * 1024,
                 slow_reader_s=30.0):
        self._synth = synth
        self._idle_unload_s = idle_unload_s   # used by the lifecycle work (Task 7)
        self._token = token                   # used by the TCP listener work (Task 6)
        self._plugin_version = plugin_version
        self._plugin_root = plugin_root
        self._max_connections = max_connections
        self._max_requests = max_requests
        self._out_buffer_bytes = out_buffer_bytes
        self._slow_reader_s = slow_reader_s
        self._conns = set()
        self._handlers = set()
        self._awaiting_load = []              # requests waiting for the model, loop thread
        self._load_in_flight = False          # loop thread
        # Shared with the worker, guarded by _cond:
        self._cond = threading.Condition()
        self._scheduler = Scheduler()
        self._registry = {}                   # (conn id, request id) -> Request
        self._load_wanted = False
        self._shutting_down = False
        # Exit handshake:
        self._exit_lock = threading.Lock()
        self._exit_reason = None
        self._loop = None
        self._exit_future = None

    # --- running -----------------------------------------------------------------

    def serve_forever(self, listeners):
        """Block, serving `listeners`, until request_exit; return the reason."""
        loop = asyncio.new_event_loop()
        worker = threading.Thread(target=self._work, name="engine-worker", daemon=True)
        try:
            loop.run_until_complete(self._main(loop, listeners, worker))
        finally:
            loop.close()
        return self._exit_reason

    async def _main(self, loop, listeners, worker):
        self._exit_future = loop.create_future()
        with self._exit_lock:
            self._loop = loop
            already = self._exit_reason is not None
        servers = [await self._listen(sock) for sock in listeners]
        worker.start()
        if already:
            self._set_exit_future()
        await self._exit_future
        await self._shutdown(servers, worker)

    async def _listen(self, sock):
        if sock.family == socket.AF_UNIX:
            return await asyncio.start_unix_server(self._accept, sock=sock)
        return await asyncio.start_server(self._accept, sock=sock)

    def request_exit(self, reason):
        """Thread-safe. The first reason given is the one serve_forever returns."""
        with self._exit_lock:
            if self._exit_reason is None:
                self._exit_reason = reason
            loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._set_exit_future)
            except RuntimeError:
                pass  # loop already closed

    def _set_exit_future(self):
        if not self._exit_future.done():
            self._exit_future.set_result(None)

    async def _shutdown(self, servers, worker):
        for server in servers:
            server.close()
        self._end_everything(self._exit_reason)
        with self._cond:
            self._shutting_down = True
            self._cond.notify_all()
        for conn in list(self._conns):
            conn.close()
        if self._handlers:
            await asyncio.wait(list(self._handlers), timeout=SHUTDOWN_WAIT_S)
        await asyncio.get_running_loop().run_in_executor(None, worker.join, SHUTDOWN_WAIT_S)

    def stop(self):
        """The `stop` op: end everything as cancelled and exit."""
        self.request_exit("stop")

    # --- accepting -----------------------------------------------------------------

    async def _accept(self, reader, writer):
        if len(self._conns) >= self._max_connections:
            writer.write(Connection.busy_line())
            writer.close()
            return
        conn = Connection(self, reader, writer, self._out_buffer_bytes, self._slow_reader_s)
        self._conns.add(conn)
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            await conn.run()
            await conn.wait_closed()
        finally:
            self._handlers.discard(task)

    def drop_connection(self, conn):
        """The caller is gone: drop its queued work and discard what is in flight."""
        self._conns.discard(conn)
        for req in list(conn.requests.values()):
            self._forget(req)
        with self._cond:
            self._scheduler.cancel(conn.id)

    def snapshot(self):
        """What hello and status report."""
        loaded = bool(self._synth.loaded)
        engine = self._synth.status() if loaded else {}
        return {"proto": PROTOCOL, "plugin": self._plugin_version, "pid": os.getpid(),
                "loaded": loaded, "device": engine.get("device"), "requested": engine.get("requested"),
                "fallback": engine.get("fallback"), "vram_mib": engine.get("vram_mib"),
                "callers": len(self._conns), "requests": len(self._registry)}

    # --- requests (loop thread) --------------------------------------------------------

    def open_request(self, conn, msg):
        req_id = msg.get("id")
        if not isinstance(req_id, str) or not req_id:
            conn.send_error("bad_request", "start needs a string id")
        elif req_id in conn.requests:
            conn.send_error("bad_request", "request id %s is still active" % req_id, req_id)
        else:
            problem = _start_problem(msg) or self._capacity_problem()
            if problem:
                conn.send_error(problem[0], problem[1], req_id)
                conn.send_end(req_id, "error", 0)
            else:
                self._begin(self._register(conn, req_id, msg), msg)

    def _capacity_problem(self):
        if len(self._registry) >= self._max_requests:
            return "busy", "too many requests in flight"
        return None

    def _register(self, conn, req_id, msg):
        req = Request(conn, req_id, msg["voice"], msg.get("speed"), msg.get("lang", DEFAULT_LANG),
                      msg.get("live") is True)
        conn.requests[req_id] = req
        with self._cond:
            self._registry[(conn.id, req_id)] = req
        return req

    def _begin(self, req, msg):
        if msg.get("text"):
            self._feed(req, msg["text"])
        if not req.ended and msg.get("end") is True:
            self._end_input(req)
        if req.ended:
            return
        if self._synth.loaded:
            self._activate(req)
        else:
            self._await_model(req)

    def _find(self, conn, msg):
        """The active request a text/flush/end line is about, else complain and None."""
        req = conn.requests.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
        if req is None:
            conn.send_error("bad_request", "no active request with that id", msg.get("id"))
        elif req.input_ended:
            conn.send_error("bad_request", "request input already ended", req.id)
            req = None
        return req

    def add_text(self, conn, msg):
        req = self._find(conn, msg)
        problem = _text_problem(msg) if req else None
        if problem:
            conn.send_error(problem[0], problem[1], req.id)
        elif req and msg.get("text"):
            self._feed(req, msg["text"])

    def flush_request(self, conn, msg):
        req = self._find(conn, msg)
        if req:
            self._add_sentences(req, req.text.drain())
            req.markers.append((req.queued, msg.get("n")))
            self._release_markers(req)

    def end_request(self, conn, msg):
        req = self._find(conn, msg)
        if req:
            self._end_input(req)

    def cancel_requests(self, conn, req_id):
        """`cancel`: one request by id, or everything on the connection."""
        if req_id is None:
            targets = list(conn.requests.values())
        else:
            targets = [conn.requests[req_id]] if req_id in conn.requests else []
        for req in targets:
            self._finish(req, "cancelled")

    def _feed(self, req, text):
        sentences = req.text.feed(text)
        if req.text.chars > LIMITS["text"]:
            self._fail(req, "text_too_long", "a request may hold at most %d characters" % LIMITS["text"])
        else:
            self._add_sentences(req, sentences)

    def _end_input(self, req):
        self._add_sentences(req, req.text.drain())
        req.input_ended = True
        self._maybe_finish(req)

    def _add_sentences(self, req, sentences):
        for sentence in sentences:
            req.queued += 1
            if req.ready:
                self._enqueue(req, sentence)
            else:
                req.held.append(sentence)

    def _enqueue(self, req, sentence):
        with self._cond:
            self._scheduler.add(Job(req.conn.id, req.id, sentence, req.live))
            self._cond.notify()

    # --- model loading and voice validation ----------------------------------------------

    def _await_model(self, req):
        self._awaiting_load.append(req)
        if not self._load_in_flight:
            self._load_in_flight = True
            with self._cond:
                self._load_wanted = True
                self._cond.notify()

    def _model_loaded(self, error):
        """Loop thread: the worker's load attempt finished; `error` is (code, message) or None."""
        self._load_in_flight = False
        waiting, self._awaiting_load = self._awaiting_load, []
        for req in waiting:
            if req.ended:
                continue
            if error:
                self._fail(req, *error)
            else:
                self._activate(req)

    def _activate(self, req):
        """The model is loaded: check the voice, then let the request's sentences run."""
        if req.voice not in self._synth.voices():
            self._fail(req, "unknown_voice", "voice %s is not in the voice file" % req.voice)
            return
        if req.speed is None:
            req.speed = self._synth.default_speed(req.voice)
        req.ready = True
        held, req.held = req.held, []
        for sentence in held:
            self._enqueue(req, sentence)
        self._maybe_finish(req)

    # --- results (loop thread) ---------------------------------------------------------------

    def _rendered(self, req, pcm, error):
        if req.ended:
            return  # cancelled or dropped while this sentence was in flight
        if error:
            self._fail(req, "synthesis_failed", error)
            return
        for frame in split_frames(pcm):
            req.conn.send_audio(req.id, req.seq, frame)
            req.seq += 1
        req.samples += len(pcm) // 2
        req.done += 1
        self._release_markers(req)
        self._maybe_finish(req)

    def _release_markers(self, req):
        while req.markers and req.markers[0][0] <= req.done:
            _, n = req.markers.pop(0)
            req.conn.send({"ev": "flushed", "id": req.id, "n": n})

    def _maybe_finish(self, req):
        if req.ready and req.input_ended and req.done == req.queued and not req.ended:
            self._finish(req, "complete")

    def _fail(self, req, code, message):
        if not req.ended:
            req.conn.send_error(code, message, req.id)
            self._finish(req, "error")

    def _finish(self, req, reason):
        """The one place a request ends: forget it, then say so exactly once."""
        if req.ended:
            return
        self._forget(req)
        req.conn.send_end(req.id, reason, req.samples)

    def _forget(self, req):
        req.ended = True
        req.conn.requests.pop(req.id, None)
        with self._cond:
            self._registry.pop((req.conn.id, req.id), None)
            self._scheduler.cancel(req.conn.id, req.id)

    def _end_everything(self, exit_reason):
        code, reason = _EXIT_ENDINGS.get(exit_reason, _DEFAULT_EXIT_ENDING)
        for conn in list(self._conns):
            for req in list(conn.requests.values()):
                if code:
                    conn.send_error(code, "engine is leaving: %s" % exit_reason, req.id)
                self._finish(req, reason)

    # --- worker thread --------------------------------------------------------------------------

    def _work(self):
        while True:
            task = self._next_task()
            if task is None:
                return
            kind, req, job = task
            if kind == "load":
                self._run_load()
            else:
                self._run_job(req, job)

    def _next_task(self):
        """Block until there is a load to do or a sentence to render; None on shutdown."""
        with self._cond:
            while not self._shutting_down:
                if self._load_wanted:
                    self._load_wanted = False
                    return "load", None, None
                job = self._scheduler.next()
                req = self._registry.get((job.conn, job.req)) if job else None
                if req is not None:
                    return "job", req, job
                if job is None:
                    self._cond.wait()
        return None

    def _run_load(self):
        try:
            self._synth.load()
            error = None
        except NoModel as exc:
            error = ("no_model", str(exc) or "no model available")
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            error = ("synthesis_failed", "model load failed: %s" % exc)
        self._to_loop(self._model_loaded, error)

    def _run_job(self, req, job):
        try:
            pcm, error = self._synth.synthesize(job.text, req.voice, req.speed, req.lang), None
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            pcm, error = b"", "synthesis failed: %s" % exc
        self._to_loop(self._rendered, req, pcm, error)

    def _to_loop(self, callback, *args):
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass  # loop closed: shutting down
