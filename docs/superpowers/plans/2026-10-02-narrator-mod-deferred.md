# Narrator Mod (Streaming Capture) — Deferred

**Status:** SUPERSEDED on 2026-10-02. Mods were switched on for this account later that day and the user put the mod in 1.4.0; its tasks are Tasks 8–10 of `2026-10-02-low-latency-narration.md`, with every change below folded in (and machine-checked with `claude plugin validate` and `claude plugin test`). Kept as the record of why and how it was deferred.

**Why:** Claude Code 2.1.287 refuses to load function-hook modules for installed and `--plugin-dir` plugins on this account. A headless probe printed:

> `hooks module not loaded: hooks modules are turned off for installed plugins in this process: the rollout switch served off; built-in plugins load regardless`

`claude plugin test` refuses for the same reason ("hooks modules are turned off in this process"). The user's condition for the mod was that it ships and reduces latency; with the switch off it can do neither, so 1.4.0 ships the rest of the design on the classic hooks (`2026-10-02-low-latency-narration.md`).

**What it would add:** capture at the moment text streams instead of at the next hook. In the replay it is the largest single gain: mid-turn heard lag median 5.0s → 2.3s, p90 15.8s → 7.6s (spec, Goals).

## The design and code on file

The full mod design is §8 of `docs/superpowers/specs/2026-10-02-low-latency-narration-design.md` as committed in `7eee726`, and the code and tests are Tasks 0, 7, 8 and 9 of the plan as committed in `7eee726`:

```bash
git show 7eee726:docs/superpowers/plans/2026-10-02-low-latency-narration.md
git show 7eee726:docs/superpowers/specs/2026-10-02-low-latency-narration-design.md
```

The mod-only pieces the 1.4.0 plan dropped, to restore with it: the bridge (`hooks/mod_bridge.py`), the session marker and stand-down (`narrator.mod_active` / `mark_mod_session` / `unmark_mod_session` / `stands_down`, `prose.skip_unread`), `hooks/mod.json` with `"hooks": "./hooks/mod.json"` in the manifest, and the TypeScript files (`segment.ts`, `bridge.ts`, `origin.ts`, `narrator.ts`, their tests).

## Before resuming: check the switch

1. Headless: a probe plugin whose `session.start` hook writes a file with `$.fs.write`, run with `claude -p --model haiku --plugin-dir <probe> "Reply ok"`. Mods are on when the file appears and stderr has no `hooks module not loaded` line.
2. The VS Code extension: Task 0 of the `7eee726` plan (the dev-mods probe), which also records the session's `surface` and its prompts' `origin.kind`.
3. `claude plugin test --help` must not say the switch is off; otherwise the mod's tests cannot run.

## Required changes before execution (adversarial review, round 2)

The `7eee726` code must absorb these first:

1. **BLOCKER — the validator's `$` rule.** The engine refuses a module that passes `$` to a closure, stores it in an object, or passes it to a function in another file. `$` may only be a parameter of a top-level function in the same file. Keep the channel queue at module level in `narrator.ts`, pass each hook's own `$` to `send($, op)`, read the session id and root per batch inside `runBatch($, ops)`, and leave only pure code in `bridge.ts`. The reviewer's validated shape is in its round-2 report (scratchpad `adversary/vprobe/v5.ts` at the time).
2. **BLOCKER — validate the right file.** `claude plugin validate .` at the repo root validates the marketplace only. Use `claude plugin validate ./.claude-plugin/plugin.json`.
3. **BLOCKER — the test kit.** Hooks answering `$` calls return `{ value: ... }` (`OpEventResult`); `session.end` returns `{ sessionId }`; `$.prompt.submit` needs `wait` and `origin` — pass `origin: { kind: 'composer' }` and assert `cutover === true`, plus a `task-notification` case asserting `false`. Run `claude plugin test` from the main session, and fail loudly if the switch is off.
4. **MAJOR — headless per batch.** Decide headless from `$.session.surfaces()` being empty (empty in a plain `-p` run), send it in the bridge payload, and gate in `mod_bridge.run`: `narrate = narrator.narrates(cwd) and not payload.get("headless")`. Otherwise a `-p` run's `sdk`-origin prompt pre-warms an engine and takes a `MAX_ENGINES` slot.
5. **MAJOR — release when narration is off.** In `mod_bridge.run`, `turn-end` and `dialog` must release the lease even when `narrate` is false, or turning narration off mid-turn holds the engine for the full TTL.
6. **MAJOR — acceptance metric.** Measure mid-turn items only (exclude each turn's final message), as 1.4.0's acceptance does with the item's `source`.
7. **MINOR — the echo.** Await the `dialog` op before `next(e)` in the `tool.call` hooks, so the permission echo sees the per-session marker.
8. **MINOR — partial round-1 fixes.** The bridge's prompt op must not pre-warm (`turn.start` does); after a module reload `session.start` re-fires, so take the gate from the `start` op's reply; the bridge pins the session's cwd per batch.
9. **MINOR — aborts.** Flush only when `!next.signal.aborted` and the final step's `stopReason !== null`; in `finally`, when not completed, `await stream.return(...)`.
10. **MINOR — segmenter.** Keep the line-start state and a scan offset across pushes (an inline ``` after a sentence boundary was taken for a fence; long fences rescanned quadratically).
11. **NITs.** A prompt typed mid-turn must not reset `lastUtterance`; `stepSpoke` should mean "queued speakable text", not "emitted a segment".
