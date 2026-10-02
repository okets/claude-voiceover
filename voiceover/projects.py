"""Project identity for the per-project narration state.

Every project - the root settings.detect_project_root() finds, the same root
the voice setting resolves against - gets its own directory under data_dir():
its queue, its engine lock, its leases and its stop requests live there, so a
project's queue and its voice always agree. Engines are handed that directory
and never compute a key themselves. Stdlib only.
"""

import hashlib
import re
from pathlib import Path

from .settings import data_dir, detect_project_root

_PROJECTS_DIR_NAME = "projects"
_ROOT_FILE_NAME = "root.txt"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def project_root(cwd=None) -> Path:
    """The project root for cwd, resolved."""
    root = detect_project_root(cwd)
    try:
        return root.resolve()
    except OSError:
        return root


def project_key(cwd=None) -> str:
    """12 hex characters naming the project's directory."""
    return _key_for(project_root(cwd))


def project_dir(cwd=None) -> Path:
    """data_dir()/projects/<key>/, created on demand, labelled by root.txt."""
    root = project_root(cwd)
    directory = data_dir() / _PROJECTS_DIR_NAME / _key_for(root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        label = directory / _ROOT_FILE_NAME
        if not label.exists():
            label.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        pass
    return directory


def session_slug(session):
    """A session id made safe for a file name, or None for no session.

    Names every per-session state file (leases, stop requests, markers).
    tts/engine_common.py applies the same rule to the stop files it reads.
    """
    slug = _UNSAFE.sub("_", str(session or ""))
    return slug or None


def _key_for(root: Path) -> str:
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
