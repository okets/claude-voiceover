# Changelog

## 1.5.0 - 2026-11-06

### Added
- Optional NVIDIA GPU for the Kokoro engine: machine-wide setting `tts_device`
  (`cpu` default, `gpu`). `gpu` runs the engine from a separate `tts-gpu` uv project
  (`onnxruntime-gpu` with CUDA/cuDNN wheels, preloaded before the session; the CPU
  `onnxruntime` wheel is overridden away so the two never share a package directory).
  The model proves the GPU with one short synthesis and reloads on CPU on any failure.
  Linux and Windows x86_64 only; macOS and arm keep the CPU environment, and the
  default `tts` project is unchanged. `setup_tts.py` reports the device.

### Changed
- One `kokoro_command()` builds every Kokoro launch (one-shot, drainer, setup test,
  voice preview), and one `load_kokoro()` loads the model at both engine load sites.
- The engine reads machine-wide settings through `engine_common.global_setting()`
  (debug_log uses it too); `settings.get_machine_setting()` is the plugin-side twin.

## 1.4.0 - 2026-10-02

**Restart every running Claude Code session (or run `/reload-plugins` in it)
after updating.** A 1.3 session left running interrupts 1.4 engines, and the
new SessionEnd hook only registers after a restart.

### Added
- Narration starts as Claude writes: a Claude Code mod speaks each sentence as
  it streams, through the same per-project queue and engine. It needs Claude
  Code 2.1.287 or newer (mods are on by default); the hooks remain the
  fallback and stand down for sessions the mod narrates.
- Per-project narration: each project has its own queue, voice and engine, and
  may speak over one other project (two engines at most). Set a project's
  voice with `/voiceover:voice <name> --project`.
- Sentence-boundary preemption: a new message from Claude ends the previous
  one at the end of its current sentence and drops what was queued between.
  Question alerts and permission requests are sticky - never dropped, always
  played whole - and are retired when the tool they asked about finishes.
- Prefetch: the next sentence is synthesized while the current one plays.
- A turn-scoped engine: a session holds a lease on its project's engine while
  a turn runs, the engine is pre-warmed on the prompt, and it unloads once the
  turn's speech has played. Adds a SessionEnd hook (the seventh event) that
  releases the lease.

### Changed
- Headless runs (`claude -p`, the Agent SDK) are no longer narrated.
- `~/.claude` is no longer a project marker, so projects without a `.claude`
  of their own no longer all resolve to the home directory and share one
  queue and voice.
- The one-shot interrupt only matches the one-shot speaker's processes.
- The machine-wide 1.3 queue directory is removed.
- Settings written with `/voiceover:voice ... --project` (or
  `/voiceover:level ... --project`) in a repo with no `.claude` of its own
  went to `~/.claude/voiceover.json` and applied to every such repo. 1.4 no
  longer reads that file as a project setting: move them into the repo's own
  `.claude/voiceover.json`, or into the global settings.
- Windows: the dead-engine takeover works on macOS and Linux. On Windows a
  crashed engine's project waits for its lock to expire (up to 60s).
- `tts_controller.py stop` stops every project's engine and clears every
  project's state.

### Fixed
- A one-shot interrupt (concise-level speech, a tool announcement) no longer
  kills another project's narration.
- A dead engine no longer silences its project: a lock whose owner process has
  died is treated as free, so the next hook starts a new engine.
- Windows playback no longer holds 30s per clip.
- The end-of-turn summary no longer parrots the request back. The legacy
  "You instructed me to: <truncated request>. Done!" line is gone, and so is
  the truncated request that prefixed the outcome summary ("2 files updated,
  3 edits. Done." stays).
- Narrator mode no longer speaks a template when the stop hook finds nothing
  unread. It used to fall through to the templated completion, which also
  re-spoke the turn's newest text when that text was short - a line already
  narrated mid-turn, heard twice. It now plays the decide chime instead, so
  narrator mode only ever speaks Claude's own words.

## 1.3.2 - 2026-09-23

### Fixed
- Narrator mode never spoke a word in some projects and fell back to the
  templated "You instructed me to: ... Done!" completion on every turn. The
  prose offset store (data_dir()/prose_state.json) is capped at 20
  transcripts, and the cap evicted by sorted path rather than by recency.
  A project whose transcript path sorts before the 20 already tracked (for
  example ~/Documents/... next to twenty ~/Projects/... sessions) had its
  offset evicted on the very commit that wrote it, so every hook treated the
  transcript as brand new, started "from now", and found nothing to read.
  Eviction is now least-recently-narrated, so the transcript just committed
  always survives. Present since narrator mode shipped in 1.2.0.

## 1.3.1 - 2026-09-11

### Fixed
- The plugin failed to load its hooks on install: "Duplicate hooks file
  detected". .claude-plugin/plugin.json declared hooks "./hooks/hooks.json"
  and commands "./commands", but Claude Code auto-discovers both, so naming
  them made it load hooks/hooks.json twice and refuse the whole hook set -
  no narration at all. Both keys are removed; the conventional locations are
  unchanged. Present since 1.0.0 and only surfaced once Claude Code began
  rejecting the duplicate. A manifest test now guards it, since nothing in
  the Python suite can see a defect that lives in the manifest.

## 1.3.0 - 2026-09-10

### Added
- Narration queue: at the narrator level, prose is queued to a spool
  (data_dir()/queue/) and one engine drains it back to back, loading its TTS
  model once per turn instead of once per sentence. Narration no longer waits
  for the next hook to resume, so the silence between sentences is a breath
  rather than a process spawn.
- New UserPromptSubmit hook: sending a new message clears that session's
  pending narration and stops playback, so the answer to the question you just
  asked is the next thing you hear. Requires one Claude Code restart to
  register. Until that restart, the other five hooks are already queuing
  narration but nothing clears an abandoned turn's queue on your next
  message - so for that one window, an old turn's narration is GUARANTEED to
  finish playing before the answer to your new question, where 1.2.0 would
  have dropped most of it on lock contention instead. Restart once after
  upgrading to get the intended behavior.

### Changed
- The transcript cursor now advances when prose is QUEUED, not when audio
  starts. Text can no longer be lost to a busy speaker, re-read from a stale
  cursor, or spoken as a merged blob that begins with an old sentence.
- Narrator mode no longer interrupts itself: permission requests and the
  end-of-turn summary join the queue in order. Only the user's next message
  cuts narration off.
- `tts/kokoro_voice.py` and `tts/macos_say.py` now share one copy of the
  data-dir and lock helpers via `tts/engine_common.py` instead of carrying an
  inline copy each.
- A new message clears that session's queue at every interaction level (including silent),
  while playback is stopped only when narration is audible, so a silenced session
  cannot interrupt another session's live playback.

### Fixed
- Claude Code's own status notices are never narrated. A usage-limit pause is
  recorded in the transcript as an assistant message with model "<synthetic>",
  so "You've hit your session limit" was read out as if Claude had said it -
  and, having sat unread across the pause, it was read at the FRONT of the next
  turn's narration. Also covers "Not logged in" and "No response requested."

### Added (dev)
- First test suite: stdlib-only scripts under tests/, run with
  `bash tests/run_all.sh`. No framework, matching the hook layer's constraint.

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
