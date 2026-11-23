"""The interaction levels: names, numeric aliases, and the legacy name.

The third level was called "verbose" through 1.7.0. It is now "tools", which
says what it does. Every settings file written before the rename still holds
"verbose", so that name must keep resolving - silently, with no migration -
or an upgrade would drop those users to the default level.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover.settings import (
    LEGACY_LEVEL_NAMES,
    LEVELS,
    get_interaction_level,
    level_at_least,
    set_setting,
)

CLI = REPO_ROOT / "scripts" / "manage_settings.py"


def level_for(stored):
    set_setting("interaction_level", stored)
    return get_interaction_level()


def cli(*args):
    return subprocess.run([sys.executable, str(CLI)] + list(args),
                          capture_output=True, text=True, env=os.environ.copy())


# --- the dial ------------------------------------------------------------
check("the dial is silent, quiet, concise, tools, narrator",
      LEVELS == ["silent", "quiet", "concise", "tools", "narrator"], LEVELS)
check("'verbose' is no longer a level name", "verbose" not in LEVELS, LEVELS)
check("the third level is reached by its own name", level_for("tools") == "tools")
check("numeric 3 still reaches it", level_for(3) == "tools")
check("the string '3' still reaches it", level_for("3") == "tools")

# --- the legacy name ------------------------------------------------------
check("'verbose' is recorded as a legacy name for 'tools'",
      LEGACY_LEVEL_NAMES.get("verbose") == "tools", LEGACY_LEVEL_NAMES)
check("a settings file written before the rename still resolves to tools",
      level_for("verbose") == "tools", level_for("verbose"))
check("case and padding are tolerated, as for any level name",
      level_for("  VERBOSE  ") == "tools", level_for("  VERBOSE  "))
check("an unknown name still falls back to the default",
      level_for("chatty") == "concise", level_for("chatty"))

# --- the level floor used by the hooks ------------------------------------
set_setting("interaction_level", "tools")
check("at tools, a tools-floor announcement speaks", level_at_least("tools"))
check("a legacy 'verbose' floor still speaks at tools", level_at_least("verbose"))
set_setting("interaction_level", "concise")
check("at concise, a tools-floor announcement stays quiet",
      not level_at_least("tools"))
check("at concise, a legacy 'verbose' floor stays quiet too",
      not level_at_least("verbose"))
set_setting("interaction_level", "narrator")
check("at narrator, a tools-floor announcement is above the floor",
      level_at_least("tools"))

# --- the settings CLI -----------------------------------------------------
out = cli("set", "interaction_level", "tools")
check("the CLI sets the level by its new name", out.returncode == 0, out.stdout + out.stderr)
check("and stores the new name", get_interaction_level() == "tools")

set_setting("interaction_level", "concise")
out = cli("set", "interaction_level", "verbose")
check("the CLI still accepts the legacy name", out.returncode == 0, out.stdout + out.stderr)
check("and stores it as the new name, not the legacy one",
      get_interaction_level() == "tools", get_interaction_level())

out = cli("set", "interaction_level", "3")
check("the CLI still accepts the numeric alias", out.returncode == 0, out.stdout + out.stderr)
check("and stores the new name for it", get_interaction_level() == "tools")

out = cli("set", "interaction_level", "chatty")
check("the CLI rejects an unknown level", out.returncode == 1, out.stdout)
check("and the rejection names the real levels",
      "tools" in out.stdout and "0-4" in out.stdout, out.stdout)

out = cli("levels")
check("the levels listing names tools", "tools" in out.stdout, out.stdout)
check("the levels listing describes it as the tool play-by-play",
      "tool" in out.stdout.lower(), out.stdout)
check("the levels listing does not offer 'verbose'",
      "verbose" not in out.stdout.lower(), out.stdout)

report()
