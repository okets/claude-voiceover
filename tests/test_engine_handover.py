"""A warm drainer hands over to another engine, and tells its speaker when the queue runs dry.

Isolated from the checkout it runs in: the project under test is a temp
directory of its own, passed as cwd to every spool and lease call.
"""

import json
import os
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
    """A speaker with no release_output(): records each sentence, plays nothing."""

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
        self.release_calls = 0

    def release_output(self):
        self.release_calls += 1


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
check("a speaker without release_output() still drains", speaker.spoken != [])
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

# --- 4 and 5: a lease alone keeps the engine warm; release_output() is called -----------------
spool.clear(cwd=CWD)
lease.hold(CWD, SESSION)
speaker = CollectIdle()
thread = start_kokoro_drain(speaker)
time.sleep(0.2)
check("a lease with an empty queue keeps the engine", thread.is_alive())
check("the speaker is told the queue ran dry", speaker.release_calls >= 1, speaker.release_calls)
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

# --- a speaker whose release_output() raises does not end the drain ---------------------------


class IdleRaises(Collect):
    def release_output(self):
        raise RuntimeError("device went away")


lease.hold(CWD, SESSION)
speaker = IdleRaises()
thread = start_kokoro_drain(speaker)
spool.enqueue("Still here.", "kokoro", "af_sky", session=SESSION, cwd=CWD)
time.sleep(0.2)
check("a speaker whose release_output() raises still speaks the next item",
      speaker.spoken == ["Still here."], speaker.spoken)
finish(thread)

# === Task 2b: a newcomer engine waits for one that is handing over =================
from voiceover import speech

LOCK = engine_common.tts_lock_path(ROOT)


def read_lock():
    try:
        return json.loads(LOCK.read_text())
    except (OSError, ValueError):
        return {}


def write_lock(**extra):
    LOCK.write_text(json.dumps(dict({"expiry": time.time() + 60, "pid": os.getpid()}, **extra)))


class Slow(Collect):
    """Each sentence 'plays' for `seconds`; records when each began and ended."""

    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds
        self.events = []
        self.playing = threading.Event()

    def begin(self, sentence, voice, upcoming=None):
        self.spoken.append(sentence)
        self.events.append(("begin", sentence, time.time()))
        self.playing.set()
        speaker = self

        class Timed(engine_common.Playback):
            ends = time.time() + speaker.seconds

            def poll(self):
                if time.time() < self.ends:
                    return False
                speaker.events.append(("end", sentence, time.time()))
                return True

        return Timed()


class Opened:
    """open_speaker() for a drain: records what the lock said when it was called."""

    def __init__(self, speaker):
        self.speaker = speaker
        self.lock_at_open = None

    def __call__(self):
        self.lock_at_open = read_lock()
        return self.speaker


def start_drain(opener, engines, lock_seconds=60.0):
    thread = threading.Thread(
        target=engine_common.drain, args=(ROOT, opener, engines),
        kwargs={"poll_seconds": 0.001, "lock_seconds": lock_seconds}, daemon=True)
    thread.start()
    return thread


spawned = []
real_resolve, real_command, real_spawn = speech.resolve_engine, speech._drain_command, speech._spawn_detached
# Phonon dispatch arrives in Task 6: until then the hook side is stubbed to
# resolve "phonon" and to capture the spawn instead of starting a process.
speech.resolve_engine = lambda cwd=None: "phonon"
speech._drain_command = lambda engine, root: ["engine", engine, "--drain", str(root)]
speech._spawn_detached = lambda command: spawned.append(command) or True


def spawned_json_ts():
    try:
        return float(json.loads((ROOT / "spawned.json").read_text())["ts"])
    except (OSError, ValueError, KeyError):
        return None


try:
    # --- the old engine's own backlog alone starts no newcomer --------------------
    spool.clear(cwd=CWD)
    (ROOT / "spawned.json").unlink(missing_ok=True)
    write_lock(engines=["kokoro"])
    spool.enqueue("Backlog one.", "kokoro", "af_sky", cwd=CWD)
    spool.enqueue("Backlog two.", "kokoro", "af_sky", cwd=CWD)
    check("only the old engine's items pending: ensure_drainer spawns nothing",
          speech.ensure_drainer(CWD) is False and spawned == [], spawned)
    spool.enqueue("For the new engine.", "phonon", "Bryony", cwd=CWD)
    check("one item for the resolved engine: it spawns once",
          speech.ensure_drainer(CWD) is True and len(spawned) == 1, spawned)
    check("pending_count counts one engine's items",
          (spool.pending_count(cwd=CWD, engine="kokoro"), spool.pending_count(cwd=CWD, engine="phonon"),
           spool.pending_count(cwd=CWD)) == (2, 1, 3))
    engine_common.remove_tts_lock(ROOT)
    spawned.clear()

    # --- the finale case, end to end ----------------------------------------------
    spool.clear(cwd=CWD)
    (ROOT / "spawned.json").unlink(missing_ok=True)
    lease.hold(CWD, SESSION)
    kokoro = Slow(1.6)
    kokoro_thread = start_drain(lambda: kokoro, {"kokoro"})
    spool.enqueue("Kokoro speaking.", "kokoro", "af_sky", session=SESSION, cwd=CWD)
    kokoro.playing.wait(2)
    check("a drainer records the engines it drains in its lock", read_lock().get("engines") == ["kokoro"],
          read_lock())
    spool.enqueue("Finale.", "phonon", "Bryony", session=SESSION, cwd=CWD)
    check("ensure_drainer starts the newly chosen engine beside a busy one",
          speech.ensure_drainer(CWD) is True and len(spawned) == 1, spawned)
    first_mark = spawned_json_ts()
    phonon = Slow(0.05)
    opener = Opened(phonon)
    phonon_thread = start_drain(opener, {"phonon"})
    time.sleep(1.2)
    check("the newcomer waits while the other engine speaks", phonon_thread.is_alive() and phonon.spoken == [])
    check("a waiting newcomer loads nothing", opener.lock_at_open is None)
    check("the waiter refreshes the spawn marker",
          first_mark is not None and (spawned_json_ts() or 0) > first_mark + 0.5, (first_mark, spawned_json_ts()))
    check("while one newcomer waits, a second ensure_drainer spawns nothing",
          speech.ensure_drainer(CWD) is False and len(spawned) == 1, spawned)
    kokoro_thread.join(3)
    deadline = time.time() + 2
    while phonon.spoken == [] and time.time() < deadline:
        time.sleep(0.01)
    check("the old engine finished its own item and handed over", not kokoro_thread.is_alive())
    check("the newcomer then speaks the finale", phonon.spoken == ["Finale."], phonon.spoken)
    kokoro_end = [t for kind, s, t in kokoro.events if kind == "end"]
    phonon_begin = [t for kind, s, t in phonon.events if kind == "begin"]
    check("in order: the old item completes before the finale begins",
          kokoro_end and phonon_begin and kokoro_end[0] <= phonon_begin[0], (kokoro.events, phonon.events))
    check("the newcomer opened its speaker only once it held the lock",
          (opener.lock_at_open or {}).get("engines") == ["phonon"], opener.lock_at_open)
    lease.release(CWD, SESSION)
    phonon_thread.join(2)
    check("both engines end with the lock released",
          not phonon_thread.is_alive() and engine_common.lock_is_live(ROOT) is False)

    # --- a waiting newcomer whose items disappear exits without loading ---------------
    spool.clear(cwd=CWD)
    write_lock(engines=["kokoro"])
    spool.enqueue("Never mind.", "phonon", "Bryony", cwd=CWD)
    opener = Opened(Collect())
    thread = start_drain(opener, {"phonon"})
    time.sleep(0.15)
    alive_while_waiting = thread.is_alive()
    spool.clear(cwd=CWD)
    thread.join(1)
    check("a newcomer waits on another engine's lock", alive_while_waiting)
    check("it exits once none of its own items remain", not thread.is_alive())
    check("without ever opening a speaker", opener.lock_at_open is None)
    check("and leaves the other engine's lock alone", read_lock().get("engines") == ["kokoro"])

    # --- a second drain for the SAME engine still exits at once ------------------------
    write_lock(engines=["phonon"])
    spool.enqueue("Mine already.", "phonon", "Bryony", cwd=CWD)
    opener = Opened(Collect())
    began = time.time()
    code = engine_common.drain(ROOT, opener, {"phonon"}, poll_seconds=0.001)
    check("a second drain for the same engine exits at once",
          code == 0 and time.time() - began < 0.5 and opener.lock_at_open is None)

    # --- a live lock with no "engines" key behaves as today ----------------------------
    write_lock()
    spawned.clear()
    (ROOT / "spawned.json").unlink(missing_ok=True)
    check("ensure_drainer leaves a lock that records no engines alone",
          speech.ensure_drainer(CWD) is False and spawned == [], spawned)
    began = time.time()
    code = engine_common.drain(ROOT, opener, {"phonon"}, poll_seconds=0.001)
    check("and a drain facing it exits at once", code == 0 and time.time() - began < 0.5)
    engine_common.remove_tts_lock(ROOT)
    spool.clear(cwd=CWD)

    # --- the lock keeps "engines" across an expiry refresh ----------------------------
    lease.hold(CWD, SESSION)
    kokoro = Slow(0.6)
    kokoro_thread = start_drain(lambda: kokoro, {"kokoro"}, lock_seconds=0.2)
    spool.enqueue("A long one.", "kokoro", "af_sky", session=SESSION, cwd=CWD)
    kokoro.playing.wait(2)
    claimed = read_lock()
    time.sleep(0.35)
    refreshed = read_lock()
    check("the lock still carries its engines after an expiry refresh",
          refreshed.get("expiry", 0) > claimed.get("expiry", 0) and refreshed.get("engines") == ["kokoro"],
          (claimed, refreshed))
    lease.release(CWD, SESSION)
    kokoro_thread.join(2)
finally:
    speech.resolve_engine, speech._drain_command, speech._spawn_detached = real_resolve, real_command, real_spawn
    lease.release(CWD, SESSION)

report()
