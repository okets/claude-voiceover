#!/usr/bin/env python3
"""The plugin manifest must not re-declare what Claude Code auto-discovers.

`hooks/hooks.json` and `commands/` are loaded by convention. Naming either
one in .claude-plugin/plugin.json makes Claude Code load it twice and refuse
the plugin outright:

    Failed to load hooks from .../hooks/hooks.json: Duplicate hooks file
    detected ... The standard hooks/hooks.json is loaded automatically

The plugin shipped that way from 1.0.0 through 1.3.0. Nothing in the Python
suite could see it, because the defect is in the manifest rather than the
code - hence this file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()

manifest_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
manifest = json.loads(manifest_path.read_text())

# --- the defect that broke 1.3.0 on install -------------------------------
check("the manifest does not declare a hooks path",
      "hooks" not in manifest,
      "plugin.json names hooks=%r; hooks/hooks.json is auto-discovered, and "
      "declaring it makes Claude Code reject the plugin" % manifest.get("hooks"))

check("the manifest does not declare a commands path",
      "commands" not in manifest,
      "plugin.json names commands=%r; commands/ is auto-discovered" % manifest.get("commands"))

# --- the files those keys used to point at must still exist ---------------
# Removing the keys is only safe because the conventional locations are real.
check("hooks/hooks.json exists at the conventional location",
      (REPO_ROOT / "hooks" / "hooks.json").is_file())
check("commands/ exists at the conventional location",
      (REPO_ROOT / "commands").is_dir())

# --- the manifest still carries what the marketplace needs ----------------
for field in ("name", "displayName", "version", "description", "author", "license"):
    check("the manifest still has %r" % field, field in manifest)

# --- hooks.json itself stays well-formed and complete ---------------------
hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
expected = {"PreToolUse", "PostToolUse", "Notification", "Stop",
            "SubagentStop", "UserPromptSubmit"}
check("all six hook events are registered",
      set(hooks) == expected, "registered: %s" % sorted(hooks))

# --- version agrees with the marketplace entry beside it ------------------
marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
entry = marketplace["plugins"][0]
check("the marketplace entry names the same plugin",
      entry["name"] == manifest["name"],
      "%r vs %r" % (entry["name"], manifest["name"]))

report()
