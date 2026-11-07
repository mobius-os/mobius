import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const clock = await vite.ssrLoadModule('/src/components/ChatView/countdownClock.js')

test('wait countdowns share one quarter-minute clock', () => {
  assert.equal(clock.COUNTDOWN_TICK_MS, 15_000)
  // Server rendering reads the shared time without starting a timer.
  assert.equal(typeof clock.useCountdownNow, 'function')
  assert.equal(clock._countdownListenerCount(), 0)
})
