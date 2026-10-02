# Low-Latency Narration — Design

Date: 2026-10-02
Status: draft, pending adversarial review and user approval
Target version: 1.4.0
Builds on: `2026-09-10-narration-queue-design.md` (the spool, the drain loop,
the cutover hook). Everything that document settles stands unless this one
changes it.

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
     0.84s (import 0.47 + load 0.37), first-sentence synthesis ~0.5s. Measured
     live with a queue monitor: enqueue → first sound ≈ 1.9s.
  3. *Read-everything FIFO across all sessions.* Speech runs at 19 characters
     per second, the screen is instant, and one machine-wide queue reads every
     item in order. Another session's lines were observed interleaving with
     this one's. Replay: one line in ten waits over 10s for its first word,
     the worst about two minutes.
- **Not causes:** hook process overhead (0.06s per hook, measured) and
  synthesis speed (warm, about 5x faster than playback).

## Goals

- Mid-turn heard lag (time from a line appearing on screen to it being
  heard) at a median ≤ 2.5s and a p90 ≤ 8s. The replay predicts 2.3s / 7.6s
  for this design.
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

## Decisions (settled with the user)

| Question | Decision |
|---|---|
| Queue scope | **Per project.** Project = `settings.detect_project_root(cwd)`, the same root the voice setting resolves against, so a project's queue and its voice always agree. Different projects overlap freely. |
| Voice | **Per project**, through the existing project setting (`/voiceover:voice nicole --project` writes `.claude/voiceover.json`). Rollout sets `bf_emma` for `/Users/hanan/Projects/OrcaMCP` and `af_nicole` for `/Users/hanan/Projects/claude-voiceover`. |
| New text arrives while speaking | **Finish the current sentence, then start the newest message.** The unread remainder of the current message, and every message queued in between, is dropped. |
| What "new text" means | A new **message** (one assistant text block, or one classic hook batch). Sentences of the same message play in order; only a different message preempts. Without this, streaming would make every sentence "new" and the narration would skip within a paragraph. |
| The user's own new prompt | **Stop at once, even mid-sentence** (today's behaviour), drop that session's pending items. The engine stays loaded so the reply starts warm. |
| Sessions sharing a project | Share the queue and the voice; the newest message wins between them. |
| Engine lifetime | **Per project, turn-scoped.** Loaded when a prompt is submitted; released when the turn ends and while a question dialog waits on the user; the process exits once its queue is empty and no session holds it. |
| Model | **Keep `kokoro-v1.0.onnx` (fp32).** Measured: int8 is 407MB vs 683MB but synthesizes a 78-character sentence in 1.72s vs 0.76s — about a second more before every line. |
| Capture | **A mod inside this plugin**, tapping `turn.step` text chunks. Python keeps all policy. The early-access API and TypeScript were accepted by the user. |

Measured to justify the turn-scoped engine: across 168 turns, the first
assistant text arrived 2.2s (p10) / 4.6s (median) after the prompt, never
sooner than the 1.4s the engine needs to load. Pre-warming at prompt submit
therefore hides the cold start completely. Turn length: median 10s, p90 254s.

## Architecture

```
Claude Code process                         data_dir()/projects/<key>/   (one per project)
───────────────────                         ─────────────────────────
mod: hooks/narrator.ts                      root.txt      the project root, for humans
  turn.step text chunks                     queue/        *.json items {..., utterance, sticky}
    → segmenter (hooks/segment.ts)          leases/       <session>.json {"expiry"}
    → bridge channel (serialized)           stop.json     {"ts", "session"}  cutover request
  prompt.submit / tool.call / turn.complete tts.lock      the project engine's lock
        │                                   play/         the engine's wav files
        ▼
hooks/run.sh mod_bridge  ──►  hooks/mod_bridge.py (stdlib)
                                │  speech.enqueue_speech / lease / request_stop
classic hooks (fallback) ───────┤  speech.ensure_drainer(cwd)
                                ▼
                        engine --drain <project dir>     one process per project
                          claim lock → load model once → sentence loop
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
`/Users/hanan/Projects/claude-voiceover` (no `.claude` of its own) resolves to
`/Users/hanan`, so every project without its own `.claude` shares one root,
and `/voiceover:voice <name> --project` run there writes
`~/.claude/voiceover.json`, changing the voice of all of them. With the fix
the `.claude` search skips the home directory and falls through to the `.git`
search, so this repo resolves to itself. Side effect: such projects' default
(hash-picked) voice changes once.

### 2. Spool — per project (`voiceover/spool.py`, `tts/engine_common.py`)

- Every spool function takes `cwd` and works in `project_dir(cwd)/queue/`.
  The filename rule, atomic write, atomic claim and 300s age cap are unchanged.
- Items gain two fields:
  - `"utterance": str` — items that share it are one message. The mod uses
    `<turnId>:<step index>:<block index>`; classic hooks use a fresh
    `uuid4().hex` per enqueue call.
  - `"sticky": bool` — a dialog alert. The jump rule never drops it.
- `enqueue_speech()` gains `utterance=None, sticky=False`; `None` means a
  fresh uuid.
- The legacy machine-wide `data_dir()/queue/` is no longer read. The first
  `ensure_drainer()` of 1.4.0 deletes whatever is left in it (every item there
  is at most five minutes from the age cap anyway).

### 3. Leases — `voiceover/lease.py` (new, stdlib) + read half in `engine_common`

```python
LEASE_TTL = 600.0
def hold(cwd, session, ttl=LEASE_TTL) -> bool   # leases/<session>.json {"expiry"}, tmp + rename
def release(cwd, session) -> None               # delete it
```

Engine side: `lease_is_live(project_dir) -> bool` — true if any lease file has
an expiry in the future; it deletes expired lease files as it goes.

The TTL is a crash backstop, not a timer: holders renew on every model step
(mod) or every tool hook (fallback) and release at turn end. A single tool
call longer than the TTL lets the lease lapse and the engine exit; the next
step's hold re-warms it, and the model's own thinking time hides the load.

| Event | Mod | Fallback hooks |
|---|---|---|
| Prompt submitted | `prompt` op: cutover (§6), hold, ensure | `user_prompt_submit`: same |
| Each model step starts | `hold` op | `pre_tool_use` / `post_tool_use`: hold |
| Question dialog opens (AskUserQuestion, ExitPlanMode) | `dialog` op: enqueue sticky alert, release | `pre_tool_use`: same |
| Dialog answered | `hold` op, ensure | `post_tool_use` for those tools: hold, ensure |
| Turn ends (answer, aborted, refusal, error) | `turn-end` op: release | `stop`: release (Stop does not fire on a user interrupt, so the lease lapses at the TTL, or is renewed by the next prompt) |
| Session ends | `end-session` op: release | — (TTL) |

### 4. The engine drain loop — `tts/engine_common.py`

`drain(project_dir, play_sentence, engine_names, prepare=None) -> int`
replaces today's `drain()`:

```
claim <project>/tts.lock atomically (exit 0 if held), expiry 60s
prepare()                                   # kokoro: load the model once
loop:
    honour_stop_request()                   # §6
    item = next_item()                      # the jump rule, below
    if item is None:
        if not lease_is_live(project_dir):
            release lock; re-check the queue once (the drain-end race,
            unchanged from 1.3); exit 0 if still empty
        sleep POLL_SECONDS (0.05); continue
    for sentence in split_sentences(item.text):
        play the sentence; while it plays, poll every POLL_SECONDS:
            a stop request naming item.session → kill playback, abandon item
        if a pending item has a different utterance → abandon the rest of item
    finish(item)
```

Every poll — idle, or while a sentence plays — refreshes the lock when fewer
than 30s of its 60s expiry remain, so a live engine never looks free.

**The jump rule (`next_item`)**, over pending items in filename order:

1. Let U be the utterance of the newest pending item.
2. Drop (finish unspoken) every pending item whose utterance is not U and
   which is not sticky.
3. Claim and return the oldest remaining item.

So the sentences of one message play in order; a different message ends the
current one at its next sentence boundary and skips everything between; a
sticky alert is never skipped.

**Sentence splitting** (`split_sentences`, engine side): split after `.`, `!`
or `?` followed by whitespace, and at newlines; drop empty pieces. Mod items
are already single sentences, so for them this is a no-op; classic items are
whole messages and are split here. This is required because kokoro-onnx
batches by 510 phonemes, not by sentence (verified in the installed
`kokoro_onnx/__init__.py`), so the existing streaming path cannot preempt at
a sentence boundary.

**Kokoro playback:** a worker thread synthesizes sentence k+1 while sentence
k plays (onnxruntime releases the GIL), so there is no gap between sentences.
Each sentence is written to `<project>/play/<n>.wav` and played with
`afplay` (Linux: `aplay`/`ffplay`; Windows: the existing PowerShell player)
through `Popen`, polled so a stop request can kill it. The existing
`stream_chunks()` path stays for the one-shot `--stream` use only.

**macOS `say` playback:** `say --voice=<Name>` through `Popen`, text on stdin,
polled the same way. No prefetch: `say` starts speaking within ~0.1s.

Exit condition change: today the drainer exits as soon as the spool is
empty. Now it waits while a lease is live, so a warm engine is ready for the
next sentence of the turn.

### 5. Writer side — `voiceover/speech.py`

```python
def enqueue_speech(text, min_level="concise", cwd=None, full=False,
                   session=None, utterance=None, sticky=False) -> bool
def ensure_drainer(cwd=None) -> bool
    # spawn `--drain <project_dir>` when the project's lock is not live AND
    # (its queue is non-empty OR a lease is live) - the second condition is
    # the pre-warm
def prewarm(cwd, session) -> None      # lease.hold + ensure_drainer
def request_stop(cwd, session) -> None # write <project>/stop.json {"ts", "session"}
```

### 6. Cutover on the user's own prompt

At the `narrator` level (mod `prompt` op, or classic `user_prompt_submit`):

1. `spool.clear(session=..., cwd=...)` — drop that session's pending items.
2. `request_stop(cwd, session)` — the engine kills the sentence playing now
   only if it belongs to that session; other sessions' items are untouched.
3. `prewarm(cwd, session)`.

Clear before stop, as in 1.3, so the engine cannot claim one more item of the
abandoned turn in between. The engine tracks the newest `stop.json` `ts` it
has honoured and ignores requests older than its own start. No process is
killed at the `narrator` level any more.

Non-`narrator` levels keep today's sequence (clear, then the global one-shot
`stop_speech()`), with the scoping below.

### 7. Scoping the non-narrator interrupt

`process_utils.stop_all_tts()` (called by `stop_speech()`) must never kill a
narrator engine or its playback, because another project may be narrating:

- Engine patterns become one-shot specific: `kokoro_voice.py --voice` and
  `macos_say.py --voice`. Drainers run as `--drain <dir>` and do not match.
- One-shot Kokoro playback files are named `voiceover-oneshot-*.wav`; the
  playback pattern becomes `afplay .*voiceover-oneshot-.*\.wav`. Drainer
  files live under `projects/<key>/play/` and do not match.
- The drainer invokes `say --voice=<Name>`, which the one-shot patterns
  `say -v Samantha` / `say -v Daniel` do not match.
- `tts/tts_controller.py stop` — the user's kill switch — still kills
  everything, and also clears every project's lock, queue and stop file.

### 8. The mod — `hooks/narrator.ts`, `hooks/segment.ts`

Declared in `hooks/hooks.json` under `"modules": ["./narrator.ts"]`, beside
the existing `"hooks"` block. A probe with both keys passes
`claude plugin validate` on Claude Code 2.1.287.

| Hook | Behaviour |
|---|---|
| `session.start` | `start` op: write the marker `data_dir()/mod_sessions/<session_id>`, prune markers older than 7 days. |
| `session.end` | `end-session` op: release the lease, delete the marker. |
| `prompt.submit` | `prompt` op, **not awaited** before `next(e)`, so the prompt is never delayed. The op returns `{"narrate": bool}` (level is `narrator`, TTS enabled, engine not `none`) and, when true, performs the cutover and pre-warm of §6. |
| `turn.step` (main loop only: `e.agentId` absent) | An async generator that yields every chunk of `next(e)` unchanged and immediately. A text chunk is yielded first, then appended to the segmenter for block key `<turnId>:<index>:<chunk.index>`. Complete segments become `say` ops. When the step's stream ends, each block's remainder is flushed. A `hold` op is sent at step start. When `narrate` is false the tap does nothing but yield. |
| `tool.call` for AskUserQuestion, ExitPlanMode | `dialog` op (Python builds the alert with `blocking_dialog_message`, enqueues it sticky under the session's most recent utterance key — so it continues the lead-in message rather than preempting it — writes `dialog_alert.json`, releases the lease); then `const r = await next(e)`; then a `hold` op; return `r`. |
| `turn.complete` (main loop only: `e.agentId` absent; every reason) | `turn-end` op: release the lease; play the decide chime when the turn's last step queued no text (the stop-hook rule from commit c9aa3de). A subagent's `turn.complete` is ignored — releasing there would unload the engine mid-turn. |

**The bridge.** `$.process.run(["bash", $.plugin.root + "/hooks/run.sh",
"mod_bridge"], { stdin, timeoutMs: 5000 })`, payload
`{"session", "cwd", "ops": [...]}`. One serialized channel per session: ops are
delivered in order; while a call is in flight, new ops accumulate and go out
together in the next call. Each call costs ~0.06s and never blocks the
stream. The `narrate` flag that `turn.step` reads is resolved long before the
first text chunk (prompt to first text was never under 2.2s in 168 turns);
the tap awaits that promise lazily, at the first text chunk, never before
calling `next(e)`.

**`hooks/mod_bridge.py`** (stdlib) executes the ops by calling the same
`speech` / `spool` / `lease` / `templates` functions the classic hooks use.
No narration policy lives in TypeScript: no gating, no cleaning, no voice
resolution. It prints one JSON line to stdout (`{"narrate": bool}`, read only
by the `prompt` op's caller).

**The segmenter** (pure, unit-tested): emits a segment after `.`, `!` or `?`
followed by whitespace, and at each newline, never inside a fenced code
block — a fence is emitted whole once it closes, so `clean_for_speech()` turns
it into the spoken code cue. `flush()` emits the remainder. The bridge
cleans each segment with `clean_for_speech()` and does not queue segments that
clean to nothing (table rows, rules).

**Failure containment.** Every hook body is wrapped in try/catch; a failure
is logged with `$.ui.log(..., { to: "debug" })`. The stream tap yields each
chunk before touching the segmenter, so a segmenter bug cannot drop or delay
displayed text. A bridge call that fails or times out loses its batch (logged);
the next batch proceeds.

### 9. Classic hooks as the fallback

- Every narrator path first checks `mod_active(session_id)` (the marker
  exists). If it does, the path does nothing — except `user_prompt_submit`'s
  session-scoped `clear()`, which runs at every level as today.
- Otherwise the paths behave as in 1.3, plus: the per-project spool, a fresh
  utterance per enqueue, and the lease table of §3. The dialog item keeps
  1.3's shape — lead-in prose and alert combined in one item — and is
  enqueued sticky.
- `notification.py` is unchanged; the mod does not cover permission prompts.
  The `dialog_alert.json` write moves into one function both paths call.

## Error handling

- Engine: a corrupt item is dropped; an unreadable `stop.json` or lease file
  is ignored; a playback failure skips that sentence; a synthesis failure
  skips the rest of the item.
- Engine crash: the project lock expires within 60s; the next
  `ensure_drainer()` respawns. Leases expire within `LEASE_TTL`.
- Claude Code crash: leases lapse at the TTL; the engine then exits on its own.
- Mod: hook failures never reach the stream (§8). The marker could outlive a
  module that loaded and then failed on every call; the narration would then
  be silent for that session. Accepted: the module catches everything, and the
  tests cover each hook.

## Testing

Python (stdlib harness, `VOICEOVER_DATA_DIR` + `VOICEOVER_DRY_RUN`):

1. Projects: same root → same dir; two roots → two dirs; `root.txt` written;
   voice resolution unchanged.
2. Spool per project: enqueue/clear/prune scoped to one project; `utterance`
   and `sticky` stored; the legacy queue is deleted once.
3. Leases: hold, renew, release, expiry, `lease_is_live` deletes expired files.
4. Jump rule: same-utterance items play in order; a newer utterance drops the
   remainder at the next sentence boundary and every item in between; sticky
   items survive; two sessions in one project — newest wins.
5. Engine lifecycle (dry-run engine recording sentences and timestamps): waits
   while a lease is live; exits once the queue is empty and the lease is
   released; the drain-end race still holds; a pre-warm with an empty queue
   spawns and loads.
6. Stop request: kills the current sentence of the named session only; an
   older request is ignored.
7. Stop scoping: a stand-in process whose command line contains
   `kokoro_voice.py --drain` survives `stop_all_tts()`; one containing
   `kokoro_voice.py --voice` does not.
8. Bridge: every op, the `narrate` reply, batching, `clean_for_speech()`
   applied, empty segments dropped.
9. Fallback stand-down: with the marker present, the classic narrator paths
   queue nothing; without it they follow the lease table.
10. Regressions: the existing suite passes unchanged in intent.

Mod (`claude plugin test`, `hooks/*.test.ts`):

1. Segmenter: sentence ends, newlines, fences held until closed, flush.
2. Stream tap: every chunk yielded unchanged and in order; subagent steps
   ignored; nothing sent when `narrate` is false; a throwing bridge does not
   break the stream.
3. Channel: ops delivered in order and batched while a call is in flight.
4. Lifecycle: `prompt` on submit; `turn-end` on `answer` and `aborted`;
   `dialog` before and `hold` after AskUserQuestion.

Live acceptance, on the user's machine, in the VS Code extension and a
terminal session: the engine logs, per sentence, its start time and the
`shown` time the mod stamped when the segment's last chunk arrived. Over a
working session of at least 50 mid-turn lines, heard lag median ≤ 2.5s and
p90 ≤ 8s; the engine process is gone within a second of a turn's last
sentence.

## Rollout

- Version 1.4.0. Live sessions must restart (or `/reload-plugins`) to pick up
  a release, as before; whether `/reload-plugins` loads a newly added module
  is verified in the plan's first task.
- The plan's first task is a throwaway probe mod that confirms, in the user's
  VS Code extension session, that `turn.step` text chunks arrive as the text
  streams and that `$.process.run` works there. If it fails, stop and report.
- Claude Code builds without function hooks: whether they tolerate the
  `modules` key in `hooks.json` is unknown. Check on any older build
  available; if it is refused, document the minimum version in the README.
- Set the two project voices (Decisions).

## Files touched

New: `voiceover/projects.py`, `voiceover/lease.py`, `hooks/mod_bridge.py`,
`hooks/narrator.ts`, `hooks/segment.ts`, `hooks/*.test.ts`,
`tests/test_projects.py`, `tests/test_lease.py`, `tests/test_jump_rule.py`,
`tests/test_engine_lifecycle.py`, `tests/test_stop_scope.py`,
`tests/test_mod_bridge.py`, this spec.

Modified: `voiceover/spool.py`, `voiceover/speech.py`,
`voiceover/process_utils.py`, `tts/engine_common.py`, `tts/kokoro_voice.py`,
`tts/macos_say.py`, `tts/tts_controller.py`, `hooks/pre_tool_use.py`,
`hooks/post_tool_use.py`, `hooks/stop.py`, `hooks/user_prompt_submit.py`,
`hooks/hooks.json`, `INTERFACES.md`, `CHANGELOG.md`, `README.md`,
`.claude-plugin/plugin.json` (1.4.0), the existing tests that assume one
machine-wide queue.

## Out of scope

- One engine shared by all projects.
- A smaller or quantized model.
- Speeding speech up when it falls behind.
- Releasing the lease during permission prompts.
- Any other change at non-`narrator` levels.
