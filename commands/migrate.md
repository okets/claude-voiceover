---
description: "Migrate from legacy smarter-claude: remove its hooks and files safely, keep everything else"
argument-hint: ""
allowed-tools: ["Bash", "AskUserQuestion"]
---

# Migrate from smarter-claude

Clean up a legacy smarter-claude installation so Voiceover can take over narration. The migration script is conservative: it only ever touches the five known smarter-claude hook entries and the known smarter-claude files. statusLine, permissions, and all other settings are never modified.

## Steps

1. Run the dry run first (it changes nothing):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/migrate_legacy.py"
   ```

2. If it reports "No legacy smarter-claude install detected", tell the user they are already clean and stop.

3. Otherwise, show the user the report: which hook entries would be removed from `~/.claude/settings.json` and which files would be deleted. Ask for explicit confirmation before applying.

4. Only after the user confirms, apply:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/migrate_legacy.py" --yes
   ```

   This first backs up `~/.claude/settings.json` to `settings.json.pre-voiceover.bak`, then strips the legacy hook entries and deletes the legacy files.

   The legacy `~/.claude/smarter-claude/` data directory (old contextual-memory DB and logs) is kept by default. If the user also wants that history gone, add `--purge-data` to the same command — but only if they explicitly say so.

   If the report lists "Review manually" items (e.g. `~/.claude/CLAUDE.md` with its obsolete Database-First Policy section), offer to help the user edit those by hand — the script deliberately never touches them because they may contain the user's own content.

5. Tell the user:
   - Their settings backup location.
   - That they should restart Claude Code so the old hooks fully unload.
   - That the old contextual-memory database feature is retired - Claude Code's native memory covers it now - and Voiceover carries the narration forward. Their voice preference can be restored with `/voiceover:voice`.

Never run `--yes` without the user's explicit confirmation in this conversation.
