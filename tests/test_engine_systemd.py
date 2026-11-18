# tests/test_engine_systemd.py
"""systemd user units (text, install/uninstall/enable/disable) and the opt-in setup step.
systemctl and loginctl are faked; units are written under a temp XDG_CONFIG_HOME."""

import contextlib
import io
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

DATA = isolate()
CONFIG = temp_dir("vo-xdg-")
os.environ["XDG_CONFIG_HOME"] = str(CONFIG)
sys.path.insert(0, str(REPO_ROOT / "tts"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import engine_launcher
import engine_paths
import setup_tts

UNITS = CONFIG / "systemd" / "user"
SOCK = Path("/run/user/1000/vo/engine.sock")
SHIM = Path("/home/u/.local/share/voiceover/bin/voiceover-engine")


def lines(text):
    return text.splitlines()


# --- unit text ------------------------------------------------------------------------
with_port = lines(engine_launcher.socket_unit(SOCK, 47320))
for wanted in ("ListenStream=%s" % SOCK, "ListenStream=127.0.0.1:47320", "SocketMode=0600", "DirectoryMode=0700",
               "NoDelay=true", "Accept=no", "WantedBy=sockets.target"):
    check("socket unit has %s" % wanted, wanted in with_port)
check("socket unit without a port has no TCP ListenStream",
      not [l for l in lines(engine_launcher.socket_unit(SOCK, None)) if "127.0.0.1" in l])
svc = engine_launcher.service_unit("/usr/bin/python3", SHIM, "/data/vo")
check("service unit ExecStart", "ExecStart=/usr/bin/python3 %s serve" % SHIM in lines(svc))
check("service unit passes the data dir", "Environment=VOICEOVER_DATA_DIR=/data/vo" in lines(svc))
check("service unit without the data dir has no Environment",
      "Environment" not in engine_launcher.service_unit("/usr/bin/python3", SHIM, None))
check("service unit has no Restart=", "Restart=" not in svc)
check("percent is doubled everywhere, dollar only in ExecStart (systemd expands $ in Exec lines only)",
      "ExecStart=/opt/p%%1/py$$X " + str(SHIM) + " serve" in lines(engine_launcher.service_unit("/opt/p%1/py$X", SHIM, None))
      and "Environment=VOICEOVER_DATA_DIR=/d%%/$h" in lines(engine_launcher.service_unit("/usr/bin/python3", SHIM, "/d%/$h"))
      and "ListenStream=/r/%%t/$s.sock" in lines(engine_launcher.socket_unit(Path("/r/%t/$s.sock"), None)))
check("a path with spaces is quoted",
      'ExecStart=/usr/bin/python3 "/a b/shim" serve' in lines(engine_launcher.service_unit("/usr/bin/python3", Path("/a b/shim"), None)))

# --- ExecStart python: a stable system python3, never the plugin's own -------------------------
real_candidates = engine_launcher._python_candidates
stable = temp_dir("vo-py-") / "python3"
stable.write_text("")
inside_plugin = REPO_ROOT / "tts" / "kokoro_engine.py"      # any real file under the plugin root
venv = temp_dir("vo-v-") / ".venv" / "bin"
venv.mkdir(parents=True)
(venv / "python3").write_text("")
engine_launcher._python_candidates = lambda: [str(inside_plugin), str(venv / "python3"), None, "/no/such/python3", str(stable)]
check("unit_python skips plugin-root, .venv, missing and None candidates", engine_launcher.unit_python() == str(stable))
engine_launcher._python_candidates = lambda: [str(inside_plugin), str(venv / "python3")]
check("unit_python is None when every candidate is rejected", engine_launcher.unit_python() is None)
engine_launcher._python_candidates = real_candidates
engine_launcher._python_candidates = lambda: [str(stable)]

# --- install / uninstall / enable / disable -------------------------------------------
calls = []
active = {"socket": False}
real_systemctl = engine_launcher.run_systemctl


def fake_systemctl(*args):
    calls.append(args)
    return 0 if args[0] != "is-active" or active["socket"] else 3


engine_launcher.run_systemctl = fake_systemctl
shim = engine_paths.shim_path()
check("install-service refuses without a shim", engine_launcher.main(["install-service"]) == 1 and not calls)
shim.parent.mkdir(parents=True, exist_ok=True)
shim.write_text("#!/bin/sh\n")
engine_launcher._python_candidates = lambda: [str(inside_plugin)]
check("install-service refuses when only a plugin python exists", engine_launcher.main(["install-service"]) == 1 and not calls)
engine_launcher._python_candidates = lambda: [str(stable)]
real_answers0, real_stop0 = engine_launcher._engine_answers, engine_launcher._stop
order = []
engine_launcher._engine_answers = lambda: True
engine_launcher._stop = lambda args: order.append("stop") or 0
_fake = fake_systemctl
engine_launcher.run_systemctl = lambda *a: order.append(a[0] if a[0] != "enable" else "enable") or _fake(*a)
check("install-service succeeds", engine_launcher.main(["install-service"]) == 0)
check("a reachable self-started engine is stopped before enable --now",
      order.index("stop") < order.index("enable"), order)
engine_launcher.run_systemctl = _fake
engine_launcher._engine_answers, engine_launcher._stop = real_answers0, real_stop0
check("the unit's ExecStart uses the chosen python", "ExecStart=%s " % stable in (UNITS / "voiceover-engine.service").read_text())
check("both unit files written", (UNITS / "voiceover-engine.socket").is_file() and (UNITS / "voiceover-engine.service").is_file())
check("units carry the temp data dir and shim",
      "VOICEOVER_DATA_DIR=%s" % DATA in (UNITS / "voiceover-engine.service").read_text()
      and str(shim) in (UNITS / "voiceover-engine.service").read_text())
check("daemon-reload then enable --now, in order",
      [c for c in calls if c[0] != "is-active"] == [("daemon-reload",), ("enable", "--now", "voiceover-engine.socket")], calls)
calls.clear(); active["socket"] = True
check("re-install with the socket active restarts it after daemon-reload",
      engine_launcher.main(["install-service"]) == 0
      and [c[0] for c in calls if c[0] != "is-active"] == ["daemon-reload", "enable", "restart"]
      and ("restart", "voiceover-engine.socket") in calls, calls)
active["socket"] = False

calls.clear()
check("uninstall-service succeeds", engine_launcher.main(["uninstall-service"]) == 0)
check("uninstall disables --now first", calls[0] == ("disable", "--now", "voiceover-engine.socket"), calls)
check("uninstall removes the files", not list(UNITS.glob("voiceover-engine.*")))

stops = []
real_answers, real_stop = engine_launcher._engine_answers, engine_launcher._stop
engine_launcher._stop = lambda args: stops.append(1) or 0
calls.clear()
engine_launcher._engine_answers = lambda: True
check("enable stops a self-started engine, then enables",
      engine_launcher.main(["enable"]) == 0 and stops == [1] and calls[-1] == ("enable", "--now", "voiceover-engine.socket"), calls)
stops.clear(); active["socket"] = True
engine_launcher.main(["enable"])
check("enable leaves an engine the socket already owns", stops == [])
stops.clear(); active["socket"] = False; engine_launcher._engine_answers = lambda: False
engine_launcher.main(["enable"])
check("enable with no engine running stops nothing", stops == [])
calls.clear(); stops.clear()
check("disable runs disable --now then stop",
      engine_launcher.main(["disable"]) == 0 and calls == [("disable", "--now", "voiceover-engine.socket")] and stops == [1])
engine_launcher._engine_answers, engine_launcher._stop = real_answers, real_stop

# --- setup_tts --share-engine ---------------------------------------------------------
def run_setup(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = setup_tts.main(list(argv))
    return code, out.getvalue()


def holding_port():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    return holder, holder.getsockname()[1]


holder, taken = holding_port()
calls.clear()
code, out = run_setup("--share-engine", str(taken))
check("share-engine refuses a taken port, naming it", code == 1 and str(taken) in out, out)
check("a refused port changes nothing", not calls and setup_tts.settings.get_machine_setting("engine_port") is None)
holder.close()

real_uv, real_find = engine_launcher._run_uv, engine_launcher._find_uv
engine_launcher._run_uv = lambda uv, project: sys.executable
engine_launcher._find_uv = lambda: "/fake/uv"
setup_tts.run_loginctl = lambda *a: "Linger=no\n"
os.environ["USER"] = "tester"
real_free = setup_tts.port_is_free
setup_tts.port_is_free = lambda port: port >= 47322          # 47320 and 47321 pretend to be taken
check("first_free_port skips taken ports", setup_tts.first_free_port() == 47322)
expected_default = 47322
code, out = run_setup("--share-engine")
check("share-engine succeeds", code == 0, out)
shared_out = out
check("default port is the first free one from 47320",
      setup_tts.settings.get_machine_setting("engine_port") == expected_default and expected_default >= 47320)
check("it prepared a pointer and installed the units",
      engine_paths.read_pointer() is not None and (UNITS / "voiceover-engine.socket").is_file())
check("the socket unit carries the chosen port", "127.0.0.1:%d" % expected_default in (UNITS / "voiceover-engine.socket").read_text())
check("re-running keeps our own configured port though the bind probe says taken",
      (lambda: (setattr(setup_tts, "port_is_free", lambda p: False), run_setup("--share-engine"))[1][0])() == 0
      and setup_tts.settings.get_machine_setting("engine_port") == expected_default)
setup_tts.port_is_free = lambda port: port >= 47322
code, out = run_setup("--share-engine", "47400")
check("an explicit new port is accepted when free", code == 0 and setup_tts.settings.get_machine_setting("engine_port") == 47400)
code, out = run_setup("--share-engine", "47322")
check("back to the default port rewrites the unit", "127.0.0.1:47322" in (UNITS / "voiceover-engine.socket").read_text())
code, out = run_setup("--share-engine", "70000")
check("an out-of-range port is refused without a traceback, setting untouched",
      code == 1 and "70000" in out and setup_tts.settings.get_machine_setting("engine_port") == 47322, out)
before = setup_tts.settings.get_machine_setting("engine_port")
engine_launcher._run_uv = lambda uv, project: None
code, out = run_setup("--share-engine", "47401")
check("a failed prepare restores the previous engine_port",
      code == 1 and setup_tts.settings.get_machine_setting("engine_port") == before, out)
engine_launcher._run_uv = lambda uv, project: sys.executable
os.environ["USER"] = ""
os.environ["LOGNAME"] = ""
setup_tts.run_loginctl = lambda *a: "Linger=no\n"
check("empty $USER falls back to the account name in the linger hint",
      ("enable-linger %s" % __import__("getpass").getuser()) in run_setup("--share-engine")[1])
os.environ["USER"] = "tester"
setup_tts.run_loginctl = lambda *a: "Linger=no\n"
check("linger off prints the warning and the command, without running it",
      "loginctl enable-linger tester" in shared_out and not any(c[0] == "enable-linger" for c in calls))
check("it prints the token and the note", engine_paths.read_or_create_token() in shared_out
      and "Put this token in your assistant's secrets; `voiceover-engine token` shows it again." in shared_out)
setup_tts.run_loginctl = lambda *a: "Linger=yes\n"
check("linger on prints no warning", "enable-linger" not in run_setup("--share-engine")[1])

stopped = []
real_stop = engine_launcher._stop
engine_launcher._COMMANDS["stop"] = lambda args: stopped.append(1) or 0
code, _ = run_setup("--unshare-engine")
check("unshare-engine removes units, clears the port, stops the engine",
      code == 0 and not list(UNITS.glob("voiceover-engine.*"))
      and setup_tts.settings.get_machine_setting("engine_port") is None and stopped == [1])
engine_launcher._COMMANDS["stop"] = real_stop
setup_tts.port_is_free = real_free
engine_launcher._run_uv, engine_launcher._find_uv = real_uv, real_find
engine_launcher.run_systemctl = real_systemctl

# --- C3: setup works where fcntl does not exist (Windows) -----------------------------------------
import subprocess
no_fcntl = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.modules['fcntl'] = None; sys.path.insert(0, %r); import setup_tts; "
     "sys.exit(0 if setup_tts.audio_player_found is not None else 1)" % str(REPO_ROOT / "scripts")],
    capture_output=True, text=True, env=dict(os.environ))
check("C3: setup_tts imports without fcntl (Windows)", no_fcntl.returncode == 0, no_fcntl.stderr[-400:])

report()
