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

LEVELS = ["silent", "quiet", "concise", "verbose", "narrator"]   # numeric aliases 0..4 accepted everywhere

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
def detect_project_root(cwd=None) -> Path
    # nearest .claude dir, else git root, else cwd. ~/.claude is Claude Code's own user
    # configuration, NOT a project marker: accepting it would give every project without a
    # .claude of its own the home directory as its root (one shared queue and voice).

KOKORO_VOICES = [...all 13 ids...]  # af_alloy? -> use the real 13 ids from old kokoro_voice.py
```

## voiceover/projects.py

Project identity. Every project (the root `detect_project_root` finds, the same root the
voice setting resolves against) gets `data_dir()/projects/<key>/`: its queue, engine lock,
leases, stop requests, spawn marker and wav scratch dir live there, so a project's queue and
its voice always agree. Engines are handed that directory and never compute a key.

```python
def project_root(cwd=None) -> Path        # detect_project_root(cwd), resolved
def project_key(cwd=None) -> str          # 12 hex chars: sha1(str(project_root))[:12]
def project_dir(cwd=None) -> Path         # data_dir()/projects/<key>/, created; root.txt labels it
def session_slug(session) -> str | None   # session id made file-safe ([^A-Za-z0-9_-] -> _); None for no session
```

`session_slug` names every per-session state file (leases, stop requests, pins, markers);
`tts/engine_common.py` applies the same rule to the stop files it reads.

## voiceover/lease.py

A session holds its project's engine while a turn runs, so the model stays loaded and
unloads once nobody needs it. Hooks WRITE leases; the engine READS them
(`engine_common.lease_is_live`) and never deletes one. The shared contract is
`<project dir>/leases/<session slug>.json` holding `{"expiry": unix seconds}`. The expiry is
a crash backstop: holders renew at every tool hook and release at turn end.

```python
LEASE_TTL = 600.0            # a holder whose turn end always arrives (the mod)
FALLBACK_LEASE_TTL = 120.0   # classic hooks: Stop does not fire on an interrupt

def lease_dir(cwd=None) -> Path
def hold(cwd, session, ttl=LEASE_TTL) -> bool   # hold or renew; also prunes other sessions' leases expired >1h
def release(cwd, session) -> None
def any_live(cwd=None) -> bool                   # any unexpired lease in the project
```

## voiceover/narrator.py

Narrator-level actions shared by the classic hooks and the mod's bridge
(`hooks/mod_bridge.py`), so what a prompt, a dialog or a turn's end does to the queue, lease and engine
has one definition. Per-session records live under `data_dir()`: `pins/` (the project a
session's turn stays in), `dialog_alert/` and `mod_sessions/` (sessions the mod narrates);
week-old records are pruned. Every function
fails soft.

```python
DIALOG_TOOLS = ("AskUserQuestion", "ExitPlanMode")

def headless() -> bool                    # CLAUDE_CODE_ENTRYPOINT startswith "sdk-" (claude -p, Agent SDK)
def narrates(cwd=None) -> bool            # narrator level + tts enabled + engine != none + not headless
def pin_cwd(session, cwd) -> None         # keep this session's turn in cwd's project
def repin(session, cwd)                   # every prompt: release a lease held under a previous pin, then pin cwd;
                                          # returns the previously pinned cwd when its PROJECT differs, else None
def pinned_cwd(session, cwd)              # the pinned cwd, else cwd
def unpin(session) -> None
def is_users_prompt(prompt_text) -> bool  # False for a "<task-notification>" arriving as a prompt
def mod_active(session) -> bool           # the mod narrates this session (it has a mod_sessions mark)
def mod_owns_prompt(session, cwd) -> bool # mod_active and the level at the session's pinned cwd is narrator
def mark_mod_session(session) -> None     # the bridge marks the session on every call
def unmark_mod_session(session) -> None   # end-session and session_end.py drop the mark
def stands_down(session, transcript_path) -> bool
    # the mod narrates this session: calls prose.skip_unread(transcript_path) (the classic path
    # does nothing else) and returns True; False when the mod does not narrate it
def abandon(cwd, session) -> int
    # spool.clear(session) THEN request_stop in cwd's project (engine can't claim one more item
    # between); returns the items dropped. No session -> nothing cleared or stopped.
def cutover(cwd, session, ttl=lease.LEASE_TTL, warm=True) -> None
    # the user's own prompt: abandon(), then prewarm if warm and narrating. No process is killed.
    # The mod passes warm=False: its turn.start hold warms, so a prompt that never becomes a turn
    # holds nothing.
def write_dialog_marker(session) -> None
def dialog_recently_announced(session, within=30.0) -> bool   # notification.py's echo guard
def queue_dialog(cwd, session, tool_name, tool_input, lead_in=None, shown=None, utterance=None) -> bool
    # sticky, whole alert (lead-in prose first); marker only if it queued; ensure_drainer; release lease.
    # utterance (the mod passes its lead-in's) makes the alert continue that message, not preempt it
def dialog_answered(cwd, session, ttl=lease.LEASE_TTL) -> None   # prewarm again for the rest of the turn
def turn_end(cwd, session, spoken) -> None   # release lease; decide chime if the turn said nothing
```

## voiceover/speech.py

```python
def speak(text, min_level="concise", cwd=None, interrupt=False, full=False) -> bool  # True = dispatched; full skips word-truncation (prose)
    # gates: is_tts_enabled + level_at_least(min_level); truncates via truncate_for_speech;
    # honors tts lock (skip if locked & not interrupt; interrupt -> stop_speech first);
    # engine dispatch:
    #   kokoro       -> subprocess: uv run --project <plugin>/tts <plugin>/tts/kokoro_voice.py --voice <voice> --stream <text>
    #   macos-female -> subprocess: python3 <plugin>/tts/macos_say.py --voice Samantha <text>
    #   macos-male   -> subprocess: python3 <plugin>/tts/macos_say.py --voice Daniel <text>
    #   none         -> no-op
    # spawn detached (Popen, no wait). DRY_RUN prints '[voiceover] <text>' to stderr.
    # Env VOICEOVER_NO_SPAWN (test harness): _spawn_detached reports success and starts nothing.
def enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None,
                   utterance=None, sticky=False, shown=None, source=None) -> bool
    # speak()'s gating and truncation, queued into the project's spool; never fails because audio
    # is playing. utterance groups items into one message; sticky = never dropped, played whole;
    # shown/source are for latency logging. The voice is resolved for the project's engine.
def ensure_drainer(cwd=None) -> bool
    # start THIS PROJECT's engine (`--drain <project dir>`) when its queue has items or a session
    # holds a lease (the pre-warm), unless a live lock whose owner pid still runs holds it (a dead
    # owner counts as free) or one was spawned <3s ago (<project>/spawned.json; the engine clears it
    # once loaded). Also removes the 1.3 machine-wide queue. Called unconditionally by narrator paths.
def prewarm(cwd=None, session=None, ttl=lease.LEASE_TTL) -> bool   # lease.hold + ensure_drainer
def request_stop(cwd=None, session=None) -> None
    # ask the project's engine to stop session's sentence now (None: any): <project>/stop/<slug|_all>.json
    # {"ts"}. The engine honours only items queued before the request.
def stop_speech() -> None                     # one-shot speaker only (lock_path()); also clears stale lock
def play_sound(name: str, cwd=None) -> None   # 'notification' | 'decide'; gated by notification_sounds
def truncate_for_speech(text: str, limit: int = 220) -> str   # port semantic_truncate suite
def lock_path() -> Path                       # data_dir()/tts.lock: the ONE-SHOT speaker's lock (expiry timestamp inside)
def project_lock_path(cwd=None) -> Path      # <project dir>/tts.lock: that project's drainer lock
def lock_is_live(path=None) -> bool          # unexpired lock at path (default lock_path()); read-only, never deletes
```

Level gating convention (callers pass min_level):
- quiet: sounds only (play_sound works at quiet+; speak() at quiet is used for nothing today)
- concise: permission requests, cycle completion
- verbose: pre/post tool play-by-play, subagent completions (EXACTLY verbose)
- narrator: Claude's actual prose via voiceover/prose.py (peek_new_prose/commit_offset,
  byte-offset cursor in data_dir()/prose_state.json); tool chatter silent.
  Prose is QUEUED via speech.enqueue_speech() into voiceover/spool.py and the cursor
  advances on enqueue, so text is never lost to a busy speaker and never re-read.
  Each PROJECT has its own queue, voice and engine. ensure_drainer(cwd) starts that
  project's engine in --drain mode; it holds the project's lock, loads its model once
  per turn (a session's lease keeps it up, turn end releases it) and speaks the spool one
  sentence at a time. A newer message preempts the current one at the sentence boundary
  (the jump rule, tts/engine_common.py); sticky items (dialog alerts, permission
  requests) are never dropped and play whole. The user's own next message
  (hooks/user_prompt_submit.py -> narrator.cutover) clears that session's queue and
  requests a stop mid-sentence. At most two projects' engines run at once.
  Headless runs (narrator.headless()) are not narrated.

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

## voiceover/prose.py

```python
def peek_new_prose(transcript_path) -> tuple      # (speech_text | None, new_offset); cursor NOT advanced
def peek_new_prose_stamped(transcript_path) -> tuple
    # (speech_text | None, new_offset, shown): shown = when the first unread prose row was
    # written (unix seconds, from its transcript timestamp), or None
def commit_offset(transcript_path, offset) -> None   # advance the cursor after the text was queued
def skip_unread(transcript_path) -> None
    # mark everything written so far as narrated (the mod already spoke it), so a later
    # fallback never replays it
def clean_for_speech(markdown_text) -> str
```

## voiceover/spool.py

The narration queue: one per project (`project_dir(cwd)/queue/`), one file per pending
utterance. Hooks WRITE here; engines READ through `tts/engine_common.py`. The two halves
share only the directory path and the filename sort rule. Every function takes `cwd=None`
(the project's cwd).

```python
QUEUE_MAX_AGE_SECONDS = 300                   # backlog cap, pruned on enqueue
def queue_dir(cwd=None) -> Path               # <project dir>/queue/
def enqueue(text, engine, voice, session=None, utterance=None, sticky=False,
            shown=None, source=None, cwd=None) -> bool   # utterance None = a message of its own
def clear(session=None, cwd=None) -> int      # all pending items, or one session's
def clear_sticky(session, cwd=None) -> int    # retire a session's sticky alerts/permission requests
def pending_count(cwd=None) -> int
def prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS, cwd=None) -> int   # also stale *.taken / *.tmp
def remove_legacy_queue() -> int              # delete 1.3's machine-wide data_dir()/queue/
```

Item filename `<epoch_ms:013d>-<pid>-<seq:02d>.json` — lexicographic order is
play order. Item content `{"text", "engine", "voice", "created", "session", "utterance",
"sticky", "shown", "source"}`: items sharing an `utterance` are one message; `sticky` items
are never skipped and play whole; `shown` (when the text appeared on screen) and `source`
(the hook that queued it, or `"mod"` for the narrator mod) are for latency logging, or None.
Writes are a temp file plus `os.rename`, so a reader never sees a partial item.
`*.taken` is an item a reader has claimed; `*.tmp` is a write in flight. Only
`*.json` counts as pending.

## tts/engine_common.py

Stdlib-only sibling imported by BOTH engines (they may not import `voiceover/`).
The single copy of the data-dir and lock helpers, plus the spool reader, the jump rule
and the drain loop. Every lock and spool helper takes `root`: a project's directory for a
drainer, `data_dir()` for the one-shot speaker's lock.

```python
MAX_ENGINES = 2                      # projects' engines running at once; a third steps aside
def data_dir() -> Path
def drain_root(argv) -> Path | None  # the directory after --drain, or None
def tts_lock_path(root) -> Path      # <root>/tts.lock
def lock_is_live(root) -> bool       # unexpired AND its pid still runs (a dead owner = free)
def try_claim_lock(root, duration) -> bool   # atomic (os.link); takes over an expired or dead-pid lock
def update_lock_expiry(root, duration) -> None
def remove_tts_lock(root) -> None
def queue_dir(root) -> Path          # <root>/queue/
def take_oldest(root, engine_names=None) -> tuple   # (item, taken_path) | (None, None); atomic claim
def finish(taken_path) -> None       # spoken: discard it
def put_back(taken_path) -> None     # not ours: restore its original name and place
def engine_log(tag, message) -> None # data_dir()/voiceover.log, when the debug_log setting is on
def split_sentences(text) -> list    # after . ! ? + whitespace, and at newlines
def drop_superseded(root, engine_names=None) -> int   # the jump rule (below); returns how many dropped
def next_item(root, engine_names=None)                # jump rule, then take_oldest
def peek_next(root, engine_names=None)                # the item next_item would play, read WITHOUT claiming
def newer_utterance_pending(root, utterance, engine_names=None) -> bool
def stop_requested(root, item) -> bool   # a stop for item's session (or _all) newer than item's "created"
def lease_is_live(root) -> bool          # any unexpired <root>/leases/*.json; read-only
def drain(root, open_speaker, engine_names, lock_seconds=60.0, poll_seconds=POLL_SECONDS) -> int
```

The jump rule: the newest own pending item's message (`utterance`) is the one to speak.
Every older own item of another message is dropped unspoken, except sticky items. A different
message ending the current one is checked at each sentence boundary, so a new message
preempts the old at the end of its current sentence; sticky items play whole, and only a
stop request (the user's own prompt) ends one early, mid-word. Items of another engine are
never touched.

Speakers (`open_speaker()` returns one, or falsy on failed setup) provide
`prefetch(sentence, voice)` and `begin(sentence, voice, upcoming=None) -> Playback`;
a `Playback` has `poll() -> bool` (True once over) and `kill()`. `ProcessPlayback(process,
cleanup=None)` wraps a child process; `DryRunSpeaker` prints `[voiceover] <sentence>`.
`drain()` prefetches the next item's first sentence during an item's last one.

Each spoken sentence logs (`engine_log("sound", ...)`, debug_log setting on):
`source=<source> utterance=<utterance> lag_created=<s> lag_shown=<s> chars=<n> :: <first 60 chars>`.
A missing `source`, `utterance` or `lag_shown` prints as `-`.
`utterance` is the item's message id, so a log reader can see which sentences were one message.

`drain()` claims the lock BEFORE `open_speaker()` loads anything, and tracks ownership with
an internal flag: it releases the lock only when it holds it, and never deletes one another
engine owns. It stays up while `lease_is_live(root)` (polling), and exits once the spool is
dry and no lease remains. If `MAX_ENGINES` other projects' engines already hold locks it
steps aside; if `open_speaker()` fails it releases the lock and backs the hooks off for 30s.

The spawn marker `<root>/spawned.json` (`{"ts"}`) is written by `speech.ensure_drainer`
before a spawn and read by it to back off for 3s; the engine clears it once loaded (or
writes it ahead on a cap/failed load so the hooks do not respawn per call).

## voiceover/audio_player.py, voiceover/process_utils.py

Straight ports (cross-platform playback / TTS-process kill). One fix: make the Unix kill
target only OUR processes — never a bare `pkill -f say`. `TTS_PROCESS_PATTERNS` match the
ONE-SHOT speaker only (`kokoro_voice.py --voice`, `macos_say.py --voice`, `say -v <Name>`,
the one-shot temp wav playback, `tts_controller.py`): narrator drainers run as
`--drain <project dir>`, speak through `say --voice=<Name>` and play wavs from their
project's `play/` dir, so a concise-level interrupt can never cut off another project's
narration. `process_utils.lock_owner_is_running(lock_path)` / `pid_is_running(pid)` give
the lock-owner liveness check hooks use.

## tts/

- `tts/pyproject.toml` — name "claude-voiceover-tts", deps: kokoro-onnx>=0.4.9, soundfile>=0.13.1, numpy>=1.24.0
- `tts/kokoro_voice.py --drain <project dir>` / `tts/macos_say.py --drain <project dir>` —
  the narrator engines: `engine_common.drain()` over that project's spool. Kokoro's
  `open_speaker` loads the model once and warms it up; its speaker synthesizes the next
  sentence on a worker thread while one plays. macOS speaks each sentence with
  `say --voice=<Name>` (text on stdin).
- `tts/kokoro_voice.py --voice <voice_id> [--stream] <text>` — the one-shot speaker, port as-is (13 voices, per-voice speed, model
  auto-download to ~/.kokoro-tts/models, plays via voiceover/audio_player logic duplicated
  locally or inline — this file may NOT import voiceover/ (different process/venv); it stays
  self-contained like today).
- `tts/macos_say.py --voice <Name> <text>` — collapse of the 3 old macos_*_tts.py scripts;
  stdlib only; uses `say`.
- `tts/tts_controller.py stop` — kill switch: kills every lock owner's process group (the
  one-shot speaker's lock and every project's `projects/*/tts.lock`, pid verified against
  our engine names), pattern-kills our engine and audio-child lines, then `clear_projects()`
  drops every project's lock, spawn marker, stop files, queue, leases and wavs.

## hooks/

`hooks/hooks.json` registers all seven events (PreToolUse, PostToolUse, Notification, Stop,
SubagentStop, UserPromptSubmit, SessionEnd) with command:
`bash "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" <hook_name>` (timeout 10 for all; Stop gets 30).

`hooks/run.sh` — probe python3 → python → py -3 (Windows Store stub detection à la
security-guidance), UTF-8 env (PYTHONIOENCODING=utf-8), cygpath conversion on Git Bash,
exec `$PYTHON "$PLUGIN_ROOT/hooks/<name>.py"` with stdin passthrough; ALWAYS exit 0.

Each hook: read JSON from stdin, `sys.path.insert(0, plugin_root)` then `from voiceover import ...`.

At the narrator level every path except `user_prompt_submit` and `session_end` works in the
session's PINNED cwd (`narrator.pinned_cwd(session, payload cwd)`: the project the turn's
prompt was sent from, wherever Claude cds to), and returns at once when `narrator.headless()`.
`user_prompt_submit` repins at every prompt.

Lease table (narrator level; `FALLBACK_LEASE_TTL` unless noted):

| Hook | Lease |
|------|-------|
| user_prompt_submit (user's prompt) | held (pre-warm) via `narrator.cutover` |
| user_prompt_submit (task notification) | held (pre-warm); nothing cut off |
| pre_tool_use | held; released instead for a dialog tool (`queue_dialog`) |
| post_tool_use | held; a dialog tool's answer re-holds via `dialog_answered` |
| stop | narrator level: released by `narrator.turn_end` (not at all when the mod narrates the session; the mod's `turn-end` op does it). Any other level: `lease.release` directly. The narrator level also releases when there is no transcript path; headless runs return before narrating |
| session_end | released |

Stand-down: at the narrator level `pre_tool_use`, `post_tool_use` and `stop` return after
`narrator.stands_down(session, transcript)` when the mod narrates the session, and
`user_prompt_submit` returns at once when `narrator.mod_owns_prompt(session, cwd)` is true (the mod's
bridge re-pins and cuts over). Below the narrator level the classic hooks run as before, mod or not.

- `pre_tool_use.py` — narrator: queue unread prose (`source="pre_tool_use"`), hold the lease,
  `ensure_drainer`; for `AskUserQuestion` / `ExitPlanMode` wait out the transcript flush race
  for the lead-in prose, then `narrator.queue_dialog` (sticky alert). Other levels: stop_speech
  (one-shot only), then verbose-level announcement via pre_tool_announcement.
- `post_tool_use.py` — narrator: `spool.clear_sticky(session)` first, in mod sessions too (a
  finished tool means the session's pending alert or permission request was answered; do not
  read it out), then stand down for a mod session; otherwise hold or re-hold the lease, queue
  unread prose. Verbose: announcement via post_tool_announcement.
- `notification.py` — clears a stale one-shot lock; permission requests: concise+ speaks
  permission_request_message (narrator: queued STICKY, `source="notification"`, unless this
  session's dialog alert was announced <30s ago); quiet plays 'notification' sound.
- `stop.py` — concise+ speaks completion_message(cycle_stats(transcript_path)); narrator queues
  the unread closing prose (back of the queue) then `narrator.turn_end`; quiet plays 'decide'
  sound. Detect stop_hook_active to avoid loops.
- `subagent_stop.py` — verbose + speak_subagent_completions -> subagent_completion_message.
- `user_prompt_submit.py` — `narrator.repin`, then a new user message ends the previous turn's
  narration: narrator level -> `narrator.cutover` (spool.clear(session) then request_stop, then
  pre-warm; a background task's notification only pre-warms); other levels -> spool.clear(session)
  then stop_speech() (one-shot speaker only).
- `session_end.py` — releases the session's lease (under its pinned cwd) and drops its pin and
  its mod mark.
  A restart is needed for Claude Code to register a newly added hook event.

## hooks/mod_bridge.py

The mod's single entry into the narrator: `bash hooks/run.sh mod_bridge`, one JSON batch on
stdin. The mod only captures text; every policy (gating, cleaning, voices, queue, engine)
lives here and in `voiceover/narrator.py`, shared with the classic hooks.

```
{"session": str, "cwd": str, "ops": [op, ...]}      cwd = the session's root
```

Every op acts in the session's pinned project (`narrator.pinned_cwd(session, root)`), wherever the
root has moved mid-turn; only `prompt` re-pins, to the root.

| Op | Does |
|----|------|
| `{"op": "start"}` / `{"op": "status"}` | `narrator.pin_cwd`, only for a session that has no pin; `status` is also how the mod re-asks the gate |
| `{"op": "end-session"}` | `lease.release`, `narrator.unmark_mod_session` |
| `{"op": "prompt", "cutover": bool}` | acts only when `narrator.mod_owns_prompt(session, cwd)`; then `narrator.repin` to the root (the gate is re-read there), and with `cutover` (not a headless run, whatever the gate says): `narrator.abandon(previous, session)` when the project moved, then `narrator.cutover(cwd, session, warm=False)` |
| `{"op": "hold"}` | a turn or step began: `prewarm` (narration on only) |
| `{"op": "say", "utterance", "text", "shown"}` | `speakable(text)`, then `enqueue_speech(full=True, source="mod", utterance=, shown=)`; `ensure_drainer` once per batch if anything queued (narration on only) |
| `{"op": "dialog", "tool", "input", "utterance"}` | narration on: `narrator.queue_dialog(..., utterance=)`; off: `lease.release` |
| `{"op": "turn-end", "spoken": bool}` | narration on: `narrator.turn_end` (its decide chime is played detached, so the bridge answers at once); off: `lease.release` |

Every batch first calls `narrator.mark_mod_session(session)` (`/clear` and resume change the id
with no `session.start`, and the classic hooks must stand down from the new session's first op),
then reads the gate `narrator.narrates(cwd)` once, in the pinned project (the `prompt` op reads it again at the root, after the move). The gate decides per op:
`start` and `status` pin a session without a pin, whatever the gate says; `end-session` always releases and unmarks;
`prompt` acts only when `narrator.mod_owns_prompt` is true, and its cutover does not depend on the gate (headless runs skip it); `turn-end` and `dialog` act when the gate is on and only release the lease when it is
off; `hold` and `say` run only when the gate is on. `speakable` cleans markdown with `prose.clean_for_speech` and strips any `*`
or backtick a mid-markup split left unpaired.

Reply: exactly one stdout line, `{"narrate": true|false|null}` - the gate as of the call; `null`
after an internal error. Always exits 0.

## The mod (hooks/mod.json and the .ts files)

`.claude-plugin/plugin.json` names `"hooks": "./hooks/mod.json"`, an ADDITIONAL hook file beside
the auto-discovered `hooks/hooks.json`. `hooks/mod.json` is `{ "modules": ["./narrator.ts"] }`.
Claude Code 2.1.287+ loads it (mods on by default); older builds, or builds with mods off, ignore
it and the classic hooks narrate. TypeScript tests sit beside the sources (`*.test.ts`).

- `narrator.ts` - registers the mod's event handlers: `session.start` (open the gate with a
  `start` op), `session.end` (`end-session`, awaited), `prompt.submit` (`prompt` op, never
  awaited, `cutover` from `isUsersOwnPrompt` and not a `wait` prompt), `turn.start` (`hold`), `turn.step` (main loop
  only, never a subagent: every text chunk is yielded first, then fed to one segmenter per block;
  each completed segment is a `say` op; a block's last sentence is flushed when the next block
  starts, stamped `shown` with when that block's last text arrived, or at a clean stop; on an
  interrupt a half sentence is dropped), `tool.call` for `AskUserQuestion` / `ExitPlanMode` (a `dialog` op,
  awaited so the alert queues before the permission echo can speak; then `hold`), and
  `turn.complete` (`turn-end` with whether the step queued speakable text, or `true` when the turn was interrupted so there is no chime; main loop only).
  One utterance id per response: `<turnId>:<step index>`. A single queue sends ops to the bridge
  in order, batching those that arrive during a call (timeout 5s); each call runs on the newest
  sender's `$`, so a batch never rides an interrupted dispatch.
  Deferred: in a mod session, a level or TTS switched ON mid-turn takes effect at the next prompt.
  - The gate: `openGate` sends an op; if the answer is unknown (`null`) it asks once more with
    the same op, and if still unknown it reads as off and leaves no gate, so the next op asks again.
    `currentGate` reuses the turn's gate or opens one with `status`. One failed bridge call never
    silences a turn.
  - The `$` rule: the engine refuses a module that stores the engine handle `$`, puts it in an
    object, or passes it to a function in another file. Every function that needs `$` takes it as a
    parameter and lives in `narrator.ts`; module state holds no `$`. Other files are pure.
- `segment.ts` - `createSegmenter()` with `push(text)` and `flush()`: cuts streamed markdown at
  `.` `!` `?` followed by whitespace, and at newlines. A fenced block (a ``` that opens a line,
  after any spaces or tabs, so list-item code counts) is held until it closes and emitted whole;
  ``` inside a line is text. Scanning resumes where it stopped (linear time).
- `origin.ts` - `isUsersOwnPrompt(origin)`: false for the automated origins (task-notification,
  scheduled-trigger, peer, peer-send-message, projects-relay, channel, coordinator, observer,
  observer-activity, auto-continuation, plugin); true for anything else, including an unknown or
  missing origin. Only the user's own prompt cuts narration off.
- `bridge.ts` - the `Op` type for the table above, and `parseReply(stdout)`, which reads the last
  stdout line. It returns `{ narrate: boolean | null }`; null (no output, a crash, bad JSON, the
  bridge's own null) means unknown.

While a session is marked, the classic hooks do not speak or queue for it (see Stand-down under
hooks/), and Stop does not end its turn; `turn.complete` does. Items the mod queues have
`source="mod"` and go through the same project queue, lease and engine as the hooks' items.

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
- `level.md` (/voiceover:level) — silent|quiet|concise|verbose|narrator or 0-4 via manage_settings.py
- `setup.md` (/voiceover:setup) — run setup_tts.py, guide model download + voice pick
- `migrate.md` (/voiceover:migrate) — run migrate_legacy.py flow with confirmation
All commands invoke scripts with `"${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py"` paths.

## .claude-plugin/

- `plugin.json` — name "voiceover", displayName "Claude Voiceover", version 1.4.0,
  description packed with search terms (voice, text-to-speech, TTS, narration, notification
  sounds, Kokoro, offline, local), author Hanan (okets), homepage/repository
  https://github.com/okets/claude-voiceover, license MIT.
  The manifest must not name `hooks/hooks.json` or `commands/`: Claude Code
  auto-discovers both, and naming either one makes it load the file twice and
  refuse the plugin ("Duplicate hooks file detected"). It names `./hooks/mod.json`,
  the mod's own file, which is an ADDITIONAL hook file.
- `marketplace.json` — marketplace name "claude-voiceover", owner okets, one plugin entry
  with source "./" so `/plugin marketplace add okets/claude-voiceover` works directly.
