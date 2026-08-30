"""Cross-platform audio playback for claude-voiceover.

Straight port of the proven legacy player:
- macOS:   afplay (native)
- Windows: pygame, winsound (WAV), then PowerShell MediaPlayer
- Linux:   aplay (WAV), pygame, then ffplay
"""

import subprocess
import sys
import time
from pathlib import Path


def play_audio_file(file_path: str, timeout: int = 30) -> bool:
    """Play an audio file with the platform-appropriate method."""
    if sys.platform == "darwin":
        return _play_macos(file_path, timeout)
    if sys.platform == "win32":
        return _play_windows(file_path, timeout)
    return _play_linux(file_path, timeout)


def _play_macos(file_path: str, timeout: int) -> bool:
    try:
        subprocess.run(["afplay", file_path], check=True, timeout=timeout)
        return True
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
        return False


def _play_windows(file_path: str, timeout: int) -> bool:
    if _play_with_pygame(file_path, timeout):
        return True
    if Path(file_path).suffix.lower() == ".wav" and _play_with_winsound(file_path):
        return True
    return _play_with_powershell(file_path, timeout)


def _play_linux(file_path: str, timeout: int) -> bool:
    if Path(file_path).suffix.lower() == ".wav" and _play_with_aplay(file_path, timeout):
        return True
    if _play_with_pygame(file_path, timeout):
        return True
    return _play_with_ffplay(file_path, timeout)


def _play_with_pygame(file_path: str, timeout: int) -> bool:
    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.music.load(file_path)
        pygame.mixer.music.play()
        start = time.time()
        while pygame.mixer.music.get_busy():
            if time.time() - start > timeout:
                pygame.mixer.music.stop()
                break
            time.sleep(0.1)
        return True
    except Exception:
        return False


def _play_with_winsound(file_path: str) -> bool:
    try:
        import winsound
        winsound.PlaySound(file_path, winsound.SND_FILENAME)
        return True
    except Exception:
        return False


def _play_with_powershell(file_path: str, timeout: int) -> bool:
    try:
        escaped_path = file_path.replace("'", "''")
        ps_cmd = (
            "Add-Type -AssemblyName presentationCore\n"
            "$mediaPlayer = New-Object System.Windows.Media.MediaPlayer\n"
            "$mediaPlayer.Open([Uri]'{path}')\n"
            "$mediaPlayer.Play()\n"
            "Start-Sleep -Seconds {seconds}\n"
        ).format(path=escaped_path, seconds=min(timeout, 10))
        subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True,
            timeout=timeout + 2,
        )
        return True
    except Exception:
        return False


def _play_with_aplay(file_path: str, timeout: int) -> bool:
    try:
        subprocess.run(["aplay", file_path], check=True, timeout=timeout)
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _play_with_ffplay(file_path: str, timeout: int) -> bool:
    try:
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", file_path],
            capture_output=True,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: audio_player.py <audio_file>")
        sys.exit(1)
    audio_file = sys.argv[1]
    print("Playing: {}".format(audio_file))
    ok = play_audio_file(audio_file)
    print("Playback {}".format("succeeded" if ok else "failed"))
    sys.exit(0 if ok else 1)
