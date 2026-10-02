"""The narration queue for claude-voiceover - one per project.

One directory per project (projects.project_dir(cwd)/queue/), one file per
pending utterance. Filenames start with a zero-padded millisecond timestamp,
so listing them in name order lists them in the order they were queued.
Writers create a temp file and rename it into place, so a reader never
observes a half-written item.

Items: {"text", "engine", "voice", "created", "session", "utterance",
"sticky", "shown", "source"}. Items sharing an "utterance" are one message:
the engine plays a message's items in order and lets a newer message preempt
it at a sentence boundary. "sticky" items (dialog alerts, permission
requests) are never skipped and play whole. "shown" is when the text
appeared on screen and "source" the hook that queued it - both for latency
logging - or None.

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
            shown=None, source=None, cwd=None) -> bool:
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
            "source": source,
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


def clear_sticky(session, cwd=None) -> int:
    """Retire a session's pending alerts and permission requests: a tool has
    finished, so whatever they asked has been answered. Returns how many."""
    if not session:
        return 0
    removed = 0
    for path in _items(cwd):
        item = _read(path)
        if item is None or not item.get("sticky") or item.get("session") != session:
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
