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
    from voiceover.speech import stop_speech
    from voiceover.spool import clear

    cwd = payload.get("cwd")
    if get_interaction_level(cwd) == "silent":
        return

    # Clear BEFORE stopping: the other order leaves a window in which the
    # drainer claims one more item and speaks it after the cut-over.
    dropped = clear(session=payload.get("session_id"))
    stop_speech()
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
