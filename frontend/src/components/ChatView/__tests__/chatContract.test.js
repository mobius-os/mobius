import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  CHAT_CONTRACT,
  PIN_OFFSET,
  PIN_BOTTOM_ROOM,
  snapshotChatUX,
  pinLanded,
  pinHeld,
  reanchored,
  scrollUnmoved,
  cushionPresent,
  singleAssistantSurface,
  checkContract,
} from '../chatContract.js'

// Fake geometry: plain objects exposing the same numeric fields the real
// elements do. The pure module can't tell them apart from DOM nodes.
const scrollEl = ({ scrollTop, scrollHeight, clientHeight }) =>
  ({ scrollTop, scrollHeight, clientHeight })
const listEl = offsetHeight => ({ offsetHeight })
const userEl = offsetTop => ({ offsetTop })

// A clean pinned frame: pin target 996, scrollTop sits there, and the spacer
// reserved exactly PIN_BOTTOM_ROOM below it (fullViewH == clientHeight == 700,
// listHeight 1040, so scrollHeight = 1040 + spacer(836) = 1876).
const pinnedEnv = {
  scrollEl: scrollEl({ scrollTop: 996, scrollHeight: 1876, clientHeight: 700 }),
  listEl: listEl(1040),
  lastUserMsgEl: userEl(1000),
  fullViewH: 700,
}

test('snapshotChatUX derives the geometry fields from a clean pinned frame', () => {
  const s = snapshotChatUX(pinnedEnv)
  assert.equal(s.scrollTop, 996)
  assert.equal(s.scrollHeight, 1876)
  assert.equal(s.clientHeight, 700)
  assert.equal(s.listHeight, 1040)
  assert.equal(s.lastUserTop, 1000)
  assert.equal(s.pinGap, 4) // lastUserTop - scrollTop == PIN_OFFSET
  assert.equal(s.distanceToBottom, 180) // reserved cushion still below
  assert.equal(s.spacerReachable, true)
  assert.equal(s.fullViewH, 700)
})

test('snapshotChatUX is null-tolerant: missing user message yields nulls, no throw', () => {
  const s = snapshotChatUX({
    scrollEl: scrollEl({ scrollTop: 100, scrollHeight: 1200, clientHeight: 700 }),
    listEl: listEl(1040),
    lastUserMsgEl: null,
    fullViewH: 700,
  })
  assert.equal(s.lastUserTop, null)
  assert.equal(s.pinGap, null)
  assert.equal(s.spacerReachable, null)
  // Scroll-only fields still resolve.
  assert.equal(s.scrollTop, 100)
  assert.equal(s.distanceToBottom, 400)
})

test('snapshotChatUX is null-tolerant: missing scroll element yields all-scroll nulls', () => {
  const s = snapshotChatUX({ scrollEl: null, listEl: listEl(1040), lastUserMsgEl: userEl(1000) })
  assert.equal(s.scrollTop, null)
  assert.equal(s.scrollHeight, null)
  assert.equal(s.clientHeight, null)
  assert.equal(s.pinGap, null)
  assert.equal(s.distanceToBottom, null)
  assert.equal(s.spacerReachable, null)
  assert.equal(s.listHeight, 1040) // listEl independent of scrollEl
})

test('snapshotChatUX does not throw on a fully empty env', () => {
  assert.doesNotThrow(() => snapshotChatUX({}))
  assert.doesNotThrow(() => snapshotChatUX(undefined))
})

test('pinLanded passes when the message is flush at the top', () => {
  const r = pinLanded(snapshotChatUX(pinnedEnv))
  assert.equal(r.ok, true)
  assert.equal(r.id, 'pin-on-send')
  assert.equal(r.measured, 4)
})

test('pinLanded fails when the message landed mid-viewport', () => {
  const midEnv = { ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 600, scrollHeight: 1876, clientHeight: 700 }) }
  const r = pinLanded(snapshotChatUX(midEnv))
  assert.equal(r.ok, false)
  assert.equal(r.measured, 400) // 1000 - 600
})

test('pinLanded reports indeterminate (ok:false + reason) on a null snapshot, never throws', () => {
  const r = pinLanded(snapshotChatUX({ ...pinnedEnv, lastUserMsgEl: null }))
  assert.equal(r.ok, false)
  assert.equal(r.measured, null)
  assert.match(r.reason, /pinGap/)
})

test('pinHeld passes when the pin does not drift as content grows', () => {
  const before = snapshotChatUX(pinnedEnv)
  // Content streamed below: scrollHeight grew, scrollTop unchanged, gap steady.
  const after = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 996, scrollHeight: 2400, clientHeight: 700 }) })
  const r = pinHeld(before, after)
  assert.equal(r.ok, true)
  assert.equal(r.id, 'pin-holds-streaming')
  assert.equal(r.measured.drift, 0)
})

test('pinHeld fails when the pin drifts off the top (offsetTop shift not re-pinned)', () => {
  const before = snapshotChatUX(pinnedEnv)
  // Content grew ABOVE the message: lastUserTop shifted 1000 -> 1500, view
  // did not follow, so the message drifts 500px down the viewport.
  const after = snapshotChatUX({ ...pinnedEnv, lastUserMsgEl: userEl(1500) })
  const r = pinHeld(before, after)
  assert.equal(r.ok, false)
  assert.equal(r.measured.drift, 500)
})

test('pinHeld is indeterminate when a snapshot lacks a pinGap', () => {
  const before = snapshotChatUX(pinnedEnv)
  const after = snapshotChatUX({ ...pinnedEnv, lastUserMsgEl: null })
  const r = pinHeld(before, after)
  assert.equal(r.ok, false)
  assert.match(r.reason, /pinGap/)
})

test('reanchored passes when promote preserves the pinned gap', () => {
  const before = snapshotChatUX(pinnedEnv)
  const after = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 999, scrollHeight: 1876, clientHeight: 700 }) })
  const r = reanchored(before, after)
  assert.equal(r.ok, true) // gap moved 4 -> 1, within tolerance
  assert.equal(r.id, 'reanchor-on-promote')
})

test('reanchored fails when promote lets the message jump', () => {
  const before = snapshotChatUX(pinnedEnv)
  const after = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 900, scrollHeight: 1876, clientHeight: 700 }) })
  const r = reanchored(before, after)
  assert.equal(r.ok, false) // gap jumped 4 -> 100
})

test('scrollUnmoved passes when a scrolled-up send leaves scrollTop put', () => {
  const before = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 500, scrollHeight: 1876, clientHeight: 700 }) })
  const after = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 503, scrollHeight: 1876, clientHeight: 700 }) })
  const r = scrollUnmoved(before, after)
  assert.equal(r.ok, true)
  assert.equal(r.id, 'no-scroll-on-read-send')
})

test('scrollUnmoved fails when a scrolled-up send yanks the reader', () => {
  const before = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 500, scrollHeight: 1876, clientHeight: 700 }) })
  const after = snapshotChatUX({ ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 996, scrollHeight: 1876, clientHeight: 700 }) })
  const r = scrollUnmoved(before, after)
  assert.equal(r.ok, false)
  assert.equal(r.measured.drift, 496)
})

test('scrollUnmoved is indeterminate when scrollTop is missing', () => {
  const r = scrollUnmoved(snapshotChatUX({}), snapshotChatUX(pinnedEnv))
  assert.equal(r.ok, false)
  assert.match(r.reason, /scrollTop/)
})

test('cushionPresent passes when the spacer reserves PIN_BOTTOM_ROOM below the pin', () => {
  const r = cushionPresent(snapshotChatUX(pinnedEnv))
  assert.equal(r.ok, true)
  assert.equal(r.id, 'spacer-reserves-room')
  assert.equal(r.measured, PIN_BOTTOM_ROOM) // exactly 180 reserved
})

test('cushionPresent fails on the R5 bug: a stale-small fullViewH undersizes the spacer', () => {
  // Stale fullViewH 400 (keyboard-open) after clientHeight grew back to 700:
  // spacer = max(0, 400 + 996 - 1040 + 180) = 536, scrollHeight = 1040 + 536.
  const buggy = {
    scrollEl: scrollEl({ scrollTop: 876, scrollHeight: 1576, clientHeight: 700 }),
    listEl: listEl(1040),
    lastUserMsgEl: userEl(1000),
    fullViewH: 400,
  }
  const snap = snapshotChatUX(buggy)
  assert.equal(snap.spacerReachable, false) // pin target unreachable
  const r = cushionPresent(snap)
  assert.equal(r.ok, false)
  assert.ok(r.measured < PIN_BOTTOM_ROOM)
})

test('cushionPresent is indeterminate when geometry is missing', () => {
  const r = cushionPresent(snapshotChatUX({ ...pinnedEnv, lastUserMsgEl: null }))
  assert.equal(r.ok, false)
  assert.match(r.reason, /cushion/)
})

test('singleAssistantSurface passes with exactly one surface and fails with a duplicate', () => {
  assert.equal(singleAssistantSurface(1).ok, true)
  const dup = singleAssistantSurface(2)
  assert.equal(dup.ok, false)
  assert.equal(dup.measured, 2)
  assert.equal(dup.id, 'single-assistant-surface')
})

test('singleAssistantSurface is indeterminate when no count is supplied', () => {
  const r = singleAssistantSurface(undefined)
  assert.equal(r.ok, false)
  assert.match(r.reason, /count/)
})

test('checkContract aggregates a clean run to ok with no violations', () => {
  const snap = snapshotChatUX(pinnedEnv)
  const result = checkContract([
    pinLanded(snap),
    cushionPresent(snap),
    singleAssistantSurface(1),
  ])
  assert.equal(result.ok, true)
  assert.deepEqual(result.violations, [])
})

test('checkContract collects the failing checks', () => {
  const snap = snapshotChatUX(pinnedEnv)
  const result = checkContract([
    pinLanded(snap),
    singleAssistantSurface(3), // fails
    cushionPresent(snapshotChatUX({})), // indeterminate -> fails
  ])
  assert.equal(result.ok, false)
  assert.equal(result.violations.length, 2)
  assert.deepEqual(
    result.violations.map(v => v.id).sort(),
    ['single-assistant-surface', 'spacer-reserves-room'],
  )
})

test('checkContract tolerates a non-array argument', () => {
  assert.deepEqual(checkContract(undefined), { ok: true, violations: [] })
})

test('every predicate id is registered in CHAT_CONTRACT (registry is the map)', () => {
  const registered = new Set(CHAT_CONTRACT.map(c => c.id))
  for (const c of CHAT_CONTRACT) {
    assert.ok(c.id && c.title && c.summary, `entry ${c.id} is fully described`)
  }
  const emitted = [
    pinLanded(snapshotChatUX(pinnedEnv)),
    pinHeld(snapshotChatUX(pinnedEnv), snapshotChatUX(pinnedEnv)),
    reanchored(snapshotChatUX(pinnedEnv), snapshotChatUX(pinnedEnv)),
    scrollUnmoved(snapshotChatUX(pinnedEnv), snapshotChatUX(pinnedEnv)),
    cushionPresent(snapshotChatUX(pinnedEnv)),
    singleAssistantSurface(1),
  ]
  for (const r of emitted) {
    assert.ok(registered.has(r.id), `predicate id ${r.id} is in the registry`)
  }
})
