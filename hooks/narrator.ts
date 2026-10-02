/**
 * Claude Voiceover's mod: speaks Claude's prose as it streams.
 *
 * It only captures. Each sentence of the main loop's visible text goes, in
 * order, to hooks/mod_bridge.py, which owns every narration policy - gating,
 * cleaning, voices, the per-project queue and engine. The bridge marks the
 * session on every call, and the classic hooks stand down for marked
 * sessions at the narrator level, so nothing is spoken twice.
 *
 * Nothing here may delay what the user sees: every chunk is yielded before
 * it is read, and only session.end and a question dialog wait on the bridge.
 *
 * The engine refuses a module that stores `$`, puts it in an object, or
 * hands it to a function in another file: every function here that needs
 * `$` takes it as a parameter and is declared at the top of this file, and
 * the state kept between hooks holds no `$`.
 */
import type { EngineInterface, Register, TurnStepResult } from 'claude-code'
import { parseReply, type Op } from './bridge'
import { isUsersOwnPrompt } from './origin'
import { createSegmenter, type Segmenter } from './segment'

type Pending = { op: Op; settle: (narrate: boolean) => void }

const SPEAKABLE = /[A-Za-z0-9]/

let queue: Pending[] = []
let pumping = false
/** This turn's narrator gate; undefined until a start, a prompt or a recovery asks for it. */
let gate: Promise<boolean> | undefined
/** The utterance of the last text sent this turn: a dialog alert continues it. */
let lastUtterance: string | null = null
/** Whether the current main step queued speakable text (the end-of-turn chime rule). */
let stepSpoke = false

function debug($: EngineInterface, text: string): void {
  void $.ui.log(`voiceover: ${text}`, { to: 'debug' })
}

/** One bridge call. The session id and root are read per call: /clear and resume change the id. */
async function runBatch($: EngineInterface, ops: readonly Op[]): Promise<boolean> {
  const [session, cwd] = await Promise.all([$.session.id(), $.session.root()])
  const { stdout } = await $.process.run(
    ['bash', `${$.plugin.root}/hooks/run.sh`, 'mod_bridge'],
    { stdin: JSON.stringify({ session, cwd, ops }), timeoutMs: 5000 },
  )
  return parseReply(stdout).narrate
}

/** Delivers queued ops in order; ops queued while a call runs go together in the next one. */
async function pump($: EngineInterface): Promise<void> {
  pumping = true
  while (queue.length > 0) {
    const batch = queue
    queue = []
    let narrate = false
    try {
      narrate = await runBatch($, batch.map(entry => entry.op))
    } catch (error) {
      debug($, `bridge: ${String(error)}`)
    }
    for (const entry of batch) entry.settle(narrate)
  }
  pumping = false
}

/** Sends op to the bridge, after every op sent before it. Resolves with the gate; never rejects. */
function send($: EngineInterface, op: Op): Promise<boolean> {
  return new Promise<boolean>(settle => {
    queue.push({ op, settle })
    if (!pumping) void pump($)
  })
}

/** This turn's gate; after a module reload lost it, ask the bridge with a status op. */
function currentGate($: EngineInterface): Promise<boolean> {
  gate ??= send($, { op: 'status' })
  return gate
}

/** Sends op once the gate says this session narrates. */
function whenNarrating($: EngineInterface, op: Op): void {
  void currentGate($).then(narrate => (narrate ? send($, op) : undefined))
}

/** Sends op after the gate is known, whatever it says (the bridge releases when narration is off). */
function always($: EngineInterface, op: Op): Promise<boolean> {
  return currentGate($).then(() => send($, op))
}

function emit($: EngineInterface, utterance: string, pieces: readonly string[]): void {
  if (pieces.length === 0) return
  const shown = Date.now() / 1000
  lastUtterance = utterance
  if (pieces.some(text => SPEAKABLE.test(text))) stepSpoke = true
  for (const text of pieces) whenNarrating($, { op: 'say', utterance, text, shown })
}

function questionsOf(e: unknown): unknown {
  return (e as { questions?: unknown }).questions ?? []
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    gate = send($, { op: 'start' })
    return started
  })

  on('session.end', async ($, e, next) => {
    await send($, { op: 'end-session' })
    gate = undefined
    return next(e)
  })

  on('prompt.submit', ($, e, next) => {
    // Never awaited: the prompt is not delayed. The gate resolves long before
    // the first text (no turn's first text came sooner than 1.84s after its
    // prompt, across 169 measured turns).
    gate = send($, { op: 'prompt', cutover: isUsersOwnPrompt(e.origin) })
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    lastUtterance = null
    whenNarrating($, { op: 'hold' }) // pre-warm: the model loads while Claude thinks
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (e.agentId !== undefined) return yield* stream // a subagent's words are not narrated
    const utterance = `${e.turnId}:${e.index}` // every block of one response is one message
    const segmenters = new Map<number, Segmenter>()
    let result: TurnStepResult | undefined
    stepSpoke = false
    whenNarrating($, { op: 'hold' })
    try {
      for (let step = await stream.next(); ; step = await stream.next()) {
        if (step.done) {
          result = step.value
          return step.value
        }
        yield step.value
        const chunk = step.value
        if (chunk.kind !== 'text') continue
        try {
          let segmenter = segmenters.get(chunk.index)
          if (!segmenter) {
            segmenter = createSegmenter()
            segmenters.set(chunk.index, segmenter)
          }
          emit($, utterance, segmenter.push(chunk.text))
        } catch (error) {
          debug($, `segmenter: ${String(error)}`)
        }
      }
    } finally {
      if (result === undefined) {
        // Interrupted: close the stream beneath; a half-sentence is not worth hearing.
        await stream.return(undefined as never)
      } else if (result.stopReason !== null && !next.signal.aborted) {
        for (const segmenter of segmenters.values()) emit($, utterance, segmenter.flush())
      }
    }
  })

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    // Awaited, so the alert is queued before the permission echo can speak.
    await always($, { op: 'dialog', tool: 'AskUserQuestion', input: { questions: questionsOf(e) }, utterance: lastUtterance })
    const result = await next(e)
    whenNarrating($, { op: 'hold' })
    return result
  })

  on('tool.call', { tool: 'ExitPlanMode' }, async ($, e, next) => {
    await always($, { op: 'dialog', tool: 'ExitPlanMode', input: {}, utterance: lastUtterance })
    const result = await next(e)
    whenNarrating($, { op: 'hold' })
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // A subagent's turn ending is not ours: releasing there would unload the
    // engine in the middle of the main turn.
    if (e.agentId === undefined) void always($, { op: 'turn-end', spoken: stepSpoke })
    return result
  })
}
