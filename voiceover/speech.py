"""Speech dispatch for claude-voiceover.

speak() gates on settings, truncates, honors the TTS lock, then spawns the
resolved engine as a DETACHED subprocess (never waits):

    kokoro       -> uv run --project <plugin>/tts <plugin>/tts/kokoro_voice.py <voice> <text>
    macos-female -> python3 <plugin>/tts/macos_say.py --voice Samantha <text>
    macos-male   -> python3 <plugin>/tts/macos_say.py --voice Daniel <text>
    none         -> no-op

With env VOICEOVER_DRY_RUN set, speak()/play_sound() print
'[voiceover] <text>' to stderr instead of producing audio.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import process_utils
from .audio_player import play_audio_file
from .settings import (
    data_dir,
    get_setting,
    get_voice,
    is_tts_enabled,
    level_at_least,
    resolve_engine,
)
from .templates import truncate_to_words

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TTS_DIR = _PLUGIN_ROOT / "tts"
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

_LOCK_FILE_NAME = "tts.lock"
_FULL_TEXT_CAP = 6000

_MACOS_VOICES = {"macos-female": "Samantha", "macos-male": "Daniel"}


def lock_path() -> Path:
    """The TTS lock file; its content is an expiry unix timestamp."""
    return data_dir() / _LOCK_FILE_NAME


def speak(text, min_level="concise", cwd=None, interrupt=False, full=False) -> bool:
    """Say text aloud if settings allow. Never blocks, never raises.

    Returns True when the utterance was dispatched (or dry-printed) - callers
    that track what has been narrated (the prose tailer) advance their state
    only on True. full=True skips the word-budget truncation used for tool
    chatter; prose is spoken whole (hard-capped at _FULL_TEXT_CAP chars)."""
    try:
        if not text or not str(text).strip():
            return False
        if not is_tts_enabled(cwd) or not level_at_least(min_level, cwd):
            return False
        if full:
            message = str(text).strip()[:_FULL_TEXT_CAP]
        else:
            message = truncate_for_speech(str(text).strip())
        if _dry_run():
            _dry_print(message)
            return True
        if interrupt:
            stop_speech()
        elif _is_locked():
            return False
        return _dispatch(message, resolve_engine(cwd), cwd)
    except Exception:
        return False


def stop_speech() -> None:
    """Kill any TTS process we spawned - the lock owner's whole process
    group first, so its audio child dies too - and clear the lock."""
    try:
        process_utils.stop_all_tts(lock_path=lock_path())
    except Exception:
        pass
    _clear_lock()


def play_sound(name, cwd=None) -> None:
    """Play a bundled mp3 ping ('notification' | 'decide'), if allowed."""
    try:
        if not get_setting("notification_sounds", cwd):
            return
        if not level_at_least("quiet", cwd):
            return
        if _dry_run():
            _dry_print("sound: {}".format(name))
            return
        sound_file = _SOUNDS_DIR / "{}.mp3".format(name)
        if sound_file.is_file():
            play_audio_file(str(sound_file), timeout=10)
    except Exception:
        pass


def truncate_for_speech(text, limit: int = 220) -> str:
    """Shorten text for speaking; limit is a soft character budget."""
    max_words = max(5, limit // 6)
    return truncate_to_words(text, max_words=max_words)


# ---------------------------------------------------------------------------
# Engine dispatch
# ---------------------------------------------------------------------------

def _dispatch(message, engine, cwd) -> None:
    if engine == "kokoro":
        command = [
            "uv", "run", "--project", str(_TTS_DIR),
            str(_TTS_DIR / "kokoro_voice.py"),
            "--voice", get_voice(cwd), "--stream", message,
        ]
    elif engine in _MACOS_VOICES:
        command = [
            "python3", str(_TTS_DIR / "macos_say.py"),
            "--voice", _MACOS_VOICES[engine], message,
        ]
    else:  # "none" or unknown
        return False
    # The engine owns the lock lifecycle (atomic claim -> speak -> remove).
    # Creating the lock here would race the engine's own claim and make it
    # skip: the caller only ever READS the lock (in _is_locked).
    return _spawn_detached(command)


def _spawn_detached(command) -> bool:
    """Start the engine process without ever waiting on it."""
    try:
        kwargs = {
            "cwd": str(_TTS_DIR),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lock file (read/clear only - the engine subprocess creates and removes it)
# ---------------------------------------------------------------------------

def _is_locked() -> bool:
    """True while a previous speak()'s expiry timestamp is in the future."""
    lock = lock_path()
    try:
        if not lock.exists():
            return False
        with open(lock, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
        try:
            expiry = float(json.loads(raw).get("expiry"))
        except Exception:
            expiry = float(raw)  # pre-1.1 locks were a bare float
        if time.time() < expiry:
            return True
    except (OSError, ValueError):
        pass
    _clear_lock()  # expired or unreadable
    return False


def _clear_lock() -> None:
    try:
        lock = lock_path()
        if lock.exists():
            lock.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def _dry_run() -> bool:
    return bool(os.environ.get("VOICEOVER_DRY_RUN"))


def _dry_print(text) -> None:
    try:
        print("[voiceover] {}".format(text), file=sys.stderr)
    except Exception:
        pass
