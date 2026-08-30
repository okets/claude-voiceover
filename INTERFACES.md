# claude-voiceover — Internal Interface Contract

This file is the build contract. Every module MUST expose exactly these interfaces.
Hook scripts and commands program against this contract, not against implementations.
(Internal doc — not shipped in README marketing.)

## Ground rules

1. **Hook layer is stdlib-only.** `hooks/*.py` and `voiceover/*.py` import nothing outside the
   Python standard library. Heavy deps (kokoro-onnx, soundfile, numpy) live ONLY in `tts/`,
   spawned as subprocesses via `uv run`.
2. **Never write into user projects.** No `.claude/smarter-claude/` dirs, no log dirs, no JSONL
   dumps. Plugin state lives under `~/.claude-voiceover/` (env override `$VOICEOVER_DATA_DIR` for tests).
   The only project-level file ever READ is an optional user-created `.claude/voiceover.json`.
3. **Fail silent, never block.** Every hook wraps its body in try/except and always exits 0.
   A narration bug must never break a Claude Code session. Hooks print nothing to stdout
   (stdout is reserved for hook JSON protocols we do not use).
4. **Dry-run mode.** If env `VOICEOVER_DRY_RUN` is set, `speak()`/`play_sound()` print
   `[voiceover] <text>` to stderr instead of producing audio. All smoke tests use this.
5. **Python >= 3.9.** No walrus-in-comprehension cleverness needed; plain readable code.
6. Ported code keeps its proven logic (templates, truncation, extraction) but drops:
   dump_hook_data, cycle_id, DB modules, hook_parser JSONL pipeline, LLM completion path
   (`llm/anth.py`, `llm/oai.py`), security blocks from pre_tool_use, and the
   `sqlite3 smarter-claude.db` special-case announcement.

## Data dir resolution (shared helper)

`voiceover.settings.data_dir() -> Path`
- `$VOICEOVER_DATA_DIR` override if set (tests), else `~/.claude-voiceover/` — deterministic so external tools (statusline) can read it; CLAUDE_PLUGIN_DATA is deliberately NOT used. Created on demand.

## voiceover/settings.py

Hierarchy (first hit wins): project `<cwd>/.claude/voiceover.json` → global
`data_dir()/settings.json` → `DEFAULTS`.

```python
DEFAULTS = {
    "interaction_level": "concise",     # silent | quiet | concise | verbose
    "tts_engine": "auto",               # auto | kokoro | macos-female | macos-male | none
    "tts_enabled": True,
    "notification_sounds": True,        # quiet-mode mp3 pings
    "voice": None,                      # explicit kokoro voice id, else per-project hash
    "speak_subagent_completions": True, # verbose level only
}

LEVELS = ["silent", "quiet", "concise", "verbose"]   # numeric aliases 0..3 accepted everywhere

def data_dir() -> Path
def get_setting(key: str, cwd: str | None = None)            # hierarchy lookup
def set_setting(key: str, value, scope: str = "global", cwd: str | None = None)
    # scope "global" writes data_dir()/settings.json; "project" writes <cwd>/.claude/voiceover.json
def get_interaction_level(cwd=None) -> str                   # normalized string
def level_at_least(minimum: str, cwd=None) -> bool           # ordering per LEVELS
def is_tts_enabled(cwd=None) -> bool                         # tts_enabled and level != silent
def resolve_engine(cwd=None) -> str
    # "auto": macOS -> "macos-female"; else "kokoro" if models_present() else "none"
def get_voice(cwd=None) -> str
    # explicit setting wins; else for kokoro: stable choice from KOKORO_VOICES by
    # sha1(project_root) — the "random voice per project" behavior, now stateless
def models_present() -> bool                                  # ~/.kokoro-tts/models/*.onnx exists

KOKORO_VOICES = [...all 13 ids...]  # af_alloy? -> use the real 13 ids from old kokoro_voice.py
```

## voiceover/speech.py

```python
def speak(text: str, min_level: str = "concise", cwd: str | None = None, interrupt: bool = False) -> None
    # gates: is_tts_enabled + level_at_least(min_level); truncates via truncate_for_speech;
    # honors tts lock (skip if locked & not interrupt; interrupt -> stop_speech first);
    # engine dispatch:
    #   kokoro       -> subprocess: uv run --project <plugin>/tts <plugin>/tts/kokoro_voice.py <voice> <text>
    #   macos-female -> subprocess: python3 <plugin>/tts/macos_say.py --voice Samantha <text>
    #   macos-male   -> subprocess: python3 <plugin>/tts/macos_say.py --voice Daniel <text>
    #   none         -> no-op
    # spawn detached (Popen, no wait). DRY_RUN prints '[voiceover] <text>' to stderr.
def stop_speech() -> None                     # via process_utils; also clears stale lock
def play_sound(name: str, cwd=None) -> None   # 'notification' | 'decide'; gated by notification_sounds
def truncate_for_speech(text: str, limit: int = 220) -> str   # port semantic_truncate suite
def lock_path() -> Path                       # data_dir()/tts.lock with expiry timestamp inside
```

Level gating convention (callers pass min_level):
- quiet: sounds only (play_sound works at quiet+; speak() at quiet is used for nothing today)
- concise: permission requests, cycle completion
- verbose: pre/post tool play-by-play, subagent completions

## voiceover/templates.py

Ports of the proven template factories from old cycle_utils/hooks (random.choice phrase lists):

```python
def pre_tool_announcement(tool_name: str, tool_input: dict, transcript_path: str | None) -> str | None
    # TodoWrite -> in-progress todo; Bash -> command word; Read/Edit/Write -> speakable filename;
    # ExitPlanMode -> user-intent phrasing. None = nothing to say.
def post_tool_announcement(tool_name: str, tool_input: dict, tool_response: dict | None) -> str | None
    # description + '- done' variants; dedup via a small state file under data_dir()/dedup.json
def permission_request_message(payload: dict) -> str
    # 'May I run git push?' style; extracts tool/command/filename from payload + transcript
def completion_message(stats: CycleStats) -> str
    # simple cycle -> stats.final_response_text if < 300 chars; else templated stats summary
def subagent_completion_message(payload: dict) -> str
    # resurrect the manager-style templates ('my agent handled X')
def speakable_filename(path: str) -> str      # '. ' -> ' dot ' etc.
```

## voiceover/transcript.py

Replaces the whole JSONL/hook_parser pipeline with a single read-only scan.

```python
@dataclass
class CycleStats:
    files_modified: list[str]; edit_count: int; todos_done: int
    final_response_text: str | None; user_intent: str | None; complexity: str  # simple|moderate|complex

def cycle_stats(transcript_path: str) -> CycleStats
    # scan the transcript JSONL backwards to the last real user message (skip meta/command
    # messages); count Edit/Write/MultiEdit/NotebookEdit tool_use, collect file_path inputs,
    # todo completions from TodoWrite inputs, final assistant text block.
    # complexity: simple = <=1 file and <=2 edits and no subagents; complex = >=4 files or
    # subagent use; else moderate.
def last_user_message(transcript_path: str) -> str | None
```

## voiceover/audio_player.py, voiceover/process_utils.py

Straight ports (cross-platform playback / TTS-process kill). One fix: make the Unix kill
target only OUR processes — match 'kokoro_voice.py' / 'macos_say.py' / the say line we spawned,
never a bare `pkill -f say`.

## tts/

- `tts/pyproject.toml` — name "claude-voiceover-tts", deps: kokoro-onnx>=0.4.9, soundfile>=0.13.1, numpy>=1.24.0
- `tts/kokoro_voice.py <voice_id> <text>` — port as-is (13 voices, per-voice speed, model
  auto-download to ~/.kokoro-tts/models, plays via voiceover/audio_player logic duplicated
  locally or inline — this file may NOT import voiceover/ (different process/venv); it stays
  self-contained like today).
- `tts/macos_say.py --voice <Name> <text>` — collapse of the 3 old macos_*_tts.py scripts;
  stdlib only; uses `say`.
- `tts/tts_controller.py stop` — kill switch, port.

## hooks/

`hooks/hooks.json` registers all five events with command:
`bash "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" <hook_name>` (timeout 10 for all; Stop gets 30).

`hooks/run.sh` — probe python3 → python → py -3 (Windows Store stub detection à la
security-guidance), UTF-8 env (PYTHONIOENCODING=utf-8), cygpath conversion on Git Bash,
exec `$PYTHON "$PLUGIN_ROOT/hooks/<name>.py"` with stdin passthrough; ALWAYS exit 0.

Each hook: read JSON from stdin, `sys.path.insert(0, plugin_root)` then `from voiceover import ...`:
- `pre_tool_use.py` — stop_speech(interrupt semantics), then verbose-level announcement via
  pre_tool_announcement.
- `post_tool_use.py` — verbose-level announcement via post_tool_announcement.
- `notification.py` — permission requests: concise+ speaks permission_request_message;
  quiet plays 'notification' sound; also clear stale lock.
- `stop.py` — concise+ speaks completion_message(cycle_stats(transcript_path)); quiet plays
  'decide' sound. Detect stop_hook_active to avoid loops.
- `subagent_stop.py` — verbose + speak_subagent_completions -> subagent_completion_message.

## scripts/ (used by slash commands; argparse CLIs, stdlib only)

- `manage_settings.py {info|get KEY|set KEY VALUE [--project]|levels}` — wraps settings.py
- `manage_voices.py {list|set NAME [--project]|test [NAME]|recommend}` — friendly-name map
  (alloy, river, sky, sarah, nicole, adam, puck, michael, emma, daniel, lewis, george,
  default-male, default-female) → engine+voice; fixes old doubled-path bug; covers all 13
  kokoro voices; `test` speaks a sample line.
- `setup_tts.py` — first-run: check uv/ffplay availability, offer kokoro model download
  (~300MB to ~/.kokoro-tts), verify audio, print status. Reuses existing ~/.kokoro-tts cache.
- `migrate_legacy.py [--dry-run]` — detect legacy smarter-claude install (~/.claude/VERSION or
  'uv run ~/.claude/hooks/' in ~/.claude/settings.json); back up settings.json; strip the five
  legacy hook groups; list (and with --yes delete) legacy files; never touch statusLine,
  permissions, or anything it does not recognize. Prints a report.

## commands/ (plugin slash commands, markdown)

- `voice.md` (/voiceover:voice) — set voice by friendly name via manage_voices.py
- `level.md` (/voiceover:level) — silent|quiet|concise|verbose or 0-3 via manage_settings.py
- `setup.md` (/voiceover:setup) — run setup_tts.py, guide model download + voice pick
- `migrate.md` (/voiceover:migrate) — run migrate_legacy.py flow with confirmation
All commands invoke scripts with `"${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py"` paths.

## .claude-plugin/

- `plugin.json` — name "voiceover", displayName "Claude Voiceover", version 1.0.0,
  description packed with search terms (voice, text-to-speech, TTS, narration, notification
  sounds, Kokoro, offline, local), author Hanan (okets), homepage/repository
  https://github.com/okets/claude-voiceover, license MIT, hooks "./hooks/hooks.json",
  commands "./commands".
- `marketplace.json` — marketplace name "claude-voiceover", owner okets, one plugin entry
  with source "./" so `/plugin marketplace add okets/claude-voiceover` works directly.
