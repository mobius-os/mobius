import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Provider-limit parking (design §2.4): a limit-killed turn persists an error
// block carrying `parked_until` + `park_reason`, which renders as a live
// "Rate limit — resets at … · Resume now" card. The extras must survive all
// three client seams: the live stream reducer, promote-to-block, and the
// MsgContent renderer.
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const promotion = readFileSync(new URL('../streamPromotion.js', import.meta.url), 'utf8')
const stream = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('MsgContent renders a parked card for a parked_until error block', () => {
  assert.match(msgContent, /block\.parked_until/,
    'the error branch must key the parked card on block.parked_until')
  assert.match(msgContent, /Rate limit/,
    'a parked block must be labeled as a rate limit, not a generic error')
  assert.match(msgContent, /Resets at \{resetLabel\}/,
    'the card must show the reset time')
  assert.match(msgContent, /\{parked \? 'Resume now' : 'Resume'\}/,
    'the tail resume button reads "Resume now" on a parked card')
})

test('MsgContent formats parked_until as viewer-local time, defensively', () => {
  assert.match(msgContent, /function formatResetTime/,
    'a formatter must exist')
  assert.match(msgContent, /Number\.isNaN\(d\.getTime\(\)\)/,
    'an unparseable timestamp must degrade (no crash, no garbage label)')
  assert.match(msgContent, /toLocaleTimeString/,
    'the reset renders in the viewer\'s local clock')
})

test('streamItemToBlock carries the park extras through promote', () => {
  assert.match(
    promotion,
    /item\.parked_until \? \{ parked_until: item\.parked_until \}/,
    'promote must not strip parked_until — the card would vanish on promote',
  )
  assert.match(
    promotion,
    /item\.park_reason \? \{ park_reason: item\.park_reason \}/,
    'promote must carry park_reason',
  )
  assert.match(
    promotion,
    /item\.resumable \? \{ resumable: true \}/,
    'promote must carry resumable (the one-tap Resume gate)',
  )
})

test('the live stream reducer carries the park extras', () => {
  assert.match(
    stream,
    /event\.parked_until \? \{ parked_until: event\.parked_until \}/,
    'a live limit error must render as the parked card before promote too',
  )
  assert.match(
    stream,
    /event\.resumable \? \{ resumable: true \}/,
    'a live stalled/paused note must carry resumable',
  )
})

test('the parked card has styling distinct from a plain error', () => {
  assert.match(css, /\.chat__text--parked\s*\{/,
    'a .chat__text--parked style must exist (wait state, not failure)')
  assert.match(css, /\.chat__parked-reset\s*\{/,
    'the reset line has its own style')
})
