import test from 'node:test'
import assert from 'node:assert/strict'
import { createPassiveSlots, grantedSlots, PASSIVE_IDLE_CAP } from '../../components/ChatView/markdown/passiveSlots.js'

test('the nearest idle blocks of an app get frames; retained sessions always keep theirs', () => {
  const entries = [
    { id: 'far', wanted: true, retained: false, distance: 900 },
    { id: 'a', wanted: true, retained: false, distance: 10 },
    { id: 'b', wanted: true, retained: false, distance: 20 },
    { id: 'c', wanted: true, retained: false, distance: 30 },
    { id: 'owner', wanted: false, retained: true, distance: 5000 },
    { id: 'offscreen', wanted: false, retained: false, distance: 1 },
  ]
  assert.equal(PASSIVE_IDLE_CAP, 3)
  assert.deepEqual([...grantedSlots(entries)].sort(), ['a', 'b', 'c', 'owner'])
})

test('the slot store grants per app and frees a slot when a block leaves', () => {
  const slots = createPassiveSlots(1)
  let notified = 0
  slots.subscribe(() => { notified += 1 })
  const one = {}, two = {}, other = {}
  slots.update(7, one, { wanted: true, retained: false, distance: 10 })
  slots.update(7, two, { wanted: true, retained: false, distance: 20 })
  slots.update(8, other, { wanted: true, retained: false, distance: 99 })
  assert.deepEqual([slots.granted(7, one), slots.granted(7, two), slots.granted(8, other)], [true, false, true])
  slots.remove(7, one)
  assert.equal(slots.granted(7, two), true)
  assert.ok(notified >= 4)
})
