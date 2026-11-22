"""The phonon engine setting, Phonon's voice lists, and engine-aware voice resolution.

Isolated from the checkout it runs in: every project is a temp directory of
its own with a .git marker, passed as cwd, so the repo's own
.claude/voiceover.json is never read.
"""

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

isolate()
from voiceover import settings


def new_project(prefix="vo-phonon-project-"):
    root = temp_dir(prefix)
    (root / ".git").mkdir()
    return root


def configure(root, engine, voice):
    """A project whose own settings name engine and voice (None: no voice)."""
    settings.set_setting("tts_engine", engine, scope="project", cwd=str(root))
    settings.set_setting("voice", voice, scope="project", cwd=str(root))


def engine_for(engine):
    root = new_project()
    configure(root, engine, None)
    return settings.resolve_engine(cwd=str(root))


def voice_for(engine, voice, root=None):
    root = root or new_project()
    configure(root, engine, voice)
    return settings.get_voice(cwd=str(root))


# The resolution and the hash pick as they were before Phonon, written out
# here so the checks compare against the old code, not the new code twice.
PRE_PHONON_KOKORO_VOICES = [
    "af_alloy", "af_river", "af_sky", "af_sarah", "af_nicole", "am_adam", "am_echo",
    "am_puck", "am_michael", "bf_emma", "bm_daniel", "bm_lewis", "bm_george",
]


def pre_phonon_auto():
    if sys.platform == "darwin":
        return "macos-female"
    return "kokoro" if settings.models_present() else "none"


def pre_phonon_kokoro_pick(root):
    path = str(settings.detect_project_root(str(root)))
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
    return PRE_PHONON_KOKORO_VOICES[int(digest, 16) % len(PRE_PHONON_KOKORO_VOICES)]


def friendly_names():
    spec = importlib.util.spec_from_file_location("manage_voices", REPO_ROOT / "scripts" / "manage_voices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.VOICES)


# --- the engine ---------------------------------------------------------------------
check("phonon is an explicit engine", engine_for("phonon") == "phonon")
check("auto is unchanged on this platform", engine_for("auto") == pre_phonon_auto())
check("kokoro is still an explicit engine", engine_for("kokoro") == "kokoro")

# --- the voice lists ----------------------------------------------------------------
check("25 standard and 12 character voices",
      (len(settings.PHONON_VOICES), len(settings.PHONON_CHARACTER_VOICES)) == (25, 12))
check("the standard voices are in catalogue order",
      settings.PHONON_VOICES[:3] == ["Zoey", "Juniper", "Everly"] and settings.PHONON_VOICES[-1] == "Bryony")
check("the bundled fallback voice is 'default'", settings.PHONON_DEFAULT_VOICE == "default")
phonon_keys = {name.lower().replace(" ", "-")
               for name in settings.PHONON_VOICES + settings.PHONON_CHARACTER_VOICES}
collisions = sorted(phonon_keys & friendly_names())
check("no Phonon name collides with a Kokoro friendly name", not collisions, collisions)
check("is_phonon_voice knows every voice and 'default'",
      all(settings.is_phonon_voice(name) for name in
          settings.PHONON_VOICES + settings.PHONON_CHARACTER_VOICES + ["default"]))
check("is_phonon_voice is exact-case and rejects other ids",
      not any(settings.is_phonon_voice(name) for name in ("bryony", "af_sky", "", None, "Missing")))

# --- engine-aware voice resolution --------------------------------------------------
check("an explicit Phonon voice wins under phonon", voice_for("phonon", "Bryony") == "Bryony")
check("a character voice may be chosen explicitly",
      voice_for("phonon", "Captain Silas Blackwater") == "Captain Silas Blackwater")
check("a Kokoro id under phonon falls back to a standard Phonon voice",
      voice_for("phonon", "af_sky") in settings.PHONON_VOICES)
check("an unknown string under phonon falls back to a standard Phonon voice",
      voice_for("phonon", "zf_custom") in settings.PHONON_VOICES)
fifty_roots = [new_project("vo-phonon-pick-") for _ in range(50)]
picks = [voice_for("phonon", None, root) for root in fifty_roots]
check("the automatic Phonon pick is never a character voice",
      all(pick in settings.PHONON_VOICES for pick in picks), picks)
check("the automatic Phonon pick varies by project", len(set(picks)) > 5, sorted(set(picks)))
stable_root = fifty_roots[0]
check("the automatic Phonon pick is stable per project",
      settings.get_voice(cwd=str(stable_root)) == picks[0])
check("Kokoro still passes any explicit string through", voice_for("kokoro", "zf_custom") == "zf_custom")
check("Kokoro still honours an explicit Kokoro voice", voice_for("kokoro", "bf_emma") == "bf_emma")
kokoro_root = new_project()
check("a Phonon name under kokoro falls back to the Kokoro pick",
      voice_for("kokoro", "Bryony", kokoro_root) == pre_phonon_kokoro_pick(kokoro_root))
check("the Kokoro automatic pick is unchanged",
      all(voice_for("kokoro", None, root) == pre_phonon_kokoro_pick(root) for root in fifty_roots[:20]))

# --- the model-present check --------------------------------------------------------
hf_home = temp_dir("vo-hf-home-")
os.environ["HF_HOME"] = str(hf_home)
check("no model, not present", settings.phonon_model_present() is False)
snapshot = hf_home / "hub" / "models--gradium--phonon-31m-en" / "snapshots" / "92741c5b07d085ff840e26fea8ce43110b87852f"
snapshot.mkdir(parents=True)
(snapshot / "config.json").write_text("{}")
check("a snapshot without the 8-bit weights is not present", settings.phonon_model_present() is False)
(snapshot / "model.q8.gguf").write_bytes(b"\0")
check("the 8-bit weights file makes it present", settings.phonon_model_present() is True)
fake_home = temp_dir("vo-fake-home-")
del os.environ["HF_HOME"]
real_home = Path.home
Path.home = classmethod(lambda cls: fake_home)
try:
    check("without HF_HOME the default cache is ~/.cache/huggingface/hub",
          settings.phonon_model_present() is False)
    default_snapshot = fake_home / ".cache" / "huggingface" / "hub" / "models--gradium--phonon-31m-en" / "snapshots" / "x"
    default_snapshot.mkdir(parents=True)
    (default_snapshot / "model.q8.gguf").write_bytes(b"\0")
    check("and a model there counts", settings.phonon_model_present() is True)
finally:
    Path.home = real_home

report()
