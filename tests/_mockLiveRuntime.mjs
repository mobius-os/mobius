import { runtimeSnapshot, testChatAgentSettings } from './_chatTestPrerequisites.mjs'

/**
 * Model the backend's live run and queue for a spec that stubs /messages and
 * holds its stream. The runtime poll and the chat-detail read are then the
 * authority on whether a turn is running; unmocked, they reach the real
 * backend (which never saw the stubbed POSTs), report idle, and the app
 * correctly retires the turn or hydrates the queue away.
 *
 * Mutate the returned state as the mocked backend would: set `running` once
 * the first message starts a turn (an unconditional true locks the composer
 * before any send), and keep `pending` in step with each queue and steer step.
 * The detail read is intercepted only while running unless `idleDetail`, so
 * bootstrap reads keep the real chat shape.
 */
export async function mockLiveRuntime(page, { idleDetail = false } = {}) {
  const live = { running: false, pending: [] }
  const snapshot = () => runtimeSnapshot({ running: live.running, pending_messages: live.pending })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/runtime$/, route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({ status: 200, contentType: 'application/json', json: snapshot() })
  })
  await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=/, route => {
    if (route.request().method() !== 'GET' || (!live.running && !idleDetail)) {
      return route.continue()
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      json: { messages: [], total: 0, offset: 0, ...snapshot(), ...testChatAgentSettings() },
    })
  })
  return live
}
