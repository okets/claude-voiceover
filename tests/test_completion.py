"""The end-of-turn completion never parrots the request, and narrator mode
never speaks a template at all."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import spool
from voiceover.prose import commit_offset
from voiceover.settings import set_setting
from voiceover.templates import completion_message
from voiceover.transcript import CycleStats

REQUEST = "please refactor the authentication module into smaller pieces"

# --- the templated summary never echoes the user's words ------------------
# completion_message picks its wording at random: sample it enough times
# that every template branch is exercised.
no_outcomes = [completion_message(CycleStats(user_intent=REQUEST, complexity="moderate"))
               for _ in range(50)]
check("no 'You instructed me to' line, ever",
      not any("instructed" in text for text in no_outcomes), no_outcomes[:3])
check("a turn with no outcomes does not repeat the request",
      not any("authentication" in text for text in no_outcomes), no_outcomes[:3])

with_outcomes = [completion_message(CycleStats(
    user_intent=REQUEST, complexity="moderate",
    files_modified=["a.py", "b.py"], edit_count=3)) for _ in range(50)]
check("a turn with outcomes does not repeat the request",
      not any("authentication" in text for text in with_outcomes), with_outcomes[:3])
check("a turn with outcomes still reports them",
      all("2 files updated" in text for text in with_outcomes), with_outcomes[:3])

# --- the closing message IS the completion, at any length ----------------
# Claude's closing words say what happened; a canned phrase says nothing.
# This one is 306 characters and 57 words: past both the old 300-character
# ceiling and the speech layer's 36-word budget for tool chatter.
LONG_CLOSING = (
    "The parser now reads the manifest before the lockfile, so a stale lock "
    "no longer wins. I moved the version check into the loader and deleted "
    "the duplicate guard in the CLI. Two tests cover the new order, and the "
    "whole suite passes. Nothing needs your input unless you want the old "
    "order back behind a flag."
)

long_simple = completion_message(CycleStats(
    user_intent=REQUEST, complexity="simple", final_response_text=LONG_CLOSING))
check("a long closing message is spoken, not swapped for a canned phrase",
      long_simple.startswith("The parser now reads the manifest"), long_simple)
check("the whole closing message is kept, not cut short",
      long_simple.endswith("behind a flag."), long_simple)

# A turn that touched files is exactly when the closing matters most.
long_complex = completion_message(CycleStats(
    user_intent=REQUEST, complexity="complex", files_modified=["a.py", "b.py", "c.py"],
    edit_count=4, final_response_text=LONG_CLOSING))
check("a turn with edits speaks its closing message, not a stats line",
      long_complex.startswith("The parser now reads the manifest"), long_complex)
check("the stats line does not ride along with the closing message",
      "files updated" not in long_complex, long_complex)

# The closing arrives as raw markdown; concise used to speak it unprocessed.
MARKDOWN_CLOSING = (
    "## Done\n\n"
    "I rewrote the `loader` and dropped the **duplicate** guard:\n\n"
    "```python\nprint(1)\n```\n\n"
    "- two tests added\n"
    "- the suite passes\n"
)
cleaned = completion_message(CycleStats(
    user_intent=REQUEST, complexity="simple", final_response_text=MARKDOWN_CLOSING))
check("header, emphasis and backtick markup never reach the voice",
      not any(mark in cleaned for mark in ("##", "**", "`")), cleaned)
check("a code block becomes the spoken cue, not source read aloud",
      "code snippet" in cleaned and "print(1)" not in cleaned, cleaned)

# No prose to speak: the templated stats summary and the fallback stay.
stats_only = completion_message(CycleStats(
    user_intent=REQUEST, complexity="moderate", files_modified=["a.py"], edit_count=2))
check("a silent turn with outcomes still reports them",
      "1 file updated" in stats_only, stats_only)
check("a silent turn with nothing to report still says something",
      bool(completion_message(CycleStats(user_intent=REQUEST)).strip()))

# --- the stop hook speaks that closing whole --------------------------------
set_setting("interaction_level", "concise")
set_setting("tts_engine", "kokoro")
concise_tx = data_dir / "tx"
concise_tx.mkdir(parents=True, exist_ok=True)
concise_path = concise_tx / "concise.jsonl"
with open(concise_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"type": "user",
                             "message": {"role": "user", "content": REQUEST}}) + "\n")
    handle.write(json.dumps({"type": "assistant", "isSidechain": False,
                             "message": {"role": "assistant", "content": [
                                 {"type": "text", "text": LONG_CLOSING}]}}) + "\n")
concise_proc = subprocess.run(
    [sys.executable, str(REPO_ROOT / "hooks" / "stop.py")],
    input=json.dumps({"cwd": str(REPO_ROOT), "transcript_path": str(concise_path),
                      "session_id": "S2"}),
    capture_output=True, text=True, env=os.environ.copy())
check("stop exits 0 at concise level", concise_proc.returncode == 0, concise_proc.stderr)
check("the closing message reaches the voice whole, not trimmed to a word budget",
      "behind a flag." in concise_proc.stderr, concise_proc.stderr)
check("no 'etcetera' tail stands in for the rest of the sentence",
      "etcetera" not in concise_proc.stderr, concise_proc.stderr)

# --- narrator stop with nothing unread: a chime, never a template --------
set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
transcripts = data_dir / "tx"
transcripts.mkdir(parents=True, exist_ok=True)
path = transcripts / "narrated.jsonl"
with open(path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"type": "user",
                             "message": {"role": "user", "content": REQUEST}}) + "\n")
    handle.write(json.dumps({"type": "assistant", "isSidechain": False,
                             "message": {"role": "assistant", "content": [
                                 {"type": "text", "text": "Running the tests now."}]}}) + "\n")
# Everything in the transcript was already narrated mid-turn.
commit_offset(str(path), path.stat().st_size)
spool.clear()
proc = subprocess.run(
    [sys.executable, str(REPO_ROOT / "hooks" / "stop.py")],
    input=json.dumps({"cwd": str(REPO_ROOT), "transcript_path": str(path),
                      "session_id": "S1"}),
    capture_output=True, text=True, env=os.environ.copy())
queued = [json.loads(item.read_text())["text"]
          for item in sorted(spool.queue_dir().glob("*.json"))]
check("stop exits 0", proc.returncode == 0, proc.stderr)
check("nothing is queued: no template, no re-spoken line", queued == [], queued)
check("the turn still audibly ends with the chime",
      "[voiceover] sound: decide" in proc.stderr, proc.stderr)

report()
