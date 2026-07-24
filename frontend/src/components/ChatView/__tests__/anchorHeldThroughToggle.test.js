import { test } from 'node:test'
import assert from 'node:assert/strict'

import { anchorHeldThroughToggle } from '../chatContract.js'

// The disclosure seam (thinking/tool/activity collapse) is where the chat UI
// regressed repeatedly: a bounce (anchor top moved across the toggle) or blank
// room without the latest user row. This predicate is the machine-checkable
// law for an ANCHOR_AT toggle.

test('a held anchor with no spacer growth passes', () => {
  const r = anchorHeldThroughToggle(
    { anchorTop: 120, spacerH: 0 },
    { anchorTop: 121, spacerH: 0 })
  assert.equal(r.ok, true)
})

test('an anchor that bounced past tolerance fails', () => {
  const r = anchorHeldThroughToggle(
    { anchorTop: 120, spacerH: 0 },
    { anchorTop: 140, spacerH: 0 })
  assert.equal(r.ok, false)
  assert.equal(r.measured.drift, 20)
})

test('an anchored disclosure cannot leave room without the latest user visible', () => {
  const reserved = anchorHeldThroughToggle(
    { anchorTop: 500, spacerH: 0 },
    { anchorTop: 500, spacerH: 48 })
  assert.equal(reserved.ok, false)
})

test('collapse may restore reservation when the latest user remains visible', () => {
  const reserved = anchorHeldThroughToggle(
    { anchorTop: 500, spacerH: 0 },
    { anchorTop: 500, spacerH: 48, latestUserVisible: true })
  assert.equal(reserved.ok, true)
})

test('missing anchor data is indeterminate, never a false pass', () => {
  const r = anchorHeldThroughToggle({}, {})
  assert.equal(r.ok, false)
  assert.match(r.reason, /anchorTop/)
})

test('a stale pre-toggle spacer passes only when the toggle leaves none behind', () => {
  const r = anchorHeldThroughToggle(
    { anchorTop: 300, spacerH: 250 },
    { anchorTop: 300, spacerH: 0 })
  assert.equal(r.ok, true)
})
