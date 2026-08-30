# Changelog

## 1.0.0 — 2026-08-30

Claude Voiceover 1.0 — the rebirth of smarter-claude as a focused voice-narration plugin.

### The story

smarter-claude did two jobs: a contextual-memory database and TTS narration. Claude Code's native memory has since made the database redundant, so that half is retired. The half people loved — the voice — lives on here, rebuilt as a proper Claude Code plugin.

### Added

- Plugin-native install: `/plugin marketplace add okets/claude-voiceover`, no setup script, no files copied into `~/.claude`.
- Real-time narration hooks for all five events (pre/post tool, notification, stop, subagent stop), fail-silent by design.
- All 13 Kokoro neural voices with friendly names, including `echo` (previously unmapped), plus instant macOS system voices.
- Stable per-project voice: projects without an explicit voice get a deterministic pick, no database needed.
- `/voiceover:setup` — guided first run; reuses an existing `~/.kokoro-tts` model cache and never re-downloads.
- `/voiceover:voice`, `/voiceover:level` — commands that actually execute, instead of printing instructions to run by hand.
- `/voiceover:migrate` — safe legacy cleanup: backs up `settings.json`, strips only the five smarter-claude hook entries, deletes only known smarter-claude files.
- Per-project settings via `.claude/voiceover.json`; global state under `~/.claude-voiceover/` — never inside your projects.

### Changed

- Narration templates, semantic truncation, and speech gating ported from smarter-claude and trimmed of every database dependency.
- TTS process cleanup now targets only Voiceover's own processes.

### Removed

- The contextual-memory SQLite database, JSONL hook-data pipeline, and LLM summary generation (superseded by Claude Code's native memory).
- The doubled-path settings bug (`~/.claude/.claude/smarter-claude`).
