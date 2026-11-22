#!/usr/bin/env python3
"""Claude Voiceover voice CLI.

Usage:
    manage_voices.py list                  Show all voices and the current selection
    manage_voices.py set NAME [--project]  Pick a voice by friendly name
    manage_voices.py test [NAME]           Speak a sample line (current voice if omitted)
    manage_voices.py recommend             Suggest the best voice for this machine

Friendly names cover the 37 Phonon voices, all 13 Kokoro neural voices and the two
built-in macOS voices.
Stdlib only; audio itself runs in subprocesses.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from voiceover import settings  # noqa: E402
from voiceover.speech import one_shot_command, phonon_command  # noqa: E402

SAMPLE_TEXT = "Hello! This is your Claude Voiceover narrator."
SAMPLE_TIMEOUT = 120  # seconds; a first run may also build the engine's environment
FETCH_TIMEOUT = 120
RECOMMENDED_PHONON = "zoey"  # placeholder until the user picks one after hearing them

# Friendly name -> engine + kokoro voice id + human label.
# All 13 Kokoro voices, plus the two zero-setup macOS system voices.
VOICES = {
    "alloy":          {"engine": "kokoro", "voice": "af_alloy",   "label": "Alloy (American Female)"},
    "river":          {"engine": "kokoro", "voice": "af_river",   "label": "River (American Female)"},
    "sky":            {"engine": "kokoro", "voice": "af_sky",     "label": "Sky (American Female)"},
    "sarah":          {"engine": "kokoro", "voice": "af_sarah",   "label": "Sarah (American Female)"},
    "nicole":         {"engine": "kokoro", "voice": "af_nicole",  "label": "Nicole (American Female, Whispering)"},
    "adam":           {"engine": "kokoro", "voice": "am_adam",    "label": "Adam (American Male)"},
    "echo":           {"engine": "kokoro", "voice": "am_echo",    "label": "Echo (American Male)"},
    "puck":           {"engine": "kokoro", "voice": "am_puck",    "label": "Puck (American Male)"},
    "michael":        {"engine": "kokoro", "voice": "am_michael", "label": "Michael (American Male)"},
    "emma":           {"engine": "kokoro", "voice": "bf_emma",    "label": "Emma (British Female)"},
    "daniel":         {"engine": "kokoro", "voice": "bm_daniel",  "label": "Daniel (British Male)"},
    "lewis":          {"engine": "kokoro", "voice": "bm_lewis",   "label": "Lewis (British Male)"},
    "george":         {"engine": "kokoro", "voice": "bm_george",  "label": "George (British Male)"},
    "default-female": {"engine": "macos-female", "voice": None,   "label": "macOS Samantha (zero setup)"},
    "default-male":   {"engine": "macos-male",   "voice": None,   "label": "macOS Daniel (zero setup)"},
}



def _phonon_voices():
    """Friendly name -> spec for every Phonon voice: the name lower-cased and
    hyphenated (none collides with a Kokoro or macOS name)."""
    groups = ((settings.PHONON_VOICES, "Phonon"), (settings.PHONON_CHARACTER_VOICES, "Phonon character"))
    return {voice.lower().replace(" ", "-"): {"engine": "phonon", "voice": voice, "label": f"{voice} ({kind})"}
            for voices, kind in groups for voice in voices}


VOICES.update(_phonon_voices())

ENGINE_LABELS = {"kokoro": "Kokoro", "phonon": "Phonon"}  # the engines setup installs


def is_macos():
    return platform.system() == "Darwin"


def uv_available():
    return shutil.which("uv") is not None


def kokoro_ready():
    return settings.models_present() and uv_available()


def phonon_ready():
    return settings.phonon_model_present() and uv_available()


def engine_ready(engine):
    """Whether an installable engine (Kokoro, Phonon) is set up on this machine."""
    return {"kokoro": kokoro_ready, "phonon": phonon_ready}[engine]()


def dry_run():
    return bool(os.environ.get("VOICEOVER_DRY_RUN"))


def current_selection():
    """Return (engine, voice) as currently resolved."""
    return settings.resolve_engine(), settings.get_voice()


def friendly_name_for(engine, voice):
    """Reverse-map the resolved engine+voice to a friendly name, if any."""
    for name, spec in VOICES.items():
        if spec["engine"] == engine and (spec["voice"] is None or spec["voice"] == voice):
            return name
    return None


def voice_availability(spec):
    """Human-readable availability of one voice on this machine."""
    if spec["engine"] in ENGINE_LABELS:
        if engine_ready(spec["engine"]):
            return "ready"
        if not uv_available():
            return "needs uv (run /voiceover:setup)"
        return "needs model download (run /voiceover:setup)"
    return "ready" if is_macos() else "macOS only"


LIST_GROUPS = ("Phonon voices", "Phonon character voices", "Kokoro neural voices", "macOS system voices")


def list_group(spec):
    """The heading a voice is listed under."""
    if spec["engine"] == "phonon":
        return LIST_GROUPS[1] if spec["voice"] in settings.PHONON_CHARACTER_VOICES else LIST_GROUPS[0]
    return LIST_GROUPS[2] if spec["engine"] == "kokoro" else LIST_GROUPS[3]


def cmd_list(_args):
    engine, voice = current_selection()
    active = friendly_name_for(engine, voice)
    print("Claude Voiceover voices")
    for group in LIST_GROUPS:
        print()
        print(f"{group}:")
        for name, spec in VOICES.items():
            if list_group(spec) == group:
                marker = ">>>" if name == active else "   "
                print(f"{marker} {name:15} {spec['label']:42} [{voice_availability(spec)}]")
    print()
    if active:
        print(f"Current voice: {active} ({engine})")
    else:
        print(f"Current engine: {engine}" + (f", voice: {voice}" if voice else ""))
    print("Set one with: /voiceover:voice NAME")
    return 0


def apply_voice(name, project):
    """Write engine (and kokoro voice id) settings for a friendly name."""
    spec = VOICES[name]
    scope = "project" if project else "global"
    settings.set_setting("tts_engine", spec["engine"], scope=scope)
    settings.set_setting("voice", spec["voice"], scope=scope)
    return spec


def cmd_set(args):
    name = args.name.lower()
    if name not in VOICES:
        print(f"Unknown voice: {args.name}")
        print(f"Valid names: {', '.join(VOICES)}")
        return 1
    spec = VOICES[name]
    engine = spec["engine"]
    if engine in ENGINE_LABELS and not engine_ready(engine):
        print(f"Note: {ENGINE_LABELS[engine]} is not fully set up yet ({voice_availability(spec)}).")
        print("The voice is saved and will work once setup completes.")
    elif engine == "phonon" and not dry_run() and not fetch_phonon_voice(spec["voice"]):
        print(f"The {name} voice could not be downloaded, so nothing was saved.")
        return 1
    if engine.startswith("macos") and not is_macos():
        print(f"Warning: {name} uses macOS system speech and this machine is {platform.system()}.")
    apply_voice(name, args.project)
    scope = "project" if args.project else "global"
    print(f"Voice set to {name} - {spec['label']} ({scope} setting)")
    return 0


def fetch_phonon_voice(voice):
    """Load a Phonon voice once, which caches it (a catalogue voice is fetched
    on first use). True on success. Never starts the model download: callers
    only fetch once Phonon is set up."""
    try:
        return subprocess.run(phonon_command("--fetch-voice", voice), timeout=FETCH_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        print(f"Fetching the {voice} voice timed out after {FETCH_TIMEOUT}s.")
        return False
    except OSError as error:
        print(f"Could not launch the Phonon engine: {error}")
        return False


def speak_sample(engine, voice, text):
    """Speak one sample line through the chosen engine. Returns process exit code."""
    if dry_run():
        print(f"[voiceover] {text}", file=sys.stderr)
        return 0
    command = one_shot_command(engine, voice, text, stream=False)
    if command is None:
        print(f"Nothing to test: engine is '{engine}'.")
        return 1
    try:
        return subprocess.run(command, timeout=SAMPLE_TIMEOUT).returncode
    except FileNotFoundError as error:
        print(f"Could not launch TTS: {error}")
        return 1
    except subprocess.TimeoutExpired:
        print("TTS test timed out.")
        return 1


def cmd_test(args):
    if args.name:
        name = args.name.lower()
        if name not in VOICES:
            print(f"Unknown voice: {args.name}")
            print(f"Valid names: {', '.join(VOICES)}")
            return 1
        spec = VOICES[name]
        engine, voice = spec["engine"], spec["voice"]
        print(f"Testing {name} - {spec['label']}...")
    else:
        engine, voice = current_selection()
        print(f"Testing current voice ({friendly_name_for(engine, voice) or engine})...")
    code = speak_sample(engine, voice, SAMPLE_TEXT)
    print("Done." if code == 0 else "Test failed.")
    return code


def cmd_recommend(_args):
    if phonon_ready():
        print(f"Recommended: {RECOMMENDED_PHONON} - {VOICES[RECOMMENDED_PHONON]['label']}")
        print("Phonon neural voices are installed; list them with /voiceover:voice.")
        return 0
    _recommend_without_phonon()
    print("Phonon voices (about 65MB, light on memory) can be added with /voiceover:setup.")
    return 0


def _recommend_without_phonon():
    if kokoro_ready():
        pick = "puck" if is_macos() else "sarah"
        print(f"Recommended: {pick} - {VOICES[pick]['label']}")
        print("Kokoro neural voices are installed; any of the 13 will sound great.")
    elif is_macos():
        print("Recommended: default-female - macOS Samantha (works right now, zero setup)")
        print("For nicer neural voices, run /voiceover:setup to install Kokoro.")
    else:
        print("Recommended: run /voiceover:setup to install the Kokoro neural voices.")
        print("(No built-in system voice is wired up on this platform.)")


def build_parser():
    parser = argparse.ArgumentParser(prog="manage_voices.py", description="Claude Voiceover voice CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show all voices and the current selection").set_defaults(func=cmd_list)
    sub.add_parser("recommend", help="Suggest the best voice for this machine").set_defaults(func=cmd_recommend)

    set_parser = sub.add_parser("set", help="Pick a voice by friendly name")
    set_parser.add_argument("name")
    set_parser.add_argument("--project", action="store_true", help="Save for this project only")
    set_parser.set_defaults(func=cmd_set)

    test_parser = sub.add_parser("test", help="Speak a sample line")
    test_parser.add_argument("name", nargs="?", help="Friendly voice name (default: current voice)")
    test_parser.set_defaults(func=cmd_test)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
