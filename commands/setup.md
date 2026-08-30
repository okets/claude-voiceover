---
description: "First-run setup: check audio tooling, optionally install the Kokoro neural voices (~300MB), pick a voice"
argument-hint: "[--download]"
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
   - The Kokoro neural voices (13 voices, local and free) need `uv` on the PATH and a one-time ~300MB model download to `~/.kokoro-tts`. If the models are already cached there, the report says so and no download is needed.

3. If Kokoro models are missing, ask the user whether to download them now (~300MB, one time, stored in `~/.kokoro-tts`). If they agree (or `--download` was passed in `$ARGUMENTS`):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py" --download
   ```

   This can take a few minutes on slow connections. Files already present are never re-downloaded.

4. Recommend a voice and let the user pick:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" recommend
   ```

   Set it with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage_voices.py" set <name>` and then play a sample with `... test <name>` so they hear it. The full gallery is available via `/voiceover:voice`.

5. Verify audio end-to-end:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_tts.py" --test
   ```

6. Close with a one-line summary: which engine and voice are active, and that `/voiceover:level` adjusts how chatty the narration is.

If the user previously used smarter-claude, point them at `/voiceover:migrate` to clean up the old install.
