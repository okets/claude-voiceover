# tts/kokoro_model.py
"""The Kokoro model: files, device choice, loading, and the engine's KokoroSynth.

Shared by the one-shot/drainer path (kokoro_voice.py) and the shared engine
(kokoro_engine.py). numpy, onnxruntime and kokoro_onnx are imported lazily,
inside functions, so this module imports in the stdlib-only test suite.
"""

import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from engine_common import engine_log, global_setting

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



# --- model loading: CPU by default, NVIDIA GPU when tts_device is "gpu" -------

_CPU = "CPUExecutionProvider"
_CUDA = "CUDAExecutionProvider"


def _gpu_provider():
    """CUDA when the gpu extra is installed and usable, else None.

    The gpu extra ships CUDA and cuDNN as pip wheels; onnxruntime only finds
    them after preload_dlls(). Without that step it silently falls back to CPU.
    """
    try:
        import onnxruntime
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()
        if _CUDA in onnxruntime.get_available_providers():
            return _CUDA
    except Exception as error:
        log("[voiceover] GPU unavailable, using CPU: " + str(error))
    return None


def choose_onnx_provider():
    """The ONNX provider for the tts_device setting.

    An ONNX_PROVIDER already in the environment wins. Otherwise CPU, unless the
    machine opted into "gpu" and CUDA is usable. CPU is always named explicitly
    (by _load_on), so no installed package can change CPU behaviour.
    """
    if os.environ.get("ONNX_PROVIDER"):
        return os.environ["ONNX_PROVIDER"]
    wants_gpu = global_setting("tts_device", "cpu") == "gpu"
    return (_gpu_provider() if wants_gpu else None) or _CPU


def _active_provider(kokoro):
    """The provider the loaded session really runs on, for diagnostics only."""
    try:
        return kokoro.sess.get_providers()[0]
    except Exception:
        return "unknown"


_PROBE_VOICE = "af_sky"


def _load_on(provider):
    """A Kokoro model whose ONNX session runs on provider."""
    os.environ["ONNX_PROVIDER"] = provider
    from kokoro_onnx import Kokoro

    return Kokoro(str(MODEL_FILE), str(VOICES_FILE))


_last_accelerated_error = None   # why the latest _load_accelerated failed (None after a success)


def _load_accelerated(provider):
    """The model on an accelerated provider, proven by one short synthesis.

    CUDA can load and still fail on first use (driver too old for the CUDA
    build, no kernel image for the card, out of VRAM). Returns None on failure.
    """
    global _last_accelerated_error
    _last_accelerated_error = None
    try:
        kokoro = _load_on(provider)
        kokoro.create("Ready.", voice=_PROBE_VOICE, speed=1.0)
        return kokoro
    except Exception as error:
        _last_accelerated_error = error
        log("[voiceover] %s failed, falling back to CPU: %s" % (provider, error))
        engine_log("device", "fallback provider=%s error=%s" % (provider, error))
        return None


def load_kokoro():
    """Load the Kokoro model on the configured device, falling back to CPU."""
    requested = choose_onnx_provider()
    kokoro = _load_accelerated(requested) if requested != _CPU else None
    if kokoro is None:
        kokoro = _load_on(_CPU)
    engine_log("device", "requested=%s active=%s" % (requested, _active_provider(kokoro)))
    return kokoro

# --- audio playback (inline port of the cross-platform player) --------------

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




def get_voice_settings(voice):
    """Per-voice settings with a sane default for unknown voices."""
    return VOICE_SETTINGS.get(voice, DEFAULT_VOICE_SETTINGS)


# --- the engine's synthesizer ------------------------------------------------

VRAM_CACHE_SECONDS = 5
VRAM_QUERY_TIMEOUT = 1.0


def _query_vram_mib(pid):
    """This process's GPU memory in MiB from nvidia-smi, or None when unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=VRAM_QUERY_TIMEOUT, check=True).stdout
        for line in out.splitlines():
            fields = [part.strip() for part in line.split(",")]
            if len(fields) == 2 and fields[0] == str(pid):
                return int(fields[1])
    except Exception:
        pass
    return None


INPUT_ERRORS = (ValueError, TypeError, KeyError)   # the caller's fault: CPU would fail the same way


class KokoroSynth:
    """The real Synth: Kokoro on CUDA when it works, else CPU. Construction is cheap; load() does the work.

    status() and synthesize() only read cached state: the VRAM figure is refreshed by a daemon timer
    thread (started by load(), every VRAM_CACHE_SECONDS while on cuda), so nvidia-smi never sits on
    the synthesis path or the event loop."""

    def __init__(self, force_cpu=False, vram_interval=VRAM_CACHE_SECONDS):
        self.loaded = False
        self._vram_interval = vram_interval
        self._force_cpu = force_cpu
        self._kokoro = None
        self._requested = "cpu"
        self._placement = ("cpu", None)   # (device, fallback), replaced as one value
        self._vram = None

    def load(self):
        from engine_server import NoModel
        if not models_present():
            raise NoModel("Kokoro model files are missing")
        import numpy  # noqa: F401  eager (S7): everything is imported before serving
        import onnxruntime  # noqa: F401
        self._requested = "cuda" if choose_onnx_provider() == _CUDA else "cpu"
        keep_off = self._keep_off()
        if self._force_cpu or keep_off:
            self._kokoro = _load_on(_CPU)
            fallback = self._fallback_record("keep_off") if keep_off else None
            self._placement = ("cpu", fallback)
        else:
            self._load_requested()
        self.loaded = True
        threading.Thread(target=self._vram_loop, name="vram-timer", daemon=True).start()

    @staticmethod
    def _keep_off():
        import engine_paths
        return engine_paths.read_keep_off()

    @staticmethod
    def _fallback_record(reason):
        return {"reason": reason, "at": time.time()}

    @staticmethod
    def _failure_reason(error):
        return "%s: %s" % (type(error).__name__, error)

    def _load_requested(self):
        fallback = None
        try:
            self._kokoro = load_kokoro()
        except Exception as error:
            self._kokoro = _load_on(_CPU)
            fallback = self._fallback_record("load_failed: " + self._failure_reason(error))
        device = self._device_of(self._kokoro)
        if fallback is None and self._requested == "cuda" and device == "cpu":
            cause = _last_accelerated_error
            reason = self._failure_reason(cause) if cause is not None else "CUDA unavailable"
            fallback = self._fallback_record("load_failed: " + reason)
        self._placement = (device, fallback)

    @staticmethod
    def _device_of(kokoro):
        return "cuda" if _active_provider(kokoro) == _CUDA else "cpu"

    def voices(self):
        return set(self._kokoro.get_voices()) if self._kokoro is not None else set()

    def default_speed(self, voice):
        return get_voice_settings(voice)["speed"]

    def synthesize(self, text, voice, speed, lang):
        from engine_protocol import pcm16le
        try:
            samples, _rate = self._kokoro.create(text, voice, speed, lang, trim=True)
        except INPUT_ERRORS:
            raise
        except Exception as error:
            if self._placement[0] != "cuda":
                raise
            self._fall_back_to_cpu(error)
            samples, _rate = self._kokoro.create(text, voice, speed, lang, trim=True)
        return pcm16le(samples)

    def _fall_back_to_cpu(self, error):
        self._kokoro = _load_on(_CPU)
        self._placement = ("cpu", self._fallback_record("synthesis_failed: " + self._failure_reason(error)))
        self._vram = None
        engine_log("device", "synthesis fallback to cpu: %s" % error)

    def status(self):
        device, fallback = self._placement
        return {"device": device, "requested": self._requested,
                "fallback": fallback, "vram_mib": self._vram if device == "cuda" else None}

    def _vram_loop(self):
        """Daemon timer thread: refresh the cached VRAM figure every interval while on cuda."""
        while True:
            self._refresh_vram()
            time.sleep(self._vram_interval)

    def _refresh_vram(self):
        if self._placement[0] != "cuda":
            return
        vram = _query_vram_mib(os.getpid())
        if self._placement[0] == "cuda":    # a fallback may have landed during the query
            self._vram = vram
