import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

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
// reserved EXACTLY enough to reach it — no extra cushion (PIN_BOTTOM_ROOM 0).
// fullViewH == clientHeight == 700, listHeight 1040, so spacer =
// max(0, 700 + 996 - 1040) = 656 and scrollHeight = 1040 + 656 = 1696.
// maxScrollTop = 1696 - 700 = 996 == pinTarget: the pin rests flush at the
// scroll ceiling, with no reservable blank below.
const pinnedEnv = {
  scrollEl: scrollEl({ scrollTop: 996, scrollHeight: 1696, clientHeight: 700 }),
  listEl: listEl(1040),
  lastUserMsgEl: userEl(1000),
  fullViewH: 700,
}

test('pin registry summary matches owner-authoritative R2 geometry', () => {
  const rule = CHAT_CONTRACT.find(entry => entry.id === 'pin-on-send')
  assert.ok(rule)
  assert.match(rule.summary, /DOM snapshot.*real-content tail/)
  assert.doesNotMatch(rule.summary, /gesture-entered auto-scroll/)
})

test('ChatView only consumes methods returned by the scroll controller', () => {
  const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const scrollController = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')

  const useEnd = chatView.indexOf('} = useScrollMode({')
  const useStart = chatView.lastIndexOf('const {', useEnd)
  assert.ok(useStart >= 0 && useEnd > useStart, 'ChatView useScrollMode destructure exists')

  const returnStart = scrollController.lastIndexOf('\n  return {')
  const returnEnd = scrollController.indexOf('\n  }', returnStart)
  assert.ok(returnStart >= 0 && returnEnd > returnStart,
    'useScrollMode has a final returned controller object')

  const identifiers = source => [...source.matchAll(/^\s*([A-Za-z_$][\w$]*)\s*,?\s*$/gm)]
    .map(match => match[1])
  const consumed = identifiers(chatView.slice(useStart + 'const {'.length, useEnd))
  const returned = new Set(identifiers(scrollController.slice(returnStart, returnEnd)))
  const missing = consumed.filter(name => !returned.has(name))

  assert.deepEqual(missing, [],
    `ChatView consumes missing useScrollMode members: ${missing.join(', ')}`)
})

test('a retained chat crosses the old unmount lifecycle while hidden', () => {
  const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  assert.match(
    chatView,
    /useLayoutEffect\(\(\) => \{\s*if \(hidden\) freezeChatExit\(\)/,
    'hiding a retained chat must freeze its reader position before Settings paints',
  )
  assert.match(
    chatView,
    /if \(hidden\) return[\s\S]*?\}, \[chatId, loadNonce, hidden\]\)/,
    'hidden chats must disconnect and refresh history when they become visible again',
  )
})

test('snapshotChatUX derives the geometry fields from a clean pinned frame', () => {
  const s = snapshotChatUX(pinnedEnv)
  assert.equal(s.scrollTop, 996)
  assert.equal(s.scrollHeight, 1696)
  assert.equal(s.clientHeight, 700)
  assert.equal(s.listHeight, 1040)
  assert.equal(s.lastUserTop, 1000)
  assert.equal(s.pinGap, 4) // lastUserTop - scrollTop == PIN_OFFSET
  assert.equal(s.distanceToBottom, 0) // pin rests flush at the scroll ceiling
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

test('pinHeld fails when the row holds steady but was never at the top', () => {
  // Zero drift is not enough: a row parked 400px down with a rock-steady gap
  // violates "pinned row holds at the top" just as much as a drifting one.
  const mid = { ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 600, scrollHeight: 1876, clientHeight: 700 }) }
  const r = pinHeld(snapshotChatUX(mid), snapshotChatUX(mid))
  assert.equal(r.ok, false)
  assert.equal(r.measured.drift, 0)
  assert.equal(r.measured.after, 400)
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

test('reanchored fails when the row is stable across promote but off the top', () => {
  const mid = { ...pinnedEnv, scrollEl: scrollEl({ scrollTop: 700, scrollHeight: 1876, clientHeight: 700 }) }
  const r = reanchored(snapshotChatUX(mid), snapshotChatUX(mid))
  assert.equal(r.ok, false) // drift 0, but gap 300 is nowhere near the pin
  assert.equal(r.measured.drift, 0)
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

test('cushionPresent passes when the spacer reaches the pin (cushion 0)', () => {
  const r = cushionPresent(snapshotChatUX(pinnedEnv))
  assert.equal(r.ok, true)
  assert.equal(r.id, 'spacer-reserves-room')
  assert.equal(r.measured, PIN_BOTTOM_ROOM) // exactly 0: pin exactly reachable
})

test('cushionPresent fails on the R5 bug: an undersized spacer leaves no keyboard-closed room', () => {
  // The hook's stale-small fullViewH (400, the keyboard-open height) sized the
  // spacer: max(0, 400 + 996 - 1040) = 356, so scrollHeight = 1396. The
  // SNAPSHOT carries the true full view height (700 — clientHeight has grown
  // back), so the cushion reads (1396 - 700) - 996 = -300: pin stranded.
  const buggy = {
    scrollEl: scrollEl({ scrollTop: 696, scrollHeight: 1396, clientHeight: 700 }),
    listEl: listEl(1040),
    lastUserMsgEl: userEl(1000),
    fullViewH: 700,
  }
  const snap = snapshotChatUX(buggy)
  assert.equal(snap.spacerReachable, false) // pin target unreachable
  const r = cushionPresent(snap)
  assert.equal(r.ok, false)
  assert.ok(r.measured < PIN_BOTTOM_ROOM)
})

test('cushionPresent fails on a keyboard-open snapshot with an undersized spacer', () => {
  // Keyboard open: clientHeight shrank to 400; the full viewport is 700 and
  // the spacer only produced scrollHeight 1396. clientHeight-based math would
  // read (1396 - 400) - 996 = 0 — a false green. Keyboard-closed terms
  // (fullViewH) read (1396 - 700) - 996 = -300: no room once the keyboard
  // closes.
  const snap = snapshotChatUX({
    scrollEl: scrollEl({ scrollTop: 696, scrollHeight: 1396, clientHeight: 400 }),
    listEl: listEl(1040),
    lastUserMsgEl: userEl(1000),
    fullViewH: 700,
  })
  const r = cushionPresent(snap)
  assert.equal(r.ok, false)
  assert.equal(r.measured, -300)
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

test('checkContract fails closed when no checks are supplied (non-array)', () => {
  // Missing evidence — failed injection, skipped monitor setup — must never
  // read as green.
  const r = checkContract(undefined)
  assert.equal(r.ok, false)
  assert.equal(r.violations.length, 1)
  assert.equal(r.violations[0].id, 'contract-no-evidence')
  assert.equal(r.violations[0].measured, null)
  assert.match(r.violations[0].reason, /no checks/)
})

test('checkContract fails closed on an empty check list', () => {
  const r = checkContract([])
  assert.equal(r.ok, false)
  assert.equal(r.violations[0].id, 'contract-no-evidence')
})

test('mirrored constants stay in sync with useScrollMode.js (sync obligation)', () => {
  // Read as TEXT, never import — importing useScrollMode.js would drag React
  // and its module-load sessionStorage read into this suite.
  const src = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
  assert.ok(
    src.includes(`PIN_OFFSET = ${PIN_OFFSET}`),
    `useScrollMode.js no longer declares PIN_OFFSET = ${PIN_OFFSET}. The value `
    + 'is mirrored in chatContract.js (see its header CONSTANTS-SYNC note) — '
    + 'update BOTH files together.',
  )
  assert.ok(
    src.includes(`PIN_BOTTOM_ROOM = ${PIN_BOTTOM_ROOM}`),
    `useScrollMode.js no longer declares PIN_BOTTOM_ROOM = ${PIN_BOTTOM_ROOM}. `
    + 'The value is mirrored in chatContract.js (see its header CONSTANTS-SYNC '
    + 'note) — update BOTH files together.',
  )
})

test('the transcript reveal safety deadline cannot be reset by message churn', () => {
  const src = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
  const deadline = src.indexOf('Absolute reveal deadline for this mounted chat')
  const messagesEffect = src.indexOf('Single layout effect: spacer sizing')
  assert.ok(deadline >= 0 && deadline < messagesEffect,
    'the safety deadline is owned outside the messages-dependent layout effect')
  assert.match(src.slice(deadline, messagesEffect), /\}, \[chatId\]\)/,
    'the safety deadline resets only when the mounted chat changes')
  assert.doesNotMatch(src.slice(messagesEffect), /clearTimeout\(safetyReveal\)/,
    'message/layout cleanup cannot cancel and restart the absolute deadline')
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
