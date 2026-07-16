import { test } from 'node:test'
import assert from 'node:assert/strict'

import { anchorHeldThroughToggle } from '../chatContract.js'

// The disclosure seam (thinking/tool/activity collapse) is where the chat UI
// regressed repeatedly: a bounce (anchor top moved across the toggle) or spacer
// over-reservation (rapid toggles stacked body heights). This predicate is the
// machine-checkable law for that interaction; these tests pin its behavior so a
// browser monitor or Playwright spec can trust it.

test('a held anchor with no spacer growth passes', () => {
  const r = anchorHeldThroughToggle(
    { anchorTop: 120, spacerH: 200 },
    { anchorTop: 121, spacerH: 200 },
    { bodyH: 0 })
  assert.equal(r.ok, true)
})

test('an anchor that bounced past tolerance fails', () => {
  const r = anchorHeldThroughToggle(
    { anchorTop: 120, spacerH: 200 },
    { anchorTop: 140, spacerH: 200 })
  assert.equal(r.ok, false)
  assert.equal(r.measured.drift, 20)
})

test('a collapse may grow the spacer by exactly one body height', () => {
  const held = anchorHeldThroughToggle(
    { anchorTop: 500, spacerH: 100 },
    { anchorTop: 500, spacerH: 148 },
    { bodyH: 48 })
  assert.equal(held.ok, true)
})

test('a spacer that stacked two body heights (rapid-toggle regression) fails', () => {
  const stacked = anchorHeldThroughToggle(
    { anchorTop: 500, spacerH: 100 },
    { anchorTop: 500, spacerH: 196 }, // baseline 100 + 48 + 48
    { bodyH: 48 })
  assert.equal(stacked.ok, false)
})

test('missing anchor data is indeterminate, never a false pass', () => {
  const r = anchorHeldThroughToggle({}, {})
  assert.equal(r.ok, false)
  assert.match(r.reason, /anchorTop/)
})

test('an explicit spacerBaseline overrides before.spacerH', () => {
  // A caller that primed the spacer before measuring `before` passes the true
  // pre-toggle baseline so the accumulation check is against the right value.
  const r = anchorHeldThroughToggle(
    { anchorTop: 300, spacerH: 250 }, // already primed
    { anchorTop: 300, spacerH: 250 },
    { bodyH: 48, spacerBaseline: 202 })
  assert.equal(r.ok, true)
})
