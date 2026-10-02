"""Per-project engine leases: a session holds its project's engine while a
turn runs, so the model stays loaded for the next sentence and unloads once
nobody needs it.

Hooks (and, later, the deferred mod's bridge) WRITE leases; the engine READS them through
tts/engine_common.py (lease_is_live) and never deletes one. The shared
contract is only this: <project dir>/leases/<session slug>.json holding
{"expiry": unix seconds}. The expiry is a crash backstop, not a timer:
holders renew at every model step or tool hook and release at turn end.
Stdlib only; every function fails soft.
"""

import json
import os
import time
from pathlib import Path

from .projects import project_dir, session_slug

LEASE_TTL = 600.0            # a holder whose turn end always arrives (the deferred mod)
FALLBACK_LEASE_TTL = 120.0   # classic hooks: Stop does not fire on an interrupt

_LEASE_DIR_NAME = "leases"
_PRUNE_AFTER_SECONDS = 3600.0


def lease_dir(cwd=None) -> Path:
    """The project's lease directory, created on demand."""
    directory = project_dir(cwd) / _LEASE_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def _lease_path(cwd, session):
    slug = session_slug(session)
    return lease_dir(cwd) / (slug + ".json") if slug else None


def _expiry(path):
    try:
        return float(json.loads(path.read_text()).get("expiry"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def hold(cwd, session, ttl=LEASE_TTL) -> bool:
    """Hold (or renew) session's lease on the project's engine.

    Also deletes other sessions' leases that expired over an hour ago: they
    are long dead, and nobody else deletes them (the engine must not - it
    could race a renewal)."""
    path = _lease_path(cwd, session)
    if path is None:
        return False
    cutoff = time.time() - _PRUNE_AFTER_SECONDS
    for other in path.parent.glob("*.json"):
        expiry = _expiry(other) if other != path else None
        if expiry is not None and expiry < cutoff:
            try:
                other.unlink()
            except OSError:
                pass
    try:
        temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
        temp.write_text(json.dumps({"expiry": time.time() + ttl}))
        os.replace(str(temp), str(path))
        return True
    except OSError:
        return False


def release(cwd, session) -> None:
    """Let go of session's lease. The engine exits once none are left and
    its queue is empty."""
    path = _lease_path(cwd, session)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def any_live(cwd=None) -> bool:
    """True while any session holds an unexpired lease in the project."""
    now = time.time()
    return any((_expiry(path) or 0) > now for path in lease_dir(cwd).glob("*.json"))
