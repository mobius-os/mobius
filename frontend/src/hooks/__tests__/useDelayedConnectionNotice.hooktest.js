import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useDelayedConnectionNotice, {
  CONNECTION_NOTICE_DELAY_MS,
} from '../useDelayedConnectionNotice.js'

test('transient connection notices do not render unless the interruption persists', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const hook = renderHook(active => useDelayedConnectionNotice(active), false)

  hook.rerender(true)
  t.mock.timers.tick(CONNECTION_NOTICE_DELAY_MS - 1)
  assert.equal(hook.result.current, false)

  hook.rerender(false)
  t.mock.timers.tick(CONNECTION_NOTICE_DELAY_MS)
  assert.equal(hook.result.current, false, 'a recovered interruption cannot publish late')

  hook.rerender(true)
  t.mock.timers.tick(CONNECTION_NOTICE_DELAY_MS)
  assert.equal(hook.result.current, true)

  hook.rerender(false)
  assert.equal(hook.result.current, false, 'recovery hides the notice synchronously')
  hook.unmount()
})
