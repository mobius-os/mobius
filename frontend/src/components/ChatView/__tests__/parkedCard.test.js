import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Provider-limit parking (design §2.4): a limit-killed turn persists an error
// block carrying `parked_until` + `park_reason`, which renders as a live
// "Rate limit — resets at … · Resume now" card. The extras must survive all
// three client seams: the live stream reducer, promote-to-block, and the
// shared ErrorCard renderer (consumed by BOTH MsgContent and
// StreamingMessage, so the persisted and live surfaces cannot diverge).
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const streamingMessage = readFileSync(new URL('../StreamingMessage.jsx', import.meta.url), 'utf8')
const errorCard = readFileSync(new URL('../ErrorCard.jsx', import.meta.url), 'utf8')
const resetTime = readFileSync(new URL('../resetTime.js', import.meta.url), 'utf8')
const promotion = readFileSync(new URL('../streamPromotion.js', import.meta.url), 'utf8')
const stream = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('ErrorCard renders a parked card for a parked_until error block', () => {
  assert.match(errorCard, /block\.parked_until/,
    'the card must key the parked classification on block.parked_until')
  assert.match(errorCard, /Rate limit/,
    'a parked block must be labeled as a rate limit, not a generic error')
  assert.match(errorCard, /Resets \{vm\.resetLabel\}/,
    'the card must show the reset time (label carries its own preposition)')
  assert.match(msgContent, /\{parked \? 'Resume now' : 'Resume'\}/,
    'the tail resume button reads "Resume now" on a parked card')
})

test('both surfaces consume the shared ErrorCard — no private error render', () => {
  // The live/catch-up surface once hardcoded a red "Error" card, so a benign
  // pause flashed red until promotion. One renderer is the invariant.
  assert.match(msgContent, /import ErrorCard from '\.\/ErrorCard\.jsx'/,
    'MsgContent must consume the shared ErrorCard')
  assert.match(streamingMessage, /import ErrorCard from '\.\/ErrorCard\.jsx'/,
    'StreamingMessage must consume the shared ErrorCard')
  assert.doesNotMatch(streamingMessage, /chat__error-label/,
    'the live surface must not hand-roll its own error card body')
  assert.doesNotMatch(msgContent, /chat__error-label/,
    'the persisted surface must not hand-roll its own error card body')
})

test('the reset formatter is a defensive, viewer-local, day-aware helper', () => {
  assert.match(resetTime, /export function formatResetTime/,
    'formatResetTime must be an exported pure helper (shared by the SR status)')
  assert.match(resetTime, /Number\.isNaN\(d\.getTime\(\)\)/,
    'an unparseable timestamp must degrade (no crash, no garbage label)')
  assert.match(resetTime, /toLocaleTimeString/,
    'the reset renders in the viewer\'s local clock')
  assert.match(resetTime, /tomorrow at/,
    'the label is day-aware — a 7-day park must not read as a bare time')
  assert.match(errorCard,
    /import \{ formatResetTime \} from '\.\/resetTime\.js'/,
    'ErrorCard must consume the shared formatter, not a private copy')
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

test('a benign pause_kind renders the calm "Paused" family, not red Error', () => {
  // A drain-restart or stall carries pause_kind; it must get the soft
  // .chat__text--parked treatment and a "Paused" label. Red "Error" is
  // reserved for genuine failures (no parked_until, no pause_kind).
  assert.match(errorCard, /block\.pause_kind/,
    'the card must read block.pause_kind')
  assert.match(errorCard, /parked \|\| !!block\.pause_kind/,
    'a park OR a pause_kind gets the soft treatment')
  assert.match(errorCard, /block\.pause_kind \? 'Paused' : 'Error'/,
    'a benign pause reads "Paused"; only genuine failures read "Error"')
})

test('the park card reassures that a reset push is coming', () => {
  assert.match(errorCard, /chat__parked-note/,
    'a muted reassurance line renders inside the parked branch')
  assert.match(errorCard, /notification when it resets/,
    'the note names the incoming reset push')
  assert.match(css, /\.chat__parked-note\s*\{/,
    'the reassurance line has its own muted style')
})

test('pause_kind rides both stream seams onto the block', () => {
  assert.match(promotion,
    /item\.pause_kind \? \{ pause_kind: item\.pause_kind \}/,
    'promote must carry pause_kind so a promoted note stays calm')
  assert.match(stream,
    /event\.pause_kind \? \{ pause_kind: event\.pause_kind \}/,
    'the live reducer must carry pause_kind')
})
