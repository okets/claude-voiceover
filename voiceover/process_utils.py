"""Cross-platform TTS process management for claude-voiceover.

Kills ONLY processes this plugin spawned. The legacy version ran a bare
``pkill -f say``, which matched any process whose command line contained
the substring "say" — that is fixed here: Unix patterns match our engine
scripts (kokoro_voice.py / macos_say.py) and the exact ``say -v <voice>``
invocations macos_say.py produces.
"""

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
]


def stop_all_tts() -> None:
    """Stop every TTS process this plugin may have spawned."""
    kill_processes_by_pattern(TTS_PROCESS_PATTERNS)


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
