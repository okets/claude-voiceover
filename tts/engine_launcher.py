# tts/engine_launcher.py
"""Command line for the shared Kokoro engine: serve, ensure, status, release,
stop, token, rotate-token. Stdlib only.

Exit codes: 0 ok, 1 engine unreachable, 2 usage.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import engine_client
import engine_paths

EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE = 0, 1, 2
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
    ensure = sub.add_parser("ensure")
    ensure.add_argument("--plugin-root", default=str(_DEFAULT_ROOT))
    sub.add_parser("status")
    release = sub.add_parser("release")
    release.add_argument("--keep-off", type=float, default=0.0)
    for name in ("stop", "token", "rotate-token"):
        sub.add_parser(name)
    return parser


def _serve(args):
    root = Path(args.plugin_root)
    script = str(root / "tts" / "kokoro_engine.py")
    if args.stub:
        python = sys.executable
    else:
        python = (engine_paths.read_pointer() or {}).get("python")
        if not python:
            print("voiceover-engine: no engine installed yet (run prepare)", file=sys.stderr)
            return EXIT_UNREACHABLE
    argv = [python, script] + (["--stub"] if args.stub else [])
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


_COMMANDS = {"serve": _serve, "ensure": _ensure, "status": _status, "release": _release,
             "stop": _stop, "token": _token, "rotate-token": _rotate_token}


def main(argv) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
