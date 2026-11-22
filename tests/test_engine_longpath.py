"""A data dir whose engine.sock path exceeds the AF_UNIX limit fails with one clear line."""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT, temp_dir

isolate()
LONG = temp_dir("vo-long-") / ("d" * 90)
LONG.mkdir()
os.environ["VOICEOVER_DATA_DIR"] = str(LONG)
os.environ["VOICEOVER_ENGINE_STUB"] = "1"
sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_client
import engine_paths

SOCK = engine_paths.socket_path()
check("the socket path is past the limit", engine_paths.socket_path_too_long(SOCK), len(str(SOCK)))

started = time.monotonic()
check("ensure returns False", engine_client.ensure(REPO_ROOT, timeout=10.0) is False)
check("ensure fails fast, not after its timeout", time.monotonic() - started < 6, time.monotonic() - started)

log_lines = [l for l in engine_paths.log_path().read_text().splitlines() if l.strip()]
check("engine.log names the path and the limit",
      any(str(SOCK) in l and str(engine_paths.UNIX_PATH_LIMIT) in l for l in log_lines), log_lines)
check("no traceback in engine.log", not any("Traceback" in l for l in log_lines), log_lines)

serve = subprocess.run([sys.executable, str(REPO_ROOT / "tts" / "engine_launcher.py"), "serve",
                        "--plugin-root", str(REPO_ROOT), "--stub"],
                       capture_output=True, text=True, timeout=10, env=dict(os.environ))
err = [l for l in serve.stderr.splitlines() if l.strip()]
check("launcher serve exits 1 with one stderr line naming the path and the limit",
      serve.returncode == 1 and len(err) == 1 and str(SOCK) in err[0]
      and str(engine_paths.UNIX_PATH_LIMIT) in err[0], (serve.returncode, serve.stderr))

direct = subprocess.run([sys.executable, str(REPO_ROOT / "tts" / "kokoro_engine.py"), "--stub"],
                        capture_output=True, text=True, timeout=10, env=dict(os.environ))
check("the engine run directly exits 1 without a traceback",
      direct.returncode == 1 and "Traceback" not in direct.stderr, (direct.returncode, direct.stderr))

report()
