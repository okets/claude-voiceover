"""Prose offsets: the tracked-transcript cap evicts the least recently
narrated transcript, never the one just committed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir

isolate()
from voiceover import prose


def make_transcript(directory, name, lines):
    path = directory / name
    path.write_text("".join(lines), encoding="utf-8")
    return path


def assistant_line(text):
    return ('{"type": "assistant", "message": {"role": "assistant", '
            '"content": [{"type": "text", "text": "%s"}]}}\n' % text)


# --- a transcript whose path sorts FIRST survives the cap --------------------
# Regression: the cap used to evict by sorted path, so a project under
# ~/Documents (sorting before ~/Projects) evicted its own offset on every
# commit and narrator mode never read a word of it.
for i in range(prose._MAX_TRACKED_TRANSCRIPTS):
    prose.commit_offset("/zzz-late-sorting/transcript-%02d.jsonl" % i, 100 + i)

early_key = "/aaa-early-sorting/session.jsonl"
prose.commit_offset(early_key, 4242)
state = prose._load_state()
check("cap keeps the just-committed early-sorting transcript",
      state.get(early_key) == 4242, sorted(state)[:3])
check("cap evicts the least recently committed transcript instead",
      "/zzz-late-sorting/transcript-00.jsonl" not in state
      and "/zzz-late-sorting/transcript-01.jsonl" in state,
      sorted(state))
check("state stays at the cap", len(state) == prose._MAX_TRACKED_TRANSCRIPTS,
      len(state))

# --- re-committing an old transcript makes it recent again -------------------
prose.commit_offset("/zzz-late-sorting/transcript-01.jsonl", 999)
prose.commit_offset("/another-new/one.jsonl", 1)
state = prose._load_state()
check("re-committed transcript is not the next eviction victim",
      "/zzz-late-sorting/transcript-01.jsonl" in state
      and "/zzz-late-sorting/transcript-02.jsonl" not in state,
      sorted(state))

# --- end to end: narrator reads a first-sorting transcript on the 2nd peek ---
work = temp_dir()
tp = make_transcript(work, "0000-first.jsonl", [assistant_line("hello")])
speech, offset = prose.peek_new_prose(tp)  # first sight: start from now
check("first peek says nothing", speech is None)
with open(tp, "a", encoding="utf-8") as handle:
    handle.write(assistant_line("Browser closed. Here is the recap."))
speech, offset = prose.peek_new_prose(tp)
check("second peek reads the new prose even with a full state file",
      speech == "Browser closed. Here is the recap.", speech)

report()
