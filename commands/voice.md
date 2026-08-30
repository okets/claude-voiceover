---
description: "Set the narration voice by friendly name: alloy, river, sky, sarah, nicole, adam, echo, puck, michael, emma, daniel, lewis, george, default-male, default-female"
argument-hint: "[voice-name] [--project]"
allowed-tools: ["Bash"]
---

# Set Voiceover Voice

Change the voice that narrates Claude Code activity. Arguments: `$ARGUMENTS`

## Steps

1. If no voice name was given in `$ARGUMENTS`, show the available voices and stop:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" list
   ```

   Present the output, then ask which voice the user wants.

2. If a voice name was given, set it (pass `--project` through when the user asked for a per-project setting):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" set <voice-name>
   ```

3. If the set succeeded, play a short sample so the user hears the new voice immediately:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" test <voice-name>
   ```

4. Report the result in one or two sentences. If the script said Kokoro is not set up yet, suggest running `/voiceover:setup`.

## Voice reference

| Name | Voice | Name | Voice |
|------|-------|------|-------|
| alloy | American Female | michael | American Male |
| river | American Female | emma | British Female |
| sky | American Female | daniel | British Male |
| sarah | American Female | lewis | British Male |
| nicole | American Female (whispering) | george | British Male |
| adam | American Male | default-female | macOS Samantha (zero setup) |
| echo | American Male | default-male | macOS Daniel (zero setup) |
| puck | American Male | | |

Do not edit any settings files by hand - the script handles the settings hierarchy.
