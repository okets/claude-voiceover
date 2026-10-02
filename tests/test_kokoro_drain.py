"""The kokoro drainer: sentence by sentence, synthesizing ahead.

Kokoro's own dependencies (kokoro-onnx, numpy, soundfile) live in the tts/
venv and are not importable from the stdlib-only suite, so they are stubbed:
what is under test is OUR wiring - per-sentence synthesis, the warm-up, both
prefetches, the lock claimed before the model loads, stops that land during
synthesis, and the Windows player.
"""

import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool, speech

_numpy = types.ModuleType("numpy")
_numpy.ndarray = list
_soundfile = types.ModuleType("soundfile")
_soundfile.write = lambda path, samples, rate: Path(path).write_bytes(b"RIFF")
sys.modules.setdefault("numpy", _numpy)
sys.modules.setdefault("soundfile", _soundfile)

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common
import kokoro_voice

ROOT = spool.queue_dir().parent
PLAY = ROOT / "play"


class FakeKokoro:
    def __init__(self, delay=0.0):
        self.created, self._delay = [], delay

    def create(self, text, voice, speed, lang, trim):
        time.sleep(self._delay)
        self.created.append(text)
        return ([0.0] * 2400, 24000)

    def create_stream(self, text, voice, speed, lang, trim):
        async def chunks():
            yield ([0.0] * 2400, 24000)
        return chunks()


kokoro_voice.ensure_models = lambda: True
kokoro_voice.DRY_RUN = False
kokoro_voice.play_audio_file = lambda path, timeout=30: True
kokoro_voice.playback_command = lambda path: [sys.executable, "-c", "pass"]
fake = FakeKokoro()
load_saw_lock = []


def _construct(*args, **kwargs):
    load_saw_lock.append(engine_common.lock_is_live(ROOT))
    return fake


sys.modules["kokoro_onnx"] = types.ModuleType("kokoro_onnx")
sys.modules["kokoro_onnx"].Kokoro = _construct

# --- drain speaks sentence by sentence, after a warm-up -------------------------
spool.clear()
spool.enqueue("One. Two. Three.", "kokoro", "af_sarah", utterance="U")
argv = sys.argv
sys.argv = ["kokoro_voice.py", "--drain", str(ROOT)]
try:
    code = kokoro_voice.main()
finally:
    sys.argv = argv
check("the kokoro drain exits 0", code == 0, code)
check("a warm-up, then each sentence synthesized once, in order",
      fake.created == ["Ready.", "One.", "Two.", "Three."], fake.created)
check("the model is loaded exactly once", len(load_saw_lock) == 1, load_saw_lock)
check("the lock is claimed BEFORE the model is loaded", load_saw_lock == [True], load_saw_lock)
check("the lock is released when the drain ends", engine_common.lock_is_live(ROOT) is False)
check("no wav file is left behind", list(PLAY.glob("*.wav")) == [], list(PLAY.glob("*.wav")))

# --- within an item: the next sentence renders while this one plays ------------
def play_out(playback):
    while not playback.poll():
        time.sleep(0.01)


speaker = kokoro_voice.KokoroSpeaker(FakeKokoro(), PLAY)
play_out(speaker.begin("Alpha.", "af_sarah", "Beta."))
check("once a sentence is rendered the upcoming one starts rendering",
      ("Beta.", "af_sarah") in speaker._ahead)
play_out(speaker.begin("Beta.", "af_sarah", None))
check("the prefetched sentence is not rendered twice",
      speaker._kokoro.created == ["Alpha.", "Beta."], speaker._kokoro.created)
play_out(speaker.begin("Gamma.", "af_sarah", "Delta."))
speaker.begin("Other.", "af_sarah", None)
check("work rendered ahead for an abandoned sentence is discarded",
      speaker._ahead == {}, speaker._ahead)

# --- across items: the next item's first sentence renders during this one -------
spool.clear()
spool.enqueue("First item.", "kokoro", "af_sarah", utterance="U")
spool.enqueue("Second item.", "kokoro", "af_sarah", utterance="U")
kokoro_voice.playback_command = lambda path: [sys.executable, "-c", "import time; time.sleep(0.3)"]
across = FakeKokoro()
prefetches = []


class Watched(kokoro_voice.KokoroSpeaker):
    def prefetch(self, sentence, voice):
        prefetches.append(sentence)
        super().prefetch(sentence, voice)


engine_common.drain(ROOT, lambda: Watched(across, PLAY), {"kokoro"})
check("the next item's first sentence is prefetched", prefetches == ["Second item."], prefetches)
check("and rendered only once", across.created == ["First item.", "Second item."], across.created)

# --- a stop that lands during synthesis still stops promptly --------------------
spool.clear()
spool.enqueue("Slow one. Never played.", "kokoro", "af_sarah", session="S1", utterance="U")
players = []
kokoro_voice.playback_command = lambda path: players.append(path) or [
    sys.executable, "-c", "import time; time.sleep(5)"]
slow = FakeKokoro(delay=0.3)
threading.Timer(0.1, lambda: speech.request_stop(None, "S1")).start()
started = time.time()
engine_common.drain(ROOT, lambda: kokoro_voice.KokoroSpeaker(slow, PLAY), {"kokoro"})
check("a stop during synthesis ends the item at once", time.time() - started < 1.0,
      time.time() - started)
check("the stopped sentence never started playing", players == [], players)
check("its item is gone", spool.pending_count() == 0)

# --- Windows playback: in-process, asynchronous, stoppable -----------------------
calls = []
fake_winsound = types.ModuleType("winsound")
fake_winsound.SND_FILENAME, fake_winsound.SND_ASYNC = 0x20000, 0x0001
fake_winsound.PlaySound = lambda sound, flags: calls.append((sound, flags))
sys.modules["winsound"] = fake_winsound
wav = PLAY / "win.wav"
wav.write_bytes(b"RIFF")
playback = kokoro_voice.WinsoundPlayback(wav, 0.2)
check("it starts the clip asynchronously from the file",
      calls == [(str(wav), 0x20000 | 0x0001)], calls)
check("it is still playing before the clip's length has passed", playback.poll() is False)
playback.kill()
check("kill stops all sound", calls[-1] == (None, 0), calls)
check("and removes the clip", not wav.exists())

# --- the one-shot path still owns and releases its own lock ----------------------
kokoro_voice.speak_streaming(FakeKokoro(), "one shot", "af_sarah")
check("speak_streaming still releases the one-shot lock",
      engine_common.tts_lock_path(engine_common.data_dir()).exists() is False)

report()
