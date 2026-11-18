# tts/kokoro_engine.py
"""The shared Kokoro engine process. Run through engine_launcher.py serve.

Only --stub works until the real synth lands; numpy and kokoro are imported lazily.
"""

import json
import sys
import time
from pathlib import Path

import engine_paths
from engine_server import (EngineServer, adopt_systemd_listeners, bind_listeners,
                           hold_single_instance)
from engine_stub import SineSynth

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _plugin_version(root: Path) -> str:
    try:
        with open(root / ".claude-plugin" / "plugin.json", encoding="utf-8") as handle:
            return str(json.load(handle).get("version", "unknown"))
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _log(message: str) -> None:
    try:
        with open(engine_paths.log_path(), "a", encoding="utf-8") as handle:
            handle.write("%.2f engine %s\n" % (time.time(), message))
    except OSError:
        pass


def _choose_listeners():
    """Systemd's sockets, else our own under the single-instance lock; None if another engine runs.
    The second element keeps the lock fd alive for the process lifetime."""
    adopted = adopt_systemd_listeners()
    if adopted is not None:
        return adopted, None
    lock_fd = hold_single_instance(engine_paths.lock_path())
    if lock_fd is None:
        return None, None
    return bind_listeners(engine_paths.socket_path(), engine_paths.engine_port()), lock_fd


def _choose_synth(stub: bool):
    if stub:
        return SineSynth()
    from kokoro_model import KokoroSynth   # lazy: pulls in numpy and onnxruntime
    return KokoroSynth()


def main(argv) -> int:
    stub = "--stub" in argv
    synth = _choose_synth(stub)   # construction stays cheap: the model loads later, on demand
    listeners, _lock_fd = _choose_listeners()
    if listeners is None:
        _log("another engine holds the lock; exiting")
        return 0
    server = EngineServer(synth, idle_unload_s=engine_paths.idle_unload_seconds(),
                          token=engine_paths.read_or_create_token(),
                          plugin_version=_plugin_version(_PLUGIN_ROOT),
                          plugin_root=str(_PLUGIN_ROOT))
    reason = server.serve_forever(listeners)
    _log("exit: %s" % reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
