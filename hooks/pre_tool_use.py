#!/usr/bin/env python3
"""PreToolUse hook: interrupt narration on new activity, then announce the tool.

Reads the PreToolUse payload from stdin. Announcements are verbose-level
play-by-play; message text comes from voiceover.templates. Never blocks,
never prints to stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover.prose import commit_offset, peek_new_prose
    from voiceover.settings import get_interaction_level
    from voiceover.speech import speak, stop_speech
    from voiceover.templates import pre_tool_announcement

    cwd = payload.get("cwd")
    if get_interaction_level(cwd) == "narrator":
        # Narrator mode: speak Claude's actual words written since the last
        # narration; tool play-by-play stays silent and playing prose is
        # never cut off by new activity.
        text, offset = peek_new_prose(payload.get("transcript_path", ""))
        if text and speak(text, min_level="concise", cwd=cwd, full=True):
            commit_offset(payload.get("transcript_path", ""), offset)
        return

    # New activity always cuts off any narration still playing.
    stop_speech()

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    text = pre_tool_announcement(tool_name, tool_input, payload.get("transcript_path"))
    if text:
        speak(text, min_level="verbose", cwd=cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
