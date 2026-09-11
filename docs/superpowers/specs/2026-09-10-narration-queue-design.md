# Narration Queue — Design

Date: 2026-09-10
Status: approved for planning
Target version: 1.3.0

## Problem

At the `narrator` level, hooks read Claude's new prose from the transcript and
call `speak()`. When audio is already playing, `speak()` returns `False`, the
byte-offset cursor is deliberately NOT advanced, and the text is retried at the
next hook. Two consequences, both measured from a real 2,200-line
`voiceover.log`:

1. **Dead air.** Nothing plays until the next hook fires. A hook fires only on
   a tool call or turn end, so when playback finishes mid-thought the narration
   stays silent for as long as the next tool call takes — observed 10–40s.
2. **Churn and drift.** 874 speak attempts produced 616 utterances: 30% were
   skipped on the lock. Because each retry re-reads from the same stale cursor,
   the pending text keeps growing — 30 utterances grew before being spoken, by
   a median of 152 characters and a maximum of 3,279. The voice therefore reads
   a merged blob that *begins with the oldest unspoken sentence*, a median 11.8s
   and a p90 42.1s behind the screen. This is what reads as an "off-by-one".

A third cost is per-utterance: every utterance spawns `uv run` and reloads the
Kokoro ONNX model. Measured on this machine: 0.22s import + 0.59s model load +
1.96s synthesis for a short line, ~2.8s before the first sound, with ~1s of
that being pure spawn-and-reload overhead paid again for every sentence.

## Goals

- Never wait for a hook to resume speaking: when one utterance ends, the next
  starts immediately.
- Keep today's read semantics: Claude's own narration never clips itself, and
  text is never silently discarded inside a turn. The single exception is the
  user's own new message, which cuts over immediately (see Decisions).
- Pay process spawn and model load once per turn, not once per sentence.
- Non-`narrator` levels keep their current behaviour exactly.

## Decisions (settled with the user)

| Question | Decision |
|---|---|
| What does an "urgent" utterance do (permission prompt, end-of-turn summary)? | **Never interrupt. Always append.** The narrator path drops `interrupt` entirely. |
| A new user message while narration is backed up? | **Cut over immediately.** Clear that session's pending queue AND stop the sentence currently playing, so the answer to the new message is the next thing heard. |
| Unbounded backlog on a long unattended run? | **Cap by age.** Prune anything older than 5 minutes. |

Rationale for never-interrupt: because the session blocks while waiting on a
permission prompt, no new prose is generated, so the queue drains and reaches
the prompt on its own. The wait is self-limiting.

Rationale for the one exception: a new user message is the user declaring the
previous turn finished from their point of view. Letting a long in-flight
sentence run to its end would delay the answer to the question they just asked
by exactly as long as that sentence — the opposite of what the queue is for.
Interrupt in narrator mode is therefore triggered only by the user, never by
Claude.

## Architecture

Three parts: a spool (the queue), a writer side in the stdlib hook layer, and
a drainer inside the engines.

```
hook (stdlib)                     spool dir                  engine (tts/)
─────────────                     ─────────                  ─────────────
peek_new_prose(transcript)
      │
      ├─ enqueue(text, engine, voice, session) ─►  0000173…-4711-00.json
      ├─ commit_offset(...)   ◄── cursor advances on ENQUEUE, not on playback
      └─ ensure_drainer()
             │  lock not live?
             └── spawn ──────────────────────────────────────►  --drain
                                                                  │
                                       take oldest ◄──────────────┤ loop:
                                       delete it   ◄──────────────┤   speak
                                                                  │   next
                                       empty? ─────────────────────►  release
                                                                      lock, exit
```

### 1. `voiceover/spool.py` (new, stdlib only)

The queue is a directory, `data_dir()/queue/`, holding one file per utterance.

- **Item filename:** `<epoch_ms:013d>-<pid>-<seq:02d>.json`. Lexicographic
  filename order equals chronological order (13 digits is good past year 2286).
  `seq` is a per-process counter so one hook enqueueing twice keeps its order.
- **Item content:** `{"text": str, "engine": str, "voice": str, "created": float,
  "session": str}`. `created` is the authority for age checks — the reader never
  parses filenames for anything but ordering. `session` is the hook payload's
  `session_id`; see *Concurrent sessions* below.
- **Listing:** only `*.json` files are candidates. `*.tmp` (a write in flight)
  and `*.taken` (an item a reader has claimed) are ignored by the sort.
- **Atomic append:** write to `<name>.tmp`, then `os.rename` into place. A
  reader can therefore never observe a half-written item.
- **Claim:** the reader lists, sorts, and `os.rename`s the oldest item to
  `<name>.taken` before reading it. Rename is atomic, so of two readers exactly
  one wins an item.

Public interface:

```python
def enqueue(text, engine, voice, session=None) -> bool   # prunes by age, then appends
def clear(session=None) -> int                # drop pending items (all, or one session's)
def pending_count() -> int
def prune(max_age_seconds=QUEUE_MAX_AGE_SECONDS) -> int
QUEUE_MAX_AGE_SECONDS = 300
```

`enqueue()` prunes before appending, so the cap is enforced without a timer.

Note there is deliberately no `take_oldest()` here: hooks only ever WRITE to the
spool and engines only ever READ from it. Keeping each side to its own half means
the two implementations overlap on nothing but the directory path and the
filename sort rule.

### 2. Writer side — `voiceover/speech.py` and the narrator hook paths

`speech.py` gains two functions and keeps `speak()` untouched for the other
levels:

```python
def enqueue_speech(text, min_level="concise", cwd=None, full=False, session=None) -> bool
    # gates exactly as speak() does (is_tts_enabled, level_at_least), applies the
    # same truncation contract (full=True -> whole text, capped at _FULL_TEXT_CAP;
    # full=False -> truncate_for_speech), resolves engine+voice, appends to the
    # spool. No lock check: queueing never fails because audio is playing.
def ensure_drainer(cwd=None) -> bool
    # if the tts lock is not live AND the spool is non-empty, spawn the resolved
    # engine with --drain. Called UNCONDITIONALLY by every narrator hook path,
    # not only after an enqueue, so a spool left by a crashed engine is picked up
    # by the next hook. Racing hooks are harmless: the engine's atomic O_EXCL
    # claim admits exactly one.
```

The four narrator paths become the same three lines — read, enqueue, commit —
which removes the duplicated lock-race handling they each carry today:

- `pre_tool_use.py` — enqueue new prose. The `AskUserQuestion`/`ExitPlanMode`
  branch keeps its flush-race retry loop (it must catch the lead-in prose before
  the session blocks) and keeps appending the "I need your input" line, but
  appends instead of interrupting.
- `post_tool_use.py` — enqueue new prose.
- `stop.py` — keep the existing poll for the final flush, then enqueue the
  finale. The templated-completion fallback also enqueues.
- `notification.py` — enqueue the permission request. It keeps `full=False`, so
  the message is truncated exactly as it is today; the three prose paths above
  pass `full=True`.

Because enqueueing guarantees the text will be spoken, `commit_offset()` is
called immediately on a successful enqueue. The stale-cursor re-read and the
growing-blob behaviour disappear by construction.

### 3. Drainer — `tts/kokoro_voice.py --drain`, `tts/macos_say.py --drain`

Engines may not import `voiceover/` (different process and venv), which is why
`kokoro_voice.py` and `macos_say.py` each carry their OWN inline copy of
`data_dir()` and the four lock helpers today. Adding a spool reader to each
would make that a third and fourth copy.

Instead one new self-contained, stdlib-only module lands **inside `tts/`**,
which both engines import as a sibling (both resolve it via `sys.path[0]`, the
script's own directory, regardless of `uv run`):

- `tts/engine_common.py` — `data_dir()`, `tts_lock_path()`, `lock_is_live()`,
  `try_claim_lock()`, `update_lock_expiry()`, `remove_tts_lock()`, the spool
  reader `queue_dir()`, `take_oldest()`, `finish()`, `put_back()`, and the
  shared `drain()` loop itself.

The two existing inline copies are deleted in favour of it. This is a targeted
cleanup of duplication the feature would otherwise multiply, not unrelated
refactoring: every function moved is one drain mode needs.

Drain loop (identical in both engines, differing only in how one item is
spoken):

```
claim lock (atomic; exit quietly if another engine holds it)
load model once
loop:
    item = take_oldest(engine_names)   # engine-aware: see below
    if item is None:
        release lock
        item = take_oldest(engine_names) # close the race with a hook that just appended
        if item is None: exit 0
        if not claim lock: exit 0   # another engine got there first
        # fall through and speak THIS item - a continue would re-scan and
        # the item, already renamed to .taken, would be invisible and lost
    if item["created"] older than cap: drop it, continue
    refresh lock expiry generously, then speak the item
```

`take_oldest(engine_names)` is engine-aware: scanning the spool in order, it
SKIPS any item whose `engine` is not in `engine_names`, leaving it exactly
where it is (both its place and its ordering intact), and claims the oldest
item this engine *can* speak. This is not the mismatch handling an earlier
version of this design described - a version where the wrong-engine drainer
stepped aside and "the hook safety net starts the right one" was tried and
found false: `ensure_drainer()` always spawns `resolve_engine(cwd)`, the very
engine that just stepped aside, so nothing ever started the other one and a
foreign item at the head froze the whole queue. Skipping instead of stopping
means the drainer always speaks the oldest item it CAN speak, and a foreign
item only leaves the queue by being spoken (once its own engine drains it) or
by aging out at the cap.

Two further changes to the kokoro drainer beyond this loop, both about
keeping exactly one voice on the speaker at a time:

- **Streaming, not synthesize-then-play.** `speak_one` calls
  `stream_chunks()`, which plays each synthesized chunk as it becomes
  available instead of rendering the whole item first - so a long prose item
  (up to `_FULL_TEXT_CAP` characters) starts speaking within seconds rather
  than after full synthesis. It refreshes the lock's expiry once per chunk
  (`update_lock_expiry`, same length-proportional estimate `speak_text()`
  uses), so a synthesis window longer than `lock_seconds` never reads as
  "free" to a concurrent hook.
- **Claim the lock before loading the model.** `prepare()` - passed to
  `drain()` and invoked only after `try_claim_lock()` succeeds - is what
  loads the ONNX model. Loading it up front would leave the lock free for
  the multi-second load, during which every hook that fires spawns another
  `uv run` that also loads the model and then exits on the lock; claiming
  first means only the drainer that will actually speak pays that cost.

### 3a. Concurrent sessions

Several Claude Code sessions run at once on this machine (five live `claude`
processes were observed while investigating). They share one `data_dir()`, and
today they already share the TTS lock: one speaks, the others drop their text.

The spool stays **global**, because the speaker is a global resource and
interleaving is unavoidable — but every item carries its `session_id`. Two
consequences:

- `clear(session=...)` drops only that session's items, so typing in one session
  never wipes narration belonging to another.
- The drainer is session-agnostic: it speaks whatever is oldest. Ordering across
  sessions is by arrival time, which is the only meaningful order for one pair
  of speakers.

The age cap is the backstop that keeps a busy second session from building an
unbounded backlog for the first.

### 4. Cut over on a new user message — `hooks/user_prompt_submit.py` (new)

A new `UserPromptSubmit` hook does two things, in this order:

1. `spool.clear(session=payload["session_id"])` — drop that session's pending
   items, so nothing from the abandoned turn can still be picked up. This runs
   at EVERY level, silent included: a queue stranded by a level change would
   otherwise be drained later and speak an abandoned turn. It is skipped only
   when the payload carries no `session_id`, because an unscoped clear would
   wipe every other session's pending narration.
2. `speech.stop_speech()` — kill the drainer's process group (the existing
   `process_utils.stop_all_tts` already targets only our own engines) and clear
   the TTS lock. Unlike the clear, this IS gated on the level being above
   `silent`, because `stop_speech()` is global rather than per-session: a user
   who has silenced their own narration must not be able to kill another
   session's live playback every time they type.

Order matters: clearing first means the drainer cannot claim one more item in
the window between the two calls. With the lock released and the spool empty,
the first prose of the new turn enqueues and starts a fresh drainer with no
wait — the next thing heard is the answer to the message just sent.

`hooks/hooks.json` registers the event with timeout 10.

**This adds a hook event, so Claude Code must be restarted once for the new
registration to take effect.** Everything else is picked up on the next hook
invocation.

### 5. Safety net

`ensure_drainer()` is called by every narrator hook path, not only after an
enqueue. So a spool left non-empty by a crashed engine is picked up by the very
next hook. Combined with the age cap, no item can be stranded indefinitely.

## Error handling

Unchanged in character: every hook keeps its try/except and exits 0, and the
spool functions fail soft — on any `OSError` they return a falsy result and the
caller proceeds. Specific cases:

- Corrupt or unparseable item file: the reader deletes it and continues.
- `.taken` leftovers from a killed reader: treated as stale and removed by
  `prune()` when older than the cap.
- Spool directory missing: created on demand by `data_dir()`-style `mkdir`.
- Lock held by a dead pid: existing expiry logic already handles it; the
  notification hook's stale-lock clearing stays.

## Testing

The repo has no test framework today; this adds `tests/` with plain
stdlib-only scripts runnable individually and by `tests/run_all.sh`, all using
`VOICEOVER_DATA_DIR` and `VOICEOVER_DRY_RUN` so nothing touches real state or
makes sound.

1. **Spool ordering** — enqueue N items across several fake pids; assert
   `take_oldest()` returns them in creation order.
2. **Concurrent writers** — spawn 8 processes each enqueueing 10 items; assert
   80 items, no partial files, no duplicate claims.
3. **Single claim** — two readers racing on one item; exactly one wins.
4. **Age cap** — backdate items past the cap; assert `enqueue()` prunes them
   and fresh items survive.
5. **Cursor commits on enqueue** — assert the prose cursor advances on a
   successful enqueue and does not re-read the same text on the next peek.
6. **End-to-end drain (dry-run)** — fill the spool, run the engine with
   `--drain` under `VOICEOVER_DRY_RUN`; assert every item is printed in order,
   the directory ends empty, and exit status is 0.
7. **Drain-end race** — append an item at the moment the drainer finds the
   spool empty; assert it is still spoken.
8. **Engine mismatch** — a macOS item at the head of the spool with a kokoro
   item queued behind it, while the kokoro drainer runs; assert the macOS
   item survives untouched AND the kokoro item behind it is still spoken -
   a foreign item at the head must never block an engine from reaching its
   own items further back.
9. **Cut over on new prompt** — `user_prompt_submit.py` empties that session's
   spool items, leaves another session's items alone, and clears the TTS lock so
   the next turn starts speaking immediately.
10. **Regressions** — status notices (the `<synthetic>` filter) stay unspoken;
    non-narrator levels still dispatch through `speak()` with today's
    interrupt and lock behaviour.

## Files touched

New: `voiceover/spool.py`, `tts/engine_common.py`, `hooks/user_prompt_submit.py`,
`tests/*`, this spec.

Modified: `voiceover/speech.py`, `hooks/pre_tool_use.py`,
`hooks/post_tool_use.py`, `hooks/stop.py`, `hooks/notification.py`,
`hooks/hooks.json`, `tts/kokoro_voice.py`, `tts/macos_say.py`, `INTERFACES.md`,
`CHANGELOG.md`, `.claude-plugin/plugin.json` (1.3.0).

## Out of scope

- Changing what gets read, or the wording of any template.
- Any behaviour change at `silent`, `quiet`, `concise`, or `verbose`.
- A long-lived daemon. The drainer lives exactly as long as it has work.
