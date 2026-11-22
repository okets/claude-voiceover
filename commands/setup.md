---
description: "First-run setup: check audio tooling, optionally install the Phonon (~65MB) or Kokoro (~300MB) neural voices, pick a voice"
argument-hint: "[--download-phonon] [--download]"
allowed-tools: ["Bash", "AskUserQuestion"]
---

# Voiceover Setup

Walk the user through getting narration working. Arguments: `$ARGUMENTS`

## Steps

1. Print the current status:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py"
   ```

2. Interpret the report for the user:
   - On macOS, narration already works with the built-in voices - nothing is required.
   - The Phonon neural voices (37 voices, local and free, light on memory) need `uv` on the PATH and a one-time ~65MB model download to the Hugging Face cache (`~/.cache/huggingface`). On Linux they also need the PortAudio library or an audio player (aplay, paplay, pw-cat or ffplay); the report says which is there.
   - The Kokoro neural voices (13 voices, local and free) need `uv` and a one-time ~300MB model download to `~/.kokoro-tts`. If the models are already cached there, the report says so and no download is needed.
   - Installing an engine does NOT change the current voice. The engine changes only when the user picks a voice (step 5 or `/voiceover:voice`).

3. If Phonon is not installed, ask the user whether to install it now (~65MB, one time). Only on a yes (or when `--download-phonon` was passed in `$ARGUMENTS`):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py" --download-phonon
   ```

   The first run also builds Phonon's own environment, so it can take a few minutes; its progress prints as it goes. If it fails, show the user the error it printed.

4. If the Kokoro models are missing, ask the user whether to download them too (~300MB, one time, stored in `~/.kokoro-tts`). Only on a yes (or when `--download` was passed in `$ARGUMENTS`):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py" --download
   ```

   This can take a few minutes on slow connections. Files already present are never re-downloaded.

5. Recommend a voice and let the user pick:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" recommend
   ```

   Set it with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" set <name>` and then play a sample with `... test <name>` so they hear it. The full gallery is available via `/voiceover:voice`.

6. Verify audio end-to-end:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py" --test
   ```

   If it says "Phonon is installed but not selected", the user has not picked a Phonon voice yet: suggest `/voiceover:voice`.

7. Close with a one-line summary: which engine and voice are active, and that `/voiceover:level` adjusts how chatty the narration is.

If the user previously used smarter-claude, point them at `/voiceover:migrate` to clean up the old install.
