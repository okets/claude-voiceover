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


def _peek_engine(path):
    """The engine an item belongs to, read WITHOUT claiming it.

    None when the item cannot be read or names no engine - such an item has
    no owner, so the caller claims it and the corrupt-item handling drops it
    rather than leaving it to wedge the head of the queue.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            item = json.load(handle)
    except (OSError, ValueError):
        return None
    return item.get("engine") if isinstance(item, dict) else None


def take_oldest(engine_names=None):
    """Claim the oldest item this engine can speak. (item, taken_path) or (None, None).

    With engine_names given, items belonging to ANOTHER engine are skipped -
    left untouched, so they keep both their place and their order - and the
    oldest item this engine owns is claimed instead. Skipping rather than
    stopping is what keeps a foreign item from blocking the whole queue:
    nothing ever starts the other engine on its behalf, because the hook
    safety net only ever spawns the engine resolved for the current project.

    The claim is an atomic rename to <name>.taken, so of two engines reading
    at once exactly one gets any given item. Unreadable items are discarded
    and the next one is tried.
    """
    try:
        candidates = sorted(queue_dir().glob("*.json"))
    except OSError:
        return None, None
    for path in candidates:
        if engine_names is not None:
            engine = _peek_engine(path)
            if engine is not None and engine not in engine_names:
                continue  # someone else's item: leave it exactly where it is
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


# --- the drain loop ---------------------------------------------------------

QUEUE_MAX_AGE_SECONDS = 300  # must match voiceover/spool.py


def drain(speak_item, engine_names, lock_seconds=60.0, prepare=None) -> int:
    """Speak the spool dry, one item at a time. Returns a process exit code.

    speak_item(item) -> bool is called for each item this engine owns;
    engine_names is the set of item["engine"] values it can handle. Items
    belonging to any other engine are skipped and left in the spool with
    their ordering intact, so a foreign item never blocks this engine's own.

    prepare() is the engine's one-time setup - loading a speech model, say.
    It runs only AFTER the lock is ours, so the expensive part happens with
    every other engine already locked out; doing it first would leave the
    lock free for the whole spin-up, and every hook firing in that window
    would spawn another engine that loads its model and then exits on the
    lock. Returning falsy from prepare() releases the lock and exits 0.

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
        if prepare is not None and not prepare():
            return 0  # setup failed; the finally below hands the lock back
        while True:
            item, taken_path = take_oldest(engine_names)
            if item is None:
                remove_tts_lock()
                owns_lock = False
                item, taken_path = take_oldest(engine_names)
                if item is None:
                    return 0
                if not try_claim_lock(lock_seconds):
                    # Another engine claimed the lock in the gap - it will
                    # drain this item. Never delete a lock we do not own.
                    put_back(taken_path)
                    return 0
                owns_lock = True
                # Fall through and speak the item we just claimed. A continue
                # here would re-scan the spool, and this item is already
                # renamed to .taken - invisible to that scan, so it would be
                # silently lost.
            if item.get("engine") not in engine_names:
                # Defensive only: take_oldest() already filters by engine, so
                # reaching here needs the item to have been rewritten between
                # the peek and the claim. Put it back and move ON - stopping
                # here would silence this engine until the age cap.
                put_back(taken_path)   # someone else's engine; keep its place
                continue
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
