# tts/kokoro_engine.py
"""The shared Kokoro engine process. Run through engine_launcher.py serve.

Only --stub works until the real synth lands; numpy and kokoro are imported lazily.
"""

import sys
import time
from pathlib import Path

import engine_paths
from engine_server import (EngineServer, adopt_systemd_listeners, bind_listeners,
                           hold_single_instance)
from engine_stub import SineSynth

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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


def _stub_delay_ms(argv) -> int:
    """The value after --stub-delay-ms, else 0 (also 0 when it is missing or not a number)."""
    try:
        return max(0, int(argv[argv.index("--stub-delay-ms") + 1]))
    except (ValueError, IndexError):
        return 0


def _choose_synth(stub: bool, stub_delay_ms: int = 0):
    if stub:
        return SineSynth(synth_delay=stub_delay_ms / 1000)
    from kokoro_model import KokoroSynth   # lazy: pulls in numpy and onnxruntime
    return KokoroSynth()


def main(argv) -> int:
    stub = "--stub" in argv
    synth = _choose_synth(stub, _stub_delay_ms(argv))   # construction stays cheap: the model loads later, on demand
    try:
        listeners, _lock_fd = _choose_listeners()
    except OSError as error:
        if "AF_UNIX path too long" not in str(error):
            raise
        _log("cannot bind %s: path longer than %d bytes (AF_UNIX limit); use a shorter VOICEOVER_DATA_DIR"
             % (engine_paths.socket_path(), engine_paths.UNIX_PATH_LIMIT))
        return 1
    if listeners is None:
        _log("another engine holds the lock; exiting")
        return 0
    server = EngineServer(synth, idle_unload_s=engine_paths.idle_unload_seconds(),
                          token=engine_paths.read_or_create_token(),
                          plugin_version=engine_paths.plugin_version(_PLUGIN_ROOT),
                          plugin_root=str(_PLUGIN_ROOT))
    reason = server.serve_forever(listeners)
    _log("exit: %s" % reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
