#!/usr/bin/env python3
"""UserPromptSubmit hook: cut narration over to the new message.

When the user types, the previous turn is over from their point of view.
Anything still queued from it is dropped and playback is stopped, so the
next thing heard is the answer to the question just asked rather than the
tail of the last one. This is the ONLY place narrator mode interrupts, and
it is the user who triggers it - Claude's own narration never cuts itself
off. Never blocks, never prints to stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover.debuglog import log as _log
    from voiceover.settings import get_interaction_level
    from voiceover.speech import ensure_drainer, stop_speech
    from voiceover.spool import clear

    cwd = payload.get("cwd")
    session = payload.get("session_id")

    # Clear BEFORE stopping: the other order leaves a window in which the
    # drainer claims one more item and speaks it after the cut-over. This
    # runs at EVERY level, including silent - a queue stranded by a level
    # change would otherwise be drained later and speak an abandoned turn.
    dropped = 0
    if session:
        dropped = clear(session=session)
    # With no session id we cannot tell which items are ours, and an
    # unscoped clear would wipe every other session's pending narration.
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)

    if get_interaction_level(cwd) == "silent":
        # stop_speech() is global, not per-session: a silenced session must
        # never be able to kill another session's live playback.
        return
    stop_speech()
    # ...and because it is global, it may have killed a drainer part-way
    # through ANOTHER session's items. Nothing else would restart it: their
    # narration would sit until one of this session's later hooks happened to
    # call ensure_drainer, or be dropped at the age cap - and whenever it did
    # restart, their stale backlog would be spoken ahead of the answer to the
    # message just sent. This turns an unbounded stall into a sub-second gap.
    ensure_drainer(cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
