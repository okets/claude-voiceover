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

import json
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


def _parse_lock_expiry(raw):
    """Lock content is JSON {"expiry", "pid"}; older locks were a bare float."""
    try:
        return float(json.loads(raw).get("expiry"))
    except Exception:
        try:
            return float(raw)
        except Exception:
            return None


def try_claim_lock(duration):
    """Atomically claim the TTS lock. True = we own it and may speak.

    O_CREAT|O_EXCL guarantees that of two engines racing, exactly one wins;
    the loser skips quietly. The lock stores our PID so stop_speech() can
    kill this engine's whole process group (including its audio child)."""
    lock_file = tts_lock_path()
    payload = json.dumps({"expiry": time.time() + duration, "pid": os.getpid()})
    # Write payload to a private temp file, then os.link() it into place:
    # the lock appears WITH its content in one atomic step, so a racing
    # claimer can never observe an empty lock and judge it stale.
    tmp_file = lock_file.with_name("tts.lock.{}".format(os.getpid()))
    try:
        tmp_file.write_text(payload)
    except OSError:
        return False
    try:
        for _ in range(2):
            try:
                os.link(str(tmp_file), str(lock_file))
                return True
            except FileExistsError:
                try:
                    expiry = _parse_lock_expiry(lock_file.read_text().strip())
                except OSError:
                    expiry = None
                if expiry is not None and time.time() < expiry:
                    return False  # someone else is speaking
                try:
                    lock_file.unlink()  # stale - retry the atomic claim once
                except OSError:
                    return False
            except OSError:
                return False
        return False
    finally:
        try:
            tmp_file.unlink()
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
    if not try_claim_lock(estimate_duration(text)):
        return True  # another narration is playing - skip quietly
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
