# tts/engine_stub.py
"""A stand-in synthesizer for the shared Kokoro engine's tests. Stdlib only."""

import math
import time

from engine_protocol import AUDIO, pcm16le

SECONDS_PER_WORD = 0.06
TONE_HZ = 440.0
AMPLITUDE = 0.3


class SineSynth:
    """Implements the Synth interface (see engine_server) with a 440 Hz tone,
    0.06 s per word at 24 kHz, and optional artificial delays."""

    def __init__(self, load_delay=0.0, synth_delay=0.0, voices=("af_sky", "bf_emma")):
        self.loaded = False
        self.synth_calls = 0
        self._load_delay = load_delay
        self._synth_delay = synth_delay
        self._voices = set(voices)

    def load(self):
        time.sleep(self._load_delay)
        self.loaded = True

    def voices(self):
        return set(self._voices)

    def default_speed(self, voice):
        return 1.0

    def synthesize(self, text, voice, speed, lang):
        self.synth_calls += 1
        time.sleep(self._synth_delay)
        count = int(len(text.split()) * SECONDS_PER_WORD * AUDIO["rate"])
        step = 2 * math.pi * TONE_HZ / AUDIO["rate"]
        return pcm16le([AMPLITUDE * math.sin(step * i) for i in range(count)])

    def status(self):
        return {"device": "cpu", "requested": "cpu", "fallback": None, "vram_mib": 0}
