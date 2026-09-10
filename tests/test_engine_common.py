# tests/test_engine_common.py
"""The engine-side half: lock helpers and single-claim spool reading."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from voiceover import spool
import engine_common


# --- the two halves agree on where the queue lives -------------------------
check("engine and hook layer resolve the same queue dir",
      engine_common.queue_dir().resolve() == spool.queue_dir().resolve(),
      "%s vs %s" % (engine_common.queue_dir(), spool.queue_dir()))

# --- lock lifecycle ---------------------------------------------------------
check("lock is not live before any claim", engine_common.lock_is_live() is False)
check("first claim wins", engine_common.try_claim_lock(30.0) is True)
check("lock reads as live once claimed", engine_common.lock_is_live() is True)
engine_common.remove_tts_lock()
check("lock is gone after removal", engine_common.lock_is_live() is False)

# --- an expired lock does not block a claim --------------------------------
engine_common.tts_lock_path().write_text(
    json.dumps({"expiry": time.time() - 5, "pid": os.getpid()}))
check("an expired lock is treated as stale", engine_common.lock_is_live() is False)
check("claim succeeds over an expired lock", engine_common.try_claim_lock(30.0) is True)
engine_common.remove_tts_lock()

# --- reading in order -------------------------------------------------------
spool.clear()
for word in ("alpha", "beta", "gamma"):
    spool.enqueue(word, "kokoro", "bf_emma")
    time.sleep(0.002)
spoken = []
while True:
    item, taken = engine_common.take_oldest()
    if item is None:
        break
    spoken.append(item["text"])
    engine_common.finish(taken)
check("take_oldest drains in creation order",
      spoken == ["alpha", "beta", "gamma"], spoken)
check("the spool is empty after draining", spool.pending_count() == 0)

# --- exactly one reader wins an item ---------------------------------------
spool.clear()
spool.enqueue("only", "kokoro", "bf_emma")
first_item, first_path = engine_common.take_oldest()
second_item, second_path = engine_common.take_oldest()
check("the first reader gets the item", first_item is not None and first_item["text"] == "only")
check("a second reader gets nothing", second_item is None, second_item)
check("a claimed item is not listed as pending", spool.pending_count() == 0)

# --- put_back restores order ------------------------------------------------
engine_common.put_back(first_path)
check("put_back makes the item pending again", spool.pending_count() == 1)
again_item, again_path = engine_common.take_oldest()
check("the restored item is the same one",
      again_item is not None and again_item["text"] == "only", again_item)
engine_common.finish(again_path)

# --- put_back preserves position in the queue ------------------------------
spool.clear()
for word in ("one", "two"):
    spool.enqueue(word, "kokoro", "bf_emma")
    time.sleep(0.002)
item, path = engine_common.take_oldest()      # claims "one"
engine_common.put_back(path)                  # must go back to the FRONT
item, path = engine_common.take_oldest()
check("put_back keeps the item at the front of the queue",
      item is not None and item["text"] == "one", item)
engine_common.finish(path)

report()
