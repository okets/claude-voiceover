"""Prose tailing for the 'narrator' interaction level.

Narrator mode speaks Claude's ACTUAL words from the session transcript -
main-thread assistant text only - instead of tool play-by-play. Hooks call
peek_new_prose() to read everything written since the last narration, speak
it, and commit_offset() only when the utterance was actually dispatched, so
text that lost a race against playing audio is retried at the next hook.

State: data_dir()/prose_state.json maps transcript path -> byte offset of
the last fully-narrated line. Transcripts are append-only JSONL, so a byte
offset is a complete, restart-safe cursor. Stdlib only.
"""

import json
import re
from pathlib import Path

from .settings import data_dir

_STATE_FILE_NAME = "prose_state.json"
_MAX_TRACKED_TRANSCRIPTS = 20


def peek_new_prose(transcript_path):
    """Return (speech_text, new_offset) for unread main-thread assistant text.

    speech_text is None when there is nothing new to say. The stored offset
    is NOT advanced - call commit_offset(transcript_path, new_offset) after
    the text was actually spoken."""
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return None, 0
        start = _read_offset(str(path))
        size = path.stat().st_size
        if start is None:
            # First time narrating this transcript: start from NOW, not from
            # the whole session's backlog.
            commit_offset(str(path), size)
            return None, size
        if size < start:
            start = 0  # transcript replaced/truncated - start over
        with open(path, "rb") as handle:
            handle.seek(start)
            blob = handle.read()
        # Only consume complete lines; a line still being written stays unread.
        last_newline = blob.rfind(b"\n")
        if last_newline < 0:
            return None, start
        new_offset = start + last_newline + 1
        texts = []
        for line in blob[: last_newline + 1].splitlines():
            text = _assistant_text(line)
            if text:
                texts.append(text)
        speech = clean_for_speech("\n\n".join(texts)) if texts else None
        return (speech or None), new_offset
    except Exception:
        return None, 0


def commit_offset(transcript_path, offset) -> None:
    """Remember that everything before offset has been narrated."""
    try:
        state = _load_state()
        state[str(transcript_path)] = int(offset)
        # Keep the state file from growing without bound across sessions.
        if len(state) > _MAX_TRACKED_TRANSCRIPTS:
            for key in sorted(state)[: len(state) - _MAX_TRACKED_TRANSCRIPTS]:
                del state[key]
        state_path = data_dir() / _STATE_FILE_NAME
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except Exception:
        pass


def _read_offset(transcript_path):
    """Stored offset, or None when this transcript was never narrated."""
    state = _load_state()
    if transcript_path not in state:
        return None
    try:
        return int(state[transcript_path])
    except (TypeError, ValueError):
        return 0


def _load_state() -> dict:
    try:
        with open(data_dir() / _STATE_FILE_NAME, encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _assistant_text(line):
    """Main-thread assistant prose from one transcript JSONL line, or None."""
    try:
        entry = json.loads(line)
    except Exception:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain") or entry.get("isMeta"):
        return None
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [
        block.get("text", "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = "\n\n".join(part for part in parts if part)
    return joined or None


# --- markdown -> speakable text ---------------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_UNCLOSED_FENCE_RE = re.compile(r"```.*\Z", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADER_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_HRULE_RE = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)


def clean_for_speech(markdown_text) -> str:
    """Markdown prose -> something worth hearing.

    Code blocks become a brief spoken cue; tables, rules, and link targets
    disappear; emphasis and header markup is stripped to plain sentences."""
    text = markdown_text
    text = _CODE_FENCE_RE.sub(" ...code snippet... ", text)
    text = _UNCLOSED_FENCE_RE.sub(" ...code snippet... ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _TABLE_ROW_RE.sub("", text)
    text = _HRULE_RE.sub("", text)
    text = _HEADER_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()
