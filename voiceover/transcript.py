"""Read-only transcript scanning for claude-voiceover.

Replaces the legacy hook_parser/JSONL pipeline with a single backward scan
over the Claude Code transcript (one JSON envelope per line).

Envelope shapes we care about:
    {"type": "user",      "message": {"role": "user", "content": <str|list>}, ...}
    {"type": "assistant", "message": {"role": "assistant", "content": [blocks]}, ...}

Assistant content blocks: {"type": "text", "text": ...} and
{"type": "tool_use", "name": ..., "input": {...}}.

Meta/command user messages (isMeta, <command-name> wrappers, tool results,
subagent prompts, sidechain entries) are skipped when finding the "real"
last user message.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

_META_CONTENT_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "<task-notification>",
)

_SUBAGENT_PROMPT_PREFIX = "You are a subagent"


@dataclass
class CycleStats:
    """What happened between the last real user message and now."""

    files_modified: List[str] = field(default_factory=list)
    edit_count: int = 0
    todos_done: int = 0
    final_response_text: Optional[str] = None
    user_intent: Optional[str] = None
    complexity: str = "simple"  # simple | moderate | complex


def _iter_entries_reversed(transcript_path):
    """Yield parsed JSON envelopes from the transcript, newest first."""
    path = Path(transcript_path)
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _user_text(entry) -> Optional[str]:
    """Return the text of a real user message, or None if it is not one."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = _flatten_user_content(message.get("content"))
    if not text:
        return None
    stripped = text.strip()
    if not stripped or stripped.startswith(_META_CONTENT_PREFIXES):
        return None
    if stripped.startswith(_SUBAGENT_PROMPT_PREFIX):
        return None
    return stripped


def _flatten_user_content(content) -> Optional[str]:
    """String content passes through; list content joins text blocks only.

    A list containing any tool_result block is a tool result envelope,
    not something the user typed, so it yields None.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        return " ".join(texts) if texts else None
    return None


def _assistant_blocks(entry) -> list:
    """Content blocks of an assistant envelope (empty list otherwise)."""
    if entry.get("type") != "assistant":
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _text_of_blocks(blocks) -> str:
    texts = [b.get("text", "") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"]
    return " ".join(t for t in texts if t).strip()


def _count_completed_todos(tool_input) -> int:
    todos = tool_input.get("todos", []) if isinstance(tool_input, dict) else []
    return sum(1 for t in todos
               if isinstance(t, dict) and t.get("status") == "completed")


def last_user_message(transcript_path) -> Optional[str]:
    """The most recent real user message, or None."""
    for entry in _iter_entries_reversed(transcript_path):
        text = _user_text(entry)
        if text is not None:
            return text
    return None


def cycle_stats(transcript_path) -> CycleStats:
    """Scan backwards to the last real user message and summarize the cycle."""
    stats = CycleStats()
    files = []
    subagent_used = False
    saw_last_todo_write = False

    for entry in _iter_entries_reversed(transcript_path):
        text = _user_text(entry)
        if text is not None:
            stats.user_intent = text
            break
        if entry.get("isSidechain"):
            continue  # subagent-internal traffic
        for block in _assistant_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and stats.final_response_text is None:
                joined = _text_of_blocks(_assistant_blocks(entry))
                if joined:
                    stats.final_response_text = joined
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                tool_input = block.get("input") or {}
                if name in _EDIT_TOOLS:
                    stats.edit_count += 1
                    _collect_file_path(files, tool_input)
                elif name == "TodoWrite" and not saw_last_todo_write:
                    # First TodoWrite seen backwards = latest todo state.
                    stats.todos_done = _count_completed_todos(tool_input)
                    saw_last_todo_write = True
                elif name == "Task":
                    subagent_used = True

    stats.files_modified = list(reversed(files))  # chronological order
    stats.complexity = _assess_complexity(
        len(stats.files_modified), stats.edit_count, subagent_used)
    return stats


def _collect_file_path(files, tool_input) -> None:
    if not isinstance(tool_input, dict):
        return
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if path and path not in files:
        files.append(path)


def _assess_complexity(file_count, edit_count, subagent_used) -> str:
    if file_count >= 4 or subagent_used:
        return "complex"
    if file_count <= 1 and edit_count <= 2:
        return "simple"
    return "moderate"
