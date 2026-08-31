# Claude Voiceover

![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)

Claude Code can hear you now — Voiceover gives it a voice back.

Since March 2026 Claude Code has native `/voice` dictation, but it is input-only: you talk, Claude responds with text. Voiceover closes the loop with real-time neural narration of everything Claude is doing — running locally, and free.

[Watch the demo](https://www.youtube.com/watch?v=p02gvuYGrbk)

## Why Voiceover

- **Live play-by-play, not just pings.** Voiceover narrates tool activity as it happens — what file Claude is editing, what command it wants to run, what it just finished — with a dial from silent to full narration. It doesn't read responses at you, and it isn't a bell; it's a narrator.
- **Local and free neural voices.** 13 Kokoro neural voices run entirely on your machine — no API keys, no cloud, no cost. On macOS you can also use the built-in system voices with zero setup.
- **Cross-platform.** macOS, Windows, and Linux.

## Quickstart (30 seconds)

```
/plugin marketplace add okets/claude-voiceover
/plugin install voiceover@claude-voiceover
/voiceover:setup
```

On macOS you'll have a voice immediately (system voices need no setup). Opt into the Kokoro neural voices during setup for the good stuff.

## Interaction levels

Set with `/voiceover:level` — per machine, or per project.

| Level | Alias | What you hear |
|-------|-------|---------------|
| silent | 0 | Nothing at all |
| quiet | 1 | Sound pings only (permission needed, task done) |
| concise | 2 | Speaks permission requests and task completions *(default)* |
| verbose | 3 | Full play-by-play of every tool Claude uses, including subagent completions |
| narrator | 4 | Claude's actual words, read aloud as it writes them — tool chatter stays quiet, code blocks become a spoken "…code snippet…" cue |

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

No voice picked? Each project gets a stable voice of its own, so you can tell your sessions apart by ear.

## Commands

| Command | What it does |
|---------|--------------|
| `/voiceover:setup` | First-run setup: checks tooling, offers the Kokoro model download, verifies audio |
| `/voiceover:voice` | Pick a voice by friendly name (add `--project` for per-project) |
| `/voiceover:level` | Set silent / quiet / concise / verbose / narrator (or 0–4) |
| `/voiceover:migrate` | Clean up a legacy smarter-claude install |

## Requirements

- **python3** (3.9+) — that's it for macOS system voices and notification sounds.
- **[uv](https://docs.astral.sh/uv/)** — only if you opt into Kokoro neural voices.
- **~300MB model download** — only if you opt into Kokoro. One time, stored in `~/.kokoro-tts`, reused if you already have it (e.g. from smarter-claude).

Nothing phones home. All speech is generated on your machine.

## Migrating from smarter-claude

Voiceover is the successor to [smarter-claude](https://github.com/okets/.claude)'s narration. If you have the legacy install, run:

```
/voiceover:migrate
```

It shows you exactly what it will remove (the five smarter-claude hooks and its files), backs up your `~/.claude/settings.json` first, and never touches your statusLine, permissions, or anything else. The old contextual-memory database is retired — Claude Code's native memory covers that now. Your downloaded Kokoro models are kept and reused.

## License

MIT
