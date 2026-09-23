import assert from 'node:assert/strict'
import test from 'node:test'
import {
  managedAppEventForShellEvent,
  managedAppFrameMessage,
} from '../managedAppEvents.js'

test('app lifecycle completion reaches reviewed manager apps only', () => {
  const event = managedAppEventForShellEvent(
    { type: 'app_updated', appId: 42 }, 8,
  )
  assert.deepEqual(event, {
    type: 'app_updated', appId: '42', sequence: 9,
  })
  assert.deepEqual(
    managedAppFrameMessage(event, { data: { manage_apps: true } }),
    {
      type: 'moebius:managed-app-event',
      event: { type: 'app_updated', appId: '42', sequence: 9 },
    },
  )
  assert.equal(
    managedAppFrameMessage(event, { data: { manage_apps: false } }), null,
  )
  assert.equal(
    managedAppFrameMessage({ type: 'chat_run_finished' }, {
      data: { manage_apps: true },
    }),
    null,
  )
  assert.equal(
    managedAppEventForShellEvent({ type: 'theme_updated' }, 9), null,
  )
})
