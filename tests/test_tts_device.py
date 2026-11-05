"""tts_device: Kokoro on CPU by default, opt-in NVIDIA GPU, CPU fallback, never on macOS."""

import json
import os
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, check, isolate, report, temp_dir

data_dir = isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import engine_common  # noqa: E402
import kokoro_voice  # noqa: E402
import manage_settings  # noqa: E402
from voiceover import settings, speech  # noqa: E402

SETTINGS_FILE = data_dir / "settings.json"
CPU, CUDA = "CPUExecutionProvider", "CUDAExecutionProvider"


def set_device(device):
    """Write the global tts_device and drop the engine's per-process cache."""
    SETTINGS_FILE.write_text(json.dumps({"tts_device": device}) if device else "{}")
    engine_common._global_settings = None
    os.environ.pop("ONNX_PROVIDER", None)


def fake_onnxruntime(providers, preload_fails=False):
    """Install a stub onnxruntime module; returns the list preload calls land in."""
    preloads = []
    module = types.ModuleType("onnxruntime")

    def preload_dlls():
        preloads.append(True)
        if preload_fails:
            raise OSError("libcudnn.so.9: cannot open shared object file")

    module.preload_dlls = preload_dlls
    module.get_available_providers = lambda: list(providers)
    sys.modules["onnxruntime"] = module
    return preloads


def fake_kokoro(fail_on=None):
    """Install a stub kokoro_onnx whose model records its provider; create() raises
    when the provider is fail_on. Returns the list of providers models were built on."""
    built = []

    class Model:
        def __init__(self, *args):
            self.provider = os.environ.get("ONNX_PROVIDER")
            built.append(self.provider)

        def create(self, text, voice, speed=1.0):
            if self.provider == fail_on:
                raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
            return ([0.0] * 10, 24000)

    module = types.ModuleType("kokoro_onnx")
    module.Kokoro = Model
    sys.modules["kokoro_onnx"] = module
    return built


# --- the setting is machine-wide ---------------------------------------------------
check("tts_device defaults to cpu", settings.DEFAULTS["tts_device"] == "cpu")
check("tts_device is a machine setting", "tts_device" in settings.MACHINE_SETTINGS)
check("cpu and gpu are accepted",
      [manage_settings.normalize("tts_device", d) for d in ("cpu", "gpu")] == ["cpu", "gpu"])
try:
    manage_settings.normalize("tts_device", "cuda")
    rejected = False
except ValueError:
    rejected = True
check("anything else is rejected", rejected)

project = temp_dir("vo-project-")
(project / ".claude").mkdir()
(project / ".claude" / "voiceover.json").write_text('{"tts_device": "gpu"}')
set_device(None)
os.chdir(project)
check("a project file's tts_device is ignored (launcher and engine read the global file)",
      settings.get_machine_setting("tts_device") == "cpu" and speech.kokoro_project() == speech._TTS_DIR)
check("set tts_device --project is refused",
      manage_settings.cmd_set(types.SimpleNamespace(key="tts_device", value="gpu", project=True)) == 1)
os.chdir(REPO_ROOT)

# --- which machines may use the gpu project ------------------------------------------
check("linux x86_64 is gpu-capable", speech.gpu_capable("linux", "x86_64"))
check("windows (AMD64) is gpu-capable", speech.gpu_capable("win32", "AMD64"))
check("macOS is never gpu-capable", not speech.gpu_capable("darwin", "x86_64")
      and not speech.gpu_capable("darwin", "arm64"))
check("arm linux is not gpu-capable", not speech.gpu_capable("linux", "aarch64"))

# --- the uv command ----------------------------------------------------------------
set_device(None)
cpu_cmd = speech.kokoro_command("--voice", "af_sky", "hello")
check("cpu: the default tts project", cpu_cmd[:4] == ["uv", "run", "--project", str(speech._TTS_DIR)], cpu_cmd)
check("cpu: script then args, --voice form intact for the kill switch",
      cpu_cmd[-4].endswith("kokoro_voice.py") and cpu_cmd[-3:] == ["--voice", "af_sky", "hello"], cpu_cmd)

set_device("gpu")
real_capable = speech.gpu_capable
speech.gpu_capable = lambda system=None, machine=None: True
gpu_cmd = speech.kokoro_command("--drain", "/tmp/root")
check("gpu on a capable machine: the tts-gpu project, same engine script",
      gpu_cmd[3] == str(speech._TTS_GPU_DIR) and gpu_cmd[4] == cpu_cmd[4], gpu_cmd)
speech.gpu_capable = lambda system=None, machine=None: False
check("gpu on an incapable machine (macOS): the default project",
      speech.kokoro_command("--drain", "/tmp/root")[3] == str(speech._TTS_DIR))
speech.gpu_capable = real_capable

# --- the engine's provider choice --------------------------------------------------
set_device(None)
fake_onnxruntime([CUDA, CPU])
check("cpu (default): CPU chosen even with CUDA present", kokoro_voice.choose_onnx_provider() == CPU)

set_device("gpu")
preloads = fake_onnxruntime(["TensorrtExecutionProvider", CUDA, CPU])
check("gpu: CUDA chosen (not TensorRT) when available", kokoro_voice.choose_onnx_provider() == CUDA)
check("gpu: CUDA/cuDNN wheels preloaded first", preloads == [True], preloads)

set_device("gpu")
fake_onnxruntime([CPU])
check("gpu without CUDA (CPU-only onnxruntime): CPU", kokoro_voice.choose_onnx_provider() == CPU)

set_device("gpu")
fake_onnxruntime([CUDA, CPU], preload_fails=True)
check("gpu whose libraries fail to load: CPU", kokoro_voice.choose_onnx_provider() == CPU)

set_device("gpu")
os.environ["ONNX_PROVIDER"] = "ROCMExecutionProvider"
check("an explicit ONNX_PROVIDER in the environment wins",
      kokoro_voice.choose_onnx_provider() == "ROCMExecutionProvider")

# --- loading: proven on the GPU, else CPU ------------------------------------------
set_device("gpu")
fake_onnxruntime([CUDA, CPU])
built = fake_kokoro()
model = kokoro_voice.load_kokoro()
check("gpu that works: one model, on CUDA", built == [CUDA] and model.provider == CUDA, built)

set_device("gpu")
fake_onnxruntime([CUDA, CPU])
built = fake_kokoro(fail_on=CUDA)
model = kokoro_voice.load_kokoro()
check("gpu that fails on first use: reloaded on CPU", built == [CUDA, CPU] and model.provider == CPU, built)

set_device(None)
built = fake_kokoro(fail_on=CUDA)
model = kokoro_voice.load_kokoro()
check("cpu: loaded once on CPU, no probe", built == [CPU] and model.provider == CPU, built)
os.environ.pop("ONNX_PROVIDER", None)
for name in ("onnxruntime", "kokoro_onnx"):
    sys.modules.pop(name, None)

# --- packaging: the default project is untouched; tts-gpu never reaches macOS -------
default_toml = (REPO_ROOT / "tts" / "pyproject.toml").read_text()
gpu_toml = (REPO_ROOT / "tts-gpu" / "pyproject.toml").read_text()


def dependencies(toml):
    block = toml.split("dependencies = [", 1)[1].split("\n]", 1)[0]
    return re.findall(r'"([^"]+)"', block)


check("the default tts project has no GPU packages", "onnxruntime-gpu" not in default_toml)
gpu_deps = dependencies(gpu_toml)
check("tts-gpu keeps every default dependency", set(dependencies(default_toml)) <= set(gpu_deps), gpu_deps)
gpu_line = next((d for d in gpu_deps if d.startswith("onnxruntime-gpu")), "")
check("onnxruntime-gpu is marked for x86_64 and AMD64, never darwin",
      "platform_machine == 'x86_64'" in gpu_line and "platform_machine == 'AMD64'" in gpu_line
      and "sys_platform != 'darwin'" in gpu_line, gpu_line)
check("the CPU onnxruntime is overridden away wherever the GPU wheel installs",
      "override-dependencies" in gpu_toml and "onnxruntime>=" in gpu_toml.split("override-dependencies", 1)[1])

report()
