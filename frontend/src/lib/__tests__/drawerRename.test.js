import test from 'node:test'
import assert from 'node:assert/strict'

import {
  drawerNameMaxLength,
  saveDrawerRename,
} from '../../components/Drawer/drawerRename.js'

function recorder() {
  const notices = []
  return { notices, notify: (message, options) => notices.push({ message, options }) }
}

test('a rejected rename save tells the owner instead of silently reverting', async () => {
  const { notices, notify } = recorder()

  const saved = await saveDrawerRename(
    async () => new Response('{"detail":[]}', { status: 422 }),
    notify,
  )

  assert.equal(saved, false)
  assert.deepEqual(notices, [
    { message: 'Couldn’t rename that item.', options: { variant: 'error' } },
  ])
})

test('a rename that cannot reach the server tells the owner without escaping', async () => {
  const { notices, notify } = recorder()

  const saved = await saveDrawerRename(
    async () => { throw new TypeError('Failed to fetch') },
    notify,
  )

  assert.equal(saved, false)
  assert.equal(notices.length, 1)
})

test('a successful rename stays quiet, whether it returns a response or a row', async () => {
  const { notices, notify } = recorder()

  assert.equal(await saveDrawerRename(async () => new Response('{}', { status: 200 }), notify), true)
  assert.equal(await saveDrawerRename(async () => ({ id: 'p1', name: 'Renamed' }), notify), true)
  assert.deepEqual(notices, [])
})

test('the rename box allows exactly what the server accepts for each kind', () => {
  assert.equal(drawerNameMaxLength('chat'), 500)
  assert.equal(drawerNameMaxLength('app'), 500)
})
