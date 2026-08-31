"""Opt-in diagnostic logging for the narration pipeline.

Enabled by the 'debug_log' setting. Each line: unix-time pid tag message.
The log is truncated when it outgrows ~200KB so it can stay on forever.
"""

import os
import time

from .settings import data_dir, get_setting

_MAX_BYTES = 200_000


def log(tag, message, cwd=None):
    try:
        if not get_setting("debug_log", cwd):
            return
        path = data_dir() / "voiceover.log"
        try:
            if path.stat().st_size > _MAX_BYTES:
                path.unlink()
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%.2f %d %-10s %s\n" % (time.time(), os.getpid(), tag, message))
    except Exception:
        pass
