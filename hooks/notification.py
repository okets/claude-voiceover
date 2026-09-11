#!/usr/bin/env python3
"""Notification hook: narrate permission requests; ping in quiet mode.

Reads the Notification payload from stdin. Concise+ levels speak a
"May I run git push?" style message; quiet plays the notification sound;
the generic idle message stays silent. Also clears a stale TTS lock left
by a crashed speech process. Never blocks, no stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

IDLE_MESSAGE = "Claude is waiting for your input"


def clear_stale_lock():
    """Remove a tts.lock whose owning engine has died (a crash leftover).

    Liveness is the pid recorded in the lock, never its mtime: the drain
    loop rewrites the lock once per ITEM, so one long utterance goes stale by
    mtime while it is still playing, and clearing it there would let a second
    drainer start and speak over the first.
    """
    try:
        from voiceover.process_utils import lock_owner_is_running
        from voiceover.speech import lock_path

        lock = lock_path()
        if lock.exists() and not lock_owner_is_running(lock):
            lock.unlink()
    except Exception:
        pass


def main():
    payload = json.load(sys.stdin)
    clear_stale_lock()

    if payload.get("message") == IDLE_MESSAGE:
        return  # generic idle ping: nothing worth narrating

    from voiceover.settings import get_interaction_level
    from voiceover.speech import play_sound, speak
    from voiceover.templates import permission_request_message

    cwd = payload.get("cwd")
    level = get_interaction_level(cwd)
    if level == "silent":
        return
    if level == "quiet":
        play_sound("notification", cwd=cwd)
        return

    from voiceover.settings import data_dir, get_interaction_level
    if get_interaction_level(cwd) == "narrator":
        # If the pre-tool hook announced a blocking dialog moments ago, this
        # notification is its "needs permission" echo - speaking it would cut
        # the real announcement mid-sentence. Marker-based: the message text
        # is not reliably parseable for the tool name.
        try:
            import time as _time
            with open(data_dir() / "dialog_alert.json") as handle:
                if _time.time() - float(json.load(handle).get("ts", 0)) < 30:
                    return
        except Exception:
            pass

    text = permission_request_message(payload)
    if text:
        if level == "narrator":
            from voiceover.speech import ensure_drainer, enqueue_speech
            enqueue_speech(text, min_level="concise", cwd=cwd,
                           session=payload.get("session_id"))
            ensure_drainer(cwd)
        else:
            speak(text, min_level="concise", cwd=cwd, interrupt=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
