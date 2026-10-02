"""Narrator-level actions shared by the classic hooks.

The hooks parse their own input and call these, so what a prompt, a question
dialog or a turn's end does to the queue, the lease and the engine has
exactly one definition (the deferred mod's bridge will call the same ones).
Per-session records live under data_dir(): pins/ (the project a session's
turn stays in), dialog_alert/ (a dialog was just announced) and
mod_sessions/ (the mod narrates this session; the classic paths stand
down).
Stdlib only; every function fails soft.
"""

import json
import os
import time

from . import lease, spool
from .debuglog import log as _log
from .projects import project_key, session_slug
from .prose import skip_unread
from .settings import data_dir, get_interaction_level, is_tts_enabled, resolve_engine
from .speech import enqueue_speech, ensure_drainer, play_sound, prewarm, request_stop
from .templates import blocking_dialog_message

DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")

_TASK_NOTIFICATION_PREFIX = "<task-notification>"
_RECORD_MAX_AGE_SECONDS = 7 * 86400


def headless() -> bool:
    """True in a headless run (claude -p, the Agent SDK): nobody is listening.

    Claude Code sets CLAUDE_CODE_ENTRYPOINT only when it is absent, so a run
    started from inside another session inherits that session's value and is
    not recognized; the engine's MAX_ENGINES cap bounds that case.
    """
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-")


def narrates(cwd=None) -> bool:
    """True when this project speaks Claude's prose, in this session."""
    return (get_interaction_level(cwd) == "narrator" and is_tts_enabled(cwd)
            and resolve_engine(cwd) != "none" and not headless())


# --- per-session records ----------------------------------------------------------

def _record(kind, session):
    slug = session_slug(session)
    return data_dir() / kind / slug if slug else None


def _write_record(kind, session, text) -> None:
    """Write session's record of this kind; prune week-old ones of the kind."""
    path = _record(kind, session)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _RECORD_MAX_AGE_SECONDS
        for old in path.parent.iterdir():
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        path.write_text(text)
    except OSError:
        pass


def _read_record(kind, session):
    path = _record(kind, session)
    try:
        return path.read_text() if path is not None else None
    except OSError:
        return None


def _drop_record(kind, session) -> None:
    path = _record(kind, session)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def pin_cwd(session, cwd) -> None:
    """Keep this session's turn in cwd's project, wherever it cds to."""
    if cwd:
        _write_record("pins", session, str(cwd))


def repin(session, cwd):
    """A prompt starts a turn in cwd's project: let go of any lease the
    session still holds in a previously pinned project (an interrupted turn
    never reached Stop), then pin cwd. Every prompt re-pins, at every level,
    so a stale pin never decides another project's narration.

    Returns the previously pinned cwd when its PROJECT differs from cwd's -
    the caller then abandons the old turn there too - else None. A cd into
    a subdirectory of the same project moves nothing."""
    previous = pinned_cwd(session, None)
    moved = None
    if previous and project_key(previous) != project_key(cwd):
        lease.release(previous, session)
        moved = previous
    pin_cwd(session, cwd)
    return moved


def pinned_cwd(session, cwd):
    """The session's pinned cwd, else cwd."""
    return _read_record("pins", session) or cwd


def unpin(session) -> None:
    _drop_record("pins", session)


def mod_active(session) -> bool:
    """True when the mod narrates this session, so the classic paths stand down."""
    return _read_record("mod_sessions", session) is not None


def mark_mod_session(session) -> None:
    """Record that the mod narrates session (hooks/mod_bridge.py, every call)."""
    _write_record("mod_sessions", session, "")


def unmark_mod_session(session) -> None:
    _drop_record("mod_sessions", session)


def stands_down(session, transcript_path) -> bool:
    """True when the mod narrates this session: the classic path does nothing
    but move the prose cursor past what the mod already spoke, so a later
    fallback never replays it."""
    if not mod_active(session):
        return False
    skip_unread(transcript_path)
    return True


def is_users_prompt(prompt_text) -> bool:
    """False for a background task's notification arriving as a prompt."""
    return not str(prompt_text or "").lstrip().startswith(_TASK_NOTIFICATION_PREFIX)


# --- the actions -----------------------------------------------------------------

def abandon(cwd, session) -> int:
    """Drop this session's pending text in cwd's project and ask its engine
    to stop the sentence mid-word. Clear before stop, so the engine cannot
    claim one more item of the abandoned turn in between. Without a session
    id nothing is cleared or stopped - an unscoped clear or stop would cut
    other sessions off. Returns how many pending items were dropped."""
    if not session:
        return 0
    dropped = spool.clear(session=session, cwd=cwd)
    _log("cutover", "dropped=%d pending items on a new prompt" % dropped, cwd)
    request_stop(cwd, session)
    return dropped


def cutover(cwd, session, ttl=lease.LEASE_TTL, warm=True) -> None:
    """The user's own new prompt: drop this session's pending text, stop its
    sentence mid-word, and warm the engine for the reply. No process is
    killed: the engine stays loaded and the reply starts warm. warm=False
    leaves the warm-up to the caller: the mod warms at turn.start, so a
    prompt that never becomes a turn holds nothing."""
    abandon(cwd, session)
    if warm and narrates(cwd):
        prewarm(cwd, session, ttl=ttl)


def write_dialog_marker(session) -> None:
    """Tell notification.py this session's blocking dialog was just
    announced, so its "needs permission" echo stays quiet."""
    _write_record("dialog_alert", session, json.dumps({"ts": time.time()}))


def dialog_recently_announced(session, within=30.0) -> bool:
    raw = _read_record("dialog_alert", session)
    try:
        return raw is not None and time.time() - float(json.loads(raw).get("ts", 0)) < within
    except (ValueError, TypeError, AttributeError):
        return False


def queue_dialog(cwd, session, tool_name, tool_input, lead_in=None, shown=None,
                 utterance=None) -> bool:
    """Announce a dialog that is about to block the session on the user.

    The alert is sticky - never dropped, played whole - and the lease is
    released, so the engine unloads once the alert has played while the user
    decides. The marker is written only when the alert actually queued:
    otherwise the notification echo is the only thing that will speak. The
    mod passes the lead-in's utterance, so the alert continues that message
    instead of preempting it.
    """
    alert = blocking_dialog_message(tool_name, tool_input or {})
    text = (lead_in + "\n" + alert) if lead_in else alert
    queued = enqueue_speech(text, cwd=cwd, full=True, session=session, sticky=True,
                            shown=shown, source="dialog", utterance=utterance)
    if queued:
        write_dialog_marker(session)
    ensure_drainer(cwd)
    lease.release(cwd, session)
    return queued


def dialog_answered(cwd, session, ttl=lease.LEASE_TTL) -> None:
    """The user answered: hold the engine again for the rest of the turn."""
    if narrates(cwd):
        prewarm(cwd, session, ttl=ttl)


def turn_end(cwd, session, spoken) -> None:
    """The turn is over: let the engine go once its queue has played. A turn
    whose last step said nothing ends on the decide chime instead."""
    lease.release(cwd, session)
    if not spoken:
        play_sound("decide", cwd=cwd)
