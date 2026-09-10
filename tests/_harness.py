"""Minimal stdlib test harness for claude-voiceover.

Each test file calls isolate() first, then check() per assertion, then
report() as its last line. No pytest: the hook layer is stdlib-only and
these tests must run anywhere the plugin runs.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_failures = []
_checks = 0
_temp_dirs = []


def isolate():
    """Point the plugin at a throwaway data dir and enable dry-run audio."""
    data_dir = tempfile.mkdtemp(prefix="vo-test-data-")
    _temp_dirs.append(data_dir)
    os.environ["VOICEOVER_DATA_DIR"] = data_dir
    os.environ["VOICEOVER_DRY_RUN"] = "1"
    sys.path.insert(0, str(REPO_ROOT))
    return Path(data_dir)


def temp_dir(prefix="vo-test-"):
    """A throwaway directory cleaned up at report() time."""
    path = tempfile.mkdtemp(prefix=prefix)
    _temp_dirs.append(path)
    return Path(path)


def check(name, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print("PASS  " + name)
    else:
        print("FAIL  " + name)
        if detail:
            print("        " + str(detail))
        _failures.append(name)


def report():
    """Print the tally, clean up, and exit non-zero if anything failed."""
    for path in _temp_dirs:
        shutil.rmtree(path, ignore_errors=True)
    print("\n%d/%d checks passed in %s" % (
        _checks - len(_failures), _checks, Path(sys.argv[0]).name))
    sys.exit(1 if _failures else 0)
