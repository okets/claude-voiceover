#!/usr/bin/env python3
"""PostToolUse hook: announce a finished tool call at verbose level.

Reads the PostToolUse payload from stdin. Message text (including the
"<description> - done" variants and deduplication) comes from
voiceover.templates. Never blocks, never prints to stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover.speech import speak
    from voiceover.templates import post_tool_announcement

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = None  # some tools return lists; templates expect dict|None

    text = post_tool_announcement(tool_name, tool_input, tool_response)
    if text:
        speak(text, min_level="verbose", cwd=payload.get("cwd"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
