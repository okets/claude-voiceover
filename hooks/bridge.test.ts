import { expect, test } from 'claude-code/testing'
import { parseReply } from './bridge'

test('the reply is read from the last stdout line', () => {
  expect(parseReply('noise\n{"narrate": true}\n')).toEqual({ narrate: true })
  expect(parseReply('{"narrate": false}')).toEqual({ narrate: false })
})

test('a missing or broken reply reads as not narrating', () => {
  expect(parseReply('')).toEqual({ narrate: false })
  expect(parseReply('{"narrate": null}')).toEqual({ narrate: false })
  expect(parseReply('not json')).toEqual({ narrate: false })
})
