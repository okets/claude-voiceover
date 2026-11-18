# tts/engine_paths.py
"""Where the shared Kokoro engine keeps its state, and the machine settings
that govern it. Stdlib only; imports no other engine module but engine_common.
"""

import json
import os
import secrets
import time
from pathlib import Path

import engine_common

DEFAULT_IDLE_SECONDS = 60.0
IDLE_SECONDS_WITH_PORT = 330.0
_PRIVATE_DIR, _PRIVATE_FILE = 0o700, 0o600


def engine_dir() -> Path:
    """<data>/engine, private to the user (mode 0700, enforced on every call)."""
    path = engine_common.data_dir() / "engine"
    path.mkdir(mode=_PRIVATE_DIR, parents=True, exist_ok=True)
    os.chmod(path, _PRIVATE_DIR)
    return path


def _file(name: str) -> Path:
    return engine_dir() / name


def socket_path() -> Path:
    return _file("engine.sock")


def lock_path() -> Path:
    return _file("engine.lock")


def start_lock_path() -> Path:
    return _file("start.lock")


def token_path() -> Path:
    return _file("token")


def pointer_path() -> Path:
    return _file("current.json")


def keep_off_path() -> Path:
    return _file("keep_off.json")


def log_path() -> Path:
    return _file("engine.log")


# --- token ------------------------------------------------------------------

def _new_token() -> str:
    return secrets.token_hex(32)


def _valid_token(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def _write_temp_token() -> Path:
    """A fresh token in a private temp file next to the real one."""
    temp = token_path().with_name("token.%d.tmp" % os.getpid())
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
    with os.fdopen(fd, "w") as handle:
        handle.write(_new_token())
    return temp


def _read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def read_or_create_token() -> str:
    """The shared secret. A new one appears complete and 0600 in one step (temp
    file + link); an existing file that is not a valid token is replaced."""
    path = token_path()
    temp = _write_temp_token()
    try:
        try:
            os.link(str(temp), str(path))
        except FileExistsError:
            existing = _read_token(path)
            if _valid_token(existing):
                return existing
            os.replace(str(temp), str(path))  # empty or garbage: repair atomically
        return _read_token(path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def rotate_token() -> str:
    """Replace the token with a fresh one."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
    return read_or_create_token()


# --- settings ---------------------------------------------------------------

def read_global_settings() -> dict:
    """The global settings file, read fresh each call; {} on any error."""
    try:
        with open(engine_common.data_dir() / "settings.json", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def engine_port():
    """The configured TCP port, or None when unset or out of range."""
    port = read_global_settings().get("engine_port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return port
    return None


def idle_unload_seconds() -> float:
    """engine_idle_s, else 330 s when a port is set, else 60 s."""
    idle = read_global_settings().get("engine_idle_s")
    if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle > 0:
        return float(idle)
    return IDLE_SECONDS_WITH_PORT if engine_port() is not None else DEFAULT_IDLE_SECONDS


# --- JSON state files -------------------------------------------------------

def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_json_atomic(path: Path, data: dict) -> None:
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    temp.write_text(json.dumps(data))
    os.replace(str(temp), str(path))


def read_pointer():
    """current.json: what the running engine published about itself, or None."""
    return _read_json(pointer_path())


def write_pointer(info: dict) -> None:
    _write_json_atomic(pointer_path(), info)


def read_keep_off():
    """The keep-off record while it is still in force, else None."""
    record = _read_json(keep_off_path())
    try:
        if record is not None and float(record["until"]) > time.time():
            return record
    except (KeyError, TypeError, ValueError):
        pass
    return None


def write_keep_off(seconds: float, reason: str) -> None:
    """Ask launchers not to start the engine for `seconds`."""
    _write_json_atomic(keep_off_path(), {"until": time.time() + seconds, "reason": reason})
