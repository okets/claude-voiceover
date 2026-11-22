"""tts/phonon_voice.py: the Phonon engine, with ptts and the audio sink stubbed.

No model, no network, no sound: ptts is a stub module, the sink a recorder.
Isolated from the checkout it runs in: spool items live in a temp project of
their own, passed as cwd; the dry-run subprocesses get a PYTHONPATH whose
ptts and sounddevice raise on import, proving they are never imported.
"""

import os
import subprocess
import sys
import threading
import re
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

data_dir = isolate()
from voiceover import projects, spool

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common  # noqa: E402
import pcm_sink  # noqa: E402

PINNED = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"


def wait_for(condition, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# --- a stub ptts ----------------------------------------------------------------------
record = {"threads": [], "args": None, "kwargs": None, "built": 0, "load_fails": False}
streams = []
events = []
behaviour = {}  # sentence -> {"chunks", "gate", "fail_after"}


class FakeChunk:
    def __len__(self):
        return 1920

    def tobytes(self):
        return b"\x00" * 7680


class FakeStream:
    def __init__(self, text, voice, chunks=3, gate=None, fail_after=None):
        self.text, self.voice = text, voice
        self.closed = False
        self._chunks, self._gate, self._fail_after = chunks, gate, fail_after
        self._produced = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._gate is not None and self._produced >= 1:
            self._gate.wait(5)  # the second chunk comes only once the test allows it
        if self._fail_after is not None and self._produced == self._fail_after:
            raise RuntimeError("decoder failed")
        if self._produced == self._chunks:
            raise StopIteration
        self._produced += 1
        return FakeChunk()

    def close(self):
        events.append("close")
        time.sleep(0.15)  # measured: closing a live generator blocks 90 to 220 ms
        self.closed = True


class FakeTTS:
    def stream(self, text, voice=None):
        stream = FakeStream(text, voice, **behaviour.get(text, {}))
        streams.append(stream)
        if voice == "Missing":
            raise OSError("cannot fetch `voices/x.safetensors`: HTTP request error")
        return stream


def build_tts(*args, **kwargs):
    if record["load_fails"]:
        raise OSError("model download failed")
    record.update(args=args, kwargs=kwargs, built=record["built"] + 1)
    return FakeTTS()


ptts = types.ModuleType("ptts")
ptts.set_num_threads = record["threads"].append
ptts.TTS = build_tts
sys.modules["ptts"] = ptts


class FakeSink(pcm_sink.Sink):
    """Records what the engine does to its audio output."""

    def __init__(self, remaining=0.0):
        self.log = []
        self.remaining_value = remaining
        self.closed = 0
        self.close_time = None

    @property
    def written(self):
        return self.log.count("write")

    def write(self, chunk):
        self.log.append("write")

    def abort(self):
        self.log.append("abort")
        events.append("abort")

    def close(self):
        self.closed += 1
        self.close_time = time.time()

    def remaining(self):
        value = self.remaining_value
        return value() if callable(value) else value


import phonon_voice  # noqa: E402

# --- the model ------------------------------------------------------------------------
phonon_voice.load_model()
check("the model loads with 2 threads and 8-bit weights",
      (record["threads"], record["kwargs"]) == ([2], {"lang": "en", "quant": "q8_0", "device": "cpu"}),
      (record["threads"], record["kwargs"]))
check("the model loads on the CPU from the pinned revision",
      record["args"] and record["args"][0] == PINNED, record["args"])

# --- streaming a sentence --------------------------------------------------------------
sink = FakeSink(remaining=1.0)
speaker = phonon_voice.PhononSpeaker(FakeTTS(), sink)
playback = speaker.begin("Hello there.", "Bryony")
wait_for(lambda: streams and streams[-1].closed)
check("chunks reach the sink as they are generated", sink.written == 3, sink.log)
check("started() once a chunk was written", playback.started())
check("a sentence is not done until its audio has played out", playback.poll() is False)
sink.remaining_value = 0.0
check("then it is", playback.poll() is True)
check("the generator is closed after a sentence", streams[-1].closed)

playback = speaker.begin("Ahoy.", "Captain Silas Blackwater")
wait_for(playback.poll)
check("the voice name is passed through, spaces and all", streams[-1].voice == "Captain Silas Blackwater")

# --- a voice that cannot be loaded ------------------------------------------------------
written_before = sink.written
first = len(streams)
wait_for(speaker.begin("One.", "Missing").poll)
check("an unfetchable voice is spoken in the default voice",
      [s.voice for s in streams[-2:]] == ["Missing", "default"] and sink.written > written_before)
wait_for(speaker.begin("Two.", "Missing").poll)
check("an unfetchable voice is tried once per process, not per sentence",
      [s.voice for s in streams[first:]] == ["Missing", "default", "default"],
      [s.voice for s in streams[first:]])

# --- a stop -----------------------------------------------------------------------------
events.clear()
gate = threading.Event()
behaviour["A long sentence."] = {"chunks": 5, "gate": gate}
playback = speaker.begin("A long sentence.", "Bryony")
wait_for(lambda: playback.started())
written_at_kill = sink.written
began = time.time()
playback.kill()
kill_seconds = time.time() - began
abort_index = len(sink.log) - 1
gate.set()  # the generator yields its next chunk only now, after kill() returned
wait_for(lambda: streams[-1].closed)
check("kill silences before the generator is closed", events[:2] == ["abort", "close"], events)
check("kill does not wait for the generator to close", kill_seconds < 0.05, kill_seconds)
check("a killed sentence writes nothing more", sink.written == written_at_kill, sink.log)
check("no write reaches the sink after the abort", "write" not in sink.log[abort_index + 1:], sink.log)
check("the cancelled generator is closed", streams[-1].closed)
written_before = sink.written
wait_for(speaker.begin("After the stop.", "Bryony").poll)
check("the next sentence plays after a kill", sink.written - written_before == 3)

# --- a generation error -----------------------------------------------------------------
behaviour["Broken."] = {"fail_after": 1}
playback = speaker.begin("Broken.", "Bryony")
wait_for(lambda: streams[-1].closed)
raised_runtime_error = False
try:
    wait_for(playback.poll)
except RuntimeError:
    raised_runtime_error = True
check("a generation error surfaces from poll", raised_runtime_error)
check("the generator is closed after an error", streams[-1].closed)

# --- a sentence cannot hang the engine --------------------------------------------------
stuck_sink = FakeSink(remaining=5.0)  # the device vanished: the buffer never drains
stuck = phonon_voice.PhononSpeaker(FakeTTS(), stuck_sink, grace_seconds=0.05)
playback = stuck.begin("Nobody hears this.", "Bryony")
began = time.time()
done = wait_for(playback.poll, seconds=3.0)
check("a sentence whose audio never drains ends after its length plus the grace",
      done and time.time() - began < 1.0, time.time() - began)
check("and the stuck audio is discarded", "abort" in stuck_sink.log)

# --- idle and warm-up -------------------------------------------------------------------
speaker.idle()
check("idle releases the audio device", sink.closed == 1)
warm_sink = FakeSink()
warm = phonon_voice.PhononSpeaker(FakeTTS(), warm_sink)
before = len(streams)
warm.warm_up()
check("warm-up renders one sentence and plays nothing",
      len(streams) - before == 1 and warm_sink.log == [] and warm_sink.closed == 0)
check("warm-up uses the bundled voice and closes its generator",
      streams[-1].voice == "default" and streams[-1].closed)

# --- opening the speaker ----------------------------------------------------------------
real_open_sink = phonon_voice.open_sink
opened_sinks = []


def counting_open_sink(result):
    def open_it():
        opened_sinks.append(1)
        return result
    return open_it


try:
    phonon_voice.open_sink = counting_open_sink(None)
    built = record["built"]
    check("no audio output means no speaker", phonon_voice.open_speaker() is None)
    check("and no model is loaded for it", record["built"] == built)
    phonon_voice.open_sink = counting_open_sink(FakeSink())
    record["load_fails"] = True
    check("a model that fails to load means no speaker", phonon_voice.open_speaker() is None)
    record["load_fails"] = False
    before = len(streams)
    opened = phonon_voice.open_speaker()
    check("a working output and model give a warmed-up speaker",
          isinstance(opened, phonon_voice.PhononSpeaker) and streams[before].text == "Ready.")

    # --- --install and --fetch-voice never open the audio device ------------------------
    opened_sinks.clear()
    check("--install loads the model and exits 0", phonon_voice.install() == 0)
    check("--fetch-voice exits 0 for a voice that loads", phonon_voice.fetch_voice("Bryony") == 0)
    check("--fetch-voice renders in that voice", streams[-1].voice == "Bryony" and streams[-1].closed)
    check("--fetch-voice exits 1 for a voice that cannot be loaded, with no fallback",
          phonon_voice.fetch_voice("Missing") == 1 and streams[-1].voice == "Missing")
    record["load_fails"] = True
    check("--install exits 1 when the model fails", phonon_voice.install() == 1)
    record["load_fails"] = False
    check("--install and --fetch-voice never open the audio device", opened_sinks == [])

    # --- the one-shot speaker -----------------------------------------------------------
    lock_seen = []
    drained_at = {}

    def draining():
        lock_seen.append(engine_common.lock_is_live(engine_common.data_dir()))
        drained_at.setdefault("t", time.time() + 0.2)
        return max(0.0, drained_at["t"] - time.time())

    one_sink = FakeSink(remaining=draining)
    phonon_voice.open_sink = counting_open_sink(one_sink)
    before = len(streams)
    ok = phonon_voice.speak_once("First sentence. Second one.", "Maeve")
    check("the one-shot speaks every sentence in its voice",
          ok and [(s.text, s.voice) for s in streams[before:]]
          == [("First sentence.", "Maeve"), ("Second one.", "Maeve")])
    check("it holds the one-shot lock while it plays", lock_seen and all(lock_seen))
    check("it closes the sink only once the audio has played out",
          one_sink.closed == 1 and one_sink.close_time >= drained_at["t"])
    check("and releases the lock", not engine_common.lock_is_live(engine_common.data_dir()))
    phonon_voice.open_sink = counting_open_sink(None)
    check("no audio output: the one-shot fails and releases the lock",
          phonon_voice.speak_once("Hi.", "Maeve") is False
          and not engine_common.lock_is_live(engine_common.data_dir()))
    phonon_voice.open_sink = counting_open_sink(FakeSink())
    record["load_fails"] = True
    check("a failed model load releases the lock too",
          phonon_voice.speak_once("Hi.", "Maeve") is False
          and not engine_common.lock_is_live(engine_common.data_dir()))
    record["load_fails"] = False
finally:
    phonon_voice.open_sink = real_open_sink

# --- the drain ----------------------------------------------------------------------------
PROJECT = temp_dir("vo-phonon-engine-project-")
(PROJECT / ".git").mkdir()
CWD = str(PROJECT)
ROOT = projects.project_dir(cwd=CWD)
spool.enqueue("First item.", "phonon", "Bryony", cwd=CWD, utterance="one-message")
spool.enqueue("Second item.", "phonon", "Bryony", cwd=CWD, utterance="one-message")
drain_sink = FakeSink()
drain_speaker = phonon_voice.PhononSpeaker(FakeTTS(), drain_sink)
before = len(streams)
code = engine_common.drain(ROOT, lambda: drain_speaker, {"phonon"}, poll_seconds=0.001)
check("a drain streams its items in order",
      code == 0 and [s.text for s in streams[before:]] == ["First item.", "Second item."],
      [s.text for s in streams[before:]])
check("and closes the sink once the queue runs dry", drain_sink.closed >= 1)

# --- the command line, in dry run, imports neither ptts nor sounddevice ----------------------
poison = temp_dir("vo-poison-")
for name in ("ptts", "sounddevice"):
    (poison / (name + ".py")).write_text("raise ImportError('%s must not be imported in dry run')\n" % name)
env = dict(os.environ, VOICEOVER_DRY_RUN="1", PYTHONPATH=str(poison))
script = str(REPO_ROOT / "tts" / "phonon_voice.py")
spool.enqueue("Hello.", "phonon", "Bryony", cwd=CWD)
proc = subprocess.run([sys.executable, script, "--drain", str(ROOT)], env=env,
                      capture_output=True, text=True, timeout=30)
check("a dry-run drain prints the sentence and loads nothing",
      proc.returncode == 0 and "[voiceover] Hello." in proc.stderr, proc.stderr)
proc = subprocess.run([sys.executable, script, "--voice", "Old Mortlake", "--stream", "Hi there."], env=env,
                      capture_output=True, text=True, timeout=30)
check("a dry-run one-shot prints the text and loads nothing",
      proc.returncode == 0 and "[voiceover] Hi there." in proc.stderr, proc.stderr)
check("the one-shot form parses a multi-word voice",
      phonon_voice.parse_args(["--voice", "Old Mortlake", "--stream", "Hi."]) == ("Old Mortlake", "Hi.", True))
check("text before the voice parses too",
      phonon_voice.parse_args(["Hello", "world", "--voice", "Bryony"]) == ("Bryony", "Hello world", False))

# --- dependency isolation -------------------------------------------------------------------


def names(relative):
    """The package names in a pyproject's dependencies (house style: no tomllib on 3.10)."""
    toml = (REPO_ROOT / relative).read_text()
    block = toml.split("dependencies = [", 1)[1].split("\n]", 1)[0]
    return {re.split(r"[\s;\[<>=!~]", dep, 1)[0].lower() for dep in re.findall(r'"([^"]+)"', block)}


check("the Phonon environment carries nothing of Kokoro's",
      not {"kokoro-onnx", "onnxruntime", "soundfile"} & names("tts-phonon/pyproject.toml"),
      names("tts-phonon/pyproject.toml"))
check("the Phonon environment needs ptts and sounddevice",
      {"ptts", "sounddevice"} <= names("tts-phonon/pyproject.toml"))
check("the Kokoro environments carry nothing of Phonon's",
      not {"ptts", "sounddevice"} & (names("tts/pyproject.toml") | names("tts-gpu/pyproject.toml")))

report()
