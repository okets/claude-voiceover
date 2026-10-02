# tts/engine_common.py
"""Shared engine-side helpers for the claude-voiceover TTS engines.

Imported as a sibling by tts/kokoro_voice.py and tts/macos_say.py, both of
which resolve it through sys.path[0] (their own directory) whether they run
under `uv run` or plain python3.

This file MUST NOT import from voiceover/ - the engines live in a different
process and venv. It therefore duplicates the data-dir resolution by design,
but it is the ONLY copy: both engines import it rather than carrying their
own. Stdlib only.

Every lock and spool helper takes `root`, the directory its state lives in:
a project's directory for a drainer, data_dir() for the one-shot speaker's
lock.

The drainer plays a project's spool one sentence at a time. Between sentences
it applies the jump rule (a newer message ends the current one and drops what
was queued between), it honours per-session stop requests, it prefetches the
next sentence while one plays, and it claims its lock before open_speaker()
loads anything.

The spool it reads is written by voiceover/spool.py. The shared contract is
just this: pending items are `*.json` files in <root>/queue/ whose
filenames sort chronologically, each holding
{"text", "engine", "voice", "created", "session"}.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

_QUEUE_DIR_NAME = "queue"
_TAKEN_SUFFIX = ".taken"
_STOP_DIR_NAME = "stop"
_LEASE_DIR_NAME = "leases"
_SPAWNED_FILE_NAME = "spawned.json"
MAX_ENGINES = 2
_ALL_SESSIONS = "_all"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
POLL_SECONDS = 0.05
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_LOG_MAX_BYTES = 200_000
_debug = None


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


def _parse_lock_pid(raw):
    try:
        return int(json.loads(raw).get("pid"))
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    """True unless pid is known to have exited.

    Unknown answers are True: taking over a live engine's lock would put two
    voices on the speaker. Windows always answers unknown - os.kill(pid, 0)
    there would signal the process instead of probing it.
    """
    if pid is None or sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _lock_held(raw) -> bool:
    """True when a lock's content is unexpired AND its engine still runs."""
    expiry = _parse_lock_expiry(raw)
    if expiry is None or time.time() >= expiry:
        return False
    return _pid_alive(_parse_lock_pid(raw))


def lock_is_live(root) -> bool:
    """True while an unexpired lock held by a running engine exists. Read-only: never clears anything."""
    try:
        raw = tts_lock_path(root).read_text().strip()
    except OSError:
        return False
    return _lock_held(raw)


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
                    raw = lock_file.read_text().strip()
                except OSError:
                    raw = ""
                if _lock_held(raw):
                    return False  # a live engine is speaking
                try:
                    lock_file.unlink()  # expired, or its engine has died
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


def take_oldest(root, engine_names=None):
    """Claim the oldest item this engine can speak. (item, taken_path) or (None, None).

    With engine_names given, items belonging to ANOTHER engine are skipped -
    left untouched, so they keep both their place and their order - and the
    oldest item this engine owns is claimed instead. Skipping rather than
    stopping is what keeps a foreign item from blocking the whole queue:
    nothing ever starts the other engine on its behalf, because the hook
    safety net only ever spawns the engine resolved for the current project.

    The claim is an atomic rename to <name>.taken, so of two engines reading
    at once exactly one gets any given item. Unreadable items are discarded,
    and so - when engine_names is given - is an item with no "engine" at
    all: it is unattributable, no engine will ever claim it, so returning it
    would only have the caller put it back and reclaim the very same item
    next time round, forever. An item that names a real but different
    engine is not discarded; it is left for its own engine to claim, in
    both the pre-claim skip below and the post-claim check that catches one
    rewritten between the peek and the claim.
    """
    try:
        candidates = sorted(queue_dir(root).glob("*.json"))
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
        if engine_names is not None:
            engine = item.get("engine")
            if not engine:
                # Unattributable: no engine will ever claim it, so putting
                # it back would spin the caller's loop forever. Drop it
                # like corruption instead.
                finish(taken_path)
                continue
            if engine not in engine_names:
                # Rewritten between the peek above and this claim (or the
                # peek raced) - it belongs to a real, different engine.
                # Put it back keeping its place; that engine will pick it
                # up, and stopping here would silence this one until the
                # age cap.
                put_back(taken_path)
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


# --- sentences and the jump rule --------------------------------------------

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


def _own_pending(root, engine_names):
    """(path, item) for this engine's readable pending items, oldest first."""
    own = []
    for path in sorted(queue_dir(root).glob("*.json")):
        item = _read_json(path)
        if item is not None and _owned(item, engine_names):
            own.append((path, item))
    return own


def _survives(item, newest):
    """Whether the jump rule keeps item, given the newest own pending item."""
    if item is newest or item.get("sticky"):
        return True
    utterance = newest.get("utterance")
    return utterance is not None and item.get("utterance") == utterance


def drop_superseded(root, engine_names=None) -> int:
    """The jump rule: finish unspoken every own item a newer message supersedes.

    The newest own pending item's message is the one to speak; every older
    own item of another message is dropped unless it is sticky. Foreign items
    are not this engine's to judge and keep their place. Returns how many.
    """
    own = _own_pending(root, engine_names)
    if len(own) < 2:
        return 0
    newest = own[-1][1]
    dropped = 0
    for path, item in own[:-1]:
        if _survives(item, newest):
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


def peek_next(root, engine_names=None):
    """The item next_item() would play, read WITHOUT claiming it.

    Never claim ahead: a .taken item is invisible to spool.clear() and would
    survive the user's cutover."""
    own = _own_pending(root, engine_names)
    if not own:
        return None
    newest = own[-1][1]
    for _path, item in own:
        if _survives(item, newest):
            return item
    return None


def newer_utterance_pending(root, utterance, engine_names=None) -> bool:
    """True when an own pending item belongs to a different message."""
    for _path, item in _own_pending(root, engine_names):
        if utterance is None or item.get("utterance") != utterance:
            return True
    return False


# --- stop requests ------------------------------------------------------------

def _stop_file_name(session):
    """voiceover/projects.session_slug's rule; '_all' for no session."""
    slug = _UNSAFE.sub("_", str(session or ""))
    return (slug or _ALL_SESSIONS) + ".json"


def stop_requested(root, item) -> bool:
    """True when a stop for item's session - or for every session - was
    requested after item was queued. A stop can therefore never cut a reply
    queued after it, and each session's request is a file of its own."""
    try:
        created = float(item.get("created") or 0)
    except (TypeError, ValueError):
        created = 0.0
    directory = Path(root) / _STOP_DIR_NAME
    for name in {_stop_file_name(item.get("session")), _stop_file_name(None)}:
        request = _read_json(directory / name)
        try:
            if request and float(request.get("ts")) > created:
                return True
        except (TypeError, ValueError):
            continue
    return False


def lease_is_live(root) -> bool:
    """True while a session holds an unexpired lease on root's engine.

    Read-only: deleting an expired lease could race a hold() that has just
    replaced it with a fresh one (voiceover/lease.py prunes old leases)."""
    now = time.time()
    try:
        paths = list((Path(root) / _LEASE_DIR_NAME).glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            if float(json.loads(path.read_text()).get("expiry")) > now:
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return False


def _other_engines_running(root) -> int:
    """How many OTHER projects' engines hold their locks right now."""
    root = Path(root)
    count = 0
    try:
        siblings = [p for p in root.parent.iterdir() if p.is_dir() and p != root]
    except OSError:
        return 0
    for sibling in siblings:
        try:
            raw = tts_lock_path(sibling).read_text().strip()
        except OSError:
            continue
        if _lock_held(raw):
            count += 1
    return count


_FAILED_OPEN_BACKOFF = 27.0  # with the hooks' 3s window: no respawn for 30s


def _mark_spawned(root, ahead=0.0):
    """voiceover/speech.py's spawn marker: hooks start no engine for 3s after
    its timestamp. Written `ahead` seconds in the future to back off longer."""
    try:
        (Path(root) / _SPAWNED_FILE_NAME).write_text(json.dumps({"ts": time.time() + ahead}))
    except OSError:
        pass


def _clear_spawned(root):
    try:
        (Path(root) / _SPAWNED_FILE_NAME).unlink()
    except OSError:
        pass


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

    def prefetch(self, sentence, voice):
        pass

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


def _log_first_sound(item):
    now = time.time()
    shown = item.get("shown")
    text = item.get("text") or ""
    engine_log("sound", "source=%s lag_created=%.2f lag_shown=%s chars=%d :: %.60s" % (
        item.get("source") or "-", now - float(item.get("created") or now),
        ("%.2f" % (now - float(shown))) if isinstance(shown, (int, float)) else "-",
        len(text), text.replace("\n", " ")))


def _prefetch_next(root, speaker, engine_names) -> bool:
    """Have the speaker synthesize the next item's first sentence now.
    True once there was a next item to prefetch."""
    upcoming = peek_next(root, engine_names)
    if upcoming is None:
        return False
    first = split_sentences(upcoming.get("text"))
    if first:
        speaker.prefetch(first[0], upcoming.get("voice"))
    return True


def _speak_item(root, speaker, item, engine_names, keeper, poll_seconds):
    """Play item sentence by sentence until it ends, is stopped or superseded.

    A sticky item (a dialog alert, a permission request) plays whole: only a
    stop request - the user's own prompt - ends it early.
    """
    utterance, sticky = item.get("utterance"), bool(item.get("sticky"))
    sentences = split_sentences(item.get("text"))
    for index, sentence in enumerate(sentences):
        last = index == len(sentences) - 1
        playback = speaker.begin(sentence, item.get("voice"), None if last else sentences[index + 1])
        if index == 0:
            _log_first_sound(item)
        prefetched = not last  # within the item, begin() already prefetched
        while not playback.poll():
            keeper.keep()
            if stop_requested(root, item):
                playback.kill()
                engine_log("stop", "session=%s" % item.get("session"))
                return
            if not prefetched:
                prefetched = _prefetch_next(root, speaker, engine_names)
            time.sleep(poll_seconds)
        keeper.keep()
        if stop_requested(root, item):
            return
        if not sticky and newer_utterance_pending(root, utterance, engine_names):
            return


# --- the drain loop ---------------------------------------------------------

QUEUE_MAX_AGE_SECONDS = 300  # must match voiceover/spool.py


def drain(root, open_speaker, engine_names, lock_seconds=60.0,
          poll_seconds=POLL_SECONDS) -> int:
    """Speak root's spool one sentence at a time. Returns a process exit code.

    open_speaker() -> speaker | falsy is the engine's one-time setup (loading
    a speech model). It runs only AFTER the lock is ours: opening first would
    leave the lock free for the whole spin-up, and every hook firing in that
    window would spawn another engine that loads its model and then exits on
    the lock. A falsy speaker releases the lock and exits 0.

    Between sentences the jump rule applies: a different message ends the
    current one at its next sentence boundary and everything queued between
    is dropped, sticky items excepted. A stop request newer than the item
    kills its sentence mid-word - only the user's own prompt sends one. On an
    item's last sentence the next item's first sentence is prefetched.

    When the spool runs dry the lock is released and the spool checked ONCE
    more before exiting: that closes the race against a hook that appended an
    item a moment earlier and saw the lock still held. A lock this drain does
    not own is never deleted. While a session holds a lease the drain waits
    for more, polling every poll_seconds, so the model stays loaded for the
    turn's next sentence. At most MAX_ENGINES projects' engines run at once;
    another steps aside.
    """
    root = Path(root)
    if not try_claim_lock(root, lock_seconds):
        return 0  # another engine is already draining this project
    owns_lock = True
    keeper = _LockKeeper(root, lock_seconds)
    try:
        if _other_engines_running(root) >= MAX_ENGINES:
            # Two projects already speak: step aside. Refresh the spawn
            # marker so the hooks back off instead of respawning per call.
            engine_log("capped", "%d other engines running" % MAX_ENGINES)
            _mark_spawned(root)
            return 0
        speaker = open_speaker()
        if not speaker:
            # Setup failed (a missing or broken model): back the hooks off
            # for 30s rather than reloading on every call. The finally below
            # hands the lock back.
            _mark_spawned(root, ahead=_FAILED_OPEN_BACKOFF)
            return 0
        _clear_spawned(root)  # up and loaded: hooks may spawn again if this one dies
        # A model download, load and warm-up can outlast the lock's expiry,
        # and the keeper only refreshes once an item starts.
        update_lock_expiry(root, lock_seconds)
        keeper.reset()
        while True:
            item, taken_path = next_item(root, engine_names)
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
                # Fall through and speak the item just claimed: it is renamed
                # to .taken, invisible to a re-scan, and would be lost.
            age = time.time() - float(item.get("created") or 0)
            if age > QUEUE_MAX_AGE_SECONDS or stop_requested(root, item):
                finish(taken_path)  # too stale, or queued before the user's prompt
                continue
            keeper.keep()
            try:
                _speak_item(root, speaker, item, engine_names, keeper, poll_seconds)
            except Exception:
                pass  # a failed sentence skips the rest of its item
            finish(taken_path)
    finally:
        if owns_lock:
            remove_tts_lock(root)
