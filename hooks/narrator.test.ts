import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

type SentOp = { op: string; session: string; text?: string; utterance?: string | null; spoken?: boolean; input?: unknown; cutover?: boolean; tool?: string }

/** Stands beneath the mod for the engine: the bridge process, the session, the event bottoms. */
function fakeEngine(on: On, narrate: boolean, options: { failures?: number; ids?: string[] } = {}) {
  const ops: SentOp[] = []
  const ids = [...(options.ids ?? ['S1'])]
  let failures = options.failures ?? 0
  const waiters: { check: (sent: SentOp[]) => boolean; done: () => void }[] = []
  on('process.run', async (_$, e) => {
    if (failures > 0) {
      failures -= 1
      throw new Error('bridge down')
    }
    const batch = JSON.parse(e.init?.stdin ?? '{}') as { session: string; ops?: Omit<SentOp, 'session'>[] }
    ops.push(...(batch.ops ?? []).map(op => ({ ...op, session: batch.session })))
    for (const waiter of waiters.splice(0)) {
      if (waiter.check(ops)) waiter.done()
      else waiters.push(waiter)
    }
    return { value: { exitCode: 0, stdout: `${JSON.stringify({ narrate })}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.id', async () => ({ value: ids[0] ?? 'S1' }))
  on('session.root', async () => ({ value: '/work/project' }))
  on('ui.log', async () => ({ value: undefined }))
  on('prompt.submit', async (_$, e) => ({ text: e.text }))
  on('turn.start', async (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', async () => ({ text: '' }))
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('session.end', async () => {
    const ended = ids.shift() ?? 'S1' // a /clear: the process goes on under the next id
    return { sessionId: ended }
  })
  /** Resolves once the ops sent so far satisfy check. */
  const until = (check: (sent: SentOp[]) => boolean): Promise<void> =>
    check(ops) ? Promise.resolve() : new Promise<void>(done => { waiters.push({ check, done }) })
  return { ops, until }
}

/** The model's response for every step: these chunks (by turnId), then a result. */
function fakeStep(on: On, chunks: (turnId: string) => readonly unknown[]) {
  on('turn.step', async function* (_$, e) {
    for (const chunk of chunks(e.turnId)) yield chunk as never
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null }
  })
}

const text = (body: string, index = 0) => ({ kind: 'text', index, text: body })
const stop = { kind: 'stop', stopReason: 'end_turn', usage: null }
const prompt = (kind = 'composer') => ({ text: 'go', wait: false, origin: { kind } }) as never
const step = (extra: { agentId?: string; turnId?: string } = {}) =>
  ({ turnId: 'T', index: 0, model: 'm', messageCount: 1, ...extra })
const turnEnd = (extra: { agentId?: string } = {}) =>
  ({ turnId: 'T', answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const, ...extra })

async function collect<C>(stream: AsyncIterable<C>): Promise<C[]> {
  const out: C[] = []
  for await (const chunk of stream) out.push(chunk)
  return out
}

const opsOf = (sent: SentOp[]) => sent.map(o => o.op)

test('every chunk passes through unchanged, and each sentence is queued in order', async ($, on) => {
  const engine = fakeEngine(on, true)
  const chunks = [text('Reading the loader. Then'), text(' the tests.'), text(' A second block.', 2), stop]
  fakeStep(on, () => chunks)
  await $.prompt.submit(prompt())
  expect(await collect($.turn.step(step()))).toEqual(chunks)
  await engine.until(sent => sent.filter(o => o.op === 'say').length === 3)
  const says = engine.ops.filter(o => o.op === 'say')
  expect(says.map(o => o.text)).toEqual(['Reading the loader. ', 'Then the tests.', ' A second block.'])
  expect(new Set(says.map(o => o.utterance))).toEqual(new Set(['T:0']))
  expect(opsOf(engine.ops).slice(0, 2)).toEqual(['prompt', 'hold'])
})

test('the user\'s own prompt cuts over; a task notification does not; turn.start warms the engine', async ($, on) => {
  const engine = fakeEngine(on, true)
  await $.prompt.submit(prompt('composer'))
  await $.turn.start({ text: 'go', turnId: 'T' })
  await $.prompt.submit(prompt('task-notification'))
  await engine.until(sent => sent.filter(o => o.op === 'prompt').length === 2 && sent.some(o => o.op === 'hold'))
  expect(engine.ops.filter(o => o.op === 'prompt').map(o => o.cutover)).toEqual([true, false])
  expect(opsOf(engine.ops)).toContain('hold')
})

test('a subagent step is never narrated', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, () => [text('Some words. ')])
  await $.prompt.submit(prompt())
  await collect($.turn.step(step({ agentId: 'A1', turnId: 'SUB' })))
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.utterance)).toEqual(['T:0'])
})

test('an interrupted stream is not flushed', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, turnId => [text(turnId === 'CUT' ? 'A partial thought with no end' : 'Done. ')])
  await $.prompt.submit(prompt())
  for await (const _chunk of $.turn.step(step({ turnId: 'CUT' }))) break
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.text)).toEqual(['Done. '])
})

test('with narration off only the gate, the turn end and the session end reach the bridge', async ($, on) => {
  const engine = fakeEngine(on, false)
  fakeStep(on, () => [text('Quiet now. ')])
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await $.turn.complete(turnEnd())
  await $.session.end({ reason: 'other' } as never)
  await engine.until(sent => sent.some(o => o.op === 'end-session'))
  expect(opsOf(engine.ops)).toEqual(['prompt', 'turn-end', 'end-session'])
})

test('a new session id after /clear is used for every later op', async ($, on) => {
  const engine = fakeEngine(on, true, { ids: ['S1', 'S2'] })
  fakeStep(on, () => [text('Fresh start. ')])
  await $.prompt.submit(prompt())
  await $.session.end({ reason: 'clear' } as never)
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.session)).toEqual(['S2'])
})

test('after a lost state the mod recovers the gate with a status op', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, () => [text('Still here. ')])
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(opsOf(engine.ops)[0]).toBe('status')
})

test('a failing bridge never breaks the stream', async ($, on) => {
  fakeEngine(on, true, { failures: Infinity })
  const chunks = [text('Still shown. '), stop]
  fakeStep(on, () => chunks)
  await $.prompt.submit(prompt())
  expect(await collect($.turn.step(step()))).toEqual(chunks)
})

test('a dialog is announced under the lead-in message before it opens; the answer re-warms the engine', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, () => [text('Let me ask you. ')])
  let opsWhenOpened: string[] = []
  on('tool.call', { tool: 'AskUserQuestion' }, async () => {
    opsWhenOpened = opsOf(engine.ops)
    return { result: {} as never, text: 'answered' } as never
  })
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  const questions = [{ question: 'Which?', header: 'Pick', options: [], multiSelect: false }]
  await $.tool.call({ tool: 'AskUserQuestion', questions } as never)
  await engine.until(sent => sent.filter(o => o.op === 'hold').length === 2)
  expect(opsWhenOpened).toContain('dialog')
  expect(opsOf(engine.ops)).toEqual(['prompt', 'hold', 'say', 'dialog', 'hold'])
  const dialog = engine.ops.find(o => o.op === 'dialog')
  expect(dialog?.utterance).toBe('T:0')
  expect(dialog?.input).toEqual({ questions })
})

test('the turn end says whether the last step spoke; a subagent turn end is ignored', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, turnId => (turnId === 'MUTE' ? [text('---\n')] : [text('Done here. ')]))
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await $.turn.complete(turnEnd({ agentId: 'A1' }))
  await $.turn.complete({ ...turnEnd(), isAborted: true, reason: 'aborted' })
  await collect($.turn.step(step({ turnId: 'MUTE' })))
  await $.turn.complete(turnEnd())
  await engine.until(sent => sent.filter(o => o.op === 'turn-end').length === 2)
  expect(engine.ops.filter(o => o.op === 'turn-end').map(o => o.spoken)).toEqual([true, false])
})

test('one failed bridge call is asked again, and the sentences waiting keep their order', async ($, on) => {
  const engine = fakeEngine(on, true, { failures: 1 })
  fakeStep(on, () => [text('One. Two. ')])
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await engine.until(sent => sent.filter(o => o.op === 'say').length === 2)
  expect(opsOf(engine.ops)).toEqual(['status', 'hold', 'say', 'say'])
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.text)).toEqual(['One. ', 'Two. '])
})

test('a gate still unknown after its retry opens none: the next op asks again', async ($, on) => {
  const engine = fakeEngine(on, true, { failures: 2 })
  fakeStep(on, turnId => [text(turnId === 'LOST' ? 'Lost. ' : 'Heard. ')])
  on('tool.call', { tool: 'ExitPlanMode' }, async () => ({ result: {} as never, text: 'approved' }) as never)
  await $.prompt.submit(prompt())
  await collect($.turn.step(step({ turnId: 'LOST' })))
  await $.tool.call({ tool: 'ExitPlanMode' } as never) // settles only after the failed gate has
  await collect($.turn.step(step({ turnId: 'NEXT' })))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.text)).toEqual(['Heard. '])
  expect(opsOf(engine.ops)).toContain('status')
})

test('a step that ended without a stop reason is not flushed', async ($, on) => {
  const engine = fakeEngine(on, true)
  on('turn.step', async function* (_$, e) {
    yield text(e.turnId === 'OPEN' ? 'An open thought' : 'Closed. ') as never
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: e.turnId === 'OPEN' ? null : 'end_turn', usage: null }
  })
  await $.prompt.submit(prompt())
  await collect($.turn.step(step({ turnId: 'OPEN' })))
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(engine.ops.filter(o => o.op === 'say').map(o => o.text)).toEqual(['Closed. '])
})

test('after session.start the gate comes from the start op', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, () => [text('Welcome back. ')])
  await $.session.start({ cwd: '/work/project' } as never)
  await collect($.turn.step(step()))
  await engine.until(sent => sent.some(o => o.op === 'say'))
  expect(opsOf(engine.ops)).toEqual(['start', 'hold', 'say'])
})

test('a dialog with no lead-in carries no utterance', async ($, on) => {
  const engine = fakeEngine(on, true)
  on('tool.call', { tool: 'ExitPlanMode' }, async () => ({ result: {} as never, text: 'approved' }) as never)
  await $.prompt.submit(prompt())
  await $.turn.start({ turnId: 'T' } as never)
  await $.tool.call({ tool: 'ExitPlanMode' } as never)
  await engine.until(sent => sent.some(o => o.op === 'dialog'))
  const dialog = engine.ops.find(o => o.op === 'dialog')
  expect(dialog?.tool).toBe('ExitPlanMode')
  expect(dialog?.utterance).toBe(null)
  expect(dialog?.input).toEqual({})
})

test('a prompt typed mid-turn keeps the lead-in a later dialog continues', async ($, on) => {
  const engine = fakeEngine(on, true)
  fakeStep(on, () => [text('Let me check. ')])
  on('tool.call', { tool: 'AskUserQuestion' }, async () => ({ result: {} as never, text: 'answered' }) as never)
  await $.prompt.submit(prompt())
  await collect($.turn.step(step()))
  await $.prompt.submit(prompt()) // typed while the turn runs: no turn.start
  await $.tool.call({ tool: 'AskUserQuestion', questions: [] } as never)
  await engine.until(sent => sent.some(o => o.op === 'dialog'))
  expect(engine.ops.find(o => o.op === 'dialog')?.utterance).toBe('T:0')
})
