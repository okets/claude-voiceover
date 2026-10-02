#!/usr/bin/env python3
"""PreToolUse hook: interrupt narration on new activity, then announce the tool.

Reads the PreToolUse payload from stdin. Announcements are verbose-level
play-by-play; message text comes from voiceover.templates. Never blocks,
never prints to stdout, always exits 0.
"""

import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover import narrator
    from voiceover.prose import commit_offset, peek_new_prose_stamped
    from voiceover.settings import get_interaction_level
    from voiceover.speech import speak, stop_speech
    from voiceover.templates import pre_tool_announcement

    session = payload.get("session_id")
    # The session's turn stays in the project its prompt was sent from.
    cwd = narrator.pinned_cwd(session, payload.get("cwd"))
    if get_interaction_level(cwd) == "narrator":
        # Narrator mode: queue Claude's actual words written since the last
        # narration. Tool play-by-play stays silent, and nothing interrupts.
        if narrator.headless():
            return
        from voiceover import lease
        from voiceover.speech import ensure_drainer, enqueue_speech

        transcript = payload.get("transcript_path", "")
        tool_name = payload.get("tool_name", "")
        text, offset, shown = peek_new_prose_stamped(transcript)
        if tool_name in narrator.DIALOG_TOOLS:
            # A dialog is about to block the session with no further hooks.
            # Wait out the transcript flush race for the lead-in prose.
            if not text:
                for _ in range(3):
                    time.sleep(0.5)
                    text, offset, shown = peek_new_prose_stamped(transcript)
                    if text:
                        break
            if narrator.queue_dialog(cwd, session, tool_name, payload.get("tool_input") or {},
                                     lead_in=text, shown=shown) and text:
                commit_offset(transcript, offset)
            return
        if narrator.narrates(cwd):  # TTS off or no engine: never hold one
            lease.hold(cwd, session, ttl=lease.FALLBACK_LEASE_TTL)
        if text and enqueue_speech(text, cwd=cwd, full=True, session=session,
                                   shown=shown, source="pre_tool_use"):
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
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
