#!/usr/bin/env bash
# claude-voiceover hook dispatcher: run.sh <hook_name>
#
# Finds a working Python 3 and runs hooks/<hook_name>.py with stdin passed
# through. Narration must never break a Claude Code session, so this script
# ALWAYS exits 0 - even when no Python is found or the hook itself fails.
#
# On Windows + Git Bash, `python3` usually resolves to the Microsoft Store
# stub, which exits non-zero silently in non-TTY subprocess context. The
# probe below runs each candidate with `-c` and skips any that fails, so the
# stub falls through to the real `python` or the `py -3` launcher.

# Force UTF-8 for all Python IO (no-op on macOS/Linux; on Windows this stops
# cp1252 crashes on non-ASCII paths). Must be set before Python starts.
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_NAME="$1"
[ -n "$HOOK_NAME" ] || exit 0
HOOK_SCRIPT="$PLUGIN_ROOT/hooks/$HOOK_NAME.py"
[ -f "$HOOK_SCRIPT" ] || exit 0

# Git Bash hands POSIX paths (/c/Users/...) to Windows python.exe, which
# misreads the leading slash. cygpath -w converts to native form; it is a
# Git Bash builtin and absent on macOS/Linux, where this guard is a no-op.
if command -v cygpath >/dev/null 2>&1; then
    HOOK_SCRIPT="$(cygpath -w "$HOOK_SCRIPT")"
fi

probe() {
    # Succeeds only for a real Python 3: the Microsoft Store stub fails to
    # run at all, and Python 2 fails the version check. Reads /dev/null so
    # the hook's stdin payload is left untouched for the real invocation.
    "$@" -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' \
        </dev/null >/dev/null 2>&1
}

for cmd in "python3" "python" "py -3"; do
    # shellcheck disable=SC2086  # "py -3" must word-split
    if probe $cmd; then
        # Not `exec`: we swallow the interpreter's exit status so a hook
        # failure can never surface as a hook error in the session.
        # shellcheck disable=SC2086
        $cmd "$HOOK_SCRIPT" || true
        exit 0
    fi
done

exit 0  # no Python 3 available: skip narration, never block the session
