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

from . import process_utils, spool
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
from .debuglog import log as _log

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TTS_DIR = _PLUGIN_ROOT / "tts"
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

_LOCK_FILE_NAME = "tts.lock"
_FULL_TEXT_CAP = 6000

_MACOS_VOICES = {"macos-female": "Samantha", "macos-male": "Daniel"}


def lock_path() -> Path:
    """The TTS lock file; its content is an expiry unix timestamp."""
    return data_dir() / _LOCK_FILE_NAME


def _gate_and_truncate(text, min_level, cwd, full):
    """The message to speak, or None when it is empty or gated out.

    Shared by speak() and enqueue_speech() so the gating rules and the
    truncation contract have exactly one definition. Callers do their own
    logging - they log under different tags - and their own downstream work.
    """
    if not text or not str(text).strip():
        return None
    if not is_tts_enabled(cwd) or not level_at_least(min_level, cwd):
        return None
    if full:
        return str(text).strip()[:_FULL_TEXT_CAP]
    return truncate_for_speech(str(text).strip())


def speak(text, min_level="concise", cwd=None, interrupt=False, full=False) -> bool:
    """Say text aloud if settings allow. Never blocks, never raises.

    Returns True when the utterance was dispatched (or dry-printed) - callers
    that track what has been narrated (the prose tailer) advance their state
    only on True. full=True skips the word-budget truncation used for tool
    chatter; prose is spoken whole (hard-capped at _FULL_TEXT_CAP chars)."""
    try:
        if not text or not str(text).strip():
            return False
        _log("speak", "req min=%s int=%s chars=%d :: %.60s" % (
            min_level, interrupt, len(str(text)), str(text).replace("\n", " ")), cwd)
        message = _gate_and_truncate(text, min_level, cwd, full)
        if message is None:
            _log("speak", "gated by level/enabled", cwd)
            return False
        if _dry_run():
            _dry_print(message)
            return True
        if interrupt:
            stop_speech()
        elif _is_locked():
            _log("speak", "skipped: lock held", cwd)
            return False
        dispatched = _dispatch(message, resolve_engine(cwd), cwd)
        _log("speak", "dispatch=%s engine=%s" % (dispatched, resolve_engine(cwd)), cwd)
        return dispatched
    except Exception:
        return False


def enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None) -> bool:
    """Queue text to be spoken. True when it was queued.

    The queued counterpart of speak(): identical gating and identical
    truncation, but it never fails because audio is already playing. That is
    the whole point - a caller that tracks what has been narrated (the prose
    tailer) can advance its cursor the moment this returns True, because a
    queued utterance is guaranteed to be spoken.
    """
    try:
        if not text or not str(text).strip():
            return False
        _log("queue", "req min=%s chars=%d :: %.60s" % (
            min_level, len(str(text)), str(text).replace("\n", " ")), cwd)
        message = _gate_and_truncate(text, min_level, cwd, full)
        if message is None:
            _log("queue", "gated by level/enabled", cwd)
            return False
        engine = resolve_engine(cwd)
        if engine == "none":
            return False
        # Resolve the voice for THIS engine, exactly as _dispatch does: the
        # macOS engines take a `say -v <Name>` voice, and get_voice() only
        # ever returns a kokoro id.
        voice = _MACOS_VOICES.get(engine) or get_voice(cwd)
        queued = spool.enqueue(message, engine, voice, session=session)
        _log("queue", "queued=%s pending=%d chars=%d :: %.60s" % (
            queued, spool.pending_count(), len(message),
            message.replace("\n", " ")), cwd)
        return queued
    except Exception:
        return False


def ensure_drainer(cwd=None) -> bool:
    """Start an engine draining the spool unless one is already speaking.

    Called unconditionally by every narrator hook path, so a spool left
    behind by a crashed engine is picked up by the next hook rather than
    sitting there. Racing hooks are harmless: the engine's own atomic lock
    claim admits exactly one drainer.
    """
    try:
        if spool.pending_count() == 0:
            return False
        if lock_is_live():
            return False  # a drainer is already working through the queue
        engine = resolve_engine(cwd)
        if engine == "kokoro":
            command = [
                "uv", "run", "--project", str(_TTS_DIR),
                str(_TTS_DIR / "kokoro_voice.py"), "--drain",
            ]
        elif engine in _MACOS_VOICES:
            command = ["python3", str(_TTS_DIR / "macos_say.py"), "--drain"]
        else:
            return False
        spawned = _spawn_detached(command)
        _log("queue", "drainer spawned=%s engine=%s pending=%d" % (
            spawned, engine, spool.pending_count()), cwd)
        return spawned
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

def _lock_expiry():
    """The lock's expiry timestamp, or None when there is no readable lock.

    Lock content is JSON {"expiry", "pid"}; pre-1.1 locks were a bare float.
    """
    try:
        raw = lock_path().read_text().strip()
    except OSError:
        return None
    try:
        return float(json.loads(raw).get("expiry"))
    except Exception:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


def lock_is_live() -> bool:
    """True while an unexpired lock exists. Read-only: never deletes anything.

    ensure_drainer() asks this from a HOOK, about a lock a live engine holds.
    The engine side already established the rule - drain() never removes a
    lock it does not own - and it applies just as much from outside: a hook
    that deletes a working drainer's lock lets a rival start beside it.
    """
    expiry = _lock_expiry()
    return expiry is not None and time.time() < expiry


def _is_locked() -> bool:
    """True while a previous speak()'s expiry timestamp is in the future.

    Clears an expired or unreadable lock as a side effect. That is
    pre-existing behaviour on speak()'s non-narrator path, where the caller
    IS the only speaker; the queue path uses the read-only lock_is_live().
    """
    if lock_is_live():
        return True
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
