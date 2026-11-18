#!/usr/bin/env python3
# tts/voiceover_engine_shim.py
"""Stable entry point for the shared Kokoro engine, installed at <state>/bin/voiceover-engine.

Finds the plugin through --plugin-root or the pointer file the engine publishes,
puts its tts/ directory on sys.path and hands over to engine_launcher. Stdlib only,
and it imports nothing from the plugin before the path is set (the state-dir rule
below deliberately repeats engine_common.data_dir).
"""

import json
import os
import sys
from pathlib import Path


def _state_dir() -> Path:
    root = os.environ.get("VOICEOVER_DATA_DIR")
    return Path(root) if root else Path.home() / ".claude-voiceover"


def _plugin_root_from_pointer():
    try:
        with open(_state_dir() / "engine" / "current.json", encoding="utf-8") as handle:
            return json.load(handle).get("plugin_root")
    except (OSError, ValueError, AttributeError):
        return None


def _split_plugin_root(argv):
    """(plugin root or None, argv without --plugin-root)."""
    if "--plugin-root" in argv:
        at = argv.index("--plugin-root")
        if at + 1 < len(argv):
            return argv[at + 1], argv[:at] + argv[at + 2:]
    return None, argv


def main(argv) -> int:
    given, rest = _split_plugin_root(argv)
    root = given or _plugin_root_from_pointer()
    if not root:
        print("voiceover-engine: no engine installed (no current.json)", file=sys.stderr)
        return 1
    if not (Path(root) / "tts" / "engine_launcher.py").is_file():
        print("voiceover-engine: plugin not found at %s" % root, file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(root) / "tts"))
    import engine_launcher
    return engine_launcher.main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
