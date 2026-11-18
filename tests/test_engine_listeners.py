# tests/test_engine_listeners.py
"""Engine listeners: systemd adoption, self-bind, single-instance lock, token-checked TCP."""

import gc
import json
import os
import socket
import stat
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_server import adopt_systemd_listeners, bind_listeners, hold_single_instance
from engine_stub import SineSynth
from _engine_test_util import Raw, serve, start_line, stop_server

WORK = temp_dir("vo-listen-")
TOKEN = "ab" * 32


def tcp_server(**kwargs):
    listener = socket.socket(socket.AF_INET)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    server = serve(SineSynth(), [listener], token=TOKEN, **kwargs)
    return server, listener.getsockname()


def unauthorized_then_eof(address, first_line):
    c = Raw(address, tcp=True)
    if first_line is not None:
        c.send(first_line)
    event = c.read_event()
    return event.get("ev") == "error" and event.get("code") == "unauthorized" and c.closed_by_peer()


# --- T17: token over TCP -------------------------------------------------------------------
srv, addr = tcp_server()
check("TCP: no token (first line is not auth) -> unauthorized, then EOF",
      unauthorized_then_eof(addr, {"op": "status"}))
check("TCP: wrong token -> unauthorized, then EOF",
      unauthorized_then_eof(addr, {"op": "auth", "token": "cd" * 32}))
check("TCP: auth without a token field -> unauthorized, then EOF", unauthorized_then_eof(addr, {"op": "auth"}))
check("TCP: non-string token -> unauthorized, then EOF", unauthorized_then_eof(addr, {"op": "auth", "token": 5}))
check("TCP: hello first -> unauthorized, then EOF", unauthorized_then_eof(addr, {"op": "hello", "proto": 1}))

c = Raw(addr, tcp=True)
c.sock.sendall(b"".join(json.dumps(line).encode() + b"\n" for line in (
    {"op": "auth", "token": TOKEN}, {"op": "hello", "proto": 1},
    start_line(id="t1", text="Over tcp.", end=True))))
first = c.read_event()
rest = c.read_until_end("t1")
check("TCP: auth + hello + start in one write -> hello, then audio",
      first.ev == "hello" and rest[0].ev == "audio" and rest[-1]["reason"] == "complete", [first] + rest)
check("TCP_NODELAY is set on the accepted socket", srv.last_tcp_nodelay not in (None, 0), srv.last_tcp_nodelay)

# bytes pipelined after a bad token are never processed
calls_before = srv._synth.synth_calls
c = Raw(addr, tcp=True)
c.sock.sendall(json.dumps({"op": "auth", "token": "cd" * 32}).encode() + b"\n"
               + json.dumps({"op": "hello", "proto": 1}).encode() + b"\n"
               + json.dumps(start_line(id="x1", text="No.", end=True)).encode() + b"\n")
check("TCP: bad auth + hello + start in one write -> unauthorized, EOF, nothing processed",
      c.read_event().get("code") == "unauthorized" and c.closed_by_peer()
      and srv.snapshot()["requests"] == 0 and srv._synth.synth_calls == calls_before)
stop_server(srv)

# --- IPv6 (any non-Unix listener needs the token) ---------------------------------------------------
try:
    v6 = socket.socket(socket.AF_INET6)
    v6.bind(("::1", 0))
    v6.listen(8)
except OSError:
    v6 = None
if v6 is None:
    print("SKIP  IPv6 not available on this host")
else:
    srv = serve(SineSynth(), [v6], token=TOKEN)
    addr6 = v6.getsockname()[:2]
    check("IPv6: no token -> unauthorized, then EOF", unauthorized_then_eof(addr6, {"op": "hello", "proto": 1}))
    c = Raw(addr6, tcp=True)
    c.send({"op": "auth", "token": TOKEN})
    check("IPv6: right token works, and TCP_NODELAY is set", c.hello().ev == "hello"
          and srv.last_tcp_nodelay not in (None, 0))
    stop_server(srv)

# --- auth deadline: idle unauthenticated peers do not hold connection slots ------------------------------
listener = socket.socket(socket.AF_INET)
listener.bind(("127.0.0.1", 0))
listener.listen(8)
unix_path = str(WORK / "cap.sock")
cap_unix = socket.socket(socket.AF_UNIX)
cap_unix.bind(unix_path)
cap_unix.listen(8)
srv = serve(SineSynth(), [listener, cap_unix], token=TOKEN, max_connections=2, auth_timeout_s=0.3)
idle = [Raw(listener.getsockname(), tcp=True, timeout=3.0) for _ in range(2)]
time.sleep(0.1)
check("idle TCP peers fill the connection cap", srv.snapshot()["callers"] == 2)
check("an idle TCP peer is closed without a reply after the deadline",
      all(p.sock.recv(100) == b"" for p in idle))
check("a Unix client can connect once they are gone", Raw(unix_path).hello().ev == "hello")
stop_server(srv)

# --- no token on the Unix socket -----------------------------------------------------------------
unix_path = str(WORK / "plain.sock")
unix_listener = socket.socket(socket.AF_UNIX)
unix_listener.bind(unix_path)
unix_listener.listen(8)
srv = serve(SineSynth(), [unix_listener], token=TOKEN)
u = Raw(unix_path)
check("Unix socket: hello needs no token", u.hello().ev == "hello")
u.send({"op": "auth", "token": "whatever"})
u.send({"op": "status"})
check("Unix socket: an auth line is accepted and ignored", u.read_event().ev == "status")
stop_server(srv)

# --- bind_listeners (T22, stale socket) -----------------------------------------------------------
sock_path = WORK / "engine.sock"
sock_path.write_text("leftover")
listeners = bind_listeners(sock_path, None)
check("a leftover file at the socket path is replaced", len(listeners) == 1
      and stat.S_ISSOCK(sock_path.stat().st_mode))
check("T22: no port -> only the Unix socket", [s.family for s in listeners] == [socket.AF_UNIX])
check("socket mode is 0600", stat.S_IMODE(sock_path.stat().st_mode) == 0o600, oct(sock_path.stat().st_mode))
listeners[0].close()  # a bound-then-abandoned socket file, as after SIGKILL
again = bind_listeners(sock_path, None)
check("a stale socket file from a dead engine is replaced", len(again) == 1)
again[0].close()

probe = socket.socket()
probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]
probe.close()
with_port = bind_listeners(sock_path, port)
check("with a port: Unix socket plus 127.0.0.1:port",
      [s.family for s in with_port] == [socket.AF_UNIX, socket.AF_INET]
      and with_port[1].getsockname() == ("127.0.0.1", port), [s.getsockname() for s in with_port])
for s in with_port:
    s.close()

# --- bind failure leaves no open listener ------------------------------------------------------------------
taken = socket.socket()
taken.bind(("127.0.0.1", 0))
taken.listen(1)
raised = False
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    try:
        bind_listeners(sock_path, taken.getsockname()[1])
    except OSError:
        raised = True
    gc.collect()
leak_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
probe_unix = socket.socket(socket.AF_UNIX)
try:
    probe_unix.connect(str(sock_path))
    still_listening = True
except OSError:
    still_listening = False
probe_unix.close()
taken.close()
check("a taken TCP port raises, and the Unix socket is not left listening",
      raised and not still_listening and not leak_warnings, (raised, still_listening, leak_warnings))

# --- single instance --------------------------------------------------------------------------------
lock = WORK / "engine.lock"
first_fd = hold_single_instance(lock)
check("first hold returns an fd", isinstance(first_fd, int))
check("second hold returns None while the first is open", hold_single_instance(lock) is None)
os.close(first_fd)
second_fd = hold_single_instance(lock)
check("a hold succeeds once the first fd is closed", isinstance(second_fd, int))
os.close(second_fd)
try:
    hold_single_instance(WORK / "no-such-dir" / "engine.lock")
    other_errors_propagate = False
except FileNotFoundError:
    other_errors_propagate = True
check("an error other than 'already held' propagates", other_errors_propagate)


# --- systemd adoption ------------------------------------------------------------------------------
def in_child(body):
    """Run body() in a forked child; its True/False result comes back as the exit code."""
    pid = os.fork()
    if pid == 0:
        code = 2
        try:
            code = 0 if body() else 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) == 0


def adopt_in_child(pid_offset=0):
    def body():
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(WORK / ("sd%d.sock" % os.getpid())))
        listener.listen(4)
        os.dup2(listener.fileno(), 3)
        env = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid() + pid_offset), "OTHER": "x"}
        adopted = adopt_systemd_listeners(env)
        if pid_offset:
            return adopted is None and "LISTEN_FDS" not in env
        return (adopted is not None and len(adopted) == 1 and adopted[0].family == socket.AF_UNIX
                and "LISTEN_FDS" not in env and "LISTEN_PID" not in env and env["OTHER"] == "x")
    return in_child(body)


def adopt_tcp_in_child():
    def body():
        listener = socket.socket(socket.AF_INET)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        os.dup2(listener.fileno(), 3)
        adopted = adopt_systemd_listeners({"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())})
        return adopted[0].family == socket.AF_INET
    return in_child(body)


check("systemd: LISTEN_FDS=1 with our pid adopts fd 3 as one Unix socket, and unsets the variables",
      adopt_in_child())
check("systemd: an inherited TCP socket is recognised as AF_INET", adopt_tcp_in_child())
check("systemd: LISTEN_PID of another process -> None, variables still unset", adopt_in_child(pid_offset=1))
check("systemd: no variables -> None", adopt_systemd_listeners({}) is None)

report()
