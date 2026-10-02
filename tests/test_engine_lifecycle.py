"""The turn-scoped engine: warm while a session holds it, gone once released."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import lease, spool, speech
from voiceover.settings import set_setting

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common

ROOT = spool.queue_dir().parent


def start_drainer():
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tts" / "macos_say.py"), "--drain", str(ROOT)],
        stderr=subprocess.PIPE, text=True, env=os.environ.copy(), cwd=str(REPO_ROOT / "tts"))


def exits_within(proc, seconds):
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False


# --- leases ---------------------------------------------------------------------
check("hold writes a live lease", lease.hold(None, "S1") is True and lease.any_live() is True)
check("the engine side sees it", engine_common.lease_is_live(ROOT) is True)
lease.release(None, "S1")
check("release ends it", lease.any_live() is False and engine_common.lease_is_live(ROOT) is False)
check("a lease needs a session", lease.hold(None, None) is False)
lease.hold(None, "S1", ttl=-1)
check("an expired lease is not live", engine_common.lease_is_live(ROOT) is False)
check("the engine never deletes a lease file", (ROOT / "leases" / "S1.json").exists())
lease.hold(None, "OLD", ttl=-7200)
lease.hold(None, "S2")
check("hold prunes another session's lease expired over an hour ago",
      not (ROOT / "leases" / "OLD.json").exists())
check("but keeps a recently expired one", (ROOT / "leases" / "S1.json").exists())
lease.release(None, "S1")
lease.hold(None, "S1")
lease.release(None, "S1")
check("one session's release leaves another's lease live", lease.any_live() is True)
lease.release(None, "S2")

# --- a held engine waits, warm, with an empty queue -----------------------------
spool.clear()
lease.hold(None, "S1")
proc = start_drainer()
time.sleep(0.4)
check("the drainer stays up while a lease is live", proc.poll() is None)
spool.enqueue("Spoken while warm.", "macos-female", "Samantha", session="S1")
time.sleep(0.3)
lease.release(None, "S1")
check("it exits soon after the lease is released", exits_within(proc, 2.0))
stderr = proc.stderr.read()
check("what arrived while it was warm was spoken",
      "[voiceover] Spoken while warm." in stderr, stderr)
check("it released its lock", engine_common.lock_is_live(ROOT) is False)

# --- without a lease it exits as soon as the queue is empty ---------------------
proc = start_drainer()
check("no lease, empty queue: the drainer exits at once", exits_within(proc, 2.0))

# --- a lease taken in the exit gap keeps the engine (the pre-warm race) ---------
answers = iter([False, True, False, False])
real_live, real_claim = engine_common.lease_is_live, engine_common.try_claim_lock
claims = []
engine_common.lease_is_live = lambda root: next(answers, False)
engine_common.try_claim_lock = lambda root, seconds: claims.append(1) or real_claim(root, seconds)
try:
    code = engine_common.drain(ROOT, engine_common.DryRunSpeaker, {"kokoro"}, poll_seconds=0.001)
finally:
    engine_common.lease_is_live, engine_common.try_claim_lock = real_live, real_claim
check("a lease that appears after the lock is released re-claims it", len(claims) == 2, claims)
check("and the drain still ends cleanly",
      code == 0 and engine_common.lock_is_live(ROOT) is False, code)

# --- at most MAX_ENGINES engines machine-wide ---------------------------------------
others = []
for key in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
    other = data_dir / "projects" / key
    other.mkdir(parents=True, exist_ok=True)
    (other / "tts.lock").write_text(json.dumps({"expiry": time.time() + 60, "pid": os.getpid()}))
    others.append(other)
spool.clear()
spool.enqueue("Wait your turn.", "kokoro", "bf_emma")
spoken = []


class Collect:
    def prefetch(self, sentence, voice):
        pass

    def begin(self, sentence, voice, upcoming=None):
        spoken.append(sentence)
        return engine_common.Playback()


engine_common.drain(ROOT, Collect, {"kokoro"})
check("a third engine steps aside while two others run", spoken == [], spoken)
check("its item waits", spool.pending_count() == 1)
check("it leaves a spawn marker so hooks back off", (ROOT / "spawned.json").exists())
check("it releases its lock", engine_common.lock_is_live(ROOT) is False)
for other in others:
    (other / "tts.lock").unlink()
engine_common.drain(ROOT, Collect, {"kokoro"})
check("once they are gone it speaks", spoken == ["Wait your turn."], spoken)
check("and clears the spawn marker once its speaker is open", not (ROOT / "spawned.json").exists())
spool.enqueue("A broken model.", "kokoro", "bf_emma")
engine_common.drain(ROOT, lambda: None, {"kokoro"})
backoff = json.loads((ROOT / "spawned.json").read_text())["ts"] - time.time()
check("a failed model load backs the hooks off for about 30s", 20 < backoff < 28, backoff)
(ROOT / "spawned.json").unlink()
spool.clear()

# --- ensure_drainer: pre-warm, dead owners, backoff -----------------------------------
set_setting("interaction_level", "narrator")
set_setting("tts_engine", "macos-female")
spool.clear()
check("no queue, no lease: nothing to start", speech.ensure_drainer() is False)
check("prewarm holds a lease and starts the engine", speech.prewarm(None, "S1") is True)
check("the lease is there", lease.any_live() is True)
check("a second start right after backs off", speech.ensure_drainer() is False)
(ROOT / "spawned.json").unlink()   # what the engine does when it claims
gone = subprocess.Popen([sys.executable, "-c", "pass"])
gone.wait()
speech.project_lock_path().write_text(json.dumps({"expiry": time.time() + 60, "pid": gone.pid}))
check("a lock whose engine has died does not stop a new one", speech.ensure_drainer() is True)
speech.project_lock_path().unlink()
lease.release(None, "S1")

report()
