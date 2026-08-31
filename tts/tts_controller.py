#!/usr/bin/env python3
"""
TTS kill switch for the claude-voiceover plugin.

Usage: python3 tts_controller.py stop

Stops any in-flight narration by killing ONLY our own engine processes
(kokoro_voice.py / macos_say.py, plus the `say -v` and afplay-of-a-wav lines
they spawn) - never a bare `pkill -f say` - and clears the tts.lock so the
next narration is not blocked by a lock whose playback was just killed.
Stdlib-only; runs under plain python3.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

# Extended regexes for `pkill -f`, matching only processes we spawned.
UNIX_PATTERNS = [
    r"kokoro_voice\.py",
    r"macos_say\.py",
    r"(^|[/ ])say -v ",   # the `say` line macos_say.py spawns
    r"afplay .*\.wav",    # kokoro playback of its temp wav
]

# Windows never runs `say`/afplay; only our python engine scripts matter.
WINDOWS_PATTERNS = [
    r"kokoro_voice\.py",
    r"macos_say\.py",
]


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


def _lock_owner_pid():
    """PID recorded in the lock by the engine that owns it, if any."""
    try:
        raw = (data_dir() / "tts.lock").read_text().strip()
        return int(json.loads(raw).get("pid"))
    except Exception:
        return None


def _kill_lock_owner_group():
    """Kill the owning engine's whole process group - the engine plus its
    afplay/say audio child - so no ghost audio outlives an interrupt.

    Engines are spawned with start_new_session=True, so PID == PGID. The PID
    is verified against our engine names before signalling, in case it was
    recycled by an unrelated process."""
    pid = _lock_owner_pid()
    if not pid or sys.platform == "win32":
        return
    try:
        probe = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, timeout=2)
        command = probe.stdout.strip()
        if "kokoro_voice.py" not in command and "macos_say.py" not in command:
            return
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


def remove_tts_lock():
    try:
        lock_file = data_dir() / "tts.lock"
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


# --- process killing ---------------------------------------------------------

def _stop_unix():
    for pattern in UNIX_PATTERNS:
        try:
            subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=2)
        except Exception:
            pass


def _stop_windows():
    clauses = " -or ".join(
        "$_.CommandLine -match '" + pattern + "'" for pattern in WINDOWS_PATTERNS
    )
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and (" + clauses + ") } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def stop_tts():
    """Stop all voiceover TTS playback immediately."""
    _kill_lock_owner_group()
    if sys.platform == "win32":
        _stop_windows()
    else:
        _stop_unix()
    remove_tts_lock()


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "stop"
    if command != "stop":
        print("Usage: tts_controller.py stop", file=sys.stderr)
        return 1
    stop_tts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
