# Claude Voiceover

![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)

Claude Code can hear you now — Voiceover gives it a voice back.

Since March 2026 Claude Code has native `/voice` dictation, but it is input-only: you talk, Claude responds with text. Voiceover closes the loop with real-time neural narration of everything Claude is doing — running locally, and free.

[Watch the demo](https://www.youtube.com/watch?v=p02gvuYGrbk)

## Why Voiceover

- **A real narrator, not a bell.** Narrator mode reads Claude's actual words aloud as it works — streaming audio that starts within seconds — and announces out loud when Claude is blocked waiting for your input, question and options included. Prefer tool-by-tool play-by-play instead? That's one level down the dial.
- **Local and free neural voices.** 13 Kokoro neural voices run entirely on your machine — no API keys, no cloud, no cost. On macOS you can also use the built-in system voices with zero setup.
- **Cross-platform.** macOS, Windows, and Linux.

## Quickstart (30 seconds)

```
/plugin marketplace add okets/claude-voiceover
/plugin install voiceover@claude-voiceover
/reload-plugins
/voiceover:setup
```

On macOS you'll have a voice immediately (system voices need no setup). Opt into the Kokoro neural voices during setup for the good stuff. Then turn on the full experience:

```
/voiceover:level narrator
/voiceover:voice emma
```

…and pick **Narration** under *Output style* in `/config` (restart Claude Code first so the bundled style appears) — Claude then writes in spoken-word cadence, so the audio keeps pace with the work.

## Interaction levels

Set with `/voiceover:level` — per machine, or per project.

| Level | Alias | What you hear |
|-------|-------|---------------|
| silent | 0 | Nothing at all |
| quiet | 1 | Sound pings only (permission needed, task done) |
| concise | 2 | Speaks permission requests and task completions *(default)* |
| verbose | 3 | Full play-by-play of every tool Claude uses, including subagent completions |
| narrator | 4 | Claude's actual words read aloud as it works, with fluent speech between sentences (model loads once per turn, not per sentence) — tool chatter stays quiet, code blocks become a spoken "…code snippet…" cue |

## Voices

Set with `/voiceover:voice <name>`. Kokoro voices are local neural TTS; the default voices use macOS system speech and work instantly.

| Name | Voice | Name | Voice |
|------|-------|------|-------|
| `alloy` | American Female | `michael` | American Male |
| `river` | American Female | `emma` | British Female |
| `sky` | American Female | `daniel` | British Male |
| `sarah` | American Female | `lewis` | British Male |
| `nicole` | American Female, whispering | `george` | British Male |
| `adam` | American Male | `default-female` | macOS Samantha, zero setup |
| `echo` | American Male | `default-male` | macOS Daniel, zero setup |
| `puck` | American Male | | |

No voice picked? Each project gets a stable voice of its own, so you can tell your sessions apart by ear. Pick one for just the current project with `/voiceover:voice <name> --project`.

### Narrator, per project

Each project has its own queue and voice, and may speak over one other project (two engines at most). A new message from Claude preempts the old one at the end of the current sentence; question alerts and permission requests always play whole. The engine is loaded only while a turn runs, so nothing sits in memory between turns. Headless (`claude -p`) runs are not narrated.

### Narration starts as Claude writes

At the narrator level, narration starts while Claude is still writing: a Claude Code mod speaks each sentence as it streams, through the same per-project queue and engine. It needs Claude Code 2.1.287 or newer, where mods are on by default. On a build without mods, the hooks narrate as before; Claude Code may log that it could not load `hooks/mod.json`, which is expected. After installing or updating, run `/reload-plugins` or restart Claude Code.

## The Narration output style

Narrator level tells you what Claude wrote; the bundled **Narration** output style makes Claude write like someone meant to be heard — outcome first, radio-brief progress notes, spoken-word prose. Opt in by picking **Narration** under *Output style* in `/config` when you're listening, and switch back anytime. (Restart Claude Code after installing so the style appears.)

## Commands

| Command | What it does |
|---------|--------------|
| `/voiceover:setup` | First-run setup: checks tooling, offers the Kokoro model download, verifies audio |
| `/voiceover:voice` | Pick a voice by friendly name (add `--project` for per-project) |
| `/voiceover:level` | Set silent / quiet / concise / verbose / narrator (or 0–4) |
| `/voiceover:migrate` | Clean up a legacy smarter-claude install |

## Requirements

- **Claude Code 2.1.287 or newer** — for narration that starts as Claude writes (mods are on by default there). Older builds narrate through the hooks.
- **python3** (3.9+) — that's it for macOS system voices and notification sounds.
- **[uv](https://docs.astral.sh/uv/)** — only if you opt into Kokoro neural voices.
- **~300MB model download** — only if you opt into Kokoro. One time, stored in `~/.kokoro-tts`, reused if you already have it (e.g. from smarter-claude).

Nothing phones home. All speech is generated on your machine.

### Optional: Kokoro on an NVIDIA GPU (Linux / Windows, x86_64)

Kokoro runs on the CPU by default, everywhere. On a machine with an NVIDIA GPU you can move it there:

```
python3 scripts/manage_settings.py set tts_device gpu
```

The next narration builds the separate `tts-gpu` environment (`onnxruntime-gpu` with its CUDA and cuDNN wheels, about 2.5GB, one time) and runs Kokoro on CUDA. It needs a recent NVIDIA driver (CUDA 13: roughly 580 or newer), not a system CUDA install. Kokoro loads on the GPU and proves it with one short synthesis; if anything fails it reloads on the CPU and keeps talking, and `debug_log` records the device actually used. It is a machine-wide setting (no `--project`). macOS and arm machines always use the CPU environment, and the default `tts` environment is never touched. `set tts_device cpu` switches back.

Measured on a GTX 1650 (i7-9750H): a narration's first audio in ~2.1 s instead of ~3.7 s, about 3x faster per sentence in a warm engine, and about 64% less CPU time per narration. It costs ~1.1GB of VRAM while speaking and ~0.9GB more RAM.

## Install and update

Install with the Quickstart commands above. The reload loads the hooks and the mod. If narration doesn't start, restart Claude Code.

To update to a new version:

1. `/plugin marketplace update claude-voiceover` refreshes the catalog.
2. `/plugin update voiceover@claude-voiceover` updates the plugin. From a terminal, run `claude plugin update voiceover@claude-voiceover`; it applies after a restart.
3. Restart every running Claude Code session, or run `/reload-plugins` in each one, so it loads the new hooks and mod. Coming from 1.3, a 1.3 session left running interrupts 1.4 engines, so restart them all.

To give a project its own voice, run `/voiceover:voice <name> --project`.

The narrator mod is code that runs inside Claude Code on your machine, with the same access Claude Code has, written by its publisher, not Anthropic. Install Voiceover only if you trust it, as with any plugin.

## Migrating from smarter-claude

Voiceover is the successor to [smarter-claude](https://github.com/okets/.claude)'s narration. If you have the legacy install, run:

```
/voiceover:migrate
```

It shows you exactly what it will remove (the five smarter-claude hooks and its files), backs up your `~/.claude/settings.json` first, and never touches your statusLine, permissions, or anything else. The old contextual-memory database is retired — Claude Code's native memory covers that now. Your downloaded Kokoro models are kept and reused.

## License

MIT
