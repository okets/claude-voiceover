"""Phonon in first-run setup (scripts/setup_tts.py).

Isolated from the checkout it runs in: setup resolves settings from the
working directory, so the test moves into a temp project of its own (a .git
marker) first, and HF_HOME points at a temp cache. subprocess.run and the
Kokoro download are captured: nothing starts uv, downloads or plays audio.
"""

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

isolate()
PROJECT = temp_dir("vo-phonon-setup-project-")
(PROJECT / ".git").mkdir()
os.chdir(PROJECT)
CWD = str(PROJECT)
HF = temp_dir("vo-phonon-setup-hf-")
os.environ["HF_HOME"] = str(HF)
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import setup_tts  # noqa: E402
from voiceover import settings, speech  # noqa: E402


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = setup_tts.main(list(argv))
    return code, out.getvalue()


def install_phonon_model():
    weights = HF / "hub" / "models--gradium--phonon-31m-en" / "snapshots" / "rev" / "model.q8.gguf"
    weights.parent.mkdir(parents=True, exist_ok=True)
    weights.write_bytes(b"\0")


def set_engine(engine):
    settings.set_setting("tts_engine", engine, scope="project", cwd=CWD)


runs = []


@contextlib.contextmanager
def patched(module, **values):
    saved = {name: getattr(module, name) for name in values}
    for name, value in values.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


def fake_run(returncode=0, raises=None):
    def run_it(command, **kwargs):
        runs.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode)
    return run_it


class FakeSubprocess:
    def __init__(self, run_it):
        self.run = run_it
        self.TimeoutExpired = subprocess.TimeoutExpired


kokoro_downloads = []
setup_tts.download_models = lambda: kokoro_downloads.append(1) or True
setup_tts.subprocess = FakeSubprocess(fake_run())

# --- the status report ----------------------------------------------------------------------
set_engine("kokoro")
code, out = run()
check("the status reports Phonon before Kokoro", "Phonon" in out and out.index("Phonon") < out.index("Kokoro neural voices"),
      out)
check("a missing Phonon model names the command that installs it", "--download-phonon" in out, out)
check("and nothing claims it is installed", "Phonon is installed but not selected" not in out)
install_phonon_model()
code, out = run()
check("installed but not selected is said plainly",
      "Phonon is installed but not selected. Pick a voice with /voiceover:voice." in out, out)
set_engine("phonon")
code, out = run()
check("selected, the not-selected line goes away", "installed but not selected" not in out, out)
set_engine("kokoro")

# --- --download-phonon ----------------------------------------------------------------------
engine_before = settings.get_setting("tts_engine", cwd=CWD)
runs.clear()
setup_tts.subprocess = FakeSubprocess(fake_run(returncode=0))
code, out = run("--download-phonon")
check("--download-phonon runs the engine's installer",
      code == 0 and runs and runs[0][0] == speech.phonon_command("--install"), runs)
check("through uv, with its output on the terminal and a long timeout",
      runs[0][1].get("timeout", 0) >= 600 and not runs[0][1].get("capture_output")
      and runs[0][1].get("stdout") is None, runs[0][1])
check("then names the next step, with zoey as the suggestion",
      "/voiceover:voice" in out and "zoey" in out, out)
check("installing Phonon selects nothing", settings.get_setting("tts_engine", cwd=CWD) == engine_before)
check("--download-phonon does not download Kokoro", kokoro_downloads == [])

runs.clear()
setup_tts.subprocess = FakeSubprocess(fake_run(returncode=1))
code, out = run("--download-phonon")
check("a failed install exits 1 and says so", code == 1 and "Phonon install failed" in out, out)
setup_tts.subprocess = FakeSubprocess(fake_run(raises=subprocess.TimeoutExpired("uv", 600)))
code, out = run("--download-phonon")
check("a timed-out install exits 1", code == 1 and "timed out" in out, out)
setup_tts.subprocess = FakeSubprocess(fake_run(raises=FileNotFoundError("uv")))
code, out = run("--download-phonon")
check("no uv: exits 1 and names uv", code == 1 and "uv" in out, out)
check("a failed install selects nothing either", settings.get_setting("tts_engine", cwd=CWD) == engine_before)

runs.clear()
setup_tts.subprocess = FakeSubprocess(fake_run())
code, out = run("--download")
check("--download still means Kokoro", kokoro_downloads == [1] and runs == [], runs)

# --- the Linux audio line -------------------------------------------------------------------
with patched(setup_tts.platform, system=lambda: "Linux"), \
        patched(setup_tts.shutil, which=lambda name: None), \
        patched(setup_tts, portaudio_found=lambda: False):
    code, out_linux_no_player = run()
check("a missing Linux player is named, with what to install",
      "aplay" in out_linux_no_player and "install" in out_linux_no_player.lower(), out_linux_no_player)
with patched(setup_tts.platform, system=lambda: "Linux"), \
        patched(setup_tts.shutil, which=lambda name: "/usr/bin/pw-cat" if name == "pw-cat" else None), \
        patched(setup_tts, portaudio_found=lambda: False):
    code, out_linux = run()
check("an available Linux player is reported", "pw-cat" in out_linux, out_linux)
check("setup never imports sounddevice", "sounddevice" not in sys.modules)

# --- the test line ----------------------------------------------------------------------------
saved = os.environ.pop("VOICEOVER_DRY_RUN")
try:
    set_engine("phonon")
    settings.set_setting("voice", "Bryony", scope="project", cwd=CWD)
    runs.clear()
    setup_tts.subprocess = FakeSubprocess(fake_run())
    with contextlib.redirect_stdout(io.StringIO()):
        ok = setup_tts.speak_test()
    check("the test line goes through speech's one-shot builder",
          ok and runs and runs[0][0] == speech.one_shot_command("phonon", "Bryony", "Claude Voiceover is ready.",
                                                              stream=False), runs)
    set_engine("none")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = setup_tts.speak_test()
    check("with no engine selected but Phonon installed, the test names the command that selects it",
          not ok and "Phonon is installed but not selected" in out.getvalue(), out.getvalue())
finally:
    os.environ["VOICEOVER_DRY_RUN"] = saved

report()
