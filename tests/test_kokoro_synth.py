"""KokoroSynth: the engine's real model behind the Synth interface, with kokoro_onnx,
numpy and onnxruntime stubbed. Under test is OUR wiring: NoModel, voices, speeds,
keep-off, load fallback and the mid-run CUDA -> CPU fallback."""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))

_numpy = types.ModuleType("numpy")
_numpy.ndarray = list
_ort = types.ModuleType("onnxruntime")
_ort.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
sys.modules.setdefault("numpy", _numpy)
sys.modules["onnxruntime"] = _ort

import engine_paths
import engine_server
import kokoro_model
import kokoro_voice

VOICES = ["voice_%d" % i for i in range(54)]


class FakeSession:
    def __init__(self, provider):
        self._provider = provider

    def get_providers(self):
        return [self._provider]


class FakeKokoro:
    """CUDA instances raise on their second create (T12 shape); CPU ones never do."""
    def __init__(self, provider):
        self.sess, self.provider, self.calls = FakeSession(provider), provider, 0

    def get_voices(self):
        return VOICES

    def create(self, text, voice, speed, lang, trim=False):
        self.calls += 1
        if self.provider == "CUDAExecutionProvider" and self.calls == 2:
            raise RuntimeError("CUDA error")
        return ([0.0] * 240, 24000)


loads = []


def fake_load_on(provider):
    loads.append(provider)
    return FakeKokoro(provider)


kokoro_model._load_on = fake_load_on
kokoro_model.models_present = lambda: True
kokoro_model.choose_onnx_provider = lambda: "CUDAExecutionProvider"
kokoro_model.load_kokoro = lambda: fake_load_on("CUDAExecutionProvider")
kokoro_model._query_vram_mib = lambda pid: 512
keep_off = [None]
kokoro_model.KokoroSynth._keep_off = staticmethod(lambda: keep_off[0])

# no model
kokoro_model.models_present = lambda: False
raised = False
try:
    kokoro_model.KokoroSynth().load()
except engine_server.NoModel:
    raised = True
check("load without model files raises NoModel", raised)
check("and loads nothing", loads == [])
kokoro_model.models_present = lambda: True

# voices, speeds, construction
synth = kokoro_model.KokoroSynth(vram_interval=0.05)
check("voices() before load() is empty", kokoro_model.KokoroSynth().voices() == set())
check("construction is cheap: not loaded", synth.loaded is False and loads == [])
synth.load()
check("loaded after load()", synth.loaded)
check("voices() returns the 54 names", synth.voices() == set(VOICES) and len(synth.voices()) == 54)
check("default speed bm_daniel 1.3", synth.default_speed("bm_daniel") == 1.3)
check("default speed af_heart 1.0", synth.default_speed("af_heart") == 1.0)
deadline = time.time() + 2
while synth.status()["vram_mib"] is None and time.time() < deadline:
    time.sleep(0.01)
check("status on cuda", synth.status() == {"device": "cuda", "requested": "cuda", "fallback": None, "vram_mib": 512},
      synth.status())

# status() never spawns a process
import subprocess
real_run = subprocess.run
def no_spawn(*a, **k):
    raise AssertionError("subprocess.run called")
subprocess.run = no_spawn
try:
    status_ok = synth.status()["vram_mib"] == 512
except AssertionError:
    status_ok = False
subprocess.run = real_run
check("status() spawns nothing and serves the cached vram", status_ok)

# synthesize() never spawns either; the timer thread refreshes the cache
subprocess.run = no_spawn
calls = []
kokoro_model._query_vram_mib = lambda pid: (calls.append(pid), 600 + len(calls))[1]
try:
    quiet = kokoro_model.KokoroSynth(vram_interval=0.05)
    quiet.load()
    quiet._kokoro = FakeKokoro("CPUExecutionProvider")  # no create failure; placement stays cuda
    quiet.synthesize("x", "voice_1", 1.0, "en-us")
    synth_ok = True
except AssertionError:
    synth_ok = False
subprocess.run = real_run
check("synthesize() spawns nothing", synth_ok)
before = quiet.status()["vram_mib"]
time.sleep(0.3)
check("the timer thread refreshes the cached vram", len(calls) >= 3 and quiet.status()["vram_mib"] > (before or 0), calls)
kokoro_model._query_vram_mib = lambda pid: 512

# input errors do not fall back
class BadInput(FakeKokoro):
    def create(self, *args, **kwargs):
        raise KeyError("no such voice")
loads.clear()
picky = kokoro_model.KokoroSynth()
picky.load()
picky._kokoro = BadInput("CUDAExecutionProvider")
loads.clear()
raised = False
try:
    picky.synthesize("x", "nope", 1.0, "en-us")
except KeyError:
    raised = True
check("a KeyError on cuda is re-raised", raised)
check("and neither reloads nor falls back", loads == [] and picky.status()["device"] == "cuda"
      and picky.status()["fallback"] is None, picky.status())

# mid-run CUDA failure
first = synth.synthesize("hello", "voice_1", 1.0, "en-us")
check("first synth returns pcm bytes", isinstance(first, bytes) and len(first) == 480)
second = synth.synthesize("hello", "voice_1", 1.0, "en-us")
status = synth.status()
check("second synth (CUDA raised) still returns audio", isinstance(second, bytes) and len(second) == 480)
check("device is cpu afterwards", status["device"] == "cpu", status)
check("fallback reason starts with synthesis_failed",
      status["fallback"] and status["fallback"]["reason"].startswith("synthesis_failed"), status)
check("fallback has a timestamp", isinstance(status["fallback"]["at"], float))
check("reloaded on CPU", loads[-1] == "CPUExecutionProvider", loads)

# a CPU failure is not retried
class Broken(FakeKokoro):
    def create(self, *args, **kwargs):
        raise RuntimeError("boom")
cpu_synth = kokoro_model.KokoroSynth()
cpu_synth.load()
cpu_synth._kokoro, cpu_synth._placement = Broken("CPUExecutionProvider"), ("cpu", None)
raised = False
try:
    cpu_synth.synthesize("x", "voice_1", 1.0, "en-us")
except RuntimeError:
    raised = True
check("a CPU synthesis failure raises", raised)

# second failure after fallback raises
loads.clear()
kokoro_model._load_on = lambda provider: (loads.append(provider), Broken(provider))[1]
kokoro_model.load_kokoro = lambda: FakeKokoro("CUDAExecutionProvider")
twice = kokoro_model.KokoroSynth()
twice.load()
twice._kokoro = Broken("CUDAExecutionProvider")
raised = False
try:
    twice.synthesize("x", "voice_1", 1.0, "en-us")
except RuntimeError:
    raised = True
check("a second failure raises", raised)
kokoro_model._load_on = fake_load_on

# keep-off
loads.clear()
keep_off[0] = {"until": 9e12, "reason": "game"}
guarded = kokoro_model.KokoroSynth()
guarded.load()
status = guarded.status()
check("keep-off loads on CPU", loads == ["CPUExecutionProvider"], loads)
check("keep-off reports the configured provider as requested", status["requested"] == "cuda", status)
check("keep-off fallback reason", status["fallback"]["reason"] == "keep_off" and status["device"] == "cpu", status)
keep_off[0] = None

# load failure on the accelerator
def failing_load():
    raise RuntimeError("no kernel image")
kokoro_model.load_kokoro = failing_load
loads.clear()
failed = kokoro_model.KokoroSynth()
failed.load()
check("load failure falls back to CPU with load_failed",
      failed.status()["device"] == "cpu" and failed.status()["fallback"]["reason"].startswith("load_failed"),
      failed.status())

# realistic load failure: _load_accelerated swallows the error, its reason survives
loads.clear()
def accel_fails_then_cpu():
    kokoro = kokoro_model._load_accelerated("CUDAExecutionProvider")
    return kokoro if kokoro is not None else fake_load_on("CPUExecutionProvider")
def broken_cuda_load(provider):
    if provider == "CUDAExecutionProvider":
        raise OSError("libcudnn missing")
    return fake_load_on(provider)
kokoro_model._load_on = broken_cuda_load
kokoro_model.load_kokoro = accel_fails_then_cpu
accel = kokoro_model.KokoroSynth()
accel.load()
check("a swallowed accelerated-load error is the fallback reason",
      accel.status()["fallback"]["reason"] == "load_failed: OSError: libcudnn missing", accel.status())
kokoro_model._load_on = fake_load_on

# cpu: no vram query
kokoro_model._query_vram_mib = lambda pid: (_ for _ in ()).throw(AssertionError("queried"))
cpu_only = kokoro_model.KokoroSynth(force_cpu=True)
cpu_only.load()
cpu_only.synthesize("x", "voice_1", 1.0, "en-us")
check("on cpu vram_mib is None with no query", cpu_only.status()["vram_mib"] is None)

# force_cpu
loads.clear()
forced = kokoro_model.KokoroSynth(force_cpu=True)
forced.load()
check("force_cpu loads on CPU", loads == ["CPUExecutionProvider"] and forced.status()["device"] == "cpu")

# old names
check("kokoro_voice.VOICE_SETTINGS is kokoro_model.VOICE_SETTINGS",
      kokoro_voice.VOICE_SETTINGS is kokoro_model.VOICE_SETTINGS)
check("kokoro_voice re-exports the loaders", kokoro_voice.load_kokoro is not None
      and kokoro_voice.models_present is not None and kokoro_voice.MODELS_DIR is kokoro_model.MODELS_DIR)

report()
