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

test('the stretch restores saved user state and open is exactly userOpen', () => {
  assert.match(body, /const \[userOpen, setUserOpen\] = useDisclosureState\(/,
    'userOpen restores only the user-authored per-chat state')
  assert.match(body, /\n\s*const open = userOpen\n/,
    'open derives from userOpen alone — no force-open expression')
  // No `open = running || userOpen` / `userOpen || live` style force-open.
  assert.doesNotMatch(body, /\|\|\s*userOpen/, 'nothing ORs into userOpen to force it open')
  assert.doesNotMatch(body, /userOpen\s*\|\|/, 'userOpen does not OR with a liveness flag')
  assert.doesNotMatch(body, /defaultOpen/, 'no defaultOpen escape hatch')
})

test('the only open-state write is the user toggle, guarded by preserveTogglePosition', () => {
  // Exactly one setter call site: the header onClick.
  assert.equal((src.match(/setUserOpen\(/g) || []).length, 1,
    'setUserOpen is called from exactly one place')
  // preserveTogglePosition runs BEFORE the state mutation on every toggle path —
  // the scroll anchor is captured before the height changes.
  assert.match(src, /preserveTogglePosition\(headerRef\.current, timelineRef\.current\)\s*setUserOpen\(o => !o\)/,
    'the toggle preserves the anchor before flipping open state')
})

test('no effect derives open state from running/live/props', () => {
  // Since the 1Hz thinking ticker was retired (bare "Thinking" has no clock),
  // the component has NO effects at all — the strongest possible form of the
  // no-auto-open guarantee: nothing can flip `open` outside the user's tap.
  assert.doesNotMatch(src, /useEffect/,
    'ActivityStretch has no effects; open state can only change in the tap handler')
})
