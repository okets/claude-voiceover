#!/usr/bin/env python3
"""
Kokoro TTS engine for the claude-voiceover plugin.

Spawned detached by voiceover/speech.py as:
    uv run --project <plugin>/tts <plugin>/tts/kokoro_voice.py <voice_id> <text>

Direct usage:
    kokoro_voice.py '<text>' [--voice VOICE] [--stream]

This file runs inside the tts/ dependency world (kokoro-onnx, soundfile,
numpy) and MUST NOT import from voiceover/. The data-dir, lock and spool
helpers therefore come from tts/engine_common.py, a stdlib-only sibling
shared with macos_say.py; audio playback stays duplicated inline.

Honors VOICEOVER_DRY_RUN: prints '[voiceover] <text>' to stderr and exits
without loading models, downloading anything, or producing audio.
"""

import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from engine_common import Playback, ProcessPlayback, data_dir, remove_tts_lock, try_claim_lock, update_lock_expiry

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


def play_chunk(samples, sample_rate, root):
    """Play one streamed chunk; extends the lock, never removes it."""
    import soundfile as sf

    duration = len(samples) / float(sample_rate) if sample_rate else 0.0
    update_lock_expiry(root, duration + 20.0)  # this chunk plus headroom for the next
    tmp_file = tempfile.NamedTemporaryFile(prefix="voiceover-oneshot-", suffix=".wav", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    try:
        sf.write(tmp_path, samples, sample_rate)
        return play_audio_file(tmp_path, timeout=max(30, int(duration) + 10))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def play_samples(samples, sample_rate):
    """Write samples to a temp wav and play it under the tts lock."""
    import soundfile as sf

    duration = len(samples) / float(sample_rate) if sample_rate else 0.0
    update_lock_expiry(data_dir(), duration)
    # delete=False + manual cleanup avoids Windows file-locking issues.
    tmp_file = tempfile.NamedTemporaryFile(prefix="voiceover-oneshot-", suffix=".wav", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    try:
        sf.write(tmp_path, samples, sample_rate)
        return play_audio_file(tmp_path, timeout=max(30, int(duration) + 10))
    finally:
        remove_tts_lock(data_dir())
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak_standard(kokoro, text, voice):
    """Standard non-streaming synthesis with lock coordination."""
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


def stream_chunks(kokoro, text, voice, root) -> bool:
    """Synthesize in chunks and play each one as it arrives. True if any played.

    Owns no lock lifecycle: play_chunk() refreshes the expiry per chunk and
    never removes it, so this is safe both for the single-utterance path and
    inside the drain loop, which holds one lock across many items. Raises on
    a synthesis failure - the caller chooses its own fallback.
    """
    import asyncio
    import numpy as np

    settings = get_voice_settings(voice)

    async def synth_and_play():
        stream = kokoro.create_stream(
            text=text,
            voice=voice,
            speed=settings["speed"],
            lang=settings["lang"],
            trim=settings["trim"],
        )
        played = False
        async for chunk in stream:
            samples, sample_rate = None, DEFAULT_SAMPLE_RATE
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                samples, sample_rate = chunk[0], chunk[1]
            elif isinstance(chunk, np.ndarray):
                samples = chunk
            elif hasattr(chunk, "audio"):
                samples = chunk.audio
            if samples is None or not len(samples):
                continue
            play_chunk(samples, sample_rate, root)
            played = True
        return played

    log("[STREAM] Streaming with " + voice + "...")
    return asyncio.run(synth_and_play())


def speak_streaming(kokoro, text, voice):
    """Streamed synthesis with INCREMENTAL playback, for a single utterance.

    Each chunk plays as soon as it is synthesized, so long narration starts
    speaking within seconds instead of staying silent while the whole text
    renders (the silence used to outlive the narration: the next turn's
    interrupt killed the engine before a single word was heard). Chunks are
    sequential - a short breath between sentences, like a human narrator.
    Falls back to standard synthesis on any failure.

    This path OWNS the lock for exactly one utterance, so it releases it at
    the end. The drain loop must not use it: it holds one lock across many
    items and would end after the first.
    """
    try:
        return stream_chunks(kokoro, text, voice, data_dir())
    except Exception as error:
        log("[ERROR] Streaming failed: " + str(error))
        log("[FALLBACK] Falling back to standard synthesis...")
        return speak_standard(kokoro, text, voice)
    finally:
        remove_tts_lock(data_dir())


def playback_command(path):
    """The player for one sentence's wav (macOS/Linux), as an argv Popen can
    poll and kill."""
    if sys.platform == "darwin":
        return ["afplay", path]
    if shutil.which("aplay"):
        return ["aplay", "-q", path]
    return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]


class WinsoundPlayback(Playback):
    """Windows: play in-process and asynchronously, so a stop is instant and
    no player process can outlive the engine. Done once the clip's length
    has passed."""

    def __init__(self, path, duration):
        import winsound

        self._winsound, self._path = winsound, Path(path)
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        self._ends = time.time() + duration

    def poll(self) -> bool:
        if time.time() < self._ends:
            return False
        self._remove()
        return True

    def kill(self) -> None:
        try:
            self._winsound.PlaySound(None, 0)
        except Exception:
            pass
        self._remove()

    def _remove(self):
        try:
            self._path.unlink()
        except OSError:
            pass


def play_file(path, duration):
    """Start playing one sentence's wav; the Playback polls and kills it."""
    if sys.platform == "win32":
        return WinsoundPlayback(path, duration)
    process = subprocess.Popen(playback_command(str(path)),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ProcessPlayback(process, cleanup=path)


class KokoroPlayback(Playback):
    """One sentence: rendering in the worker thread, then playing.

    The drain loop polls it from the moment begin() returns, so a stop
    request or a lock refresh never waits for synthesis - a sentence stopped
    while it renders never makes a sound. A failed synthesis raises from
    poll(), which skips the rest of the item.
    """

    def __init__(self, speaker, future, path, voice, upcoming):
        self._speaker, self._future, self._path = speaker, future, path
        self._voice, self._upcoming = voice, upcoming
        self._player = None

    def poll(self) -> bool:
        if self._player is None:
            if not self._future.done():
                return False
            samples, sample_rate = self._future.result()
            if self._upcoming:
                self._speaker.prefetch(self._upcoming, self._voice)
            self._player = self._speaker.play(self._path, samples, sample_rate)
            return False
        return self._player.poll()

    def kill(self) -> None:
        if self._player is not None:
            self._player.kill()


class KokoroSpeaker:
    """Plays sentences through a loaded Kokoro model, synthesizing ahead.

    A worker thread renders the next sentence while the current one plays
    (onnxruntime releases the GIL): within an item through begin()'s
    `upcoming`, across items through prefetch(). A rendering that does not
    match the sentence actually begun next is discarded.
    """

    def __init__(self, kokoro, play_dir):
        self._kokoro = kokoro
        self._play_dir = Path(play_dir)
        self._play_dir.mkdir(parents=True, exist_ok=True)
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._ahead = {}
        self._count = 0
        cutoff = time.time() - 60  # wavs a killed or Windows-held player left behind
        for old in self._play_dir.glob("*.wav"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def warm_up(self):
        """Pay the first synthesis' one-time cost now, not on a real sentence."""
        self._render("Ready.", DEFAULT_VOICE).result()

    def prefetch(self, sentence, voice):
        """Start rendering a sentence the next item will begin with."""
        voice = voice or DEFAULT_VOICE
        if (sentence, voice) not in self._ahead:
            self._ahead[(sentence, voice)] = self._render(sentence, voice)

    def begin(self, sentence, voice, upcoming=None):
        voice = voice or DEFAULT_VOICE
        future = self._ahead.pop((sentence, voice), None) or self._render(sentence, voice)
        self._ahead.clear()  # anything else rendered ahead belongs to an abandoned item
        self._count += 1
        path = self._play_dir / ("%d-%d.wav" % (os.getpid(), self._count))
        return KokoroPlayback(self, future, path, voice, upcoming)

    def play(self, path, samples, sample_rate):
        """Write one rendered sentence and start playing it."""
        import soundfile as sf

        sf.write(str(path), samples, sample_rate)
        duration = len(samples) / float(sample_rate) if sample_rate else 0.0
        return play_file(path, duration)

    def _render(self, sentence, voice):
        settings = get_voice_settings(voice)
        return self._worker.submit(
            self._kokoro.create, text=sentence, voice=voice, speed=settings["speed"],
            lang=settings["lang"], trim=settings["trim"])


def speak_text(text, voice, use_streaming=False):
    try:
        if not ensure_models():
            return False
        # Claim BEFORE the slow model load: callers and sibling engines must
        # see this narration during the multi-second spin-up, not only once
        # playback begins. play_samples() refreshes the expiry with the real
        # audio duration and removes the lock when playback ends.
        if not try_claim_lock(data_dir(), 10.0 + 0.4 * len(text.split())):
            return True  # another narration is playing - skip quietly
        try:
            from kokoro_onnx import Kokoro

            kokoro = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
            if use_streaming:
                return speak_streaming(kokoro, text, voice)
            return speak_standard(kokoro, text, voice)
        except Exception:
            remove_tts_lock(data_dir())
            raise
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
    if "--drain" in sys.argv[1:]:
        from engine_common import DryRunSpeaker, drain, drain_root

        root = drain_root(sys.argv)
        if root is None:
            log("Usage: kokoro_voice.py --drain <project dir>")
            return 0
        if DRY_RUN:
            return drain(root, DryRunSpeaker, {"kokoro"})

        def open_speaker():
            """Load the model ONCE, and only after the lock is ours."""
            if not ensure_models():
                return None
            try:
                from kokoro_onnx import Kokoro

                speaker = KokoroSpeaker(Kokoro(str(MODEL_FILE), str(VOICES_FILE)), root / "play")
                speaker.warm_up()
                return speaker
            except Exception as error:
                log("[ERROR] Model load failed: " + str(error))
                return None

        return drain(root, open_speaker, {"kokoro"})

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
