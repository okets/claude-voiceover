#!/usr/bin/env python3
"""The mod's bridge into the narrator: `run.sh mod_bridge`, a JSON batch on stdin.

The mod (hooks/narrator.ts) only captures text; every narration policy lives
here and in voiceover/narrator.py, shared with the classic hooks. Input:

    {"session": str, "cwd": str, "ops": [op, ...]}      cwd = the session's root

    Every op acts in the session's pinned project, wherever the root has moved.

    {"op": "start"} / {"op": "status"}   pin a session that has no pin
    {"op": "end-session"}                release the lease, drop the mark
    {"op": "prompt", "cutover": bool}    narrator level only: re-pin to the root; the
                                         user's own prompt stops the old turn (in the old
                                         project too, after a move) and cuts over, whatever
                                         the gate says. The classic
                                         prompt hook keeps the other levels.
    {"op": "hold"}                       a turn or step began: hold, warm up
    {"op": "say", "utterance": str, "text": str, "shown": float}
    {"op": "dialog", "tool": str, "input": dict, "utterance": str | null}
    {"op": "turn-end", "spoken": bool}

Every batch marks the session first: /clear and resume change the id with
no session.start, and the classic hooks must stand down for the new one from
its very first op. Headless runs (CLAUDE_CODE_ENTRYPOINT=sdk-*) never narrate:
narrator.narrates() says so. A turn end or a dialog releases the lease even
when narration is off, so turning it off mid-turn never leaves an engine
held. Prints exactly one line, {"narrate": bool} - the narrator gate as of
this call (null after an internal error). Always exits 0.
"""

import json
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

_STRAY_MARKERS = re.compile(r"[*`]+")


def speakable(text):
    """A streamed segment as speech: markdown cleaned, and any * or backtick
    a mid-markup split left unpaired removed."""
    from voiceover.prose import clean_for_speech

    return _STRAY_MARKERS.sub("", clean_for_speech(text or "")).strip()


def run(payload):
    """Execute one batch of ops in order; return the narrator gate."""
    from voiceover import lease, narrator
    from voiceover.debuglog import log as _log
    from voiceover.speech import enqueue_speech, ensure_drainer, prewarm

    session = payload.get("session")
    root = payload.get("cwd")  # the session's root now: /cd or a worktree move can change it mid-turn
    ops = [op for op in (payload.get("ops") or []) if isinstance(op, dict)]
    narrator.mark_mod_session(session)
    # A turn stays in the project its prompt pinned, wherever the root moves.
    cwd = narrator.pinned_cwd(session, root)
    narrate = narrator.narrates(cwd)
    _log("bridge", "session=%s ops=%s narrate=%s" % (
        session, ",".join(str(op.get("op")) for op in ops), narrate), cwd)
    queued = False
    for op in ops:
        kind = op.get("op")
        if kind == "end-session":
            lease.release(cwd, session)
            narrator.unmark_mod_session(session)
        elif kind in ("start", "status"):
            if narrator.pinned_cwd(session, None) is None:
                narrator.pin_cwd(session, root)  # a reload or a gate retry keeps the turn's pin
        elif kind == "prompt":
            owns = narrator.mod_owns_prompt(session, root)
            cwd, narrate = root, narrator.narrates(root)  # whichever hook owns it re-pins to the root
            if not owns:
                continue  # below the narrator level the classic prompt hook re-pins and clears
            previous = narrator.repin(session, root)
            if op.get("cutover") and not narrator.headless():
                # The user's own prompt stops the old turn, whatever narrates() says now.
                if previous:
                    narrator.abandon(previous, session)  # the old turn, in its own project
                narrator.cutover(cwd, session, warm=False)  # turn.start's hold warms
        elif kind == "turn-end":
            if narrate:
                narrator.turn_end(cwd, session, bool(op.get("spoken")))
            else:
                lease.release(cwd, session)
        elif kind == "dialog":
            if narrate:
                narrator.queue_dialog(cwd, session, op.get("tool") or "", op.get("input") or {},
                                      utterance=op.get("utterance"))
            else:
                lease.release(cwd, session)
        elif not narrate:
            continue
        elif kind == "hold":
            prewarm(cwd, session)
        elif kind == "say":
            text = speakable(op.get("text"))
            if text and enqueue_speech(text, cwd=cwd, full=True, session=session,
                                       utterance=op.get("utterance"), shown=op.get("shown"),
                                       source="mod"):
                queued = True
    if queued:
        ensure_drainer(cwd)
    return narrate


def main():
    try:
        narrate = run(json.load(sys.stdin))
    except Exception:
        narrate = None
    print(json.dumps({"narrate": narrate}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
