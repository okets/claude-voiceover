#!/usr/bin/env python3
"""Stop hook: speak a completion summary for the finished request cycle.

Reads the Stop payload from stdin. Concise and verbose speak
completion_message(cycle_stats(transcript_path)); narrator queues Claude's
unread closing prose, or plays the decide sound when there is none; quiet
plays the decide sound; stop_hook_active is skipped to avoid announcement
loops. Never blocks, never prints to stdout, always exits 0.
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

    from voiceover import lease, narrator

    session = payload.get("session_id")
    cwd = narrator.pinned_cwd(session, payload.get("cwd"))
    level = get_interaction_level(cwd)
    if level != "narrator":
        # The turn is over and nothing narrates it now (the level may have
        # changed mid-turn): never leave an engine held for it.
        lease.release(cwd, session)
    if level == "silent":
        return
    if level == "quiet":
        play_sound("decide", cwd=cwd)
        return

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        if level == "narrator":
            lease.release(cwd, session)  # the turn is over, even with nothing to read
        return

    if level == "narrator":
        if narrator.headless():
            return
        from voiceover.debuglog import log as _log
        from voiceover.prose import commit_offset, peek_new_prose_stamped
        from voiceover.speech import ensure_drainer, enqueue_speech
        import os as _os
        spoken = False
        try:
            _log("stop", "enter size=%s" % _os.path.getsize(transcript_path), cwd)
            # The final assistant message is flushed to the transcript shortly
            # AFTER Stop fires (measured ~0.5s); poll briefly so the finale is
            # actually readable instead of always arriving one turn late.
            prose, offset, shown = peek_new_prose_stamped(transcript_path)
            retries = 0
            for _ in range(10):
                if prose:
                    break
                time.sleep(0.5)
                prose, offset, shown = peek_new_prose_stamped(transcript_path)
                retries += 1
            _log("stop", "peek after %d retries: %d chars, size=%s" % (
                retries, len(prose or ""), _os.path.getsize(transcript_path)), cwd)
            spoken = bool(prose)
            if prose and enqueue_speech(prose, cwd=cwd, full=True, session=session,
                                        shown=shown, source="stop"):
                # The finale is Claude's own closing words. It goes to the BACK
                # of the queue: order is what makes narration followable.
                commit_offset(transcript_path, offset)
            ensure_drainer(cwd)
        finally:
            # The turn is over, whatever went wrong above: the engine goes
            # once the finale has played. A turn with nothing left to say ends
            # on the chime instead - narrator mode only ever speaks Claude's
            # own words.
            narrator.turn_end(cwd, session, spoken=spoken)
        return

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
        speak(text, min_level="concise", cwd=cwd, interrupt=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
