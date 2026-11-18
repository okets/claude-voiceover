"""The version pointer: should_replace, refresh (shim install, rate limit, spawn),
engine_launcher prepare, serve's own prepare, and the hook's isolation from failures."""

import contextlib
import fcntl
import io
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


def good(root, version, project=None, python=None):
    """A usable pointer for root: its python is a real executable."""
    return {"plugin_root": str(root), "plugin_version": version, "python": python or sys.executable,
            "project": str(project or Path(root) / "tts")}


# --- should_replace ---------------------------------------------------------------
here = make_root("1.6.0")
other = make_root("1.6.0")
current = good(here, "1.6.0")
check("should_replace: no pointer", engine_paths.should_replace(None, here, "1.6.0", None) is True)
check("should_replace: higher version wins", engine_paths.should_replace(current, other, "1.6.1", None) is True)
check("should_replace: numeric, not textual (1.10.0 > 1.9.0)",
      engine_paths.should_replace(good(here, "1.9.0"), other, "1.10.0", None) is True)
check("should_replace: equal version from another root keeps the pointer",
      engine_paths.should_replace(current, other, "1.6.0", None) is False)
check("should_replace: lower version keeps the pointer",
      engine_paths.should_replace(current, other, "1.5.0", None) is False)
check("should_replace: vanished current root is replaced",
      engine_paths.should_replace(good("/no/such/root", "9.9.9"), other, "1.0.0", None) is True)
check("should_replace: an override that differs wins even over a higher current",
      engine_paths.should_replace(good(here, "9.0.0"), other, "1.0.0", str(other)) is True)
check("should_replace: override L, candidate R is False (only the override is admitted)",
      engine_paths.should_replace(good(here, "1.0.0"), other, "9.0.0", str(here)) is False
      and engine_paths.should_replace(None, other, "9.0.0", str(here)) is False)
check("should_replace: a pointer with no python is unusable",
      engine_paths.should_replace({"plugin_root": str(here), "plugin_version": "9.9.9", "project": str(here / "tts")},
                                    other, "1.0.0", None) is True)
check("should_replace: a pointer whose python is not an executable file is unusable",
      engine_paths.should_replace(good(here, "9.9.9", python="/no/such/python"), other, "1.0.0", None) is True
      and engine_paths.should_replace(good(here, "9.9.9", python=str(here)), other, "1.0.0", None) is True)
check("should_replace: same root, project flipped (cpu -> gpu) replaces",
      engine_paths.should_replace(good(here, "1.6.0"), here, "1.6.0", None, here / "tts-gpu") is True)
check("should_replace: same root, same project keeps",
      engine_paths.should_replace(good(here, "1.6.0"), here, "1.6.0", None, here / "tts") is False)
check("should_replace: override == current root, but the project flipped, replaces",
      engine_paths.should_replace(good(here, "1.6.0"), here, "1.6.0", str(here), here / "tts-gpu") is True)
check("should_replace: an override equal to the current root replaces nothing",
      engine_paths.should_replace(current, other, "9.9.9", str(here)) is False)

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

# an older copy never overwrites the shim of a newer pointer
SHIM_TARGET.write_bytes(b"newer shim")
engine_paths.write_pointer(good(here, "99.0.0"))
(engine_paths.engine_dir() / "pointer_checked").unlink()
engine_pointer.refresh(here)
check("an older plugin copy leaves a newer version's shim alone", SHIM_TARGET.read_bytes() == b"newer shim")
engine_paths.write_pointer(good(here, "0.0.1"))
(engine_paths.engine_dir() / "pointer_checked").unlink()
engine_pointer.refresh(here)
check("a copy at or above the pointer's version installs the shim", SHIM_TARGET.read_bytes() == SHIM_SOURCE.read_bytes())

reset_state()
del os.environ["VOICEOVER_NO_SPAWN"]
try:
    check("refresh spawns prepare when the pointer should change", engine_pointer.refresh(here) is True
          and len(spawned) == 1 and spawned[0][0] == here, spawned)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    engine_paths.write_pointer(good(here, "1.6.0", here / "tts"))
    check("refresh spawns nothing when the pointer is current", engine_pointer.refresh(other) is False
          and len(spawned) == 1)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    check("refresh spawns for a higher version",
          engine_pointer.refresh(make_root("2.0.0")) is True and len(spawned) == 2)
    (engine_paths.engine_dir() / "pointer_checked").unlink()
    real_installer = engine_pointer.install_shim
    engine_pointer.install_shim = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        check("refresh never raises", engine_pointer.refresh(here) is False)
    finally:
        engine_pointer.install_shim = real_installer
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
    return sys.executable


real_run_uv, real_find_uv = engine_launcher._run_uv, engine_launcher._find_uv
engine_launcher._run_uv = fake_uv
engine_launcher._find_uv = lambda: "/fake/uv"
try:
    root = make_root("1.6.1")
    code = engine_launcher.main(["prepare", str(root), "--project", str(root / "tts")])
    pointer = engine_paths.read_pointer()
    check("prepare exits 0 and writes the pointer", code == 0 and pointer == {
        "plugin_root": str(root), "plugin_version": "1.6.1", "uv": "/fake/uv",
        "python": sys.executable, "project": str(root / "tts")}, pointer)
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
    engine_paths.write_pointer(good(here, "0.1.0"))
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

# a second prepare while one holds prepare.lock exits 0 without running uv
engine_launcher._run_uv = fake_uv
engine_launcher._find_uv = lambda: "/fake/uv"
uv_calls.clear()
held_lock = open(engine_paths.prepare_lock_path(), "a+")
fcntl.flock(held_lock, fcntl.LOCK_EX)
try:
    code = engine_launcher.main(["prepare", str(root), "--project", str(root / "tts")])
    check("prepare while another holds prepare.lock exits 0 without running uv", code == 0 and not uv_calls, uv_calls)
finally:
    held_lock.close()
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
          execs and execs[0][0] == sys.executable and execs[0][1][1] == str(root / "tts" / "kokoro_engine.py"),
          execs)
    check("serve's prepare used the cpu project by default", uv_calls[-1][1] == str(root / "tts"), uv_calls)

    check("default project: gpu setting on x86_64 Linux is tts-gpu",
          engine_launcher.default_project(root, {"tts_device": "gpu"}, "linux", "x86_64") == root / "tts-gpu")
    check("default project: gpu on macOS is tts",
          engine_launcher.default_project(root, {"tts_device": "gpu"}, "darwin", "arm64") == root / "tts")
    check("default project: gpu on arm64 Linux is tts",
          engine_launcher.default_project(root, {"tts_device": "gpu"}, "linux", "aarch64") == root / "tts")
    check("default project: gpu on a non-Linux/Windows platform is tts",
          engine_launcher.default_project(root, {"tts_device": "gpu"}, "freebsd14", "amd64") == root / "tts"
          and engine_launcher.default_project(root, {"tts_device": "gpu"}, "win32", "AMD64") == root / "tts-gpu")
    check("default project: no gpu setting is tts",
          engine_launcher.default_project(root, {}, "linux", "x86_64") == root / "tts")

    reset_state()
    engine_launcher._run_uv = lambda uv, project: None
    execs.clear()
    code = engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve whose prepare fails exits 1 without exec", code == 1 and not execs)

    reset_state()
    engine_launcher._run_uv = fake_uv
    ptr_python = temp_dir("vo-ptrpy-") / "python"
    ptr_python.write_text("#!/bin/sh\n")
    ptr_python.chmod(0o755)
    engine_paths.write_pointer(good(root, "1.6.1", python=str(ptr_python)))
    uv_calls.clear()
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve with a usable, current pointer skips prepare", execs[-1][0] == str(ptr_python) and not uv_calls,
          (execs, uv_calls))

    # C2: serve repairs an unusable pointer, and runs the POINTER's script with the pointer's python
    reset_state()
    engine_paths.write_pointer(good(root, "1.6.1", python="/no/such/python"))
    uv_calls.clear(); execs.clear()
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("C2: serve with an unusable pointer (python missing) prepares first",
          uv_calls == [("/fake/uv", str(root / "tts"))], uv_calls)
    check("C2: then execs the repaired pointer's python and script",
          execs and execs[-1][0] == sys.executable and execs[-1][1][1] == str(root / "tts" / "kokoro_engine.py"), execs)

    newer = make_root("9.9.9")
    reset_state()
    engine_paths.write_pointer(good(newer, "9.9.9", python=str(ptr_python)))
    uv_calls.clear(); execs.clear()
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("C2: a newer pointer's root differs from --plugin-root: no prepare, the pointer root's script runs",
          not uv_calls and execs and execs[-1][0] == str(ptr_python)
          and execs[-1][1][1] == str(newer / "tts" / "kokoro_engine.py"), (uv_calls, execs))

    older = make_root("1.0.0")
    reset_state()
    engine_paths.write_pointer(good(older, "1.0.0", python=str(ptr_python)))
    uv_calls.clear(); execs.clear()
    engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("C2: an older pointer is superseded: serve prepares this copy and runs its script",
          uv_calls == [("/fake/uv", str(root / "tts"))] and execs
          and execs[-1][1][1] == str(root / "tts" / "kokoro_engine.py"), (uv_calls, execs))

    reset_state()
    engine_paths.write_pointer(good(root, "1.6.1", python="/no/such/python"))
    engine_launcher._run_uv = lambda uv, project: None
    execs.clear()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("C2: a pointer still unusable after prepare: one stderr line, exit 1, no exec",
          code == 1 and not execs and len(err.getvalue().strip().splitlines()) == 1, (code, execs, err.getvalue()))
    engine_launcher._run_uv = fake_uv
    engine_paths.write_pointer(good(root, "1.6.1", python=str(ptr_python)))
    os.execv = lambda python, argv: (_ for _ in ()).throw(FileNotFoundError(2, "No such file", python))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = engine_launcher.main(["serve", "--plugin-root", str(root)])
    check("serve whose exec fails prints one stderr line and exits 1, no traceback",
          code == 1 and len(err.getvalue().strip().splitlines()) == 1 and "Traceback" not in err.getvalue(), err.getvalue())
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

# --- the hook refreshes for Kokoro users at any level, and for share-only users (R23) ------------
from voiceover import narrator


def hook_installs_shim(level="narrator", pointer=None, mod=False, **settings):
    """Run the hook (NO_SPAWN: only the shim shows) under settings; True when it installed the shim."""
    SHIM_TARGET.unlink(missing_ok=True)
    reset_state()
    set_setting("tts_enabled", True)
    set_setting("interaction_level", level)
    for key, value in settings.items():
        set_setting(key, value)
    if pointer is not None:
        engine_paths.write_pointer(pointer)
    if mod:
        narrator.mark_mod_session("S1")
    else:
        narrator.unmark_mod_session("S1")
    subprocess.run([sys.executable, str(REPO_ROOT / "hooks" / "user_prompt_submit.py")], capture_output=True,
                   text=True, env=os.environ.copy(),
                   input=json.dumps({"cwd": str(REPO_ROOT), "session_id": "S1", "prompt": "go"}))
    return SHIM_TARGET.exists()


check("hook installs the shim for a kokoro narrator", hook_installs_shim(tts_engine="kokoro") is True)
check("C2: hook installs the shim for kokoro at the concise level", hook_installs_shim("concise", tts_engine="kokoro") is True)
check("C2: hook installs the shim for kokoro at the silent level", hook_installs_shim("silent", tts_engine="kokoro") is True)
check("C2: hook installs the shim when the mod owns the prompt",
      hook_installs_shim(mod=True, tts_engine="kokoro") is True)
check("C2: hook installs the shim for a share-only user (a pointer exists, engine is not kokoro)",
      hook_installs_shim("concise", pointer=good(REPO_ROOT, "1.6.0"), tts_engine="macos-female") is True)
check("hook installs no shim for a macOS-voice narrator", hook_installs_shim(tts_engine="macos-female") is False)
check("hook installs no shim when the engine is none", hook_installs_shim(tts_engine="none") is False)
check("C2: hook installs the shim for kokoro even with TTS disabled (any level, R23)",
      hook_installs_shim(tts_engine="kokoro", tts_enabled=False) is True)
set_setting("tts_enabled", True)
set_setting("interaction_level", "narrator")
narrator.unmark_mod_session("S1")

report()
