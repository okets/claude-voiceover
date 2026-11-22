"""A warm drainer hands over to another engine, and tells its speaker when the queue runs dry.

Isolated from the checkout it runs in: the project under test is a temp
directory of its own, passed as cwd to every spool and lease call.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

isolate()
from voiceover import lease, projects, spool

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common

PROJECT = temp_dir("vo-handover-project-")
(PROJECT / ".git").mkdir()
CWD = str(PROJECT)
ROOT = projects.project_dir(cwd=CWD)
SESSION = "S1"


class Collect:
    """A speaker with no idle(): records each sentence, plays nothing."""

    def __init__(self):
        self.spoken = []

    def prefetch(self, sentence, voice):
        pass

    def begin(self, sentence, voice, upcoming=None):
        self.spoken.append(sentence)
        return engine_common.Playback()


class CollectIdle(Collect):
    """A speaker that counts how often it was told the queue ran dry."""

    def __init__(self):
        super().__init__()
        self.idle_calls = 0

    def idle(self):
        self.idle_calls += 1


def start_kokoro_drain(speaker):
    thread = threading.Thread(
        target=engine_common.drain,
        args=(ROOT, lambda: speaker, {"kokoro"}),
        kwargs={"poll_seconds": 0.001},
        daemon=True)
    thread.start()
    time.sleep(0.1)  # the drainer has claimed its lock and is waiting warm
    return thread


def finish(thread):
    """Release the lease and give the drainer time to exit."""
    lease.release(CWD, SESSION)
    thread.join(2)


# --- 1 and 2: a warm engine finishes its own items, then steps aside ---------------
spool.clear(cwd=CWD)
lease.hold(CWD, SESSION)
speaker = Collect()
thread = start_kokoro_drain(speaker)
spool.enqueue("Mine.", "kokoro", "af_sky", session=SESSION, cwd=CWD)
spool.enqueue("Hello.", "phonon", "Bryony", session=SESSION, cwd=CWD)
thread.join(2)
check("a warm engine steps aside for another engine's speech", not thread.is_alive())
check("it released its lock", engine_common.lock_is_live(ROOT) is False)
check("it left the other engine's item in place", spool.pending_count(cwd=CWD) == 1,
      spool.pending_count(cwd=CWD))
check("it finishes its own items first", speaker.spoken == ["Mine."], speaker.spoken)
check("a speaker without idle() still drains", speaker.spoken != [])
finish(thread)

# --- 3: a foreign item queued BEFORE the drainer started does not evict it ----------
# (Otherwise a leftover item for an engine nobody starts would make every new
# drainer exit at once, and the hooks would respawn it in a loop.)
spool.clear(cwd=CWD)
spool.enqueue("Left over.", "phonon", "Bryony", session=SESSION, cwd=CWD)
time.sleep(0.01)
lease.hold(CWD, SESSION)
thread = start_kokoro_drain(Collect())
time.sleep(0.2)
check("an older foreign item does not evict the engine", thread.is_alive())
finish(thread)
check("released, it exits", not thread.is_alive())

# --- 4 and 5: a lease alone keeps the engine warm; idle() is called -----------------
spool.clear(cwd=CWD)
lease.hold(CWD, SESSION)
speaker = CollectIdle()
thread = start_kokoro_drain(speaker)
time.sleep(0.2)
check("a lease with an empty queue keeps the engine", thread.is_alive())
check("the speaker is told the queue ran dry", speaker.idle_calls >= 1, speaker.idle_calls)
finish(thread)
check("released, the idle engine exits", not thread.is_alive())

# --- foreign_pending, directly --------------------------------------------------------
spool.clear(cwd=CWD)
before = time.time()
time.sleep(0.01)
spool.enqueue("Theirs.", "phonon", "Bryony", cwd=CWD)
check("a newer foreign item counts", engine_common.foreign_pending(ROOT, {"kokoro"}, before) is True)
check("an older foreign item does not", engine_common.foreign_pending(ROOT, {"kokoro"}, time.time()) is False)
check("an own item is not foreign", engine_common.foreign_pending(ROOT, {"phonon"}, before) is False)
spool.clear(cwd=CWD)

report()
