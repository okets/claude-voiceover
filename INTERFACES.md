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
    "interaction_level": "concise",     # silent | quiet | concise | tools
    "tts_engine": "auto",               # auto | kokoro | phonon | macos-female | macos-male | none
    "tts_enabled": True,
    "notification_sounds": True,        # quiet-mode mp3 pings
    "voice": None,                      # explicit voice for the engine (Kokoro id or Phonon name), else per-project hash
    "speak_subagent_completions": True, # tools level only
    # machine-wide only (global settings file; read by the engine too, see "Shared engine"):
    "tts_device": "cpu",                # cpu | gpu
    "engine_port": None,                # TCP port of the shared Kokoro engine on 127.0.0.1; null = socket only
    "engine_idle_s": None,              # seconds the engine keeps the model; null = 330 with a port, else 60
    "engine_plugin_root": None,         # plugin directory the engine runs from; null = this install
}

LEVELS = ["silent", "quiet", "concise", "tools", "narrator"]    # numeric aliases 0..4 accepted everywhere
LEGACY_LEVEL_NAMES = {"verbose": "tools"}                       # old names still resolve, stored as the new one

def data_dir() -> Path
def get_setting(key: str, cwd: str | None = None)            # hierarchy lookup
def set_setting(key: str, value, scope: str = "global", cwd: str | None = None)
    # scope "global" writes data_dir()/settings.json; "project" writes <cwd>/.claude/voiceover.json
def get_interaction_level(cwd=None) -> str                   # normalized string
def level_at_least(minimum: str, cwd=None) -> bool           # ordering per LEVELS
def is_tts_enabled(cwd=None) -> bool                         # tts_enabled and level != silent
def resolve_engine(cwd=None) -> str
    # explicit "kokoro" | "phonon" | "macos-female" | "macos-male" | "none" as given;
    # "auto": macOS -> "macos-female"; else "kokoro" if models_present() else "none" (never phonon)
def get_voice(cwd=None) -> str
    # engine-aware. Under phonon: the explicit voice if is_phonon_voice(), else a stable pick from
    # PHONON_VOICES (standard voices only) by sha1(project_root). Under any other engine: any
    # explicit string as given, except a Phonon name, which falls back to the stable pick from
    # KOKORO_VOICES by sha1(project_root) — the "random voice per project" behavior, stateless
def models_present() -> bool                                  # ~/.kokoro-tts/models/*.onnx exists
def is_phonon_voice(name) -> bool             # one of the 37 catalogue names or "default", exact case
def phonon_model_present() -> bool
    # <hub>/models--gradium--phonon-31m-en/snapshots/*/model.q8.gguf exists, <hub> = $HF_HOME/hub
    # or ~/.cache/huggingface/hub (measured in the spike). Reads the folder; never raises
def detect_project_root(cwd=None) -> Path
    # nearest .claude dir, else git root, else cwd. ~/.claude is Claude Code's own user
    # configuration, NOT a project marker: accepting it would give every project without a
    # .claude of its own the home directory as its root (one shared queue and voice).

KOKORO_VOICES = [...all 13 ids...]  # af_alloy? -> use the real 13 ids from old kokoro_voice.py
PHONON_VOICES = [...25 standard names...]          # Zoey ... Bryony, catalogue order and case
PHONON_CHARACTER_VOICES = [...12 character names...]  # Captain Silas Blackwater ... Fairy Godmother
PHONON_DEFAULT_VOICE = "default"                    # bundled with the model; the offline fallback
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
    #   kokoro       -> subprocess: kokoro_command("--voice", <voice>, "--stream", <text>): Linux/macOS run
    #                   plain `python3 <plugin>/tts/kokoro_voice.py ...` (a client of the shared engine);
    #                   Windows runs `uv run --project <plugin>/tts[-gpu] <plugin>/tts/kokoro_voice.py ...`
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
    # One exception to the live-lock rule (engine switch): when the lock records "engines", the
    # resolved engine is not among them and items are queued for the resolved engine, it spawns the resolved engine anyway
    # (the 3s back-off still applies). That newcomer waits for the holder to hand over; no hook waits.
    # (the "engine" here is the project's narrator drainer; on Linux/macOS it is a client of the shared engine)
def prewarm(cwd=None, session=None, ttl=lease.LEASE_TTL) -> bool   # lease.hold + ensure_drainer
def kokoro_command(*args) -> list    # uv run --project <tts or tts-gpu> <plugin>/tts/kokoro_voice.py *args
def phonon_command(*args) -> list    # uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py *args
def macos_say_command(*args) -> list # python3 <plugin>/tts/macos_say.py *args
def one_shot_command(engine, voice, text, stream=True) -> list | None
    # the ONE builder of a one-shot command line (dispatch, the voice sample, setup's test):
    # neural engines get --voice <voice> [--stream] <text>; macOS engines their own `say` voice
    # The drain command and the one-shot dispatch look the neural engines up in one table
    # (kokoro -> kokoro_command, phonon -> phonon_command); macOS voices use macos_say_command.
def request_stop(cwd=None, session=None) -> None
    # ask the project's engine to stop session's sentence now (None: any): <project>/stop/<slug|_all>.json
    # {"ts"}. The engine honours only items queued before the request.
def stop_speech() -> None                     # one-shot speaker only (lock_path()); also clears stale lock; never the shared engine
def kokoro_command(*args) -> list             # argv for the Kokoro narration client (plain python off Windows, uv run on Windows)
def play_sound(name: str, cwd=None) -> None   # 'notification' | 'decide'; gated by notification_sounds
def truncate_for_speech(text: str, limit: int = 220) -> str   # port semantic_truncate suite
def lock_path() -> Path                       # data_dir()/tts.lock: the ONE-SHOT speaker's lock (expiry timestamp inside)
def project_lock_path(cwd=None) -> Path      # <project dir>/tts.lock: that project's drainer lock
def lock_is_live(path=None) -> bool          # unexpired lock at path (default lock_path()); read-only, never deletes
```

Level gating convention (callers pass min_level):
- quiet: sounds only (play_sound works at quiet+; speak() at quiet is used for nothing today)
- concise: permission requests, cycle completion
- tools: pre/post tool play-by-play, subagent completions (EXACTLY tools)
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
    # stats.final_response_text, cleaned for speech, at any length; a cycle that
    # left no prose -> templated stats summary, else a fallback phrase
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
def pending_count(cwd=None, engine=None) -> int   # engine given: only items queued for it
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
def update_lock_expiry(root, duration, engines=None) -> None   # a drainer passes its engine names
def lock_holder_engines(root) -> frozenset | None   # None: no live lock; empty: holder records none
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
def foreign_pending(root, engine_names, since) -> bool  # another engine's item, created after `since`
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
A speaker may also define `release_output()`: `drain()` calls it each time it finds nothing of its own
to speak, before it waits on a lease or exits. It is called on every such poll, so it must be
near-free when there is nothing to release, and return within 200 ms. Speakers without it are
unaffected. A speaker's `idle()` is separate: it is called only while the drain waits on a live
lease (the shared-engine client re-warms the engine there).

Each spoken sentence logs (`engine_log("sound", ...)`, debug_log setting on):
`source=<source> utterance=<utterance> lag_created=<s> lag_shown=<s> chars=<n> :: <first 60 chars>`.
A missing `source`, `utterance` or `lag_shown` prints as `-`.
`utterance` is the item's message id, so a log reader can see which sentences were one message.

`drain()` claims the lock BEFORE `open_speaker()` loads anything, and tracks ownership with
an internal flag: it releases the lock only when it holds it, and never deletes one another
engine owns. It stays up while `lease_is_live(root)` (polling), and exits once the spool is
dry and no lease remains. With nothing of its own to speak, it hands over: when
`foreign_pending(root, engine_names, started)` is true (`started` = when it claimed its lock),
it logs `handover` and exits even under a live lease, so the next hook starts the engine those
items name. It never abandons a sentence it is speaking and never touches the other engine's
items. A foreign item queued before it started does not count, so a leftover item for an engine
nobody starts cannot make every new drainer exit at once.

Lock content is `{"expiry", "pid"}`; a drainer's lock adds `"engines"`, the sorted engine names it
drains, written at every claim and kept by every expiry refresh. One-shot locks and older locks
have no such key and behave as before.

A newcomer waits for an engine that is handing over. When `drain()`'s first claim fails and the
live lock records engines disjoint from its own, and it has pending items of its own, it polls
every `poll_seconds` until the claim succeeds, none of its own items remain, the holder stops
being another engine, or `QUEUE_MAX_AGE_SECONDS` pass. While waiting it loads nothing
(`open_speaker()` runs only after the claim) and refreshes `spawned.json` about once a second so
the hooks start no second waiter. A holder with one of its engines, or recording none, makes it
exit 0 at once as before. A speaker's `release_output()` that raises is logged (`error`) and the drain goes
on. If `MAX_ENGINES` other projects' engines already hold locks it
steps aside; if `open_speaker()` fails it releases the lock and backs the hooks off for 30s.

The spawn marker `<root>/spawned.json` (`{"ts"}`) is written by `speech.ensure_drainer`
before a spawn and read by it to back off for 3s; the engine clears it once loaded (or
writes it ahead on a cap/failed load so the hooks do not respawn per call).

## tts/pcm_sink.py

Stdlib only at import (`sounddevice` is imported inside `SoundDeviceSink`). Plays raw 24 kHz
mono float32 audio as it arrives, for a streaming engine (Phonon). Chunks need only `len()`
(samples) and `tobytes()`; nothing imports numpy.

```python
SAMPLE_RATE = 24000
class Sink:
    def write(self, chunk) -> None   # play after what is written; opens the output on demand
    def abort(self) -> None          # silence now, discard buffered audio; the next write plays again
    def close(self) -> None          # release the output; idempotent and near-free when closed;
                                     # within 200 ms (a PortAudio stop+close is ~115-125 ms on macOS)
    def remaining(self) -> float     # seconds written but not yet played
class SoundDeviceSink(Sink)          # PortAudio, callback-fed (latency="low", blocksize=480)
class PipeSink(Sink)                 # PipeSink(command): a player process fed on stdin
def player_command() -> list | None  # first player on PATH: aplay, paplay, pw-cat, ffplay
PLAYER_NAMES = ("aplay", "paplay", "pw-cat", "ffplay")   # in that order; setup reports them
def open_sink() -> Sink | None       # SoundDeviceSink, else PipeSink, else None; never raises
```

`SoundDeviceSink` never calls a stream's blocking `write()`: the spike measured that `write()` on
one thread with `abort()` from another crashes the process. The worker appends to a buffer the
sink owns; PortAudio's callback copies from it and pads with zeros (a partial last block plays at
once). `abort()` only empties that buffer and never calls into PortAudio (silence reaches the
device ~25-30 ms later, measured). `close()` calls the stream's `stop()` (so a tail in the device
plays out) then `close()`. `remaining()` is the bytes in the sink's own buffer; the device's
~25 ms output latency is not added. Construction fails (and `open_sink()` falls back) when the
import fails (no PortAudio: the normal case on Linux, measured) or `check_output_settings` finds
no usable output.

`PipeSink`: `remaining()` by wall clock (each write extends an end time from `max(now, end)`);
`abort()` kills the player; `close()` closes its stdin without waiting. A player that died never
raises into the writer: that chunk is dropped and the next write starts a new player. Player
arguments: the `ffplay` line is measured (ffplay 9 rejects `-ac 1`; raw input defaults to mono);
the `aplay`, `paplay` and `pw-cat` lines are untested.

## tts/phonon_voice.py, tts-phonon/

The Phonon engine. Runs from its own uv project, `tts-phonon/pyproject.toml` (`ptts>=0.7.1`,
`sounddevice>=0.5`; nothing of Kokoro's), and imports `ptts` and `sounddevice` only inside
functions, so a dry run and the test suite load neither. Never imports `voiceover/`.

```text
uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py --drain <project dir>
uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py --voice <name> --stream <text>
phonon_voice.py --install              # load (downloading on first use) the model; 0, or 1 + stderr
phonon_voice.py --fetch-voice <name>   # load the model and render one word in <name>; 0, or 1
```

```python
MODEL_REPO = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"  # pinned
QUANT, DEVICE, THREADS = "q8_0", "cpu", 2
FALLBACK_VOICE = "default"             # bundled with the model; works offline
def load_model()                       # ptts.set_num_threads(2); ptts.TTS(MODEL_REPO, lang="en", quant, device)
class PhononSpeaker:                   # PhononSpeaker(tts, sink, grace_seconds=10.0)
    def warm_up(self)                  # renders "Ready." in "default"; writes nothing, opens no device
    def prefetch(self, sentence, voice)    # no-op: a warm first chunk takes < 35 ms
    def begin(self, sentence, voice, upcoming=None) -> PhononPlayback
    def release_output(self)           # sink.close(): a warm engine holds no device
class PhononPlayback(Playback)         # started(): a chunk was written; poll(); kill()
def open_speaker()                     # open_sink(), load_model(), warm_up(); None (logged) on failure
def speak_once(text, voice) -> bool    # the one-shot: owns data_dir()'s lock, released on every path
def parse_args(argv) -> (voice, text, use_streaming)
```

One worker thread per speaker renders sentences in order: it opens `tts.stream(sentence,
voice=...)`, writes each chunk to the sink, and closes the generator on every path (end, cancel,
error). A voice that cannot be loaded raises from `stream()` itself (`LookupError`, or `OSError`
after a ~2.6 s failed fetch): the sentence is spoken in `"default"`, logged as `voice`, and that
voice goes straight to `"default"` for the rest of the process. `poll()` re-raises a generation
error, and is true once the worker finished and `sink.remaining()` is 0; if the audio has not
drained within its length plus `grace_seconds` after the worker finished (a vanished device), it
aborts the sink, logs `stall`, and reports the sentence done. `kill()` sets the cancel flag,
aborts the sink (silence first), and returns without waiting for the generator; the cancel check
and each write happen under one lock, so no chunk reaches the sink after `kill()` returns. The
one-shot waits for `remaining()` to reach 0 before closing the sink, so the end is not cut off.
`--install` and `--fetch-voice` never open the audio device. `--fetch-voice` has no fallback.
A sentence that completes no write for `stall_seconds` (30 s; a `PhononSpeaker` argument)
while the worker is still running is killed by `poll()`, logged as `stall`, and reported done:
a player that stopped reading blocks the worker inside its write, and the abort ends that player.

Known limit (Linux, untested): a drainer's piped player is a child that reads stdin and matches
no kill pattern. The lock-owner process-group kill stops it whenever the engine holds its lock;
killed by pattern alone (only in the drain's brief exit gap), the engine dies and the player plays
out what its pipe holds (at most ~0.7 s) and exits at EOF. A possible fix, not made: have the kill
switch `pgrep -f` the engine patterns and `killpg` each match's group.

Known limit: if `ptts` itself hangs inside generation, that sentence is abandoned after 30 s, but
the single worker thread stays occupied, so later sentences stall too. The kill switch, or the
next prompt's cutover, ends the process.

## voiceover/audio_player.py, voiceover/process_utils.py

Straight ports (cross-platform playback / TTS-process kill). One fix: make the Unix kill
target only OUR processes — never a bare `pkill -f say`. `TTS_PROCESS_PATTERNS` match the
ONE-SHOT speaker only (`kokoro_voice.py --voice`, `phonon_voice.py --voice`, `macos_say.py --voice`, `say -v <Name>`,
the one-shot temp wav playback, `tts_controller.py`): narrator drainers run as
`--drain <project dir>`, speak through `say --voice=<Name>` and play wavs from their
project's `play/` dir, so a concise-level interrupt can never cut off another project's
narration. `process_utils.lock_owner_is_running(lock_path)` / `pid_is_running(pid)` give
the lock-owner liveness check hooks use. `ENGINE_SCRIPTS` (kokoro_voice.py, macos_say.py,
phonon_voice.py) is the one list a lock-owner pid's command line must match before its process
group is signalled. Phonon's one-shot plays in-process on macOS and Windows, so killing the
engine is the whole stop there.

## tts/

- `tts/pyproject.toml` — name "claude-voiceover-tts", deps: kokoro-onnx>=0.4.9, soundfile>=0.13.1, numpy>=1.24.0
- `tts/kokoro_engine.py` — the shared Kokoro engine (Linux/macOS), the only process that holds the
  model there; run through `tts/engine_launcher.py serve`. Its name deliberately matches no kill
  pattern. Protocol and lifecycle: "Shared engine (protocol 1)" below. Modules, all stdlib-only
  except `kokoro_engine.py`/`kokoro_model.py` (numpy, onnxruntime and kokoro-onnx are imported
  lazily): `engine_server`, `engine_connection`, `engine_protocol`, `engine_scheduler`,
  `engine_text`, `engine_lifecycle`, `engine_listeners`, `engine_paths`, `engine_client`, `engine_launcher`,
  `engine_speaker`, `engine_stub` (test synth), `voiceover_engine_shim.py`, and `kokoro_model.py`
  (model files, device choice, `KokoroSynth`, shared with the Windows in-process path).
- `tts/kokoro_voice.py --drain <project dir>` / `tts/macos_say.py --drain <project dir>` —
  the narrator drainers: `engine_common.drain()` over that project's spool. On Linux/macOS the
  Kokoro drainer is a client of the shared engine (`engine_speaker.EngineSpeaker`: it requests each
  sentence, writes a wav and plays it, prefetches the next, and warms the engine while it waits on
  a lease; when the engine has gone (exited or killed) it reconnects through `ensure`, starting a
  new one; it runs under plain `python3`). On Windows it keeps the in-process `KokoroSpeaker`
  (loads the model once, synthesizes the next sentence on a worker thread), spawned with `uv run`.
  macOS system voices speak each sentence with `say --voice=<Name>` (text on stdin).
- `tts/kokoro_voice.py --voice <voice_id> [--stream] <text>` — the one-shot speaker (13 voices, per-voice speed,
  model auto-download to ~/.kokoro-tts/models; ensures the model files before using the engine). On
  Linux/macOS it asks the shared engine (starting it with `ensure`); `--stream` submits one request
  per sentence up front and plays each sentence's audio whole, in order. On Windows it synthesizes in-process.
  This file may NOT import voiceover/; it stays self-contained.
- `tts/phonon_voice.py` (run from `tts-phonon/`) — the Phonon engine; see its own section above.
- `tts/macos_say.py --voice <Name> <text>` — collapse of the 3 old macos_*_tts.py scripts;
  stdlib only; uses `say`.
- `tts/tts_controller.py stop [--all]` — kill switch: kills every lock owner's process group (the
  one-shot speaker's lock and every project's `projects/*/tts.lock`, pid verified against
  our engine names, `ENGINE_SCRIPTS`), pattern-kills our engine lines (`kokoro_voice.py`,
  `macos_say.py`, `phonon_voice.py` — a drainer or one-shot, including a Phonon newcomer waiting
  without a lock) and audio-child lines, then `clear_projects()`
  drops every project's lock, spawn marker, stop files, queue, leases and wavs. It stops narration
  only: the shared engine and any interface request in flight are untouched. `--all` also sends
  the engine `stop` (through `engine_client`, every error ignored), cancelling whatever it was serving.

## Shared engine (protocol 1)

On Linux and macOS one engine process per user holds the Kokoro model and serves every
caller: the narrator's drainers, the one-shot speaker, and any other program the user
lets in. Callers send text and get PCM audio back. This section is the contract an
outside client can code against. The code is `tts/engine_server.py`,
`engine_connection.py`, `engine_lifecycle.py`, `engine_listeners.py`, `engine_protocol.py`, `engine_client.py`
(the stdlib client the plugin itself uses), `engine_launcher.py`, `engine_paths.py` and
`voiceover/engine_pointer.py`. Windows keeps the in-process `KokoroSpeaker` and has no engine.

### Transports and paths

Everything lives under `<state>/engine/` (`<state>` = the data dir: `$VOICEOVER_DATA_DIR`,
else `~/.claude-voiceover`; the directory is mode 0700, enforced on every use):

| File | What |
|------|------|
| `engine.sock` | the Unix socket (0600). Always served. No auth: file permissions are the guard |
| `engine.lock` | flock held by the one running engine (single instance); an engine adopted from systemd takes it too, and serves even if it is held (systemd guarantees one instance) |
| `start.lock` | flock that serialises starters (`ensure`) and pointer writes |
| `prepare.lock` | one `prepare` at a time |
| `token` | the TCP secret: 64 lowercase hex characters, mode 0600 |
| `current.json` | the version pointer (below) |
| `keep_off.json` | `{"until": <epoch s>, "reason": str}`; until `until`, an engine that starts (launchers still start it) loads on the CPU and reports `fallback.reason` `keep_off`, and leaves for `card_return` once it has expired |
| `engine.log` | the engine's own log, plus the stdout/stderr of the processes `ensure` spawns |

`<state>/bin/voiceover-engine` is the launcher shim (0755), the stable entry point for
the systemd unit and for other programs.

TCP is optional: when the machine-wide `engine_port` setting is an integer 1-65535 the
engine also listens on `127.0.0.1:<port>`, loopback only. Every accepted TCP socket gets
`TCP_NODELAY`. A port that cannot be bound (another program holds it) is logged to `engine.log`
and the engine serves the Unix socket only: narration is unaffected, TCP callers get no answer
until the port is free and the engine restarts. An AF_UNIX path is limited to 107 bytes on Linux (103 on macOS); a longer
`engine.sock` path makes the engine log one line to `engine.log` and exit 1, `ensure`
returns False, and `voiceover-engine serve` prints one stderr line naming the path and the
limit. Use a shorter `VOICEOVER_DATA_DIR`.

Settings (machine-wide, in `<state>/settings.json`): `engine_port` (null = socket only),
`engine_idle_s` (seconds the loaded model stays after the last activity; default 330 when
`engine_port` is set, else 60), `engine_plugin_root` (pin the plugin directory the engine
runs from), `tts_device` (`cpu` | `gpu`).

### Wire format

One UTF-8 JSON object per line (`\n` terminated, blank lines ignored). Audio events are a
header line followed by exactly `bytes` raw bytes. Audio is signed 16-bit PCM,
**little-endian**, mono, 24000 Hz, announced in `hello` as
`{"format":"pcm_s16le","rate":24000,"channels":1}`. Limits, also announced in `hello`:
`{"line":65536,"text":6000,"frame":32768}`: a line over 65536 bytes is `bad_request` (the
rest of that line is dropped); one request may carry at most 6000 characters of text in
total (`text_too_long`); an audio frame is even-length and at most 32768 bytes, so never
assume a frame size, and a sentence may arrive as several frames.

Client rules that matter in practice:
- After each `audio` line read exactly `bytes` bytes, then go on with lines.
- Behind an HTTP `CONNECT` proxy, read the proxy's reply exactly up to the blank line
  (`\r\n\r\n`). A buffered reader may swallow audio bytes that follow it.
- Decode UTF-8 only on whole lines; a multi-byte character may be split across reads.
- A request that fails gets an `error` with its `id` and is then always closed by an `end`; see the error rule below.

### Auth (TCP only)

The first line on a TCP connection must be `{"op":"auth","token":"<token>"}`. A wrong,
missing or non-first auth line gets `{"ev":"error","code":"unauthorized",...}` and the
connection is closed; `unauthorized` means the token is wrong: do not retry, tell the
owner (`voiceover-engine token` prints it, `rotate-token` replaces it). A connection that
has not authenticated within 5 s is closed silently. `auth`, `hello` and the first `start`
may be pipelined in one write. After authenticating, an `auth` line is accepted and ignored.
The Unix socket needs no auth line (an `auth` line there is ignored).

### Ops (client to engine)

`hello` must come first (anything else before it: `bad_request` "hello comes first").
Unknown ops and non-object lines get `bad_request`.

| Op | Fields | Effect |
|----|--------|--------|
| `hello` | `proto` (1) | Replies `hello`. A different proto: `unsupported_protocol`, then the connection closes. Missing proto: `bad_request` |
| `status` | | Replies `status` |
| `start` | `id` (string, unique among the connection's active requests), `voice` (string), optional `text`, `end` (true), `speed` (0.5-2.0), `lang` (default `en-us`), `live` (true) | Opens a request. `text` is fed as if sent by `text`; `end:true` also ends the input |
| `text` | `id`, `text` | Appends text to an active request |
| `flush` | `id`, `n` (integer) | Speak what is buffered now (a trailing partial sentence too) and, when every sentence queued so far has been rendered and sent, reply `flushed` with the same `n` |
| `end` | `id` | No more text: the buffered rest is spoken, then the request completes |
| `cancel` | optional `id` (string) | Cancels that request, or every request on the connection when `id` is absent. An unknown id is ignored; a non-string `id` gets `bad_request` with that value as `ref` |
| `warm` | optional `hold_s` (number, default 0, capped at 600), optional `id` | Loads the model if needed and holds the engine up for `hold_s` seconds. Replies `warm` once loaded; the optional `id` is echoed in that reply (as `id`) and in a `warm` error (as `ref`) |
| `release` | optional `keep_off_s` (finite number 0-86400, default 0) | Leave now: replies `released`, ends open requests, writes `keep_off.json` when `keep_off_s` > 0, exits. Anything else (not a number, NaN, Infinity, negative, over 86400) gets an id-less `bad_request` and the engine stays |
| `stop` | | Exit now. Open requests end `cancelled`; there is no reply |
| `auth` | `token` | TCP only; see above |

Without a `speed`, the voice's own default applies (the `VOICE_SETTINGS` speed, else 1.0).
Text is split into sentences at `. ! ?` followed by whitespace, and at newlines; a sentence
over 40 words is split at the clause boundary nearest its middle. Text with no sentence
punctuation is spoken when the input ends or is flushed (`start` "ok" then `end` speaks
"ok"); whitespace-only text with `end` gives `end`/`complete` with `samples` 0 and no
synthesis. Sentences of one connection are rendered in strict order; requests on one
connection are served in start order (only the oldest renders; a later one waits for it),
and each ends before the next one's audio starts.

`live: true` (on `start`) marks a latency-sensitive caller. The first sentence of a live
utterance is also split early at its first clause boundary (after at least 4 words), so first audio
does not wait for a whole sentence. In the scheduler a live caller goes first among the
waiters at the next sentence boundary, even when it arrives in the middle of a round (it still
gets one sentence per round). It never preempts a sentence already being synthesized, so a live
caller waits at most one in-flight sentence. Callers are served in
rounds, so no caller waits behind more than one sentence of any other caller. The
my_agent framework maps its surfaces `call` and `filler` to `live:true` and `voice-note`
and `narration` to `live:false`, and it sends `warm` when its client starts, often with no
request following for seconds.

### Events (engine to client)

| Event | Fields |
|-------|--------|
| `hello` | `proto` (1), `plugin` (the plugin version, from `.claude-plugin/plugin.json`), `audio`, `limits`, `loaded` (model in memory), `device` (`"cpu"`, `"cuda"` or null before load) |
| `status` | `proto`, `plugin`, `pid`, `loaded`, `device`, `requested` (`"cpu"`/`"cuda"`), `fallback` (null, or `{"reason", "at"}` when the GPU was not used; reasons: `keep_off`, `load_failed: <error>`, `synthesis_failed: <error>`), `vram_mib`, `callers`, `requests`, `warm_until` (epoch s or null), `idle_unload_s`, `idle_s`, `uptime_s`, `leases` |
| `audio` | `id`, `seq` (0, 1, 2... per request), `bytes`; then exactly `bytes` raw bytes follow the line |
| `end` | `id`, `reason` (`complete` \| `cancelled` \| `error`), `samples` (audio samples sent for this request; 24000 per second). Exactly one per request |
| `flushed` | `id`, `n` |
| `warm` | `loaded` (true), `device`, optional `id` |
| `released` | `vram_mib` (0) |
| `error` | `code`, `message`, optional `id` (request failure, followed by `end`) or `ref` (ends nothing); see the rule below |

`status` semantics: `requests` is the number of requests in progress right now (a gauge,
not a total); `callers` is the number of open connections (the one asking included);
`leases` is the number of active warm holds.

#### Errors

Codes: `bad_request`, `unsupported_protocol`, `unknown_voice`, `bad_speed`,
`text_too_long`, `busy`, `no_model`, `synthesis_failed`, `released`, `restarting`,
`unauthorized`.

Rule for client authors: an `error` carrying `"id"` is a request failure and is **always**
followed by `{"ev":"end","id":<same id>,"reason":"error",...}`; read to that `end`. Request
failures are a rejected `start`, `unknown_voice`, `bad_speed`, `text_too_long`, `no_model`,
`synthesis_failed`, `busy` on a request, and `restarting` when the engine leaves. An `error`
that ends nothing carries the id it refers to in `"ref"` instead, and nothing follows it: a
`start` that reuses an active id (the first request carries on and ends by itself), a
`text`/`flush`/`end` for an id with no active request or after its input ended, a non-string
`text`, a non-integer flush `n`, a `cancel` whose `id` is not a string (`ref` is that value), and every `warm` error (`ref` is the `warm` id, when one was sent;
the success reply `{"ev":"warm","id":...}` keeps `id`). An `error` with neither `id` nor `ref`
concerns the connection (bad line, unknown op, hello first, `busy` connection refusal,
`unauthorized`).

Codes in practice: `unknown_voice` (not in the voice file, checked once the model is
loaded), `bad_speed` (outside 0.5-2.0 or not a number), `no_model` (model files missing:
run `/voiceover:setup`), `synthesis_failed` (render or load failed), `busy` (16 TCP connections
already open: the new one gets one id-less `busy` line and is closed; Unix-socket connections are
never counted or refused, so unauthenticated TCP peers cannot lock local callers out; or 64
requests already active on this connection: the limit is per connection),
`released` (a defined code the engine does not currently send; the `release` op's reply is the separate `released` event), `restarting` (the engine is
leaving; reconnect and retry, `ensure` starts a new one).

#### Backpressure and drops

A caller that stops reading is not allowed to hold the engine: rendering for that
connection pauses while 2 MiB are unsent, and a connection stuck over the limit for 30 s is
dropped. A caller that disconnects has its queued and in-flight work discarded at once. An
engine bug in one request ends that request with `synthesis_failed`, never the process.

### Lifecycle

- **Start.** `engine_launcher.py ensure` (the stdlib client's `ensure()`) connects, and if
  nothing answers spawns `engine_launcher.py serve` detached under `start.lock` (single
  flight) and polls until it answers (default 10 s). `serve` first checks the pointer: when it
  is unusable (root or python gone) or this plugin copy supersedes it (the version rule below),
  it runs `prepare` itself; then it replaces itself with the pointer's
  `<plugin_root>/tts/kokoro_engine.py` run by the pointer's `python`, so code and environment
  always come from one plugin version. Still no usable pointer: one stderr line, exit 1. The real
  engine imports numpy, onnxruntime and kokoro-onnx before it binds; the model loads on the first
  request or `warm`, and a CPU load renders one short warm-up sentence first. Under systemd
  socket activation the engine adopts the passed sockets (`LISTEN_PID`/`LISTEN_FDS`) instead of
  binding, and keeps their files when it exits. A second self-started engine finds `engine.lock`
  held and exits 0; one that finds an `engine.sock` that still answers (systemd's, or an engine
  that does not hold the lock) exits 0 without touching it; a stale `engine.sock` left by a
  killed engine (nothing answers) is replaced on bind.
- **Idle.** The model unloads, by the process exiting, when nothing has happened for
  `engine_idle_s` seconds (330 s with `engine_port`, else 60 s) and no request, warm wait
  or warm hold is open. Any request activity resets the clock.
- **Warm.** `warm` with `hold_s` keeps the engine alive for that long (at most 600 s per hold,
  holds are independent) so the first real request is not cold. Callers that expect speech
  soon (the narrator re-warms every 30 s for 60 s while it waits) send it repeatedly.
- **Quiet-moment exits.** Every 10 s, when no request is open, no warm hold is active and
  no connection is open, the engine leaves for `update` if the pointer's or the
  `engine_plugin_root` setting's plugin directory differs from its own, or `tts_device`
  changed since it started; or for `card_return` if it fell back to the CPU for `keep_off`
  and that keep-off has expired (the GPU is back). Launchers then start a fresh engine.
- **Exits close connections.** Every exit (`idle`, `update`, `card_return`, `release`, `stop`)
  closes all open connections. A client that keeps a connection open, idle between requests,
  must treat EOF as "the engine left": reconnect (through `ensure`, or the systemd socket, which
  starts a new engine) and resend what it still needs.
- **Release.** `release` ends open requests with `error restarting` + `end error`, answers
  `released`, writes `keep_off.json` when `keep_off_s` > 0 and exits. With a keep-off in
  force a restarted engine loads on the CPU and reports `fallback.reason` `keep_off`
  (use it to hand the GPU to another program).
- **Stop.** `stop` ends every open request as `cancelled` (no `error`) and exits. Any other
  exit that finds requests open ends them `error restarting` + `end error`.
- **Kill switch.** `tts_controller.py stop` and the one-shot interrupt stop narration only; no
  kill pattern matches `kokoro_engine.py`, and an interface request in flight keeps
  playing. `tts_controller.py stop --all` also sends `stop` to the engine reachable through
  the current data dir.

### Launcher (`voiceover-engine`)

Run through the shim: `<state>/bin/voiceover-engine <command>`, or
`python3 tts/engine_launcher.py <command>`. Exit codes: 0 ok, 1 engine unreachable
(`prepare`: failed), 2 usage.

| Command | What |
|---------|------|
| `serve [--plugin-root R] [--stub] [--stub-delay-ms N]` | run the engine in this process (exec); `--stub` serves a sine-tone synth for tests |
| `prepare ROOT --project P` | `uv sync` project P, then publish the pointer (below) |
| `ensure [--plugin-root R]` | start the engine if it is not answering |
| `status` | print the `status` event as JSON |
| `release [--keep-off SECONDS]` | the `release` op |
| `stop` | the `stop` op |
| `token` / `rotate-token` | print the TCP token (creating it) / replace it |
| `install-service` / `uninstall-service` | write / remove the systemd user units (Linux) |
| `enable` / `disable` | hand the socket to systemd (stopping a self-started engine first) / back: `disable` stops the socket unit and the engine; the next narration `ensure` self-starts an engine again (Unix socket, plus TCP while `engine_port` is set) |

The shim finds the plugin through `--plugin-root`, else `current.json`'s `plugin_root`, puts
its `tts/` on `sys.path` and hands over to `engine_launcher.main`; so a program that knows only
`<state>/bin/voiceover-engine` keeps working across plugin updates.

#### The pointer, `<state>/engine/current.json`

```json
{"plugin_root": "/path/to/plugin", "plugin_version": "1.6.0", "uv": "/usr/bin/uv",
 "python": "/path/to/tts/.venv/bin/python", "project": "/path/to/plugin/tts"}
```

`python` is the engine's interpreter (it has kokoro-onnx), `project` the uv project
it came from (`tts` or `tts-gpu`). `prepare` builds the environment first and replaces the
pointer atomically only afterwards, so the pointer never names something that does not run.
The `UserPromptSubmit` hook installs the shim and starts `prepare` (rate-limited to once a
minute, never on Windows) for every session whose engine resolves to `kokoro`, at any level, and
whenever a pointer already exists (a machine that only shares the engine); `serve` repairs or
supersedes the pointer itself too (see Start). Version rule (`engine_paths.should_replace`): a candidate
replaces the pointer when there is no usable pointer (root missing, python not an executable
file), when the wanted project kind differs (`tts` vs `tts-gpu`), or when its version is
**strictly higher** (numeric, dot by dot); an equal or lower version keeps the pointer, so a
dev checkout never flips an installed engine. With `engine_plugin_root` set, only that
directory may become the pointer, and it replaces when the current root differs. A shim from
an older plugin never overwrites that of a newer pointer.

#### systemd units (Linux, `setup_tts.py --share-engine [PORT]` / `--unshare-engine`)

`install-service` writes `~/.config/systemd/user/voiceover-engine.socket` and
`voiceover-engine.service` (under `$XDG_CONFIG_HOME` when set), reloads systemd and enables
the socket. The socket unit has `ListenStream=<engine.sock>` (plus `ListenStream=127.0.0.1:<port>` when
`engine_port` is set), `SocketMode=0600`, `DirectoryMode=0700`, `NoDelay=true`, `Accept=no`,
`WantedBy=sockets.target`. The service unit has
`ExecStart=<system python3> <shim> serve` and `Environment=VOICEOVER_DATA_DIR=...` when the
data dir is overridden, and no `Restart=`: the engine exits when idle and the socket starts it
again. The interpreter is a stable system `python3`, never the plugin's own or a `.venv`.
`%` is doubled in every value; `$` is doubled only in `ExecStart` (systemd expands `$`
only in Exec lines). `setup_tts.py --share-engine` also creates the token, without printing it
(so it never lands in a Claude Code transcript), says to run `voiceover-engine token` in the
user's own terminal, and warns when `loginctl` shows lingering off (the engine then stops at
logout). `--unshare-engine` removes the units, clears `engine_port` (so a self-started engine
binds no TCP port afterwards) and stops the engine.

### Reference numbers

Measured through a proxy with the stub synthesizer (a boxed my_agent probe):
spawn to `hello` 247 ms median; `start` to first audio frame 5.4 ms (server overhead only);
`SIGTERM` to a new client's first frame 301 ms (stub), 687 ms with a 450 ms-per-sentence
synthesizer; `cancel` to `end`/`cancelled` median 2.3 ms; a live caller waits for one in-flight
sentence.

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
  (one-shot only), then tools-level announcement via pre_tool_announcement.
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
- `subagent_stop.py` — tools + speak_subagent_completions -> subagent_completion_message.
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

- `manage_settings.py {info|get KEY|set KEY VALUE [--project]|levels}` — wraps settings.py.
  `tts_engine` accepts `phonon`; `voice` accepts null, a Kokoro id or a Phonon name (exact case),
  whatever the engine (a voice for the other engine falls back at resolution time); `info`
  prints a `Phonon model:` line beside `Kokoro models:`.
- `manage_voices.py {list|set NAME [--project]|test [NAME]|recommend}` — friendly-name map →
  engine+voice: 37 Phonon voices (catalogue name lower-cased and hyphenated, e.g. `bryony`,
  `captain-silas-blackwater`; labels `"<Name> (Phonon)"` / `"<Name> (Phonon character)"`), the 13
  Kokoro voices (alloy … george) and `default-female` / `default-male`. `list` groups them under
  "Phonon voices", "Phonon character voices", "Kokoro neural voices", "macOS system voices".
  Availability: `ready` when uv and the engine's model are present (`kokoro_ready()`,
  `phonon_ready()`), else `needs uv …` / `needs model download …`. `set` for a Phonon voice when
  Phonon is ready (outside dry run) first runs `phonon_command("--fetch-voice", voice)` (120 s
  timeout; a failure or timeout saves nothing and exits 1); before setup it saves with the
  "will work once setup completes" note and never starts a download. `test` speaks a sample through
  `speech.one_shot_command(engine, voice, text, stream=False)`. `recommend` names `zoey` (a
  placeholder until the user picks one) when Phonon is ready, else the previous advice plus one
  line mentioning Phonon.
- `setup_tts.py [--download-phonon] [--download] [--test]` — first-run: check uv/ffplay
  availability, print status, verify audio. The status shows a Phonon block (model present via
  `settings.phonon_model_present()`; audio output: bundled PortAudio on macOS/Windows, on Linux
  the PortAudio library (`ctypes.util.find_library`) or the first of `pcm_sink.PLAYER_NAMES` on
  PATH, else what to install; never imports sounddevice) before the Kokoro block, and says
  "Phonon is installed but not selected. Pick a voice with /voiceover:voice." when the model is
  present and the resolved engine is not phonon. `--download-phonon` runs
  `speech.phonon_command("--install")` with its output on the terminal and a 600 s timeout (the
  first run builds tts-phonon/), then names `/voiceover:voice` (zoey as the placeholder
  suggestion); a failure exits 1. `--download` keeps meaning Kokoro (~300MB to ~/.kokoro-tts;
  an existing cache is reused). Installing never selects an engine. `--test` speaks through
  `speech.one_shot_command(..., stream=False)`.
- `migrate_legacy.py [--dry-run]` — detect legacy smarter-claude install (~/.claude/VERSION or
  'uv run ~/.claude/hooks/' in ~/.claude/settings.json); back up settings.json; strip the five
  legacy hook groups; list (and with --yes delete) legacy files; never touch statusLine,
  permissions, or anything it does not recognize. Prints a report.

## commands/ (plugin slash commands, markdown)

- `voice.md` (/voiceover:voice) — set voice by friendly name via manage_voices.py
- `level.md` (/voiceover:level) — silent|quiet|concise|tools|narrator or 0-4 via manage_settings.py
- `setup.md` (/voiceover:setup) — run setup_tts.py, guide model download + voice pick
- `migrate.md` (/voiceover:migrate) — run migrate_legacy.py flow with confirmation
All commands invoke scripts with `"${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py"` paths.

## .claude-plugin/

- `plugin.json` — name "voiceover", displayName "Claude Voiceover", version 1.6.0,
  description packed with search terms (voice, text-to-speech, TTS, narration, notification
  sounds, Kokoro, offline, local), author Hanan (okets), homepage/repository
  https://github.com/okets/claude-voiceover, license MIT.
  The manifest must not name `hooks/hooks.json` or `commands/`: Claude Code
  auto-discovers both, and naming either one makes it load the file twice and
  refuse the plugin ("Duplicate hooks file detected"). It names `./hooks/mod.json`,
  the mod's own file, which is an ADDITIONAL hook file.
- `marketplace.json` — marketplace name "claude-voiceover", owner okets, one plugin entry
  with source "./" so `/plugin marketplace add okets/claude-voiceover` works directly.
