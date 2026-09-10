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

    from voiceover.settings import get_interaction_level
    from voiceover.speech import speak
    from voiceover.templates import post_tool_announcement

    if get_interaction_level(payload.get("cwd")) == "narrator":
        # No '- done' chatter. Prose written just before this tool call
        # (which pre-tool can miss by a flush race) is queued here instead,
        # at the tool's completion.
        from voiceover.prose import commit_offset, peek_new_prose
        from voiceover.speech import ensure_drainer, enqueue_speech

        transcript = payload.get("transcript_path", "")
        cwd = payload.get("cwd")
        text, offset = peek_new_prose(transcript)
        if text and enqueue_speech(text, cwd=cwd, full=True,
                                   session=payload.get("session_id")):
            # Queued means it WILL be spoken, so the cursor can advance now.
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
        return

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
