# tests/test_engine_paths.py
"""Engine state paths, auth token, machine settings, pointer and keep-off record."""

import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

data_dir = isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import engine_paths
import manage_settings
from voiceover import settings


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def write_settings(values):
    (data_dir / "settings.json").write_text(json.dumps(values))


def rejected(key, value):
    try:
        manage_settings.normalize(key, value)
        return False
    except ValueError:
        return True


# --- folder and files ---------------------------------------------------------
folder = engine_paths.engine_dir()
check("engine dir lives under the data dir", folder == data_dir / "engine")
check("engine dir is 0700", mode(folder) == 0o700)
os.chmod(folder, 0o755)
engine_paths.engine_dir()
check("an existing engine dir is chmod-ed back to 0700", mode(folder) == 0o700)
names = {fn.__name__: fn().name for fn in (
    engine_paths.socket_path, engine_paths.lock_path, engine_paths.start_lock_path,
    engine_paths.token_path, engine_paths.pointer_path, engine_paths.keep_off_path,
    engine_paths.log_path)}
check("file names", names == {
    "socket_path": "engine.sock", "lock_path": "engine.lock", "start_lock_path": "start.lock",
    "token_path": "token", "pointer_path": "current.json", "keep_off_path": "keep_off.json",
    "log_path": "engine.log"}, names)

# --- token --------------------------------------------------------------------
token = engine_paths.read_or_create_token()
check("token is 64 hex chars", len(token) == 64 and all(c in "0123456789abcdef" for c in token))
check("token file is 0600", mode(engine_paths.token_path()) == 0o600)
check("token is stable", engine_paths.read_or_create_token() == token)
rotated = engine_paths.rotate_token()
check("rotation changes the token", rotated != token and engine_paths.read_or_create_token() == rotated)
check("rotated token file is 0600", mode(engine_paths.token_path()) == 0o600)

for garbage in ("", "not-a-token", "A" * 64):
    engine_paths.token_path().write_text(garbage)
    repaired = engine_paths.read_or_create_token()
    check("bad token file %r is repaired" % garbage[:6],
          len(repaired) == 64 and all(c in "0123456789abcdef" for c in repaired)
          and engine_paths.token_path().read_text() == repaired and mode(engine_paths.token_path()) == 0o600)
check("no temp files left in the engine dir", sorted(p.name for p in engine_paths.engine_dir().iterdir()) == ["token"])

# --- settings -----------------------------------------------------------------
check("idle defaults to 60 s", engine_paths.idle_unload_seconds() == 60)
write_settings({"engine_port": 47320})
check("idle is 330 s with a port", engine_paths.idle_unload_seconds() == 330)
check("settings are read uncached", engine_paths.engine_port() == 47320)
write_settings({"engine_port": 47320, "engine_idle_s": 12})
check("engine_idle_s wins", engine_paths.idle_unload_seconds() == 12)
write_settings({"engine_port": 70000})
check("out-of-range port is None", engine_paths.engine_port() is None)
(data_dir / "settings.json").write_text("not json")
check("unreadable settings -> {}", engine_paths.read_global_settings() == {})

# --- keep-off -----------------------------------------------------------------
check("no keep-off record at first", engine_paths.read_keep_off() is None)
engine_paths.write_keep_off(30, "release")
record = engine_paths.read_keep_off()
check("keep-off returns the record", record is not None and record["reason"] == "release")
engine_paths.write_keep_off(-1, "release")
check("an elapsed keep-off reads as None", engine_paths.read_keep_off() is None)

# --- pointer ------------------------------------------------------------------
check("no pointer at first", engine_paths.read_pointer() is None)
info = {"pid": 123, "port": 47320, "socket": "x"}
engine_paths.write_pointer(info)
check("pointer round-trips", engine_paths.read_pointer() == info)

# --- machine settings ---------------------------------------------------------
check("the three engine keys are machine settings",
      all(k in settings.MACHINE_SETTINGS for k in ("engine_port", "engine_idle_s", "engine_plugin_root")))
check("the three engine keys default to None",
      all(settings.DEFAULTS[k] is None for k in ("engine_port", "engine_idle_s", "engine_plugin_root")))
check("valid port accepted", manage_settings.normalize("engine_port", 47320) == 47320)
check("null accepted", all(manage_settings.normalize(k, None) is None
                           for k in ("engine_port", "engine_idle_s", "engine_plugin_root")))
check("bad ports rejected", all(rejected("engine_port", v) for v in (0, 65536, "80", True, 1.5)))
check("idle of 5 accepted, 4 rejected",
      manage_settings.normalize("engine_idle_s", 5) == 5 and rejected("engine_idle_s", 4))
check("non-finite idle rejected", rejected("engine_idle_s", float("inf")) and rejected("engine_idle_s", float("nan")))
check("plugin root must exist",
      manage_settings.normalize("engine_plugin_root", str(temp_dir())) and
      rejected("engine_plugin_root", str(data_dir / "missing")))

report()
