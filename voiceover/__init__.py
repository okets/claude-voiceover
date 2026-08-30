"""claude-voiceover core library.

Stdlib-only TTS narration for Claude Code hooks. Heavy TTS engines live in
the sibling tts/ directory and are spawned as detached subprocesses.
"""

from .settings import (
    DEFAULTS,
    KOKORO_VOICES,
    KOKORO_VOICE_SPEEDS,
    LEVELS,
    data_dir,
    detect_project_root,
    get_interaction_level,
    get_setting,
    get_voice,
    is_tts_enabled,
    level_at_least,
    models_present,
    resolve_engine,
    set_setting,
)
from .speech import (
    lock_path,
    play_sound,
    speak,
    stop_speech,
    truncate_for_speech,
)
from .templates import (
    completion_message,
    permission_request_message,
    post_tool_announcement,
    pre_tool_announcement,
    speakable_filename,
    subagent_completion_message,
)
from .transcript import (
    CycleStats,
    cycle_stats,
    last_user_message,
)

__version__ = "1.0.0"

__all__ = [
    "DEFAULTS",
    "KOKORO_VOICES",
    "KOKORO_VOICE_SPEEDS",
    "LEVELS",
    "CycleStats",
    "completion_message",
    "cycle_stats",
    "data_dir",
    "detect_project_root",
    "get_interaction_level",
    "get_setting",
    "get_voice",
    "is_tts_enabled",
    "last_user_message",
    "level_at_least",
    "lock_path",
    "models_present",
    "permission_request_message",
    "play_sound",
    "post_tool_announcement",
    "pre_tool_announcement",
    "resolve_engine",
    "set_setting",
    "speak",
    "speakable_filename",
    "stop_speech",
    "subagent_completion_message",
    "truncate_for_speech",
]
