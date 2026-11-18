# tts/engine_launcher.py
"""Command line for the shared Kokoro engine: serve, ensure, status, release,
stop, token, rotate-token. Stdlib only.

Exit codes: 0 ok, 1 engine unreachable (prepare: failed), 2 usage.
"""

import argparse
import fcntl
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import engine_client
import engine_paths

EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE = 0, 1, 2
_POINTER_LOCK_WAIT_S = 5.0
_UV_STEP_TIMEOUT_S = 1800
_PRINT_PYTHON = "import sys; print(sys.executable)"
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit(EXIT_USAGE)


def _parser():
    parser = _Parser(prog="voiceover-engine")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    serve = sub.add_parser("serve")
    serve.add_argument("--plugin-root", default=str(_DEFAULT_ROOT))
    serve.add_argument("--stub", action="store_true")
    serve.add_argument("--stub-delay-ms", type=int, default=0)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("root")
    prepare.add_argument("--project", required=True)
    ensure = sub.add_parser("ensure")
    ensure.add_argument("--plugin-root", default=str(_DEFAULT_ROOT))
    sub.add_parser("status")
    release = sub.add_parser("release")
    release.add_argument("--keep-off", type=float, default=0.0)
    for name in ("stop", "token", "rotate-token"):
        sub.add_parser(name)
    return parser


# --- prepare: build the uv environment, then publish the pointer ---------------------

def _find_uv():
    """uv from PATH, else ~/.local/bin/uv, else ~/.cargo/bin/uv; None when absent."""
    found = shutil.which("uv")
    if found:
        return found
    for relative in (".local/bin/uv", ".cargo/bin/uv"):
        candidate = Path.home() / relative
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def _run_uv(uv, project):
    """`uv sync` the project, then ask its interpreter for its path; None on any failure."""
    try:
        subprocess.run([uv, "sync", "--project", str(project)], check=True,
                       stdin=subprocess.DEVNULL, timeout=_UV_STEP_TIMEOUT_S)
        out = subprocess.run([uv, "run", "--project", str(project), "python", "-c", _PRINT_PYTHON],
                             check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=_UV_STEP_TIMEOUT_S).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else None


def _hold_start_lock(lock):
    """flock start.lock, waiting at most _POINTER_LOCK_WAIT_S; True when held."""
    deadline = time.monotonic() + _POINTER_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(engine_client.CONNECT_POLL_S)


def _write_unless_kept(root, info):
    """Write `info` unless the pointer rule, re-checked now, says to keep the current one."""
    override = engine_paths.read_global_settings().get("engine_plugin_root")
    if engine_paths.should_replace(engine_paths.read_pointer(), root, info["plugin_version"], override):
        engine_paths.write_pointer(info)


def _publish_pointer(root, info, take_lock):
    """Re-check and write the pointer, under start.lock when `take_lock`.
    True when it is written or kept; False when the lock could not be taken."""
    if not take_lock:
        _write_unless_kept(root, info)
        return True
    try:
        lock = open(engine_paths.start_lock_path(), "a+")
    except OSError:
        return False
    with lock:
        if not _hold_start_lock(lock):
            return False
        _write_unless_kept(root, info)
        return True


def _prepare_pointer(root, project, take_lock=True):
    """Build `project`'s environment and publish it for `root`; EXIT_OK or EXIT_UNREACHABLE.

    start.lock is held only for the re-check and the write, never across the build: ensure()
    holds it while it waits for the engine we are being run by, and a minutes-long uv sync
    would stall every other starter. serve passes take_lock=False for that same reason: its
    starter already holds start.lock, and waiting for it here would deadlock."""
    uv = _find_uv()
    python = _run_uv(uv, project) if uv else None
    if not python:
        print("voiceover-engine: could not build the engine environment for %s" % project, file=sys.stderr)
        return EXIT_UNREACHABLE
    info = {"plugin_root": str(root), "plugin_version": engine_paths.plugin_version(root),
            "uv": uv, "python": python, "project": str(project)}
    return EXIT_OK if _publish_pointer(root, info, take_lock) else EXIT_UNREACHABLE


def _default_project(root, settings, system=None, machine=None):
    """tts-gpu for an opted-in, capable machine, else tts.

    Twin of voiceover.speech.kokoro_project/gpu_capable, duplicated because tts/ cannot
    import voiceover/: the onnxruntime-gpu wheels exist on x86_64 Linux and Windows only."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    capable = system != "darwin" and machine in ("x86_64", "amd64")
    return root / ("tts-gpu" if settings.get("tts_device") == "gpu" and capable else "tts")


def _prepare(args):
    return _prepare_pointer(Path(args.root), Path(args.project))


def _pointer_python():
    return (engine_paths.read_pointer() or {}).get("python")


def _serve(args):
    root = Path(args.plugin_root)
    script = str(root / "tts" / "kokoro_engine.py")
    if args.stub:
        python = sys.executable
    else:
        python = _pointer_python()
        if not python:
            project = _default_project(root, engine_paths.read_global_settings())
            if _prepare_pointer(root, project, take_lock=False) != EXIT_OK or not _pointer_python():
                print("voiceover-engine: no engine environment could be prepared", file=sys.stderr)
                return EXIT_UNREACHABLE
            python = _pointer_python()
    stub_args = ["--stub"] + (["--stub-delay-ms", str(args.stub_delay_ms)] if args.stub_delay_ms else [])
    argv = [python, script] + (stub_args if args.stub else [])
    os.execv(python, argv)   # exec keeps the pid, so LISTEN_PID stays valid


def _ensure(args):
    return EXIT_OK if engine_client.ensure(Path(args.plugin_root)) else EXIT_UNREACHABLE


def _with_engine(action):
    """Run action(connection) against a running engine; EXIT_UNREACHABLE if none answers."""
    try:
        conn = engine_client.EngineConnection.connect()
    except (OSError, engine_client.EngineError):
        return EXIT_UNREACHABLE
    try:
        action(conn)
    except engine_client.EngineError:
        return EXIT_UNREACHABLE
    finally:
        conn.close()
    return EXIT_OK


def _print_json(value):
    print(json.dumps(value))


def _status(args):
    return _with_engine(lambda conn: _print_json(conn.status()))


def _release(args):
    return _with_engine(lambda conn: _print_json(conn.release(args.keep_off)))


def _stop(args):
    return _with_engine(lambda conn: conn.stop())


def _token(args):
    print(engine_paths.read_or_create_token())
    return EXIT_OK


def _rotate_token(args):
    print(engine_paths.rotate_token())
    return EXIT_OK


_COMMANDS = {"serve": _serve, "prepare": _prepare, "ensure": _ensure, "status": _status, "release": _release,
             "stop": _stop, "token": _token, "rotate-token": _rotate_token}


def main(argv) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
