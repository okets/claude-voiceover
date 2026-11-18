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
import fcntl
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

import engine_paths
from engine_common import engine_log
from engine_connection import Connection
from engine_lifecycle import LifecyclePolicy, QuietInputs, quiet_exit_reason
from engine_protocol import LIMITS, PROTOCOL, split_frames
from engine_scheduler import Job, Scheduler
from engine_text import UtteranceText

SPEED_RANGE = (0.5, 2.0)
DEFAULT_LANG = "en-us"
SHUTDOWN_WAIT_S = 2.0

# Exit reason -> how a request still open at that moment ends: (error code or None, end reason).
_EXIT_ENDINGS = {"stop": (None, "cancelled")}
_DEFAULT_EXIT_ENDING = ("restarting", "error")


SYSTEMD_FIRST_FD = 3
SOCKET_MODE = 0o600
TCP_HOST = "127.0.0.1"
LISTEN_BACKLOG = 32


def adopt_systemd_listeners(environ=os.environ):
    """Sockets passed by systemd socket activation, or None when there are none for this process."""
    pid, count = environ.pop("LISTEN_PID", None), environ.pop("LISTEN_FDS", None)
    environ.pop("LISTEN_FDNAMES", None)
    if pid != str(os.getpid()) or not (count or "").isdigit() or int(count) < 1:
        return None
    return [_adopt_fd(fd) for fd in range(SYSTEMD_FIRST_FD, SYSTEMD_FIRST_FD + int(count))]


def _adopt_fd(fd):
    """Wrap an inherited listening fd; socket() reads the family from the fd's own address."""
    return socket.socket(fileno=fd)


def bind_listeners(sock_path: Path, port):
    """Bind the Unix socket (and 127.0.0.1:port). The caller already holds engine.lock,
    so any socket file at the path is a leftover and may be removed."""
    sock_path = Path(sock_path)
    listeners = [_bind_unix(sock_path)]
    if port is not None:
        try:
            listeners.append(_bind_tcp(port))
        except BaseException:
            listeners[0].close()
            raise
    return listeners


def _bind_unix(sock_path):
    sock_path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # the socket is created 0600, never briefly wider
    try:
        sock.bind(str(sock_path))
        os.chmod(sock_path, SOCKET_MODE)
        sock.listen(LISTEN_BACKLOG)
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
    return sock


def _bind_tcp(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((TCP_HOST, port))
        sock.listen(LISTEN_BACKLOG)
    except BaseException:
        sock.close()
        raise
    return sock


def hold_single_instance(lock: Path):
    """Take engine.lock without blocking. The fd (keep it open), or None if another engine holds it."""
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


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
                 slow_reader_s=30.0, auth_timeout_s=5.0, quiet_check_s=10.0):
        self._synth = synth
        self._idle_unload_s = idle_unload_s
        self._policy = LifecyclePolicy(idle_unload_s, quiet_check_s)
        self._start_device = None             # tts_device when serving began
        self._warm_waiters = []               # (conn) waiting for the model to answer warm
        self._token = token                   # required on TCP connections only
        self.last_tcp_nodelay = None          # test hook: TCP_NODELAY of the last accepted TCP socket
        self._plugin_version = plugin_version
        self._plugin_root = plugin_root
        self._max_connections = max_connections
        self._max_requests = max_requests
        self._out_buffer_bytes = out_buffer_bytes
        self._slow_reader_s = slow_reader_s
        self._auth_timeout_s = auth_timeout_s
        self._conns = set()
        self._handlers = set()
        self._awaiting_load = []              # requests waiting for the model, loop thread
        self._load_in_flight = False          # loop thread
        # Shared with the worker, guarded by _cond:
        self._cond = threading.Condition()
        self._scheduler = Scheduler()
        self._registry = {}                   # (conn id, request id) -> Request
        self._blocked = frozenset()           # connections whose output buffer is over its limit
        self._load_wanted = False
        self._delivery_pending = False        # a rendered sentence has not reached the loop yet
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
        self._start_device = engine_paths.read_global_settings().get("tts_device")
        worker.start()
        if already:
            self._set_exit_future()
        ticker = loop.create_task(self._tick_lifecycle())
        await self._exit_future
        ticker.cancel()
        await self._shutdown(servers, worker)

    # --- lifecycle wiring (the decisions live in engine_lifecycle) -------------------

    def _busy(self):
        """Something queued, active, or waiting on the model: the engine must stay."""
        return bool(self._registry or self._warm_waiters)

    async def _tick_lifecycle(self):
        while True:
            await asyncio.sleep(self._policy.tick_s)
            reason = self._guarded_lifecycle_exit()
            if reason:
                self.request_exit(reason)
                return

    def _guarded_lifecycle_exit(self):
        """A failing check is logged and skipped: the ticker must never die (VRAM would stay held)."""
        try:
            return self._lifecycle_exit()
        except Exception as exc:
            engine_log("error", "lifecycle check failed: %s: %s" % (type(exc).__name__, exc))
            return None

    def _lifecycle_exit(self):
        if self._policy.idle_due(self._busy()):
            return "idle"
        if self._policy.quiet_due() and self._is_quiet():
            return quiet_exit_reason(self._quiet_inputs())
        return None

    def _is_quiet(self):
        return not self._busy() and not self._policy.leases() and not self._conns

    def _quiet_inputs(self):
        loaded = bool(self._synth.loaded)
        return QuietInputs(plugin_root=self._plugin_root, start_device=self._start_device,
                           pointer=engine_paths.read_pointer(), settings=engine_paths.read_global_settings(),
                           engine_status=self._synth.status() if loaded else {},
                           keep_off=engine_paths.read_keep_off())

    def warm(self, conn, msg):
        """`warm`: load the model if needed, hold it for hold_s, reply once it is loaded."""
        hold_s = msg.get("hold_s", 0)
        warm_id = msg.get("id") if isinstance(msg.get("id"), str) else None   # echoed back, optional
        if not isinstance(hold_s, (int, float)) or isinstance(hold_s, bool):
            conn.send_error("bad_request", "hold_s must be a number", ref=warm_id)
            return
        token = self._policy.hold(hold_s)
        if self._synth.loaded:
            self._send_warm(conn, warm_id)
        else:
            self._warm_waiters.append((conn, token, warm_id))
            self._request_load()

    def _send_warm(self, conn, warm_id=None):
        reply = {"ev": "warm", "loaded": True, "device": self._synth.status().get("device")}
        if warm_id is not None:
            reply["id"] = warm_id
        conn.send(reply)

    def release(self, conn, msg):
        """`release`: leave for a while (keep-off), ending open requests with restarting."""
        keep_off_s = msg.get("keep_off_s", 0)
        if not isinstance(keep_off_s, (int, float)) or isinstance(keep_off_s, bool):
            conn.send_error("bad_request", "keep_off_s must be a number")
            return
        if keep_off_s > 0:
            engine_paths.write_keep_off(keep_off_s, "release")
        conn.send({"ev": "released", "vram_mib": 0})
        self.request_exit("release")

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
        conn = Connection(self, reader, writer, self._out_buffer_bytes, self._slow_reader_s,
                          token=self._token, needs_auth=self._prepare_remote(writer),
                          auth_timeout_s=self._auth_timeout_s)
        self._conns.add(conn)
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            await conn.run()
            await conn.wait_closed()
        finally:
            self._handlers.discard(task)

    @staticmethod
    def _is_remote(writer):
        """Fail closed: anything that is not a Unix socket (IPv4, IPv6, unknown) needs the token."""
        sock = writer.get_extra_info("socket")
        return sock is None or sock.family != socket.AF_UNIX

    def _prepare_remote(self, writer):
        """Non-Unix callers get TCP_NODELAY (set explicitly, then read back for tests)."""
        if not self._is_remote(writer):
            return False
        sock = writer.get_extra_info("socket")
        if sock is None:
            return True
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.last_tcp_nodelay = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        return True

    def drop_connection(self, conn):
        """The caller is gone: drop its queued work and discard what is in flight."""
        self._conns.discard(conn)
        self._warm_waiters = [w for w in self._warm_waiters if w[0] is not conn]
        for req in list(conn.requests.values()):
            self._forget(req, promote=False)
        with self._cond:
            self._scheduler.cancel(conn.id)
            self._blocked -= {conn.id}

    def set_blocked(self, conn_id, blocked):
        """A caller's output buffer went over (or back under) its limit: stop/resume rendering for it."""
        with self._cond:
            self._blocked = self._blocked | {conn_id} if blocked else self._blocked - {conn_id}
            self._cond.notify()

    def snapshot(self):
        """What hello and status report."""
        loaded = bool(self._synth.loaded)
        engine = self._synth.status() if loaded else {}
        return {"proto": PROTOCOL, "plugin": self._plugin_version, "pid": os.getpid(),
                "loaded": loaded, "device": engine.get("device"), "requested": engine.get("requested"),
                "fallback": engine.get("fallback"), "vram_mib": engine.get("vram_mib"),
                "callers": len(self._conns), "requests": len(self._registry),
                "warm_until": self._warm_until(), "idle_unload_s": self._idle_unload_s,
                "idle_s": self._policy.idle_s(self._busy()), "uptime_s": self._policy.uptime_s(),
                "leases": self._policy.leases()}

    def _warm_until(self):
        """Wall-clock time the last warm hold ends, or None."""
        remaining = self._policy.warm_remaining()
        return None if remaining is None else time.time() + remaining

    # --- requests (loop thread) --------------------------------------------------------

    def open_request(self, conn, msg):
        req_id = msg.get("id")
        if not isinstance(req_id, str) or not req_id:
            conn.send_error("bad_request", "start needs a string id")
        elif req_id in conn.requests:
            conn.send_error("bad_request", "request id %s is still active" % req_id, ref=req_id)
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
        self._policy.note_activity()
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
            self._guarded(req, self._activate, req)
        else:
            self._await_model(req)

    def _find(self, conn, msg):
        """The active request a text/flush/end line is about, else complain and None."""
        req = conn.requests.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
        if req is None:
            conn.send_error("bad_request", "no active request with that id", ref=msg.get("id"))
        elif req.input_ended:
            conn.send_error("bad_request", "request input already ended", ref=req.id)
            req = None
        return req

    def add_text(self, conn, msg):
        req = self._find(conn, msg)
        problem = _text_problem(msg) if req else None
        if problem:
            conn.send_error(problem[0], problem[1], ref=req.id)
        elif req and msg.get("text"):
            self._feed(req, msg["text"])

    def flush_request(self, conn, msg):
        req = self._find(conn, msg)
        if req and (not isinstance(msg.get("n"), int) or isinstance(msg.get("n"), bool)):
            conn.send_error("bad_request", "flush needs an integer n", ref=req.id)
        elif req:
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
            if self._may_render(req):
                self._enqueue(req, sentence)
            else:
                req.held.append(sentence)

    @staticmethod
    def _is_head(req):
        """Requests on a connection are served in start order: only the oldest one renders."""
        return next(iter(req.conn.requests.values()), None) is req

    def _may_render(self, req):
        return req.ready and self._is_head(req)

    def _enqueue(self, req, sentence):
        with self._cond:
            self._scheduler.add(Job(req.conn.id, req.id, sentence, req.live))
            self._cond.notify()

    # --- model loading and voice validation ----------------------------------------------

    def _await_model(self, req):
        self._awaiting_load.append(req)
        self._request_load()

    def _request_load(self):
        if not self._load_in_flight:
            self._load_in_flight = True
            with self._cond:
                self._load_wanted = True
                self._cond.notify()

    def _model_loaded(self, error):
        """Loop thread: the worker's load attempt finished; `error` is (code, message) or None."""
        self._load_in_flight = False
        waiting, self._awaiting_load = self._awaiting_load, []
        self._answer_warm_waiters(error)
        for req in waiting:
            if req.ended:
                continue
            if error:
                self._fail(req, *error)
            else:
                self._guarded(req, self._activate, req)

    def _answer_warm_waiters(self, error):
        waiters, self._warm_waiters = self._warm_waiters, []
        for conn, token, warm_id in waiters:
            if error:
                self._policy.drop_hold(token)  # a failed load must not keep the engine alive
                conn.send_error(error[0], error[1], ref=warm_id)
            else:
                self._send_warm(conn, warm_id)

    def _activate(self, req):
        """The model is loaded: check the voice, then let the request's sentences run."""
        if req.voice not in self._synth.voices():
            self._fail(req, "unknown_voice", "voice %s is not in the voice file" % req.voice)
            return
        if req.speed is None:
            req.speed = self._synth.default_speed(req.voice)
        req.ready = True
        self._release(req)

    def _release(self, req):
        """Hand a ready head request its buffered sentences, and finish it if nothing is left."""
        if self._may_render(req):
            held, req.held = req.held, []
            for sentence in held:
                self._enqueue(req, sentence)
            self._release_markers(req)
            self._maybe_finish(req)

    # --- results (loop thread) ---------------------------------------------------------------

    def _rendered(self, req, pcm, error):
        """One sentence at a time: the worker renders the next only after this one is
        written, so the output-buffer check below always sees every rendered byte."""
        try:
            if not req.ended:  # else cancelled or dropped while this sentence was in flight
                self._guarded(req, self._deliver, req, pcm, error)
        finally:
            with self._cond:
                self._delivery_pending = False
                self._cond.notify()

    def _guarded(self, req, work, *args):
        """Run loop-side work for one request; a failure ends that request only (M7)."""
        try:
            work(*args)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self._fail(req, "synthesis_failed", "internal error: %s" % exc)

    def _deliver(self, req, pcm, error):
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
        while self._is_head(req) and req.markers and req.markers[0][0] <= req.done:
            _, n = req.markers.pop(0)
            req.conn.send({"ev": "flushed", "id": req.id, "n": n})

    def _maybe_finish(self, req):
        if self._is_head(req) and req.ready and req.input_ended and req.done == req.queued and not req.ended:
            self._finish(req, "complete")

    def _fail(self, req, code, message):
        if not req.ended:
            req.conn.send_error(code, message, req.id)
            self._finish(req, "error")

    def _finish(self, req, reason):
        """The one place a request ends: forget it, then say so exactly once."""
        if req.ended:
            return
        self._forget(req, promote=False)
        req.conn.send_end(req.id, reason, req.samples)  # before the next request can finish
        self._promote(req.conn)

    def _forget(self, req, promote=True):
        req.ended = True
        self._policy.note_activity()
        req.conn.requests.pop(req.id, None)
        with self._cond:
            self._registry.pop((req.conn.id, req.id), None)
            self._scheduler.cancel(req.conn.id, req.id)
        if promote:
            self._promote(req.conn)

    def _promote(self, conn):
        """The head request ended: the next one becomes head and may render."""
        head = next(iter(conn.requests.values()), None)
        if head is not None:
            self._guarded(head, self._release, head)

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
                job = None if self._delivery_pending else self._scheduler.next(self._blocked)
                req = self._registry.get((job.conn, job.req)) if job else None
                if req is not None:
                    self._delivery_pending = True
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
