#!/usr/bin/env python3
"""
Phonon TTS engine for the claude-voiceover plugin (Gradium's on-device TTS, `ptts`).

Spawned detached by voiceover/speech.py as:
    uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py --drain <project dir>
    uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py --voice <name> --stream <text>

Also:
    phonon_voice.py --install            download and load the model, exit 0 (1 on failure)
    phonon_voice.py --fetch-voice <name> load the model and that voice, exit 0 (1 if it cannot load)

This file runs inside the tts-phonon/ dependency world (ptts, sounddevice) and
MUST NOT import from voiceover/. Lock, spool and drain helpers come from the
stdlib-only tts/engine_common.py, audio output from tts/pcm_sink.py. ptts and
sounddevice are imported only inside functions, so a dry run - and the test
suite - loads neither.

Audio is streamed: a worker thread writes each chunk to the sink as ptts
produces it, so sound starts while the sentence is still being generated.

Honors VOICEOVER_DRY_RUN: prints '[voiceover] <text>' to stderr and exits
without loading the model, downloading anything, or producing audio.
"""

import concurrent.futures
import os
import sys
import threading
import time

from engine_common import (POLL_SECONDS, Playback, data_dir, drain_root, engine_log, remove_tts_lock,
                           split_sentences, try_claim_lock, update_lock_expiry)
from pcm_sink import SAMPLE_RATE, open_sink

DRY_RUN = bool(os.environ.get("VOICEOVER_DRY_RUN"))
ENGINE_NAMES = {"phonon"}

# Pinned: an unpinned name checks the network on every load (measured: 1.6 s
# online, 17 s when the network is unreachable); pinned it loads in 0.41 s
# either way. This revision is ptts 0.7.1's own DEFAULT_MODEL.
MODEL_REPO = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"
QUANT = "q8_0"      # 64.5 MB, about half the memory of full precision
DEVICE = "cpu"      # CPU only; Metal is out of scope
THREADS = 2         # measured: fastest, and steadiest under load
FALLBACK_VOICE = "default"  # bundled with the model: works offline
WARM_UP_TEXT = "Ready."
FETCH_TEXT = "Hello."
GRACE_SECONDS = 10.0  # a sentence's audio may take this long past its length to drain


def log(message):
    """Diagnostics go to stderr; stdout stays silent for the hook protocol."""
    print(message, file=sys.stderr)


def load_model():
    """The Phonon model: 8-bit weights on the CPU with 2 threads."""
    import ptts

    ptts.set_num_threads(THREADS)  # before loading: it sizes a pool built once
    return ptts.TTS(MODEL_REPO, lang="en", quant=QUANT, device=DEVICE)


# --- voices --------------------------------------------------------------------

class _Voices:
    """Opens a stream in a voice, falling back to the bundled one.

    A catalogue voice is fetched on first use; offline, the fetch fails after
    about 2.6 s (measured). A voice that failed is remembered for the life of
    this speaker - one per engine process - so that delay is paid once, not
    per sentence."""

    def __init__(self, tts):
        self._tts = tts
        self._failed = set()

    def stream(self, sentence, voice):
        voice = voice or FALLBACK_VOICE
        if voice not in self._failed:
            try:
                return self._tts.stream(sentence, voice=voice)
            except (LookupError, OSError) as error:  # raised by the call, before any chunk
                self._failed.add(voice)
                engine_log("voice", "%s unavailable, speaking in %s: %s" % (voice, FALLBACK_VOICE, error))
        return self._tts.stream(sentence, voice=FALLBACK_VOICE)


# --- playback ------------------------------------------------------------------

class PhononPlayback(Playback):
    """One sentence, streamed by the speaker's worker thread into the sink.

    The worker's check of the cancel flag and its write to the sink happen
    under one lock, so once kill() has returned no chunk reaches the sink.
    kill() silences first and never waits for the generator: the worker
    closes it (that blocks 90 to 220 ms, measured), and the next sentence
    starts after.
    """

    def __init__(self, sink, grace_seconds):
        self._sink, self._grace = sink, grace_seconds
        self._gate = threading.Lock()
        self._cancelled = False
        self._writes = 0
        self._audio_seconds = 0.0
        self._finished_at = None
        self._future = None

    def attach(self, future):
        self._future = future

    # --- the worker's side ---

    def cancelled(self) -> bool:
        return self._cancelled

    def write(self, chunk) -> bool:
        """Play one chunk unless the sentence was killed. False = stop generating."""
        with self._gate:
            if self._cancelled:
                return False
            self._sink.write(chunk)
            self._writes += 1
            self._audio_seconds += len(chunk) / float(SAMPLE_RATE)
            return True

    def finish(self):
        self._finished_at = time.monotonic()

    # --- the drain loop's side ---

    def started(self) -> bool:
        return self._writes > 0

    def poll(self) -> bool:
        if self._future is None or not self._future.done():
            return False
        self._future.result()  # a generation error surfaces here
        if self._sink.remaining() <= 0:
            return True
        if self._overdue():
            # The output stopped draining (a device that vanished): discard
            # the audio and move on rather than hold the engine for ever.
            engine_log("stall", "audio did not drain; discarded")
            self._sink.abort()
            return True
        return False

    def kill(self) -> None:
        self._cancelled = True  # the worker stops at its next chunk
        writes = self._writes
        self._sink.abort()      # silence now; also frees a write stuck on a full pipe
        with self._gate:        # a write already under way when the flag was set...
            if self._writes != writes:
                self._sink.abort()  # ...is silenced too

    def _overdue(self) -> bool:
        deadline = self._finished_at + self._audio_seconds + self._grace
        return time.monotonic() > deadline


class PhononSpeaker:
    """Streams sentences through a loaded Phonon model into an audio sink.

    One worker thread renders sentences in order. The sink's output is
    opened on the first chunk and released by idle(), which the drain loop
    calls when the queue runs dry, so a warm engine holds no device.
    """

    def __init__(self, tts, sink, grace_seconds=GRACE_SECONDS):
        self._tts, self._sink, self._grace = tts, sink, grace_seconds
        self._voices = _Voices(tts)
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def warm_up(self):
        """Pay the first sentence's one-time cost (130 to 270 ms to the first
        chunk, measured) now. Writes nothing and opens no device."""
        stream = self._tts.stream(WARM_UP_TEXT, voice=FALLBACK_VOICE)
        try:
            for _chunk in stream:
                pass
        finally:
            stream.close()

    def prefetch(self, sentence, voice):
        """Nothing to do: a warm first chunk arrives in under 35 ms (measured)."""

    def begin(self, sentence, voice, upcoming=None):
        playback = PhononPlayback(self._sink, self._grace)
        playback.attach(self._worker.submit(self._render, playback, sentence, voice))
        return playback

    def idle(self):
        self._sink.close()

    def _render(self, playback, sentence, voice):
        stream = None
        try:
            if playback.cancelled():
                return
            stream = self._voices.stream(sentence, voice)
            for chunk in stream:
                if not playback.write(chunk):
                    break
        finally:
            if stream is not None:
                stream.close()
            playback.finish()


# --- entry points --------------------------------------------------------------

def open_speaker():
    """The drain's one-time setup, run once the lock is ours: an audio output,
    the model, one warm-up sentence. None (logged) when any of it fails, and
    the drain then backs the hooks off for 30 s."""
    sink = open_sink()
    if sink is None:
        log("[ERROR] Phonon has no audio output: PortAudio did not load and no player "
            "(aplay, paplay, pw-cat or ffplay) is on PATH")
        engine_log("error", "phonon: no audio output")
        return None
    try:
        speaker = PhononSpeaker(load_model(), sink)
        speaker.warm_up()
        return speaker
    except Exception as error:
        log("[ERROR] Phonon model load failed: " + str(error))
        engine_log("error", "phonon load: %s: %s" % (type(error).__name__, error))
        return None


def _play_through(playback):
    while not playback.poll():
        time.sleep(POLL_SECONDS)


def _wait_until_played(sink):
    while sink.remaining() > 0:
        time.sleep(POLL_SECONDS)


def speak_once(text, voice) -> bool:
    """One utterance under the one-shot lock, released on every path. The
    sink is closed only once its audio has played, so the end is not cut off."""
    root = data_dir()
    seconds = 10.0 + 0.4 * len(text.split())
    if not try_claim_lock(root, seconds):
        return True  # another narration is playing - skip quietly
    try:
        sink = open_sink()
        if sink is None:
            log("[ERROR] Phonon has no audio output")
            return False
        speaker = PhononSpeaker(load_model(), sink)
        for sentence in split_sentences(text):
            update_lock_expiry(root, seconds)
            _play_through(speaker.begin(sentence, voice))
        _wait_until_played(sink)
        sink.close()
        return True
    except Exception as error:
        log("[ERROR] Speech failed: " + str(error))
        return False
    finally:
        remove_tts_lock(root)


def install() -> int:
    """Download (on first use) and load the model. Opens no audio device."""
    try:
        load_model()
    except Exception as error:
        log("[ERROR] Phonon model install failed: " + str(error))
        return 1
    log("[OK] Phonon model installed")
    return 0


def fetch_voice(name) -> int:
    """Load the model and render one word in `name`, which caches that voice.
    No fallback: a voice that cannot be loaded exits 1. Opens no audio device."""
    try:
        stream = load_model().stream(FETCH_TEXT, voice=name)
        try:
            for _chunk in stream:
                pass
        finally:
            stream.close()
    except Exception as error:
        log("[ERROR] Phonon voice %s could not be loaded: %s" % (name, error))
        return 1
    return 0


# --- CLI -------------------------------------------------------------------------

def parse_args(argv):
    """(voice, text, use_streaming) from `<text...> [--voice NAME] [--stream]`.
    A voice name with spaces arrives as one argument. Phonon always streams."""
    voice, use_streaming, words = FALLBACK_VOICE, False, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--voice" and i + 1 < len(argv):
            voice = argv[i + 1]
            i += 2
            continue
        if arg == "--stream":
            use_streaming = True
        else:
            words.append(arg)
        i += 1
    return voice, " ".join(words).strip(), use_streaming


def _value_after(argv, flag):
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def print_usage():
    log("Phonon TTS Voice Engine")
    log("Usage:")
    log("  phonon_voice.py '<text>' [--voice NAME] [--stream]")
    log("  phonon_voice.py --drain <project dir>")
    log("  phonon_voice.py --install")
    log("  phonon_voice.py --fetch-voice NAME")


def _drain(argv) -> int:
    from engine_common import DryRunSpeaker, drain

    root = drain_root(argv)
    if root is None:
        log("Usage: phonon_voice.py --drain <project dir>")
        return 0
    return drain(root, DryRunSpeaker if DRY_RUN else open_speaker, ENGINE_NAMES)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--drain" in argv:
        return _drain(argv)
    if "--install" in argv:
        return 0 if DRY_RUN else install()
    if "--fetch-voice" in argv:
        name = _value_after(argv, "--fetch-voice")
        if not name:
            print_usage()
            return 1
        return 0 if DRY_RUN else fetch_voice(name)
    if not argv:
        print_usage()
        return 0
    voice, text, _use_streaming = parse_args(argv)
    if not text:
        log("[ERROR] No text to speak")
        return 1
    if DRY_RUN:
        print("[voiceover] " + text, file=sys.stderr)
        return 0
    return 0 if speak_once(text, voice) else 1


if __name__ == "__main__":
    sys.exit(main())
