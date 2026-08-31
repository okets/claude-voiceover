#!/usr/bin/env python3
"""Notification hook: narrate permission requests; ping in quiet mode.

Reads the Notification payload from stdin. Concise+ levels speak a
"May I run git push?" style message; quiet plays the notification sound;
the generic idle message stays silent. Also clears a stale TTS lock left
by a crashed speech process. Never blocks, no stdout, always exits 0.
"""

import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

STALE_LOCK_SECONDS = 30
IDLE_MESSAGE = "Claude is waiting for your input"


def clear_stale_lock():
    """Remove a tts.lock older than STALE_LOCK_SECONDS (crash leftover)."""
    try:
        from voiceover.speech import lock_path

        lock = lock_path()
        if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
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

    from voiceover.settings import get_interaction_level
    raw_message = payload.get("message", "") or ""
    if (get_interaction_level(cwd) == "narrator"
            and ("AskUserQuestion" in raw_message or "ExitPlanMode" in raw_message)):
        # Narrator's pre-tool hook already announced this dialog with the
        # actual question text; a second "needs permission" alert would only
        # interrupt it mid-sentence. Stay quiet.
        return

    text = permission_request_message(payload)
    if text:
        speak(text, min_level="concise", cwd=cwd, interrupt=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
