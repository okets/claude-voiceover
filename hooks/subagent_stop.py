#!/usr/bin/env python3
"""SubagentStop hook: manager-style announcement when a subagent finishes.

Reads the SubagentStop payload from stdin. Speaks only at verbose level
and only when the speak_subagent_completions setting is on; message text
comes from voiceover.templates. Never blocks, no stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    # A Stop hook already continued this turn; skip to avoid loops.
    if payload.get("stop_hook_active"):
        return

    from voiceover.settings import get_setting, level_at_least
    from voiceover.speech import speak
    from voiceover.templates import subagent_completion_message

    cwd = payload.get("cwd")
    if not level_at_least("verbose", cwd):
        return
    if not get_setting("speak_subagent_completions", cwd):
        return

    text = subagent_completion_message(payload)
    if text:
        speak(text, min_level="verbose", cwd=cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
