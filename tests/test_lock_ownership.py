# tests/test_lock_ownership.py
"""Hooks must never delete a lock they do not own.

The engine side already holds this rule - the drain loop puts an item back
rather than removing a lock another engine claimed. The hook side is the
same problem seen from outside: a hook that deletes a working drainer's lock
lets a second drainer start beside it, and two voices speak at once.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool, speech
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
# "none" stops ensure_drainer BEFORE it spawns anything: what is under test
# here is the lock check it makes first, not the spawn.
set_setting("tts_engine", "none")

lock = speech.lock_path()


def dead_pid():
    """A pid that has certainly exited: a child we started and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_lock(expiry_offset, pid):
    lock.write_text(json.dumps({"expiry": time.time() + expiry_offset, "pid": pid}))


# --- ensure_drainer reads the lock, it does not clear it -------------------
spool.clear()
spool.enqueue("something to say", "kokoro", "af_sarah")

write_lock(-5, os.getpid())          # expired, but held by a live process
speech.ensure_drainer()
check("ensure_drainer does not delete an expired lock it does not own",
      lock.exists(), "the hook unlinked a live engine's lock")

write_lock(300, os.getpid())
check("a live lock still stops a second drainer", speech.ensure_drainer() is False)
check("and that lock survives too", lock.exists())

# --- speak()'s own lock check is unchanged for the non-narrator levels -----
# It clears an expired lock as a side effect; that is pre-existing behaviour
# on a path this queue does not touch.
write_lock(-5, os.getpid())
check("_is_locked still reports an expired lock as free",
      speech._is_locked() is False)
check("_is_locked still clears it, as it always has", not lock.exists())
write_lock(300, os.getpid())
check("_is_locked still reports a live lock as held", speech._is_locked() is True)

# --- clear_stale_lock: liveness is the pid, never the mtime ----------------
spec = importlib.util.spec_from_file_location(
    "notification_under_test", REPO_ROOT / "hooks" / "notification.py")
notification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notification)

# The drain loop rewrites the lock once per ITEM, so one long utterance goes
# stale by mtime while it is still playing. Backdate the mtime well past the
# old 30-second cutoff while the owning process is very much alive.
write_lock(300, os.getpid())
old = time.time() - 600
os.utime(lock, (old, old))
notification.clear_stale_lock()
check("a lock whose owner is still running is kept, however old its mtime",
      lock.exists(), "the hook unlinked a live engine's lock on mtime alone")

write_lock(300, dead_pid())
notification.clear_stale_lock()
check("a lock whose owner has died is cleared", not lock.exists())

# A pre-1.1 lock is a bare float with no pid recorded: unattributable, and no
# 1.1+ engine writes one, so it is a crash leftover.
lock.write_text(str(time.time() + 300))
notification.clear_stale_lock()
check("a pre-1.1 lock with no pid recorded is cleared", not lock.exists())

# --- a missing lock is a no-op, not an error ------------------------------
notification.clear_stale_lock()
check("clearing a lock that is not there is harmless", not lock.exists())

report()
