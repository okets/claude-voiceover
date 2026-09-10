# Narration Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make narrator-level narration play back-to-back from a persistent queue, so it never waits for the next hook to resume and never re-reads stale text.

**Architecture:** Hooks append utterances to a spool directory (one file per utterance, filename-ordered) and advance the transcript cursor on enqueue rather than on playback. Whichever TTS engine holds the lock drains the spool in a loop, loading its model once and speaking items in order until empty. A new `UserPromptSubmit` hook clears the session's pending items and stops playback so a new question is answered immediately.

**Tech Stack:** Python 3.9+, standard library only in `hooks/` and `voiceover/`; `tts/` may use kokoro-onnx / soundfile / numpy via `uv run`. No test framework — plain stdlib test scripts.

**Spec:** `docs/superpowers/specs/2026-09-10-narration-queue-design.md`

## Global Constraints

- **Hook layer is stdlib-only.** `hooks/*.py` and `voiceover/*.py` import nothing outside the Python standard library.
- **`tts/` may never import `voiceover/`.** Different process and venv. Shared engine code goes in `tts/engine_common.py`.
- **Fail silent, never block.** Every hook wraps its body in try/except and always exits 0. Nothing is printed to stdout.
- **Never write into user projects.** All state lives under `data_dir()` — `$VOICEOVER_DATA_DIR` if set, else `~/.claude-voiceover/`.
- **Python >= 3.9.** No `match`, no `X | Y` type syntax at runtime, no walrus cleverness.
- **`VOICEOVER_DRY_RUN`** must keep working: engines print `[voiceover] <text>` to stderr and produce no audio.
- **All tests set `VOICEOVER_DATA_DIR`** to a temp dir and `VOICEOVER_DRY_RUN=1`, so no test touches real state or makes a sound.
- `QUEUE_MAX_AGE_SECONDS = 300` — the single source of truth for the backlog cap.
- Target version: **1.3.0**.

---

### Task 0: Land the status-notice fix and stand up the test harness

The fix for Claude Code's own status notices being narrated is **already applied
and verified** in `voiceover/prose.py` and `voiceover/transcript.py` — it was
made while diagnosing the bug that motivated this feature, and it is already
copied into the installed plugin. It is uncommitted, so this task commits it and
gives the repo its first tests. Do this first: every later task's tests import
the harness created here, and a clean tree makes the feature commits reviewable.

**Files:**
- Already modified (commit as-is): `voiceover/transcript.py`, `voiceover/prose.py`
- Create: `tests/_harness.py`
- Create: `tests/run_all.sh`
- Create: `tests/test_status_notices.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `voiceover.transcript.is_status_notice(entry: dict) -> bool` — True when an
    assistant envelope is a Claude Code status notice rather than Claude's own
    words. Used by `transcript._assistant_blocks` and `prose._assistant_text`.
  - `tests._harness.isolate() -> Path`, `check(name, condition, detail="")`,
    `report()`, `temp_dir(prefix="vo-test-")`, `REPO_ROOT: Path`.

- [ ] **Step 1: Confirm the fix is present in the working tree**

Run: `git diff --stat`
Expected: `voiceover/prose.py` and `voiceover/transcript.py` modified, plus
`.gitignore` (one added line, `.superpowers/`, which keeps this plan's scratch
workspace out of the repo). Nothing else.

Run: `grep -c is_status_notice voiceover/prose.py voiceover/transcript.py`
Expected: `voiceover/prose.py:2` and `voiceover/transcript.py:2`.

If either is missing, the fix is: add `is_status_notice()` to `transcript.py`
(True when `entry["isApiErrorMessage"]` is truthy, or `entry["apiErrorStatus"]`
is not None, or `entry["message"]["model"] == "<synthetic>"`), return `[]` from
`_assistant_blocks` for such an envelope, and return `None` from
`prose._assistant_text` for one.

- [ ] **Step 2: Create the harness**

This harness is imported by every test file in this plan. It gives each test an
isolated data dir and a `check()` that records failures instead of aborting on
the first one.

```python
# tests/_harness.py
"""Minimal stdlib test harness for claude-voiceover.

Each test file calls isolate() first, then check() per assertion, then
report() as its last line. No pytest: the hook layer is stdlib-only and
these tests must run anywhere the plugin runs.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_failures = []
_checks = 0
_temp_dirs = []


def isolate():
    """Point the plugin at a throwaway data dir and enable dry-run audio."""
    data_dir = tempfile.mkdtemp(prefix="vo-test-data-")
    _temp_dirs.append(data_dir)
    os.environ["VOICEOVER_DATA_DIR"] = data_dir
    os.environ["VOICEOVER_DRY_RUN"] = "1"
    sys.path.insert(0, str(REPO_ROOT))
    return Path(data_dir)


def temp_dir(prefix="vo-test-"):
    """A throwaway directory cleaned up at report() time."""
    path = tempfile.mkdtemp(prefix=prefix)
    _temp_dirs.append(path)
    return Path(path)


def check(name, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print("PASS  " + name)
    else:
        print("FAIL  " + name)
        if detail:
            print("        " + str(detail))
        _failures.append(name)


def report():
    """Print the tally, clean up, and exit non-zero if anything failed."""
    for path in _temp_dirs:
        shutil.rmtree(path, ignore_errors=True)
    print("\n%d/%d checks passed in %s" % (
        _checks - len(_failures), _checks, Path(sys.argv[0]).name))
    sys.exit(1 if _failures else 0)
```

- [ ] **Step 3: Create the test runner**

```bash
# tests/run_all.sh
#!/usr/bin/env bash
# Run every claude-voiceover test. No framework: each test file is a script
# that prints PASS/FAIL lines and exits non-zero on any failure.
set -u
cd "$(dirname "$0")/.."
status=0
for test_file in tests/test_*.py; do
    echo "=== $test_file ==="
    if ! python3 "$test_file"; then
        status=1
    fi
    echo
done
if [ "$status" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi
exit "$status"
```

Then: `chmod +x tests/run_all.sh`

- [ ] **Step 4: Write the regression test**

```python
# tests/test_status_notices.py
"""Claude Code's own status notices must never be narrated.

A usage-limit pause is recorded in the transcript as an assistant message
whose model is "<synthetic>". It therefore looked exactly like Claude's own
prose to the tailer: it sat unread across the pause and was then spoken at
the FRONT of the next turn's narration.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir

isolate()
from voiceover.prose import commit_offset, peek_new_prose
from voiceover.transcript import cycle_stats, is_status_notice


def assistant(text, model="claude-fable-5-1", **extra):
    entry = {"type": "assistant", "isSidechain": False,
             "message": {"role": "assistant", "type": "message", "model": model,
                         "content": [{"type": "text", "text": text}]}}
    entry.update(extra)
    return entry


USER = {"type": "user", "message": {"role": "user", "content": "go on"}}
LIMIT = assistant("You've hit your session limit \u00b7 resets 11:20pm (Asia/Bangkok)",
                  model="<synthetic>", isApiErrorMessage=True, apiErrorStatus=429)
NO_RESP = assistant("No response requested.", model="<synthetic>",
                    isApiErrorMessage=False)
LOGIN = assistant("Not logged in \u00b7 Please run /login", model="<synthetic>")
REAL = assistant("Right, picking the thread back up where we left it.")

tx_dir = temp_dir("vo-test-tx-")


def transcript(name, before, after):
    """A transcript with `before` already narrated and `after` unread."""
    path = tx_dir / name
    with open(path, "w", encoding="utf-8") as handle:
        for entry in before:
            handle.write(json.dumps(entry) + "\n")
    commit_offset(str(path), path.stat().st_size)
    with open(path, "a", encoding="utf-8") as handle:
        for entry in after:
            handle.write(json.dumps(entry) + "\n")
    return path


# --- the predicate itself ---------------------------------------------------
check("a 429 limit notice is a status notice", is_status_notice(LIMIT) is True)
check("'No response requested.' is a status notice", is_status_notice(NO_RESP) is True)
check("'Not logged in' is a status notice", is_status_notice(LOGIN) is True)
check("real assistant prose is NOT a status notice", is_status_notice(REAL) is False)
check("a non-dict is handled", is_status_notice(None) is False)

# --- the reported bug: limit notice glued to the resumed prose -------------
prose, _ = peek_new_prose(str(transcript("pause.jsonl", [USER], [LIMIT, REAL])))
check("the limit notice is not narrated after a pause",
      prose is not None and "session limit" not in prose, "spoke: %r" % prose)
check("the real resumed prose IS still narrated",
      prose is not None and "picking the thread back up" in prose, "spoke: %r" % prose)

# --- a notice alone leaves nothing to say ---------------------------------
prose, _ = peek_new_prose(str(transcript("noresp.jsonl", [USER], [NO_RESP])))
check("a lone status notice yields no narration", prose is None, "spoke: %r" % prose)

# --- regression guard: ordinary prose still narrated ----------------------
prose, _ = peek_new_prose(str(transcript("normal.jsonl", [USER], [REAL])))
check("ordinary assistant prose is narrated",
      prose is not None and "picking the thread" in prose, "spoke: %r" % prose)

# --- the other reader, used at concise/verbose levels --------------------
path = tx_dir / "cycle.jsonl"
with open(path, "w", encoding="utf-8") as handle:
    for entry in (USER, REAL, LIMIT):
        handle.write(json.dumps(entry) + "\n")
stats = cycle_stats(str(path))
check("cycle_stats does not report the notice as the final response",
      stats.final_response_text is not None
      and "session limit" not in stats.final_response_text,
      "final_response_text: %r" % stats.final_response_text)

report()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 tests/test_status_notices.py`
Expected: PASS on all 10 checks, exit 0.

(The fix is already in place, so this test passes immediately. To see it fail
first — the honest TDD check — run
`git stash && python3 tests/test_status_notices.py; git stash pop` and confirm
the limit-notice checks fail before the fix.)

- [ ] **Step 6: Run the whole suite**

Run: `bash tests/run_all.sh`
Expected: `ALL TESTS PASSED`, exit 0.

- [ ] **Step 7: Commit**

```bash
git add voiceover/transcript.py voiceover/prose.py .gitignore \
        tests/_harness.py tests/run_all.sh tests/test_status_notices.py
git commit -m "fix: never narrate Claude Code's own status notices"
```

---

### Task 1: The spool — writer side

**Files:**
- Create: `voiceover/spool.py`
- Create: `tests/test_spool.py`

**Interfaces:**
- Consumes: `voiceover.settings.data_dir()` (existing); the test harness from Task 0.
- Produces:
  - `voiceover.spool.QUEUE_MAX_AGE_SECONDS: int = 300`
  - `voiceover.spool.queue_dir() -> pathlib.Path`
  - `voiceover.spool.enqueue(text: str, engine: str, voice: str, session=None) -> bool`
  - `voiceover.spool.clear(session=None) -> int`
  - `voiceover.spool.pending_count() -> int`
  - `voiceover.spool.prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS) -> int`
  - Item file: `<epoch_ms:013d>-<pid>-<seq:02d>.json`, content
    `{"text": str, "engine": str, "voice": str, "created": float, "session": str|None}`

- [ ] **Step 1: Confirm the harness from Task 0 is present**

Run: `ls tests/_harness.py tests/run_all.sh`
Expected: both exist. They were created in Task 0; do not recreate them.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_spool.py
"""The spool: ordering, atomicity, session-scoped clear, age cap."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
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

report()
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python3 tests/test_spool.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'voiceover.spool'`

- [ ] **Step 4: Write the implementation**

```python
# voiceover/spool.py
"""The narration queue for claude-voiceover.

One directory, one file per pending utterance. Filenames start with a
zero-padded millisecond timestamp, so listing them in name order lists them
in the order they should be spoken. Writers create a temp file and rename it
into place, so a reader never observes a half-written item.

This module is the WRITE half, used by the stdlib hook layer. Engines cannot
import voiceover/, so they carry the read half in tts/engine_common.py; the
two sides share nothing but the directory path and the filename sort rule.

Stdlib only. Every function fails soft: on any OSError it returns a falsy
result and the caller carries on unnarrated rather than breaking a session.
"""

import json
import os
import time
from pathlib import Path

from .settings import data_dir

QUEUE_MAX_AGE_SECONDS = 300  # a five-minute backlog cap

_QUEUE_DIR_NAME = "queue"
_seq = 0


def queue_dir() -> Path:
    """The spool directory, created on demand."""
    directory = data_dir() / _QUEUE_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def _item_name() -> str:
    """A filename whose lexicographic order is chronological order.

    13 digits of millisecond epoch stays fixed-width past the year 2286; the
    pid and a per-process counter keep two items from one instant distinct
    and in the order they were enqueued.
    """
    global _seq
    # Wraps at 100: one process would have to enqueue more than 100 items
    # inside a single millisecond for names to repeat, which real per-item
    # file I/O makes unreachable.
    _seq = (_seq + 1) % 100
    return "%013d-%d-%02d.json" % (int(time.time() * 1000), os.getpid(), _seq)


def enqueue(text, engine, voice, session=None) -> bool:
    """Append one utterance to the spool. True when it was written.

    Prunes the backlog first, so the age cap needs no timer or daemon.
    """
    try:
        if not text or not str(text).strip():
            return False
        prune()
        payload = {
            "text": str(text).strip(),
            "engine": engine,
            "voice": voice,
            "created": time.time(),
            "session": session,
        }
        directory = queue_dir()
        name = _item_name()
        temp_path = directory / (name + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        # Rename is atomic: the item appears complete or not at all.
        os.rename(str(temp_path), str(directory / name))
        return True
    except OSError:
        return False


def _items():
    """Pending item paths, oldest first. Only complete items are listed."""
    try:
        return sorted(queue_dir().glob("*.json"))
    except OSError:
        return []


def pending_count() -> int:
    return len(_items())


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def clear(session=None) -> int:
    """Drop pending items - all of them, or only one session's. Returns how many."""
    removed = 0
    for path in _items():
        if session is not None:
            item = _read(path)
            if item is not None and item.get("session") != session:
                continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS) -> int:
    """Drop items older than the cap, plus leftovers from a killed reader."""
    removed = 0
    cutoff = time.time() - max_age_seconds
    for path in _items():
        item = _read(path)
        # A malformed 'created' is corruption, not a young item: treat the
        # item as stale and drop it. Letting the conversion raise here would
        # wedge the queue - enqueue() prunes first, so one bad file would
        # silently stop every future utterance.
        created = None
        if item is not None:
            try:
                created = float(item.get("created") or 0)
            except (TypeError, ValueError):
                created = None
        if item is None or created is None or created < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    for pattern in ("*.taken", "*.tmp"):
        try:
            for path in queue_dir().glob(pattern):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        except OSError:
            pass
    return removed
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 tests/test_spool.py`
Expected: PASS on all 12 checks, exit 0

- [ ] **Step 6: Commit**

```bash
git add voiceover/spool.py tests/test_spool.py
git commit -m "feat: narration spool - the write half of the queue"
```

---

### Task 2: Shared engine module — lock helpers and the spool reader

Both engines carry their own inline copy of `data_dir()` and the four lock helpers today. Drain mode needs those plus a spool reader, which would mean four copies. This task moves them into one sibling module and deletes both copies. Pure refactor for the lock helpers — no behaviour change — plus the new reader functions.

**Files:**
- Create: `tts/engine_common.py`
- Create: `tests/test_engine_common.py`
- Modify: `tts/kokoro_voice.py` — delete lines 69-155 (the `data_dir` / lock block) and import instead
- Modify: `tts/macos_say.py` — delete its equivalent `data_dir` / lock block and import instead

**Interfaces:**
- Consumes: the item format and filename sort rule from Task 1.
- Produces (all stdlib-only, importable by both engines as a sibling):
  - `data_dir() -> Path`
  - `tts_lock_path() -> Path`
  - `try_claim_lock(duration: float) -> bool`
  - `update_lock_expiry(duration: float) -> None`
  - `remove_tts_lock() -> None`
  - `lock_is_live() -> bool`
  - `queue_dir() -> Path`
  - `take_oldest() -> tuple[dict, Path] | tuple[None, None]` — claims the oldest item by renaming it to `<name>.taken`, returns the parsed item and that path
  - `finish(taken_path: Path) -> None` — delete a claimed item (spoken successfully)
  - `put_back(taken_path: Path) -> None` — restore a claimed item to its original name, preserving order

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_engine_common.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine_common'`

- [ ] **Step 3: Write the shared module**

The `data_dir` and lock functions below are moved verbatim from `tts/kokoro_voice.py:69-155`, with `lock_is_live()` added and the spool reader appended.

```python
# tts/engine_common.py
"""Shared engine-side helpers for the claude-voiceover TTS engines.

Imported as a sibling by tts/kokoro_voice.py and tts/macos_say.py, both of
which resolve it through sys.path[0] (their own directory) whether they run
under `uv run` or plain python3.

This file MUST NOT import from voiceover/ - the engines live in a different
process and venv. It therefore duplicates the data-dir resolution by design,
but it is the ONLY copy: both engines import it rather than carrying their
own. Stdlib only.

The spool it reads is written by voiceover/spool.py. The shared contract is
just this: pending items are `*.json` files in data_dir()/queue/ whose
filenames sort chronologically, each holding
{"text", "engine", "voice", "created", "session"}.
"""

import json
import os
import time
from pathlib import Path

_QUEUE_DIR_NAME = "queue"
_TAKEN_SUFFIX = ".taken"


# --- data dir / tts.lock ----------------------------------------------------

def data_dir():
    """$VOICEOVER_DATA_DIR override if set, else ~/.claude-voiceover. Created on demand."""
    root = os.environ.get("VOICEOVER_DATA_DIR")
    path = Path(root) if root else Path.home() / ".claude-voiceover"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def tts_lock_path():
    return data_dir() / "tts.lock"


def _parse_lock_expiry(raw):
    """Lock content is JSON {"expiry", "pid"}; older locks were a bare float."""
    try:
        return float(json.loads(raw).get("expiry"))
    except Exception:
        try:
            return float(raw)
        except Exception:
            return None


def lock_is_live() -> bool:
    """True while an unexpired lock exists. Read-only: never clears anything."""
    try:
        raw = tts_lock_path().read_text().strip()
    except OSError:
        return False
    expiry = _parse_lock_expiry(raw)
    return expiry is not None and time.time() < expiry


def try_claim_lock(duration):
    """Atomically claim the TTS lock. True = we own it and may speak.

    O_CREAT|O_EXCL semantics via os.link guarantee that of two engines
    racing, exactly one wins; the loser skips quietly. The lock stores our
    PID so stop_speech() can kill this engine's whole process group.
    """
    lock_file = tts_lock_path()
    payload = json.dumps({"expiry": time.time() + duration, "pid": os.getpid()})
    # Write payload to a private temp file, then os.link() it into place:
    # the lock appears WITH its content in one atomic step, so a racing
    # claimer can never observe an empty lock and judge it stale.
    tmp_file = lock_file.with_name("tts.lock.{}".format(os.getpid()))
    try:
        tmp_file.write_text(payload)
    except OSError:
        return False
    try:
        for _ in range(2):
            try:
                os.link(str(tmp_file), str(lock_file))
                return True
            except FileExistsError:
                try:
                    expiry = _parse_lock_expiry(lock_file.read_text().strip())
                except OSError:
                    expiry = None
                if expiry is not None and time.time() < expiry:
                    return False  # someone else is speaking
                try:
                    lock_file.unlink()  # stale - retry the atomic claim once
                except OSError:
                    return False
            except OSError:
                return False
        return False
    finally:
        try:
            tmp_file.unlink()
        except OSError:
            pass


def update_lock_expiry(duration):
    """Refresh our own lock with the real playback end time (keeps our PID)."""
    try:
        tts_lock_path().write_text(
            json.dumps({"expiry": time.time() + duration, "pid": os.getpid()}))
    except OSError:
        pass


def remove_tts_lock():
    try:
        lock_file = tts_lock_path()
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


# --- spool reading ----------------------------------------------------------

def queue_dir():
    directory = data_dir() / _QUEUE_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def take_oldest():
    """Claim the oldest pending item. Returns (item, taken_path) or (None, None).

    The claim is an atomic rename to <name>.taken, so of two engines reading
    at once exactly one gets any given item. Unreadable items are discarded
    and the next one is tried.
    """
    try:
        candidates = sorted(queue_dir().glob("*.json"))
    except OSError:
        return None, None
    for path in candidates:
        taken_path = path.with_name(path.name + _TAKEN_SUFFIX)
        try:
            os.rename(str(path), str(taken_path))
        except OSError:
            continue  # another engine claimed it first
        try:
            with open(taken_path, encoding="utf-8") as handle:
                item = json.load(handle)
        except (OSError, ValueError):
            item = None
        if not isinstance(item, dict) or not item.get("text"):
            finish(taken_path)  # corrupt: drop it and move on
            continue
        return item, taken_path
    return None, None


def finish(taken_path) -> None:
    """Discard a claimed item - it has been spoken (or was unusable)."""
    try:
        Path(taken_path).unlink()
    except OSError:
        pass


def put_back(taken_path) -> None:
    """Return a claimed item to the queue under its original name.

    The name carries the timestamp, so restoring it keeps the item's place in
    the play order rather than sending it to the back.
    """
    try:
        path = Path(taken_path)
        original = path.with_name(path.name[:-len(_TAKEN_SUFFIX)])
        os.rename(str(path), str(original))
    except OSError:
        pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 tests/test_engine_common.py`
Expected: PASS on all 15 checks, exit 0

- [ ] **Step 5: Delete the duplicated block from `tts/kokoro_voice.py`**

Remove lines 69-155 — everything from the `# --- data dir / tts.lock (inline: may not import voiceover.settings) --------` comment through the end of `remove_tts_lock()` — and replace with an import. The module docstring's claim about duplication needs correcting too.

Replace this docstring sentence:

```python
This file is fully self-contained: it runs inside the tts/ dependency world
(kokoro-onnx, soundfile, numpy) and MUST NOT import from voiceover/. The
data-dir/lock resolution and audio playback are therefore duplicated inline.
```

with:

```python
This file runs inside the tts/ dependency world (kokoro-onnx, soundfile,
numpy) and MUST NOT import from voiceover/. The data-dir, lock and spool
helpers therefore come from tts/engine_common.py, a stdlib-only sibling
shared with macos_say.py; audio playback stays duplicated inline.
```

And insert after the existing imports:

```python
from engine_common import remove_tts_lock, try_claim_lock, update_lock_expiry
```

Those three are the only ones this file calls directly (verified: `data_dir`
and `tts_lock_path` were used solely by the lock helpers themselves, which now
live in the shared module). Drain mode imports what it needs locally in Task 4.

- [ ] **Step 6: Delete the duplicated block from `tts/macos_say.py`**

Remove lines 28-105 — the `# --- data dir / tts.lock (inline: may not import voiceover.settings) --------` comment through the end of `remove_tts_lock()` — and add:

```python
from engine_common import remove_tts_lock, try_claim_lock, update_lock_expiry
```

This file has no `update_lock_expiry` of its own today; it is imported here
because drain mode uses it in Task 4. Keep `estimate_duration()`, `speak()`,
`parse_args()` and `main()` untouched in this task.

- [ ] **Step 7: Verify both engines still work unchanged**

Run:
```bash
VOICEOVER_DRY_RUN=1 python3 tts/macos_say.py --voice Samantha "refactor smoke test"
VOICEOVER_DRY_RUN=1 uv run --project tts tts/kokoro_voice.py "refactor smoke test" --voice bf_emma
python3 tests/test_engine_common.py
```
Expected: each engine prints `[voiceover] refactor smoke test` to stderr and exits 0; the test passes.

- [ ] **Step 8: Commit**

```bash
git add tts/engine_common.py tts/kokoro_voice.py tts/macos_say.py tests/test_engine_common.py
git commit -m "refactor: one shared engine module for data dir, lock and spool reading"
```

---

### Task 3: Enqueue and drainer-spawn in the speech layer

**Files:**
- Modify: `voiceover/speech.py` — add two functions; leave `speak()` and everything else untouched
- Create: `tests/test_enqueue_speech.py`

**Interfaces:**
- Consumes: `voiceover.spool.enqueue/pending_count` (Task 1); existing `settings.resolve_engine/get_voice/is_tts_enabled/level_at_least`; existing `speech.truncate_for_speech`, `speech._FULL_TEXT_CAP`, `speech._is_locked`, `speech._spawn_detached`, `speech._dry_run`.
- Produces:
  - `speech.enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None) -> bool`
  - `speech.ensure_drainer(cwd=None) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enqueue_speech.py
"""enqueue_speech gates like speak() but never loses to the lock."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report

data_dir = isolate()
from voiceover import spool, speech
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
set_setting("voice", "bf_emma")


def texts():
    out = []
    for path in sorted(spool.queue_dir().glob("*.json")):
        out.append(json.loads(path.read_text())["text"])
    return out


# --- the basic path ---------------------------------------------------------
check("enqueue_speech returns True", speech.enqueue_speech("hello there", full=True) is True)
check("the text landed in the spool", texts() == ["hello there"], texts())

# --- it resolves engine and voice ------------------------------------------
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("engine is resolved at enqueue time", item["engine"] == "kokoro", item)
check("voice is resolved at enqueue time", item["voice"] == "bf_emma", item)

# --- THE POINT: a held lock does not stop queueing -------------------------
spool.clear()
speech.lock_path().write_text(json.dumps({"expiry": time.time() + 60, "pid": 1}))
check("the lock reads as held", speech._is_locked() is True)
check("enqueue_speech still succeeds while audio plays",
      speech.enqueue_speech("queued during playback", full=True) is True)
check("the text is queued, not dropped", texts() == ["queued during playback"], texts())
speech.lock_path().unlink()

# --- level gating still applies --------------------------------------------
spool.clear()
set_setting("interaction_level", "silent")
check("silent level enqueues nothing", speech.enqueue_speech("shh", full=True) is False)
check("the spool stayed empty", spool.pending_count() == 0)
set_setting("interaction_level", "narrator")

# --- truncation contract mirrors speak() -----------------------------------
spool.clear()
long_text = " ".join(["word"] * 400)
speech.enqueue_speech(long_text, full=False)
short = texts()[0]
check("full=False truncates like speak()", len(short) < len(long_text), len(short))
spool.clear()
speech.enqueue_speech(long_text, full=True)
check("full=True keeps the whole text", len(texts()[0]) > 1000, len(texts()[0]))

# --- empty text is refused --------------------------------------------------
spool.clear()
check("empty text is refused", speech.enqueue_speech("   ", full=True) is False)
check("whitespace never reaches the spool", spool.pending_count() == 0)

# --- session tagging --------------------------------------------------------
spool.clear()
speech.enqueue_speech("tagged", full=True, session="S1")
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("the session id is recorded", item["session"] == "S1", item)

# --- ensure_drainer -------------------------------------------------------
spool.clear()
check("no drainer is spawned for an empty spool", speech.ensure_drainer() is False)
speech.enqueue_speech("something to say", full=True)
speech.lock_path().write_text(json.dumps({"expiry": time.time() + 60, "pid": 1}))
check("no drainer is spawned while one already holds the lock",
      speech.ensure_drainer() is False)
speech.lock_path().unlink()
check("a drainer is spawned for a non-empty spool with a free lock",
      speech.ensure_drainer() is True)

report()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_enqueue_speech.py`
Expected: FAIL — `AttributeError: module 'voiceover.speech' has no attribute 'enqueue_speech'`

- [ ] **Step 3: Write the implementation**

Add to `voiceover/speech.py`. Extend the existing `from . import process_utils` import block with `from . import spool`, and add these two functions after `speak()`:

```python
def enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None) -> bool:
    """Queue text to be spoken. True when it was queued.

    The queued counterpart of speak(): identical gating and identical
    truncation, but it never fails because audio is already playing. That is
    the whole point - a caller that tracks what has been narrated (the prose
    tailer) can advance its cursor the moment this returns True, because a
    queued utterance is guaranteed to be spoken.
    """
    try:
        if not text or not str(text).strip():
            return False
        if not is_tts_enabled(cwd) or not level_at_least(min_level, cwd):
            _log("queue", "gated by level/enabled", cwd)
            return False
        if full:
            message = str(text).strip()[:_FULL_TEXT_CAP]
        else:
            message = truncate_for_speech(str(text).strip())
        engine = resolve_engine(cwd)
        if engine == "none":
            return False
        queued = spool.enqueue(message, engine, get_voice(cwd), session=session)
        _log("queue", "queued=%s pending=%d chars=%d :: %.60s" % (
            queued, spool.pending_count(), len(message),
            message.replace("\n", " ")), cwd)
        return queued
    except Exception:
        return False


def ensure_drainer(cwd=None) -> bool:
    """Start an engine draining the spool unless one is already speaking.

    Called unconditionally by every narrator hook path, so a spool left
    behind by a crashed engine is picked up by the next hook rather than
    sitting there. Racing hooks are harmless: the engine's own atomic lock
    claim admits exactly one drainer.
    """
    try:
        if spool.pending_count() == 0:
            return False
        if _is_locked():
            return False  # a drainer is already working through the queue
        engine = resolve_engine(cwd)
        if engine == "kokoro":
            command = [
                "uv", "run", "--project", str(_TTS_DIR),
                str(_TTS_DIR / "kokoro_voice.py"), "--drain",
            ]
        elif engine in _MACOS_VOICES:
            command = ["python3", str(_TTS_DIR / "macos_say.py"), "--drain"]
        else:
            return False
        spawned = _spawn_detached(command)
        _log("queue", "drainer spawned=%s engine=%s pending=%d" % (
            spawned, engine, spool.pending_count()), cwd)
        return spawned
    except Exception:
        return False
```

Note `--drain` takes no text and no voice: the drainer reads both from each item.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 tests/test_enqueue_speech.py`
Expected: PASS on all 18 checks, exit 0

- [ ] **Step 5: Commit**

```bash
git add voiceover/speech.py tests/test_enqueue_speech.py
git commit -m "feat: enqueue_speech and ensure_drainer"
```

---

### Task 4: Drain mode in both engines

**Files:**
- Modify: `tts/macos_say.py` — add `--drain`
- Modify: `tts/kokoro_voice.py` — add `--drain`
- Create: `tests/test_drain.py`

**Interfaces:**
- Consumes: `engine_common.take_oldest/finish/put_back/try_claim_lock/remove_tts_lock/update_lock_expiry` (Task 2).
- Produces: both engines accept `--drain` with no text argument. Exit 0 when the spool is empty or another engine holds the lock.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_drain.py`
Expected: FAIL — the first check fails because `--drain` is treated as text to speak

- [ ] **Step 3: Add the shared drain loop to `tts/engine_common.py`**

The loop is identical for both engines; only "speak one item" differs. So the loop lives here and takes that as a callback.

```python
# append to tts/engine_common.py

# --- the drain loop ---------------------------------------------------------

QUEUE_MAX_AGE_SECONDS = 300  # must match voiceover/spool.py


def drain(speak_item, engine_names, lock_seconds=60.0) -> int:
    """Speak the spool dry, one item at a time. Returns a process exit code.

    speak_item(item) -> bool is called for each item this engine owns;
    engine_names is the set of item["engine"] values it can handle.

    The loop holds the TTS lock for as long as it has work, so the model is
    loaded once per drain rather than once per utterance. When the spool runs
    dry it releases the lock and checks ONCE more before exiting: that closes
    the race against a hook that appended an item a moment earlier and saw
    the lock still held, which would otherwise strand that item until the
    next hook fired.
    """
    if not try_claim_lock(lock_seconds):
        return 0  # another engine is already draining
    owns_lock = True
    try:
        while True:
            item, taken_path = take_oldest()
            if item is None:
                remove_tts_lock()
                owns_lock = False
                item, taken_path = take_oldest()
                if item is None:
                    return 0
                if not try_claim_lock(lock_seconds):
                    # Another engine claimed it in the gap - it will drain
                    # this item. Never delete a lock we do not own.
                    put_back(taken_path)
                    return 0
                owns_lock = True
                # NO continue here: fall through and speak the item just
                # claimed. A continue would re-scan the spool, and this item
                # is already renamed to .taken - invisible to that scan.
            if item.get("engine") not in engine_names:
                put_back(taken_path)   # someone else's engine; keep its place
                return 0
            age = time.time() - float(item.get("created") or 0)
            if age > QUEUE_MAX_AGE_SECONDS:
                finish(taken_path)     # too stale to be worth hearing
                continue
            update_lock_expiry(lock_seconds)
            try:
                speak_item(item)
            except Exception:
                pass
            finish(taken_path)
    finally:
        if owns_lock:
            remove_tts_lock()
```

- [ ] **Step 4: Wire `--drain` into `tts/macos_say.py`**

`speak(voice, text)` claims and removes the lock itself, which would end a drain
after one item. Split the actual speaking out so both paths share it, leaving
`speak()` behaving exactly as before. Replace the existing `speak()`:

```python
def say_once(voice, text) -> bool:
    """Speak one utterance. Does NOT touch the tts lock - the caller owns it."""
    try:
        subprocess.run(["say", "-v", voice], input=text, text=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as error:
        print("[ERROR] say failed: " + str(error), file=sys.stderr)
        return False


def speak(voice, text):
    """Single-utterance path: claim the lock, speak, release it."""
    if not try_claim_lock(estimate_duration(text)):
        return True  # another narration is playing - skip quietly
    try:
        return say_once(voice, text)
    finally:
        remove_tts_lock()
```

Then add drain mode at the very top of `main()`, before `parse_args` — which
would otherwise treat `--drain` as text to speak:

```python
def main():
    if "--drain" in sys.argv[1:]:
        from engine_common import drain

        def speak_one(item):
            text = item["text"]
            voice = item.get("voice") or DEFAULT_VOICE
            if DRY_RUN:
                print("[voiceover] " + text, file=sys.stderr)
                return True
            if sys.platform != "darwin":
                return False
            # Extend the lock to this item's real length before speaking, so a
            # long utterance cannot let the lock expire under a sibling engine.
            update_lock_expiry(estimate_duration(text) + 5.0)
            return say_once(voice, text)

        return drain(speak_one, {"macos-female", "macos-male"})

    voice, text = parse_args(sys.argv[1:])
    ...   # the rest of main() unchanged
```

- [ ] **Step 5: Wire `--drain` into `tts/kokoro_voice.py`**

Add at the very top of `main()`, before `parse_args(sys.argv[1:])` — which
would otherwise collect `--drain` as text to speak:

```python
    if "--drain" in sys.argv[1:]:
        from engine_common import drain

        if DRY_RUN:
            def speak_one(item):
                print("[voiceover] " + item["text"], file=sys.stderr)
                return True
            return drain(speak_one, {"kokoro"})

        if not ensure_models():
            return 1
        from kokoro_onnx import Kokoro

        # Loaded ONCE for the whole drain - this is where the per-sentence
        # spawn and model-reload cost goes away.
        kokoro = Kokoro(str(MODEL_FILE), str(VOICES_FILE))

        def speak_one(item):
            voice = item.get("voice") or DEFAULT_VOICE
            settings = get_voice_settings(voice)
            samples, sample_rate = kokoro.create(
                text=item["text"], voice=voice, speed=settings["speed"],
                lang=settings["lang"], trim=settings["trim"])
            duration = len(samples) / float(sample_rate) if sample_rate else 0.0
            update_lock_expiry(duration + 20.0)
            return _play_samples_keeping_lock(samples, sample_rate, duration)

        return drain(speak_one, {"kokoro"})
```

`speak_text()`'s existing helpers remove the lock when playback ends, which would end the drain after one item. Add a variant that leaves the lock alone, next to `play_samples()`:

```python
def _play_samples_keeping_lock(samples, sample_rate, duration):
    """Play samples WITHOUT removing the tts lock - the drain loop owns it."""
    import soundfile as sf

    tmp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    try:
        sf.write(tmp_path, samples, sample_rate)
        return play_audio_file(tmp_path, timeout=max(30, int(duration) + 10))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 tests/test_drain.py`
Expected: PASS on all 17 checks, exit 0

- [ ] **Step 7: Verify the kokoro drainer too**

Run:
```bash
VOICEOVER_DATA_DIR=/tmp/vo-drain VOICEOVER_DRY_RUN=1 python3 -c "
import sys; sys.path.insert(0,'.')
from voiceover import spool
spool.enqueue('one', 'kokoro', 'bf_emma'); spool.enqueue('two', 'kokoro', 'bf_emma')"
VOICEOVER_DATA_DIR=/tmp/vo-drain VOICEOVER_DRY_RUN=1 uv run --project tts tts/kokoro_voice.py --drain
```
Expected: `[voiceover] one` then `[voiceover] two` on stderr, exit 0.

- [ ] **Step 8: Commit**

```bash
git add tts/engine_common.py tts/kokoro_voice.py tts/macos_say.py tests/test_drain.py
git commit -m "feat: drain mode - speak the spool back to back, model loaded once"
```

---

### Task 5: Point the narrator hook paths at the queue

**Files:**
- Modify: `hooks/pre_tool_use.py:27-66`
- Modify: `hooks/post_tool_use.py:24-33`
- Modify: `hooks/stop.py:43-80`
- Modify: `hooks/notification.py:53-69`
- Create: `tests/test_narrator_hooks.py`

**Interfaces:**
- Consumes: `speech.enqueue_speech/ensure_drainer` (Task 3); existing `prose.peek_new_prose/commit_offset`.
- Produces: no new names. Behaviour change only — the narrator paths queue instead of racing the lock, and never interrupt.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_narrator_hooks.py
"""The narrator hook paths queue their text and never lose it to the lock."""

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
set_setting("tts_engine", "kokoro")
set_setting("voice", "bf_emma")

transcripts = Path(os.environ["VOICEOVER_DATA_DIR"]) / "tx"
transcripts.mkdir(parents=True, exist_ok=True)


def write_transcript(name, entries):
    path = transcripts / name
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def assistant(text):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "type": "message",
                        "model": "claude-fable-5-1",
                        "content": [{"type": "text", "text": text}]}}


USER = {"type": "user", "message": {"role": "user", "content": "go"}}


def run_hook(name, payload):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / (name + ".py"))],
        input=json.dumps(payload), capture_output=True, text=True,
        env=os.environ.copy())
    return proc


def queued_texts():
    out = []
    for path in sorted(spool.queue_dir().glob("*.json")):
        out.append(json.loads(path.read_text())["text"])
    return out


# --- post_tool_use queues Claude's prose -----------------------------------
spool.clear()
path = write_transcript("post.jsonl", [USER])
from voiceover.prose import commit_offset
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Reading the settings loader now.")) + "\n")
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("post_tool_use exits 0", proc.returncode == 0, proc.stderr)
check("post_tool_use queued the prose",
      queued_texts() == ["Reading the settings loader now."], queued_texts())

# --- THE POINT: a held lock no longer costs the text ----------------------
spool.clear()
path = write_transcript("locked.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("This must not be lost.")) + "\n")
(Path(os.environ["VOICEOVER_DATA_DIR"]) / "tts.lock").write_text(
    json.dumps({"expiry": time.time() + 60, "pid": 1}))
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("the hook still exits 0 while audio plays", proc.returncode == 0, proc.stderr)
check("the prose was queued, not skipped",
      queued_texts() == ["This must not be lost."], queued_texts())

# --- and the cursor advanced, so it is not read twice --------------------
proc = run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                  "tool_name": "Bash", "tool_input": {},
                                  "session_id": "S1"})
check("the same prose is not queued a second time",
      queued_texts() == ["This must not be lost."], queued_texts())
(Path(os.environ["VOICEOVER_DATA_DIR"]) / "tts.lock").unlink()

# --- pre_tool_use queues too ----------------------------------------------
spool.clear()
path = write_transcript("pre.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("About to run the tests.")) + "\n")
proc = run_hook("pre_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                                 "tool_name": "Bash", "tool_input": {},
                                 "session_id": "S1"})
check("pre_tool_use exits 0", proc.returncode == 0, proc.stderr)
check("pre_tool_use queued the prose",
      queued_texts() == ["About to run the tests."], queued_texts())

# --- stop queues the finale, and does not interrupt ----------------------
spool.clear()
path = write_transcript("stop.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("All done, tests are green.")) + "\n")
    handle.write(json.dumps(assistant("Here is the summary.")) + "\n")
proc = run_hook("stop", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                         "session_id": "S1"})
check("stop exits 0", proc.returncode == 0, proc.stderr)
check("stop queued the closing prose", len(queued_texts()) >= 1, queued_texts())

# --- status notices still never get queued (regression) ------------------
spool.clear()
path = write_transcript("synthetic.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "apiErrorStatus": 429,
        "message": {"role": "assistant", "type": "message", "model": "<synthetic>",
                    "content": [{"type": "text",
                                 "text": "You've hit your session limit"}]}}) + "\n")
run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                           "tool_name": "Bash", "tool_input": {}, "session_id": "S1"})
check("a session-limit notice is never queued", spool.pending_count() == 0,
      queued_texts())

# --- items are tagged with the session -----------------------------------
spool.clear()
path = write_transcript("session.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("Tagged with a session.")) + "\n")
run_hook("post_tool_use", {"cwd": str(REPO_ROOT), "transcript_path": str(path),
                           "tool_name": "Bash", "tool_input": {},
                           "session_id": "SESSION-XYZ"})
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("the hook records its session id", item["session"] == "SESSION-XYZ", item)

report()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_narrator_hooks.py`
Expected: FAIL — "the prose was queued, not skipped" fails, because today the hook drops the text when the lock is held

- [ ] **Step 3: Rewrite the narrator branch of `hooks/post_tool_use.py`**

Replace lines 24-33 with:

```python
    if get_interaction_level(payload.get("cwd")) == "narrator":
        # No '- done' chatter. Prose written just before this tool call
        # (which pre-tool can miss by a flush race) is queued here instead,
        # at the tool's completion.
        from voiceover.prose import commit_offset, peek_new_prose
        from voiceover.speech import ensure_drainer, enqueue_speech

        transcript = payload.get("transcript_path", "")
        cwd = payload.get("cwd")
        text, offset = peek_new_prose(transcript)
        if text and enqueue_speech(text, cwd=cwd, full=True,
                                   session=payload.get("session_id")):
            # Queued means it WILL be spoken, so the cursor can advance now.
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
        return
```

- [ ] **Step 4: Rewrite the narrator branch of `hooks/pre_tool_use.py`**

Replace lines 27-66 with:

```python
    if get_interaction_level(cwd) == "narrator":
        # Narrator mode: queue Claude's actual words written since the last
        # narration. Tool play-by-play stays silent, and nothing interrupts -
        # the queue keeps order and the drainer plays it back to back.
        from voiceover.speech import ensure_drainer, enqueue_speech

        transcript = payload.get("transcript_path", "")
        tool_name = payload.get("tool_name", "")
        session = payload.get("session_id")
        text, offset = peek_new_prose(transcript)
        if tool_name in ("AskUserQuestion", "ExitPlanMode"):
            # A dialog is about to block the session with no further hooks.
            # Wait out the transcript flush race for the lead-in prose, then
            # queue prose + an explicit 'I need your input' announcement of
            # the actual question.
            if not text:
                for _ in range(3):
                    time.sleep(0.5)
                    text, offset = peek_new_prose(transcript)
                    if text:
                        break
            from voiceover.templates import blocking_dialog_message
            alert = blocking_dialog_message(tool_name, payload.get("tool_input") or {})
            combined = (text + "\n" + alert) if text else alert
            if enqueue_speech(combined, cwd=cwd, full=True, session=session) and text:
                commit_offset(transcript, offset)
            ensure_drainer(cwd)
            # Tell the notification hook this block was already announced,
            # so its "needs permission" echo stays quiet (marker-based:
            # message wording is not parseable reliably).
            try:
                import json as _json
                import time as _time
                from voiceover.settings import data_dir
                with open(data_dir() / "dialog_alert.json", "w") as handle:
                    _json.dump({"ts": _time.time()}, handle)
            except Exception:
                pass
            return
        if text and enqueue_speech(text, cwd=cwd, full=True, session=session):
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
        return
```

Note the dialog marker is now written whenever the announcement is queued, not only when audio was dispatched — queueing is the guarantee.

- [ ] **Step 5: Rewrite the narrator branch of `hooks/stop.py`**

Replace lines 43-66 with:

```python
    if level == "narrator":
        from voiceover.debuglog import log as _log
        from voiceover.prose import commit_offset, peek_new_prose
        from voiceover.speech import ensure_drainer, enqueue_speech
        import os as _os
        _log("stop", "enter size=%s" % _os.path.getsize(transcript_path), cwd)
        # The final assistant message is flushed to the transcript shortly
        # AFTER Stop fires (measured ~0.5s); poll briefly so the finale is
        # actually readable instead of always arriving one turn late.
        prose, offset = peek_new_prose(transcript_path)
        retries = 0
        for _ in range(10):
            if prose:
                break
            time.sleep(0.5)
            prose, offset = peek_new_prose(transcript_path)
            retries += 1
        _log("stop", "peek after %d retries: %d chars, size=%s" % (
            retries, len(prose or ""), _os.path.getsize(transcript_path)), cwd)
        if prose:
            # The finale is Claude's own closing words. It goes to the BACK of
            # the queue: order is what makes narration followable, and the
            # queue drains without waiting for another hook anyway.
            if enqueue_speech(prose, cwd=cwd, full=True,
                              session=payload.get("session_id")):
                commit_offset(transcript_path, offset)
            ensure_drainer(cwd)
            return
        ensure_drainer(cwd)
        # Nothing unread (all prose narrated mid-turn): fall through to the
        # templated completion so the turn still audibly ends.
```

And replace the final dispatch at line 78-80 with:

```python
    text = completion_message(stats)
    if text:
        if get_interaction_level(cwd) == "narrator":
            from voiceover.speech import ensure_drainer, enqueue_speech
            enqueue_speech(text, min_level="concise", cwd=cwd,
                           session=payload.get("session_id"))
            ensure_drainer(cwd)
        else:
            speak(text, min_level="concise", cwd=cwd, interrupt=True)
```

- [ ] **Step 6: Rewrite the narrator branch of `hooks/notification.py`**

Replace lines 67-69 with:

```python
    text = permission_request_message(payload)
    if text:
        if level == "narrator":
            from voiceover.speech import ensure_drainer, enqueue_speech
            enqueue_speech(text, min_level="concise", cwd=cwd,
                           session=payload.get("session_id"))
            ensure_drainer(cwd)
        else:
            speak(text, min_level="concise", cwd=cwd, interrupt=True)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run:
```bash
python3 tests/test_narrator_hooks.py
bash tests/run_all.sh
```
Expected: all 12 checks pass; the whole suite passes.

- [ ] **Step 8: Commit**

```bash
git add hooks/pre_tool_use.py hooks/post_tool_use.py hooks/stop.py hooks/notification.py tests/test_narrator_hooks.py
git commit -m "feat: narrator hooks queue prose instead of racing the audio lock"
```

---

### Task 6: Cut over on a new user message

**Files:**
- Create: `hooks/user_prompt_submit.py`
- Modify: `hooks/hooks.json` — register `UserPromptSubmit`
- Create: `tests/test_cutover.py`

**Interfaces:**
- Consumes: `spool.clear(session=...)` (Task 1); existing `speech.stop_speech()`.
- Produces: the `UserPromptSubmit` hook. No new module-level names.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cutover.py
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_cutover.py`
Expected: FAIL — `hooks/user_prompt_submit.py` does not exist

- [ ] **Step 3: Write the hook**

```python
#!/usr/bin/env python3
"""UserPromptSubmit hook: cut narration over to the new message.

When the user types, the previous turn is over from their point of view.
Anything still queued from it is dropped and playback is stopped, so the
next thing heard is the answer to the question just asked rather than the
tail of the last one. This is the ONLY place narrator mode interrupts, and
it is the user who triggers it - Claude's own narration never cuts itself
off. Never blocks, never prints to stdout, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def main():
    payload = json.load(sys.stdin)

    from voiceover.debuglog import log as _log
    from voiceover.settings import get_interaction_level
    from voiceover.speech import stop_speech
    from voiceover.spool import clear

    cwd = payload.get("cwd")
    if get_interaction_level(cwd) == "silent":
        return

    # Clear BEFORE stopping: the other order leaves a window in which the
    # drainer claims one more item and speaks it after the cut-over.
    dropped = clear(session=payload.get("session_id"))
    stop_speech()
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
```

- [ ] **Step 4: Register the hook in `hooks/hooks.json`**

Add this entry to the `"hooks"` object, alongside the existing five:

```json
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh\" user_prompt_submit",
            "timeout": 10
          }
        ]
      }
    ]
```

Verify it parses: `python3 -c "import json; print(list(json.load(open('hooks/hooks.json'))['hooks'].keys()))"`
Expected: six events including `UserPromptSubmit`.

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
python3 tests/test_cutover.py
bash tests/run_all.sh
```
Expected: all 9 checks pass; the whole suite passes.

- [ ] **Step 6: Commit**

```bash
git add hooks/user_prompt_submit.py hooks/hooks.json tests/test_cutover.py
git commit -m "feat: a new user message clears the queue and cuts narration over"
```

---

### Task 7: Documentation and release

**Files:**
- Modify: `INTERFACES.md`
- Modify: `CHANGELOG.md`
- Modify: `.claude-plugin/plugin.json` — version to `1.3.0`
- Modify: `README.md` — one line in the narrator description
- Add (untracked, commit as-is): `docs/superpowers/specs/2026-09-10-narration-queue-design.md` and `docs/superpowers/plans/2026-09-10-narration-queue.md` — the design and plan this work implements. They are currently untracked; nothing else in the plan commits them.

**Interfaces:**
- Consumes: everything built in Tasks 1-6.
- Produces: no code.

- [ ] **Step 1: Document the queue in `INTERFACES.md`**

Add a section after the `voiceover/speech.py` section:

```markdown
## voiceover/spool.py

The narration queue: one directory, one file per pending utterance.
Hooks WRITE here; engines READ through `tts/engine_common.py`. The two halves
share only the directory path and the filename sort rule.

```python
QUEUE_MAX_AGE_SECONDS = 300                   # backlog cap, pruned on enqueue
def queue_dir() -> Path                       # data_dir()/queue/
def enqueue(text, engine, voice, session=None) -> bool
def clear(session=None) -> int                # all pending items, or one session's
def pending_count() -> int
def prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS) -> int
```

Item filename `<epoch_ms:013d>-<pid>-<seq:02d>.json` — lexicographic order is
play order. Item content `{"text", "engine", "voice", "created", "session"}`.
Writes are a temp file plus `os.rename`, so a reader never sees a partial item.
`*.taken` is an item a reader has claimed; `*.tmp` is a write in flight. Only
`*.json` counts as pending.

## tts/engine_common.py

Stdlib-only sibling imported by BOTH engines (they may not import `voiceover/`).
The single copy of the data-dir and lock helpers, plus the spool reader and the
drain loop.

```python
def data_dir() -> Path
def tts_lock_path() -> Path
def lock_is_live() -> bool
def try_claim_lock(duration: float) -> bool
def update_lock_expiry(duration: float) -> None
def remove_tts_lock() -> None
def queue_dir() -> Path
def take_oldest() -> tuple           # (item, taken_path) | (None, None); atomic claim
def finish(taken_path) -> None       # spoken: discard it
def put_back(taken_path) -> None     # not ours: restore its original name and place
def drain(speak_item, engine_names, lock_seconds=60.0) -> int
```
```

Then update the narrator line in the level-gating list:

```markdown
- narrator: Claude's actual prose via voiceover/prose.py (peek_new_prose/commit_offset,
  byte-offset cursor in data_dir()/prose_state.json); tool chatter silent.
  Prose is QUEUED via speech.enqueue_speech() into voiceover/spool.py and the cursor
  advances on enqueue, so text is never lost to a busy speaker and never re-read.
  ensure_drainer() starts one engine in --drain mode; it holds the lock, loads its
  model once, and speaks the spool back to back. Nothing interrupts except the user's
  own next message (hooks/user_prompt_submit.py), which clears that session's queue
  and stops playback.
```

And add the new hook to the `hooks/` list:

```markdown
- `user_prompt_submit.py` — a new user message ends the previous turn's narration:
  spool.clear(session) then stop_speech(), in that order.
```

- [ ] **Step 2: Add the changelog entry**

Insert at the top of `CHANGELOG.md`, under `# Changelog`:

```markdown
## 1.3.0 - 2026-09-10

### Added
- Narration queue: at the narrator level, prose is queued to a spool
  (data_dir()/queue/) and one engine drains it back to back, loading its TTS
  model once per turn instead of once per sentence. Narration no longer waits
  for the next hook to resume, so the silence between sentences is a breath
  rather than a process spawn.
- New UserPromptSubmit hook: sending a new message clears that session's
  pending narration and stops playback, so the answer to the question you just
  asked is the next thing you hear. Requires one Claude Code restart to
  register.

### Changed
- The transcript cursor now advances when prose is QUEUED, not when audio
  starts. Text can no longer be lost to a busy speaker, re-read from a stale
  cursor, or spoken as a merged blob that begins with an old sentence.
- Narrator mode no longer interrupts itself: permission requests and the
  end-of-turn summary join the queue in order. Only the user's next message
  cuts narration off.
- `tts/kokoro_voice.py` and `tts/macos_say.py` now share one copy of the
  data-dir and lock helpers via `tts/engine_common.py` instead of carrying an
  inline copy each.

### Fixed
- Claude Code's own status notices are never narrated. A usage-limit pause is
  recorded in the transcript as an assistant message with model "<synthetic>",
  so "You've hit your session limit" was read out as if Claude had said it -
  and, having sat unread across the pause, it was read at the FRONT of the next
  turn's narration. Also covers "Not logged in" and "No response requested."

### Added (dev)
- First test suite: stdlib-only scripts under tests/, run with
  `bash tests/run_all.sh`. No framework, matching the hook layer's constraint.
```

- [ ] **Step 3: Bump the version**

In `.claude-plugin/plugin.json`, change `"version": "1.2.0"` to `"version": "1.3.0"`.

- [ ] **Step 4: Run the whole suite one last time**

Run: `bash tests/run_all.sh`
Expected: `ALL TESTS PASSED`, exit 0

- [ ] **Step 5: Verify every hook still exits 0 on a junk payload**

Run:
```bash
for hook in pre_tool_use post_tool_use notification stop subagent_stop user_prompt_submit; do
  echo '{"cwd":"/tmp","transcript_path":"/nonexistent.jsonl","tool_name":"Bash","tool_input":{},"session_id":"S"}' \
    | VOICEOVER_DRY_RUN=1 VOICEOVER_DATA_DIR=/tmp/vo-smoke python3 hooks/$hook.py
  echo "$hook -> exit $?"
done
```
Expected: every hook prints `exit 0` and nothing on stdout.

- [ ] **Step 6: Commit**

```bash
git add INTERFACES.md CHANGELOG.md .claude-plugin/plugin.json README.md docs/
git commit -m "docs: narration queue interfaces, spec, plan and 1.3.0 changelog"
```

Confirm nothing is left untracked afterwards: `git status --short` should print
nothing (`.superpowers/` is gitignored scratch and correctly invisible).

---

## Post-plan verification

Before calling this done, confirm against the spec's own testing list:

1. Spool ordering — `tests/test_spool.py`
2. Concurrent writers — `tests/test_spool.py`
3. Single claim — `tests/test_engine_common.py`
4. Age cap — `tests/test_spool.py` (enqueue prunes) and `tests/test_drain.py` (drainer skips)
5. Cursor commits on enqueue — `tests/test_narrator_hooks.py`
6. End-to-end drain, dry-run — `tests/test_drain.py`
7. Drain-end race — `tests/test_drain.py`
8. Engine mismatch — `tests/test_drain.py`
9. Cut over on new prompt — `tests/test_cutover.py`
10. Regressions: `<synthetic>` filter — `tests/test_narrator_hooks.py`; non-narrator
    levels still use `speak()` with today's interrupt semantics — verified by the
    untouched `speak()` path and the Task 7 Step 5 smoke test

Then, in a live session: restart Claude Code once so `UserPromptSubmit`
registers, set `debug_log` true, and confirm in `data_dir()/voiceover.log` that
`queue` lines show `queued=True` with no `skipped: lock held` on the narrator
path, and that a new prompt logs a `cutover` line.
