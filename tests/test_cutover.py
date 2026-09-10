"""A new user message clears that session's queue and stops playback."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import spool
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")


def run_hook(payload):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / "user_prompt_submit.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=os.environ.copy())


lock = Path(os.environ["VOICEOVER_DATA_DIR"]) / "tts.lock"

# --- the queue is cleared for the session that typed ---------------------
spool.clear()
for word in ("stale one", "stale two", "stale three"):
    spool.enqueue(word, "kokoro", "bf_emma", session="S1")
    time.sleep(0.002)
proc = run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"})
check("the hook exits 0", proc.returncode == 0, proc.stderr)
check("nothing is printed to stdout", proc.stdout == "", repr(proc.stdout))
check("the abandoned turn's queue is gone", spool.pending_count() == 0,
      spool.pending_count())

# --- another session's queue is untouched --------------------------------
spool.clear()
spool.enqueue("mine", "kokoro", "bf_emma", session="S1")
spool.enqueue("theirs", "kokoro", "bf_emma", session="S2")
run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"})
remaining = [json.loads(p.read_text())["text"]
             for p in sorted(spool.queue_dir().glob("*.json"))]
check("only the typing session's items are dropped", remaining == ["theirs"], remaining)

# --- playback is stopped, so the answer is heard immediately ------------
spool.clear()
lock.write_text(json.dumps({"expiry": time.time() + 300, "pid": 999999}))
run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"})
check("the tts lock is cleared so a new drainer can start now",
      not lock.exists(), "lock still present")

# --- clearing happens before stopping, so no item can slip through -----
# (order check: with items queued AND the lock held, both must end empty)
spool.clear()
spool.enqueue("doomed", "kokoro", "bf_emma", session="S1")
lock.write_text(json.dumps({"expiry": time.time() + 300, "pid": 999999}))
run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"})
check("queue empty after cut-over", spool.pending_count() == 0)
check("lock released after cut-over", not lock.exists())

# --- a silent level still exits cleanly ---------------------------------
set_setting("interaction_level", "silent")
proc = run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "x"})
check("silent level exits 0", proc.returncode == 0, proc.stderr)
set_setting("interaction_level", "narrator")

# --- a malformed payload never breaks the session ----------------------
proc = subprocess.run(
    [sys.executable, str(REPO_ROOT / "hooks" / "user_prompt_submit.py")],
    input="not json at all", capture_output=True, text=True, env=os.environ.copy())
check("a malformed payload still exits 0", proc.returncode == 0, proc.stderr)

report()
