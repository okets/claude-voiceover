# Low-Latency Narration — Design

Date: 2026-10-02
Status: revised after adversarial review (round 1); pending user approval
Target version: 1.4.0
Builds on: `2026-09-10-narration-queue-design.md` (the spool, the drain loop,
the cutover hook). Everything that document settles stands unless this one
changes it. The adversarial review's findings and how each is resolved are
listed at the end (§12).

## Problem

Measured on this machine on 2026-10-02, at the `narrator` level:

- **Mid-turn narration trails the screen** by a median 7.3s and a p90 of 23s.
  Source: a replay of three days of transcripts (444 mid-turn lines, 10
  sessions, 5 projects) through a model of today's pipeline, using the rates
  measured below. The replay script is `sim_mid.py` in the session scratchpad;
  its figures are model output, not live measurement.
- **Where the time goes, per line:**
  1. *Capture waits for a hook.* Prose is read from the transcript only when
     the NEXT tool call's block has finished streaming (PreToolUse), or at
     Stop plus a 0.5s flush poll. Transcript rows show 0.4–3.7s between a text
     block completing and the following tool block completing.
  2. *Cold engine per drain.* `uv run` spawn and lock claim 0.5s, model load
     0.84s (import 0.47 + load 0.37), first-sentence synthesis ~0.5s (the
     first call after a load costs another ~0.44s). Measured live with a queue
     monitor: enqueue → first sound ≈ 1.9s.
  3. *Read-everything FIFO across all sessions.* Speech runs at 19 characters
     per second, the screen is instant, and one machine-wide queue reads every
     item in order. Another session's lines were observed interleaving with
     this one's. Replay: one line in ten waits over 10s for its first word,
     the worst about two minutes.
- **Not causes:** hook process overhead (0.06s per hook, measured) and
  synthesis speed (warm, about 5.7x faster than playback).
- **A latent bug found on the way:** `settings.detect_project_root` accepts
  `~/.claude` as a project marker, so every repo without its own `.claude`
  — this one included — resolves to the home directory (§1).

## Goals

- Mid-turn heard lag (time from a line appearing on screen to it being
  heard) at a median ≤ 2.5s and a p90 ≤ 8s. The replay predicts 2.3s / 7.6s
  for this design, provided the next sentence is synthesized while the
  current one plays (§4); without that cross-item prefetch the reviewer's
  re-run gave 2.8s / 8.8s.
- One queue and one voice per project. Different projects may speak over
  each other.
- New text preempts only at a sentence boundary — a sentence is never cut
  mid-word, except by the user's own new prompt.
- The Kokoro engine (683MB peak RSS, measured) is resident only while a turn
  runs in its project, plus until its last queued sentence has played.
- Without the mod (older Claude Code, mod failed to load), the classic hooks
  still get per-project queues, the jump rule and the turn-scoped engine;
  only capture latency stays as today.
- Non-`narrator` levels keep today's behaviour, except that their interrupt
  can no longer kill a narrator engine (§7).

## Decisions

Settled with the user:

| Question | Decision |
|---|---|
| Queue scope | **Per project.** Different projects overlap freely. |
| Voice | **Per project**, through the existing project setting (`/voiceover:voice nicole --project` writes `.claude/voiceover.json`). Rollout sets `bf_emma` for `/Users/hanan/Projects/OrcaMCP` and `af_nicole` for `/Users/hanan/Projects/claude-voiceover`. |
| New text arrives while speaking | **Finish the current sentence, then start the newest message.** The unread remainder, and every message queued in between, is dropped. |
| The user's own new prompt | **Stop at once, even mid-sentence**, drop that session's pending items. The engine stays loaded so the reply starts warm. |
| Sessions sharing a project | Share the queue and the voice; the newest message wins between them. |
| Engine lifetime | **Per project, turn-scoped.** Loaded as a turn starts, released when it ends and while a question dialog waits on the user; the process exits once its queue is empty and no session holds it. |
| Model | **Keep `kokoro-v1.0.onnx` (fp32).** Measured: int8 is 407MB vs 683MB but synthesizes a 78-character sentence in 1.72s vs 0.76s. |
| Capture | **A mod inside this plugin**, tapping `turn.step` text chunks. Python keeps all policy. Early-access API and TypeScript accepted. |

Defaults chosen in the review round — the user may overrule any of them:

| Question | Default |
|---|---|
| What "new text" means | A new **message**: one model response (one `turn.step`) in the mod, one hook batch in the fallback. Its sentences play in order; only a different message preempts. |
| Which prompts cut speech off | Only the user's own: prompt origins `composer` (typed at the terminal), `bridge` (Remote Control) and `sdk` (an SDK host such as the VS Code extension). A background task's notification, a schedule, a peer session or a plugin never stops speech; such a prompt only holds the engine for its turn. The fallback skips prompts that begin with `<task-notification>`. |
| Dialog alerts and permission requests | **Sticky**: never dropped by the jump rule, and played whole once started. Only the user's own prompt stops one. |
| Headless sessions (`claude -p`, no surface) | **Not narrated.** The mod marks them, so the classic hooks stand down too. |
| Concurrent engines | **At most 2 machine-wide** (`MAX_ENGINES = 2`). A third project's engine steps aside until one exits; its items wait, and age out at the 300s cap. |
| Cost of the jump rule | The replay drops 11% of narration audio, against 7–8% today (today's drops are the cutover's). |

Measured to justify the turn-scoped engine: across 169 turns, the first
assistant text arrived at least 1.84s after the prompt (p10 2.2s, median
4.6s; 23 turns under 2.2s). A cold engine needs ~1.8s including a warm-up
synthesis, so pre-warming at the start of the turn hides it in nearly every
turn. Turn length: median 10s, p90 254s.

## Architecture

```
Claude Code process                         data_dir()/projects/<key>/   (one per project)
───────────────────                         ─────────────────────────
mod: hooks/narrator.ts (via hooks/mod.json) root.txt      the project root, for humans
  turn.step text chunks                     queue/        *.json items {..., utterance, sticky, shown}
    → segmenter (hooks/segment.ts)          leases/       <session>.json {"expiry"}
    → bridge channel (hooks/bridge.ts)      stop/         <session>.json {"ts"}   cutover requests
  prompt.submit / turn.start / tool.call    tts.lock      the project engine's lock {"expiry","pid"}
  / turn.complete / session.*               spawned.json  {"ts"} - an engine is starting
        │                                   play/         the engine's wav files
        ▼
hooks/run.sh mod_bridge  ──►  hooks/mod_bridge.py (stdlib)
                                │  voiceover/narrator.py actions
classic hooks (fallback) ───────┤  speech / spool / lease
                                ▼
                        engine --drain <project dir>     one process per project, at most 2
                          claim lock → load model + warm up → sentence loop
                          → exit when queue empty AND no live lease
```

### 1. Project identity — `voiceover/projects.py` (new, stdlib)

```python
def project_root(cwd=None) -> Path     # settings.detect_project_root(cwd), resolved
def project_key(cwd=None) -> str       # sha1(str(project_root(cwd)))[:12]
def project_dir(cwd=None) -> Path      # data_dir()/projects/<key>/, created on demand;
                                       # writes root.txt on creation
```

Engines receive the project directory as an argument and never compute a key:
the hook layer is the single authority for what a project is.

**Fix in `settings.detect_project_root`:** a `.claude` directory found at
`Path.home()` is not a project marker — it is Claude Code's own user
configuration. Today the upward search accepts it: measured on 2026-10-02,
`/Users/hanan/Projects/claude-voiceover` resolves to `/Users/hanan`, as do
ComfyUI, flashforge-obico, string-width, thinking-homes-website,
`~/Documents/3D-Prints` and any session started in `~`. `/voiceover:voice
<name> --project` run in any of them writes `~/.claude/voiceover.json`,
changing all of them. With the fix the `.claude` search skips the home
directory and falls through to the `.git` search. Side effect: such projects'
default (hash-picked) voice changes once.

**A session keeps its project for the whole turn.** The working directory
moves inside sessions (one session observed moving through five project
roots, another through nine directories); keying each operation on the
payload's `cwd` would hold a lease in one project and speak in another.
- The mod passes `$.session.root()` — the session's project root, which a
  shell `cd` does not move — as the bridge's `cwd`.
- The classic hooks pin the session's `cwd` at its user prompt
  (`data_dir()/pins/<session>`, written by `user_prompt_submit`) and every
  narrator path of that session uses the pinned value.

### 2. Spool — per project (`voiceover/spool.py`, `tts/engine_common.py`)

- Every spool function takes `cwd` and works in `project_dir(cwd)/queue/`.
  The filename rule, atomic write, atomic claim and 300s age cap are unchanged.
- Items gain three fields:
  - `"utterance": str` — items that share it are one message. The mod uses
    `<turnId>:<step index>`; classic hooks use a fresh `uuid4().hex` per
    enqueue call; a dialog alert reuses the lead-in's utterance.
  - `"sticky": bool` — dialog alerts and permission requests.
  - `"shown": float | None` — when the text appeared on screen (the mod's
    stamp), for latency logging.
- The legacy machine-wide `data_dir()/queue/` is no longer read. The first
  `ensure_drainer()` of 1.4.0 deletes whatever is left in it.

### 3. Leases — `voiceover/lease.py` (new, stdlib) + read half in `engine_common`

```python
LEASE_TTL = 600.0            # the mod: turn.complete releases reliably
FALLBACK_LEASE_TTL = 120.0   # classic hooks: Stop does not fire on an interrupt
def hold(cwd, session, ttl=LEASE_TTL) -> bool   # leases/<session>.json {"expiry"}, temp + os.replace
def release(cwd, session) -> None
def any_live(cwd=None) -> bool
```

Engine side: `lease_is_live(project_dir) -> bool`, true if any lease file has
an expiry in the future. **The engine never deletes a lease file** — reading
an expired lease and deleting it could race a `hold()` that just replaced it
with a fresh one. Instead `hold()` deletes other sessions' leases that
expired more than an hour ago.

The TTL is a crash backstop, not a timer. Holders renew on every model step
(mod) or every tool hook (fallback). A single tool call longer than the TTL
lets the lease lapse and the engine exit; the next renewal re-warms it.

| Event | Mod | Fallback hooks |
|---|---|---|
| User's own prompt submitted | `prompt` op with `cutover: true` (§6) | `user_prompt_submit`: cutover, pin cwd |
| Turn starts (`turn.start`) | `hold` op: hold + ensure (the pre-warm) | (`user_prompt_submit` holds) |
| Each model step starts | `hold` op | `pre_tool_use` / `post_tool_use`: hold |
| Question dialog opens (AskUserQuestion, ExitPlanMode) | `dialog` op: sticky alert, release | `pre_tool_use`: same |
| Dialog answered | `hold` op | `post_tool_use` for those tools: hold, ensure |
| Turn ends (answer, aborted, refusal, error) | `turn-end` op (main loop only — a subagent's `turn.complete` is ignored): release | `stop`: release |
| Session ends | `end-session` op: release | new classic `SessionEnd` hook: release, unpin |

A prompt that never becomes a turn (dropped by another plugin, a local slash
command) holds nothing, because the hold happens at `turn.start`.

### 4. The engine drain loop — `tts/engine_common.py`

`drain(project_dir, open_speaker, engine_names, lock_seconds=60.0,
poll_seconds=0.05) -> int` replaces today's `drain()`:

```
claim <project>/tts.lock atomically (exit 0 if held by a live engine)
if MAX_ENGINES other projects' engines are live: release, exit 0
speaker = open_speaker()                    # kokoro: load the model, warm up once
loop:
    item = next_item()                      # the jump rule, below
    if item is None:
        if lease_is_live(project_dir): sleep poll_seconds; continue
        release lock; re-check the queue AND the leases once (the drain-end
        race); re-claim and continue if either has something; else exit 0
    for sentence in split_sentences(item.text):
        play the sentence; while it plays, poll every poll_seconds:
            a stop request for item.session newer than item.created
                → kill playback, abandon item
            on the item's last sentence: peek the next item that will play
                and have the speaker synthesize its first sentence now
        if item is not sticky and a pending item has a different utterance
            → abandon the rest of item
    finish(item)
```

Every poll — idle, or while a sentence plays — refreshes the lock once half
of its 60s expiry has passed, so a live engine never looks free.

**The jump rule (`next_item`)**, over this engine's own pending items in
filename order: let U be the utterance of the newest; drop (finish unspoken)
every item whose utterance is not U and which is not sticky; claim and return
the oldest remaining item. Items of another engine are neither judged nor
dropped, and never block (today's engine-aware skip is kept).

**Sentence splitting** (`split_sentences`): split after `.`, `!` or `?`
followed by whitespace, and at newlines; drop empty pieces. Required because
kokoro-onnx batches by 510 phonemes, not by sentence (verified in the
installed `kokoro_onnx/__init__.py`).

**Prefetch.** The speaker synthesizes ahead in a worker thread (onnxruntime
releases the GIL): within an item, sentence k+1 while k plays; across items,
the first sentence of the next item to play, found by peeking without
claiming (a claimed `.taken` item would be invisible to `spool.clear()` and
survive a cutover). A prefetch that does not match what is actually played
next is discarded. Mod items are single sentences, so the cross-item prefetch
is what keeps the gap between sentences near zero.

**Warm-up.** `open_speaker()` for Kokoro synthesizes one short throwaway
sentence after loading, so the first real sentence does not pay the ~0.44s
first-call cost.

**Playback.**
- Kokoro on macOS/Linux: each sentence is written to `<project>/play/<n>.wav`
  and played by `afplay` (Linux: `aplay`, else `ffplay`) through `Popen`,
  polled so a stop can kill it.
- Kokoro on Windows: in-process `winsound.PlaySound(path, SND_FILENAME |
  SND_ASYNC)`, polled against the clip's computed duration, stopped with
  `PlaySound(None, 0)`. (Today's PowerShell player sleeps for at least 30s per
  clip and cannot be killed with the engine.)
- macOS `say`: `say --voice=<Name>` through `Popen`, text on stdin. No
  prefetch: `say` starts speaking within ~0.1s.

**Dead engines.** A lock whose recorded pid is no longer running is stale
even before its expiry: the engine's claim takes it over, and
`ensure_drainer()` treats it as free. Otherwise a killed engine (an
out-of-memory kill, a 1.3 session's interrupt during rollout) would silence
its project for up to 60s.

### 5. Writer side — `voiceover/speech.py`

```python
def enqueue_speech(text, min_level="concise", cwd=None, full=False,
                   session=None, utterance=None, sticky=False, shown=None) -> bool
def ensure_drainer(cwd=None) -> bool
    # spawn `--drain <project_dir>` when (the queue is non-empty OR a lease is
    # live) AND the project's lock is not held by a live engine AND no engine
    # was spawned for it in the last 3 seconds (spawned.json) - the pre-warm,
    # without a burst of spawns while uv starts up
def prewarm(cwd, session, ttl=LEASE_TTL) -> bool   # lease.hold + ensure_drainer
def request_stop(cwd, session) -> None             # <project>/stop/<session>.json {"ts"}
```

### 6. Cutover on the user's own prompt

At the `narrator` level, for a prompt of the user's own (Decisions):

1. `spool.clear(session=..., cwd=...)` — drop that session's pending items.
2. `request_stop(cwd, session)` — the engine kills the sentence playing now
   if it belongs to that session and was queued before the request. A stale
   request can never cut a later reply, because the reply's items are newer
   than it; each session has its own stop file, so two prompts at once
   cannot overwrite each other's request.
3. `prewarm(cwd, session)`.

Clear before stop, as in 1.3. No process is killed at the `narrator` level.
Non-`narrator` levels keep today's sequence (clear, then the one-shot
`stop_speech()`), scoped by §7.

### 7. Scoping the non-narrator interrupt

`process_utils.stop_all_tts()` (called by `stop_speech()`) must never kill a
narrator engine or its playback, because another project may be narrating:

- Engine patterns become one-shot specific: `kokoro_voice.py --voice` and
  `macos_say.py --voice`. Drainers run as `--drain <dir>`. Every one-shot
  caller uses the `--voice` form (`speech._dispatch`, `scripts/manage_voices.py`
  and `scripts/setup_tts.py` today pass the bare `kokoro_voice.py <voice>
  <text>` form, which the new pattern would miss).
- One-shot Kokoro playback files are named `voiceover-oneshot-*.wav`; the
  playback pattern becomes `afplay .*voiceover-oneshot-.*\.wav`. Drainer
  files live under `projects/<key>/play/`.
- The drainer invokes `say --voice=<Name>`, which the one-shot patterns
  `say -v Samantha` / `say -v Daniel` do not match.
- `tts/tts_controller.py stop` — the user's kill switch — kills every
  engine: the global lock owner's process group, every project lock owner's
  process group, and the patterns (now also `say --voice=`, `aplay .*\.wav`,
  `ffplay .*\.wav`); then it clears every project's lock, queue, leases, stop
  files and wavs.

### 8. The mod — `hooks/narrator.ts`, `hooks/segment.ts`, `hooks/bridge.ts`

Declared in its own hooks file, `hooks/mod.json` = `{"modules":
["./narrator.ts"]}`, named by `.claude-plugin/plugin.json` as `"hooks":
"./hooks/mod.json"`. The standard `hooks/hooks.json` stays as it is and is
still auto-discovered (declaring an ADDITIONAL hooks file is allowed; naming
the standard one is what broke 1.3.0). A build that cannot read `modules`
can then fail only that file, not the classic hooks. Validated with `claude
plugin validate` on 2.1.287; older builds are checked in the rollout.

| Hook | Behaviour |
|---|---|
| `session.start` | Open the session state; a session with `e.surface === null` (headless) is never narrated. `start` op. |
| `session.end` | `end-session` op, awaited. |
| `prompt.submit` | `prompt` op with `cutover` = the origin is `composer`, `bridge` or `sdk`. Not awaited: the prompt is never delayed. The reply carries this turn's `narrate` gate. |
| `turn.start` | `hold` op (the pre-warm). |
| `turn.step` (main loop only) | Yields every chunk of `next(e)` unchanged and immediately, then feeds text chunks to the segmenter. Every block of one response shares the utterance `<turnId>:<index>`. Complete segments become `say` ops. When the stream ends normally each block's remainder is flushed; an aborted stream is not flushed. A `hold` op is sent at step start. |
| `tool.call` AskUserQuestion, ExitPlanMode | `dialog` op under the last utterance; `await next(e)`; `hold` op. |
| `turn.complete` (main loop only) | `turn-end` op with `spoken` = the last step queued text. |

**The bridge channel.** `$.process.run(["bash", $.plugin.root +
"/hooks/run.sh", "mod_bridge"], { stdin, timeoutMs: 5000 })`, payload
`{"session", "cwd", "ops": [...]}`, `cwd` = `$.session.root()`. One
serialized channel per session: ops are delivered in order; ops queued while
a call is in flight go together in the next call. A call costs ~0.06s and
never blocks the stream: no hook awaits a bridge call, except `session.end`.
Segments emitted before the turn's gate is known wait on it in order.

**Recovering after a module restart.** If a hook finds no session state (the
module reloaded mid-turn), it opens one and asks the bridge for the gate with
a `status` op, so narration resumes instead of going silent while the
classic hooks stand down.

**`hooks/mod_bridge.py`** (stdlib) executes the ops through
`voiceover/narrator.py`, the same actions the classic hooks call. No
narration policy lives in TypeScript. **Every batch marks the session**
(`data_dir()/mod_sessions/<id>`), so a session id that changes mid-process
(`/clear`, a resume — no `session.start` fires for it) is marked by its very
first op. It prints one JSON line, `{"narrate": bool}`.

**The segmenter** (pure, unit-tested): emits a segment after `.`, `!` or `?`
followed by whitespace, and at each newline. A fence (```` ``` ```` at the
start of a line) holds everything until it closes, then is emitted whole, so
`clean_for_speech()` turns it into the code cue; an inline ```` ``` ```` is
not a fence. The bridge cleans each segment with `clean_for_speech()`, strips
any `*` or backtick left unpaired by a split, and does not queue segments
that clean to nothing.

**Failure containment.** Every hook body catches its own errors and logs
with `$.ui.log(..., { to: "debug" })`. The stream tap yields each chunk
before touching the segmenter. A bridge call that fails or times out
settles its ops as "not narrating"; the next call proceeds.

### 9. Classic hooks as the fallback

- Every narrator path first resolves the session's pinned `cwd` (§1), then
  checks `mod_active(session)`. If the mod narrates this session the path
  does nothing except advance the prose cursor to the end of the transcript,
  so a later fallback never replays what the mod already spoke.
- Otherwise the paths behave as in 1.3, plus: the per-project spool, a fresh
  utterance per enqueue, the lease table of §3 with `FALLBACK_LEASE_TTL`, and
  the dialog item enqueued sticky (lead-in prose and alert combined, as in
  1.3).
- `notification.py` enqueues permission requests sticky.
- The `dialog_alert` echo-suppression marker becomes per session
  (`data_dir()/dialog_alert/<session>`), so a dialog in one project no longer
  silences permission requests in another.
- A new classic `SessionEnd` hook releases the session's lease and pin. It is
  a new hook event: live sessions need one restart to register it.

## Error handling

- Engine: a corrupt item is dropped; an unreadable stop or lease file is
  ignored; a playback failure skips that sentence; a synthesis failure skips
  the rest of the item.
- Engine crash: the next `ensure_drainer()` sees the dead pid and respawns at
  once. Leases expire within their TTL.
- Claude Code crash: leases lapse at their TTL; the engine then exits.
- Mod: hook failures never reach the stream (§8). Accepted residual risk: a
  module that loads and then fails on every call leaves the session marked
  and silent until the session ends.

## Testing

Python (stdlib harness, `VOICEOVER_DATA_DIR` + `VOICEOVER_DRY_RUN` +
`VOICEOVER_NO_SPAWN`):

1. Projects: a fake HOME containing `.claude/`: a git project under it
   resolves to itself; a subdirectory to its project; two roots → two dirs.
2. Spool per project; the new fields; the legacy queue removed once.
3. Leases: hold, renew, release, expiry; the engine never deletes a lease;
   `hold` prunes hour-old leases of other sessions.
4. Jump rule with a speaker whose sentences take real polls: same-utterance
   items in order; a newer message preempts at the next sentence boundary;
   items in between dropped; sticky items survive the jump AND play whole;
   newest wins across sessions; foreign items neither dropped nor blocking.
5. Stop requests: kill the current sentence of the named session; a request
   older than an item never stops it; another session's request is ignored.
6. Engine lifecycle: waits while a lease is live; exits once released; the
   drain-end race re-checks leases; the machine-wide cap; a dead engine's
   lock is taken over; pre-warm on a lease alone; the spawn backoff.
7. Kokoro speaker: per-sentence synthesis, in-item and cross-item prefetch, a
   stop that lands during synthesis, Windows playback through a fake
   `winsound`, the warm-up.
8. Stop scoping: the one-shot patterns, checked in-process against drainer
   and one-shot command lines (never by killing real processes); the kill
   switch clears every project.
9. Bridge: every op, `narrate`, batching, cleaning, the session marked by any
   op, `cutover: false` prompts.
10. Fallback: stand-down with the cursor advanced; the lease table; the pin
    surviving a `cd`; task-notification prompts do not cut over; the new
    SessionEnd hook; per-session dialog marker; sticky permission requests.

Mod (`claude plugin test`, `hooks/*.test.ts`):

1. Segmenter: sentence ends, newlines, line-start fences held until closed,
   inline backticks not fences, flush.
2. Stream tap: chunks unchanged and in order; subagent steps ignored; nothing
   sent when `narrate` is false; a throwing bridge does not break the stream;
   no flush after an abort.
3. Channel: in order, batched while a call is in flight, failures silent.
4. Lifecycle: `cutover` true only for the user's own origins; `hold` at
   `turn.start`; `turn-end` on `answer` and `aborted`, ignored for a
   subagent; `dialog` then `hold` around AskUserQuestion; a new session id
   after `/clear` is used for every later op; headless sessions send no
   `say`; recovery with `status` after a lost state.

Live acceptance, on the user's machine, in the VS Code extension: the engine
logs, per item, its first-sound time against the `shown` stamp. Over a
working session of at least 50 mid-turn lines, heard lag median ≤ 2.5s and
p90 ≤ 8s; the engine process is gone within a few seconds of a turn's last
sentence.

## Rollout

- Version 1.4.0. **Every live session must restart or `/reload-plugins`.** A
  1.3 session left running kills 1.4 engines with its broad interrupt
  patterns and keeps narrating through the old global queue. (The dead-pid
  rule of §4 limits the damage to the sentence being cut.)
- The plan's first task is a throwaway probe mod, run in the user's VS Code
  session, that confirms: `turn.step` text chunks arrive as the text
  streams; a `$.process.run` that is not awaited completes after its hook
  returns; `$.session.id()` matches the transcript; subagent steps carry
  `agentId`; and records the VS Code session's `surface` and its prompts'
  `origin.kind`. If VS Code reports `surface: null` or an origin outside
  `composer`/`bridge`/`sdk`, §8's rules are adjusted before building.
- Builds without function hooks: check on the oldest Claude Code build
  available that the separate `hooks/mod.json` does not stop
  `hooks/hooks.json` from loading. If it does, document the minimum version.
- Set the two project voices (Decisions).

## Files touched

New: `voiceover/projects.py`, `voiceover/lease.py`, `voiceover/narrator.py`,
`hooks/mod_bridge.py`, `hooks/session_end.py`, `hooks/mod.json`,
`hooks/narrator.ts`, `hooks/segment.ts`, `hooks/bridge.ts`,
`hooks/*.test.ts`, `tests/test_projects.py`, `tests/test_jump_rule.py`,
`tests/test_engine_lifecycle.py`, `tests/test_stop_scope.py`,
`tests/test_mod_bridge.py`, this spec.

Modified: `voiceover/settings.py`, `voiceover/spool.py`, `voiceover/speech.py`,
`voiceover/process_utils.py`, `voiceover/prose.py`, `tts/engine_common.py`,
`tts/kokoro_voice.py`, `tts/macos_say.py`, `tts/tts_controller.py`,
`hooks/pre_tool_use.py`, `hooks/post_tool_use.py`, `hooks/stop.py`,
`hooks/user_prompt_submit.py`, `hooks/notification.py`, `hooks/hooks.json`,
`scripts/manage_voices.py`, `scripts/setup_tts.py`, `INTERFACES.md`,
`CHANGELOG.md`, `README.md`, `.claude-plugin/plugin.json` (1.4.0, `"hooks":
"./hooks/mod.json"`), `.gitignore`, and the existing tests that assume one
machine-wide queue.

## Out of scope

- One engine shared by all projects.
- A smaller or quantized model.
- Speeding speech up when it falls behind.
- Releasing the lease during permission prompts (they are rare and short;
  the TTL bounds them).
- Any other change at non-`narrator` levels.

## 12. Adversarial review, round 1 — findings and resolutions

| # | Sev. | Finding | Resolution |
|---|---|---|---|
| 1 | BLOCKER | `~/.claude` makes most repos (this one too) resolve to home | §1 fix; tests with a fake HOME |
| 2 | BLOCKER | After `/clear`/resume the new session id is never marked, so mod and hooks both narrate | Every bridge batch marks the session (§8) |
| 3 | BLOCKER | Windows PowerShell player holds ≥30s per sentence and survives kills | In-process `winsound` async playback (§4) |
| 4 | MAJOR | Single-sentence mod items get no prefetch: 2.8s / 8.8s, targets missed | Cross-item prefetch by peeking; warm-up synthesis (§4) |
| 5 | MAJOR | Cutover fires for task notifications, schedules, peers | Cutover only for `composer`/`bridge`/`sdk`; fallback skips `<task-notification>` (§6, Decisions) |
| 6 | MAJOR | Subagent `turn.complete` releases mid-turn and chimes | Main loop only (§3, §8) |
| 7 | MAJOR | Project key moves with `cd` mid-session | Mod uses `$.session.root()`; hooks pin cwd per session (§1) |
| 8 | MAJOR | Sticky alerts cut mid-alert; permission requests not sticky | Sticky items play whole; permission requests sticky (§4, §9) |
| 9 | MAJOR | A killed engine silences its project up to 60s; 1.3 sessions kill 1.4 engines | Dead-pid locks are stale (§4); rollout restarts every session |
| 10 | MAJOR | Parallel headless sessions each load an engine and speak at once | Headless sessions not narrated; `MAX_ENGINES = 2` (Decisions, §4) |
| 11 | MINOR | A stale stop cuts a later reply; one stop file is overwritten | Stop only items queued before the request; one file per session (§6) |
| 12 | MINOR | Drain-end race ignores leases; engine deletes a freshly renewed lease; Windows rename | Re-check leases; engine never deletes leases; `os.replace` (§3, §4) |
| 13 | MINOR | Esc/exit hold the lease 600s; a prompt with no turn holds one | Hold at `turn.start`; fallback TTL 120s; classic SessionEnd hook (§3, §9) |
| 14 | MINOR | The 2.2s claim was wrong (min 1.84s); awaiting in the tap stalls text; respawn loses the gate | Corrected figures; the tap never awaits; `status` recovery (§8) |
| 15 | MINOR | No spawn backoff: a spawn per bridge batch while uv starts | `spawned.json`, 3s (§5) |
| 16 | MINOR | `dialog_alert.json` is machine-wide | Per session (§9) |
| 17 | MINOR | Kill switch misses drainers' `say`/`aplay`; voice preview uses the bare form | Kill switch kills every project lock owner and the new patterns; one-shot callers use `--voice` (§7) |
| 18 | MINOR | An old build rejecting `modules` would fail all of `hooks.json` | Mod in its own `hooks/mod.json` (§8); checked in rollout |
| nits | — | Segmenter splits markup and treats inline ```` ``` ```` as a fence; Esc flushes a partial; per-block keys self-preempt; cursor not advanced in stand-down; drop rate unstated; `hold` gating | Line-start fences, stray-marker strip; no flush on abort; per-step utterance; cursor advanced (§9); drop rate in Decisions; `hold` gated on the narrator gate |
