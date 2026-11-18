# tts/engine_speaker.py
"""Narration as a client of the shared Kokoro engine (stdlib only).

EngineSpeaker is the drain loop's speaker on Linux/macOS: it asks the engine for
each sentence's audio, writes it as a WAV and plays it. The drain loop sees the
same begin/prefetch/Playback surface as the in-process KokoroSpeaker, so a stop
request or lock refresh never waits on synthesis.
"""

import os
import time
import wave
from pathlib import Path

from engine_client import EngineError
from engine_common import Playback
from kokoro_model import DEFAULT_VOICE

SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2        # pcm_s16le
WARM_HOLD_S = 60
WARM_EVERY_S = 30
_RETRYABLE = ("restarting", "disconnected")


def write_wav(path, pcm: bytes) -> float:
    """Write mono 16-bit 24 kHz PCM as a WAV; the clip's length in seconds."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    return len(pcm) / float(SAMPLE_WIDTH * SAMPLE_RATE)


class EnginePlayback(Playback):
    """One sentence: rendering in the engine, then playing.

    poll() is False until the engine has the audio. A failed request raises
    EngineError, which skips the rest of the item; a restarting engine or a
    dropped connection is retried once through a fresh connection first.
    """

    def __init__(self, speaker, sentence, voice, pending, path, upcoming):
        self._speaker, self._sentence, self._voice = speaker, sentence, voice
        self._pending, self._path, self._upcoming = pending, path, upcoming
        self._retried = False
        self._player = None

    def started(self) -> bool:
        return self._player is not None

    def poll(self) -> bool:
        if self._player is not None:
            return self._player.poll()
        if not self._pending.done.is_set():
            return False
        if self._pending.error is not None:
            return self._handle_failure(self._pending.error)
        if self._upcoming:
            self._speaker.prefetch(self._upcoming, self._voice)
        pcm = self._pending.pcm()
        if not pcm:
            return True  # nothing to say (the engine trimmed it all)
        duration = write_wav(self._path, pcm)
        self._player = self._speaker.play_file(self._path, duration)
        return False

    def _handle_failure(self, error) -> bool:
        if error.code not in _RETRYABLE or self._retried:
            raise error
        self._retried = True
        self._pending = self._speaker.resubmit(self._sentence, self._voice)
        return False

    def kill(self) -> None:
        if self._player is not None:
            self._player.kill()
        self._speaker.cancel_all([self._pending])


class EngineSpeaker:
    """Plays sentences from the shared engine, requesting the next one ahead."""

    def __init__(self, connect, play_dir, play_file, clock=time.monotonic):
        self._connect, self.play_file, self._clock = connect, play_file, clock
        self._play_dir = Path(play_dir)
        self._play_dir.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._ahead = {}
        self._count = 0
        self._warmed_at = None

    # --- the drain loop's surface -------------------------------------------

    def warm_up(self):
        """Connect and hold the engine warm, so the first real sentence is not cold."""
        self._conn = self._open()
        self._conn.warm(WARM_HOLD_S)
        self._warmed_at = self._clock()

    def idle(self):
        """Called while the drain waits on a live lease: keep the engine's hold fresh."""
        if self._warmed_at is not None and self._clock() - self._warmed_at < WARM_EVERY_S:
            return
        try:
            self._connection().warm(WARM_HOLD_S)
            self._warmed_at = self._clock()
        except (EngineError, OSError):
            self._conn = None  # the next real sentence reconnects

    def prefetch(self, sentence, voice):
        """Ask the engine for a sentence the next item will begin with."""
        voice = voice or DEFAULT_VOICE
        key = (sentence, voice)
        if key not in self._ahead:
            self._ahead[key] = self.submit(sentence, voice)

    def begin(self, sentence, voice, upcoming=None):
        voice = voice or DEFAULT_VOICE
        pending = self._ahead.pop((sentence, voice), None) or self.submit(sentence, voice)
        self.cancel_all([])  # anything else rendered ahead belongs to an abandoned item
        self._count += 1
        path = self._play_dir / ("%d-%d.wav" % (os.getpid(), self._count))
        return EnginePlayback(self, sentence, voice, pending, path, upcoming)

    # --- requests --------------------------------------------------------------

    def submit(self, sentence, voice):
        """Submit with no speed (the engine applies the voice's own); reconnect once if the link is dead."""
        try:
            return self._connection().submit(sentence, voice)
        except EngineError as error:
            if error.code not in _RETRYABLE:
                raise
        return self.resubmit(sentence, voice)

    def resubmit(self, sentence, voice):
        self._conn = self._open()
        return self._conn.submit(sentence, voice)

    def cancel_all(self, pendings):
        """Cancel the given requests and everything rendered ahead."""
        for pending in list(pendings) + list(self._ahead.values()):
            self._cancel(pending)
        self._ahead.clear()

    def _cancel(self, pending):
        if pending.done.is_set() or self._conn is None:
            return
        try:
            self._conn.cancel(pending)
        except EngineError:
            pass

    def _connection(self):
        if self._conn is None or not self._conn.alive:
            self._conn = self._open()
        return self._conn

    def _open(self):
        """A fresh connection; a socket error (the engine is gone) is a retryable `disconnected`."""
        try:
            return self._connect()
        except OSError as error:
            raise EngineError("disconnected", str(error)) from None
