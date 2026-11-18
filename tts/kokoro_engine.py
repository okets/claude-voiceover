# tts/kokoro_engine.py
"""The shared Kokoro engine process. Run through engine_launcher.py serve.

Only --stub works until the real synth lands; numpy and kokoro are imported lazily.
"""

import sys
from pathlib import Path

import engine_paths
from engine_listeners import open_listeners
from engine_server import EngineServer
from engine_stub import SineSynth

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_log = engine_paths.append_log


def _open_listeners():
    """The engine's sockets (see engine_listeners.open_listeners); None if another engine serves."""
    return open_listeners(engine_paths.socket_path(), engine_paths.engine_port(), engine_paths.lock_path())


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
        listening = _open_listeners()   # holds engine.lock (listening.lock_fd) for the process lifetime
    except OSError as error:
        if "AF_UNIX path too long" not in str(error):
            raise
        _log("cannot bind %s: path longer than %d bytes (AF_UNIX limit); use a shorter VOICEOVER_DATA_DIR"
             % (engine_paths.socket_path(), engine_paths.UNIX_PATH_LIMIT))
        return 1
    if listening is None:
        _log("another engine is serving; exiting")
        return 0
    server = EngineServer(synth, idle_unload_s=engine_paths.idle_unload_seconds(),
                          token=engine_paths.read_or_create_token(),
                          plugin_version=engine_paths.plugin_version(_PLUGIN_ROOT),
                          plugin_root=str(_PLUGIN_ROOT))
    reason = server.serve_forever(listening.sockets, adopted=listening.adopted)
    _log("exit: %s" % reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
