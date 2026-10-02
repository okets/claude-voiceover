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
