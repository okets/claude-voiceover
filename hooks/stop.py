#!/usr/bin/env python3
"""Stop hook: speak a completion summary for the finished request cycle.

Reads the Stop payload from stdin. Concise+ levels speak
completion_message(cycle_stats(transcript_path)); quiet plays the decide
sound; stop_hook_active is skipped to avoid announcement loops. Never
blocks, never prints to stdout, always exits 0.
"""

import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    # A Stop hook already continued this turn; narrating again would loop.
    if payload.get("stop_hook_active"):
        return

    from voiceover.settings import get_interaction_level
    from voiceover.speech import play_sound, speak
    from voiceover.templates import completion_message
    from voiceover.transcript import cycle_stats

    cwd = payload.get("cwd")
    level = get_interaction_level(cwd)
    if level == "silent":
        return
    if level == "quiet":
        play_sound("decide", cwd=cwd)
        return

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        return

    if level == "narrator":
        from voiceover.prose import commit_offset, peek_new_prose
        # The final assistant message is flushed to the transcript shortly
        # AFTER Stop fires (measured ~0.5s); poll briefly so the finale is
        # actually readable instead of always arriving one turn late.
        prose, offset = peek_new_prose(transcript_path)
        for _ in range(10):
            if prose:
                break
            time.sleep(0.5)
            prose, offset = peek_new_prose(transcript_path)
        if prose:
            # The finale is Claude's own closing words; it outranks whatever
            # play-by-play is still in the air.
            if speak(prose, min_level="concise", cwd=cwd, interrupt=True, full=True):
                commit_offset(transcript_path, offset)
            return
        # Nothing unread (all prose narrated mid-turn): fall through to the
        # templated completion so the turn still audibly ends.

    stats = cycle_stats(transcript_path)
    # Same flush race: wait briefly for the final response text so simple
    # turns can speak Claude's actual answer rather than a generic template.
    for _ in range(6):
        if stats.final_response_text:
            break
        time.sleep(0.5)
        stats = cycle_stats(transcript_path)
    text = completion_message(stats)
    if text:
        speak(text, min_level="concise", cwd=cwd, interrupt=True)  # the completion always outranks leftover play-by-play


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
