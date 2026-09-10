"""Claude Code's own status notices must never be narrated.

A usage-limit pause is recorded in the transcript as an assistant message
whose model is "<synthetic>". It therefore looked exactly like Claude's own
prose to the tailer: it sat unread across the pause and was then spoken at
the FRONT of the next turn's narration.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir

isolate()
from voiceover.prose import commit_offset, peek_new_prose
from voiceover.transcript import cycle_stats, is_status_notice


def assistant(text, model="claude-fable-5-1", **extra):
    entry = {"type": "assistant", "isSidechain": False,
             "message": {"role": "assistant", "type": "message", "model": model,
                         "content": [{"type": "text", "text": text}]}}
    entry.update(extra)
    return entry


USER = {"type": "user", "message": {"role": "user", "content": "go on"}}
LIMIT = assistant("You've hit your session limit · resets 11:20pm (Asia/Bangkok)",
                  model="<synthetic>", isApiErrorMessage=True, apiErrorStatus=429)
NO_RESP = assistant("No response requested.", model="<synthetic>",
                    isApiErrorMessage=False)
LOGIN = assistant("Not logged in · Please run /login", model="<synthetic>")
REAL = assistant("Right, picking the thread back up where we left it.")

tx_dir = temp_dir("vo-test-tx-")


def transcript(name, before, after):
    """A transcript with `before` already narrated and `after` unread."""
    path = tx_dir / name
    with open(path, "w", encoding="utf-8") as handle:
        for entry in before:
            handle.write(json.dumps(entry) + "\n")
    commit_offset(str(path), path.stat().st_size)
    with open(path, "a", encoding="utf-8") as handle:
        for entry in after:
            handle.write(json.dumps(entry) + "\n")
    return path


# --- the predicate itself ---------------------------------------------------
check("a 429 limit notice is a status notice", is_status_notice(LIMIT) is True)
check("'No response requested.' is a status notice", is_status_notice(NO_RESP) is True)
check("'Not logged in' is a status notice", is_status_notice(LOGIN) is True)
check("real assistant prose is NOT a status notice", is_status_notice(REAL) is False)
check("a non-dict is handled", is_status_notice(None) is False)

# --- the reported bug: limit notice glued to the resumed prose -------------
prose, _ = peek_new_prose(str(transcript("pause.jsonl", [USER], [LIMIT, REAL])))
check("the limit notice is not narrated after a pause",
      prose is not None and "session limit" not in prose, "spoke: %r" % prose)
check("the real resumed prose IS still narrated",
      prose is not None and "picking the thread back up" in prose, "spoke: %r" % prose)

# --- a notice alone leaves nothing to say ---------------------------------
prose, _ = peek_new_prose(str(transcript("noresp.jsonl", [USER], [NO_RESP])))
check("a lone status notice yields no narration", prose is None, "spoke: %r" % prose)

# --- regression guard: ordinary prose still narrated ----------------------
prose, _ = peek_new_prose(str(transcript("normal.jsonl", [USER], [REAL])))
check("ordinary assistant prose is narrated",
      prose is not None and "picking the thread" in prose, "spoke: %r" % prose)

# --- the other reader, used at concise/verbose levels --------------------
path = tx_dir / "cycle.jsonl"
with open(path, "w", encoding="utf-8") as handle:
    for entry in (USER, REAL, LIMIT):
        handle.write(json.dumps(entry) + "\n")
stats = cycle_stats(str(path))
check("cycle_stats does not report the notice as the final response",
      stats.final_response_text is not None
      and "session limit" not in stats.final_response_text,
      "final_response_text: %r" % stats.final_response_text)

report()
