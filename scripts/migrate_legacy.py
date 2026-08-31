#!/usr/bin/env python3
"""Migrate from a legacy smarter-claude install to the Voiceover plugin.

Usage:
    migrate_legacy.py               Dry run (default): report what would change
    migrate_legacy.py --yes         Apply: back up settings, strip legacy hooks, delete legacy files
    migrate_legacy.py --purge-data  Also delete the legacy smarter-claude/ data dir (DB + logs)
    migrate_legacy.py --claude-dir  Override ~/.claude (mainly for testing)

What it does, and nothing more:
  1. Detects a legacy install (~/.claude/VERSION, or 'uv run .../hooks/*.py' hook commands).
  2. Backs up settings.json to settings.json.pre-voiceover.bak before touching it.
  3. Removes ONLY the five smarter-claude hook entries (pre_tool_use, post_tool_use,
     notification, stop, subagent_stop). statusLine, permissions, and any config it
     does not recognize are never modified.
  4. Lists the legacy smarter-claude files; deletes them only with --yes.
     Never follows symlinks: a symlinked path inside ~/.claude is skipped (or, for a
     leaf symlink, only the link itself is removed), so dotfile-manager setups
     (stow/chezmoi) are safe.

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
    "install.sh",
    "install.ps1",
    "update.sh",     # would reinstall legacy files from GitHub if left behind
    "update.ps1",
    "migrations/",
    "templates/",
    "developer-docs/",
    ".claude/smarter-claude/",  # artifact of the old doubled-path bug
]
# The legacy data dir (SQLite DB + logs). Holds user history, so only --purge-data removes it.
LEGACY_DATA_DIR = "smarter-claude/"
LEGACY_COMMAND_GLOB = "commands/smarter-claude_*.md"
# Legacy-shipped files we only advise about: users may have replaced them with their own.
ADVISORY_PATHS = ["CLAUDE.md", "README.md", "GETTING_STARTED.md", "pyproject.toml", "uv.lock"]

# Paths that must not be deleted while legacy hook entries might still be registered.
HOOK_RUNTIME_PREFIX = "hooks"


def is_legacy_hook_command(command):
    """True when a hook command string belongs to legacy smarter-claude."""
    return "uv run" in command and LEGACY_HOOK_FILE_RE.search(command) is not None


def load_settings(settings_path):
    """Parse settings.json. Returns (config, error): config is None when absent
    or unreadable; error is a message only when the file exists but cannot be parsed."""
    try:
        with open(settings_path, encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as error:
        return None, f"{settings_path} could not be parsed ({error})"


def find_legacy_hooks(config):
    """List (event, command) pairs for every legacy hook entry in settings."""
    found = []
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return found
    for event in LEGACY_HOOK_EVENTS:
        for group in hooks.get(event) or []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") == "command" and is_legacy_hook_command(hook.get("command", "")):
                    found.append((event, hook.get("command", "")))
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
    if not hooks:
        cleaned.pop("hooks", None)
    return cleaned


def safely_deletable(claude_dir, path):
    """True when deleting path cannot reach outside claude_dir.

    Refuses any path whose intermediate components under claude_dir are symlinks
    (deleting through them would destroy the link target's contents). A leaf
    symlink is fine: delete_path() unlinks it without following. A real file/dir
    must resolve to somewhere inside the real claude_dir."""
    root = claude_dir.resolve()
    try:
        relative = path.relative_to(claude_dir)
    except ValueError:
        return False
    current = claude_dir
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return False
    if path.is_symlink():
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def find_legacy_files(claude_dir, purge_data=False):
    """(deletable, skipped) legacy files/dirs under claude_dir, as Paths.

    skipped holds paths that exist but were refused by the symlink-containment check."""
    candidates = []
    relative_paths = list(LEGACY_PATHS) + ([LEGACY_DATA_DIR] if purge_data else [])
    for relative in relative_paths:
        path = claude_dir / relative.rstrip("/")
        if path.exists() or path.is_symlink():
            candidates.append(path)
    candidates.extend(sorted(claude_dir.glob(LEGACY_COMMAND_GLOB)))
    deletable = [path for path in candidates if safely_deletable(claude_dir, path)]
    skipped = [path for path in candidates if path not in deletable]
    return deletable, skipped


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
    """Remove a file or directory tree. Never follows a symlink."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def print_report(claude_dir, legacy_hooks, legacy_files, skipped_files, apply_mode, purge_data):
    print(f"Legacy smarter-claude scan of {claude_dir}")
    print()
    print(f"Hook entries to remove from settings.json: {len(legacy_hooks)}")
    for event, command in legacy_hooks:
        print(f"  - {event}: {command}")
    print()
    print(f"Legacy files to {'delete' if apply_mode else 'delete (with --yes)'}: {len(legacy_files)}")
    for path in legacy_files:
        suffix = "/" if path.is_dir() and not path.is_symlink() else ""
        print(f"  - {path}{suffix}")
    for path in skipped_files:
        print(f"  ! SKIPPED (symlinked path, will not touch): {path}")
    data_dir = claude_dir / LEGACY_DATA_DIR.rstrip("/")
    if not purge_data and data_dir.exists():
        print(f"  i Kept (legacy DB/history; delete with --purge-data): {data_dir}/")
    advisories = [claude_dir / name for name in ADVISORY_PATHS if (claude_dir / name).exists()]
    if advisories:
        print()
        print("Review manually (shipped by smarter-claude, but may hold your own content):")
        for path in advisories:
            note = " — its 'Database-First Policy' section is obsolete" if path.name == "CLAUDE.md" else ""
            print(f"  - {path}{note}")
    print()
    print("Untouched, always: statusLine, permissions, env, plugins, and every other setting.")


def apply_migration(claude_dir, settings_path, config, legacy_hooks, legacy_files):
    """Back up, strip hooks, delete files. Returns exit code."""
    if legacy_hooks and config is not None:
        backup = backup_path_for(settings_path)
        shutil.copy2(settings_path, backup)
        print(f"Backed up settings to {backup}")
        with open(settings_path, "w", encoding="utf-8") as handle:
            json.dump(strip_legacy_hooks(config), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"Removed {len(legacy_hooks)} legacy hook entries from {settings_path}")
    failures = 0
    for path in legacy_files:
        try:
            delete_path(path)
            print(f"Deleted {path}")
        except OSError as error:
            print(f"Could not delete {path}: {error}")
            failures += 1
    print()
    if failures:
        print(f"{failures} item(s) could not be deleted - fix permissions and re-run.")
        return 1
    print("Migration complete. Restart Claude Code so the old hooks fully unload.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="migrate_legacy.py",
                                     description="Migrate from legacy smarter-claude to the Voiceover plugin")
    parser.add_argument("--yes", action="store_true",
                        help="Apply the migration (default is a dry run that changes nothing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; this is already the default")
    parser.add_argument("--purge-data", action="store_true",
                        help="Also delete the legacy smarter-claude/ data dir (DB, logs, history)")
    parser.add_argument("--claude-dir", default=str(Path.home() / ".claude"),
                        help="Claude config directory (default: ~/.claude)")
    args = parser.parse_args(argv)

    apply_mode = args.yes and not args.dry_run
    claude_dir = Path(args.claude_dir).expanduser()
    settings_path = claude_dir / "settings.json"

    config, settings_error = load_settings(settings_path)
    legacy_hooks = find_legacy_hooks(config) if config else []
    legacy_files, skipped_files = find_legacy_files(claude_dir, purge_data=args.purge_data)

    if settings_error:
        print(f"WARNING: {settings_error}")
        print("Hook entries were NOT scanned and hook scripts will NOT be deleted -")
        print("they may still be registered. Fix settings.json and re-run.")
        print()
        legacy_files = [
            path for path in legacy_files
            if HOOK_RUNTIME_PREFIX not in path.relative_to(claude_dir).parts[:1]
        ]

    if not detect_legacy(claude_dir, legacy_hooks):
        print(f"No legacy smarter-claude install detected in {claude_dir}. Nothing to do.")
        return 0

    print_report(claude_dir, legacy_hooks, legacy_files, skipped_files, apply_mode, args.purge_data)

    if not apply_mode:
        print()
        print("Dry run - nothing was modified. Re-run with --yes to apply.")
        return 0
    return apply_migration(claude_dir, settings_path, config, legacy_hooks, legacy_files)


if __name__ == "__main__":
    sys.exit(main())
