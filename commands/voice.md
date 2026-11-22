---
description: "Set the narration voice by friendly name: Phonon voices (e.g. zoey, bryony, captain-silas-blackwater), Kokoro neural voices (e.g. echo, sarah) or macOS system voices (default-female, default-male)"
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

2. If a voice name was given, set it (pass `--project` through when the user asked for a per-project setting). For a Phonon voice that is not cached yet, this downloads that one voice (about 256KB) first; if the download fails, nothing is saved and the script says so:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" set <voice-name>
   ```

3. If the set succeeded, play a short sample so the user hears the new voice immediately:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" test <voice-name>
   ```

4. Report the result in one or two sentences. If the script said Phonon or Kokoro is not set up yet, suggest running `/voiceover:setup`. Picking a voice also picks its engine (Phonon, Kokoro or macOS).

## Voice reference

Voices come in three families, and the list groups them:

- **Phonon** (37): 25 standard voices (zoey, juniper, everly, harper, hadley, delia, easton, micah, trevor, russell, preston, miles, maeve, freya, pippa, elodie-rose, imogen, lottie, alfie, hugh, edwin, spencer, toby, teddy, bryony) and 12 character voices (captain-silas-blackwater, the-snow-queen, fairy-godmother and more).
- **Kokoro** (13): alloy, river, sky, sarah, nicole, adam, echo, puck, michael, emma, daniel, lewis, george.
- **macOS** (2): default-female (Samantha), default-male (Daniel), zero setup.

For the full list with labels and what is ready on this machine, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" list
```

Do not edit any settings files by hand - the script handles the settings hierarchy.
