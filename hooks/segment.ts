/**
 * Cuts streaming markdown into speakable segments: one per sentence or line,
 * as soon as each completes. A fenced code block (``` at the start of a
 * line) is held until it closes and emitted whole, so the bridge's cleaner
 * turns it into the spoken code cue; ``` inside a line is text.
 *
 * Scanning resumes where the last push stopped, so a long fence streamed in
 * small pieces costs linear, not quadratic, time.
 */
export type Segmenter = {
  /** Appends streamed text; returns the segments it completed, in order. */
  push(text: string): string[]
  /** Returns what is left as a final segment (nothing when only whitespace). */
  flush(): string[]
}

const FENCE = '```'
const ENDS = new Set(['.', '!', '?'])

export function createSegmenter(): Segmenter {
  let buffer = ''
  let scanned = 0 // buffer[0, scanned) holds no boundary
  let inFence = false // fence state at `scanned`
  let startsLine = true // whether buffer[0] begins a line

  const atLineStart = (i: number): boolean => (i === 0 ? startsLine : buffer[i - 1] === '\n')

  /** End (exclusive) of the first complete segment, or -1 (and remember how far it got). */
  const boundary = (): number => {
    let i = scanned
    while (i < buffer.length) {
      if (buffer[i] === '`' && atLineStart(i)) {
        const marker = buffer.slice(i, i + FENCE.length)
        if (marker === FENCE) {
          inFence = !inFence
          i += FENCE.length
          continue
        }
        if (i + marker.length === buffer.length && FENCE.startsWith(marker)) break // a marker still arriving
      }
      const char = buffer[i]
      if (inFence) {
        i += 1
        continue
      }
      if (char === '\n') return i + 1
      if (char !== undefined && ENDS.has(char)) {
        const following = buffer[i + 1]
        if (following === undefined) break // the next piece decides
        if (/\s/.test(following)) return i + 2
      }
      i += 1
    }
    scanned = i
    return -1
  }

  const take = (): string[] => {
    const out: string[] = []
    for (let end = boundary(); end > 0; end = boundary()) {
      const piece = buffer.slice(0, end)
      buffer = buffer.slice(end)
      startsLine = piece.endsWith('\n')
      scanned = 0
      inFence = false
      if (piece.trim()) out.push(piece)
    }
    return out
  }

  return {
    push(text) {
      buffer += text
      return take()
    },
    flush() {
      const rest = buffer
      buffer = ''
      scanned = 0
      inFence = false
      startsLine = true
      return rest.trim() ? [rest] : []
    },
  }
}
