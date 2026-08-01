import test from 'node:test'
import assert from 'node:assert/strict'
import {
  _anchorReapplyNeeded,
  _pinReapplyNeeded,
} from '../useScrollMode.js'

test('anchor repair never moves backward over an unchanged reader position', () => {
  // target = 960, but the viewport is now farther down at 1080. With an
  // unchanged row offset this is reader movement, not browser clamp damage.
  assert.equal(
    _anchorReapplyNeeded(
      {
        scrollHeight: 2000,
        scrollTop: 1080,
        clientHeight: 700,
        querySelector: selector => (
          selector === '[data-key="k-1"]' ? { offsetTop: 1000 } : null
        ),
      },
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 40 },
      1000,
    ),
    false,
  )
})

test('pin repair never moves backward over an unchanged reader position', () => {
  const row = { offsetTop: 133 }
  const scrollEl = {
    scrollHeight: 1600,
    clientHeight: 700,
    scrollTop: 221,
    querySelector: selector => (
      selector === '[data-cid="c-111"]' ? row : null
    ),
  }
  // The target is 101px. Being at 221px is a downward reader move, not damage.
  assert.equal(
    _pinReapplyNeeded(
      scrollEl,
      { kind: 'PIN_USER_MSG', cid: 'c-111' },
      row.offsetTop,
    ),
    false,
  )
})
