"""Keeps the shared engine's version pointer and launcher shim fresh from the hooks.

refresh() installs <state>/bin/voiceover-engine and, when the pointer should move to this
plugin version, starts `engine_launcher prepare` detached (it builds the uv environment first
and only then replaces current.json). Fail-silent and rate-limited; stdlib only.

The pointer rule itself lives in tts/engine_paths.py, which the launcher's prepare shares.
"""

import os
import subprocess
import sys
import time

from pathlib import Path

from . import speech
from .settings import data_dir, get_machine_setting

sys.path.insert(0, str(speech._TTS_DIR))
import engine_paths  # noqa: E402  (the engine's stdlib-only path and pointer rules)

CHECK_INTERVAL_S = 60.0
_SHIM_NAME = "voiceover_engine_shim.py"
_SHIM_MODE = 0o755


def should_replace(current, candidate_root, candidate_version, override) -> bool:
    return engine_paths.should_replace(current, candidate_root, candidate_version, override)


def _windows() -> bool:
    return sys.platform == "win32"


def _due() -> bool:
    """True at most once per CHECK_INTERVAL_S; marks the check as made."""
    marker = engine_paths.engine_dir() / "pointer_checked"
    try:
        if time.time() - marker.stat().st_mtime < CHECK_INTERVAL_S:
            return False
    except OSError:
        pass
    marker.write_text(str(time.time()))
    return True


def _install_shim() -> None:
    """Put the launcher shim at <state>/bin/voiceover-engine (0755) when its bytes differ."""
    wanted = (speech._TTS_DIR / _SHIM_NAME).read_bytes()
    target = data_dir() / "bin" / "voiceover-engine"
    try:
        if target.read_bytes() == wanted:
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name("%s.%d.tmp" % (target.name, os.getpid()))
    temp.write_bytes(wanted)
    os.chmod(str(temp), _SHIM_MODE)
    os.replace(str(temp), str(target))


def _candidate_root(plugin_root: Path, override) -> Path:
    return Path(override) if override else Path(plugin_root)


def _project_for(root: Path) -> Path:
    """The uv project the device setting needs, inside `root`."""
    return root / speech.kokoro_project().name


def _spawn_prepare(root: Path, project: Path) -> None:
    command = [sys.executable, str(root / "tts" / "engine_launcher.py"), "prepare", str(root),
               "--project", str(project)]
    with open(engine_paths.log_path(), "ab") as log:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def _prepare_if_needed(plugin_root: Path) -> bool:
    override = get_machine_setting("engine_plugin_root")
    root = _candidate_root(plugin_root, override)
    if not should_replace(engine_paths.read_pointer(), root, engine_paths.plugin_version(root), override):
        return False
    if os.environ.get("VOICEOVER_NO_SPAWN"):
        return False
    _spawn_prepare(root, _project_for(root))
    return True


def refresh(plugin_root: Path = speech._PLUGIN_ROOT) -> bool:
    """Install the shim and start prepare when the pointer should move; True when it spawned.
    Never raises, does nothing on Windows, and runs at most once a minute."""
    try:
        if _windows() or not _due():
            return False
        _install_shim()
        return _prepare_if_needed(plugin_root)
    except Exception:
        return False
