#!/usr/bin/env python3
"""Migrate from a legacy smarter-claude install to the Voiceover plugin.

Usage:
    migrate_legacy.py               Dry run (default): report what would change
    migrate_legacy.py --yes         Apply: back up settings, strip legacy hooks, delete legacy files
    migrate_legacy.py --claude-dir  Override ~/.claude (mainly for testing)

What it does, and nothing more:
  1. Detects a legacy install (~/.claude/VERSION, or 'uv run .../hooks/*.py' hook commands).
  2. Backs up settings.json to settings.json.pre-voiceover.bak before touching it.
  3. Removes ONLY the five smarter-claude hook entries (pre_tool_use, post_tool_use,
     notification, stop, subagent_stop). statusLine, permissions, and any config it
     does not recognize are never modified.
  4. Lists the legacy smarter-claude files; deletes them only with --yes.

Stdlib only.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

LEGACY_HOOK_EVENTS = ["PreToolUse", "PostToolUse", "Notification", "Stop", "SubagentStop"]
LEGACY_HOOK_FILE_RE = re.compile(
    r"[\\/]hooks[\\/](pre_tool_use|post_tool_use|notification|stop|subagent_stop)\.py"
)

# Legacy artifacts relative to the claude dir. Directories end with '/'.
LEGACY_PATHS = [
    "hooks/pre_tool_use.py",
    "hooks/post_tool_use.py",
    "hooks/notification.py",
    "hooks/stop.py",
    "hooks/subagent_stop.py",
    "hooks/utils/",
    "hooks/__pycache__/",
    "VERSION",
    "setup.sh",
    "setup.ps1",
    ".claude/smarter-claude/",  # artifact of the old doubled-path bug
]
LEGACY_COMMAND_GLOB = "commands/smarter-claude_*.md"


def is_legacy_hook_command(command):
    """True when a hook command string belongs to legacy smarter-claude."""
    return "uv run" in command and LEGACY_HOOK_FILE_RE.search(command) is not None


def load_settings(settings_path):
    """Parse settings.json; returns None when absent or unreadable."""
    try:
        with open(settings_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def find_legacy_hooks(config):
    """List (event, command) pairs for every legacy hook entry in settings."""
    found = []
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return found
    for event in LEGACY_HOOK_EVENTS:
        for group in hooks.get(event) or []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if hook.get("type") == "command" and is_legacy_hook_command(command):
                    found.append((event, command))
    return found


def strip_legacy_hooks(config):
    """Return a copy of config with only the legacy hook entries removed."""
    cleaned = json.loads(json.dumps(config))  # deep copy, JSON-safe
    hooks = cleaned.get("hooks")
    if not isinstance(hooks, dict):
        return cleaned
    for event in LEGACY_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            kept_hooks = [
                hook for hook in group.get("hooks") or []
                if not (isinstance(hook, dict)
                        and hook.get("type") == "command"
                        and is_legacy_hook_command(hook.get("command", "")))
            ]
            if kept_hooks:
                group = dict(group)
                group["hooks"] = kept_hooks
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    return cleaned


def find_legacy_files(claude_dir):
    """Existing legacy files/dirs under claude_dir, as Paths."""
    found = []
    for relative in LEGACY_PATHS:
        path = claude_dir / relative.rstrip("/")
        if path.exists():
            found.append(path)
    found.extend(sorted(claude_dir.glob(LEGACY_COMMAND_GLOB)))
    return found


def detect_legacy(claude_dir, legacy_hooks):
    """A legacy install is a VERSION file or any legacy hook entry."""
    return (claude_dir / "VERSION").exists() or bool(legacy_hooks)


def backup_path_for(settings_path):
    """Backup destination that never clobbers an earlier backup."""
    base = settings_path.with_name(settings_path.name + ".pre-voiceover.bak")
    candidate, counter = base, 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{counter}")
        counter += 1
    return candidate


def delete_path(path):
    """Remove a file or directory tree."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def print_report(claude_dir, legacy_hooks, legacy_files, apply_mode):
    print(f"Legacy smarter-claude scan of {claude_dir}")
    print()
    print(f"Hook entries to remove from settings.json: {len(legacy_hooks)}")
    for event, command in legacy_hooks:
        print(f"  - {event}: {command}")
    print()
    print(f"Legacy files to {'delete' if apply_mode else 'delete (with --yes)'}: {len(legacy_files)}")
    for path in legacy_files:
        suffix = "/" if path.is_dir() else ""
        print(f"  - {path}{suffix}")
    print()
    print("Untouched, always: statusLine, permissions, env, plugins, and every other setting.")


def apply_migration(claude_dir, settings_path, config, legacy_hooks, legacy_files):
    """Back up, strip hooks, delete files. Returns exit code."""
    if legacy_hooks and config is not None:
        backup = backup_path_for(settings_path)
        shutil.copy2(settings_path, backup)
        print(f"Backed up settings to {backup}")
        with open(settings_path, "w", encoding="utf-8") as handle:
            json.dump(strip_legacy_hooks(config), handle, indent=2)
            handle.write("\n")
        print(f"Removed {len(legacy_hooks)} legacy hook entries from {settings_path}")
    for path in legacy_files:
        try:
            delete_path(path)
            print(f"Deleted {path}")
        except OSError as error:
            print(f"Could not delete {path}: {error}")
            return 1
    print()
    print("Migration complete. Restart Claude Code so the old hooks fully unload.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="migrate_legacy.py",
                                     description="Migrate from legacy smarter-claude to the Voiceover plugin")
    parser.add_argument("--yes", action="store_true",
                        help="Apply the migration (default is a dry run that changes nothing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; this is already the default")
    parser.add_argument("--claude-dir", default=str(Path.home() / ".claude"),
                        help="Claude config directory (default: ~/.claude)")
    args = parser.parse_args(argv)

    apply_mode = args.yes and not args.dry_run
    claude_dir = Path(args.claude_dir).expanduser()
    settings_path = claude_dir / "settings.json"

    config = load_settings(settings_path)
    legacy_hooks = find_legacy_hooks(config) if config else []
    legacy_files = find_legacy_files(claude_dir)

    if not detect_legacy(claude_dir, legacy_hooks):
        print(f"No legacy smarter-claude install detected in {claude_dir}. Nothing to do.")
        return 0

    print_report(claude_dir, legacy_hooks, legacy_files, apply_mode)

    if not apply_mode:
        print()
        print("Dry run - nothing was modified. Re-run with --yes to apply.")
        return 0
    return apply_migration(claude_dir, settings_path, config, legacy_hooks, legacy_files)


if __name__ == "__main__":
    sys.exit(main())
