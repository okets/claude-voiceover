"""Hierarchical settings for claude-voiceover.

Lookup order (first hit wins):
    project  <project-root>/.claude/voiceover.json
    global   data_dir()/settings.json
    DEFAULTS

Stdlib only. All functions fail soft: on any error they fall back to defaults.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "interaction_level": "concise",     # silent | quiet | concise | verbose | narrator
    "tts_engine": "auto",               # auto | kokoro | macos-female | macos-male | none
    "tts_enabled": True,
    "notification_sounds": True,        # quiet-mode mp3 pings
    "voice": None,                      # explicit kokoro voice id, else per-project hash
    "speak_subagent_completions": True, # verbose level only
    "debug_log": False,                 # write pipeline diagnostics to data_dir()/voiceover.log
}

LEVELS = ["silent", "quiet", "concise", "verbose", "narrator"]  # numeric aliases 0..4 accepted

# The 13 Kokoro voice ids, ported verbatim from the legacy kokoro_voice.py.
KOKORO_VOICES = [
    "af_alloy",
    "af_river",
    "af_sky",
    "af_sarah",
    "af_nicole",
    "am_adam",
    "am_echo",
    "am_puck",
    "am_michael",
    "bf_emma",
    "bm_daniel",
    "bm_lewis",
    "bm_george",
]

# Per-voice speed map, ported verbatim from the legacy VOICE_SETTINGS.
KOKORO_VOICE_SPEEDS = {
    "af_alloy": 1.1,
    "af_river": 1.1,
    "af_sky": 1.05,
    "af_sarah": 1.0,
    "af_nicole": 1.3,
    "am_adam": 1.0,
    "am_echo": 1.0,
    "am_puck": 0.94,
    "am_michael": 1.1,
    "bf_emma": 1.0,
    "bm_daniel": 1.3,
    "bm_lewis": 1.0,
    "bm_george": 1.2,
}

_GLOBAL_SETTINGS_NAME = "settings.json"
_PROJECT_SETTINGS_RELPATH = Path(".claude") / "voiceover.json"


def data_dir() -> Path:
    """Plugin state directory: $VOICEOVER_DATA_DIR override, else ~/.claude-voiceover/."""
    raw = os.environ.get("VOICEOVER_DATA_DIR", "").strip()
    directory = Path(raw) if raw else Path.home() / ".claude-voiceover"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def detect_project_root(cwd=None) -> Path:
    """Find the project root: nearest .claude dir, else git root, else cwd.

    Ported from the legacy cycle_utils.detect_project_root().
    """
    start = Path(cwd) if cwd else Path.cwd()
    for marker in (".claude", ".git"):
        found = _search_up_for(start, marker)
        if found is not None:
            return found
    return start


def _search_up_for(start: Path, marker: str):
    """Walk up from start looking for a directory named marker (depth-limited)."""
    current = start
    for _ in range(10):
        candidate = current / marker
        if candidate.is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _global_settings_path() -> Path:
    return data_dir() / _GLOBAL_SETTINGS_NAME


def _project_settings_path(cwd=None) -> Path:
    return detect_project_root(cwd) / _PROJECT_SETTINGS_RELPATH


def _load_json(path: Path) -> dict:
    """Load a JSON object from path, returning {} on any problem."""
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                return loaded
    except (OSError, ValueError):
        pass
    return {}


def get_setting(key: str, cwd=None):
    """Hierarchy lookup: project file -> global file -> DEFAULTS."""
    for source in (_load_json(_project_settings_path(cwd)),
                   _load_json(_global_settings_path())):
        if key in source:
            return source[key]
    return DEFAULTS.get(key)


def set_setting(key: str, value, scope: str = "global", cwd=None) -> bool:
    """Write one setting. scope 'global' or 'project'. Returns True on success."""
    if scope == "project":
        path = _project_settings_path(cwd)
    else:
        path = _global_settings_path()
    try:
        current = _load_json(path)
        current[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2)
        return True
    except OSError:
        return False


def _normalize_level(value) -> str:
    """Accept level names or numeric aliases 0..3; fall back to 'concise'."""
    if isinstance(value, bool):
        return DEFAULTS["interaction_level"]
    if isinstance(value, int) and 0 <= value < len(LEVELS):
        return LEVELS[value]
    if isinstance(value, str):
        text = value.strip().lower()
        if text in LEVELS:
            return text
        if text.isdigit() and 0 <= int(text) < len(LEVELS):
            return LEVELS[int(text)]
    return DEFAULTS["interaction_level"]


def get_interaction_level(cwd=None) -> str:
    """Current interaction level as a normalized string."""
    return _normalize_level(get_setting("interaction_level", cwd))


def level_at_least(minimum: str, cwd=None) -> bool:
    """True when the current level is at or above minimum (per LEVELS order)."""
    current = LEVELS.index(get_interaction_level(cwd))
    floor = LEVELS.index(_normalize_level(minimum))
    return current >= floor


def is_tts_enabled(cwd=None) -> bool:
    """TTS is on when tts_enabled is truthy and the level is not silent."""
    if get_interaction_level(cwd) == "silent":
        return False
    return bool(get_setting("tts_enabled", cwd))


def models_present() -> bool:
    """True when a Kokoro onnx model exists under ~/.kokoro-tts/models/."""
    try:
        models_dir = Path.home() / ".kokoro-tts" / "models"
        return any(models_dir.glob("*.onnx"))
    except OSError:
        return False


def resolve_engine(cwd=None) -> str:
    """Resolve the configured tts_engine to a concrete engine name.

    'auto': macOS -> 'macos-female'; otherwise 'kokoro' when models are
    installed, else 'none'.
    """
    engine = get_setting("tts_engine", cwd)
    if isinstance(engine, str):
        engine = engine.strip().lower()
    if engine in ("kokoro", "macos-female", "macos-male", "none"):
        return engine
    # 'auto' and anything unrecognized resolve automatically.
    if sys.platform == "darwin":
        return "macos-female"
    return "kokoro" if models_present() else "none"


def get_voice(cwd=None) -> str:
    """Kokoro voice id: explicit setting wins, else a stable per-project pick.

    The stable pick hashes the project root path so each project keeps the
    same "random" voice across sessions, without storing any state.
    """
    explicit = get_setting("voice", cwd)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    root = str(detect_project_root(cwd))
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()
    return KOKORO_VOICES[int(digest, 16) % len(KOKORO_VOICES)]
