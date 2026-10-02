# Low-Latency Narration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut mid-turn narration lag from a median 7.3s / p90 23s to ≤ 2.5s / ≤ 8s by giving every project its own queue, voice and turn-scoped engine, preempting at sentence boundaries, and capturing Claude's text as it streams through a Claude Code mod.

**Architecture:** The Python hook layer keeps all narration policy. A new per-project directory (`data_dir()/projects/<key>/`) holds that project's queue, engine lock, leases and stop requests. The drainer engine plays one sentence at a time, applies the jump-to-newest rule between sentences, and stays loaded only while a session holds a lease. A thin TypeScript mod taps `turn.step` text chunks, cuts them into sentences and hands them to `hooks/mod_bridge.py`; the classic hooks stand down for sessions the mod has marked, and remain the fallback.

**Tech Stack:** Python 3.9+ stdlib (hook layer, `tts/engine_common.py`), kokoro-onnx in the `tts/` uv venv, macOS `say`, TypeScript ES modules against the Claude Code 2.1.287 function-hooks API (`claude-code`, `claude-code/testing`).

**Spec:** `docs/superpowers/specs/2026-10-02-low-latency-narration-design.md` (read it first; it carries the measurements and the user's decisions). It builds on `docs/superpowers/specs/2026-09-10-narration-queue-design.md`.

## Global Constraints

- Branch: `feature/low-latency-narration` (already created, based on `fix/completion-no-parrot`).
- Hook layer (`hooks/*.py`, `voiceover/*.py`) imports only the Python standard library; Python ≥ 3.9.
- Engines (`tts/*.py`) never import `voiceover/`; `tts/engine_common.py` is stdlib only.
- Every hook and `hooks/mod_bridge.py` always exits 0. Hooks print nothing to stdout. The bridge prints exactly one JSON line.
- Never write into a user's project, except the user-run `/voiceover:voice <name> --project` (existing behaviour).
- Tests: the stdlib harness in `tests/_harness.py`, run by `bash tests/run_all.sh`; every test runs under `VOICEOVER_DATA_DIR` (a temp dir) and `VOICEOVER_DRY_RUN=1`; from Task 4 on also `VOICEOVER_NO_SPAWN=1`. Never run anything that plays audio or touches `~/.claude-voiceover`.
- Mod tests: `claude plugin test .` from the repo root; validation: `claude plugin validate .`.
- Constants, verbatim from the spec: `QUEUE_MAX_AGE_SECONDS = 300`; `LEASE_TTL = 600.0`; `POLL_SECONDS = 0.05`; drainer lock expiry 60s, refreshed once half of it has passed; bridge `timeoutMs: 5000`; project key = `sha1(str(resolved project root))[:12]`.
- Target version: 1.4.0.
- macOS `sed -i ''` syntax is used in mechanical edit steps (this machine is macOS).
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

1. **A project with no `.claude` of its own, under a home directory that has `~/.claude`** — it must resolve to its own git root, not to home (today it resolves to home: measured). Pinned by Task 1's tests.
2. **Two engines' items in one project's queue** (the user switched `tts_engine` mid-session) — the jump rule must neither drop nor be blocked by another engine's items. Pinned by Task 3 ("foreign items").
3. **A session id that changes mid-session** (`/clear` starts a new transcript with no `session.start`) — the mod must still mark the new session so the classic hooks stand down instead of narrating twice. Pinned by Task 7 ("prompt marks the session").
4. **A very long message** (2,700 characters, ~140s of speech) — the drainer's 60s lock must stay live through every sentence so no second engine starts beside it. Pinned by Task 3 ("long message keeps the lock").
5. **A prompt submitted while Kokoro is mid-synthesis** — the stop must still land as soon as synthesis returns, not after the sentence plays. Pinned by Task 3 (Kokoro "stop during synthesis").

---

### Task 0: Probe the mod API in the user's own session (throwaway)

This task runs in the MAIN session, not a subagent: it needs the user to answer Claude Code's "Enable hot reloading for this session?" question, and a second turn to read the results. Nothing from it is committed.

**Files:**
- Create (throwaway): `<dev-mods folder>/vo-probe/.claude-plugin/plugin.json`, `<dev-mods folder>/vo-probe/hooks/hooks.json`, `<dev-mods folder>/vo-probe/hooks/register.ts`

- [ ] **Step 1: Load the plugin-authoring skill**

Invoke the `plugin-authoring` skill. Its text names this session's dev-mods folder (`/Users/hanan/.claude/dev-mods/<session id>`) and the types file path. Use them below as `<dev-mods folder>` and `<TYPES>`. Let `<SCRATCH>` be this session's scratchpad directory.

- [ ] **Step 2: Write the probe**

`<dev-mods folder>/vo-probe/.claude-plugin/plugin.json`:

```json
{ "name": "vo-probe", "version": "0.1.0", "description": "Throwaway: measures turn.step text timing for claude-voiceover" }
```

`<dev-mods folder>/vo-probe/hooks/hooks.json`:

```json
{ "modules": ["./register.ts"] }
```

`<dev-mods folder>/vo-probe/hooks/register.ts` (replace `<SCRATCH>` with the absolute scratchpad path):

```ts
import type { Register } from 'claude-code'

const OUT = '<SCRATCH>/vo-probe.jsonl'
const lines: string[] = []
const note = (event: string, data: Record<string, unknown> = {}): void => {
  lines.push(JSON.stringify({ t: Date.now() / 1000, event, ...data }))
}

export const register: Register = on => {
  on('prompt.submit', ($, e, next) => {
    note('prompt.submit')
    // Fire-and-forget: does a $.process.run outlive the hook that started it?
    void $.process
      .run(['/bin/sh', '-c', `date +%s > "${OUT}.fire"`])
      .then(() => note('fire-and-forget resolved'))
      .catch(error => note('fire-and-forget failed', { error: String(error) }))
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    note('step.start', { index: e.index, agentId: e.agentId ?? null })
    const stream = next(e)
    for (let step = await stream.next(); ; step = await stream.next()) {
      if (step.done) {
        note('step.end', { index: e.index, agentId: e.agentId ?? null })
        return step.value
      }
      const chunk = step.value
      if (chunk.kind === 'text') note('text', { index: e.index, block: chunk.index, chars: chunk.text.length })
      if (chunk.kind === 'tool') note('tool', { index: e.index, name: chunk.name })
      yield chunk
    }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    note('turn.complete', {
      reason: e.reason,
      agentId: e.agentId ?? null,
      session: await $.session.id(),
      cwd: await $.session.cwd(),
      root: await $.session.root(),
      pluginRoot: $.plugin.root,
    })
    await $.fs.write(OUT, lines.join('\n') + '\n')
    return result
  })
}
```

- [ ] **Step 3: Validate it**

Run: `claude plugin validate "<dev-mods folder>/vo-probe"`
Expected: `✔ Validation passed` listing hooks `prompt.submit, turn.step, turn.complete`.

- [ ] **Step 4: Ask the user to enable hot reloading, then end the turn**

Tell the user, in one sentence, to answer "Enable for this session" and then send this prompt in the same session:

> Write three short paragraphs about sentence boundaries, then run `ls`, then use a general-purpose subagent to run `pwd`, then say done.

- [ ] **Step 5: Read the results (next turn)**

Run:

```bash
cat "<SCRATCH>/vo-probe.jsonl"; ls -la "<SCRATCH>/vo-probe.jsonl.fire"
```

Then compare with the transcript (`~/.claude/projects/<project>/<session id>.jsonl`): for the paragraphs' text block, find its assistant row timestamp.

Pass criteria — ALL must hold:
1. Several `text` notes for that block, spread over ≥ 0.2s, the first one earlier than the transcript row's timestamp.
2. `vo-probe.jsonl.fire` exists and `fire-and-forget resolved` was noted (a `$.process.run` started and not awaited completes after its hook returned).
3. `session` equals the transcript file's name (without `.jsonl`).
4. The subagent's `step.start` / `turn.complete` notes carry a non-null `agentId`.
5. This ran in the VS Code extension session (the user's main surface).

If 1 or 2 fails, STOP and report to the user: the mod's fire-and-forget bridge design (Task 9) does not hold and needs redesign. If 3–5 fail, report which and stop.

- [ ] **Step 6: Remove the probe**

```bash
rm -rf "<dev-mods folder>/vo-probe" "<SCRATCH>/vo-probe.jsonl" "<SCRATCH>/vo-probe.jsonl.fire"
```

---

### Task 1: Project identity

**Files:**
- Modify: `voiceover/settings.py` (`_search_up_for`, new `_resolved`)
- Create: `voiceover/projects.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `settings.data_dir() -> Path`, `settings.detect_project_root(cwd=None) -> Path`
- Produces:
  - `projects.project_root(cwd=None) -> Path` — resolved project root
  - `projects.project_key(cwd=None) -> str` — 12 lowercase hex chars
  - `projects.project_dir(cwd=None) -> Path` — `data_dir()/projects/<key>/`, created, with `root.txt`
  - `detect_project_root` never returns the home directory because of `~/.claude`

- [ ] **Step 1: Write the failing test**

Create `tests/test_projects.py`:

```python
"""Project identity: one root per working folder, never the home directory."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir

data_dir = isolate()
home = temp_dir("vo-home-")
(home / ".claude").mkdir()              # Claude Code's own config dir
os.environ["HOME"] = str(home)

from voiceover import projects
from voiceover.settings import detect_project_root

git_project = home / "work" / "git-project"
(git_project / ".git").mkdir(parents=True)
(git_project / "src" / "deep").mkdir(parents=True)
claude_project = home / "work" / "claude-project"
(claude_project / ".claude").mkdir(parents=True)
bare = home / "work" / "bare"
bare.mkdir(parents=True)

# --- the home directory's .claude is not a project marker -----------------
check("a git project under home resolves to itself, not to home",
      detect_project_root(str(git_project)) == git_project,
      detect_project_root(str(git_project)))
check("a subdirectory resolves to its project",
      detect_project_root(str(git_project / "src" / "deep")) == git_project,
      detect_project_root(str(git_project / "src" / "deep")))
check("a project with its own .claude still resolves by it",
      detect_project_root(str(claude_project)) == claude_project,
      detect_project_root(str(claude_project)))
check("a folder with no marker resolves to itself, not to home",
      detect_project_root(str(bare)) == bare, detect_project_root(str(bare)))

# --- project_dir: one per root, stable, labelled ---------------------------
a = projects.project_dir(str(git_project))
check("the project dir lives under the data dir",
      a.parent == data_dir / "projects", a)
check("a subdirectory shares its project's dir",
      projects.project_dir(str(git_project / "src")) == a)
check("two projects get two dirs",
      projects.project_dir(str(claude_project)) != a)
key = projects.project_key(str(git_project))
check("the key is 12 hex characters",
      len(key) == 12 and all(c in "0123456789abcdef" for c in key), key)
check("the key is the dir's name", a.name == key, (a.name, key))
check("root.txt names the resolved project root",
      (a / "root.txt").read_text().strip() == str(git_project.resolve()),
      (a / "root.txt").read_text())

report()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_projects.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'voiceover.projects'` (import error before any check).

- [ ] **Step 3: Fix `_search_up_for` in `voiceover/settings.py`**

Replace the existing `_search_up_for` function with:

```python
def _search_up_for(start: Path, marker: str):
    """Walk up from start looking for a directory named marker (depth-limited).

    ~/.claude is Claude Code's own user configuration, not a project marker:
    accepting it makes every project without a .claude of its own resolve to
    the home directory, so they would all share one queue and one voice.
    """
    home = _resolved(Path.home())
    current = start
    for _ in range(10):
        candidate = current / marker
        if candidate.is_dir() and not (marker == ".claude" and _resolved(current) == home):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path
```

- [ ] **Step 4: Create `voiceover/projects.py`**

```python
"""Project identity for the per-project narration state.

Every project - the root settings.detect_project_root() finds, the same root
the voice setting resolves against - gets its own directory under data_dir():
its queue, its engine lock, its leases and its stop requests live there, so a
project's queue and its voice always agree. Engines are handed that directory
and never compute a key themselves. Stdlib only.
"""

import hashlib
from pathlib import Path

from .settings import data_dir, detect_project_root

_PROJECTS_DIR_NAME = "projects"
_ROOT_FILE_NAME = "root.txt"


def project_root(cwd=None) -> Path:
    """The project root for cwd, resolved."""
    root = detect_project_root(cwd)
    try:
        return root.resolve()
    except OSError:
        return root


def project_key(cwd=None) -> str:
    """12 hex characters naming the project's directory."""
    return _key_for(project_root(cwd))


def project_dir(cwd=None) -> Path:
    """data_dir()/projects/<key>/, created on demand, labelled by root.txt."""
    root = project_root(cwd)
    directory = data_dir() / _PROJECTS_DIR_NAME / _key_for(root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        label = directory / _ROOT_FILE_NAME
        if not label.exists():
            label.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        pass
    return directory


def _key_for(root: Path) -> str:
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 tests/test_projects.py`
Expected: `10/10 checks passed in test_projects.py`

- [ ] **Step 6: Run the whole suite**

Run: `bash tests/run_all.sh 2>&1 | tail -3`
Expected: `ALL TESTS PASSED`

- [ ] **Step 7: Commit**

```bash
git add voiceover/settings.py voiceover/projects.py tests/test_projects.py
git commit -m "feat: per-project identity; ~/.claude is no longer a project marker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Per-project spool, end to end

The hook side writes to the project's queue and the engine must read the same one, so both halves move in this task. Behaviour is otherwise unchanged: same drain loop, same rules.

**Files:**
- Modify: `voiceover/spool.py` (whole file below)
- Modify: `tts/engine_common.py` (lock + spool helpers take `root`; `drain_root`)
- Modify: `tts/kokoro_voice.py`, `tts/macos_say.py` (`--drain <dir>`; one-shot paths use `data_dir()`)
- Modify: `voiceover/speech.py` (`project_lock_path`, `lock_is_live(path=None)`, `ensure_drainer`, `_drain_command`, `enqueue_speech` fields)
- Modify: `hooks/user_prompt_submit.py` (pass `cwd` to `clear`)
- Test: `tests/test_spool.py`, `tests/test_engine_common.py`, `tests/test_drain.py`, `tests/test_kokoro_drain.py`, `tests/test_lock_ownership.py`, `tests/test_enqueue_speech.py`, `tests/test_cutover.py`

**Interfaces:**
- Consumes: `projects.project_dir(cwd=None) -> Path` (Task 1)
- Produces:
  - `spool.queue_dir(cwd=None) -> Path` (`project_dir(cwd)/queue`)
  - `spool.enqueue(text, engine, voice, session=None, utterance=None, sticky=False, shown=None, cwd=None) -> bool`; item JSON `{"text","engine","voice","created","session","utterance","sticky","shown"}`; `utterance=None` → a fresh `uuid4().hex`
  - `spool.clear(session=None, cwd=None) -> int`, `spool.pending_count(cwd=None) -> int`, `spool.prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS, cwd=None) -> int`, `spool.remove_legacy_queue() -> int`
  - `engine_common.drain_root(argv) -> Path | None`; `tts_lock_path(root)`, `lock_is_live(root)`, `try_claim_lock(root, duration)`, `update_lock_expiry(root, duration)`, `remove_tts_lock(root)`, `queue_dir(root)`, `take_oldest(root, engine_names=None)`; `drain(root, speak_item, engine_names, lock_seconds=60.0, prepare=None)` (replaced in Task 3)
  - Engines: `kokoro_voice.py --drain <dir>`, `macos_say.py --drain <dir>`
  - `speech.project_lock_path(cwd=None) -> Path`; `speech.lock_is_live(path=None) -> bool` (default: the one-shot lock); `speech.enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None, utterance=None, sticky=False, shown=None) -> bool`; `speech.ensure_drainer(cwd=None) -> bool` spawns `--drain <project_dir(cwd)>`

- [ ] **Step 1: Write the failing spool tests**

In `tests/test_spool.py`: change the import line to
`from _harness import check, isolate, report, REPO_ROOT, temp_dir`
and change the bare `isolate()` call to `data_dir = isolate()`. Then insert before the final `report()`:

```python
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
              cwd=str(project_a))
fields = [json.loads(p.read_text())
          for p in sorted(spool.queue_dir(cwd=str(project_a)).glob("*.json"))]
check("an item with no utterance starts a message of its own",
      fields[0]["utterance"] and fields[0]["utterance"] != fields[1]["utterance"],
      fields[:2])
check("a given utterance, sticky flag and shown time are kept",
      fields[2]["utterance"] == "U1" and fields[2]["sticky"] is True
      and fields[2]["shown"] == 12.5, fields[2])
check("items are not sticky by default", fields[0]["sticky"] is False, fields[0])

# --- the legacy machine-wide queue is removed once -----------------------------
legacy = data_dir / "queue"
legacy.mkdir(exist_ok=True)
(legacy / "0000000000001-1-00.json").write_text("{}")
check("remove_legacy_queue deletes the old items", spool.remove_legacy_queue() == 1)
check("and the old directory", not legacy.exists())
check("a second call has nothing to do", spool.remove_legacy_queue() == 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_spool.py`
Expected: FAIL — `TypeError: enqueue() got an unexpected keyword argument 'cwd'`.

- [ ] **Step 3: Replace `voiceover/spool.py`**

```python
"""The narration queue for claude-voiceover - one per project.

One directory per project (projects.project_dir(cwd)/queue/), one file per
pending utterance. Filenames start with a zero-padded millisecond timestamp,
so listing them in name order lists them in the order they were queued.
Writers create a temp file and rename it into place, so a reader never
observes a half-written item.

Items: {"text", "engine", "voice", "created", "session", "utterance",
"sticky", "shown"}. Items sharing an "utterance" are one message: the engine
plays a message's items in order and lets a newer message preempt it at a
sentence boundary. "sticky" items (dialog alerts) are never skipped.
"shown" is when the text appeared on screen, for latency logging, or None.

This module is the WRITE half, used by the stdlib hook layer. Engines cannot
import voiceover/, so they carry the read half in tts/engine_common.py; the
two sides share nothing but the directory layout and the filename sort rule.

Stdlib only. Every function fails soft: on any OSError it returns a falsy
result and the caller carries on unnarrated rather than breaking a session.
"""

import json
import os
import time
import uuid
from pathlib import Path

from .projects import project_dir
from .settings import data_dir

QUEUE_MAX_AGE_SECONDS = 300  # a five-minute backlog cap

_QUEUE_DIR_NAME = "queue"
_seq = 0


def queue_dir(cwd=None) -> Path:
    """The project's spool directory, created on demand."""
    directory = project_dir(cwd) / _QUEUE_DIR_NAME
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


def enqueue(text, engine, voice, session=None, utterance=None, sticky=False,
            shown=None, cwd=None) -> bool:
    """Append one utterance to the project's spool. True when it was written.

    utterance None makes the item a message of its own. Prunes the backlog
    first, so the age cap needs no timer or daemon.
    """
    try:
        if not text or not str(text).strip():
            return False
        prune(cwd=cwd)
        payload = {
            "text": str(text).strip(),
            "engine": engine,
            "voice": voice,
            "created": time.time(),
            "session": session,
            "utterance": utterance or uuid.uuid4().hex,
            "sticky": bool(sticky),
            "shown": shown,
        }
        directory = queue_dir(cwd)
        name = _item_name()
        temp_path = directory / (name + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        # Rename is atomic: the item appears complete or not at all.
        os.rename(str(temp_path), str(directory / name))
        return True
    except OSError:
        return False


def _items(cwd=None):
    """Pending item paths, oldest first. Only complete items are listed."""
    try:
        return sorted(queue_dir(cwd).glob("*.json"))
    except OSError:
        return []


def pending_count(cwd=None) -> int:
    return len(_items(cwd))


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def clear(session=None, cwd=None) -> int:
    """Drop pending items - all of them, or only one session's. Returns how many."""
    removed = 0
    for path in _items(cwd):
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


def prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS, cwd=None) -> int:
    """Drop items older than the cap, plus leftovers from a killed reader."""
    removed = 0
    cutoff = time.time() - max_age_seconds
    for path in _items(cwd):
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
            for path in queue_dir(cwd).glob(pattern):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        except OSError:
            pass
    return removed


def remove_legacy_queue() -> int:
    """Delete what 1.3's machine-wide data_dir()/queue/ left behind.

    Nothing reads that directory any more, and every item in it was at most
    five minutes from the age cap. Returns how many files were removed.
    """
    legacy = data_dir() / _QUEUE_DIR_NAME
    if not legacy.is_dir():
        return 0
    removed = 0
    for path in list(legacy.iterdir()):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    try:
        legacy.rmdir()
    except OSError:
        pass
    return removed
```

- [ ] **Step 4: Run the spool test**

Run: `python3 tests/test_spool.py`
Expected: all checks pass (the existing checks use the process cwd's project, which is this repo).

- [ ] **Step 5: Thread `root` through `tts/engine_common.py`**

In `tts/engine_common.py`, replace everything from `def tts_lock_path():` down to (but not including) `# --- the drain loop` with the code below, and update the module docstring's second paragraph to say every helper takes `root`, the directory its state lives in: a project's directory for a drainer, `data_dir()` for the one-shot speaker's lock.

```python
def drain_root(argv):
    """The directory given after --drain in argv, or None when it is missing."""
    argv = list(argv)
    try:
        value = argv[argv.index("--drain") + 1]
    except (ValueError, IndexError):
        return None
    if value.startswith("--"):
        return None
    return Path(value)


def tts_lock_path(root):
    return Path(root) / "tts.lock"


def _parse_lock_expiry(raw):
    """Lock content is JSON {"expiry", "pid"}; older locks were a bare float."""
    try:
        return float(json.loads(raw).get("expiry"))
    except Exception:
        try:
            return float(raw)
        except Exception:
            return None


def lock_is_live(root) -> bool:
    """True while an unexpired lock exists. Read-only: never clears anything."""
    try:
        raw = tts_lock_path(root).read_text().strip()
    except OSError:
        return False
    expiry = _parse_lock_expiry(raw)
    return expiry is not None and time.time() < expiry


def try_claim_lock(root, duration):
    """Atomically claim root's TTS lock. True = we own it and may speak.

    O_CREAT|O_EXCL semantics via os.link guarantee that of two engines
    racing, exactly one wins; the loser skips quietly. The lock stores our
    PID so stop_speech() can kill this engine's whole process group.
    """
    lock_file = tts_lock_path(root)
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
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


def update_lock_expiry(root, duration):
    """Refresh our own lock with a new expiry (keeps our PID)."""
    try:
        tts_lock_path(root).write_text(
            json.dumps({"expiry": time.time() + duration, "pid": os.getpid()}))
    except OSError:
        pass


def remove_tts_lock(root):
    try:
        lock_file = tts_lock_path(root)
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


# --- spool reading ----------------------------------------------------------

def queue_dir(root):
    directory = Path(root) / _QUEUE_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory
```

Keep `_peek_engine`, `finish` and `put_back` exactly as they are. In `take_oldest`, change the signature to `def take_oldest(root, engine_names=None):` and its first lookup to `candidates = sorted(queue_dir(root).glob("*.json"))`. In `drain`, change the signature to `def drain(root, speak_item, engine_names, lock_seconds=60.0, prepare=None) -> int:` and pass `root` as the first argument to every `try_claim_lock`, `update_lock_expiry`, `remove_tts_lock` and `take_oldest` call inside it.

- [ ] **Step 6: Point the engines at a root**

In `tts/kokoro_voice.py`:
- change the import to `from engine_common import data_dir, remove_tts_lock, try_claim_lock, update_lock_expiry`;
- give `play_chunk` a third parameter `root` and call `update_lock_expiry(root, duration + 20.0)`;
- give `stream_chunks` a fourth parameter `root` and call `play_chunk(samples, sample_rate, root)`;
- in `play_samples` call `update_lock_expiry(data_dir(), duration)` and `remove_tts_lock(data_dir())`;
- in `_speak_whole_keeping_lock` add a fourth parameter `root` and call `update_lock_expiry(root, duration + 20.0)`;
- in `speak_streaming` call `stream_chunks(kokoro, text, voice, data_dir())` and `remove_tts_lock(data_dir())`;
- in `speak_text` call `try_claim_lock(data_dir(), 10.0 + 0.4 * len(text.split()))` and `remove_tts_lock(data_dir())`;
- in `main()`'s `--drain` branch, first resolve the root and pass it everywhere:

```python
    if "--drain" in sys.argv[1:]:
        from engine_common import drain, drain_root

        root = drain_root(sys.argv)
        if root is None:
            log("Usage: kokoro_voice.py --drain <project dir>")
            return 0
```

then `return drain(root, speak_one, {"kokoro"})` in the dry-run case, `update_lock_expiry(root, 10.0 + 0.4 * len(text.split()))`, `stream_chunks(kokoro, text, voice, root)`, `_speak_whole_keeping_lock(kokoro, text, voice, root)` in `speak_one`, and `return drain(root, speak_one, {"kokoro"}, prepare=prepare)`.

In `tts/macos_say.py`:
- change the import to `from engine_common import data_dir, remove_tts_lock, try_claim_lock, update_lock_expiry`;
- in `speak()` use `try_claim_lock(data_dir(), estimate_duration(text))` and `remove_tts_lock(data_dir())`;
- in `main()`'s `--drain` branch:

```python
    if "--drain" in sys.argv[1:]:
        from engine_common import drain, drain_root

        root = drain_root(sys.argv)
        if root is None:
            print("Usage: macos_say.py --drain <project dir>", file=sys.stderr)
            return 0
```

and inside `speak_one` call `update_lock_expiry(root, estimate_duration(text) + 5.0)`; finish with `return drain(root, speak_one, {"macos-female", "macos-male"})`.

- [ ] **Step 7: Per-project writer side in `voiceover/speech.py`**

Add `from .projects import project_dir` to the imports. Replace `_lock_expiry` and `lock_is_live` with:

```python
def project_lock_path(cwd=None) -> Path:
    """The project's drainer lock. The engine creates and removes it."""
    return project_dir(cwd) / _LOCK_FILE_NAME


def _lock_expiry(path=None):
    """A lock's expiry timestamp, or None when there is no readable lock.

    path defaults to the one-shot speaker's lock. Lock content is JSON
    {"expiry", "pid"}; pre-1.1 locks were a bare float.
    """
    try:
        raw = (path or lock_path()).read_text().strip()
    except OSError:
        return None
    try:
        return float(json.loads(raw).get("expiry"))
    except Exception:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


def lock_is_live(path=None) -> bool:
    """True while an unexpired lock exists. Read-only: never deletes anything.

    ensure_drainer() asks this from a HOOK, about a lock a live engine holds.
    The engine side already established the rule - drain() never removes a
    lock it does not own - and it applies just as much from outside: a hook
    that deletes a working drainer's lock lets a rival start beside it.
    """
    expiry = _lock_expiry(path)
    return expiry is not None and time.time() < expiry
```

Replace `ensure_drainer` with:

```python
def ensure_drainer(cwd=None) -> bool:
    """Start the project's engine draining its spool unless one is already up.

    Called unconditionally by every narrator path, so a spool left behind by
    a crashed engine is picked up by the next hook rather than sitting
    there. Racing callers are harmless: the engine's own atomic lock claim
    admits exactly one drainer per project.
    """
    try:
        spool.remove_legacy_queue()
        if spool.pending_count(cwd) == 0:
            return False
        if lock_is_live(project_lock_path(cwd)):
            return False  # this project's drainer is already working
        engine = resolve_engine(cwd)
        command = _drain_command(engine, project_dir(cwd))
        if command is None:
            return False
        spawned = _spawn_detached(command)
        _log("queue", "drainer spawned=%s engine=%s pending=%d" % (
            spawned, engine, spool.pending_count(cwd)), cwd)
        return spawned
    except Exception:
        return False


def _drain_command(engine, root):
    """The argv that drains root's spool with engine, or None for no engine."""
    if engine == "kokoro":
        return ["uv", "run", "--project", str(_TTS_DIR),
                str(_TTS_DIR / "kokoro_voice.py"), "--drain", str(root)]
    if engine in _MACOS_VOICES:
        return ["python3", str(_TTS_DIR / "macos_say.py"), "--drain", str(root)]
    return None
```

Change `enqueue_speech`'s signature to
`def enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None, utterance=None, sticky=False, shown=None) -> bool:`
and its spool calls to
`queued = spool.enqueue(message, engine, voice, session=session, utterance=utterance, sticky=sticky, shown=shown, cwd=cwd)`
and `spool.pending_count(cwd)` in the log line.

In `hooks/user_prompt_submit.py` change `dropped = clear(session=session)` to `dropped = clear(session=session, cwd=cwd)`.

- [ ] **Step 8: Update the tests that address the engine side**

Run these mechanical edits:

```bash
for f in tests/test_engine_common.py tests/test_drain.py tests/test_kokoro_drain.py; do
  sed -i '' -E \
    -e 's/engine_common\.(queue_dir|lock_is_live|remove_tts_lock|tts_lock_path)\(\)/engine_common.\1(ROOT)/g' \
    -e 's/engine_common\.try_claim_lock\(/engine_common.try_claim_lock(ROOT, /g' \
    -e 's/engine_common\.take_oldest\(\)/engine_common.take_oldest(ROOT)/g' \
    -e 's/engine_common\.take_oldest\(\{/engine_common.take_oldest(ROOT, {/g' \
    -e 's/engine_common\.drain\(/engine_common.drain(ROOT, /g' "$f"
done
```

Then, by hand:
- In each of those three files, right after `import engine_common`, add `ROOT = spool.queue_dir().parent  # this repo's project dir`.
- `tests/test_drain.py` `run_drain`: append `str(ROOT)` to both commands (after `"--drain"`).
- `tests/test_kokoro_drain.py`: set `sys.argv = ["kokoro_voice.py", "--drain", str(ROOT)]`; change the fallback call to `kokoro_voice._speak_whole_keeping_lock(fallback, "fallback text", "af_sarah", ROOT)`; change the last check's lock to `engine_common.tts_lock_path(engine_common.data_dir()).exists() is False`.
- `tests/test_engine_common.py`: append before `report()`:

```python
# --- drain_root ---------------------------------------------------------------
check("drain_root reads the directory after --drain",
      engine_common.drain_root(["x.py", "--drain", "/p/q"]) == Path("/p/q"))
check("drain_root is None without one",
      engine_common.drain_root(["x.py", "--drain"]) is None
      and engine_common.drain_root(["x.py", "--drain", "--voice"]) is None)
```

- `tests/test_lock_ownership.py`: the `ensure_drainer` section must address the PROJECT lock. Replace `lock = speech.lock_path()` and `write_lock` with:

```python
lock = speech.lock_path()                  # the one-shot speaker's lock
project_lock = speech.project_lock_path()  # this project's drainer lock


def write_lock(expiry_offset, pid, path=None):
    (path or lock).write_text(json.dumps({"expiry": time.time() + expiry_offset, "pid": pid}))
```

and in the `ensure_drainer` section use `write_lock(-5, os.getpid(), project_lock)`, `project_lock.exists()`, and `write_lock(300, os.getpid(), project_lock)`; leave the `_is_locked` and `clear_stale_lock` sections on `lock`.

- `tests/test_enqueue_speech.py`, `ensure_drainer` section: replace each `speech.lock_path()` with `speech.project_lock_path()`, and append:

```python
# --- the drainer is started for THIS project ----------------------------------
spawned = []
real_spawn = speech._spawn_detached
speech._spawn_detached = lambda command: spawned.append(command) or True
try:
    spool.clear()
    speech.enqueue_speech("project scoped", full=True)
    speech.ensure_drainer()
finally:
    speech._spawn_detached = real_spawn
check("ensure_drainer drains this project's directory",
      spawned and spawned[0][-2:] == ["--drain", str(spool.queue_dir().parent)], spawned)
```

- `tests/test_cutover.py`: change `def record_clear(session=None):` to `def record_clear(session=None, cwd=None):`.

- [ ] **Step 9: Run the whole suite**

Run: `bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 10: Commit**

```bash
git add voiceover/spool.py voiceover/speech.py hooks/user_prompt_submit.py tts/engine_common.py tts/kokoro_voice.py tts/macos_say.py tests/
git commit -m "feat: one narration queue per project, end to end

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The engine plays sentences: jump rule, stop requests, speakers

**Files:**
- Modify: `tts/engine_common.py` (new drain loop and helpers)
- Modify: `tts/macos_say.py` (`SaySpeaker`, `--drain` wiring)
- Modify: `tts/kokoro_voice.py` (`playback_command`, `KokoroSpeaker`, `--drain` wiring; delete `_speak_whole_keeping_lock`, `_play_samples_keeping_lock`)
- Modify: `voiceover/speech.py` (`request_stop`)
- Test: `tests/test_jump_rule.py` (new), `tests/test_kokoro_drain.py` (rewritten), `tests/test_drain.py` (updated)

**Interfaces:**
- Consumes: Task 2's root-parameterized helpers; `spool.enqueue(..., utterance=, sticky=)`
- Produces:
  - `engine_common.POLL_SECONDS = 0.05`
  - `engine_common.split_sentences(text) -> list[str]`
  - `engine_common.drop_superseded(root, engine_names=None) -> int`, `next_item(root, engine_names=None) -> (item, taken_path)`, `newer_utterance_pending(root, utterance, engine_names=None) -> bool`
  - `engine_common.stop_request(root) -> dict | None` (reads `root/stop.json`)
  - `engine_common.Playback` (`poll() -> bool`, `kill() -> None`), `ProcessPlayback(process, cleanup=None)`, `DryRunSpeaker` (`begin(sentence, voice, upcoming=None) -> Playback`)
  - `engine_common.engine_log(tag, message) -> None` (same line format as `voiceover/debuglog.py`, gated on the global `debug_log` setting)
  - `engine_common.drain(root, open_speaker, engine_names, lock_seconds=60.0, poll_seconds=POLL_SECONDS) -> int` — `open_speaker()` runs after the lock is claimed and returns a speaker or a falsy value
  - `speech.request_stop(cwd=None, session=None) -> None` writes `project_dir(cwd)/stop.json` `{"ts": float, "session": str | None}`
  - `macos_say.SaySpeaker`, `kokoro_voice.playback_command(path) -> list`, `kokoro_voice.KokoroSpeaker(kokoro, play_dir)`

- [ ] **Step 1: Write the failing jump-rule test**

Create `tests/test_jump_rule.py`:

```python
"""The engine loop: sentences, the jump rule, sticky alerts, stop requests."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool, speech

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common

ROOT = spool.queue_dir().parent
OWN = {"kokoro"}


class Recorder:
    """Records sentences; each plays for `polls` polls. on_begin(sentence)
    may act mid-drain: enqueue a newer message, request a stop."""

    def __init__(self, polls=0, on_begin=None):
        self.spoken, self.killed = [], []
        self._polls, self._on_begin = polls, on_begin

    def begin(self, sentence, voice, upcoming=None):
        self.spoken.append(sentence)
        if self._on_begin:
            self._on_begin(sentence)
        recorder, remaining = self, [self._polls]

        class Play(engine_common.Playback):
            def poll(self):
                remaining[0] -= 1
                return remaining[0] < 0

            def kill(self):
                recorder.killed.append(sentence)

        return Play()


def run(speaker, **kwargs):
    kwargs.setdefault("poll_seconds", 0.001)
    return engine_common.drain(ROOT, lambda: speaker, OWN, **kwargs)


def put(text, utterance=None, session="S1", sticky=False, engine="kokoro"):
    spool.enqueue(text, engine, "bf_emma", session=session, utterance=utterance, sticky=sticky)
    time.sleep(0.002)


# --- sentence splitting -------------------------------------------------------
check("split after . ! ? and at newlines",
      engine_common.split_sentences("One. Two! Three?\nFour") == ["One.", "Two!", "Three?", "Four"],
      engine_common.split_sentences("One. Two! Three?\nFour"))
check("no split inside a version number",
      engine_common.split_sentences("Version 1.4.0 ships.") == ["Version 1.4.0 ships."])
check("blank text has no sentences", engine_common.split_sentences("  ") == [])

# --- one message plays in order, sentence by sentence -------------------------
spool.clear()
put("First. Second.", utterance="U1")
put("Third.", utterance="U1")
speaker = Recorder()
check("drain exits 0", run(speaker) == 0)
check("a message's items play in order, sentence by sentence",
      speaker.spoken == ["First.", "Second.", "Third."], speaker.spoken)

# --- a newer message waiting at the start skips everything older ---------------
spool.clear()
put("Old one.", utterance="A")
put("Old two.", utterance="B")
put("Newest.", utterance="C")
speaker = Recorder()
run(speaker)
check("only the newest message is spoken", speaker.spoken == ["Newest."], speaker.spoken)
check("the skipped items are gone", spool.pending_count() == 0, spool.pending_count())

# --- a newer message arriving mid-item preempts at the sentence boundary -------
spool.clear()
put("One. Two. Three.", utterance="A")


def news_arrives(sentence):
    if sentence == "One.":
        put("Fresh news.", utterance="B")


speaker = Recorder(polls=3, on_begin=news_arrives)
run(speaker)
check("the current sentence finishes, the rest of its message is dropped",
      speaker.spoken == ["One.", "Fresh news."], speaker.spoken)
check("nothing was cut mid-sentence", speaker.killed == [], speaker.killed)

# --- sticky alerts are never skipped -------------------------------------------
spool.clear()
put("Lead-in.", utterance="A")
put("I need your input.", utterance="D", sticky=True)
put("Later text.", utterance="E")
speaker = Recorder()
run(speaker)
check("a sticky alert plays before the newest message",
      speaker.spoken == ["I need your input.", "Later text."], speaker.spoken)

# --- two sessions in one project: newest wins ----------------------------------
spool.clear()
put("From session one.", utterance="A", session="S1")
put("From session two.", utterance="B", session="S2")
speaker = Recorder()
run(speaker)
check("the newest message wins across sessions",
      speaker.spoken == ["From session two."], speaker.spoken)

# --- foreign items are neither dropped nor in the way (Review Focus 2) ----------
spool.clear()
put("Theirs.", utterance="X", engine="macos-female")
put("Mine old.", utterance="A")
put("Mine new.", utterance="B")
speaker = Recorder()
run(speaker)
check("own items obey the jump rule", speaker.spoken == ["Mine new."], speaker.spoken)
check("a foreign item keeps its place", spool.pending_count() == 1, spool.pending_count())
spool.clear()

# --- a stop request for the item's session kills the sentence ------------------
put("Long one. Never heard.", utterance="A", session="S1")


def stop_s1(sentence):
    if sentence == "Long one.":
        speech.request_stop(None, "S1")


speaker = Recorder(polls=50, on_begin=stop_s1)
run(speaker)
check("the playing sentence is killed", speaker.killed == ["Long one."], speaker.killed)
check("the rest of the item is abandoned", speaker.spoken == ["Long one."], speaker.spoken)

# --- ... one for another session does not -----------------------------------
spool.clear()
put("Keep going. To the end.", utterance="A", session="S1")


def stop_s2(sentence):
    if sentence == "Keep going.":
        speech.request_stop(None, "S2")


speaker = Recorder(polls=5, on_begin=stop_s2)
run(speaker)
check("another session's stop leaves this item alone",
      speaker.spoken == ["Keep going.", "To the end."] and speaker.killed == [],
      (speaker.spoken, speaker.killed))

# --- a stop request older than the engine is ignored ---------------------------
spool.clear()
speech.request_stop(None, "S1")
time.sleep(0.01)
put("Spoken despite an old stop.", utterance="A", session="S1")
speaker = Recorder(polls=3)
run(speaker)
check("an old stop request is ignored",
      speaker.spoken == ["Spoken despite an old stop."], speaker.spoken)

# --- a long message keeps the lock live (Review Focus 4) -----------------------
spool.clear()
put("A. B. C. D.", utterance="A")
alive = []
speaker = Recorder(polls=40, on_begin=lambda s: alive.append(engine_common.lock_is_live(ROOT)))
run(speaker, lock_seconds=0.2, poll_seconds=0.005)
check("the lock stays live through playback longer than its expiry",
      alive == [True, True, True, True], alive)

# --- request_stop writes the contract file -------------------------------------
speech.request_stop(None, "S9")
stop = json.loads((ROOT / "stop.json").read_text())
check("request_stop records the session and a fresh timestamp",
      stop["session"] == "S9" and time.time() - stop["ts"] < 5, stop)

report()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_jump_rule.py`
Expected: FAIL — `AttributeError: module 'engine_common' has no attribute 'split_sentences'`.

- [ ] **Step 3: Write the new engine loop in `tts/engine_common.py`**

Add `import re` and `import sys` to the imports, and add after `_TAKEN_SUFFIX = ".taken"`:

```python
_STOP_FILE_NAME = "stop.json"
POLL_SECONDS = 0.05
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_LOG_MAX_BYTES = 200_000
_debug = None
```

Add this section after `put_back` (before `# --- the drain loop`):

```python
# --- logging ----------------------------------------------------------------

def _debug_enabled():
    """The global debug_log setting, read once per engine process."""
    global _debug
    if _debug is None:
        try:
            with open(data_dir() / "settings.json", encoding="utf-8") as handle:
                _debug = bool(json.load(handle).get("debug_log"))
        except Exception:
            _debug = False
    return _debug


def engine_log(tag, message):
    """Append to data_dir()/voiceover.log in voiceover/debuglog.py's line
    format, when the global debug_log setting is on. Never raises."""
    try:
        if not _debug_enabled():
            return
        path = data_dir() / "voiceover.log"
        try:
            if path.stat().st_size > _LOG_MAX_BYTES:
                path.unlink()
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%.2f %d %-10s %s\n" % (time.time(), os.getpid(), tag, message))
    except Exception:
        pass


# --- sentences, the jump rule, stop requests --------------------------------

def split_sentences(text):
    """Text -> its sentences: split after . ! ? + whitespace and at newlines."""
    return [piece.strip() for piece in _SENTENCE_BREAK.split(text or "")
            if piece and piece.strip()]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _owned(item, engine_names):
    """True unless the item names a real engine outside engine_names."""
    engine = item.get("engine")
    return engine_names is None or not engine or engine in engine_names


def drop_superseded(root, engine_names=None) -> int:
    """The jump rule: finish unspoken every own item a newer message supersedes.

    The newest pending own item's utterance is the message to speak; every
    older own item of a different utterance is dropped unless it is sticky
    (a dialog alert). Foreign items are not this engine's to judge and keep
    their place. Returns how many items were dropped.
    """
    own = []
    for path in sorted(queue_dir(root).glob("*.json")):
        item = _read_json(path)
        if item is not None and _owned(item, engine_names):
            own.append((path, item))
    if len(own) < 2:
        return 0
    newest = own[-1][1].get("utterance")
    dropped = 0
    for path, item in own[:-1]:
        if item.get("sticky") or (newest is not None and item.get("utterance") == newest):
            continue
        try:
            path.unlink()
            dropped += 1
        except OSError:
            pass  # cleared or claimed meanwhile
    return dropped


def next_item(root, engine_names=None):
    """Apply the jump rule, then claim the oldest remaining own item."""
    dropped = drop_superseded(root, engine_names)
    if dropped:
        engine_log("jump", "dropped=%d superseded items" % dropped)
    return take_oldest(root, engine_names)


def newer_utterance_pending(root, utterance, engine_names=None) -> bool:
    """True when an own pending item belongs to a different message."""
    for path in queue_dir(root).glob("*.json"):
        item = _read_json(path)
        if item is None or not _owned(item, engine_names):
            continue
        if utterance is None or item.get("utterance") != utterance:
            return True
    return False


def stop_request(root):
    """The project's latest stop request {"ts", "session"}, or None."""
    return _read_json(Path(root) / _STOP_FILE_NAME)


# --- playback ---------------------------------------------------------------

class Playback:
    """A sentence that is already over - what a dry run plays."""

    def poll(self) -> bool:
        return True

    def kill(self) -> None:
        pass


class ProcessPlayback(Playback):
    """A sentence playing in a child process; cleanup is a file to delete."""

    def __init__(self, process, cleanup=None):
        self._process = process
        self._cleanup = cleanup

    def poll(self) -> bool:
        if self._process.poll() is None:
            return False
        self._remove()
        return True

    def kill(self) -> None:
        try:
            self._process.terminate()
            self._process.wait(timeout=1)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
        self._remove()

    def _remove(self):
        if self._cleanup is not None:
            try:
                Path(self._cleanup).unlink()
            except OSError:
                pass
            self._cleanup = None


class DryRunSpeaker:
    """Prints each sentence as '[voiceover] <sentence>' instead of playing it."""

    def begin(self, sentence, voice, upcoming=None):
        print("[voiceover] " + sentence, file=sys.stderr, flush=True)
        return Playback()


class _LockKeeper:
    """Refreshes the drainer's lock whenever half its expiry has passed."""

    def __init__(self, root, seconds):
        self._root, self._seconds = root, seconds
        self._last = time.time()

    def keep(self):
        now = time.time()
        if now - self._last >= self._seconds / 2:
            update_lock_expiry(self._root, self._seconds)
            self._last = now

    def reset(self):
        self._last = time.time()


class _StopWatch:
    """Reports each stop request newer than the last one seen, once.

    Requests older than this engine belong to an earlier engine's turn.
    """

    def __init__(self, root):
        self._root = root
        self._seen = time.time()

    def poll(self):
        """(True, session named) for a new request, else (False, None)."""
        request = stop_request(self._root)
        try:
            ts = float(request.get("ts")) if request else 0.0
        except (TypeError, ValueError):
            return False, None
        if ts <= self._seen:
            return False, None
        self._seen = ts
        return True, request.get("session")


def _stops(watch, session):
    """True when a new stop request names this session, or names none."""
    fresh, named = watch.poll()
    return fresh and (named is None or named == session)


def _log_first_sound(item):
    now = time.time()
    shown = item.get("shown")
    text = item.get("text") or ""
    engine_log("sound", "lag_created=%.2f lag_shown=%s chars=%d :: %.60s" % (
        now - float(item.get("created") or now),
        ("%.2f" % (now - float(shown))) if isinstance(shown, (int, float)) else "-",
        len(text), text.replace("\n", " ")))
```

Replace the whole `drain` function (and its docstring) with:

```python
def _speak_item(root, speaker, item, engine_names, keeper, watch, poll_seconds):
    """Play item sentence by sentence until it ends, is stopped or superseded."""
    session, utterance = item.get("session"), item.get("utterance")
    sentences = split_sentences(item.get("text"))
    for index, sentence in enumerate(sentences):
        upcoming = sentences[index + 1] if index + 1 < len(sentences) else None
        playback = speaker.begin(sentence, item.get("voice"), upcoming)
        if index == 0:
            _log_first_sound(item)
        while not playback.poll():
            keeper.keep()
            if _stops(watch, session):
                playback.kill()
                engine_log("stop", "session=%s" % session)
                return
            time.sleep(poll_seconds)
        keeper.keep()
        if _stops(watch, session):
            return
        if newer_utterance_pending(root, utterance, engine_names):
            return


def drain(root, open_speaker, engine_names, lock_seconds=60.0,
          poll_seconds=POLL_SECONDS) -> int:
    """Speak root's spool one sentence at a time. Returns a process exit code.

    open_speaker() -> speaker | falsy is the engine's one-time setup (loading
    a speech model). It runs only AFTER the lock is ours: opening first would
    leave the lock free for the whole spin-up, and every hook firing in that
    window would spawn another engine that loads its model and then exits on
    the lock. A falsy speaker releases the lock and exits 0. A speaker has
    begin(sentence, voice, upcoming) -> Playback.

    Between sentences the jump rule applies (next_item, newer_utterance_
    pending): a different message ends the current one at its next sentence
    boundary and everything queued between is dropped, sticky alerts excepted.
    A stop request naming the item's session kills the sentence mid-word -
    only the user's own new prompt sends one.

    When the spool runs dry the lock is released and the spool checked ONCE
    more before exiting: that closes the race against a hook that appended an
    item a moment earlier and saw the lock still held. A lock this drain does
    not own is never deleted.
    """
    root = Path(root)
    if not try_claim_lock(root, lock_seconds):
        return 0  # another engine is already draining this project
    owns_lock = True
    keeper = _LockKeeper(root, lock_seconds)
    watch = _StopWatch(root)
    try:
        speaker = open_speaker()
        if not speaker:
            return 0  # setup failed; the finally below hands the lock back
        while True:
            watch.poll()  # nothing is playing: the hook already dropped the items
            item, taken_path = next_item(root, engine_names)
            if item is None:
                remove_tts_lock(root)
                owns_lock = False
                item, taken_path = next_item(root, engine_names)
                if item is None:
                    return 0
                if not try_claim_lock(root, lock_seconds):
                    # Another engine claimed the lock in the gap - it will
                    # drain this item. Never delete a lock we do not own.
                    put_back(taken_path)
                    return 0
                owns_lock = True
                keeper.reset()
                # Fall through and speak the item just claimed: it is renamed
                # to .taken, invisible to a re-scan, and would be lost.
            age = time.time() - float(item.get("created") or 0)
            if age > QUEUE_MAX_AGE_SECONDS:
                finish(taken_path)     # too stale to be worth hearing
                continue
            keeper.keep()
            try:
                _speak_item(root, speaker, item, engine_names, keeper, watch, poll_seconds)
            except Exception:
                pass  # a failed sentence skips the rest of its item
            finish(taken_path)
    finally:
        if owns_lock:
            remove_tts_lock(root)
```

Also replace the module docstring's description of the drain loop (it describes 1.3's item-at-a-time loop) with one paragraph: the drainer plays sentences, applies the jump rule between them, honours stop requests, and claims its lock before `open_speaker()` loads anything.

- [ ] **Step 4: `request_stop` in `voiceover/speech.py`**

Add after `ensure_drainer`:

```python
def request_stop(cwd=None, session=None) -> None:
    """Ask the project's engine to stop the sentence it is playing now.

    The engine stops it only when it belongs to `session` (None: any), and
    ignores requests older than itself. Written atomically: a temp file
    renamed over project_dir(cwd)/stop.json, {"ts", "session"}.
    """
    try:
        path = project_dir(cwd) / "stop.json"
        temp = path.with_name("stop.json.%d.tmp" % os.getpid())
        temp.write_text(json.dumps({"ts": time.time(), "session": session}))
        os.replace(str(temp), str(path))
    except OSError:
        pass
```

- [ ] **Step 5: The macOS speaker**

In `tts/macos_say.py` add `from engine_common import ProcessPlayback` beside the other import and add:

```python
class SaySpeaker:
    """Plays each sentence with `say --voice=<Name>`, the text on stdin.

    The --voice= spelling is deliberate: the one-shot interrupt matches
    `say -v <Name>`, and must never reach a drainer's sentence.
    """

    def begin(self, sentence, voice, upcoming=None):
        process = subprocess.Popen(["say", "--voice=" + (voice or DEFAULT_VOICE)],
                                   stdin=subprocess.PIPE, text=True)
        try:
            process.stdin.write(sentence)
            process.stdin.close()
        except OSError:
            pass
        return ProcessPlayback(process)
```

Replace `main()`'s `--drain` branch with:

```python
    if "--drain" in sys.argv[1:]:
        from engine_common import DryRunSpeaker, drain, drain_root

        root = drain_root(sys.argv)
        if root is None:
            print("Usage: macos_say.py --drain <project dir>", file=sys.stderr)
            return 0
        engines = {"macos-female", "macos-male"}
        if DRY_RUN:
            return drain(root, DryRunSpeaker, engines)
        if sys.platform != "darwin":
            return 0
        return drain(root, SaySpeaker, engines)
```

- [ ] **Step 6: The Kokoro speaker**

In `tts/kokoro_voice.py`: add `import concurrent.futures` and `import shutil`; change the engine_common import to
`from engine_common import ProcessPlayback, data_dir, remove_tts_lock, try_claim_lock, update_lock_expiry`;
delete `_play_samples_keeping_lock` and `_speak_whole_keeping_lock`; in `play_chunk` and `play_samples` create the temp file with `tempfile.NamedTemporaryFile(prefix="voiceover-oneshot-", suffix=".wav", delete=False)`; add:

```python
def playback_command(path):
    """The player for one sentence's wav, as an argv Popen can poll and kill."""
    if sys.platform == "darwin":
        return ["afplay", path]
    if sys.platform == "win32":
        escaped = path.replace("'", "''")
        return ["powershell", "-NoProfile", "-Command",
                "(New-Object Media.SoundPlayer '" + escaped + "').PlaySync()"]
    if shutil.which("aplay"):
        return ["aplay", "-q", path]
    return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]


class KokoroSpeaker:
    """Plays sentences through a loaded Kokoro model, one synthesized ahead.

    begin() waits for its sentence's samples (already rendering when it was
    the previous call's `upcoming`), starts rendering the next one in a
    worker thread - onnxruntime releases the GIL, so it overlaps playback -
    and plays the wav from the project's play/ directory through a child
    process the drain loop polls and can kill.
    """

    def __init__(self, kokoro, play_dir):
        self._kokoro = kokoro
        self._play_dir = Path(play_dir)
        self._play_dir.mkdir(parents=True, exist_ok=True)
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._ahead = {}
        self._count = 0

    def begin(self, sentence, voice, upcoming=None):
        import soundfile as sf

        voice = voice or DEFAULT_VOICE
        future = self._ahead.pop((sentence, voice), None) or self._render(sentence, voice)
        self._ahead.clear()  # anything else rendered ahead belongs to an abandoned item
        samples, sample_rate = future.result()
        if upcoming:
            self._ahead[(upcoming, voice)] = self._render(upcoming, voice)
        self._count += 1
        path = self._play_dir / ("%d-%d.wav" % (os.getpid(), self._count))
        sf.write(str(path), samples, sample_rate)
        process = subprocess.Popen(playback_command(str(path)),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ProcessPlayback(process, cleanup=path)

    def _render(self, sentence, voice):
        settings = get_voice_settings(voice)
        return self._worker.submit(
            self._kokoro.create, text=sentence, voice=voice, speed=settings["speed"],
            lang=settings["lang"], trim=settings["trim"])
```

Replace `main()`'s `--drain` branch with:

```python
    if "--drain" in sys.argv[1:]:
        from engine_common import DryRunSpeaker, drain, drain_root

        root = drain_root(sys.argv)
        if root is None:
            log("Usage: kokoro_voice.py --drain <project dir>")
            return 0
        if DRY_RUN:
            return drain(root, DryRunSpeaker, {"kokoro"})

        def open_speaker():
            """Load the model ONCE, and only after the lock is ours."""
            if not ensure_models():
                return None
            try:
                from kokoro_onnx import Kokoro

                return KokoroSpeaker(Kokoro(str(MODEL_FILE), str(VOICES_FILE)), root / "play")
            except Exception as error:
                log("[ERROR] Model load failed: " + str(error))
                return None

        return drain(root, open_speaker, {"kokoro"})
```

- [ ] **Step 7: Rewrite `tests/test_kokoro_drain.py`**

```python
"""The kokoro drainer: sentence by sentence, one sentence synthesized ahead.

Kokoro's own dependencies (kokoro-onnx, numpy, soundfile) live in the tts/
venv and are not importable from the stdlib-only suite, so they are stubbed:
what is under test is OUR wiring - per-sentence synthesis, the prefetch, the
lock claimed before the model loads, and stops that land during synthesis.
"""

import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool, speech

_numpy = types.ModuleType("numpy")
_numpy.ndarray = list
_soundfile = types.ModuleType("soundfile")
_soundfile.write = lambda path, samples, rate: Path(path).write_bytes(b"RIFF")
sys.modules.setdefault("numpy", _numpy)
sys.modules.setdefault("soundfile", _soundfile)

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common
import kokoro_voice

ROOT = spool.queue_dir().parent
PLAY = ROOT / "play"


class FakeKokoro:
    def __init__(self, delay=0.0):
        self.created, self._delay = [], delay

    def create(self, text, voice, speed, lang, trim):
        time.sleep(self._delay)
        self.created.append(text)
        return ([0.0] * 2400, 24000)

    def create_stream(self, text, voice, speed, lang, trim):
        async def chunks():
            yield ([0.0] * 2400, 24000)
        return chunks()


kokoro_voice.ensure_models = lambda: True
kokoro_voice.DRY_RUN = False
kokoro_voice.play_audio_file = lambda path, timeout=30: True
kokoro_voice.playback_command = lambda path: [sys.executable, "-c", "pass"]
fake = FakeKokoro()
load_saw_lock = []


def _construct(*args, **kwargs):
    load_saw_lock.append(engine_common.lock_is_live(ROOT))
    return fake


sys.modules["kokoro_onnx"] = types.ModuleType("kokoro_onnx")
sys.modules["kokoro_onnx"].Kokoro = _construct

# --- drain speaks sentence by sentence -----------------------------------------
spool.clear()
spool.enqueue("One. Two. Three.", "kokoro", "af_sarah", utterance="U")
argv = sys.argv
sys.argv = ["kokoro_voice.py", "--drain", str(ROOT)]
try:
    code = kokoro_voice.main()
finally:
    sys.argv = argv
check("the kokoro drain exits 0", code == 0, code)
check("each sentence is synthesized once, in order",
      fake.created == ["One.", "Two.", "Three."], fake.created)
check("the model is loaded exactly once", len(load_saw_lock) == 1, load_saw_lock)
check("the lock is claimed BEFORE the model is loaded", load_saw_lock == [True], load_saw_lock)
check("the lock is released when the drain ends", engine_common.lock_is_live(ROOT) is False)
check("no wav file is left behind", list(PLAY.glob("*.wav")) == [], list(PLAY.glob("*.wav")))

# --- the next sentence is synthesized while this one plays ---------------------
speaker = kokoro_voice.KokoroSpeaker(FakeKokoro(), PLAY)
playback = speaker.begin("Alpha.", "af_sarah", "Beta.")
check("begin starts synthesizing the upcoming sentence", ("Beta.", "af_sarah") in speaker._ahead)
while not playback.poll():
    time.sleep(0.01)
speaker.begin("Beta.", "af_sarah", None)
check("the prefetched sentence is not synthesized twice",
      speaker._kokoro.created == ["Alpha.", "Beta."], speaker._kokoro.created)
speaker.begin("Gamma.", "af_sarah", "Delta.")
speaker.begin("Other.", "af_sarah", None)
check("work rendered ahead for an abandoned sentence is discarded",
      speaker._ahead == {}, speaker._ahead)

# --- a stop that lands during synthesis still stops promptly (Review Focus 5) ---
spool.clear()
spool.enqueue("Slow one. Never played.", "kokoro", "af_sarah", session="S1", utterance="U")
kokoro_voice.playback_command = lambda path: [sys.executable, "-c", "import time; time.sleep(5)"]
slow = FakeKokoro(delay=0.3)
threading.Timer(0.1, lambda: speech.request_stop(None, "S1")).start()
started = time.time()
engine_common.drain(ROOT, lambda: kokoro_voice.KokoroSpeaker(slow, PLAY), {"kokoro"})
check("the 5-second sentence was killed right after synthesis",
      time.time() - started < 2.0, time.time() - started)
check("its item is gone", spool.pending_count() == 0)

# --- the one-shot path still owns and releases its own lock --------------------
kokoro_voice.speak_streaming(FakeKokoro(), "one shot", "af_sarah")
check("speak_streaming still releases the one-shot lock",
      engine_common.tts_lock_path(engine_common.data_dir()).exists() is False)

report()
```

- [ ] **Step 8: Update `tests/test_drain.py` for the new loop**

- Every in-process `engine_common.drain(ROOT, <lambda item ...>, {...})` becomes `engine_common.drain(ROOT, lambda: Collect(spoken_items), {...})` (or `lambda: Collect([])` where nothing is recorded), with this helper added after `run_drain`:

```python
class Collect:
    """A speaker that records each sentence and finishes it at once."""

    def __init__(self, sink):
        self.sink = sink

    def begin(self, sentence, voice, upcoming=None):
        self.sink.append(sentence)
        return engine_common.Playback()
```

- Items that the old test expects to ALL be spoken must now be one message: give `utterance="U1"` to the three items of "drains the whole spool, in order", and `utterance="U2"` to "mine to speak" / "mine as well" in the foreign-item section.
- Append before `report()`:

```python
# --- separate messages queued together: only the newest is heard ---------------
spool.clear()
for word in ("stale one", "stale two", "the newest"):
    spool.enqueue(word, "macos-female", "Samantha")
    time.sleep(0.002)
stderr, code = run_drain("macos")
spoken = [line.split("[voiceover] ", 1)[1].strip()
          for line in stderr.splitlines() if "[voiceover] " in line]
check("the jump rule holds end to end", spoken == ["the newest"], spoken)
```

- [ ] **Step 9: Run the whole suite**

Run: `bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 10: Commit**

```bash
git add tts/ voiceover/speech.py tests/
git commit -m "feat: the engine plays sentences and jumps to the newest message

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Leases and the turn-scoped engine

**Files:**
- Create: `voiceover/lease.py`
- Modify: `tts/engine_common.py` (`lease_is_live`; `drain`'s empty branch)
- Modify: `voiceover/speech.py` (`ensure_drainer` lease condition, `prewarm`, `_spawn_detached` honours `VOICEOVER_NO_SPAWN`)
- Modify: `tests/_harness.py` (sets `VOICEOVER_NO_SPAWN=1`)
- Test: `tests/test_engine_lifecycle.py` (new)

**Interfaces:**
- Consumes: `projects.project_dir`, Task 3's `drain`, `DryRunSpeaker`
- Produces:
  - `lease.LEASE_TTL = 600.0`; `lease.lease_dir(cwd=None) -> Path`; `lease.hold(cwd, session, ttl=LEASE_TTL) -> bool`; `lease.release(cwd, session) -> None`; `lease.any_live(cwd=None) -> bool`. File contract: `<project dir>/leases/<session, [A-Za-z0-9_-] only>.json` = `{"expiry": float}`
  - `engine_common.lease_is_live(root) -> bool` (deletes expired lease files)
  - `speech.prewarm(cwd=None, session=None) -> bool`
  - `drain` waits (polling every `poll_seconds`) while a lease is live and the queue is empty; after releasing the lock it re-claims if a lease appeared in the gap

- [ ] **Step 1: Write the failing lifecycle test**

Create `tests/test_engine_lifecycle.py`:

```python
"""The turn-scoped engine: warm while a session holds it, gone once released."""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
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
check("the engine deletes expired leases", list((ROOT / "leases").glob("*.json")) == [])
lease.hold(None, "../../escape")
check("a hostile session id stays inside the lease dir",
      all(p.parent == ROOT / "leases" for p in (ROOT / "leases").glob("*.json")))
lease.release(None, "../../escape")
lease.hold(None, "S1")
lease.hold(None, "S2")
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

# --- ensure_drainer pre-warms on a lease alone -----------------------------------
set_setting("interaction_level", "narrator")
set_setting("tts_engine", "macos-female")
spool.clear()
check("no queue, no lease: nothing to start", speech.ensure_drainer() is False)
check("prewarm holds a lease and starts the engine", speech.prewarm(None, "S1") is True)
check("the lease is there", lease.any_live() is True)
lease.release(None, "S1")

report()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_engine_lifecycle.py`
Expected: FAIL — `ImportError: cannot import name 'lease' from 'voiceover'`.

- [ ] **Step 3: Create `voiceover/lease.py`**

```python
"""Per-project engine leases: a session holds its project's engine while a
turn runs, so the model stays loaded for the next sentence and unloads once
nobody needs it.

Hooks and the mod bridge WRITE leases; the engine READS them through
tts/engine_common.py (lease_is_live). The shared contract is only this:
<project dir>/leases/<session>.json holding {"expiry": unix seconds}. The
expiry is a crash backstop, not a timer: holders renew at every model step or
tool hook and release at turn end. Stdlib only; every function fails soft.
"""

import json
import os
import re
import time
from pathlib import Path

from .projects import project_dir

LEASE_TTL = 600.0

_LEASE_DIR_NAME = "leases"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def lease_dir(cwd=None) -> Path:
    """The project's lease directory, created on demand."""
    directory = project_dir(cwd) / _LEASE_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def _lease_path(cwd, session):
    name = _UNSAFE.sub("_", str(session or ""))
    return lease_dir(cwd) / (name + ".json") if name else None


def hold(cwd, session, ttl=LEASE_TTL) -> bool:
    """Hold (or renew) session's lease on the project's engine."""
    path = _lease_path(cwd, session)
    if path is None:
        return False
    try:
        temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
        temp.write_text(json.dumps({"expiry": time.time() + ttl}))
        os.replace(str(temp), str(path))
        return True
    except OSError:
        return False


def release(cwd, session) -> None:
    """Let go of session's lease. The engine exits once none are left and
    its queue is empty."""
    path = _lease_path(cwd, session)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def any_live(cwd=None) -> bool:
    """True while any session holds an unexpired lease in the project."""
    now = time.time()
    for path in lease_dir(cwd).glob("*.json"):
        try:
            if float(json.loads(path.read_text()).get("expiry")) > now:
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return False
```

- [ ] **Step 4: Engine side — `lease_is_live` and the waiting drain**

In `tts/engine_common.py` add `_LEASE_DIR_NAME = "leases"` beside the other names, and after `stop_request`:

```python
def lease_is_live(root) -> bool:
    """True while a session holds an unexpired lease on root's engine.

    Expired lease files are deleted on the way: a crashed holder's lease must
    not keep a model loaded."""
    now = time.time()
    live = False
    try:
        paths = list((Path(root) / _LEASE_DIR_NAME).glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            expiry = float(json.loads(path.read_text()).get("expiry"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if expiry > now:
            live = True
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return live
```

In `drain`, replace the `if item is None:` block with:

```python
            if item is None:
                if lease_is_live(root):
                    keeper.keep()  # a session's turn is running: stay warm
                    time.sleep(poll_seconds)
                    continue
                remove_tts_lock(root)
                owns_lock = False
                item, taken_path = next_item(root, engine_names)
                if item is None:
                    # A session may have taken a lease after our check, then
                    # seen our lock and started nothing: stay for it.
                    if not lease_is_live(root) or not try_claim_lock(root, lock_seconds):
                        return 0
                    owns_lock = True
                    keeper.reset()
                    continue
                if not try_claim_lock(root, lock_seconds):
                    # Another engine claimed the lock in the gap - it will
                    # drain this item. Never delete a lock we do not own.
                    put_back(taken_path)
                    return 0
                owns_lock = True
                keeper.reset()
```

and add one sentence to `drain`'s docstring: "While a session holds a lease the drain waits for more, polling every poll_seconds, so the model stays loaded for the turn's next sentence."

- [ ] **Step 5: Writer side — pre-warm and the test switch**

In `voiceover/speech.py` add `from . import lease, process_utils, spool` (replacing the existing `from . import process_utils, spool`). In `ensure_drainer` change the first condition to:

```python
        if spool.pending_count(cwd) == 0 and not lease.any_live(cwd):
            return False
```

and update its docstring to say it also starts the engine when a session holds a lease (the pre-warm). Add after it:

```python
def prewarm(cwd=None, session=None) -> bool:
    """Hold the project's engine for session's turn and start it now, so the
    model has loaded by the time the first sentence arrives (the first text
    of a turn arrived at least 2.2s after the prompt in 168 measured turns;
    loading takes about 1.4s)."""
    if not lease.hold(cwd, session):
        return False
    return ensure_drainer(cwd)
```

At the top of `_spawn_detached`'s body add:

```python
    if os.environ.get("VOICEOVER_NO_SPAWN"):
        # Set by the test harness: report success, start nothing, so a test
        # never leaves an engine waiting on a lease.
        return True
```

In `tests/_harness.py` `isolate()`, add `os.environ["VOICEOVER_NO_SPAWN"] = "1"` after the dry-run line, and add a sentence to its docstring saying spawning is suppressed.

- [ ] **Step 6: Run the new test and the suite**

Run: `python3 tests/test_engine_lifecycle.py && bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: `18/18 checks passed in test_engine_lifecycle.py` then `ALL TESTS PASSED`

- [ ] **Step 7: Commit**

```bash
git add voiceover/lease.py voiceover/speech.py tts/engine_common.py tests/_harness.py tests/test_engine_lifecycle.py
git commit -m "feat: turn-scoped engine - leases keep it warm, release unloads it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Narrator actions and the classic hooks

**Files:**
- Create: `voiceover/narrator.py`
- Modify: `hooks/user_prompt_submit.py`, `hooks/pre_tool_use.py`, `hooks/post_tool_use.py`, `hooks/stop.py`
- Test: `tests/test_narrator_hooks.py` (additions), `tests/test_cutover.py` (narrator expectations)

**Interfaces:**
- Consumes: `spool.clear`, `lease.hold/release`, `speech.enqueue_speech/ensure_drainer/prewarm/request_stop/play_sound`, `templates.blocking_dialog_message`
- Produces (`voiceover/narrator.py`):
  - `DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")`
  - `narrates(cwd=None) -> bool` — level is `narrator`, TTS enabled, engine not `none`
  - `mod_active(session) -> bool`; `mark_mod_session(session) -> None` (prunes markers older than 7 days); `unmark_mod_session(session) -> None`. Marker: `data_dir()/mod_sessions/<session>`
  - `cutover(cwd, session) -> None` — clear session's items, `request_stop`, `prewarm` when `narrates`
  - `queue_dialog(cwd, session, tool_name, tool_input, lead_in=None, utterance=None) -> bool` — sticky item, `dialog_alert.json` on success, `ensure_drainer`, release
  - `dialog_answered(cwd, session) -> None` — `prewarm` when `narrates`
  - `turn_end(cwd, session, spoken) -> None` — release; decide chime when not spoken
  - `write_dialog_marker() -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_narrator_hooks.py` before `report()`:

```python
# --- the lease table ------------------------------------------------------------
from voiceover import narrator


def lease_held(session="S1"):
    return (spool.queue_dir().parent / "leases" / (session + ".json")).exists()


spool.clear()
path = write_transcript("lease.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
base = {"cwd": str(REPO_ROOT), "transcript_path": str(path), "session_id": "S1"}
run_hook("pre_tool_use", dict(base, tool_name="Bash", tool_input={}))
check("a tool call holds the project's engine", lease_held())
run_hook("pre_tool_use", dict(base, tool_name="AskUserQuestion",
                              tool_input={"questions": [{"question": "Which?", "options": []}]}))
check("a question dialog releases it while the user decides", not lease_held())
items = [json.loads(p.read_text()) for p in sorted(spool.queue_dir().glob("*.json"))]
check("the dialog alert is queued sticky", items and items[-1]["sticky"] is True, items)
run_hook("post_tool_use", dict(base, tool_name="AskUserQuestion", tool_input={}))
check("the answer takes it back", lease_held())
run_hook("stop", base)
check("the turn's end lets it go", not lease_held())

# --- with the mod running this session, the classic narrator paths stand down ---
spool.clear()
narrator.mark_mod_session("MOD")
path = write_transcript("mod.jsonl", [USER])
commit_offset(str(path), path.stat().st_size)
with open(path, "a") as handle:
    handle.write(json.dumps(assistant("The mod speaks this, not the hook.")) + "\n")
mod = {"cwd": str(REPO_ROOT), "transcript_path": str(path), "session_id": "MOD"}
for hook in ("pre_tool_use", "post_tool_use"):
    run_hook(hook, dict(mod, tool_name="Bash", tool_input={}))
run_hook("stop", mod)
check("no classic narrator path queues text for a mod session", queued_texts() == [], queued_texts())
check("nor holds a lease for it", not lease_held("MOD"))
narrator.unmark_mod_session("MOD")
check("unmarking ends the stand-down", not narrator.mod_active("MOD"))

# --- old markers are pruned ---------------------------------------------------------
narrator.mark_mod_session("OLD")
old = Path(os.environ["VOICEOVER_DATA_DIR"]) / "mod_sessions" / "OLD"
stale = time.time() - 8 * 86400
os.utime(old, (stale, stale))
narrator.mark_mod_session("NEW")
check("a marker older than 7 days is pruned when another is written", not old.exists())
narrator.unmark_mod_session("NEW")
```

In `tests/test_cutover.py`:
- after `set_setting("interaction_level", "narrator")` near the top add `set_setting("tts_engine", "kokoro")`;
- replace the "playback is stopped" section with:

```python
# --- the session's sentence is stopped, the engine is NOT killed ---------------
spool.clear()
project = spool.queue_dir().parent
(project / "tts.lock").write_text(json.dumps({"expiry": time.time() + 300, "pid": 999999}))
run_hook({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"})
stop = json.loads((project / "stop.json").read_text())
check("a stop request names the typing session", stop["session"] == "S1", stop)
check("the engine's lock is left alone: it stays loaded for the reply",
      (project / "tts.lock").exists())
(project / "tts.lock").unlink()
```

- replace the ordering block (from `order = []` through the `a silenced session ...` check) with:

```python
order = []
import voiceover.narrator
real = (voiceover.spool.clear, voiceover.narrator.request_stop,
        voiceover.narrator.prewarm, voiceover.speech.stop_speech)
real_stdin = sys.stdin


def run_main_recording(level):
    """Drive main() in-process at one level; return the calls it made, in order."""
    del order[:]
    set_setting("interaction_level", level)
    voiceover.spool.clear = lambda session=None, cwd=None: order.append("clear") or 0
    voiceover.narrator.request_stop = lambda cwd=None, session=None: order.append("stop")
    voiceover.narrator.prewarm = lambda cwd=None, session=None: order.append("prewarm") or True
    voiceover.speech.stop_speech = lambda: order.append("stop_speech")
    sys.stdin = io.StringIO(json.dumps(
        {"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "new question"}))
    try:
        ups.main()
    finally:
        (voiceover.spool.clear, voiceover.narrator.request_stop,
         voiceover.narrator.prewarm, voiceover.speech.stop_speech) = real
        sys.stdin = real_stdin
    return list(order)


check("narrator: clear, then stop the sentence, then warm the engine",
      run_main_recording("narrator") == ["clear", "stop", "prewarm"], order)
check("concise: clear, then stop the one-shot speaker",
      run_main_recording("concise") == ["clear", "stop_speech"], order)
check("silent: only the clear", run_main_recording("silent") == ["clear"], order)
voiceover.narrator.mark_mod_session("S1")
check("narrator with the mod running: the hook leaves the cutover to the mod",
      run_main_recording("narrator") == [], order)
voiceover.narrator.unmark_mod_session("S1")
set_setting("interaction_level", "narrator")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 tests/test_narrator_hooks.py; python3 tests/test_cutover.py`
Expected: FAIL — `ImportError: cannot import name 'narrator'` / `No module named 'voiceover.narrator'`.

- [ ] **Step 3: Create `voiceover/narrator.py`**

```python
"""Narrator-level actions shared by the classic hooks and the mod bridge.

Both entry points parse their own input and call these, so what a prompt, a
question dialog or a turn's end does to the queue, the lease and the engine
has exactly one definition. Stdlib only; every function fails soft.
"""

import json
import re
import time

from . import lease, spool
from .debuglog import log as _log
from .settings import data_dir, get_interaction_level, is_tts_enabled, resolve_engine
from .speech import enqueue_speech, ensure_drainer, play_sound, prewarm, request_stop
from .templates import blocking_dialog_message

DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")

_MOD_SESSIONS_DIR_NAME = "mod_sessions"
_MARKER_MAX_AGE_SECONDS = 7 * 86400
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def narrates(cwd=None) -> bool:
    """True when this project speaks Claude's prose."""
    return (get_interaction_level(cwd) == "narrator" and is_tts_enabled(cwd)
            and resolve_engine(cwd) != "none")


# --- the mod's session marker ---------------------------------------------------

def _marker(session):
    name = _UNSAFE.sub("_", str(session or ""))
    return data_dir() / _MOD_SESSIONS_DIR_NAME / name if name else None


def mod_active(session) -> bool:
    """True when the mod narrates this session, so the classic paths stand down."""
    marker = _marker(session)
    return marker is not None and marker.exists()


def mark_mod_session(session) -> None:
    """Record that the mod narrates session; prune week-old markers."""
    marker = _marker(session)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _MARKER_MAX_AGE_SECONDS
        for old in marker.parent.iterdir():
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        marker.write_text("")
    except OSError:
        pass


def unmark_mod_session(session) -> None:
    marker = _marker(session)
    if marker is None:
        return
    try:
        marker.unlink()
    except OSError:
        pass


# --- the actions -----------------------------------------------------------------

def cutover(cwd, session) -> None:
    """The user's own new prompt: drop this session's pending text, stop its
    sentence mid-word, and warm the engine for the reply.

    Clear before stop, so the engine cannot claim one more item of the
    abandoned turn in between. No process is killed: the engine stays loaded
    and the reply starts warm. Without a session id nothing is cleared - an
    unscoped clear would wipe other sessions' narration.
    """
    dropped = spool.clear(session=session, cwd=cwd) if session else 0
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)
    request_stop(cwd, session)
    if narrates(cwd):
        prewarm(cwd, session)


def write_dialog_marker() -> None:
    """Tell notification.py a blocking dialog was just announced, so its
    "needs permission" echo stays quiet."""
    try:
        with open(data_dir() / "dialog_alert.json", "w") as handle:
            json.dump({"ts": time.time()}, handle)
    except OSError:
        pass


def queue_dialog(cwd, session, tool_name, tool_input, lead_in=None, utterance=None) -> bool:
    """Announce a dialog that is about to block the session on the user.

    The alert is sticky - the jump rule never drops it - and the lease is
    released, so the engine unloads once the alert has played while the user
    decides. The marker is written only when the alert actually queued:
    otherwise the notification echo is the only thing that will speak.
    """
    alert = blocking_dialog_message(tool_name, tool_input or {})
    text = (lead_in + "\n" + alert) if lead_in else alert
    queued = enqueue_speech(text, cwd=cwd, full=True, session=session,
                            utterance=utterance, sticky=True)
    if queued:
        write_dialog_marker()
    ensure_drainer(cwd)
    lease.release(cwd, session)
    return queued


def dialog_answered(cwd, session) -> None:
    """The user answered: hold the engine again for the rest of the turn."""
    if narrates(cwd):
        prewarm(cwd, session)


def turn_end(cwd, session, spoken) -> None:
    """The turn is over: let the engine go once its queue has played. A turn
    whose last step said nothing ends on the decide chime instead."""
    lease.release(cwd, session)
    if not spoken:
        play_sound("decide", cwd=cwd)
```

- [ ] **Step 4: Rewire the classic hooks**

`hooks/user_prompt_submit.py` — replace `main()` with:

```python
def main():
    payload = json.load(sys.stdin)

    from voiceover.debuglog import log as _log
    from voiceover.settings import get_interaction_level
    from voiceover.spool import clear

    cwd = payload.get("cwd")
    session = payload.get("session_id")
    level = get_interaction_level(cwd)

    if level == "narrator":
        from voiceover import narrator
        # The mod cuts over on its own when it narrates this session.
        if not narrator.mod_active(session):
            narrator.cutover(cwd, session)
        return

    # Clear at every other level, silent included: a queue stranded by a level
    # change would otherwise be drained later and speak an abandoned turn.
    # With no session id we cannot tell which items are ours.
    dropped = clear(session=session, cwd=cwd) if session else 0
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)
    if level == "silent":
        return
    from voiceover.speech import stop_speech
    stop_speech()  # the one-shot speaker only: process_utils scopes it
```

and update the module docstring: at the narrator level a new prompt stops the session's sentence (a stop request) and pre-warms the engine; other levels clear and stop the one-shot speaker.

`hooks/pre_tool_use.py` — replace the narrator branch (from `if get_interaction_level(cwd) == "narrator":` through its `return`) with:

```python
    if get_interaction_level(cwd) == "narrator":
        # Narrator mode: queue Claude's actual words written since the last
        # narration. When the mod narrates this session it captures the text
        # as it streams, so this path stands down.
        from voiceover import lease, narrator
        from voiceover.speech import ensure_drainer, enqueue_speech

        session = payload.get("session_id")
        if narrator.mod_active(session):
            return
        transcript = payload.get("transcript_path", "")
        tool_name = payload.get("tool_name", "")
        text, offset = peek_new_prose(transcript)
        if tool_name in narrator.DIALOG_TOOLS:
            # A dialog is about to block the session with no further hooks.
            # Wait out the transcript flush race for the lead-in prose.
            if not text:
                for _ in range(3):
                    time.sleep(0.5)
                    text, offset = peek_new_prose(transcript)
                    if text:
                        break
            if narrator.queue_dialog(cwd, session, tool_name,
                                     payload.get("tool_input") or {}, lead_in=text) and text:
                commit_offset(transcript, offset)
            return
        lease.hold(cwd, session)
        if text and enqueue_speech(text, cwd=cwd, full=True, session=session):
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
        return
```

`hooks/post_tool_use.py` — replace the narrator branch with:

```python
    if get_interaction_level(payload.get("cwd")) == "narrator":
        # No '- done' chatter. Prose written just before this tool call
        # (which pre-tool can miss by a flush race) is queued here instead.
        from voiceover import lease, narrator
        from voiceover.prose import commit_offset, peek_new_prose
        from voiceover.speech import ensure_drainer, enqueue_speech

        cwd = payload.get("cwd")
        session = payload.get("session_id")
        if narrator.mod_active(session):
            return
        if payload.get("tool_name") in narrator.DIALOG_TOOLS:
            narrator.dialog_answered(cwd, session)  # warm up again for the rest
        else:
            lease.hold(cwd, session)
        transcript = payload.get("transcript_path", "")
        text, offset = peek_new_prose(transcript)
        if text and enqueue_speech(text, cwd=cwd, full=True, session=session):
            # Queued means it WILL be spoken, so the cursor can advance now.
            commit_offset(transcript, offset)
        ensure_drainer(cwd)
        return
```

`hooks/stop.py` — in the narrator branch: right after `if level == "narrator":` add

```python
        from voiceover import narrator
        session = payload.get("session_id")
        if narrator.mod_active(session):
            return
```

and replace everything after the polling loop's log line, up to the end of the narrator branch, with:

```python
        if prose and enqueue_speech(prose, cwd=cwd, full=True, session=session):
            # The finale is Claude's own closing words. It goes to the BACK of
            # the queue: order is what makes narration followable.
            commit_offset(transcript_path, offset)
        ensure_drainer(cwd)
        # The turn is over: the engine goes once the finale has played. A
        # turn with nothing left to say ends on the chime instead - narrator
        # mode only ever speaks Claude's own words.
        narrator.turn_end(cwd, session, spoken=bool(prose))
        return
```

- [ ] **Step 5: Run the tests and the suite**

Run: `python3 tests/test_narrator_hooks.py && python3 tests/test_cutover.py && bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: both files pass, then `ALL TESTS PASSED`

- [ ] **Step 6: Commit**

```bash
git add voiceover/narrator.py hooks/ tests/
git commit -m "feat: narrator actions shared by hooks and mod; hooks hold/release leases

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The non-narrator interrupt never reaches a narrator engine

**Files:**
- Modify: `voiceover/process_utils.py` (`TTS_PROCESS_PATTERNS`)
- Modify: `tts/tts_controller.py` (`UNIX_PATTERNS`, `clear_projects`, `stop_tts`)
- Test: `tests/test_stop_scope.py` (new)

**Interfaces:**
- Consumes: drainer argv `--drain <dir>` (Task 2), `say --voice=` (Task 3), one-shot wavs named `voiceover-oneshot-*.wav` (Task 3)
- Produces: `process_utils.TTS_PROCESS_PATTERNS` matching one-shots only; `tts_controller.clear_projects() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stop_scope.py`:

```python
"""The one-shot interrupt never reaches a narrator engine or its playback.

Another project may be narrating: a concise-level session's interrupt must
only stop the one-shot speaker. Stand-in processes carry the real command
lines; pkill -f matches on those."""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import process_utils

if sys.platform == "win32":
    print("SKIP  POSIX-only: pkill command-line patterns")
    report()


def stand_in(*argv):
    """A sleeping process whose command line carries argv."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"] + list(argv))


survivors = {
    "a kokoro drainer": stand_in("kokoro_voice.py", "--drain", "/d/projects/abc"),
    "a macOS drainer": stand_in("macos_say.py", "--drain", "/d/projects/abc"),
    "a drainer's say": stand_in("say", "--voice=Samantha"),
    "a drainer's wav playback": stand_in("afplay", "/d/projects/abc/play/1-2.wav"),
}
victims = {
    "the one-shot kokoro engine": stand_in("kokoro_voice.py", "--voice", "bf_emma", "--stream", "hi"),
    "the one-shot say engine": stand_in("macos_say.py", "--voice", "Samantha", "hi"),
    "the one-shot say child": stand_in("say", "-v", "Samantha"),
    "the one-shot wav playback": stand_in("afplay", "/tmp/voiceover-oneshot-abc.wav"),
}
time.sleep(0.2)
process_utils.kill_processes_by_pattern(process_utils.TTS_PROCESS_PATTERNS)
time.sleep(0.3)
for name, proc in survivors.items():
    check(name + " survives the one-shot interrupt", proc.poll() is None)
for name, proc in victims.items():
    check(name + " is stopped", proc.poll() is not None)
for proc in list(survivors.values()) + list(victims.values()):
    if proc.poll() is None:
        proc.kill()
    proc.wait()

# --- the kill switch clears every project's state --------------------------------
sys.path.insert(0, str(REPO_ROOT / "tts"))
import tts_controller

project = data_dir / "projects" / "abc123def456"
for relative in ("tts.lock", "stop.json", "queue/1.json", "leases/S1.json", "play/1.wav"):
    (project / relative).parent.mkdir(parents=True, exist_ok=True)
    (project / relative).write_text("x")
tts_controller.clear_projects()
check("the kill switch leaves no lock, queue, lease or stop file",
      [p for p in project.rglob("*") if p.is_file()] == [],
      [str(p) for p in project.rglob("*") if p.is_file()])

report()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_stop_scope.py`
Expected: FAIL — the drainer survivors are killed (`a kokoro drainer survives ...` FAIL) and `AttributeError: module 'tts_controller' has no attribute 'clear_projects'`.

- [ ] **Step 3: Scope the patterns in `voiceover/process_utils.py`**

```python
# Full-command patterns for the ONE-SHOT speaker (pkill -f / psutil cmdline).
# Narrator drainers run as `--drain <project dir>`, speak through
# `say --voice=<Name>` and play wavs from their project's play/ directory, so
# none of these reach them: another project may be narrating, and a
# concise-level interrupt must not cut it off.
TTS_PROCESS_PATTERNS = [
    "kokoro_voice.py --voice",
    "macos_say.py --voice",
    "tts_controller.py",
    # The `say` children macos_say.py's one-shot path launches.
    "say -v Samantha",
    "say -v Daniel",
    # kokoro's one-shot temp-wav playback child.
    "afplay .*voiceover-oneshot-.*\\.wav",
]
```

Update the module docstring's pattern note to match, and `_kill_lock_owner_group`'s comment: only one-shot engines hold the global lock now.

- [ ] **Step 4: Extend the kill switch in `tts/tts_controller.py`**

Add `r"(^|[/ ])say --voice="` to `UNIX_PATTERNS` (after the `say -v` line, comment: "the `say` a drainer spawns"). Add:

```python
def clear_projects():
    """Drop every project's lock, queue, leases, stop file and wavs, so the
    kill switch leaves nothing that would restart narration."""
    try:
        projects = [p for p in (data_dir() / "projects").iterdir() if p.is_dir()]
    except OSError:
        return
    for project in projects:
        for pattern in ("tts.lock", "stop.json", "queue/*", "leases/*", "play/*"):
            for path in project.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass
```

and call `clear_projects()` at the end of `stop_tts()`. Update the module docstring: the kill switch stops every engine, drainers included.

- [ ] **Step 5: Run the test and the suite**

Run: `python3 tests/test_stop_scope.py && bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: `9/9 checks passed in test_stop_scope.py` then `ALL TESTS PASSED`

- [ ] **Step 6: Commit**

```bash
git add voiceover/process_utils.py tts/tts_controller.py tests/test_stop_scope.py
git commit -m "fix: a one-shot interrupt can no longer kill another project's narration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The mod bridge

**Files:**
- Create: `hooks/mod_bridge.py`
- Test: `tests/test_mod_bridge.py`

**Interfaces:**
- Consumes: `voiceover.narrator.*` (Task 5), `speech.enqueue_speech/ensure_drainer/prewarm`, `lease.release`, `prose.clean_for_speech`
- Produces: `bash hooks/run.sh mod_bridge` reading `{"session": str, "cwd": str, "ops": [...]}` on stdin, ops:
  - `{"op":"start"}` → `mark_mod_session`
  - `{"op":"end-session"}` → `lease.release`, `unmark_mod_session`
  - `{"op":"prompt"}` → `mark_mod_session`; when narrating, `cutover`
  - `{"op":"hold"}` → `prewarm` (narrating only)
  - `{"op":"say","utterance":str,"text":str,"shown":float}` → `clean_for_speech`, `enqueue_speech(full=True, utterance=, shown=)` (narrating only); one `ensure_drainer` after the batch
  - `{"op":"dialog","tool":str,"input":dict,"utterance":str|null}` → `queue_dialog` (narrating only)
  - `{"op":"turn-end","spoken":bool}` → `turn_end` (narrating only)
  - stdout: exactly one line, `{"narrate": true|false}` (`null` after an internal error)

- [ ] **Step 1: Write the failing test**

Create `tests/test_mod_bridge.py`:

```python
"""The mod's bridge into the narrator: ops in, one JSON line out."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

data_dir = isolate()
from voiceover import narrator, spool
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
PROJECT = spool.queue_dir().parent


def bridge(*ops, session="S1", stdin=None):
    payload = stdin if stdin is not None else json.dumps(
        {"session": session, "cwd": str(REPO_ROOT), "ops": list(ops)})
    proc = subprocess.run(["bash", str(REPO_ROOT / "hooks" / "run.sh"), "mod_bridge"],
                          input=payload, capture_output=True, text=True, env=os.environ.copy())
    return proc


def reply(proc):
    return json.loads(proc.stdout.strip().splitlines()[-1])


def items():
    return [json.loads(p.read_text()) for p in sorted(spool.queue_dir().glob("*.json"))]


def leased(session="S1"):
    return (PROJECT / "leases" / (session + ".json")).exists()


# --- prompt: the gate, the marker, the cutover ------------------------------------
proc = bridge({"op": "prompt"})
check("the bridge exits 0", proc.returncode == 0, proc.stderr)
check("it prints exactly one line", len(proc.stdout.strip().splitlines()) == 1, proc.stdout)
check("prompt reports narration on", reply(proc) == {"narrate": True}, proc.stdout)
check("prompt marks the session (Review Focus 3)", narrator.mod_active("S1"))
check("prompt writes the stop request", json.loads((PROJECT / "stop.json").read_text())["session"] == "S1")
check("prompt pre-warms: the session holds a lease", leased())

# --- say: cleaned, one message, timestamps kept -------------------------------------
spool.clear()
bridge({"op": "say", "utterance": "T:0:0", "text": "**Reading** the loader. ", "shown": 100.5},
       {"op": "say", "utterance": "T:0:0", "text": "| a | b |\n", "shown": 100.6},
       {"op": "say", "utterance": "T:0:0", "text": "Then the tests.", "shown": 100.7})
queued = items()
check("each speakable segment is queued, markdown cleaned",
      [i["text"] for i in queued] == ["Reading the loader.", "Then the tests."], queued)
check("segments of one block share its utterance", {i["utterance"] for i in queued} == {"T:0:0"})
check("the shown time is kept for latency logging", queued[0]["shown"] == 100.5, queued[0])

# --- dialog: sticky, under the lead-in's utterance, lease released --------------------
spool.clear()
bridge({"op": "hold"})
bridge({"op": "dialog", "tool": "AskUserQuestion", "utterance": "T:0:0",
        "input": {"questions": [{"question": "Which approach?", "options": [{"label": "A"}]}]}})
alert = items()
check("the dialog alert is queued sticky under the lead-in's message",
      len(alert) == 1 and alert[0]["sticky"] is True and alert[0]["utterance"] == "T:0:0", alert)
check("it reads the actual question", alert and "Which approach?" in alert[0]["text"], alert)
check("the lease is released while the user decides", not leased())
check("the notification echo is suppressed", (data_dir / "dialog_alert.json").exists())

# --- turn-end ---------------------------------------------------------------------------
bridge({"op": "hold"})
proc = bridge({"op": "turn-end", "spoken": False})
check("turn-end releases the lease", not leased())
check("a silent turn ends on the chime", "[voiceover] sound: decide" in proc.stderr, proc.stderr)

# --- below the narrator level, only the gate and the marker act -----------------------
set_setting("interaction_level", "concise")
spool.clear()
proc = bridge({"op": "prompt"}, {"op": "say", "utterance": "U", "text": "Not spoken."},
              session="S2")
check("concise: narration reported off", reply(proc) == {"narrate": False}, proc.stdout)
check("concise: nothing queued", items() == [], items())
check("concise: no lease taken", not leased("S2"))
check("concise: the session is still marked", narrator.mod_active("S2"))
set_setting("interaction_level", "narrator")

# --- end-session ------------------------------------------------------------------------
bridge({"op": "hold"})
bridge({"op": "end-session"})
check("end-session unmarks the session", not narrator.mod_active("S1"))
check("end-session releases its lease", not leased())

# --- garbage in, one line out ------------------------------------------------------------
proc = bridge(stdin="not json")
check("a malformed batch still exits 0", proc.returncode == 0, proc.stderr)
check("and still prints one JSON line", "narrate" in reply(proc), proc.stdout)

report()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test_mod_bridge.py`
Expected: FAIL — `json.decoder.JSONDecodeError` (run.sh finds no `hooks/mod_bridge.py`, prints nothing).

- [ ] **Step 3: Create `hooks/mod_bridge.py`**

```python
#!/usr/bin/env python3
"""The mod's bridge into the narrator: `run.sh mod_bridge`, a JSON batch on stdin.

The mod (hooks/narrator.ts) only captures text; every narration policy lives
here and in voiceover/, shared with the classic hooks. Input:

    {"session": str, "cwd": str, "ops": [op, ...]}

    {"op": "start"}          the mod narrates this session (classic paths stand down)
    {"op": "end-session"}    release the lease, drop the marker
    {"op": "prompt"}         mark the session (a /clear starts a new id with no
                             session.start); when narrating, cut over
    {"op": "hold"}           a model step began: hold and warm the engine
    {"op": "say", "utterance": str, "text": str, "shown": float}
    {"op": "dialog", "tool": str, "input": dict, "utterance": str | null}
    {"op": "turn-end", "spoken": bool}

Prints exactly one line, {"narrate": bool}: the narrator gate as of this call
(null after an internal error). Never blocks, always exits 0.
"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def run(payload):
    """Execute one batch of ops in order; return the narrator gate."""
    from voiceover import lease, narrator
    from voiceover.prose import clean_for_speech
    from voiceover.speech import enqueue_speech, ensure_drainer, prewarm

    session = payload.get("session")
    cwd = payload.get("cwd")
    narrate = narrator.narrates(cwd)
    queued = False
    for op in payload.get("ops") or []:
        kind = op.get("op") if isinstance(op, dict) else None
        if kind == "start":
            narrator.mark_mod_session(session)
        elif kind == "end-session":
            lease.release(cwd, session)
            narrator.unmark_mod_session(session)
        elif kind == "prompt":
            narrator.mark_mod_session(session)
            if narrate:
                narrator.cutover(cwd, session)
        elif not narrate:
            continue
        elif kind == "hold":
            prewarm(cwd, session)
        elif kind == "say":
            text = clean_for_speech(op.get("text") or "")
            if text and enqueue_speech(text, cwd=cwd, full=True, session=session,
                                       utterance=op.get("utterance"), shown=op.get("shown")):
                queued = True
        elif kind == "dialog":
            narrator.queue_dialog(cwd, session, op.get("tool") or "",
                                  op.get("input") or {}, utterance=op.get("utterance"))
        elif kind == "turn-end":
            narrator.turn_end(cwd, session, bool(op.get("spoken")))
    if queued:
        ensure_drainer(cwd)
    return narrate


def main():
    try:
        narrate = run(json.load(sys.stdin))
    except Exception:
        narrate = None
    print(json.dumps({"narrate": narrate}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
```

- [ ] **Step 4: Run the test and the suite**

Run: `python3 tests/test_mod_bridge.py && bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"`
Expected: `23/23 checks passed in test_mod_bridge.py` then `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add hooks/mod_bridge.py tests/test_mod_bridge.py
git commit -m "feat: mod bridge - the mod's ops run through the shared narrator actions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The segmenter

**Files:**
- Create: `hooks/segment.ts`
- Test: `hooks/segment.test.ts`

**Interfaces:**
- Produces: `createSegmenter(): Segmenter` with `push(text: string): string[]` and `flush(): string[]`; `boundary(text: string): number`

- [ ] **Step 1: Load the mod reference and set up type-checking**

Invoke the `plugin-authoring` skill; note its types file path as `<TYPES>`. Write `<SCRATCH>/tsconfig.json` (scratchpad, not the repo):

```json
{
  "compilerOptions": {
    "target": "es2023", "lib": ["es2023"], "types": [],
    "module": "esnext", "moduleResolution": "bundler",
    "strict": true, "noUncheckedIndexedAccess": true,
    "noEmit": true, "skipLibCheck": true
  },
  "include": ["<TYPES>", "/Users/hanan/Projects/claude-voiceover/hooks/*.ts"]
}
```

Type-check command used below: `npx -y -p typescript@5 tsc -p <SCRATCH>/tsconfig.json`

- [ ] **Step 2: Write the failing test**

Create `hooks/segment.test.ts`:

```ts
import { expect, test } from 'claude-code/testing'
import { boundary, createSegmenter } from './segment'

test('sentences are emitted as soon as they complete', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Hello there. Sec')).toEqual(['Hello there. '])
  expect(segmenter.push('ond one! Third')).toEqual(['Second one! '])
  expect(segmenter.flush()).toEqual(['Third'])
})

test('a newline ends a segment', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('A heading\n- an item\n')).toEqual(['A heading\n', '- an item\n'])
})

test('a fenced block is held until it closes, then emitted whole', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Look:\n```ts\nconst a = 1. b\n')).toEqual(['Look:\n'])
  expect(segmenter.push('```\nAfter.')).toEqual(['```ts\nconst a = 1. b\n```\n'])
  expect(segmenter.flush()).toEqual(['After.'])
})

test('a fence marker split across chunks is still recognized', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('``')).toEqual([])
  expect(segmenter.push('`\nx. y\n```\n')).toEqual(['```\nx. y\n```\n'])
})

test('punctuation inside a word or number is not a boundary', () => {
  expect(boundary('v1.4.0 is out')).toBe(-1)
  expect(boundary('see e.g.this')).toBe(-1)
})

test('flushing only whitespace yields nothing', () => {
  const segmenter = createSegmenter()
  segmenter.push('Done. ')
  expect(segmenter.flush()).toEqual([])
})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `claude plugin test .`
Expected: FAIL — `segment.test.ts` cannot resolve `./segment`.

- [ ] **Step 4: Create `hooks/segment.ts`**

```ts
/**
 * Cuts streaming markdown into speakable segments: one per sentence or line,
 * as soon as each completes, never splitting a fenced code block - a fence
 * is emitted whole once it closes, so the bridge's cleaner turns it into the
 * spoken code cue.
 */
export type Segmenter = {
  /** Appends streamed text; returns the segments it completed, in order. */
  push(text: string): string[]
  /** Returns what is left as a final segment (nothing when only whitespace). */
  flush(): string[]
}

const FENCE = '```'
const ENDS = new Set(['.', '!', '?'])

export function createSegmenter(): Segmenter {
  let buffer = ''
  const take = (): string[] => {
    const out: string[] = []
    for (let end = boundary(buffer); end > 0; end = boundary(buffer)) {
      const piece = buffer.slice(0, end)
      buffer = buffer.slice(end)
      if (piece.trim()) out.push(piece)
    }
    return out
  }
  return {
    push(text) {
      buffer += text
      return take()
    },
    flush() {
      const rest = buffer
      buffer = ''
      return rest.trim() ? [rest] : []
    },
  }
}

/** End (exclusive) of the first complete segment in text, or -1. */
export function boundary(text: string): number {
  let inFence = false
  for (let i = 0; i < text.length; i++) {
    if (text.startsWith(FENCE, i)) {
      inFence = !inFence
      i += FENCE.length - 1
      continue
    }
    if (inFence) continue
    const char = text[i]
    if (char === '\n') return i + 1
    const following = text[i + 1]
    if (char !== undefined && ENDS.has(char) && following !== undefined && /\s/.test(following)) {
      return i + 2
    }
  }
  return -1
}
```

- [ ] **Step 5: Run the tests and the type check**

Run: `claude plugin test . && npx -y -p typescript@5 tsc -p <SCRATCH>/tsconfig.json`
Expected: all `segment.test.ts` tests pass; tsc prints nothing.

- [ ] **Step 6: Commit**

```bash
git add hooks/segment.ts hooks/segment.test.ts
git commit -m "feat: segmenter for streamed markdown (sentence, line, whole fences)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: The mod

**Files:**
- Create: `hooks/bridge.ts`, `hooks/narrator.ts`
- Modify: `hooks/hooks.json` (add `"modules"`)
- Test: `hooks/bridge.test.ts`, `hooks/narrator.test.ts`, `tests/test_plugin_manifest.py` (modules check)

**Interfaces:**
- Consumes: `createSegmenter` (Task 8); `hooks/mod_bridge.py`'s op protocol (Task 7); Claude Code events `session.start`, `session.end`, `prompt.submit`, `turn.step`, `tool.call`, `turn.complete`; `$.process.run`, `$.session.id/cwd`, `$.plugin.root`, `$.ui.log`
- Produces:
  - `bridge.ts`: `type Op` (the seven ops), `type Reply = { narrate: boolean }`, `createChannel(run: Run): Channel` (`send(op): Promise<Reply>`, never rejects), `runBridge($, session, cwd, ops): Promise<Reply>`, `parseReply(stdout): Reply`
  - `narrator.ts`: `export const register: Register`

- [ ] **Step 1: Write the failing channel test**

Create `hooks/bridge.test.ts`:

```ts
import { expect, test } from 'claude-code/testing'
import { createChannel, parseReply, type Op } from './bridge'

test('ops are delivered in order, batched while a call is in flight', async () => {
  const calls: Op[][] = []
  let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const channel = createChannel(async ops => {
    calls.push([...ops])
    if (calls.length === 1) await gate
    return { narrate: true }
  })
  const first = channel.send({ op: 'prompt' })
  const second = channel.send({ op: 'hold' })
  const third = channel.send({ op: 'turn-end', spoken: false })
  release()
  expect(await first).toEqual({ narrate: true })
  await second
  await third
  expect(calls).toEqual([[{ op: 'prompt' }], [{ op: 'hold' }, { op: 'turn-end', spoken: false }]])
})

test('a failed call settles its ops as silent and the next call still runs', async () => {
  let n = 0
  const channel = createChannel(async () => {
    n += 1
    if (n === 1) throw new Error('down')
    return { narrate: true }
  })
  expect(await channel.send({ op: 'prompt' })).toEqual({ narrate: false })
  expect(await channel.send({ op: 'prompt' })).toEqual({ narrate: true })
})

test('the reply is read from the last stdout line', () => {
  expect(parseReply('noise\n{"narrate": true}\n')).toEqual({ narrate: true })
  expect(parseReply('')).toEqual({ narrate: false })
  expect(parseReply('{"narrate": null}')).toEqual({ narrate: false })
})
```

- [ ] **Step 2: Write the failing mod test**

Create `hooks/narrator.test.ts`:

```ts
import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

type SentOp = { op: string; text?: string; utterance?: string | null; spoken?: boolean; input?: unknown }

/** Stands beneath the mod for the engine: the bridge process, the session, the bottoms. */
function fakeEngine(on: On, narrate: boolean, bridgeFails = false) {
  const ops: SentOp[] = []
  const waiters: { check: (sent: SentOp[]) => boolean; done: () => void }[] = []
  on('process.run', async (_$, e) => {
    if (bridgeFails) throw new Error('bridge down')
    const batch = JSON.parse(e.init?.stdin ?? '{}') as { ops?: SentOp[] }
    ops.push(...(batch.ops ?? []))
    for (const waiter of waiters.splice(0)) {
      if (waiter.check(ops)) waiter.done()
      else waiters.push(waiter)
    }
    return { exitCode: 0, stdout: `${JSON.stringify({ narrate })}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  })
  on('session.id', async () => 'S1')
  on('session.cwd', async () => '/work/project')
  on('ui.log', async () => undefined)
  on('prompt.submit', async (_$, e) => ({ text: e.text }))
  on('turn.complete', async () => ({ text: '' }))
  on('session.end', async () => ({}) as never)
  /** Resolves once the ops sent so far satisfy check. */
  const until = (check: (sent: SentOp[]) => boolean): Promise<void> =>
    check(ops) ? Promise.resolve() : new Promise<void>(done => { waiters.push({ check, done }) })
  return { ops, until }
}

/** The model's response for every step: these chunks, then a result. */
function fakeStep(on: On, chunks: readonly unknown[]) {
  on('turn.step', async function* (_$, e) {
    for (const chunk of chunks) yield chunk as never
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null }
  })
}

const step = (extra: { agentId?: string; turnId?: string } = {}) =>
  ({ turnId: 'T', index: 0, model: 'm', messageCount: 1, ...extra })

async function collect<C>(stream: AsyncIterable<C>): Promise<C[]> {
  const out: C[] = []
  for await (const chunk of stream) out.push(chunk)
  return out
}

const turnEnd = (extra: { agentId?: string } = {}) =>
  ({ turnId: 'T', answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const, ...extra })

test('every chunk passes through unchanged, and each sentence is queued in order', async ($, on) => {
  const engine = fakeEngine(on, true)
  const chunks = [
    { kind: 'text', index: 0, text: 'Reading the loader. Then' },
    { kind: 'text', index: 0, text: ' the tests.' },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ]
  fakeStep(on, chunks)
  await $.prompt.submit({ text: 'go' })
  expect(await collect($.turn.step(step()))).toEqual(chunks)
  await engine.until(sent => sent.filter(o => o.op === 'say').length === 2)
  const says = engine.ops.filter(o => o.op === 'say')
  expect(says.map(o => o.text)).toEqual(['Reading the loader. ', 'Then the tests.'])
  expect(says.map(o => o.utterance)).toEqual(['T:0:0', 'T:0:0'])
  expect(engine.ops.slice(0, 2).map(o => o.op)).toEqual(['prompt', 'hold'])
})

test('a subagent step is never narrated', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, [{ kind: 'text', index: 0, text: 'Some words. ' }])
  await $.prompt.submit({ text: 'go' })
  await collect($.turn.step(step({ agentId: 'A1', turnId: 'SUB' })))
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.utterance)).toEqual(['T:0:0'])
})

test('nothing but the gate and the marker is sent when narration is off', async ($, on) => {
  const engine = fakeEngine(on, false)
  fakeStep(on, [{ kind: 'text', index: 0, text: 'Quiet now. ' }])
  await $.prompt.submit({ text: 'go' })
  await collect($.turn.step(step()))
  await $.turn.complete(turnEnd())
  await $.session.end({ reason: 'other' } as never)
  await engine.until(sent => sent.some(o => o.op === 'end-session'))
  expect(engine.ops.map(o => o.op)).toEqual(['prompt', 'end-session'])
})

test('a failing bridge never breaks the stream', async ($, on) => {
  fakeEngine(on, true, true)
  const chunks = [{ kind: 'text', index: 0, text: 'Still shown. ' }, { kind: 'stop', stopReason: 'end_turn', usage: null }]
  fakeStep(on, chunks)
  await $.prompt.submit({ text: 'go' })
  expect(await collect($.turn.step(step()))).toEqual(chunks)
})

test('a dialog is announced under the lead-in message; the answer re-warms the engine', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, [{ kind: 'text', index: 0, text: 'Let me ask you. ' }])
  on('tool.call', { tool: 'AskUserQuestion' }, async () => ({ result: {} as never, text: 'answered' }) as never)
  await $.prompt.submit({ text: 'go' })
  await collect($.turn.step(step()))
  const questions = [{ question: 'Which?', header: 'Pick', options: [], multiSelect: false }]
  await $.tool.call({ tool: 'AskUserQuestion', questions } as never)
  await engine.until(sent => sent.filter(o => o.op === 'hold').length === 2)
  expect(engine.ops.map(o => o.op)).toEqual(['prompt', 'hold', 'say', 'dialog', 'hold'])
  const dialog = engine.ops.find(o => o.op === 'dialog')
  expect(dialog?.utterance).toBe('T:0:0')
  expect(dialog?.input).toEqual({ questions })
})

test('the turn end says whether the last step spoke; a subagent turn end is ignored', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, [{ kind: 'text', index: 0, text: 'Done here. ' }])
  await $.prompt.submit({ text: 'go' })
  await collect($.turn.step(step()))
  await $.turn.complete(turnEnd({ agentId: 'A1' }))
  await $.turn.complete({ ...turnEnd(), isAborted: true, reason: 'aborted' })
  await engine.until(sent => sent.some(o => o.op === 'turn-end'))
  expect(engine.ops.filter(o => o.op === 'turn-end')).toEqual([{ op: 'turn-end', spoken: true }])
})
```

If `claude plugin test` reports that `$.tool.call` raised `classic.PreToolUse` into this plugin's own command hooks (it runs `bash hooks/run.sh pre_tool_use`), add `on('classic.PreToolUse', async () => ({}) as never)` at the top of that test, beneath the plugin, and re-run.

- [ ] **Step 3: Run them to verify they fail**

Run: `claude plugin test .`
Expected: FAIL — `bridge.test.ts` cannot resolve `./bridge`; `narrator.test.ts` sees no `say` ops (no module loaded).

- [ ] **Step 4: Create `hooks/bridge.ts`**

```ts
/**
 * The mod's line to hooks/mod_bridge.py. Ops go out in order; ops queued
 * while a call is in flight travel together in the next one, so a sentence
 * streamed during a 60ms bridge call never overtakes the one before it.
 */
import type { EngineInterface } from 'claude-code'

export type Op =
  | { op: 'start' }
  | { op: 'end-session' }
  | { op: 'prompt' }
  | { op: 'hold' }
  | { op: 'say'; utterance: string; text: string; shown: number }
  | { op: 'dialog'; tool: string; input: unknown; utterance: string | null }
  | { op: 'turn-end'; spoken: boolean }

export type Reply = { narrate: boolean }
export type Run = (ops: readonly Op[]) => Promise<Reply>
export type Channel = { send(op: Op): Promise<Reply> }

const SILENT: Reply = { narrate: false }

/** A channel over run. send() never rejects: a failed call answers SILENT. */
export function createChannel(run: Run): Channel {
  let queued: { op: Op; settle: (reply: Reply) => void }[] = []
  let running = false
  const pump = async (): Promise<void> => {
    running = true
    while (queued.length > 0) {
      const batch = queued
      queued = []
      let reply = SILENT
      try {
        reply = await run(batch.map(entry => entry.op))
      } catch {
        reply = SILENT
      }
      for (const entry of batch) entry.settle(reply)
    }
    running = false
  }
  return {
    send(op) {
      return new Promise<Reply>(settle => {
        queued.push({ op, settle })
        if (!running) void pump()
      })
    },
  }
}

/** One bridge call: the plugin's run.sh finds a Python 3 and runs mod_bridge.py. */
export async function runBridge($: EngineInterface, session: string, cwd: string, ops: readonly Op[]): Promise<Reply> {
  const { stdout } = await $.process.run(
    ['bash', `${$.plugin.root}/hooks/run.sh`, 'mod_bridge'],
    { stdin: JSON.stringify({ session, cwd, ops }), timeoutMs: 5000 },
  )
  return parseReply(stdout)
}

/** The bridge's answer is its last stdout line: {"narrate": bool}. */
export function parseReply(stdout: string): Reply {
  const line = stdout.trim().split('\n').pop() ?? ''
  try {
    return { narrate: (JSON.parse(line) as { narrate?: unknown }).narrate === true }
  } catch {
    return SILENT
  }
}
```

- [ ] **Step 5: Create `hooks/narrator.ts`**

```ts
/**
 * Claude Voiceover's mod: speaks Claude's prose as it streams.
 *
 * It only captures. Each sentence of the main loop's visible text goes, in
 * order, to hooks/mod_bridge.py, which owns every narration policy - gating,
 * cleaning, voices, the per-project queue and engine. The bridge marks this
 * session, and the classic hooks stand down for marked sessions at the
 * narrator level, so nothing is spoken twice.
 *
 * Nothing here may delay what the user sees: every chunk is yielded before
 * the tap reads it, and every bridge call is fire-and-forget.
 */
import type { EngineInterface, Register } from 'claude-code'
import { createChannel, runBridge, type Channel, type Op } from './bridge'
import { createSegmenter, type Segmenter } from './segment'

type SessionState = {
  $: EngineInterface
  session: string
  cwd: string
  channel: Channel
  /** This turn's narrator gate, from the bridge's answer to the prompt op. */
  narrate: Promise<boolean>
  /** The utterance of the last text sent: a dialog alert continues it. */
  lastUtterance: string | null
  /** Whether the current step queued any text (for the end-of-turn chime). */
  stepSpoke: boolean
}

export const register: Register = on => {
  let state: SessionState | undefined
  let ready: Promise<SessionState | undefined> = Promise.resolve(undefined)

  const debug = ($: EngineInterface, text: string): void => {
    void $.ui.log(`voiceover: ${text}`, { to: 'debug' })
  }

  /** The state for the session $ belongs to: a fresh one after /clear. */
  const open = async ($: EngineInterface): Promise<SessionState> => {
    const [session, cwd] = await Promise.all([$.session.id(), $.session.cwd()])
    if (state && state.session === session) {
      state.$ = $
      state.cwd = cwd
      return state
    }
    const fresh: SessionState = {
      $,
      session,
      cwd,
      channel: createChannel(ops => runBridge(fresh.$, fresh.session, fresh.cwd, ops)),
      narrate: Promise.resolve(false),
      lastUtterance: null,
      stepSpoke: false,
    }
    state = fresh
    return fresh
  }

  /** Sends op once this turn's gate says narration is on. */
  const whenNarrating = (s: SessionState, op: Op): void => {
    void s.narrate
      .then(narrate => (narrate ? s.channel.send(op) : undefined))
      .catch(error => debug(s.$, `bridge: ${String(error)}`))
  }

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    ready = open($).then(
      s => {
        void s.channel.send({ op: 'start' })
        return s
      },
      error => {
        debug($, `session.start: ${String(error)}`)
        return undefined
      },
    )
    return started
  })

  on('session.end', async ($, e, next) => {
    const s = await ready
    if (s) {
      await s.channel.send({ op: 'end-session' })
      state = undefined
    }
    ready = Promise.resolve(undefined)
    return next(e)
  })

  on('prompt.submit', ($, e, next) => {
    // Never awaited: the prompt is not delayed. The gate resolves long
    // before the first text chunk (no turn's first text arrived sooner than
    // 2.2s after the prompt, across 168 measured turns).
    ready = open($).then(
      s => {
        s.lastUtterance = null
        s.narrate = s.channel.send({ op: 'prompt' }).then(reply => reply.narrate)
        return s
      },
      error => {
        debug($, `prompt.submit: ${String(error)}`)
        return undefined
      },
    )
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (e.agentId !== undefined) return yield* stream // a subagent's words are not narrated
    const current = ready
    const segmenters = new Map<number, Segmenter>()
    void current.then(s => {
      if (!s) return
      s.$ = $
      s.stepSpoke = false
      whenNarrating(s, { op: 'hold' })
    })
    const emit = (block: number, pieces: readonly string[]): void => {
      if (pieces.length === 0) return
      const utterance = `${e.turnId}:${e.index}:${block}`
      const shown = Date.now() / 1000
      void current.then(s => {
        if (!s) return
        s.lastUtterance = utterance
        s.stepSpoke = true
        for (const text of pieces) whenNarrating(s, { op: 'say', utterance, text, shown })
      })
    }
    try {
      for (let step = await stream.next(); ; step = await stream.next()) {
        if (step.done) return step.value
        yield step.value
        try {
          const chunk = step.value
          if (chunk.kind === 'text') {
            let segmenter = segmenters.get(chunk.index)
            if (!segmenter) {
              segmenter = createSegmenter()
              segmenters.set(chunk.index, segmenter)
            }
            emit(chunk.index, segmenter.push(chunk.text))
          }
        } catch (error) {
          debug($, `segmenter: ${String(error)}`)
        }
      }
    } finally {
      for (const [block, segmenter] of segmenters) emit(block, segmenter.flush())
    }
  })

  const announceDialog = (tool: string, input: unknown): void => {
    void ready.then(s => {
      if (s) whenNarrating(s, { op: 'dialog', tool, input, utterance: s.lastUtterance })
    })
  }
  const dialogAnswered = (): void => {
    void ready.then(s => {
      if (s) whenNarrating(s, { op: 'hold' })
    })
  }

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    announceDialog('AskUserQuestion', { questions: (e as { questions?: unknown }).questions ?? [] })
    const result = await next(e)
    dialogAnswered()
    return result
  })

  on('tool.call', { tool: 'ExitPlanMode' }, async ($, e, next) => {
    announceDialog('ExitPlanMode', {})
    const result = await next(e)
    dialogAnswered()
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // A subagent's turn ending is not ours: releasing there would unload the
    // engine in the middle of the main turn.
    if (e.agentId === undefined) {
      void ready.then(s => {
        if (s) whenNarrating(s, { op: 'turn-end', spoken: s.stepSpoke })
      })
    }
    return result
  })
}
```

- [ ] **Step 6: Declare the module**

In `hooks/hooks.json` add `"modules": ["./narrator.ts"],` as the line after `"description"`. In `tests/test_plugin_manifest.py`, after the "all six hook events" check, add:

```python
hooks_json = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())
check("hooks.json declares the narrator mod beside the command hooks",
      hooks_json.get("modules") == ["./narrator.ts"], hooks_json.get("modules"))
check("the mod's module file exists", (REPO_ROOT / "hooks" / "narrator.ts").is_file())
```

- [ ] **Step 7: Validate, test, type-check, run the suite**

Run:

```bash
claude plugin validate . && claude plugin test . && npx -y -p typescript@5 tsc -p <SCRATCH>/tsconfig.json && bash tests/run_all.sh 2>&1 | grep -E "^FAIL|ALL TESTS|SOME TESTS"
```

Expected: validation passes and lists `./narrator.ts hooks: session.start, session.end, prompt.submit, turn.step, tool.call, turn.complete` and calls including `process.run`, `session.id`, `session.cwd`, `ui.log`; every mod test passes; tsc prints nothing; `ALL TESTS PASSED`.

- [ ] **Step 8: Commit**

```bash
git add hooks/bridge.ts hooks/narrator.ts hooks/bridge.test.ts hooks/narrator.test.ts hooks/hooks.json tests/test_plugin_manifest.py
git commit -m "feat: the narrator mod - speak Claude's prose as it streams

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Docs and version

**Files:**
- Modify: `INTERFACES.md`, `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `.gitignore`

- [ ] **Step 1: INTERFACES.md**

Update every section the earlier tasks changed so the contract matches the code exactly:
- `voiceover/settings.py`: `detect_project_root` skips `~/.claude`.
- New `voiceover/projects.py`, `voiceover/lease.py`, `voiceover/narrator.py` sections with the signatures from Tasks 1, 4, 5.
- `voiceover/spool.py`: the per-project path, the `cwd` parameters, the item fields, `remove_legacy_queue`.
- `voiceover/speech.py`: `enqueue_speech` fields, `ensure_drainer` (per project, pre-warm), `prewarm`, `request_stop`, `project_lock_path`, `lock_is_live(path=None)`.
- `tts/engine_common.py`: the root-parameterized API, `drain(root, open_speaker, engine_names, lock_seconds, poll_seconds)`, the jump rule, speakers and playbacks, `lease_is_live`, `stop_request`, `engine_log`.
- `voiceover/process_utils.py`: one-shot-only patterns.
- hooks: the narrator paths' stand-down and lease table; `hooks/mod_bridge.py`'s op protocol; `hooks/narrator.ts`; `hooks.json` `modules`.

- [ ] **Step 2: README.md**

Add to the narrator-level section: narration now starts as Claude writes (with the mod, Claude Code 2.1.287 or newer; older builds fall back to the hooks); each project has its own queue and voice and may overlap another project; set a project's voice with `/voiceover:voice <name> --project`; a new message from Claude preempts the old one at the end of the current sentence; the engine is loaded only while a turn runs.

- [ ] **Step 3: CHANGELOG.md and version**

Rename `## Unreleased` to `## 1.4.0 - <today>` and add `### Added` / `### Changed` / `### Fixed` entries: streaming capture through the mod; per-project queue, voice and engine; sentence-boundary preemption; turn-scoped engine with pre-warm; `~/.claude` no longer a project marker; the one-shot interrupt no longer kills another project's narration. Note that sessions must restart (or `/reload-plugins`) after updating. Set `"version": "1.4.0"` in `.claude-plugin/plugin.json`. Add `.claude/voiceover.json` to `.gitignore`.

- [ ] **Step 4: Run everything**

Run: `bash tests/run_all.sh 2>&1 | tail -1 && claude plugin validate . && claude plugin test .`
Expected: `ALL TESTS PASSED`, validation passes, mod tests pass.

- [ ] **Step 5: Commit**

```bash
git add INTERFACES.md README.md CHANGELOG.md .claude-plugin/plugin.json .gitignore
git commit -m "docs: 1.4.0 - low-latency narration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Live acceptance (main session, with the user)

Runs in the MAIN session. It changes the user's machine settings, so every step that does needs the user's yes first.

- [ ] **Step 1: Set the two project voices (ask first)**

```bash
(cd /Users/hanan/Projects/claude-voiceover && python3 scripts/manage_voices.py set nicole --project)
(cd /Users/hanan/Projects/OrcaMCP && python3 /Users/hanan/Projects/claude-voiceover/scripts/manage_voices.py set emma --project)
```

Expected: each prints `Voice set to ... (project setting)`; `/Users/hanan/Projects/claude-voiceover/.claude/voiceover.json` and `/Users/hanan/Projects/OrcaMCP/.claude/voiceover.json` exist.

- [ ] **Step 2: Load the branch in a session (ask first)**

Disable the installed copy for the test, then start a terminal session on the branch:

```bash
claude plugin disable voiceover@claude-voiceover
claude --plugin-dir /Users/hanan/Projects/claude-voiceover
```

For the VS Code extension, the equivalent is `CLAUDE_CODE_PLUGIN_DIRS=/Users/hanan/Projects/claude-voiceover` in the `env` block of `~/.claude/settings.json`, followed by reopening the panel. Re-enable the installed copy (`claude plugin enable voiceover@claude-voiceover`) when done. Also check whether `/reload-plugins` alone loads the new module in a session that was already running; record the answer in the CHANGELOG's restart note.

- [ ] **Step 3: Measure**

With `debug_log` on (it is), work normally in that session until at least 50 mid-turn lines have been spoken, then run:

```bash
grep " sound " ~/.claude-voiceover/voiceover.log | sed -E 's/.*lag_shown=([0-9.]+).*/\1/;t;d' | sort -n \
  | awk '{a[NR]=$1} END {print "n=" NR, "median=" a[int(NR/2)+1], "p90=" a[int(NR*0.9)+1]}'
```

Pass: `n ≥ 50`, median ≤ 2.5, p90 ≤ 8.0. Then end a turn and run `sleep 3; pgrep -fl "kokoro_voice.py --drain"`: expected no output (the engine unloaded once the last sentence played and the turn's lease was released).

- [ ] **Step 4: Report**

Tell the user the measured median and p90 against the targets, whether the engine unloaded, whether `/reload-plugins` was enough, and anything that misbehaved. Do not merge or push without their go-ahead.
