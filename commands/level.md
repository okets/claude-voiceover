---
description: "Set the narration level: silent, quiet, concise, verbose (or 0-3)"
argument-hint: "[level] [--project]"
allowed-tools: ["Bash"]
---

# Set Voiceover Interaction Level

Change how much Claude Voiceover says. Arguments: `$ARGUMENTS`

## Levels

| Level | Alias | What you hear |
|-------|-------|---------------|
| silent | 0 | Nothing at all |
| quiet | 1 | Sound pings only (permission needed, task done) |
| concise | 2 | Speaks permission requests and task completions (default) |
| verbose | 3 | Full play-by-play narration of every tool Claude uses |

## Steps

1. If no level was given in `$ARGUMENTS`, show the levels and the current one, then stop and ask:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_settings.py" levels
   ```

2. If a level was given (word or number 0-3), set it. Add `--project` only when the user asked for a per-project setting:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_settings.py" set interaction_level <level>
   ```

3. Confirm in one sentence what the user will now hear (use the table above).

Do not edit any settings files by hand - the script handles the settings hierarchy and accepts both names and 0-3 aliases.
