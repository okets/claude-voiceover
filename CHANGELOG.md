# Changelog

## 1.2.0 - 2026-08-31

### Added
- Bundled "Narration" output style (output-styles/narration.md): spoken-word
  cadence so audio keeps up with the work - lead with the outcome,
  radio-brief progress notes, prose over tables/headers, code kept in
  blocks, a fuller closing summary. Opt-in via /output-style Narration
  (deliberately NOT force-applied); pairs with the narrator level.

## 1.1.7 - 2026-08-31

### Added
- Opt-in pipeline diagnostics: set debug_log true to trace every speak gate,
  peek, and dispatch to data_dir()/voiceover.log.

## 1.1.6 - 2026-08-31

### Fixed
- The permission echo could still cut question announcements: the
  notification payload does not reliably name the tool, so the substring
  gate missed. Suppression is now marker-based - the pre-tool hook records
  when it announced a dialog, and the notification hook skips permission
  alerts within 30s of that marker. Bash and other real permission requests
  are unaffected.

## 1.1.5 - 2026-08-31

### Fixed
- The question announcement was cut mid-sentence by its own echo: the same
  dialog also raises a "needs permission" notification, which spoke with
  interrupt. In narrator mode, permission notifications for AskUserQuestion /
  ExitPlanMode now stay silent - the pre-tool announcement already reads the
  actual question. All other permission requests keep their voice.

## 1.1.4 - 2026-08-31

### Added
- Blocking dialogs are announced out loud: when AskUserQuestion or
  ExitPlanMode is about to wait on the user, the narrator speaks
  "I need your input" plus the actual question text and option labels,
  interrupting any narration backlog - a blocked session outranks old
  audio. The away-from-screen moment the plugin exists for.

## 1.1.3 - 2026-08-31

### Fixed
- Question dialogs (AskUserQuestion / ExitPlanMode) appeared in silence: the
  prose leading up to them lost the transcript flush race and no further
  hooks fire while a dialog waits. Pre-tool now waits out the flush for
  dialog tools, so the words are narrated while the dialog is on screen.
- Mid-turn prose that missed the pre-tool flush race is now picked up at the
  same tool's completion (PostToolUse) instead of the next tool call.

## 1.1.2 - 2026-08-31

### Fixed
- The turn's closing narration always arrived one turn late: Claude Code
  flushes the final assistant message to the transcript ~0.5s AFTER the Stop
  hook fires, so the finale was invisible at Stop time and only played when
  the next hook fired. The Stop hook now polls briefly (up to 5s) for the
  flush - narrator finales and spoken final responses land right when the
  turn ends.

## 1.1.1 - 2026-08-31

### Fixed
- Narrator prose was synthesized in full before any audio played, so long
  answers sat in 30-60s of silence and were usually killed by the next turn
  before a single word was heard - only the short legacy templates survived
  to the speakers. Kokoro now streams: each chunk plays as soon as it is
  synthesized, and first audio lands in ~5 seconds regardless of length.

## 1.1.0 - 2026-08-31

### Added
- **Narrator level** (`/voiceover:level narrator`, alias 4): speaks Claude's
  ACTUAL words from the session transcript - main-thread only - instead of
  tool play-by-play. Code blocks become a spoken "...code snippet..." cue;
  markdown is stripped to plain sentences; narration starts from "now", never
  a session backlog. Unspoken text is retried at the next hook, and the Stop
  hook reads Claude's closing words as the finale.

### Fixed
- Two narrations could speak over each other. The TTS lock is now claimed
  atomically (temp file + os.link, so a racing engine can never observe an
  empty lock), and interrupts kill the owning engine's whole process GROUP -
  via the PID recorded in the lock - so a leftover afplay/say child can no
  longer keep talking under the next utterance.

## 1.0.2 - 2026-08-31

### Fixed
- Completion narration was skipped when it fired while a tool announcement was
  still playing: the Stop hook now interrupts leftover play-by-play instead of
  yielding to it.
- Kokoro created its TTS lock only after the multi-second model load, leaving a
  window where overlapping narrations double-spawned and callers mis-read the
  engine as idle. It now locks before loading the model.

## 1.0.1 - 2026-08-31

### Fixed
- Narration was silent when spoken through the hooks: `speak()` wrote the TTS
  lock immediately after spawning the engine, and the engine's own startup
  lock check then saw it and skipped. The engine now solely owns the lock
  lifecycle (create on start, remove at true end of speech); the caller only
  reads it for overlap gating.

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
