"""Engine client, launcher CLI, shim and the stub engine process."""

import contextlib
import fcntl
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

DATA = isolate()
os.environ["VOICEOVER_ENGINE_STUB"] = "1"
sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_client
import engine_launcher
import engine_paths


def engine_pids():
    """Stub engines belonging to this test (matched by their VOICEOVER_DATA_DIR)."""
    out = subprocess.run(["pgrep", "-f", "kokoro_engine.py --stub"], capture_output=True, text=True).stdout
    marker = ("VOICEOVER_DATA_DIR=%s" % DATA).encode()
    pids = []
    for pid in out.split():
        try:
            if marker in Path("/proc/%s/environ" % pid).read_bytes().split(b"\0"):
                pids.append(int(pid))
        except OSError:
            pass
    return pids


def run_cli(*argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine_launcher.main(list(argv))
    return code, buffer.getvalue()


def wait_gone(timeout=5.0):
    end = time.monotonic() + timeout
    while engine_pids() and time.monotonic() < end:
        time.sleep(0.05)
    return not engine_pids()


try:
    # T10 shape: ten concurrent ensure calls start exactly one engine
    results, spawns = [], []
    real_spawn = engine_client._spawn
    engine_client._spawn = lambda root: spawns.append(1) or real_spawn(root)
    threads = [threading.Thread(target=lambda: results.append(engine_client.ensure(REPO_ROOT)))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    check("ten ensures all succeed", results == [True] * 10, results)
    engine_client._spawn = real_spawn
    check("start.lock lets exactly one ensure spawn", len(spawns) == 1, len(spawns))
    check("exactly one stub engine runs", len(engine_pids()) == 1, engine_pids())

    # streaming client
    conn = engine_client.EngineConnection.connect()
    p = conn.submit("Hello there.", "bf_emma")
    first = p.chunks.get(timeout=5)
    chunks = [first]
    while chunks[-1] is not None:
        chunks.append(p.chunks.get(timeout=5))
    check("chunks then None", len(chunks) >= 2 and chunks[0] and chunks[-1] is None)
    check("request completes", p.done.wait(5) and p.reason == "complete" and p.error is None, p.reason)
    check("pcm() returns the audio", len(p.pcm()) > 0 and len(p.pcm()) % 2 == 0)
    bad = conn.submit("Hi.", "no_such_voice")
    check("error routed to its request", bad.done.wait(5) and bad.error and bad.error.code == "unknown_voice")
    check("status reply", "loaded" in conn.status())
    # sync calls: concurrent status() each get their own reply; fire-and-forget errors don't leak
    replies = []
    callers = [threading.Thread(target=lambda: replies.append(conn.status())) for _ in range(8)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(10)
    check("concurrent status calls all get a status",
          len(replies) == 8 and all(r.get("ev") == "status" for r in replies), replies)
    conn.warm("not-a-number")
    time.sleep(0.3)
    check("warm error does not poison the next call", conn.status().get("ev") == "status")
    conn.close()

    # CLI
    code, out = run_cli("status")
    check("status exits 0 and shows loaded", code == 0 and '"loaded"' in out, out)
    code, out = run_cli("token")
    check("token prints the token file", code == 0 and out.strip() == engine_paths.token_path().read_text().strip())
    check("usage error exits 2", run_cli("bogus")[0] == 2)

    # shim
    shim = subprocess.run([sys.executable, str(REPO_ROOT / "tts" / "voiceover_engine_shim.py"),
                           "--plugin-root", str(REPO_ROOT), "status"], capture_output=True, text=True)
    check("shim status exits 0", shim.returncode == 0, shim.stderr)
    shim_path = str(REPO_ROOT / "tts" / "voiceover_engine_shim.py")
    empty = temp_dir("vo-empty-")
    bare = subprocess.run([sys.executable, shim_path, "status"], capture_output=True, text=True,
                          env=dict(os.environ, VOICEOVER_DATA_DIR=str(empty)))
    check("shim without pointer exits 1", bare.returncode == 1 and bare.stderr.strip() != "")
    (empty / "engine").mkdir()
    (empty / "engine" / "current.json").write_text('{"plugin_root": "/nonexistent/plugin"}')
    stale = subprocess.run([sys.executable, shim_path, "status"], capture_output=True, text=True,
                           env=dict(os.environ, VOICEOVER_DATA_DIR=str(empty)))
    check("shim with stale pointer: one line, exit 1, no traceback",
          stale.returncode == 1 and len(stale.stderr.strip().splitlines()) == 1
          and "Traceback" not in stale.stderr, stale.stderr)

    # T9 shape: SIGKILL recovery
    conn = engine_client.EngineConnection.connect()
    victim = engine_pids()
    for pid in victim:
        os.kill(pid, signal.SIGSTOP)    # frozen: the request cannot complete before the kill
    pending = conn.submit("Hello there.", "bf_emma")
    for pid in victim:
        os.kill(pid, signal.SIGKILL)
    start = time.monotonic()
    while conn.alive and time.monotonic() - start < 1.0:
        time.sleep(0.02)
    check("client sees disconnect within 1 s", not conn.alive)
    check("pending fails with disconnected", pending.done.wait(1) and
          (pending.error is not None and pending.error.code == "disconnected"))
    conn.close()
    start = time.monotonic()
    check("ensure recovers within 3 s", engine_client.ensure(REPO_ROOT, timeout=3.0)
          and time.monotonic() - start < 3.0)
    check("new engine is a different process", engine_pids() and not set(engine_pids()) & set(victim))

    # S6: a waiter that gets start.lock only after its deadline still succeeds if an engine is up
    run_cli("stop")
    check("engine stopped before the late-waiter test", wait_gone())
    held = open(engine_paths.start_lock_path(), "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    late = []
    waiter = threading.Thread(target=lambda: late.append(engine_client.ensure(REPO_ROOT, timeout=1.0)))
    started = time.monotonic()
    waiter.start()
    time.sleep(0.2)
    subprocess.Popen([sys.executable, str(REPO_ROOT / "tts" / "kokoro_engine.py"), "--stub"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(1.0)     # past the waiter's deadline, lock still held
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    waiter.join(10)
    check("late waiter returns True when an engine is up", late == [True], late)
    check("late waiter did not block far past its deadline", time.monotonic() - started < 2.5)
finally:
    code, _ = run_cli("stop")
    check("stop exits 0", code == 0)
    check("no stub engines left", wait_gone())
    for pid in engine_pids():
        os.kill(pid, signal.SIGKILL)

report()
