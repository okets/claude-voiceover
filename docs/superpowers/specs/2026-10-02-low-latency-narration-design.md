# Low-Latency Narration — Design

Date: 2026-10-02
Status: approved by the user 2026-10-02, after two adversarial review rounds.
**Streaming capture (the mod, §8) is deferred**: Claude Code's rollout switch
for mods in installed plugins is off on this account; 1.4.0 ships everything
else on the classic hooks. The mod work is parked in
`docs/superpowers/plans/2026-10-02-narrator-mod-deferred.md`.
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

- On the classic hooks (1.4.0): the first word of a mid-turn line, measured
  from when its text reached the transcript, at a median ≤ 4.0s and a p90
  ≤ 9.0s. The replay predicts 3.6s / 8.4s for this design (today 4.8s /
  15.2s); what you hear trails the screen median 5.0s / p90 15.8s (today
  7.3s / 23s). Prefetch (§4) is not modelled and should only lower these.
- With the deferred mod (§8): heard lag median ≤ 2.5s and p90 ≤ 8s (replay
  2.3s / 7.6s, given the cross-item prefetch).
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
| Capture | **The classic hooks for 1.4.0.** A mod tapping `turn.step` text chunks was approved, but it only ships when Claude Code loads mods for installed plugins (off on this account, 2026-10-02); the user asked to be asked each new session whether to test for it. |

Defaults chosen in the review round — the user may overrule any of them:

| Question | Default |
|---|---|
| What "new text" means | A new **message**: one hook batch (with the mod: one model response). Its sentences play in order; only a different message preempts. |
| Which prompts cut speech off | The user's own. The hooks cannot see a prompt's origin, so they skip only prompts that begin with `<task-notification>` (a background task finishing); such a prompt only holds the engine for its turn. (The mod would use the origin: `composer`, `bridge`, `sdk`.) |
| Dialog alerts and permission requests | **Sticky**: never dropped by the jump rule, and played whole once started. Only the user's own prompt stops one. |
| Headless sessions (`claude -p`, the Agent SDK) | **Not narrated**: the hooks skip a session whose `CLAUDE_CODE_ENTRYPOINT` starts with `sdk-` (measured: `sdk-cli` for `claude -p`, `claude-vscode` for the VS Code extension). A headless run started from inside another session inherits that session's value and is not recognized; `MAX_ENGINES` bounds that case. |
| Concurrent engines | **At most 2 machine-wide** (`MAX_ENGINES = 2`). A third project's engine steps aside until one exits; its items wait, and age out at the 300s cap. |
| Cost of the jump rule | The replay drops 11% of narration audio, against 7–8% today (today's drops are the cutover's). |

Measured to justify the turn-scoped engine: across 169 turns, the first
assistant text arrived at least 1.84s after the prompt (p10 2.2s, median
4.6s; 23 turns under 2.2s). A cold engine needs ~1.8s including a warm-up
synthesis, so pre-warming at the start of the turn hides it in nearly every
turn. Turn length: median 10s, p90 254s.

## Architecture

```
classic hooks                               data_dir()/projects/<key>/   (one per project)
─────────────                               ─────────────────────────
user_prompt_submit  cutover, pin, pre-warm  root.txt      the project root, for humans
pre/post_tool_use   prose, hold, dialogs    queue/        *.json {..., utterance, sticky, shown, source}
stop                finale, release         leases/       <session>.json {"expiry"}
notification        sticky permission       stop/         <session>.json {"ts"}   cutover requests
session_end         release, unpin          tts.lock      the engine's lock {"expiry","pid"}
        │                                   spawned.json  {"ts"} - an engine is starting
        ▼                                   play/         the engine's wav files
voiceover/narrator.py actions → speech / spool / lease
        │ ensure_drainer(project)
        ▼
engine --drain <project dir>     one process per project, at most 2 machine-wide
  claim lock → load model + warm up → sentence loop
  → exit when queue empty AND no live lease
```

(The deferred mod would feed the same actions through `hooks/mod_bridge.py`;
§8.)

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
- The hooks pin the session's `cwd` at its user prompt
  (`data_dir()/pins/<session>`, written by `user_prompt_submit`). Every other
  hook of that session resolves the pin first — before even reading the
  narration level — and uses the pinned value; `SessionEnd` drops it.
- (The deferred mod would pass `$.session.root()`, which a shell `cd` does
  not move.)

### 2. Spool — per project (`voiceover/spool.py`, `tts/engine_common.py`)

- Every spool function takes `cwd` and works in `project_dir(cwd)/queue/`.
  The filename rule, atomic write, atomic claim and 300s age cap are unchanged.
- Items gain three fields:
  - `"utterance": str` — items that share it are one message. The hooks use
    a fresh `uuid4().hex` per enqueue call.
  - `"sticky": bool` — dialog alerts and permission requests.
  - `"shown": float | None` — when the text appeared: the transcript
    timestamp of the batch's first prose row (`prose.peek_new_prose_stamped`).
  - `"source": str | None` — the hook that queued it (`pre_tool_use`,
    `post_tool_use`, `stop`, `dialog`, `notification`). With `shown` it lets
    the engine log the first-word lag of mid-turn lines, the acceptance
    measure.
- `spool.clear_sticky(session, cwd)` retires a session's pending sticky
  items: `post_tool_use` calls it, because a finished tool means the alert or
  permission request was answered — an answered alert must not be read out
  afterwards while every later message waits behind it.
- The legacy machine-wide `data_dir()/queue/` is no longer read. The first
  `ensure_drainer()` of 1.4.0 deletes whatever is left in it.

### 3. Leases — `voiceover/lease.py` (new, stdlib) + read half in `engine_common`

```python
LEASE_TTL = 600.0            # a holder whose turn end always arrives (the deferred mod)
FALLBACK_LEASE_TTL = 120.0   # the hooks: Stop does not fire on an interrupt
def hold(cwd, session, ttl=LEASE_TTL) -> bool   # leases/<session>.json {"expiry"}, temp + os.replace
def release(cwd, session) -> None
def any_live(cwd=None) -> bool
```

Engine side: `lease_is_live(project_dir) -> bool`, true if any lease file has
an expiry in the future. **The engine never deletes a lease file** — reading
an expired lease and deleting it could race a `hold()` that just replaced it
with a fresh one. Instead `hold()` deletes other sessions' leases that
expired more than an hour ago.

The TTL is a crash backstop, not a timer: the hooks renew on every tool hook
with `FALLBACK_LEASE_TTL`. A single tool call longer than that lets the lease
lapse and the engine exit; the next hook re-warms it.

| Event | Hook | Lease |
|---|---|---|
| A prompt (the user's own, or a task notification) | `user_prompt_submit` | hold + start the engine (the pre-warm); pin cwd |
| Each tool call | `pre_tool_use` / `post_tool_use` | hold (renew) |
| Question dialog opens (AskUserQuestion, ExitPlanMode) | `pre_tool_use` | sticky alert queued, then release |
| Dialog answered | `post_tool_use` for those tools | hold + start |
| Turn ends | `stop` | release (at every level, so a level changed mid-turn never leaves an engine held) |
| Session ends | new `SessionEnd` hook | release, unpin |

Stop does not fire when the user interrupts; the lease then lapses within
`FALLBACK_LEASE_TTL`, or the next prompt takes it over.

### 4. The engine drain loop — `tts/engine_common.py`

`drain(project_dir, open_speaker, engine_names, lock_seconds=60.0,
poll_seconds=0.05) -> int` replaces today's `drain()`:

```
claim <project>/tts.lock atomically (exit 0 if held by a live engine)
if MAX_ENGINES other projects' engines are live: refresh spawned.json, exit 0
speaker = open_speaker()                    # kokoro: load the model, warm up once
if it failed: write spawned.json 27s ahead (hooks back off 30s), exit 0
delete spawned.json                         # up and loaded
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
next is discarded. (The deferred mod's items are single sentences; for them
the cross-item prefetch is what keeps the gap between sentences near zero.)

**Synthesis never blocks the loop.** Kokoro's `begin()` returns at once with
a playback that first waits for its sentence's rendering and then plays it;
the loop polls it the whole time, so a stop request or a lock refresh never
waits for synthesis, and a sentence stopped while it renders makes no sound.

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
                   session=None, utterance=None, sticky=False, shown=None,
                   source=None) -> bool
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
A prompt that carries no session id clears and stops nothing (an unscoped
stop would cut every session off), and only pre-warms.
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

### 8. Deferred: streaming capture (the mod)

Approved, designed, reviewed twice — and deferred: on 2026-10-02 Claude Code
2.1.287 refused to load function-hook modules for installed and
`--plugin-dir` plugins on this account ("hooks modules are turned off for
installed plugins in this process: the rollout switch served off"), and
`claude plugin test` refuses for the same reason. The design (a TypeScript
mod tapping `turn.step` text chunks, cutting them into sentences, and
handing them through `hooks/mod_bridge.py` to the same narrator actions the
hooks use) is §8 of this spec as committed in `7eee726`; the plan, the
round-2 changes it must absorb, and the switch test are in
`docs/superpowers/plans/2026-10-02-narrator-mod-deferred.md`. Everything in
§1–§7 and §9 is built so the mod can be added later without reworking them:
the queue, utterances, sticky items, `shown` stamps, leases, stop requests
and narrator actions are what its bridge would call.

### 9. The classic hooks

- Every narrator path resolves the session's pinned `cwd` (§1) before
  anything else, then skips headless runs (Decisions).
- The paths behave as in 1.3, plus: the per-project spool, a fresh
  utterance per enqueue, `shown` and `source` on every item, and the lease
  table of §3 with `FALLBACK_LEASE_TTL`. The dialog item keeps 1.3's shape
  — lead-in prose and alert combined in one item — and is enqueued sticky.
- `post_tool_use` retires the session's pending sticky items (§2).
- `notification.py` enqueues permission requests sticky.
- The `dialog_alert` echo-suppression marker becomes per session
  (`data_dir()/dialog_alert/<session>`), so a dialog in one project no longer
  silences permission requests in another.
- A new classic `SessionEnd` hook releases the session's lease and pin. It is
  a new hook event: live sessions need one restart to register it.

## Error handling

- Engine: a corrupt item is dropped; an unreadable stop or lease file is
  ignored; a playback failure skips that sentence; a synthesis failure skips
  the rest of the item; a failed model load backs the hooks off for 30s.
- Engine crash: the next `ensure_drainer()` sees the dead pid and respawns at
  once. Leases expire within their TTL.
- Claude Code crash: leases lapse at their TTL; the engine then exits.

## Testing

Stdlib harness: `VOICEOVER_DATA_DIR`, `VOICEOVER_DRY_RUN`,
`VOICEOVER_NO_SPAWN` and `CLAUDE_CODE_ENTRYPOINT=cli`; the suite passes under
the system Python 3.9 and at the end of every task.

1. Projects: a fake HOME containing `.claude/`: a git project under it
   resolves to itself; a subdirectory to its project; two roots → two dirs.
2. Spool per project; the new fields; `clear_sticky`; the legacy queue
   removed once.
3. Leases: hold, renew, release, expiry; the engine never deletes a lease;
   `hold` prunes hour-old leases of other sessions.
4. Jump rule with a speaker whose sentences take real polls: same-utterance
   items in order; a newer message preempts at the next sentence boundary;
   items in between dropped; sticky items survive the jump AND play whole;
   newest wins across sessions; foreign items neither dropped nor blocking.
5. Stop requests: kill the current sentence of the named session; a request
   older than an item never stops it; another session's request is ignored;
   an item queued before its session's stop is dropped unplayed.
6. Engine lifecycle: waits while a lease is live; exits once released; the
   drain-end race re-checks leases; the machine-wide cap; a dead engine's
   lock is taken over; pre-warm on a lease alone; the spawn backoff and the
   30s backoff after a failed load.
7. Kokoro speaker: per-sentence synthesis, in-item and cross-item prefetch, a
   stop during synthesis that leaves the sentence unplayed, Windows playback
   through a fake `winsound`, the warm-up.
8. Stop scoping: the one-shot patterns, checked in-process against drainer
   and one-shot command lines (never by killing real processes); the voice
   preview's command form; the kill switch clears every project.
9. Hooks: the lease table; an answered alert retired; `source`/`shown` on
   items; the pin surviving a `cd`; task-notification prompts and
   session-less prompts do not cut over; headless runs not narrated; a stop
   at any level releases; the new SessionEnd hook; per-session dialog
   marker; sticky permission requests.

Live acceptance, on the user's machine: the engine logs, per item, its
first-sound time against the item's `shown` stamp and its `source`. Over a
working session of at least 50 mid-turn lines (`pre_tool_use` /
`post_tool_use`), first-word lag median ≤ 4.0s and p90 ≤ 9.0s; the engine
process is gone within a few seconds of a turn's last sentence.

## Rollout

- Version 1.4.0. **Every live session must restart or `/reload-plugins`.** A
  1.3 session left running kills 1.4 engines with its broad interrupt
  patterns and keeps narrating through the old global queue. (The dead-pid
  rule of §4 limits the damage to the sentence being cut.) The new SessionEnd
  hook registers only after a restart.
- Set the two project voices (Decisions).
- Each new session in this project asks the user whether to test if mods now
  load (`2026-10-02-narrator-mod-deferred.md` has the test).

## Files touched

New: `voiceover/projects.py`, `voiceover/lease.py`, `voiceover/narrator.py`,
`hooks/session_end.py`, `tests/test_projects.py`, `tests/test_jump_rule.py`,
`tests/test_engine_lifecycle.py`, `tests/test_stop_scope.py`, this spec,
`docs/superpowers/plans/2026-10-02-narrator-mod-deferred.md`.

Modified: `voiceover/settings.py`, `voiceover/spool.py`, `voiceover/speech.py`,
`voiceover/process_utils.py`, `voiceover/prose.py`, `tts/engine_common.py`,
`tts/kokoro_voice.py`, `tts/macos_say.py`, `tts/tts_controller.py`,
`hooks/pre_tool_use.py`, `hooks/post_tool_use.py`, `hooks/stop.py`,
`hooks/user_prompt_submit.py`, `hooks/notification.py`, `hooks/hooks.json`,
`scripts/manage_voices.py`, `scripts/setup_tts.py`, `INTERFACES.md`,
`CHANGELOG.md`, `README.md`, `.claude-plugin/plugin.json` (1.4.0),
`.gitignore`, `tests/_harness.py`, and the existing tests that assume one
machine-wide queue.

## Out of scope

- The mod (§8), until Claude Code loads mods for installed plugins.
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

## 13. Adversarial review, round 2 — findings and resolutions

Round 2 attacked the plan's code; the reviewer applied Tasks 1–7 to a copy of
the repo and ran them under Python 3.14 and 3.9.6.

| # | Sev. | Finding | Resolution |
|---|---|---|---|
| 1 | BLOCKER | The engine refuses the mod: `$` passed to closures, stored, or passed across an import; `claude plugin validate .` validates only the marketplace and hid it | Mod deferred (§8); required change recorded in the deferred plan; the 1.4.0 plan validates `./.claude-plugin/plugin.json` |
| 2 | BLOCKER | Mod tests use wrong answer shapes; `claude plugin test` is switched off | Deferred with the mod; recorded |
| 3 | MAJOR | Headless runs pre-warm engines and take `MAX_ENGINES` slots | Hooks skip `CLAUDE_CODE_ENTRYPOINT=sdk-*` (Decisions); mod variant recorded |
| 4 | MAJOR | Acceptance measured every sentence, not mid-turn lines | Items carry `source`; acceptance keeps `pre_tool_use`/`post_tool_use` items (Testing) |
| 5 | MAJOR | Narration turned off mid-turn holds the engine 10 minutes (bridge) | Hooks: `stop` releases at every level (§3); bridge variant recorded |
| 6 | MINOR | Tasks 2, 3, 5 leave the suite red (a third `--drain` call, two global-lock writes, real drainers racing tests, a stale dialog marker) | All four fixed in the plan; `VOICEOVER_NO_SPAWN` moves to Task 2 |
| 7 | MINOR | The permission echo plays beside the mod's dialog alert | Mod-only; recorded (the hooks write the marker synchronously) |
| 8 | MINOR | Partial round-1 fixes: prompt pre-warms; reload gate; backoff cleared too early; pins resolved after the level check | Mod parts recorded; `spawned.json` cleared only after `open_speaker()` succeeds, 30s after a failed load (§4); pins resolved first (§9) |
| 9 | MINOR | Abort flush depends on how the engine aborts | Mod-only; recorded |
| 10 | MINOR | Sticky items cannot be retired | `post_tool_use` retires a session's pending sticky items (§2) |
| 11 | MINOR | Segmenter: inline fence after a boundary; quadratic rescans | Mod-only; recorded |
| 12 | MINOR | A prompt with no session id stops every session | No session: no clear, no stop (§6) |
| 13 | MINOR | Windows: wavs left behind; the chime's 10s PowerShell sleep | `play/` pruned when a speaker opens (§4); the chime is unchanged 1.3 behaviour |
| 14 | MINOR | `KokoroSpeaker.begin()` blocks during synthesis: no stop polling, no lock refresh | `begin()` returns a playback that renders, then plays, polled throughout (§4) |
| nits | — | Fallback recognizes only `<task-notification>`; tautological queue-dir check; contradictory Task 2 wording; a timing-sensitive lock test; mod nits | Origin limit stated in Decisions; Task 2 wording fixed; lock test margins doubled; mod nits recorded |

