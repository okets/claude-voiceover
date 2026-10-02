import { expect, test } from 'claude-code/testing'
import { isUsersOwnPrompt } from './origin'

test('the user typing, Remote Control, an SDK host, or an origin this build does not know is the user', () => {
  for (const kind of ['composer', 'bridge', 'sdk', 'unclassified', 'something-new']) {
    expect(isUsersOwnPrompt({ kind })).toBe(true)
  }
  expect(isUsersOwnPrompt(undefined)).toBe(true)
})

test('background work, schedules, other sessions and plugins are not', () => {
  for (const kind of ['task-notification', 'scheduled-trigger', 'peer', 'peer-send-message', 'plugin', 'auto-continuation', 'observer']) {
    expect(isUsersOwnPrompt({ kind })).toBe(false)
  }
})
