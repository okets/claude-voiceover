"""Phonon in the voice and settings commands (scripts/manage_voices.py, manage_settings.py).

Isolated from the checkout it runs in: the scripts resolve settings from the
working directory, so the test moves into a temp project of its own (a .git
marker) first - never the repo - and every settings read it makes names that
project as cwd. subprocess.run is captured: nothing is fetched, spoken or run.
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
PROJECT = temp_dir("vo-phonon-cli-project-")
(PROJECT / ".git").mkdir()
os.chdir(PROJECT)
CWD = str(PROJECT)
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import manage_settings  # noqa: E402
import manage_voices  # noqa: E402
from voiceover import settings, speech  # noqa: E402


def run(func, *argv):
    """Run a CLI entry point; (exit code, printed output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = func(list(argv))
    return code, out.getvalue()


def current():
    return settings.get_setting("tts_engine", cwd=CWD), settings.get_setting("voice", cwd=CWD)


class Patched:
    """Temporarily replace attributes of manage_voices; restores them on exit."""

    def __init__(self, **values):
        self._values, self._saved = values, {}

    def __enter__(self):
        for name, value in self._values.items():
            self._saved[name] = getattr(manage_voices, name)
            setattr(manage_voices, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(manage_voices, name, value)


runs = []


def fake_run(returncode=0, raises=None):
    def run_it(command, **kwargs):
        runs.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode)
    return run_it


class FakeSubprocess:
    """manage_voices.subprocess with run() captured; everything else real."""

    def __init__(self, run_it):
        self.run = run_it
        self.TimeoutExpired = subprocess.TimeoutExpired


@contextlib.contextmanager
def not_dry_run():
    saved = os.environ.pop("VOICEOVER_DRY_RUN", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["VOICEOVER_DRY_RUN"] = saved


# --- the catalogue ------------------------------------------------------------------------
PRE_PHONON = {  # friendly name -> (engine, voice), as before Phonon
    "alloy": ("kokoro", "af_alloy"), "river": ("kokoro", "af_river"), "sky": ("kokoro", "af_sky"),
    "sarah": ("kokoro", "af_sarah"), "nicole": ("kokoro", "af_nicole"), "adam": ("kokoro", "am_adam"),
    "echo": ("kokoro", "am_echo"), "puck": ("kokoro", "am_puck"), "michael": ("kokoro", "am_michael"),
    "emma": ("kokoro", "bf_emma"), "daniel": ("kokoro", "bm_daniel"), "lewis": ("kokoro", "bm_lewis"),
    "george": ("kokoro", "bm_george"), "default-female": ("macos-female", None),
    "default-male": ("macos-male", None),
}
VOICES = manage_voices.VOICES
phonon_entries = [spec for spec in VOICES.values() if spec["engine"] == "phonon"]
check("all 37 Phonon voices are in the catalogue", len(phonon_entries) == 37, len(phonon_entries))
check("Kokoro's thirteen names (and the macOS two) keep their meaning",
      all((VOICES[name]["engine"], VOICES[name]["voice"]) == pair for name, pair in PRE_PHONON.items()))
check("a character voice has a hyphenated friendly name",
      VOICES["captain-silas-blackwater"]["voice"] == "Captain Silas Blackwater")
check("labels say which Phonon group a voice is in",
      (VOICES["bryony"]["label"], VOICES["captain-silas-blackwater"]["label"])
      == ("Bryony (Phonon)", "Captain Silas Blackwater (Phonon character)"))
check("friendly_name_for reverse-maps a Phonon voice",
      manage_voices.friendly_name_for("phonon", "Elodie-Rose") == "elodie-rose")

# --- availability ---------------------------------------------------------------------------
bryony = VOICES["bryony"]
with Patched(uv_available=lambda: True):
    hf = temp_dir("vo-hf-")
    os.environ["HF_HOME"] = str(hf)
    check("Phonon without its model needs the download",
          manage_voices.voice_availability(bryony) == "needs model download (run /voiceover:setup)")
    weights = hf / "hub" / "models--gradium--phonon-31m-en" / "snapshots" / "rev" / "model.q8.gguf"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"\0")
    check("Phonon with uv and its model is ready", manage_voices.voice_availability(bryony) == "ready")
with Patched(uv_available=lambda: False):
    check("Phonon without uv says so", manage_voices.voice_availability(bryony) == "needs uv (run /voiceover:setup)")
del os.environ["HF_HOME"]

# --- set --------------------------------------------------------------------------------------
with Patched(phonon_ready=lambda: False):
    code, out = run(manage_voices.main, "set", "bryony")
check("setting a Phonon voice sets engine and voice together", (code, current()) == (0, ("phonon", "Bryony")))
check("before setup it is saved with the setup note",
      "Phonon is not fully set up yet" in out and "will work once setup completes" in out, out)
code, out = run(manage_voices.main, "set", "echo")
check("setting a Kokoro voice afterwards switches back", (code, current()) == (0, ("kokoro", "am_echo")))

before = current()
with not_dry_run(), Patched(phonon_ready=lambda: True, subprocess=FakeSubprocess(fake_run(returncode=1))):
    runs.clear()
    code, out = run(manage_voices.main, "set", "maeve")
check("a voice that cannot be fetched is not saved", code == 1 and current() == before, (code, current()))
check("and the command says so", "could not be downloaded" in out and "nothing was saved" in out.lower(), out)
check("the fetch runs the Phonon engine's --fetch-voice with a generous timeout",
      runs and runs[0][0] == speech.phonon_command("--fetch-voice", "Maeve") and runs[0][1].get("timeout", 0) >= 120,
      runs)
with not_dry_run(), Patched(phonon_ready=lambda: True,
                            subprocess=FakeSubprocess(fake_run(raises=subprocess.TimeoutExpired("uv", 120)))):
    code, out = run(manage_voices.main, "set", "maeve")
check("a fetch that times out saves nothing", code == 1 and current() == before and "timed out" in out, out)
with not_dry_run(), Patched(phonon_ready=lambda: True, subprocess=FakeSubprocess(fake_run(returncode=0))):
    runs.clear()
    code, out = run(manage_voices.main, "set", "maeve")
check("a fetched voice is saved after the fetch", (code, current()) == (0, ("phonon", "Maeve")) and len(runs) == 1)
with not_dry_run(), Patched(phonon_ready=lambda: False, subprocess=FakeSubprocess(fake_run())):
    runs.clear()
    code, out = run(manage_voices.main, "set", "zoey")
check("set never starts a download before setup", runs == [] and (code, current()) == (0, ("phonon", "Zoey")))

# --- list and test ----------------------------------------------------------------------------
run(manage_voices.main, "set", "bryony")
code, listing = run(manage_voices.main, "list")
headings = [line for line in listing.splitlines() if line.endswith(":") and not line.startswith(" ")]
check("the list groups voices by engine, Phonon's two groups apart",
      [h for h in headings if "voices" in h] ==
      ["Phonon voices:", "Phonon character voices:", "Kokoro neural voices:", "macOS system voices:"], headings)
check("the list marks the current Phonon voice", any(line.startswith(">>> bryony ") for line in listing.splitlines()))
check("each line keeps its shape: marker, name, label, availability",
      any(line.startswith("    echo ") and "Echo (American Male)" in line and line.endswith("]")
          for line in listing.splitlines()), listing)
code, out = run(manage_voices.main, "test")
check("test with no name says which voice it is testing", "Testing current voice (bryony)" in out, out)

with not_dry_run(), Patched(subprocess=FakeSubprocess(fake_run())):
    runs.clear()
    manage_voices.speak_sample("phonon", "Bryony", "Hi.")
    manage_voices.speak_sample("kokoro", "af_sky", "Hi.")
    manage_voices.speak_sample("macos-female", None, "Hi.")
check("the sample goes through speech's one-shot command builder",
      [command for command, _ in runs] == [speech.one_shot_command("phonon", "Bryony", "Hi.", stream=False),
                                          speech.one_shot_command("kokoro", "af_sky", "Hi.", stream=False),
                                          speech.one_shot_command("macos-female", None, "Hi.", stream=False)], runs)
check("the Kokoro sample command is unchanged", runs[1][0] == speech.kokoro_command("--voice", "af_sky", "Hi."))
check("the one-shot builder streams by default, as dispatch does",
      speech.one_shot_command("phonon", "Bryony", "Hi.") == speech.phonon_command("--voice", "Bryony", "--stream", "Hi.")
      and speech.one_shot_command("none", None, "Hi.") is None)

# --- recommend ------------------------------------------------------------------------------------
with Patched(phonon_ready=lambda: True):
    code, recommended = run(manage_voices.main, "recommend")
check("recommend prefers Phonon once it is installed", recommended.lower().startswith("recommended: zoey"), recommended)
for kokoro, mac in ((True, True), (True, False), (False, True), (False, False)):
    if kokoro:
        before_first = "Recommended: %s - %s" % ("puck" if mac else "sarah", VOICES["puck" if mac else "sarah"]["label"])
    elif mac:
        before_first = "Recommended: default-female - macOS Samantha (works right now, zero setup)"
    else:
        before_first = "Recommended: run /voiceover:setup to install the Kokoro neural voices."
    with Patched(phonon_ready=lambda: False, kokoro_ready=lambda k=kokoro: k, is_macos=lambda m=mac: m):
        code, recommended_without = run(manage_voices.main, "recommend")
    check("recommend is unchanged without Phonon (kokoro=%s, mac=%s)" % (kokoro, mac),
          recommended_without.splitlines()[0] == before_first, recommended_without)
    check("and mentions Phonon once (kokoro=%s, mac=%s)" % (kokoro, mac),
          sum("Phonon" in line for line in recommended_without.splitlines()) == 1, recommended_without)

# --- manage_settings ---------------------------------------------------------------------------------
check("manage_settings accepts the phonon engine", manage_settings.normalize("tts_engine", "phonon") == "phonon")
check("manage_settings accepts a Phonon voice", manage_settings.normalize("voice", "Bryony") == "Bryony")
check("and a multi-word one", manage_settings.normalize("voice", "Old Mortlake") == "Old Mortlake")
check("and still a Kokoro id or none",
      (manage_settings.normalize("voice", "af_sky"), manage_settings.normalize("voice", None)) == ("af_sky", None))
rejected = []
for bad in ("bryony", "nobody"):
    try:
        manage_settings.normalize("voice", bad)
    except ValueError:
        rejected.append(bad)
check("it rejects a name that is neither", rejected == ["bryony", "nobody"], rejected)
code, info = run(manage_settings.main, "info")
check("info reports the Phonon model beside Kokoro's",
      "Kokoro models:" in info and "Phonon model:" in info, info)

report()
