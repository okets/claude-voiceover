/**
 * Whose prompt is it? Only the user's own prompt may cut narration off
 * mid-sentence. Prompts the engine stamps as automated - a background
 * task's notification, a schedule, another session, a plugin - never do.
 * Anything else, including an origin this build does not know, counts as
 * the user's: missing a cutover the user asked for costs more than one
 * that was not needed.
 */
const AUTOMATED = new Set([
  'task-notification',
  'scheduled-trigger',
  'peer',
  'peer-send-message',
  'projects-relay',
  'channel',
  'coordinator',
  'observer',
  'observer-activity',
  'auto-continuation',
  'plugin',
])

export function isUsersOwnPrompt(origin: { kind: string } | undefined): boolean {
  return origin === undefined || !AUTOMATED.has(origin.kind)
}
