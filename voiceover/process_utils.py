"""Cross-platform TTS process management for claude-voiceover.

Kills ONLY processes this plugin spawned. The legacy version ran a bare
``pkill -f say``, which matched any process whose command line contained
the substring "say" — that is fixed here: Unix patterns match our engine
scripts (kokoro_voice.py / macos_say.py) and the exact ``say -v <voice>``
invocations macos_say.py produces.
"""

import json
import os
import signal
import subprocess
import sys

# Full-command patterns for processes we spawn (pkill -f / psutil cmdline).
TTS_PROCESS_PATTERNS = [
    "kokoro_voice.py",
    "macos_say.py",
    "tts_controller.py",
    # The `say` children macos_say.py launches (contract voices only) —
    # anchored on the voice flag so unrelated processes never match.
    "say -v Samantha",
    "say -v Daniel",
    # kokoro's temp-wav playback child - anchored on .wav so a user's own
    # afplay of music etc. never matches (same tradeoff as tts_controller).
    "afplay .*\\.wav",
]


def stop_all_tts(lock_path=None) -> None:
    """Stop every TTS process this plugin may have spawned.

    When lock_path is given, the engine PID recorded in the lock is killed as
    a whole process GROUP first - taking down its afplay/say audio child too,
    so no ghost audio keeps playing under the next narration."""
    if lock_path is not None:
        _kill_lock_owner_group(lock_path)
    kill_processes_by_pattern(TTS_PROCESS_PATTERNS)


def _kill_lock_owner_group(lock_path) -> None:
    """POSIX only: engines run as session leaders, so PID == PGID. The PID is
    verified against our engine names before signalling, in case it was
    recycled. Windows relies on the pattern kill below."""
    if sys.platform == "win32":
        return
    try:
        raw = lock_path.read_text().strip()
        pid = int(json.loads(raw).get("pid"))
        probe = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, timeout=2)
        command = probe.stdout.strip()
        if "kokoro_voice.py" in command or "macos_say.py" in command:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


def kill_processes_by_pattern(patterns, timeout: int = 2) -> None:
    """Kill processes whose command line matches any of the patterns."""
    if sys.platform == "win32":
        _kill_windows(patterns)
    else:
        _kill_unix(patterns, timeout)


def _kill_unix(patterns, timeout) -> None:
    """macOS/Linux: pkill -f per pattern, failing silently."""
    for pattern in patterns:
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                timeout=timeout,
            )
        except Exception:
            pass


def _kill_windows(patterns) -> None:
    """Windows: match command lines via psutil when available."""
    try:
        import psutil
    except ImportError:
        return
    try:
        lowered = [p.lower() for p in patterns]
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if any(pattern in cmdline for pattern in lowered):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                pass
    except Exception:
        pass


if __name__ == "__main__":
    print("Platform: {}".format(sys.platform))
    print("Stopping voiceover TTS processes...")
    stop_all_tts()
    print("Done")
