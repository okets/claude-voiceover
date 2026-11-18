"""The version pointer: should_replace, refresh (shim install, rate limit, spawn),
engine_launcher prepare, serve's own prepare, and the hook's isolation from failures."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

DATA = isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from voiceover import engine_pointer
import engine_launcher
import engine_paths

SHIM_SOURCE = REPO_ROOT / "tts" / "voiceover_engine_shim.py"
SHIM_TARGET = DATA / "bin" / "voiceover-engine"


def make_root(version):
    root = temp_dir("vo-root-")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    (root / "tts").mkdir()
    return root


def reset_state():
    for name in ("current.json", "pointer_checked"):
        (engine_paths.engine_dir() / name).unlink(missing_ok=True)
    (DATA / "settings.json").unlink(missing_ok=True)


# --- should_replace ---------------------------------------------------------------
here = make_root("1.6.0")
other = make_root("1.6.0")
current = {"plugin_root": str(here), "plugin_version": "1.6.0"}
check("should_replace: no pointer", engine_pointer.should_replace(None, here, "1.6.0", None) is True)
check("should_replace: higher version wins", engine_pointer.should_replace(current, other, "1.6.1", None) is True)
check("should_replace: numeric, not textual (1.10.0 > 1.9.0)",
      engine_pointer.should_replace({"plugin_root": str(here), "plugin_version": "1.9.0"}, other, "1.10.0", None) is True)
check("should_replace: equal version from another root keeps the pointer",
      engine_pointer.should_replace(current, other, "1.6.0", None) is False)
check("should_replace: lower version keeps the pointer",
      engine_pointer.should_replace(current, other, "1.5.0", None) is False)
check("should_replace: vanished current root is replaced",
      engine_pointer.should_replace({"plugin_root": "/no/such/root", "plugin_version": "9.9.9"}, other, "1.0.0", None) is True)
check("should_replace: an override that differs wins even over a higher current",
      engine_pointer.should_replace({"plugin_root": str(here), "plugin_version": "9.0.0"}, other, "1.0.0", str(other)) is True)
check("should_replace: an override equal to the current root replaces nothing",
      engine_pointer.should_replace(current, other, "9.9.9", str(here)) is False)

# --- refresh: shim install, rate limit, spawn ---------------------------------------
spawned = []
engine_pointer._spawn_prepare = lambda root, project: spawned.append((root, project))
reset_state()
check("refresh with NO_SPAWN spawns nothing", engine_pointer.refresh(here) is False and spawned == [])
check("refresh installs the shim, identical bytes", SHIM_TARGET.read_bytes() == SHIM_SOURCE.read_bytes())
check("the shim is mode 0755", stat.S_IMODE(SHIM_TARGET.stat().st_mode) == 0o755,
      oct(SHIM_TARGET.stat().st_mode))
check("the shim's first line is the python3 shebang",
      SHIM_TARGET.read_bytes().splitlines()[0] == b"#!/usr/bin/env python3")
SHIM_TARGET.write_bytes(b"stale")
engine_pointer.refresh(here)
check("a second refresh within 60 s does nothing", SHIM_TARGET.read_bytes() == b"stale")
(engine_paths.engine_dir() / "pointer_checked").unlink()
engine_pointer.refresh(here)
check("a refresh after the window repairs a differing shim", SHIM_TARGET.read_bytes() == SHIM_SOURCE.read_bytes())

reset_state()
del os.environ["VOICEOVER_NO_SPAWN"]
try:
    check("refresh spawns prepare when the pointer should change", engine_pointer.refresh(here) is True
          and len(spawned) == 1 and spawned[0][0] == here, spawned)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    engine_paths.write_pointer({"plugin_root": str(here), "plugin_version": "1.6.0"})
    check("refresh spawns nothing when the pointer is current", engine_pointer.refresh(other) is False
          and len(spawned) == 1)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    check("refresh spawns for a higher version",
          engine_pointer.refresh(make_root("2.0.0")) is True and len(spawned) == 2)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    real_installer = engine_pointer._install_shim
    engine_pointer._install_shim = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        check("refresh never raises", engine_pointer.refresh(here) is False)
    finally:
        engine_pointer._install_shim = real_installer
    (engine_paths.engine_dir() / "pointer_checked").unlink(missing_ok=True)
    real_platform = engine_pointer._windows
    engine_pointer._windows = lambda: True
    try:
        before = len(spawned)
        check("refresh is a no-op on Windows", engine_pointer.refresh(make_root("3.0.0")) is False
              and len(spawned) == before)
    finally:
        engine_pointer._windows = real_platform
finally:
    os.environ["VOICEOVER_NO_SPAWN"] = "1"

# --- prepare ---------------------------------------------------------------------------
reset_state()
uv_calls = []


def fake_uv(uv, project):
    uv_calls.append((uv, str(project)))
    return "/fake/venv/bin/python"


real_run_uv, real_find_uv = engine_launcher._run_uv, engine_launcher._find_uv
engine_launcher._run_uv = fake_uv
engine_launcher._find_uv = lambda: "/fake/uv"
try:
    root = make_root("1.6.1")
    code = engine_launcher.main(["prepare", str(root), "--project", str(root / "tts")])
    pointer = engine_paths.read_pointer()
    check("prepare exits 0 and writes the pointer", code == 0 and pointer == {
        "plugin_root": str(root), "plugin_version": "1.6.1", "uv": "/fake/uv",
        "python": "/fake/venv/bin/python", "project": str(root / "tts")}, pointer)
    check("prepare ran uv for that project", uv_calls == [("/fake/uv", str(root / "tts"))], uv_calls)

    lower = make_root("1.6.0")
    code = engine_launcher.main(["prepare", str(lower), "--project", str(lower / "tts")])
    check("prepare keeps a higher current pointer and exits 0",
          code == 0 and engine_paths.read_pointer()["plugin_root"] == str(root))

    (DATA / "settings.json").write_text(json.dumps({"engine_plugin_root": str(lower)}))
    code = engine_launcher.main(["prepare", str(lower), "--project", str(lower / "tts")])
    check("prepare honours the engine_plugin_root override",
          code == 0 and engine_paths.read_pointer()["plugin_root"] == str(lower))

    reset_state()
    engine_paths.write_pointer({"plugin_root": "/old", "plugin_version": "0.1.0", "python": "/old/python"})
    before = engine_paths.pointer_path().read_text()
    engine_launcher._run_uv = lambda uv, project: None
    code = engine_launcher.main(["prepare", str(root), "--project", str(root / "tts")])
    check("a failed uv exits 1 and leaves the pointer untouched",
          code == 1 and engine_paths.pointer_path().read_text() == before)
    engine_launcher._find_uv = lambda: None
    code = engine_launcher.main(["prepare", str(root), "--project", str(root / "tts")])
    check("no uv at all exits 1 and leaves the pointer untouched",
          code == 1 and engine_paths.pointer_path().read_text() == before)
finally:
    engine_launcher._run_uv, engine_launcher._find_uv = real_run_uv, real_find_uv

# uv lookup order: PATH, then ~/.local/bin, then ~/.cargo/bin
home = temp_dir("vo-home-")
path_dir = temp_dir("vo-path-")
saved_env = dict(os.environ)
os.environ["HOME"] = str(home)
os.environ["PATH"] = str(path_dir)


def make_uv(directory, body="#!/bin/sh\nexit 0\n"):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "uv"
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


try:
    check("uv lookup: nothing found is None", engine_launcher._find_uv() is None)
    cargo = make_uv(home / ".cargo" / "bin")
    check("uv lookup: ~/.cargo/bin last", engine_launcher._find_uv() == cargo)
    local = make_uv(home / ".local" / "bin")
    check("uv lookup: ~/.local/bin before ~/.cargo/bin", engine_launcher._find_uv() == local)
    on_path = make_uv(path_dir, '#!/bin/sh\nif [ "$1" = run ]; then echo /fake/path/python; fi\nexit 0\n')
    check("uv lookup: PATH first", engine_launcher._find_uv() == on_path)
    check("_run_uv: sync then run, returns the printed python",
          engine_launcher._run_uv(on_path, temp_dir("vo-proj-")) == "/fake/path/python")
    failing = make_uv(temp_dir("vo-failuv-"), "#!/bin/sh\nexit 3\n")
    check("_run_uv: a failing uv gives None", engine_launcher._run_uv(failing, temp_dir("vo-proj-")) is None)
finally:
    os.environ.clear()
    os.environ.update(saved_env)

# --- serve prepares by itself when there is no pointer (R3) ----------------------------
reset_state()
execs = []
real_execv = os.execv
os.execv = lambda python, argv: execs.append((python, argv))
engine_launcher._run_uv = fake_uv
engine_launcher._find_uv = lambda: "/fake/uv"
uv_calls.clear()
try:
    root = make_root("1.6.1")
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve with no pointer prepares, then execs the prepared python",
          execs and execs[0][0] == "/fake/venv/bin/python" and execs[0][1][1] == str(root / "tts" / "kokoro_engine.py"),
          execs)
    check("serve's prepare used the cpu project by default", uv_calls[-1][1] == str(root / "tts"), uv_calls)

    check("default project: gpu setting on x86_64 Linux is tts-gpu",
          engine_launcher._default_project(root, {"tts_device": "gpu"}, "linux", "x86_64") == root / "tts-gpu")
    check("default project: gpu on macOS is tts",
          engine_launcher._default_project(root, {"tts_device": "gpu"}, "darwin", "arm64") == root / "tts")
    check("default project: gpu on arm64 Linux is tts",
          engine_launcher._default_project(root, {"tts_device": "gpu"}, "linux", "aarch64") == root / "tts")
    check("default project: no gpu setting is tts",
          engine_launcher._default_project(root, {}, "linux", "x86_64") == root / "tts")

    reset_state()
    engine_launcher._run_uv = lambda uv, project: None
    execs.clear()
    code = engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve whose prepare fails exits 1 without exec", code == 1 and not execs)

    reset_state()
    engine_launcher._run_uv = fake_uv
    engine_paths.write_pointer({"plugin_root": str(root), "plugin_version": "1.6.1", "python": "/ptr/python"})
    uv_calls.clear()
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve with a pointer python skips prepare", execs[-1][0] == "/ptr/python" and not uv_calls, (execs, uv_calls))
finally:
    os.execv = real_execv
    engine_launcher._run_uv, engine_launcher._find_uv = real_run_uv, real_find_uv

# --- the hook stays silent and exits 0 even if refresh raises -------------------------------
from voiceover.settings import set_setting
set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
marker = DATA / "refresh_called"
driver = (
    "import runpy, sys\n"
    "sys.path.insert(0, %r)\n"
    "from voiceover import engine_pointer\n"
    "def boom(*a, **k):\n"
    "    open(%r, 'w').write('x')\n"
    "    raise RuntimeError('refresh failed')\n"
    "engine_pointer.refresh = boom\n"
    "runpy.run_path(%r, run_name='__main__')\n"
) % (str(REPO_ROOT), str(marker), str(REPO_ROOT / "hooks" / "user_prompt_submit.py"))
proc = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, env=os.environ.copy(),
                      input=json.dumps({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "go"}))
check("hook calls refresh on a user prompt", marker.exists(), proc.stderr)
check("hook exits 0 and prints nothing when refresh raises", proc.returncode == 0 and proc.stdout == "", (proc.returncode, proc.stdout, proc.stderr))

report()
