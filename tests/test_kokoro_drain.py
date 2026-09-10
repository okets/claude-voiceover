# tests/test_kokoro_drain.py
"""The kokoro drain path: stream incrementally, and hold the lock throughout.

Dry-run short-circuits before synthesis, so it cannot reach this code at all.
Kokoro's own dependencies (kokoro-onnx, numpy, soundfile) live in the tts/
venv and are not importable from the stdlib-only test suite, so they are
stubbed here: what is under test is OUR wiring - that drain streams rather
than rendering a whole item first, that the lock horizon is extended BEFORE
synthesis begins, and that no drain path removes the lock mid-loop.
"""

import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool

# Stub the tts-world dependencies kokoro_voice imports lazily.
_numpy = types.ModuleType("numpy")
_numpy.ndarray = list
_soundfile = types.ModuleType("soundfile")
_soundfile.write = lambda *args, **kwargs: None
sys.modules.setdefault("numpy", _numpy)
sys.modules.setdefault("soundfile", _soundfile)

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common
import kokoro_voice


class FakeKokoro:
    """Records how it was asked to synthesize, and the lock horizon at the time."""

    def __init__(self):
        self.streamed = []
        self.whole = []
        self.horizons = []

    def _note_horizon(self):
        try:
            raw = engine_common.tts_lock_path().read_text()
            self.horizons.append(json.loads(raw)["expiry"] - time.time())
        except Exception:
            self.horizons.append(None)

    def create_stream(self, text, voice, speed, lang, trim):
        self._note_horizon()
        self.streamed.append(text)

        async def chunks():
            for _ in range(3):
                yield ([0.0] * 2400, 24000)   # 0.1s of audio per chunk

        return chunks()

    def create(self, text, voice, speed, lang, trim):
        self._note_horizon()
        self.whole.append(text)
        return ([0.0] * 2400, 24000)


fake = FakeKokoro()
played = []
kokoro_voice.play_audio_file = lambda path, timeout=30: played.append(path) or True
kokoro_voice.ensure_models = lambda: True
kokoro_voice.DRY_RUN = False
load_saw_lock = []


def _construct(*args, **kwargs):
    # The lock must already be ours when the model starts loading.
    load_saw_lock.append(engine_common.lock_is_live())
    return fake


sys.modules["kokoro_onnx"] = types.ModuleType("kokoro_onnx")
sys.modules["kokoro_onnx"].Kokoro = _construct

# --- drain streams each item, one chunk at a time ---------------------------
spool.clear()
long_text = " ".join(["word"] * 500)   # ~200s of speech: far past drain's 60s lock
spool.enqueue(long_text, "kokoro", "af_sarah")
time.sleep(0.002)
spool.enqueue("second item", "kokoro", "af_sarah")

argv = sys.argv
sys.argv = ["kokoro_voice.py", "--drain"]
try:
    code = kokoro_voice.main()
finally:
    sys.argv = argv

check("the kokoro drain exits 0", code == 0, code)
check("every item was streamed, in order",
      fake.streamed == [long_text, "second item"], fake.streamed)
check("no item was rendered whole before playing", fake.whole == [], fake.whole)
check("each chunk was played as it arrived", len(played) == 6, len(played))
check("the spool is empty afterwards", spool.pending_count() == 0)
check("the model is loaded exactly once for the whole drain",
      len(load_saw_lock) == 1, load_saw_lock)
check("the lock is claimed BEFORE the model is loaded",
      load_saw_lock == [True], load_saw_lock)
check("the lock is released when the drain ends",
      engine_common.lock_is_live() is False)

# --- the lock outlives synthesis, not just playback ------------------------
# drain() sets the expiry to its own lock_seconds (60). A long item can take
# longer than that to render; if the lock expired inside the synthesis window
# the next hook would read it as free and start a second drainer, and two
# voices would speak at once.
check("the lock horizon is extended BEFORE a long item is synthesized",
      fake.horizons and fake.horizons[0] is not None and fake.horizons[0] > 60,
      fake.horizons)

# --- the non-streaming fallback keeps the lock too --------------------------
engine_common.try_claim_lock(60.0)
fallback = FakeKokoro()
kokoro_voice._speak_whole_keeping_lock(fallback, "fallback text", "af_sarah")
check("the fallback renders the item whole", fallback.whole == ["fallback text"])
check("the fallback does NOT remove the lock the drain loop owns",
      engine_common.lock_is_live() is True)

# --- the single-utterance path still owns and releases its lock ------------
kokoro_voice.speak_streaming(FakeKokoro(), "one shot", "af_sarah")
check("speak_streaming still releases the lock when it is done",
      engine_common.tts_lock_path().exists() is False)

report()
