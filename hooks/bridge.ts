/**
 * The mod's protocol with hooks/mod_bridge.py: the ops it sends, and how the
 * bridge's one-line answer is read. Pure: the channel that sends them lives
 * in narrator.ts, which alone may hold the engine handle.
 */
export type Op =
  | { op: 'start' }
  | { op: 'status' }
  | { op: 'end-session' }
  | { op: 'prompt'; cutover: boolean }
  | { op: 'hold' }
  | { op: 'say'; utterance: string; text: string; shown: number }
  | { op: 'dialog'; tool: string; input: unknown; utterance: string | null }
  | { op: 'turn-end'; spoken: boolean }

export type Reply = { narrate: boolean }

/** The bridge's answer is its last stdout line: {"narrate": bool}. */
export function parseReply(stdout: string): Reply {
  const line = stdout.trim().split('\n').pop() ?? ''
  try {
    return { narrate: (JSON.parse(line) as { narrate?: unknown }).narrate === true }
  } catch {
    return { narrate: false }
  }
}
