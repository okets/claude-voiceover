#!/usr/bin/env python3
"""
macOS `say` engine for the claude-voiceover plugin.

Spawned detached by voiceover/speech.py as:
    python3 <plugin>/tts/macos_say.py --voice <Name> <text>

Collapses the three legacy macos_*_tts.py scripts into one stdlib-only script
(no uv header, no python-dotenv). Text is fed to `say` via stdin, so narration
text never appears in the process command line and cannot be mistaken for a
`say` flag.

Honors VOICEOVER_DRY_RUN: prints '[voiceover] <text>' to stderr, no audio.
"""

import os
import subprocess
import sys

from engine_common import remove_tts_lock, try_claim_lock, update_lock_expiry

DRY_RUN = bool(os.environ.get("VOICEOVER_DRY_RUN"))
DEFAULT_VOICE = "Samantha"
WORDS_PER_SECOND = 3.0  # macOS `say` default rate is ~180 wpm


# --- speech ------------------------------------------------------------------

def estimate_duration(text):
    """Rough playback duration for the lock expiry, in seconds."""
    words = len(text.split())
    return max(1.5, words / WORDS_PER_SECOND + 0.8)


def say_once(voice, text) -> bool:
    """Speak one utterance. Does NOT touch the tts lock - the caller owns it."""
    try:
        subprocess.run(["say", "-v", voice], input=text, text=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as error:
        print("[ERROR] say failed: " + str(error), file=sys.stderr)
        return False


def speak(voice, text):
    """Single-utterance path: claim the lock, speak, release it."""
    if not try_claim_lock(estimate_duration(text)):
        return True  # another narration is playing - skip quietly
    try:
        return say_once(voice, text)
    finally:
        remove_tts_lock()


# --- CLI ---------------------------------------------------------------------

def parse_args(argv):
    """Manual parse so narration text may safely start with a dash."""
    voice = DEFAULT_VOICE
    text_parts = []
    i = 0
    while i < len(argv):
        if argv[i] == "--voice" and i + 1 < len(argv):
            voice = argv[i + 1]
            i += 2
        else:
            text_parts.append(argv[i])
            i += 1
    return voice, " ".join(text_parts).strip()


def main():
    if "--drain" in sys.argv[1:]:
        from engine_common import drain

        def speak_one(item):
            text = item["text"]
            voice = item.get("voice") or DEFAULT_VOICE
            if DRY_RUN:
                print("[voiceover] " + text, file=sys.stderr)
                return True
            if sys.platform != "darwin":
                return False
            # Extend the lock to this item's real length before speaking, so a
            # long utterance cannot let the lock expire under a sibling engine.
            update_lock_expiry(estimate_duration(text) + 5.0)
            return say_once(voice, text)

        return drain(speak_one, {"macos-female", "macos-male"})

    voice, text = parse_args(sys.argv[1:])
    if not text:
        print("Usage: macos_say.py --voice <Name> <text>", file=sys.stderr)
        return 1
    if DRY_RUN:
        print("[voiceover] " + text, file=sys.stderr)
        return 0
    if sys.platform != "darwin":
        print("[ERROR] macos_say.py only works on macOS", file=sys.stderr)
        return 1
    return 0 if speak(voice, text) else 1


if __name__ == "__main__":
    sys.exit(main())
