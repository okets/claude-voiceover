import { expect, test } from 'claude-code/testing'
import { createSegmenter } from './segment'

test('sentences are emitted as soon as they complete', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Hello there. Sec')).toEqual(['Hello there. '])
  expect(segmenter.push('ond one! Third')).toEqual(['Second one! '])
  expect(segmenter.flush()).toEqual(['Third'])
})

test('a sentence end split across pieces waits for the next piece', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Done.')).toEqual([])
  expect(segmenter.push(' Next')).toEqual(['Done. '])
})

test('a newline ends a segment', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('A heading\n- an item\n')).toEqual(['A heading\n', '- an item\n'])
})

test('a fence at a line start is held until it closes, then emitted whole', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Look:\n```ts\nconst a = 1. b\n')).toEqual(['Look:\n'])
  expect(segmenter.push('```\nAfter.')).toEqual(['```ts\nconst a = 1. b\n```\n'])
  expect(segmenter.flush()).toEqual(['After.'])
})

test('a fence marker split across pieces is still recognized', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('``')).toEqual([])
  expect(segmenter.push('`\nx. y\n```\n')).toEqual(['```\nx. y\n```\n'])
})

test('backticks inside a line are not a fence, even right after a sentence', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('Wrap code in ``` marks. Then go.')).toEqual(['Wrap code in ``` marks. '])
  const after = createSegmenter()
  expect(after.push('Use triple backticks. ``` opens a block. Then go.'))
    .toEqual(['Use triple backticks. ', '``` opens a block. '])
})

test('punctuation inside a word or number is not a boundary', () => {
  const segmenter = createSegmenter()
  expect(segmenter.push('v1.4.0 is out and e.g.this too')).toEqual([])
  expect(segmenter.flush()).toEqual(['v1.4.0 is out and e.g.this too'])
})

test('a long fence streamed in small pieces is emitted once, whole', () => {
  const segmenter = createSegmenter()
  segmenter.push('```\n')
  let emitted: string[] = []
  for (let i = 0; i < 5000; i++) emitted = emitted.concat(segmenter.push('x = 1. y = 2;\n'))
  expect(emitted).toEqual([])
  const closed = segmenter.push('```\n')
  expect(closed.length).toBe(1)
  expect(closed[0]?.length).toBe(4 + 5000 * 14 + 4)
})

test('flushing only whitespace yields nothing', () => {
  const segmenter = createSegmenter()
  segmenter.push('Done. ')
  expect(segmenter.flush()).toEqual([])
})
