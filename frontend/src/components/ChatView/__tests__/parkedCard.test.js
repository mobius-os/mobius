import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Provider-limit parking (design §2.4): a limit-killed turn persists an error
// block carrying a single `pause` descriptor ({kind, resets_at?}), which
// renders as a live "Rate limit — resets at … · Resume now" card. That one
// field must survive all three client seams: the live stream reducer,
// promote-to-block, and the shared ErrorCard renderer. MsgContent owns the
// block tree for BOTH persisted and live data, so those sources cannot diverge.
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const streamingMessage = readFileSync(new URL('../StreamingMessage.jsx', import.meta.url), 'utf8')
const errorCard = readFileSync(new URL('../ErrorCard.jsx', import.meta.url), 'utf8')
const resetTime = readFileSync(new URL('../resetTime.js', import.meta.url), 'utf8')
const promotion = readFileSync(new URL('../streamPromotion.js', import.meta.url), 'utf8')
const stream = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const shell = readFileSync(new URL('../../Shell/Shell.jsx', import.meta.url), 'utf8')
const chatEmbed = readFileSync(new URL('../../ChatEmbed/ChatEmbed.jsx', import.meta.url), 'utf8')
const chatSettingsPanel = readFileSync(new URL('../ChatSettingsPanel.jsx', import.meta.url), 'utf8')
const settingsView = readFileSync(
  new URL('../../SettingsView/SettingsView.jsx', import.meta.url), 'utf8',
)

test('ErrorCard renders a parked card for a block whose pause has a reset time', () => {
  assert.match(errorCard, /block\.pause\?\.resets_at/,
    'the card must key the parked classification on block.pause.resets_at')
  assert.match(errorCard, /Rate limit/,
    'a parked block must be labeled as a rate limit, not a generic error')
  assert.match(errorCard, /Resets \{vm\.resetLabel\}/,
    'the card must show the reset time (label carries its own preposition)')
  assert.match(msgContent, /\{parked \? 'Resume now' : 'Resume'\}/,
    'the tail resume button reads "Resume now" on a parked card')
})

test('the one block renderer owns ErrorCard for both active sources', () => {
  // The live/catch-up surface once hardcoded a red "Error" card, so a benign
  // pause flashed red until promotion. StreamingMessage is now only the stable
  // <li> shell and delegates all blocks to MsgContent.
  assert.match(msgContent, /import ErrorCard from '\.\/ErrorCard\.jsx'/,
    'MsgContent must consume the shared ErrorCard')
  assert.match(streamingMessage, /import MsgContent from '\.\/MsgContent\.jsx'/,
    'the active row shell must delegate both DB and live payloads to MsgContent')
  assert.doesNotMatch(streamingMessage, /import (ErrorCard|ToolBlock|QuestionCard)/,
    'the active row shell must not grow a second block renderer')
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

test('streamItemToBlock carries the pause descriptor through promote', () => {
  assert.match(
    promotion,
    /item\.pause \? \{ pause: item\.pause \}/,
    'promote must carry the whole pause descriptor — the card would vanish otherwise',
  )
  assert.match(
    promotion,
    /item\.resumable \? \{ resumable: true \}/,
    'promote must carry resumable (the one-tap Resume gate)',
  )
  assert.doesNotMatch(promotion, /parked_until|park_reason|pause_kind/,
    'the old flat park fields must be gone from the promote seam')
})

test('the live stream reducer carries the pause descriptor', () => {
  assert.match(
    stream,
    /event\.pause \? \{ pause: event\.pause \}/,
    'a live limit/restart/stall note must render as the pause card before promote too',
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

test('auto-resume is a chat-local switch shown only on the active rate-limit card', () => {
  assert.match(msgContent, /resumable && parked && autoResumeAvailable && onAutoResumeChange/,
    'the switch must require the tail resumable rate-limit state')
  assert.match(msgContent, /Always continue after limits in this chat/,
    'the switch label makes the persistent, chat-local scope explicit')
  assert.match(msgContent, /htmlFor=\{autoResumeSwitchId\}/,
    'the visible switch label must also be its accessible name')
  assert.doesNotMatch(msgContent, /This chat only/,
    'the card keeps the control copy concise')
  assert.match(msgContent, /onCheckedChange=\{onAutoResumeChange\}/,
    'toggling changes the chat preference rather than sending continue')
  assert.match(css, /\.chat__limit-option\s*\{/,
    'the in-card control has a dedicated layout')
  assert.doesNotMatch(settingsView, /auto_resume_on_limit|Auto.?resume/i,
    'the removed global automatic option must not reappear in Settings')
  assert.match(chatSettingsPanel, /Always continue after limits/,
    'the durable policy remains manageable in the per-chat settings surface')
  assert.match(chatSettingsPanel, /Applies only to this chat/,
    'the management surface names its chat-local scope')
})

test('an enabled policy stays cancellable after the viewer clock reaches reset', () => {
  assert.match(
    chatView,
    /\(!limitResetElapsed \|\| autoResumeEnabled\)/,
    'an enabled policy must remain visible until the server starts the turn',
  )
  assert.match(chatView, /!embedded[\s\S]*chatInfo !== null[\s\S]*pendingLimitResetAt/,
    'the owner-only switch waits for chat policy hydration and a real limit card')
})

test('a system-announced auto-resume reconnects the mounted chat surface', () => {
  assert.match(shell, /externalRunSignal=\{chatRunSignal\(chatRunSignals, activeChatId\)\}/,
    'Shell must forward monotonic run activity to the mounted ChatView')
  assert.match(chatView, /fetchMessages\(\{[\s\S]*force: true,[\s\S]*authoritative: true/,
    'the mounted chat must refresh the promoted continuation row')
  assert.match(chatView, /Promise\.resolve\(connectToStream\(true\)\)/,
    'the mounted chat must attach to the automatically started stream')
  assert.match(streamingMessage, /autoResumeAvailable=\{autoResumeAvailable\}/,
    'the active assistant surface must receive the same policy control props')
  assert.match(chatView, /useSystemEventStream\(handleEmbeddedRunEvent/,
    'an eligible parked embed must observe automatic runs without Shell')
  assert.match(chatView, /onExternalRunEventRef\.current\?\.\('auto_resume_waiting'\)/,
    'the durable park arms parent completion before system events can be missed')
  assert.match(chatView, /processedExternalSignalRef[\s\S]*externalReconcileInFlightRef/,
    'external activity must drain through one queued reconciliation')
  assert.match(chatEmbed, /onExternalRunEvent=\{handleExternalRunEvent\}/,
    'the embed must receive structured start and finish events')
  assert.doesNotMatch(chatView, /onStreamEndRef\.current\?\.\(\)/,
    'system finish reconciliation must not duplicate the stream completion callback')
})

test('a benign pause (no reset time) renders the calm "Paused" family, not red Error', () => {
  // A drain-restart or stall carries pause.kind but no resets_at; it must get
  // the soft .chat__text--parked treatment and a "Paused" label. Red "Error"
  // is reserved for genuine failures (no pause at all).
  assert.match(errorCard, /benign = !!block\.pause/,
    'ANY pause gets the soft treatment')
  assert.match(errorCard, /block\.pause \? 'Paused' : 'Error'/,
    'a benign pause reads "Paused"; only genuine failures read "Error"')
  assert.match(errorCard, /role=\{vm\.benign \? undefined : 'alert'\}/,
    'the global live region announces waits; only genuine failures alert here')
  assert.match(errorCard, /className="chat__error-status"[\s\S]*<\/div>\s*\{children\}/,
    'interactive recovery controls must remain separate from the error body')
})

test('the park card reassures that a reset push is coming', () => {
  assert.match(errorCard, /chat__parked-note/,
    'a muted reassurance line renders inside the parked branch')
  assert.match(errorCard, /notification when it resets/,
    'the note names the incoming reset push')
  assert.match(errorCard, /keep trying to continue this chat after the limit resets/,
    'the note reflects the enabled per-chat behavior')
  assert.match(css, /\.chat__parked-note\s*\{/,
    'the reassurance line has its own muted style')
})
