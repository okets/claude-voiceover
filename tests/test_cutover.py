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

# --- Ordering is a real invariant: clear must precede stop ---------------
# Drive main() in-process so the two calls can be observed in order.
import importlib.util
import io

import voiceover.speech
import voiceover.spool

spec = importlib.util.spec_from_file_location(
    "ups_under_test", REPO_ROOT / "hooks" / "user_prompt_submit.py")
ups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ups)          # __main__ guard keeps main() from running

order = []
real_clear, real_stop = voiceover.spool.clear, voiceover.speech.stop_speech
real_stdin = sys.stdin


def record_clear(session=None):
    order.append("clear")
    return 0


def record_stop():
    order.append("stop")


voiceover.spool.clear = record_clear
voiceover.speech.stop_speech = record_stop
sys.stdin = io.StringIO(json.dumps(
    {"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"}))
try:
    ups.main()
finally:
    voiceover.spool.clear = real_clear
    voiceover.speech.stop_speech = real_stop
    sys.stdin = real_stdin

check("the clear runs before playback is stopped", order == ["clear", "stop"], order)

# --- a queue stranded by a level change is cleared even at silent level ----
spool.clear()
spool.enqueue("stranded", "kokoro", "bf_emma", session="S1")
set_setting("interaction_level", "silent")
proc = run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "x"})
check("silent level clears the queue anyway", spool.pending_count() == 0,
      spool.pending_count())
check("the hook exits 0 when silent", proc.returncode == 0, proc.stderr)
set_setting("interaction_level", "narrator")

# --- without a session_id, nothing is wiped (avoid unscoped clear) ---------
spool.clear()
spool.enqueue("item1", "kokoro", "bf_emma", session="S1")
spool.enqueue("item2", "kokoro", "bf_emma", session="S2")
run_hook({"cwd": str(REPO_ROOT), "prompt": "new question"})
remaining = [json.loads(p.read_text())["text"]
             for p in sorted(spool.queue_dir().glob("*.json"))]
check("with no session_id, both sessions' items survive", remaining == ["item1", "item2"],
      remaining)

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
