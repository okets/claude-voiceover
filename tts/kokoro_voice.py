#!/usr/bin/env python3
"""
Kokoro TTS engine for the claude-voiceover plugin.

Spawned detached by voiceover/speech.py as:
    uv run --project <plugin>/tts <plugin>/tts/kokoro_voice.py <voice_id> <text>

Direct usage:
    kokoro_voice.py '<text>' [--voice VOICE] [--stream]

This file is fully self-contained: it runs inside the tts/ dependency world
(kokoro-onnx, soundfile, numpy) and MUST NOT import from voiceover/. The
data-dir/lock resolution and audio playback are therefore duplicated inline.

Honors VOICEOVER_DRY_RUN: prints '[voiceover] <text>' to stderr and exits
without loading models, downloading anything, or producing audio.
"""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DRY_RUN = bool(os.environ.get("VOICEOVER_DRY_RUN"))

MODELS_DIR = Path.home() / ".kokoro-tts" / "models"
MODEL_FILE = MODELS_DIR / "kokoro-v1.0.onnx"
VOICES_FILE = MODELS_DIR / "voices-v1.0.bin"
MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

DEFAULT_VOICE = "am_echo"
DEFAULT_SAMPLE_RATE = 24000  # Kokoro default
DEFAULT_VOICE_SETTINGS = {"speed": 1.0, "lang": "en-us", "trim": True}

# Per-voice delivery settings for all 13 Kokoro voices.
VOICE_SETTINGS = {
    "af_alloy": {"speed": 1.1, "lang": "en-us", "trim": True},
    "af_river": {"speed": 1.1, "lang": "en-us", "trim": True},
    "af_sky": {"speed": 1.05, "lang": "en-us", "trim": True},
    "af_sarah": {"speed": 1.0, "lang": "en-us", "trim": True},
    "af_nicole": {"speed": 1.3, "lang": "en-us", "trim": True},
    "am_adam": {"speed": 1.0, "lang": "en-us", "trim": True},
    "am_echo": {"speed": 1.0, "lang": "en-us", "trim": True},
    "am_puck": {"speed": 0.94, "lang": "en-us", "trim": True},
    "am_michael": {"speed": 1.1, "lang": "en-us", "trim": True},
    "bf_emma": {"speed": 1.0, "lang": "en-us", "trim": True},
    "bm_daniel": {"speed": 1.3, "lang": "en-us", "trim": True},
    "bm_lewis": {"speed": 1.0, "lang": "en-us", "trim": True},
    "bm_george": {"speed": 1.2, "lang": "en-us", "trim": True},
}


def log(message):
    """Diagnostics go to stderr; stdout stays silent for the hook protocol."""
    print(message, file=sys.stderr)


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


# --- audio playback (inline port of the cross-platform player) --------------

def play_audio_file(file_path, timeout=30):
    if sys.platform == "darwin":
        return _play_macos(file_path, timeout)
    if sys.platform == "win32":
        return _play_windows(file_path, timeout)
    return _play_linux(file_path, timeout)


def _play_macos(file_path, timeout):
    try:
        subprocess.run(["afplay", file_path], check=True, timeout=timeout)
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _play_windows(file_path, timeout):
    try:
        import winsound
        winsound.PlaySound(file_path, winsound.SND_FILENAME)
        return True
    except Exception:
        return _play_windows_powershell(file_path, timeout)


def _play_windows_powershell(file_path, timeout):
    escaped = file_path.replace("'", "''")
    script = (
        "Add-Type -AssemblyName presentationCore; "
        "$p = New-Object System.Windows.Media.MediaPlayer; "
        "$p.Open([Uri]'" + escaped + "'); $p.Play(); "
        "Start-Sleep -Seconds " + str(min(timeout, 60))
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=timeout + 2,
        )
        return True
    except Exception:
        return False


def _play_linux(file_path, timeout):
    commands = (
        ["aplay", file_path],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path],
    )
    for command in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=timeout)
            return True
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            continue
    return False


# --- model management --------------------------------------------------------

def models_present():
    return MODEL_FILE.exists() and VOICES_FILE.exists()


def download_models():
    """Download kokoro-v1.0.onnx and voices-v1.0.bin to ~/.kokoro-tts/models."""
    log("[INSTALL] First run - downloading Kokoro models...")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for url, dest in ((MODEL_URL, MODEL_FILE), (VOICES_URL, VOICES_FILE)):
        if dest.exists():
            continue
        log("[DOWNLOAD] " + url)
        partial = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, partial)
        partial.replace(dest)
    log("[OK] Kokoro models installed")


def ensure_models():
    if models_present():
        return True
    try:
        download_models()
        return True
    except Exception as error:
        log("[ERROR] Model download failed: " + str(error))
        return False


# --- synthesis ---------------------------------------------------------------

def get_voice_settings(voice):
    """Per-voice settings with a sane default for unknown voices."""
    return VOICE_SETTINGS.get(voice, DEFAULT_VOICE_SETTINGS)


def play_samples(samples, sample_rate):
    """Write samples to a temp wav and play it under the tts lock."""
    import soundfile as sf

    duration = len(samples) / float(sample_rate) if sample_rate else 0.0
    create_tts_lock(duration)
    # delete=False + manual cleanup avoids Windows file-locking issues.
    tmp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    try:
        sf.write(tmp_path, samples, sample_rate)
        return play_audio_file(tmp_path, timeout=max(30, int(duration) + 10))
    finally:
        remove_tts_lock()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak_standard(kokoro, text, voice):
    """Standard non-streaming synthesis with lock coordination."""
    if check_tts_lock():
        return True  # another narration is playing - skip quietly
    settings = get_voice_settings(voice)
    log("[TTS] Speaking with " + voice + "...")
    samples, sample_rate = kokoro.create(
        text=text,
        voice=voice,
        speed=settings["speed"],
        lang=settings["lang"],
        trim=settings["trim"],
    )
    return play_samples(samples, sample_rate)


def speak_streaming(kokoro, text, voice):
    """Streaming synthesis; falls back to standard on any failure."""
    import asyncio
    import numpy as np

    if check_tts_lock():
        return True  # another narration is playing - skip quietly
    settings = get_voice_settings(voice)

    async def collect_stream():
        chunks = []
        sample_rate = DEFAULT_SAMPLE_RATE
        stream = kokoro.create_stream(
            text=text,
            voice=voice,
            speed=settings["speed"],
            lang=settings["lang"],
            trim=settings["trim"],
        )
        async for chunk in stream:
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                chunks.append(chunk[0])
                sample_rate = chunk[1]
            elif isinstance(chunk, np.ndarray):
                chunks.append(chunk)
            elif hasattr(chunk, "audio"):
                chunks.append(chunk.audio)
        return chunks, sample_rate

    log("[STREAM] Streaming with " + voice + "...")
    try:
        chunks, sample_rate = asyncio.run(collect_stream())
        if not chunks:
            log("[ERROR] No audio data received from stream")
            return False
        return play_samples(np.concatenate(chunks), sample_rate)
    except Exception as error:
        log("[ERROR] Streaming failed: " + str(error))
        log("[FALLBACK] Falling back to standard synthesis...")
        return speak_standard(kokoro, text, voice)


def speak_text(text, voice, use_streaming=False):
    try:
        if not ensure_models():
            return False
        from kokoro_onnx import Kokoro

        kokoro = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
        if use_streaming:
            return speak_streaming(kokoro, text, voice)
        return speak_standard(kokoro, text, voice)
    except Exception as error:
        log("[ERROR] Speech failed: " + str(error))
        return False


# --- CLI ---------------------------------------------------------------------

def normalize_voice(name):
    """Accept both bare ids ('af_sarah') and legacy 'kokoro-af_sarah' ids."""
    if name.startswith("kokoro-"):
        return name[len("kokoro-"):]
    return name


def parse_args(argv):
    """Return (voice, text, use_streaming), or None when there are no args."""
    if not argv:
        return None
    first = normalize_voice(argv[0])
    if first in VOICE_SETTINGS:
        # Hook call: kokoro_voice.py <voice_id> <text...>  (standard synthesis)
        return first, " ".join(argv[1:]).strip(), False
    # Direct call: kokoro_voice.py <text...> [--voice VOICE] [--stream]
    voice = DEFAULT_VOICE
    use_streaming = False
    text_parts = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--voice" and i + 1 < len(argv):
            voice = normalize_voice(argv[i + 1])
            i += 2
        elif arg == "--stream":
            use_streaming = True
            i += 1
        else:
            text_parts.append(arg)
            i += 1
    return voice, " ".join(text_parts).strip(), use_streaming


def print_usage():
    log("Kokoro TTS Voice Engine")
    log("Usage:")
    log("  Hook call:   kokoro_voice.py <voice_id> <text>")
    log("  Direct call: kokoro_voice.py '<text>' [--voice VOICE] [--stream]")
    log("")
    log("Available voices:")
    for voice_id in VOICE_SETTINGS:
        log("  " + voice_id)


def main():
    parsed = parse_args(sys.argv[1:])
    if parsed is None:
        print_usage()
        return 0
    voice, text, use_streaming = parsed
    if not text:
        log("[ERROR] No text to speak")
        return 1
    if DRY_RUN:
        print("[voiceover] " + text, file=sys.stderr)
        return 0
    return 0 if speak_text(text, voice, use_streaming) else 1


if __name__ == "__main__":
    sys.exit(main())
