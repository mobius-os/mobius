import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Hard constraint 1 — no auto open/close, ever: the user's tap is the ONLY thing
// that opens or closes an activity stretch. A force-open version once flapped the
// card at every tool boundary and displaced the reader's scroll (see the header
// comment in ActivityStretch.jsx). This is a source-scan guard, in the style of
// toolOutputLazy.test.js: it reads the component and asserts the open state can
// only come from the user, so a future edit that re-introduces derived-open trips
// a red test rather than a scroll-displacement bug in production.

const src = readFileSync(new URL('../ActivityStretch.jsx', import.meta.url), 'utf8')

// Scan the function body only — the header comment deliberately QUOTES the old
// `open = running || userOpen` force-open expression to document why it was
// removed, and that history must not trip the code-level guard below.
const body = src.slice(src.indexOf('function GroupedActivityStretch'))

test('the stretch restores user intent and reveals only layout-ready detail', () => {
  assert.match(body, /const \[userOpen, setUserOpen\] = useDisclosureState\(/,
    'userOpen restores only the user-authored per-chat state')
  assert.match(
    body,
    /const detailReady = !detailRef \|\| detailEntries !== null \|\| detailError\s*const open = userOpen && detailReady/,
    'saved intent may wait for historical detail, but readiness can never open a row by itself',
  )
  // No `open = running || userOpen` / `userOpen || live` style force-open.
  assert.doesNotMatch(
    body,
    /const open\s*=\s*[^\n]*(?:running|live)|const open\s*=\s*[^\n]*\|\|/,
    'the rendered open state does not OR user intent with a liveness flag',
  )
  assert.doesNotMatch(body, /defaultOpen/, 'no defaultOpen escape hatch')
})

test('the summary is the only open-state write and preserves position', () => {
  assert.equal((src.match(/setUserOpen\(/g) || []).length, 1,
    'setUserOpen is called from exactly one place')
  assert.match(
    body,
    /const nextOpen = !userOpen[\s\S]*if \(detailReady\) \{[\s\S]*preserveTogglePosition\(headerRef\.current, timelineRef\.current\)[\s\S]*\} else if \(open\) \{[\s\S]*preserveTogglePosition\(headerRef\.current, timelineRef\.current\)[\s\S]*setUserOpen\(nextOpen\)/,
    'the summary preserves its anchor at the actual visible open/close boundary',
  )
  assert.doesNotMatch(src, /setHelperOpen|toggleHelper/,
    'helper status rows do not create a second disclosure state')
})

test('interaction prepares detail without deriving or writing user intent', () => {
  assert.match(src, /useEffect/,
    'historical activity detail is fetched only for the interacted row')
  assert.match(
    body,
    /!detailRequested\s*\|\| !detailRef\s*\|\| detailEntries\s*\|\| detailError/,
    'lazy detail stays network-free until pointer or keyboard activation requests it',
  )
  assert.match(body, /onPrepare=\{\(\) => setDetailRequested\(true\)\}/)
  assert.doesNotMatch(
    body.slice(0, body.indexOf('onToggle={() =>')),
    /setUserOpen\(/,
    'loading/reset effects never write the disclosure state',
  )
})

test('cold detail settles before the hidden boundary flips', () => {
  assert.match(
    body,
    /revealBeforeReady\(\)\s*setDetailEntries\(/,
    'successful detail prepares scroll preservation immediately before readiness',
  )
  assert.match(
    body,
    /revealBeforeReady\(\)\s*setDetailError\(true\)/,
    'a terminal load error is also revealed as one final layout',
  )
  assert.doesNotMatch(body, /Loading activity…/,
    'the first painted open state must not be a temporary one-line body')
})
