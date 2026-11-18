"""The kill switch stops narration only; `stop --all` also stops the engine (M18).

Runs a real stub engine under the harness's temp data dir (never the one a probe may
be using) and records pkill calls instead of making them."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

DATA = isolate()
os.environ["VOICEOVER_ENGINE_STUB"] = "1"
sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_client
import tts_controller
from voiceover import process_utils, speech

LONG = " ".join("Sentence number %d goes here." % i for i in range(12))   # 12 sentences, 0.25 s each

pkills = []
real_run = subprocess.run


def recording_run(argv, *args, **kwargs):
    if argv and argv[0] == "pkill":
        pkills.append(argv[-1])
        return subprocess.CompletedProcess(argv, 1, "", "")
    return real_run(argv, *args, **kwargs)


def start_engine():
    command = [sys.executable, str(REPO_ROOT / "tts" / "engine_launcher.py"), "serve",
               "--plugin-root", str(REPO_ROOT), "--stub", "--stub-delay-ms", "250"]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if engine_client.reachable():
            return child
        time.sleep(0.05)
    raise RuntimeError("engine did not start")


def real_cmdline(pid):
    """The running process's command line, as pkill -f sees it."""
    return Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ").decode().strip()


def wait_exec(child, timeout=5.0):
    """The launcher execs into the engine under the same pid; wait for that."""
    deadline = time.monotonic() + timeout
    while "kokoro_engine.py" not in real_cmdline(child.pid) and time.monotonic() < deadline:
        time.sleep(0.02)


def wait_exit(child, timeout=5.0):
    try:
        child.wait(timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


child = start_engine()
try:
    # the narration kill switch leaves an in-flight interface request alone
    subprocess.run = recording_run
    conn = engine_client.EngineConnection.connect()
    pending = conn.submit(LONG, "bf_emma")
    pending.chunks.get(timeout=5)
    tts_controller.stop_tts()
    speech.stop_speech()
    check("both kill switches ran their pkills", len(pkills) > 0, pkills)
    wait_exec(child)
    cmdline = real_cmdline(child.pid)
    check("the running engine's own command line is the engine script", "kokoro_engine.py" in cmdline, cmdline)
    patterns = (list(tts_controller.UNIX_PATTERNS) + list(tts_controller.WINDOWS_PATTERNS)
                + list(process_utils.TTS_PROCESS_PATTERNS))
    matching = [p for p in patterns if re.search(p, cmdline)]
    check("no kill pattern (regex) matches the real engine command line", not matching, (matching, cmdline))
    check("the request still completes", pending.done.wait(10) and pending.reason == "complete",
          (pending.reason, pending.error))
    check("the engine is still alive", child.poll() is None and engine_client.reachable())

    # plain `stop` does not touch the engine either
    pending = conn.submit(LONG, "bf_emma")
    pending.chunks.get(timeout=5)
    check("main stop returns 0", tts_controller.main(["stop"]) == 0)
    check("engine alive after main stop", child.poll() is None)
    conn.cancel(pending)
    pending.done.wait(5)

    # stop --all also stops the engine; the in-flight request is cancelled
    pending = conn.submit(LONG, "bf_emma")
    pending.chunks.get(timeout=5)
    check("main stop --all returns 0", tts_controller.main(["stop", "--all"]) == 0)
    check("the engine exits", wait_exit(child), child.poll())
    check("the request ends cancelled", pending.done.wait(5) and pending.reason == "cancelled",
          (pending.reason, pending.error))
    conn.close()

    # stop --all with no engine running ignores every error
    check("stop --all without an engine returns 0", tts_controller.main(["stop", "--all"]) == 0)
    check("unknown command is a usage error", tts_controller.main(["bogus"]) == 1)
finally:
    subprocess.run = real_run
    if child.poll() is None:
        child.kill()

report()
