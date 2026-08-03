import test from 'node:test'
import assert from 'node:assert/strict'

import {
  beginMenuPress,
  cancelMenuPress,
  consumeMenuClick,
  finishMenuPress,
} from '../../Drawer/menuPointerOwnership.js'

const empty = () => ({ press: null, clickAction: null })
const actionA = {}
const actionB = {}

test('the opener release has no menu-owned press and cannot activate an action', () => {
  const outcome = consumeMenuClick(empty(), { detail: 1, action: actionA })
  assert.equal(outcome.allowed, false)
})

test('one complete primary press authorizes exactly its own action click', () => {
  let owner = beginMenuPress(empty(), {
    pointerId: 7, action: actionA, isPrimary: true,
  })
  owner = finishMenuPress(owner, { pointerId: 7, action: actionA })
  const first = consumeMenuClick(owner, { detail: 1, action: actionA })
  assert.equal(first.allowed, true)
  assert.equal(consumeMenuClick(first.owner, {
    detail: 1, action: actionA,
  }).allowed, false)
})

test('drag-out, cancellation, and a second pointer cannot authorize a click', () => {
  let owner = beginMenuPress(empty(), {
    pointerId: 7, action: actionA, isPrimary: true,
  })
  owner = beginMenuPress(owner, {
    pointerId: 8, action: actionB, isPrimary: false,
  })
  owner = finishMenuPress(owner, { pointerId: 7, action: actionB })
  assert.equal(consumeMenuClick(owner, { detail: 1, action: actionA }).allowed, false)

  owner = beginMenuPress(empty(), {
    pointerId: 9, action: actionA, isPrimary: true,
  })
  owner = cancelMenuPress(owner, 9)
  assert.equal(consumeMenuClick(owner, { detail: 1, action: actionA }).allowed, false)
})

test('keyboard and assistive activation remain available without a pointer', () => {
  assert.equal(consumeMenuClick(empty(), {
    detail: 0, action: actionA,
  }).allowed, true)
})
