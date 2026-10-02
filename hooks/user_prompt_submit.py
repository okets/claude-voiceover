#!/usr/bin/env python3
"""UserPromptSubmit hook: cut narration over to the new message.

When the user types, the previous turn is over from their point of view. At
the narrator level the user's own prompt drops the session's queued text,
stops its sentence and pre-warms the engine for the reply; a background
task's notification only pre-warms, and headless runs are left alone. Other
levels clear the session's queue and stop the one-shot speaker. This is the
ONLY place narrator mode interrupts, and it is the user who triggers it -
Claude's own narration never cuts itself off. Never blocks, never prints to
stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover import lease, narrator
    from voiceover.debuglog import log as _log
    from voiceover.settings import get_interaction_level
    from voiceover.spool import clear

    cwd = payload.get("cwd")
    session = payload.get("session_id")
    level = get_interaction_level(cwd)

    if level == "narrator":
        if narrator.headless():
            return
        narrator.pin_cwd(session, cwd)  # this turn's project, whatever it cds to
        if narrator.is_users_prompt(payload.get("prompt")):
            narrator.cutover(cwd, session, ttl=lease.FALLBACK_LEASE_TTL)
        elif narrator.narrates(cwd):
            # A background task's turn: warm the engine, cut nothing off.
            narrator.prewarm(cwd, session, ttl=lease.FALLBACK_LEASE_TTL)
        return

    # Clear at every other level, silent included: a queue stranded by a level
    # change would otherwise be drained later and speak an abandoned turn.
    # With no session id we cannot tell which items are ours.
    dropped = clear(session=session, cwd=cwd) if session else 0
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)
    if level == "silent":
        return
    from voiceover.speech import stop_speech
    stop_speech()  # the one-shot speaker only: process_utils scopes it


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
