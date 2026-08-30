#!/usr/bin/env python3
"""
macOS `say` engine for the claude-voiceover plugin.

Spawned detached by voiceover/speech.py as:
    python3 <plugin>/tts/macos_say.py --voice <Name> <text>

Collapses the three legacy macos_*_tts.py scripts into one stdlib-only script
(no uv header, no python-dotenv). Text is fed to `say` via stdin, so narration
text never appears in the process command line and cannot be mistaken for a
`say` flag.

Honors VOICEOVER_DRY_RUN: prints '[voiceover] <text>' to stderr, no audio.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

DRY_RUN = bool(os.environ.get("VOICEOVER_DRY_RUN"))
DEFAULT_VOICE = "Samantha"
WORDS_PER_SECOND = 3.0  # macOS `say` default rate is ~180 wpm


# --- data dir / tts.lock (inline: may not import voiceover.settings) --------

def data_dir():
    """$VOICEOVER_DATA_DIR override if set, else ~/.claude-voiceover. Created on demand."""
    root = os.environ.get("VOICEOVER_DATA_DIR")
    path = Path(root) if root else Path.home() / ".claude-voiceover"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def tts_lock_path():
    return data_dir() / "tts.lock"


def check_tts_lock():
    """True when another narration holds the lock (this one should skip)."""
    lock_file = tts_lock_path()
    try:
        if not lock_file.exists():
            return False
        end_time = float(lock_file.read_text().strip())
        if time.time() < end_time:
            return True
        lock_file.unlink()  # expired
    except (ValueError, OSError):
        try:
            lock_file.unlink()  # unreadable -> stale
        except OSError:
            pass
    return False


def create_tts_lock(duration):
    """Write the expected playback end time so concurrent narrations skip."""
    try:
        tts_lock_path().write_text(str(time.time() + duration))
    except OSError:
        pass


def remove_tts_lock():
    try:
        lock_file = tts_lock_path()
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


# --- speech ------------------------------------------------------------------

def estimate_duration(text):
    """Rough playback duration for the lock expiry, in seconds."""
    words = len(text.split())
    return max(1.5, words / WORDS_PER_SECOND + 0.8)


def speak(voice, text):
    if check_tts_lock():
        return True  # another narration is playing - skip quietly
    create_tts_lock(estimate_duration(text))
    try:
        subprocess.run(["say", "-v", voice], input=text, text=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as error:
        print("[ERROR] say failed: " + str(error), file=sys.stderr)
        return False
    finally:
        remove_tts_lock()


# --- CLI ---------------------------------------------------------------------

def parse_args(argv):
    """Manual parse so narration text may safely start with a dash."""
    voice = DEFAULT_VOICE
    text_parts = []
    i = 0
    while i < len(argv):
        if argv[i] == "--voice" and i + 1 < len(argv):
            voice = argv[i + 1]
            i += 2
        else:
            text_parts.append(argv[i])
            i += 1
    return voice, " ".join(text_parts).strip()


def main():
    voice, text = parse_args(sys.argv[1:])
    if not text:
        print("Usage: macos_say.py --voice <Name> <text>", file=sys.stderr)
        return 1
    if DRY_RUN:
        print("[voiceover] " + text, file=sys.stderr)
        return 0
    if sys.platform != "darwin":
        print("[ERROR] macos_say.py only works on macOS", file=sys.stderr)
        return 1
    return 0 if speak(voice, text) else 1


if __name__ == "__main__":
    sys.exit(main())
