"""Engine client, launcher CLI, shim and the stub engine process."""

import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

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
    results = []
    threads = [threading.Thread(target=lambda: results.append(engine_client.ensure(REPO_ROOT)))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    check("ten ensures all succeed", results == [True] * 10, results)
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
    bare = subprocess.run([sys.executable, str(REPO_ROOT / "tts" / "voiceover_engine_shim.py"), "status"],
                          capture_output=True, text=True)
    check("shim without pointer exits 1", bare.returncode == 1 and bare.stderr.strip() != "")

    # T9 shape: SIGKILL recovery
    conn = engine_client.EngineConnection.connect()
    pending = conn.submit("one two three four five six seven eight nine ten " * 20, "bf_emma")
    victim = engine_pids()
    for pid in victim:
        os.kill(pid, signal.SIGKILL)
    start = time.monotonic()
    while conn.alive and time.monotonic() - start < 1.0:
        time.sleep(0.02)
    check("client sees disconnect within 1 s", not conn.alive)
    check("pending fails with disconnected", pending.done.wait(1) and
          (pending.error is not None and pending.error.code == "disconnected" or pending.reason == "complete"))
    conn.close()
    start = time.monotonic()
    check("ensure recovers within 3 s", engine_client.ensure(REPO_ROOT, timeout=3.0)
          and time.monotonic() - start < 3.0)
    check("new engine is a different process", engine_pids() and not set(engine_pids()) & set(victim))
finally:
    code, _ = run_cli("stop")
    check("stop exits 0", code == 0)
    check("no stub engines left", wait_gone())
    for pid in engine_pids():
        os.kill(pid, signal.SIGKILL)

report()
