"""Engine lifecycle over a Unix socket: idle, warm, release, stop, quiet-moment exits."""

import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

DATA = isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_paths
from engine_server import NoModel
from engine_stub import SineSynth
from _engine_test_util import Raw, serve, start_line

SOCK_DIR = temp_dir("vo-life-")
_count = [0]


def start_server(synth=None, **kwargs):
    _count[0] += 1
    path = str(SOCK_DIR / ("l%d.sock" % _count[0]))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen(32)
    server = serve(synth or SineSynth(), [listener], **kwargs)
    return server, path


def wait_exit(server, within):
    server.thread.join(within)
    return server.result[0] if server.result else None


def write_settings(**values):
    (DATA / "settings.json").write_text(json.dumps(values))


def point_elsewhere():
    engine_paths.write_pointer({"plugin_root": "/elsewhere", "pid": 1})


def clear_state():
    for name in ("current.json", "keep_off.json"):
        (engine_paths.engine_dir() / name).unlink(missing_ok=True)
    (DATA / "settings.json").unlink(missing_ok=True)


# --- idle ---------------------------------------------------------------------
srv, path = start_server(idle_unload_s=0.5)
c = Raw(path)
c.hello()
c.send(start_line(id="a", text="Hello there.", end=True))
c.read_until_end("a")
t0 = time.monotonic()
reason = wait_exit(srv, 3)
check("idle: exits with 'idle' within 1.5 s of the last request, with an idle connection open",
      reason == "idle" and time.monotonic() - t0 < 1.5, (reason, time.monotonic() - t0))

srv, path = start_server(idle_unload_s=0.5)
c = Raw(path)
c.hello()
t0 = time.monotonic()
check("idle: a connection that never sends anything does not hold the engine",
      wait_exit(srv, 3) == "idle" and time.monotonic() - t0 < 1.5)

srv, path = start_server(SineSynth(synth_delay=0.3), idle_unload_s=0.3)
c = Raw(path)
c.hello()
c.send(start_line(id="long", text="Word. " * 6, end=True))
events = c.read_until_end("long")
check("idle: never exits while a request is active", events[-1]["reason"] == "complete" and not srv.result)
wait_exit(srv, 3)

# --- warm ---------------------------------------------------------------------
srv, path = start_server(SineSynth(load_delay=0.1), idle_unload_s=0.5)
c = Raw(path)
c.hello()
t0 = time.monotonic()
c.send({"op": "warm", "hold_s": 2})
reply = c.read_event()
check("warm: replies loaded with the device", reply.ev == "warm" and reply["loaded"] is True
      and reply["device"] == "cpu", reply)
c.send({"op": "status"})
status = c.read_event()
check("warm: status shows the lease and warm_until", status["leases"] == 1 and status["warm_until"] > time.time() + 1,
      status)
time.sleep(1.2)
check("warm: still alive past idle_unload_s while held", not srv.result)
reason = wait_exit(srv, 4)
elapsed = time.monotonic() - t0
check("warm: exits idle only after the hold", reason == "idle" and 1.9 <= elapsed < 3.5, (reason, elapsed))

srv, path = start_server(idle_unload_s=60)
c = Raw(path)
c.hello()
c.send({"op": "warm", "hold_s": "soon"})
check("warm: a non-numeric hold_s is bad_request", c.read_error()["code"] == "bad_request")
c.send({"op": "warm", "hold_s": 100000})
c.read_event()
c.send({"op": "status"})
held = c.read_event()["warm_until"] - time.time()
check("warm: hold_s is capped at 600", 590 < held <= 600.5, held)
c.send({"op": "warm"})
check("warm: hold_s defaults to 0 and still loads", c.read_event()["loaded"] is True)


class NoModelSynth(SineSynth):
    def load(self):
        raise NoModel("no files")


nm, nm_path = start_server(NoModelSynth(), idle_unload_s=60)
c = Raw(nm_path)
c.hello()
c.send({"op": "warm"})
check("warm: a missing model replies no_model", c.read_error()["code"] == "no_model")
c.send({"op": "status"})
check("warm: a failed load drops the hold", c.read_event()["leases"] == 0)
nm.request_exit("stop")

# a dead connection waiting on a slow load does not count as busy
slow, slow_path = start_server(SineSynth(load_delay=1.5), idle_unload_s=0.3)
c = Raw(slow_path)
c.hello()
c.send({"op": "warm"})
time.sleep(0.1)
c.close_abruptly()
t0 = time.monotonic()
check("warm: a waiter that disconnected does not block the idle exit",
      wait_exit(slow, 3) == "idle", time.monotonic() - t0)

# --- stop and release -----------------------------------------------------------
srv.request_exit("stop")
check("stop: request_exit stop returns 'stop'", wait_exit(srv, 3) == "stop")

clear_state()
srv, path = start_server(SineSynth(synth_delay=0.1), idle_unload_s=60)
c = Raw(path)
c.hello()
c.send(start_line(id="fly", text="Word. " * 20, end=True))
c.read_event()
c.send({"op": "release", "keep_off_s": 30})
events = c.read_until_end("fly")
reason = wait_exit(srv, 3)
keep = engine_paths.read_keep_off()
check("release: replies released, ends the request with restarting, writes keep-off, exits 'release'",
      reason == "release" and keep and keep["reason"] == "release"
      and any(e.ev == "released" and e["vram_mib"] == 0 for e in events)
      and [e["code"] for e in events if e.ev == "error"] == ["restarting"]
      and events[-1]["reason"] == "error", (reason, keep, events[-3:]))

clear_state()
srv, path = start_server(idle_unload_s=60)
c = Raw(path)
c.hello()
c.send({"op": "release"})
check("release without keep_off_s: replies and exits, no keep-off file",
      c.read_event().ev == "released" and wait_exit(srv, 3) == "release"
      and engine_paths.read_keep_off() is None)

c = None
srv, path = start_server(idle_unload_s=60)
c = Raw(path)
c.hello()
c.send({"op": "release", "keep_off_s": "long"})
check("release: a non-numeric keep_off_s is bad_request and the engine stays",
      c.read_error()["code"] == "bad_request" and not srv.result)
srv.request_exit("stop")
wait_exit(srv, 3)


class FlakyStatusSynth(SineSynth):
    failures = 3

    def status(self):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("status broke")
        return super().status()


flaky = FlakyStatusSynth()
flaky.load()
srv, path = start_server(flaky, idle_unload_s=0.3, quiet_check_s=0.1)
time.sleep(0.5)
check("ticker: a raising status() does not stop the idle exit", wait_exit(srv, 3) == "idle", srv.result)

# --- status ----------------------------------------------------------------------
srv, path = start_server(idle_unload_s=42)
c = Raw(path)
c.hello()
c.send({"op": "status"})
status = c.read_event()
check("status: lifecycle fields",
      status["idle_unload_s"] == 42 and status["leases"] == 0 and status["warm_until"] is None
      and status["uptime_s"] >= 0 and status["idle_s"] >= 0 and status["callers"] == 1
      and status["requests"] == 0 and status["pid"] > 0 and status["plugin"] == "9.9.9"
      and status["proto"] == 1, status)
srv.request_exit("stop")
wait_exit(srv, 3)

# --- quiet-moment exits ------------------------------------------------------------
CHECK = 0.2
clear_state()
point_elsewhere()
srv, path = start_server(idle_unload_s=60, quiet_check_s=CHECK)
c = Raw(path)
c.hello()
time.sleep(3 * CHECK + 0.3)
check("update: no exit while a client is connected and idle", not srv.result)
c.close_abruptly()
check("update: exits 'update' once the last connection closes", wait_exit(srv, 3) == "update")

clear_state()
point_elsewhere()
srv, path = start_server(SineSynth(synth_delay=0.2), idle_unload_s=60, quiet_check_s=CHECK)
c = Raw(path)
c.hello()
c.send(start_line(id="busy", text="Word. " * 4, end=True))
c.read_until_end("busy")
c.close_abruptly()
check("update: exit comes after the request finished, not during it", wait_exit(srv, 3) == "update")

clear_state()
srv, path = start_server(idle_unload_s=60, quiet_check_s=CHECK)
time.sleep(3 * CHECK)
check("update: no pointer and no setting means no exit", not srv.result)
write_settings(engine_plugin_root="/other")
check("update: the engine_plugin_root setting differing triggers it", wait_exit(srv, 3) == "update")

clear_state()
write_settings(engine_plugin_root="/x")
engine_paths.write_pointer({"plugin_root": "/x"})
srv, path = start_server(idle_unload_s=60, quiet_check_s=CHECK)
time.sleep(3 * CHECK)
check("update: a matching pointer and setting do not", not srv.result)
srv.request_exit("stop")
wait_exit(srv, 3)

clear_state()
write_settings(tts_device="cpu")
srv, path = start_server(idle_unload_s=60, quiet_check_s=CHECK)
time.sleep(2 * CHECK)
write_settings(tts_device="cuda")
check("device change: exits 'update'", wait_exit(srv, 3) == "update")


class KeepOffSynth(SineSynth):
    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    def status(self):
        return {"device": "cpu", "requested": "cuda", "vram_mib": 0,
                "fallback": {"reason": self.reason, "at": "now"}}


clear_state()
engine_paths.write_keep_off(0.6, "release")
synth = KeepOffSynth("keep_off")
synth.load()
srv, path = start_server(synth, idle_unload_s=60, quiet_check_s=CHECK)
time.sleep(0.3)
check("card_return: no exit while keep-off is in force", not srv.result)
check("card_return: exits once the keep-off expires", wait_exit(srv, 3) == "card_return")

clear_state()
synth = KeepOffSynth("load_failed")
synth.load()
srv, path = start_server(synth, idle_unload_s=60, quiet_check_s=CHECK)
time.sleep(3 * CHECK)
check("card_return: a fallback for another reason stays on CPU", not srv.result)
srv.request_exit("stop")
wait_exit(srv, 3)

report()
