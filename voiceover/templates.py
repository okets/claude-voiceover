"""Narration templates for claude-voiceover.

Ports the proven phrase factories, truncation suite, and transcript
extraction helpers from the legacy smarter-claude hooks. Pure text in,
pure text out — no audio, no subprocesses. Stdlib only.
"""

import json
import random
import re
from pathlib import Path

from .settings import data_dir, get_interaction_level
from .transcript import (
    CycleStats,
    last_user_message,
    _assistant_blocks,
    _iter_entries_reversed,
)

_DEDUP_FILE_NAME = "dedup.json"


# ---------------------------------------------------------------------------
# Truncation suite (ported from cycle_utils)
# ---------------------------------------------------------------------------

def semantic_truncate(text, max_words=None, max_length=None,
                      flexibility=0.15, preserve_meaning=True):
    """Truncate at natural boundaries: sentences, punctuation, connectors."""
    if not text:
        return text
    if max_words is None:
        max_words = max(5, max_length // 5) if max_length is not None else 15

    words = text.split()
    if len(words) <= max_words:
        return text

    min_words = int(max_words * (1 - flexibility))
    max_flex = min(int(max_words * (1 + flexibility)), len(words) - 1)

    def char_pos_at(word_index):
        return len(" ".join(words[:word_index + 1]))

    # Priority 1: sentence boundaries and strong punctuation.
    for punct in (".", "!", "?"):
        for idx in range(max_flex, min_words - 1, -1):
            start = char_pos_at(idx - 1) if idx > 0 else 0
            pos = text.find(punct, start, min(char_pos_at(idx) + 1, len(text)))
            if pos != -1:
                return text[:pos + 1].strip()

    # Priority 2: phrase breaks.
    for punct in (" - ", " — ", ";", ","):
        for idx in range(max_flex, min_words - 1, -1):
            start = char_pos_at(idx - 1) if idx > 0 else 0
            pos = text.find(punct, start, min(char_pos_at(idx) + 1, len(text)))
            if pos != -1:
                return text[:pos].strip() + "..."

    # Priority 3: natural connectors.
    for idx in range(max_flex, min_words - 1, -1):
        if idx < len(words) and words[idx].lower() in ("and", "but", "or", "so", "yet"):
            return " ".join(words[:idx]).strip() + "..."

    # Priority 4: plain word-boundary cut.
    if preserve_meaning and len(words) > max_words * 2:
        return " ".join(words[:min_words]).strip() + "..."
    return " ".join(words[:max_words]).strip() + "..."


def truncate_user_intent(text, max_words=18):
    """Truncate a user request, dropping wrapping quotes first."""
    if not text:
        return text
    clean = text.strip()
    if len(clean) > 2 and clean.startswith('"') and clean.endswith('"'):
        clean = clean[1:-1]
    return semantic_truncate(clean, max_words=max_words, flexibility=0.2)


def truncate_to_words(text, max_words=15):
    """TTS truncation: soft word target, cut at the nearest punctuation.

    Ported from the legacy truncate_for_speech. Only truly long text is
    truncated; the cut lands on nearby punctuation when possible, otherwise
    on a word boundary with an ', etcetera' tail.
    """
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words * 1.5:  # 50% buffer before truncating at all
        return text

    target_pos = len(" ".join(words[:max_words]))
    window_start = max(0, target_pos - 50)
    window = text[window_start:min(len(text), target_pos + 100)]

    cut = _best_punctuation_cut(window, window_start, target_pos)
    max_cut = len(" ".join(words[:int(max_words * 1.5)]))
    min_cut = len(" ".join(words[:max(3, int(max_words * 0.6))]))
    if cut is not None and min_cut <= cut <= max_cut and cut < len(text):
        return text[:cut].strip()

    return " ".join(words[:max_words]).strip() + ", etcetera"


def _best_punctuation_cut(window, window_start, target_pos):
    """Nearest punctuation position to target_pos; strong marks win outright."""
    for group in ((".", "!", "?"), (";", ":", "-", ",")):
        best, best_distance = None, float("inf")
        for punct in group:
            for match in re.finditer(re.escape(punct), window):
                pos = window_start + match.start() + 1  # include the mark
                distance = abs(pos - target_pos)
                if distance < best_distance:
                    best, best_distance = pos, distance
        if best is not None:
            return best
    return None


def extract_action_and_subject(user_request):
    """Rule-based (action, subject) pair for phrasing notifications."""
    if not user_request:
        return "help", "with your task"
    if len(user_request.strip()) <= 50:
        return "help", "with: {}".format(user_request.strip())

    subject = truncate_to_words(user_request, max_words=16)
    lower = user_request.lower()
    action_patterns = [
        ("find", ("find", "search", "look")),
        ("fix", ("fix", "debug", "resolve")),
        ("test", ("test", "check", "verify")),
        ("implement", ("implement", "add", "create", "build")),
        ("handle git", ("commit", "git")),
    ]
    for action, needles in action_patterns:
        if any(needle in lower for needle in needles):
            return action, subject
    return "help", subject


def speakable_filename(path):
    """Make a filename TTS-friendly: 'a.py' -> 'a dot py', '_' -> ' '."""
    name = str(path).replace("\\", "/").rstrip("/").split("/")[-1]
    return name.replace(".", " dot ").replace("_", " ")


# ---------------------------------------------------------------------------
# Transcript extraction helpers (ported, rebuilt on the clean scanner)
# ---------------------------------------------------------------------------

def _iter_tool_uses_reversed(transcript_path):
    """Yield (name, input) of tool_use blocks, newest first."""
    for entry in _iter_entries_reversed(transcript_path):
        for block in reversed(_assistant_blocks(entry)):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name", ""), block.get("input") or {}


def extract_current_todos(transcript_path):
    """Current todo from the latest TodoWrite: in_progress, else next pending."""
    if not transcript_path:
        return None
    for name, tool_input in _iter_tool_uses_reversed(transcript_path):
        if name != "TodoWrite":
            continue
        todos = tool_input.get("todos", []) if isinstance(tool_input, dict) else []
        if not todos:
            return None
        for status in ("in_progress", "pending"):
            for todo in todos:
                if isinstance(todo, dict) and todo.get("status") == status:
                    return todo.get("content", "").strip() or None
        completed = sum(1 for t in todos
                        if isinstance(t, dict) and t.get("status") == "completed")
        if completed == len(todos):
            return "All {} tasks completed".format(len(todos))
        return None
    return None


def extract_bash_command_from_transcript(transcript_path):
    """Command word of the most recent Bash tool call (git keeps subcommand)."""
    if not transcript_path:
        return None
    for name, tool_input in _iter_tool_uses_reversed(transcript_path):
        if name != "Bash":
            continue
        return _command_word(tool_input.get("command", ""))
    return None


def _command_word(command):
    """First word of a shell command; 'git X' keeps the subcommand."""
    parts = (command or "").strip().split()
    if not parts:
        return None
    if parts[0].lower() == "git" and len(parts) > 1:
        return "git {}".format(parts[1])
    return parts[0]


def _extract_tool_from_trigger(trigger_message):
    """Tool name out of 'Claude needs your permission to use TOOL'."""
    if not trigger_message:
        return None
    for pattern in (r"permission to use (\w+)", r"permission for (\w+)"):
        match = re.search(pattern, trigger_message, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


_FILE_PATTERNS = [
    r"read\s+(?:the\s+)?file\s+([/\w\.-]+)",
    r"(/etc/[^\s\"']+)",
    r"(/var/[^\s\"']+)",
    r"(/usr/[^\s\"']+)",
    r"(/[^\s\"']+\.[a-zA-Z0-9]+)",
    r"([A-Za-z]:[/\\][^\s\"']+\.[a-zA-Z0-9]+)",
    r"access\s+([^\s\"']+\.[a-zA-Z0-9]+)",
    r"open\s+([^\s\"']+\.[a-zA-Z0-9]+)",
    r"edit\s+([^\s\"']+\.[a-zA-Z0-9]+)",
    r"modify\s+([^\s\"']+\.[a-zA-Z0-9]+)",
    r"check\s+([^\s\"']+\.[a-zA-Z0-9]+)",
    r"[\"']([^\"']*[/\\][^\"']*)[\"']",
    r"(/[^\s\"']+/[^\s\"']+)",
]


def _extract_file_from_request(user_request):
    """Filename mentioned in the user request, if any."""
    if not user_request:
        return None
    for pattern in _FILE_PATTERNS:
        match = re.search(pattern, user_request, re.IGNORECASE)
        if match:
            return match.group(1).replace("\\", "/").split("/")[-1]
    return None


_COMMAND_PATTERNS = [
    r"run\s+([a-zA-Z0-9_\-]+)",
    r"execute\s+([a-zA-Z0-9_\-]+)",
    r"([a-zA-Z0-9_\-]+)\s+command",
    r"use\s+([a-zA-Z0-9_\-]+)",
    r"\b(ls|git|npm|pip|python|node|yarn|docker|kubectl|ssh|curl|wget|grep|find"
    r"|cat|tail|head|ps|top|htop|vim|nano|mkdir|rmdir|rm|cp|mv|chmod|chown"
    r"|sudo|apt|yum|brew|make|cmake|gcc|javac|java|go|rust|cargo)\b",
    r"([a-zA-Z0-9_\-]+)\s+install",
    r"([a-zA-Z0-9_\-]+)\s+build",
    r"([a-zA-Z0-9_\-]+)\s+test",
    r"([a-zA-Z0-9_\-]+)\s+start",
]

_NON_COMMANDS = {"the", "and", "or", "but", "for", "with", "file",
                 "directory", "folder", "code", "script"}


def _extract_command_from_request(user_request):
    """Command word mentioned in the user request, if any."""
    if not user_request:
        return None
    for pattern in _COMMAND_PATTERNS:
        match = re.search(pattern, user_request, re.IGNORECASE)
        if match:
            command = match.group(1).lower()
            if command not in _NON_COMMANDS:
                return command
    return None


# ---------------------------------------------------------------------------
# Pre-tool announcements
# ---------------------------------------------------------------------------

_GENERIC_PRE_TOOL = {
    "Task": [
        "I'm creating a specialized agent", "I'm delegating this task",
        "I'm launching a subagent", "I'm spinning up an expert agent",
        "I'm creating a focused agent", "I'm deploying specialized help",
        "I'm starting a dedicated agent",
    ],
    "Glob": [
        "I'm finding files now", "I need to locate files",
        "I'm searching for files", "I'm looking for files",
        "I'm hunting for files", "I'm locating files now",
    ],
    "Grep": [
        "I'm searching for this now", "I need to find this pattern",
        "I'm looking for matches", "I'm scanning for this",
        "I'm searching through code", "I'm examining content",
    ],
    "Read": [
        "I need to read this now", "I'm checking this file",
        "I'm looking at this", "I'm reading through this now",
        "I'm examining this", "I'm reviewing this file",
    ],
    "Write": [
        "I'm writing this now", "I'm creating this file",
        "I need to write this", "I'm building this",
        "I'm putting this together", "I'm generating this",
    ],
    "Edit": [
        "I'm updating this now", "I need to modify this", "I'm changing this",
        "I'm fixing this", "I'm adjusting this now", "I'm refining this now",
    ],
    "WebFetch": [
        "I'm getting web data now", "I need to fetch this",
        "I'm retrieving this data", "I'm gathering web data",
    ],
    "WebSearch": [
        "I'm searching online now", "I need to search the web",
        "I'm looking this up online", "I'm researching online",
    ],
}

_READ_WITH_FILE = [
    "I need to read {0}", "I'm checking {0}", "I'm looking at {0}",
    "I'm reading {0} now", "I'm examining {0}", "I'm reviewing {0}",
    "I'm going through {0}", "I'm analyzing {0}",
]

_WRITE_WITH_FILE = [
    "I'm writing {0}", "I'm creating {0}", "I need to write {0}",
    "I'm building {0}", "I'm making {0}", "I'm generating {0}",
    "I'm composing {0}", "I'm constructing {0}",
]

_EDIT_WITH_FILE = [
    "I'm updating {0}", "I need to modify {0}", "I'm changing {0}",
    "I'm fixing {0}", "I need to edit {0}", "I'm adjusting {0}",
    "I'm improving {0}", "I'm revising {0}",
]

_BASH_TEMPLATES = [
    "I'm running {0} now", "I need to execute {0}", "I'm launching {0}",
    "I'm starting {0}", "I need to run {0}", "I'm executing {0} now",
    "Running {0}", "Executing {0}",
]

_WEB_SEARCH_TEMPLATES = [
    "I'm searching online for: {0}", "Looking up: {0}",
    "Searching the web for: {0}", "Finding information about: {0}",
    "Researching: {0}",
]

_WEB_FETCH_TEMPLATES = [
    "Fetching web content about: {0}", "Getting online data for: {0}",
    "Retrieving web information about: {0}", "Accessing online content for: {0}",
]

_EXIT_PLAN_TEMPLATES = [
    "You asked me to {0}. Please review my plan.",
    "You asked me to {0}. Care to review my plan?",
    "You asked me to {0}. My plan is ready for your approval.",
    "You asked me to {0}. What do you think of this approach?",
    "You asked me to {0}. Does this plan look good to you?",
]


def pre_tool_announcement(tool_name, tool_input, transcript_path):
    """Speakable line for a tool about to run, or None for nothing to say."""
    tool_input = tool_input or {}
    if tool_name == "TodoWrite":
        return _todo_announcement(tool_input)
    if tool_name == "Bash":
        return _bash_announcement(tool_input)
    if tool_name == "Read":
        return _file_announcement(tool_input, _READ_WITH_FILE, "Read")
    if tool_name == "Write":
        return _file_announcement(tool_input, _WRITE_WITH_FILE, "Write")
    if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        return _file_announcement(tool_input, _EDIT_WITH_FILE, "Edit")
    if tool_name == "WebSearch":
        return _query_announcement(tool_input.get("query"),
                                   _WEB_SEARCH_TEMPLATES, "WebSearch")
    if tool_name == "WebFetch":
        return _query_announcement(tool_input.get("prompt"),
                                   _WEB_FETCH_TEMPLATES, "WebFetch")
    if tool_name == "ExitPlanMode":
        return _exit_plan_announcement(transcript_path)
    if tool_name in _GENERIC_PRE_TOOL:
        return random.choice(_GENERIC_PRE_TOOL[tool_name])
    return None


def _todo_announcement(tool_input):
    todos = tool_input.get("todos", [])
    if not todos:
        return "Planning this task"
    for todo in todos:
        if todo.get("status") == "in_progress":
            return "Working on: {}".format(todo.get("content", "").strip())
    for todo in todos:
        if todo.get("status") == "pending":
            return "Next task: {}".format(todo.get("content", "").strip())
    completed = sum(1 for t in todos if t.get("status") == "completed")
    return "Planning tasks - {} of {} completed".format(completed, len(todos))


def _bash_announcement(tool_input):
    word = _command_word(tool_input.get("command", ""))
    if not word:
        return "I'm running this command now"
    return random.choice(_BASH_TEMPLATES).format(word)


def _file_announcement(tool_input, templates, generic_key):
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if file_path:
        return random.choice(templates).format(speakable_filename(file_path))
    return random.choice(_GENERIC_PRE_TOOL[generic_key])


def _query_announcement(query, templates, generic_key):
    if query:
        return random.choice(templates).format(truncate_to_words(query, max_words=20))
    return random.choice(_GENERIC_PRE_TOOL[generic_key])


def _exit_plan_announcement(transcript_path):
    intent = last_user_message(transcript_path) if transcript_path else None
    if intent:
        intent = truncate_user_intent(intent, max_words=15)
    else:
        intent = "help with this task"
    return random.choice(_EXIT_PLAN_TEMPLATES).format(intent)


# ---------------------------------------------------------------------------
# Post-tool announcements
# ---------------------------------------------------------------------------

_POST_DONE_SUFFIXES = ["- done", "- complete", "- finished", "- success", "- ready"]

_POST_FALLBACKS = {
    "Read": ["Read completed", "File processed", "Content analyzed",
             "File examined", "Reading finished"],
    "Write": ["Write completed", "File created", "Content written",
              "File saved successfully", "Writing finished"],
    "Edit": ["Edit completed", "Changes applied", "File modified",
             "Updates saved", "Editing finished"],
    "MultiEdit": ["Edit completed", "Changes applied", "File modified",
                  "Updates saved", "Editing finished"],
    "Bash": ["Command executed", "Script completed", "Execution finished",
             "Command finished", "Shell command done"],
    "Task": ["Task completed", "Agent finished", "Delegation successful",
             "Agent work done", "Task successful"],
    "Glob": ["File search completed", "Files located", "Search finished",
             "Files found", "Search complete"],
    "Grep": ["Text search completed", "Pattern found", "Search finished",
             "Pattern matching finished", "Grep complete"],
    "WebFetch": ["Web fetch completed", "Data retrieved", "Download finished",
                 "Web content retrieved", "Fetch complete"],
    "ExitPlanMode": ["Let's do this!", "Time to make it happen!",
                     "Let's get to work!", "Ready to dive in!",
                     "Let's ship it!", "Ready to execute!"],
}


def post_tool_announcement(tool_name, tool_input, tool_response):
    """Speakable line after a tool ran, or None (TodoWrite, dupes, unknowns)."""
    if tool_name == "TodoWrite":
        return None
    tool_input = tool_input or {}
    description = (tool_input.get("description") or "").strip()
    if description:
        if _is_duplicate_description(description):
            return None
        return "{} {}".format(description, random.choice(_POST_DONE_SUFFIXES))
    if tool_name in _POST_FALLBACKS:
        return random.choice(_POST_FALLBACKS[tool_name])
    return None


def _dedup_path():
    return data_dir() / _DEDUP_FILE_NAME


def _is_duplicate_description(description):
    """True when description matches the previous one; records it otherwise."""
    path = _dedup_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if json.load(handle).get("last_post_description") == description:
                return True
    except (OSError, ValueError):
        pass
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"last_post_description": description}, handle)
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Permission request notifications
# ---------------------------------------------------------------------------

_FALLBACK_MESSAGES = [
    "I need your input please", "Your input is needed",
    "Waiting for your guidance", "Ready for your next instruction",
    "What would you like me to do next?", "Awaiting your command",
    "I'm listening for your next request", "How can I help you today?",
]

_TOOL_PERMISSION = {
    "Read": ["May I read files?", "Permission needed to read files",
             "Can I access files?", "Should I go ahead and read files?"],
    "Write": ["May I write files?", "Permission needed to create files",
              "Can I write files?", "Should I go ahead and create files?"],
    "Edit": ["May I edit files?", "Permission needed to modify files",
             "Can I update files?", "Should I go ahead and edit files?"],
    "MultiEdit": ["May I edit multiple files?", "Permission needed to modify files",
                  "Can I update files?", "Should I go ahead and edit files?"],
    "Bash": ["May I execute bash commands?", "Permission needed to run commands",
             "Can I execute bash commands?", "Should I go ahead and run bash commands?"],
    "Task": ["May I create subtasks?", "Permission needed to delegate work",
             "Can I spawn agents?", "Should I go ahead and create subtasks?"],
    "Glob": ["May I search for files?", "Permission needed to find files",
             "Can I locate files?", "Should I go ahead and search files?"],
    "Grep": ["May I search file content?", "Permission needed to find text",
             "Can I search through files?", "Should I go ahead and search content?"],
    "WebFetch": ["May I fetch web content?", "Permission needed to access websites",
                 "Can I get web data?", "Should I go ahead and fetch web content?"],
}

_FILE_OP_PERMISSION = {
    "Read": ["May I read {0}?", "Permission needed to read {0}",
             "Can I access {0}?", "Should I go ahead and read {0}?"],
    "Write": ["May I write {0}?", "Permission needed to create {0}",
              "Can I write to {0}?", "Should I go ahead and create {0}?"],
    "Edit": ["May I edit {0}?", "Permission needed to modify {0}",
             "Can I update {0}?", "Should I go ahead and modify {0}?"],
    "MultiEdit": ["May I edit {0}?", "Permission needed to modify {0}",
                  "Can I update {0}?", "Should I go ahead and modify {0}?"],
}

_COMMAND_PERMISSION = [
    "May I execute {0}?", "Permission needed to run {0}",
    "Can I execute {0}?", "Should I go ahead and run {0}?",
]

_TODO_FILE_PERMISSION = [
    "Working on: {0} - may I {1} {2}?",
    "For task '{0}' - permission to {1} {2}?",
    "Current task: {0} - can I {1} {2}?",
]

_TODO_VERBS = {"Read": "read", "Write": "write", "Edit": "edit",
               "MultiEdit": "edit", "Bash": "run"}


def permission_request_message(payload):
    """'May I run git push?'-style line for a permission notification."""
    payload = payload or {}
    trigger = payload.get("message", "") or ""
    transcript_path = payload.get("transcript_path", "") or ""
    user_request = last_user_message(transcript_path) if transcript_path else None

    tool_name = _extract_tool_from_trigger(trigger)
    file_name = _extract_file_from_request(user_request)
    command = _extract_command_from_request(user_request)
    if tool_name and tool_name.lower() == "bash" and transcript_path:
        command = extract_bash_command_from_transcript(transcript_path) or command

    todo_message = _todo_aware_permission(
        tool_name, file_name, command, transcript_path)
    if todo_message:
        return todo_message
    if tool_name and tool_name.lower() == "bash" and command:
        return random.choice(_COMMAND_PERMISSION).format(command)
    if file_name and tool_name and tool_name.lower() != "bash":
        templates = _FILE_OP_PERMISSION.get(
            tool_name, ["May I use " + tool_name + " on {0}?"])
        return random.choice(templates).format(file_name)
    if tool_name:
        return random.choice(_TOOL_PERMISSION.get(tool_name, [
            "May I use {0}?".format(tool_name),
            "Permission needed to use {0}".format(tool_name),
            "Can I proceed with {0}?".format(tool_name),
        ]))
    if command:
        return random.choice(_COMMAND_PERMISSION).format(command)
    if file_name:
        return random.choice([
            "May I work with {0}?", "Permission needed to access {0}",
            "Can I proceed with {0}?",
        ]).format(file_name)
    return _generic_permission(user_request)


def _todo_aware_permission(tool_name, file_name, command, transcript_path):
    """Permission line carrying the current todo, when one is known."""
    current_todo = extract_current_todos(transcript_path)
    if not current_todo:
        return None
    todo_short = truncate_to_words(current_todo, max_words=10)
    if file_name and tool_name in _TODO_VERBS and tool_name != "Bash":
        verb = _TODO_VERBS[tool_name]
        return random.choice(_TODO_FILE_PERMISSION).format(
            todo_short, verb, file_name)
    if command and tool_name and tool_name.lower() == "bash":
        return random.choice(_TODO_FILE_PERMISSION).format(
            todo_short, "run", command)
    if tool_name:
        return random.choice([
            "Working on: {0} - may I use {1}?",
            "For task '{0}' - permission to use {1}?",
            "Current task: {0} - can I proceed with {1}?",
        ]).format(todo_short, tool_name)
    return None


def _generic_permission(user_request):
    if not user_request or not user_request.strip():
        return random.choice(_FALLBACK_MESSAGES)
    action, _subject = extract_action_and_subject(user_request)
    if action == "help":
        return random.choice([
            "May I proceed with your request?", "Permission needed to continue",
            "Can I go ahead with this task?", "Should I proceed with your request?",
            "Ready to help - may I continue?",
        ])
    return random.choice([
        "May I {0}?", "Permission needed to {0}", "Can I proceed to {0}?",
        "Should I go ahead and {0}?", "Ready to {0} - may I continue?",
    ]).format(action)


# ---------------------------------------------------------------------------
# Cycle completion
# ---------------------------------------------------------------------------

_COMPLETION_ENDINGS = ["Done.", "Complete.", "Ready.", "All set.", "Good."]
_COMPLETION_FALLBACKS = ["Task complete.", "All done!", "Finished.",
                         "Work complete.", "Task finished."]


def completion_message(stats):
    """Spoken summary of a finished request cycle.

    Simple cycles read the assistant's short closing response verbatim;
    everything else gets a templated stats summary.
    """
    if not isinstance(stats, CycleStats):
        return random.choice(_COMPLETION_FALLBACKS)
    response = (stats.final_response_text or "").strip()
    if stats.complexity == "simple" and 10 < len(response) < 300:
        return response
    return _templated_completion(stats)


def _templated_completion(stats):
    outcomes = _completion_outcomes(stats)
    ending = random.choice(_COMPLETION_ENDINGS)
    intent = None
    if stats.user_intent:
        intent = truncate_user_intent(stats.user_intent, max_words=8)
    if intent and outcomes:
        return "{}. {}. {}".format(intent, ", ".join(outcomes), ending)
    if outcomes:
        return "{}. {}".format(", ".join(outcomes), ending)
    if intent:
        return "You instructed me to: {}. Done!".format(intent)
    return random.choice(_COMPLETION_FALLBACKS)


def _completion_outcomes(stats):
    outcomes = []
    files = len(stats.files_modified)
    if files:
        outcomes.append("{} file{} updated".format(files, _s(files)))
    if stats.edit_count:
        outcomes.append("{} edit{}".format(stats.edit_count, _s(stats.edit_count)))
    if stats.todos_done:
        outcomes.append("{} task{} completed".format(
            stats.todos_done, _s(stats.todos_done)))
    return outcomes


def _s(count):
    return "" if count == 1 else "s"


# ---------------------------------------------------------------------------
# Subagent completion (manager-style)
# ---------------------------------------------------------------------------

_SUBAGENT_CONCISE = [
    "My agent handled {0}", "Subagent completed {0}",
    "Agent finished the work.", "Delegation successful.",
    "My helper got it done!", "Agent reported back.",
    "My unpaid intern delivered.",
]

_SUBAGENT_VERBOSE = [
    "I sent a subagent to handle {0} and they delivered excellent results",
    "My subagent did some digging on {0} and came back with solid findings",
    "Dispatched a specialist agent for {0} - mission accomplished",
    "My agent team completed {0} with great success",
    "Delegated {0} to a subagent who executed it perfectly",
    "My helper agent wrapped up {0} beautifully",
    "My specialist completed {0} with impressive efficiency",
    "My subagent tackled {0} and delivered outstanding work",
    "My field agent completed {0} and filed their report",
    "Sent my digital minion to do {0} - they work for free and never complain",
    "My robot employee finished {0} without asking for benefits or vacation time",
]


def subagent_completion_message(payload):
    """Manager-style 'my agent handled X' line for a SubagentStop event."""
    payload = payload or {}
    task = (payload.get("task") or payload.get("description")
            or payload.get("summary") or "the task")
    snippet = truncate_to_words(str(task), max_words=8)
    if get_interaction_level() == "verbose":
        return random.choice(_SUBAGENT_VERBOSE).format(snippet)
    return random.choice(_SUBAGENT_CONCISE).format(snippet)


def blocking_dialog_message(tool_name, tool_input):
    """Spoken alert for tools that BLOCK the session waiting on the user.

    This is the plugin's highest-value moment for someone away from the
    screen: it says out loud that input is needed and reads the actual
    question and its option labels."""
    if tool_name == "ExitPlanMode":
        return "I need your review: my plan is ready for approval."
    parts = ["I need your input."]
    for question in (tool_input.get("questions") or [])[:4]:
        if not isinstance(question, dict):
            continue
        text = (question.get("question") or "").strip()
        if text:
            parts.append(text)
        labels = [option.get("label") for option in (question.get("options") or [])
                  if isinstance(option, dict) and option.get("label")]
        if labels:
            parts.append("Options: " + ", ".join(labels) + ".")
    return " ".join(parts)
