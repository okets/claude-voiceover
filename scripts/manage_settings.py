#!/usr/bin/env python3
"""Claude Voiceover settings CLI.

Usage:
    manage_settings.py info                       Show settings files and effective values
    manage_settings.py get KEY                    Show one effective setting
    manage_settings.py set KEY VALUE [--project]  Write a setting (global by default)
    manage_settings.py levels                     Explain the four interaction levels

Wraps voiceover/settings.py. Stdlib only.
"""

import argparse
import json
import math
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from voiceover import settings  # noqa: E402

LEVEL_DESCRIPTIONS = {
    "silent": "Nothing at all - no speech, no sounds",
    "quiet": "Sound pings only (permission request, cycle done) - no speech",
    "concise": "Speaks permission requests and cycle completions [default]",
    "tools": "Tool narration: play-by-play of every tool, plus subagent completions",
    "narrator": "Claude's actual words, read aloud - tool chatter stays quiet",
}

NUMERIC_ALIASES = {"0": "silent", "1": "quiet", "2": "concise", "3": "tools", "4": "narrator"}

VALID_ENGINES = ["auto", "kokoro", "phonon", "macos-female", "macos-male", "none"]
VALID_DEVICES = ["cpu", "gpu"]


def project_settings_path(cwd=None):
    """Path of the optional per-project settings file."""
    base = Path(cwd) if cwd else Path.cwd()
    return base / ".claude" / "voiceover.json"


def global_settings_path():
    """Path of the global settings file."""
    return settings.data_dir() / "settings.json"


def describe_file(label, path):
    """Print one settings file's path and whether it exists."""
    status = "exists" if path.exists() else "not created (defaults apply)"
    print(f"  {label}: {path}  [{status}]")


def effective_settings():
    """Resolve every known setting through the hierarchy."""
    return {key: settings.get_setting(key) for key in settings.DEFAULTS}


def cmd_info(_args):
    print("Claude Voiceover settings")
    print()
    print("Files (project overrides global overrides defaults):")
    describe_file("Project", project_settings_path())
    describe_file("Global ", global_settings_path())
    print()
    print("Effective settings:")
    print(json.dumps(effective_settings(), indent=2))
    print()
    print(f"Resolved engine: {settings.resolve_engine()}")
    print(f"Resolved voice:  {settings.get_voice()}")
    print(f"Kokoro models:   {'installed' if settings.models_present() else 'not downloaded (run /voiceover:setup)'}")
    print(f"Phonon model:    {'installed' if settings.phonon_model_present() else 'not downloaded (run /voiceover:setup)'}")
    return 0


def cmd_levels(_args):
    current = settings.get_interaction_level()
    print("Interaction levels (set with: manage_settings.py set interaction_level LEVEL)")
    print()
    for number, level in enumerate(settings.LEVELS):
        marker = ">>>" if level == current else "   "
        print(f"{marker} {number}  {level:8} {LEVEL_DESCRIPTIONS.get(level, '')}")
    print()
    print(f"Current level: {current}")
    return 0


def cmd_get(args):
    if args.key not in settings.DEFAULTS:
        print(f"Unknown setting: {args.key}")
        print(f"Known settings: {', '.join(sorted(settings.DEFAULTS))}")
        return 1
    value = settings.get_setting(args.key)
    print(f"{args.key} = {json.dumps(value)}")
    return 0


def parse_value(raw):
    """Parse a CLI value: JSON when valid, plain string otherwise."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _check_engine_port(value):
    if not (isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535):
        raise ValueError("engine_port must be an integer from 1 to 65535, or null")


def _check_engine_idle(value):
    if not (_is_number(value) and value >= 5):
        raise ValueError("engine_idle_s must be a number of at least 5, or null")


def _check_engine_plugin_root(value):
    if not (isinstance(value, str) and Path(value).is_dir()):
        raise ValueError("engine_plugin_root must be an existing directory, or null")


_ENGINE_VALIDATORS = {
    "engine_port": _check_engine_port,
    "engine_idle_s": _check_engine_idle,
    "engine_plugin_root": _check_engine_plugin_root,
}


def normalize(key, value):
    """Validate and normalize a value for a known key. Raises ValueError."""
    if key == "interaction_level":
        if isinstance(value, int):
            value = str(value)
        value = NUMERIC_ALIASES.get(str(value), str(value)).strip().lower()
        # A level renamed in a past release is still accepted by its old name
        # and stored under the current one.
        value = settings.LEGACY_LEVEL_NAMES.get(value, value)
        if value not in settings.LEVELS:
            raise ValueError(f"interaction_level must be one of {settings.LEVELS} (or 0-4)")
        return value
    if key == "tts_engine":
        if value not in VALID_ENGINES:
            raise ValueError(f"tts_engine must be one of {VALID_ENGINES}")
        return value
    if key == "tts_device":
        if value not in VALID_DEVICES:
            raise ValueError(f"tts_device must be one of {VALID_DEVICES}")
        return value
    if key in _ENGINE_VALIDATORS:
        if value is not None:
            _ENGINE_VALIDATORS[key](value)
        return value
    if key == "voice":
        # Either engine's voice is accepted whatever tts_engine says: a voice
        # for the other engine falls back at resolution time. /voiceover:voice
        # sets engine and voice together.
        if value is not None and value not in settings.KOKORO_VOICES and not settings.is_phonon_voice(value):
            raise ValueError("voice must be null, a Kokoro id (%s) or a Phonon voice name (exact case)"
                             % ", ".join(settings.KOKORO_VOICES))
        return value
    if key in ("tts_enabled", "notification_sounds", "speak_subagent_completions"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    return value


def cmd_set(args):
    if args.key not in settings.DEFAULTS:
        print(f"Unknown setting: {args.key}")
        print(f"Known settings: {', '.join(sorted(settings.DEFAULTS))}")
        return 1
    try:
        value = normalize(args.key, parse_value(args.value))
    except ValueError as error:
        print(f"Invalid value: {error}")
        return 1
    if args.project and args.key in settings.MACHINE_SETTINGS:
        print(f"{args.key} is a machine-wide setting; set it without --project.")
        return 1
    scope = "project" if args.project else "global"
    settings.set_setting(args.key, value, scope=scope)
    target = project_settings_path() if args.project else global_settings_path()
    print(f"Set {args.key} = {json.dumps(value)} ({scope}: {target})")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="manage_settings.py", description="Claude Voiceover settings CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Show settings files and effective values").set_defaults(func=cmd_info)
    sub.add_parser("levels", help="Explain the four interaction levels").set_defaults(func=cmd_levels)

    get_parser = sub.add_parser("get", help="Show one effective setting")
    get_parser.add_argument("key")
    get_parser.set_defaults(func=cmd_get)

    set_parser = sub.add_parser("set", help="Write a setting")
    set_parser.add_argument("key")
    set_parser.add_argument("value")
    set_parser.add_argument("--project", action="store_true", help="Write to <cwd>/.claude/voiceover.json instead of global settings")
    set_parser.set_defaults(func=cmd_set)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
