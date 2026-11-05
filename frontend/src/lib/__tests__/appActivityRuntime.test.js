import assert from 'node:assert/strict'
import { test } from 'node:test'

import { makeActivity } from '../../runtime/activity.js'
import { publishAppActivity, subscribeAppActivity } from '../appActivitySignal.js'

function fakeFrame() {
  const parent = { name: 'shell' }
  const listeners = []
  const win = {
    parent,
    addEventListener(type, cb) { if (type === 'message') listeners.push(cb) },
  }
  return {
    win,
    parent,
    post(data, source = parent) { for (const cb of listeners) cb({ source, data }) },
  }
}

test('an app hears each new-activity nudge from its shell', () => {
  const frame = fakeFrame()
  const activity = makeActivity(frame)
  let heard = 0
  const stop = activity.onActivity(() => { heard += 1 })
  frame.post({ type: 'moebius:app-activity' })
  frame.post({ type: 'moebius:app-activity' })
  assert.equal(heard, 2)
  stop()
  frame.post({ type: 'moebius:app-activity' })
  assert.equal(heard, 2)
})

test('only the hosting shell can nudge, and other messages are ignored', () => {
  const frame = fakeFrame()
  const activity = makeActivity(frame)
  let heard = 0
  activity.onActivity(() => { heard += 1 })
  frame.post({ type: 'moebius:app-activity' }, { name: 'another frame' })
  frame.post({ type: 'moebius:frame-visibility', visible: true })
  frame.post(null)
  assert.equal(heard, 0)
})

test('a failing listener does not stop the others', () => {
  const frame = fakeFrame()
  const activity = makeActivity(frame)
  let heard = 0
  activity.onActivity(() => { throw new Error('app bug') })
  activity.onActivity(() => { heard += 1 })
  frame.post({ type: 'moebius:app-activity' })
  assert.equal(heard, 1)
})

test('the shell signal reaches only the canvases of the app it names', () => {
  const heard = []
  const stopSocial = subscribeAppActivity(9, () => heard.push('social'))
  const stopKanban = subscribeAppActivity('7', () => heard.push('kanban'))
  publishAppActivity('9')
  publishAppActivity(7)
  publishAppActivity(42) // no mounted canvas: dropped
  assert.deepEqual(heard, ['social', 'kanban'])
  stopSocial()
  publishAppActivity(9)
  assert.deepEqual(heard, ['social', 'kanban'])
  stopKanban()
})
