# tests/test_drain.py
"""Drain mode: play the whole spool in order, back to back, then exit."""

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
sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common


def run_drain(engine="macos"):
    """Run an engine in drain mode under dry-run; return (stderr, returncode)."""
    if engine == "macos":
        command = [sys.executable, str(REPO_ROOT / "tts" / "macos_say.py"), "--drain"]
    else:
        command = ["uv", "run", "--project", str(REPO_ROOT / "tts"),
                   str(REPO_ROOT / "tts" / "kokoro_voice.py"), "--drain"]
    proc = subprocess.run(command, capture_output=True, text=True,
                          env=os.environ.copy(), cwd=str(REPO_ROOT / "tts"))
    return proc.stderr, proc.returncode


# --- drains the whole spool, in order --------------------------------------
spool.clear()
for word in ("first line", "second line", "third line"):
    spool.enqueue(word, "macos-female", "Samantha")
    time.sleep(0.002)
stderr, code = run_drain("macos")
spoken = [line.split("[voiceover] ", 1)[1].strip()
          for line in stderr.splitlines() if "[voiceover] " in line]
check("drain exits 0", code == 0, "code=%d stderr=%s" % (code, stderr))
check("every queued item was spoken, in order",
      spoken == ["first line", "second line", "third line"], spoken)
check("the spool is empty afterwards", spool.pending_count() == 0)
check("the lock is released afterwards", engine_common.lock_is_live() is False)
check("no .taken leftovers", list(spool.queue_dir().glob("*.taken")) == [])

# --- an empty spool is a clean no-op ---------------------------------------
stderr, code = run_drain("macos")
check("draining an empty spool exits 0", code == 0, code)
check("draining an empty spool says nothing", "[voiceover] " not in stderr, stderr)

# --- a held lock makes the drainer step aside ------------------------------
spool.clear()
spool.enqueue("must survive", "macos-female", "Samantha")
engine_common.tts_lock_path().write_text(
    json.dumps({"expiry": time.time() + 60, "pid": os.getpid()}))
stderr, code = run_drain("macos")
check("a second drainer exits 0 when the lock is held", code == 0, code)
check("it speaks nothing", "[voiceover] " not in stderr, stderr)
check("the item is left for the lock holder", spool.pending_count() == 1)
engine_common.remove_tts_lock()

# --- items for another engine are left alone -------------------------------
spool.clear()
spool.enqueue("kokoro only", "kokoro", "bf_emma")
stderr, code = run_drain("macos")
check("the macos drainer exits 0 on a kokoro item", code == 0, code)
check("it does not speak another engine's item", "[voiceover] " not in stderr, stderr)
check("the item is still pending for the right engine", spool.pending_count() == 1)
check("the lock is released so the right engine can claim it",
      engine_common.lock_is_live() is False)

# --- items past the age cap are dropped, not spoken ------------------------
spool.clear()
spool.enqueue("ancient", "macos-female", "Samantha")
path = sorted(spool.queue_dir().glob("*.json"))[0]
item = json.loads(path.read_text())
item["created"] = time.time() - (spool.QUEUE_MAX_AGE_SECONDS + 60)
path.write_text(json.dumps(item))
time.sleep(0.002)
spool.enqueue("current", "macos-female", "Samantha")
stderr, code = run_drain("macos")
spoken = [line.split("[voiceover] ", 1)[1].strip()
          for line in stderr.splitlines() if "[voiceover] " in line]
check("stale items are dropped, fresh ones spoken", spoken == ["current"], spoken)

# --- an item appended as the drainer finishes is still spoken -------------
# The drainer re-checks the spool after releasing the lock, so this cannot
# be stranded. Simulated by appending while a drain is in flight.
spool.clear()
spool.enqueue("before", "macos-female", "Samantha")
proc = subprocess.Popen(
    [sys.executable, str(REPO_ROOT / "tts" / "macos_say.py"), "--drain"],
    stderr=subprocess.PIPE, text=True, env=os.environ.copy(),
    cwd=str(REPO_ROOT / "tts"))
time.sleep(0.05)
spool.enqueue("after", "macos-female", "Samantha")
stderr = proc.communicate()[1]
spoken = [line.split("[voiceover] ", 1)[1].strip()
          for line in stderr.splitlines() if "[voiceover] " in line]
# The invariant is that the item is never LOST: it is either spoken by this
# drainer (it won the re-check) or still pending for the next one. A failure
# here means it was claimed and silently dropped.
check("an item appended mid-drain is never lost",
      "after" in spoken or spool.pending_count() == 1,
      "spoken=%s pending=%d" % (spoken, spool.pending_count()))

report()
