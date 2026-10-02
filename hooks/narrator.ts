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

type Pending = { op: Op; settle: (narrate: boolean | null) => void }

/** A text block being cut into sentences, and when its last text arrived. */
type Block = { segmenter: Segmenter; at: number }

const SPEAKABLE = /[A-Za-z0-9]/

let queue: Pending[] = []
/** The bridge call in progress, or the last one: each delivery waits for it. */
let inFlight: Promise<void> = Promise.resolve()
/** The newest send's number: only its delivery runs, so a batch always goes out on a live dispatch's `$`. */
let latest = 0
/** This turn's narrator gate; undefined until a start, a prompt or a recovery asks for it, and again after an unknown answer. */
let gate: Promise<boolean> | undefined
/** The utterance of the last text sent this turn: a dialog alert continues it. */
let lastUtterance: string | null = null
/** Whether the current main step queued speakable text (the end-of-turn chime rule). */
let stepSpoke = false

function debug($: EngineInterface, text: string): void {
  try {
    void $.ui.log(`voiceover: ${text}`, { to: 'debug' })
  } catch {
    // an ended dispatch takes its log with it
  }
}

/** One bridge call. The session id and root are read per call: /clear and resume change the id. */
async function runBatch($: EngineInterface, ops: readonly Op[]): Promise<boolean | null> {
  const [session, cwd] = await Promise.all([$.session.id(), $.session.root()])
  const { stdout } = await $.process.run(
    ['bash', `${$.plugin.root}/hooks/run.sh`, 'mod_bridge'],
    { stdin: JSON.stringify({ session, cwd, ops }), timeoutMs: 5000 },
  )
  return parseReply(stdout).narrate
}

/**
 * Delivers every queued op in one bridge call, on this sender's `$`, unless a
 * newer send will: an interrupted hook's `$` calls abort with it.
 */
async function deliver($: EngineInterface, seq: number): Promise<void> {
  if (seq !== latest || queue.length === 0) return
  const batch = queue
  queue = []
  let narrate: boolean | null = null // unknown when the call fails
  try {
    narrate = await runBatch($, batch.map(entry => entry.op))
  } catch (error) {
    debug($, `bridge: ${String(error)}`)
  } finally {
    for (const entry of batch) entry.settle(narrate)
  }
}

/** Sends op to the bridge, after every op sent before it. Resolves with the gate, null when unknown; never rejects. */
function send($: EngineInterface, op: Op): Promise<boolean | null> {
  return new Promise<boolean | null>(settle => {
    queue.push({ op, settle })
    const seq = ++latest
    inFlight = inFlight.then(() => deliver($, seq)).catch(() => undefined)
  })
}

/**
 * Asks the bridge for this turn's gate with op. An unknown answer is asked
 * once more with status on the same promise, so the sentences already
 * waiting keep their order; still unknown, it reads as off and leaves no
 * gate, so the next op asks again. One failed call never silences a turn.
 */
function openGate($: EngineInterface, op: Op): Promise<boolean> {
  const asked: Promise<boolean> = send($, op)
    .then(narrate => (narrate === null ? send($, { op: 'status' }) : narrate))
    .then(narrate => {
      if (narrate === null && gate === asked) gate = undefined
      return narrate === true
    })
  gate = asked
  return asked
}

/** This turn's gate; after a module reload or an unknown answer lost it, ask the bridge with a status op. */
function currentGate($: EngineInterface): Promise<boolean> {
  return gate ?? openGate($, { op: 'status' })
}

/** Sends op once the gate says this session narrates. */
function whenNarrating($: EngineInterface, op: Op): void {
  void currentGate($).then(narrate => (narrate ? send($, op) : undefined))
}

/** Sends op after the gate is known, whatever it says (the bridge releases when narration is off). */
function always($: EngineInterface, op: Op): Promise<boolean | null> {
  return currentGate($).then(() => send($, op))
}

function emit($: EngineInterface, utterance: string, pieces: readonly string[], shown: number): void {
  if (pieces.length === 0) return
  lastUtterance = utterance
  if (pieces.some(text => SPEAKABLE.test(text))) stepSpoke = true
  for (const text of pieces) whenNarrating($, { op: 'say', utterance, text, shown })
}

/** Ends every open text block but `keep`: a block's last sentence goes out as soon as the next block starts, stamped when its text arrived. */
function closeBlocks($: EngineInterface, utterance: string, blocks: Map<number, Block>, keep?: number): void {
  for (const [index, block] of blocks) {
    if (index === keep) continue
    blocks.delete(index)
    emit($, utterance, block.segmenter.flush(), block.at)
  }
}

function questionsOf(e: unknown): unknown {
  return (e as { questions?: unknown }).questions ?? []
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    void openGate($, { op: 'start' })
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
    // A prompt asked to wait its turn (ctrl+x enter) leaves the running turn's speech alone.
    void openGate($, { op: 'prompt', cutover: isUsersOwnPrompt(e.origin) && !e.wait })
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
    const blocks = new Map<number, Block>()
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
        try {
          // Blocks stream one after another: a chunk of another block means the open ones are whole.
          if ('index' in chunk) closeBlocks($, utterance, blocks, chunk.index)
          if (chunk.kind !== 'text') continue
          let block = blocks.get(chunk.index)
          if (!block) {
            block = { segmenter: createSegmenter(), at: 0 }
            blocks.set(chunk.index, block)
          }
          block.at = Date.now() / 1000
          emit($, utterance, block.segmenter.push(chunk.text), block.at)
        } catch (error) {
          debug($, `segmenter: ${String(error)}`)
        }
      }
    } finally {
      if (result === undefined) {
        // Interrupted: close the stream beneath; a half-sentence is not worth hearing.
        await stream.return(undefined as never)
      } else if (result.stopReason !== null && !next.signal.aborted) {
        closeBlocks($, utterance, blocks)
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
    // An interrupted turn ends without the your-turn chime: the user stopped it.
    if (e.agentId === undefined) void always($, { op: 'turn-end', spoken: stepSpoke || e.isAborted })
    return result
  })
}
