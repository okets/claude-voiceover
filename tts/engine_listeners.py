# tts/engine_listeners.py
"""The shared Kokoro engine's listening sockets: systemd socket activation, its own Unix
socket (plus 127.0.0.1:port), and the single-instance lock. Stdlib only."""

import fcntl
import os
import socket
from pathlib import Path
from typing import NamedTuple, Optional

import engine_paths

SYSTEMD_FIRST_FD = 3
SOCKET_MODE = 0o600
TCP_HOST = "127.0.0.1"
LISTEN_BACKLOG = 32
PROBE_TIMEOUT_S = 0.5


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
    """Bind the Unix socket (and 127.0.0.1:port); None when a socket at the path answers.

    The caller holds engine.lock, but an engine adopted from systemd may not (it serves
    regardless), so a socket file that still answers is never removed: another engine serves.
    A file that does not answer is a leftover and is replaced. A TCP port that cannot be bound
    is logged and skipped: narration needs only the Unix socket."""
    sock_path = Path(sock_path)
    if _answers(sock_path):
        return None
    listeners = [_bind_unix(sock_path)]
    tcp = _try_bind_tcp(port)
    return listeners + [tcp] if tcp is not None else listeners


def _answers(sock_path):
    """True when something accepts connections on the Unix socket at sock_path.
    A full backlog (timeout, would block) is a live listener too."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(PROBE_TIMEOUT_S)
    try:
        probe.connect(str(sock_path))
        return True
    except (socket.timeout, BlockingIOError):
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _try_bind_tcp(port):
    """127.0.0.1:port, or None when no port is set or it cannot be bound (logged)."""
    if port is None:
        return None
    try:
        return _bind_tcp(port)
    except OSError as error:
        engine_paths.append_log("cannot listen on %s:%d (%s); serving the Unix socket only"
                                % (TCP_HOST, port, error))
        return None


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


class Listening(NamedTuple):
    sockets: list
    adopted: bool          # systemd's sockets: their files outlive this process
    lock_fd: Optional[int]  # engine.lock, held for the process lifetime


def open_listeners(sock_path: Path, port, lock: Path):
    """Systemd's sockets, else our own; None when another engine already serves.

    engine.lock is taken either way, so a self-starter never mistakes systemd's socket for a
    leftover. Systemd guarantees one service instance, so an adopted engine serves even when
    the lock is held (logged)."""
    adopted = adopt_systemd_listeners()
    lock_fd = hold_single_instance(lock)
    if adopted is not None:
        if lock_fd is None:
            engine_paths.append_log("engine.lock is held by another engine; serving systemd's sockets anyway")
        return Listening(adopted, True, lock_fd)
    if lock_fd is None:
        return None
    sockets = bind_listeners(sock_path, port)
    if sockets is None:
        os.close(lock_fd)
        return None
    return Listening(sockets, False, lock_fd)
