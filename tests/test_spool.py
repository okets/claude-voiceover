"""The spool: ordering, atomicity, session-scoped clear, age cap."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

data_dir = isolate()
from voiceover import spool


def item_files():
    return sorted(p.name for p in spool.queue_dir().glob("*.json"))


# --- append + count ---------------------------------------------------------
check("enqueue returns True", spool.enqueue("first", "kokoro", "bf_emma") is True)
check("pending_count sees it", spool.pending_count() == 1,
      "count=%d" % spool.pending_count())

# --- item content is complete ----------------------------------------------
data = json.loads((spool.queue_dir() / item_files()[0]).read_text())
check("item carries text/engine/voice",
      data["text"] == "first" and data["engine"] == "kokoro"
      and data["voice"] == "bf_emma",
      data)
check("item carries a created timestamp",
      isinstance(data["created"], float) and data["created"] > 0, data)

# --- ordering is creation order --------------------------------------------
spool.clear()
for word in ("one", "two", "three", "four", "five"):
    spool.enqueue(word, "kokoro", "bf_emma")
    time.sleep(0.002)  # distinct millisecond stamps
texts = [json.loads((spool.queue_dir() / n).read_text())["text"]
         for n in item_files()]
check("filename order equals creation order",
      texts == ["one", "two", "three", "four", "five"], texts)

# --- no partial files are ever visible -------------------------------------
check("no .tmp files left behind",
      list(spool.queue_dir().glob("*.tmp")) == [], "tmp files present")

# --- session-scoped clear ---------------------------------------------------
spool.clear()
spool.enqueue("mine", "kokoro", "bf_emma", session="A")
spool.enqueue("theirs", "kokoro", "bf_emma", session="B")
removed = spool.clear(session="A")
remaining = [json.loads((spool.queue_dir() / n).read_text())["text"]
             for n in item_files()]
check("clear(session) removes only that session", removed == 1, "removed=%d" % removed)
check("the other session's item survives", remaining == ["theirs"], remaining)

# --- clear() with no argument removes everything ---------------------------
spool.enqueue("x", "kokoro", "bf_emma", session="A")
spool.clear()
check("clear() empties the spool", spool.pending_count() == 0)

# --- age cap ----------------------------------------------------------------
spool.enqueue("stale", "kokoro", "bf_emma")
stale_path = spool.queue_dir() / item_files()[0]
stale = json.loads(stale_path.read_text())
stale["created"] = time.time() - (spool.QUEUE_MAX_AGE_SECONDS + 60)
stale_path.write_text(json.dumps(stale))
spool.enqueue("fresh", "kokoro", "bf_emma")   # enqueue prunes first
survivors = [json.loads((spool.queue_dir() / n).read_text())["text"]
             for n in item_files()]
check("enqueue prunes items past the age cap", survivors == ["fresh"], survivors)

# --- concurrent writers -----------------------------------------------------
spool.clear()
writer = REPO_ROOT / "tests" / "_spool_writer.py"
writer.write_text(
    "import sys\n"
    "sys.path.insert(0, %r)\n" % str(REPO_ROOT) +
    "from voiceover import spool\n"
    "for i in range(10):\n"
    "    spool.enqueue('w%s-%d' % (sys.argv[1], i), 'kokoro', 'bf_emma')\n"
)
procs = [subprocess.Popen([sys.executable, str(writer), str(n)], env=os.environ.copy())
         for n in range(8)]
for proc in procs:
    proc.wait()
writer.unlink()
check("8 concurrent writers produce 80 distinct items",
      spool.pending_count() == 80, "count=%d" % spool.pending_count())
check("concurrent writes leave no .tmp files",
      list(spool.queue_dir().glob("*.tmp")) == [])
texts = set()
for name in item_files():
    texts.add(json.loads((spool.queue_dir() / name).read_text())["text"])
check("no item was overwritten by a racing writer", len(texts) == 80, len(texts))

# --- regression: malformed 'created' field does not wedge the queue --------
spool.clear()
corrupt_path = spool.queue_dir() / "0000000000001-1-00.json"
corrupt_item = {
    "text": "corrupt",
    "engine": "kokoro",
    "voice": "bf_emma",
    "created": "not-a-number",  # This should not crash prune()
    "session": None,
}
corrupt_path.write_text(json.dumps(corrupt_item))
check("prune does not raise on malformed 'created'",
      spool.prune() >= 0, "prune() raised or returned invalid")
remaining_after_prune = [json.loads((spool.queue_dir() / n).read_text())["text"]
                         for n in item_files()]
check("corrupt item is removed by prune", remaining_after_prune == [], remaining_after_prune)
enqueued = spool.enqueue("recovery", "kokoro", "bf_emma")
check("enqueue succeeds after pruning corrupt item", enqueued is True,
      "enqueued=%s" % enqueued)
final_texts = [json.loads((spool.queue_dir() / n).read_text())["text"]
               for n in item_files()]
check("new item lands after prune of corrupt item", final_texts == ["recovery"], final_texts)

# --- one queue per project --------------------------------------------------
project_a = temp_dir("vo-proj-a-")
(project_a / ".git").mkdir()
project_b = temp_dir("vo-proj-b-")
(project_b / ".git").mkdir()
spool.enqueue("for a", "kokoro", "bf_emma", cwd=str(project_a))
check("an item lands in its own project's queue",
      spool.pending_count(cwd=str(project_a)) == 1
      and spool.pending_count(cwd=str(project_b)) == 0)
check("the two projects' queue dirs differ",
      spool.queue_dir(cwd=str(project_a)) != spool.queue_dir(cwd=str(project_b)))
spool.enqueue("b one", "kokoro", "bf_emma", session="S", cwd=str(project_b))
spool.clear(session="S", cwd=str(project_a))
check("clear is scoped to one project", spool.pending_count(cwd=str(project_b)) == 1)

# --- message fields -----------------------------------------------------------
spool.clear(cwd=str(project_a))
spool.enqueue("one", "kokoro", "v", cwd=str(project_a))
spool.enqueue("two", "kokoro", "v", cwd=str(project_a))
spool.enqueue("three", "kokoro", "v", utterance="U1", sticky=True, shown=12.5,
              source="pre_tool_use", cwd=str(project_a))
fields = [json.loads(p.read_text())
          for p in sorted(spool.queue_dir(cwd=str(project_a)).glob("*.json"))]
check("an item with no utterance starts a message of its own",
      fields[0]["utterance"] and fields[0]["utterance"] != fields[1]["utterance"],
      fields[:2])
check("a given utterance, sticky flag, shown time and source are kept",
      fields[2]["utterance"] == "U1" and fields[2]["sticky"] is True
      and fields[2]["shown"] == 12.5 and fields[2]["source"] == "pre_tool_use", fields[2])
check("items are not sticky by default", fields[0]["sticky"] is False, fields[0])

# --- the legacy machine-wide queue is removed once -----------------------------
legacy = data_dir / "queue"
legacy.mkdir(exist_ok=True)
(legacy / "0000000000001-1-00.json").write_text("{}")
check("remove_legacy_queue deletes the old items", spool.remove_legacy_queue() == 1)
check("and the old directory", not legacy.exists())
check("a second call has nothing to do", spool.remove_legacy_queue() == 0)

report()
