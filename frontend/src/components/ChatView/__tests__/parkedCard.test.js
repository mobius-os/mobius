import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ownsRecoveryAction } from '../recoveryCard.js'

// Provider-limit parking (design §2.4): a limit-killed turn persists an error
// block carrying a single `pause` descriptor ({kind, resets_at?}), which
// renders as one calm queued/paused recovery card. That one
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
const paneChatView = readFileSync(new URL('../../Shell/PaneChatView.jsx', import.meta.url), 'utf8')
const chatEmbed = readFileSync(new URL('../../ChatEmbed/ChatEmbed.jsx', import.meta.url), 'utf8')
const chatSettingsPanel = readFileSync(new URL('../ChatSettingsPanel.jsx', import.meta.url), 'utf8')
const continuationCard = readFileSync(
  new URL('../ContinuationCard.jsx', import.meta.url), 'utf8',
)
const settingsView = readFileSync(
  new URL('../../SettingsView/SettingsView.jsx', import.meta.url), 'utf8',
)

test('ErrorCard renders a parked card for a block whose pause has a reset time', () => {
  assert.match(errorCard, /block\.pause\?\.resets_at/,
    'the card must key the parked classification on block.pause.resets_at')
  assert.match(errorCard, /Usage resets/,
    'a parked block must lead with a plain-language reset outcome')
  assert.match(errorCard, /Queued to continue/,
    'enabled automatic continuation is the authoritative state')
  assert.match(msgContent, /\{parked \? 'Continue now' : 'Resume'\}/,
    'an elapsed park offers Continue now rather than a premature retry')
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
    'a live limit/restart note must render as the pause card before promote too',
  )
  assert.match(
    stream,
    /event\.resumable \? \{ resumable: true \}/,
    'a live paused note must carry resumable',
  )
})

test('the parked card has styling distinct from a plain error', () => {
  assert.match(css, /\.chat__text--parked\s*\{/,
    'a .chat__text--parked style must exist (wait state, not failure)')
  assert.match(css, /\.chat__recovery-title\s*\{/,
    'the authoritative recovery outcome has its own hierarchy')
})

test('the rate-limit card presents one recovery action at a time', () => {
  assert.match(msgContent, /recoveryOwner && parked && autoResumeAvailable && onAutoResumeChange/,
    'the action must require the tail resumable rate-limit state')
  assert.match(msgContent, /Auto-continue this chat/,
    'a future reset names the persistent chat policy')
  assert.match(msgContent, /Turn off auto-continue/,
    'an enabled policy stays reversible without a competing retry')
  assert.match(msgContent, /manualResumeAvailable = recoveryOwner && \([\s\S]*!parked \|\| \(!!limitResetElapsed && !autoResumeEnabled\)/,
    'manual continuation appears only after reset and only when auto continuation is off')
  assert.doesNotMatch(msgContent, /<Switch/,
    'the card must not present a switch beside a competing action')
  assert.match(css, /\.chat__recovery-actions\s*\{/,
    'the in-card action has a dedicated layout')
  assert.doesNotMatch(settingsView, /auto_resume_on_limit|Auto.?resume/i,
    'the removed global automatic option must not reappear in Settings')
  assert.match(chatSettingsPanel, /Automatically continue after usage limits/,
    'the paid-usage policy remains manageable in chat settings')
  assert.doesNotMatch(chatSettingsPanel, /Continue after planned restarts/,
    'restart continuation is always on and exposes no toggle')
  assert.doesNotMatch(chatSettingsPanel, /On for this chat|Off for this chat/,
    'the switch color communicates state without redundant state copy')
  assert.match(chatSettingsPanel, /className="chat-policy-switch"/,
    'the settings surface uses the same full-size black/purple switch treatment')
  assert.match(css, /\.chat-policy-switch button\[role="switch"\]\[data-state="checked"\]/,
    'the chat switch restores the SDK checked track after the button reset')
})

test('only the visible tail block owns recovery controls', () => {
  const block = { type: 'error', resumable: true, pause: { resets_at: 'later' } }
  const context = {
    block,
    lastEntryIndex: 3,
    isLastMessage: true,
    canResume: true,
  }
  assert.equal(ownsRecoveryAction({ ...context, entryIndex: 2 }), false)
  assert.equal(ownsRecoveryAction({ ...context, entryIndex: 3 }), true)
  assert.equal(ownsRecoveryAction({ ...context, entryIndex: 3, isLastMessage: false }), false)
  assert.equal(ownsRecoveryAction({ ...context, entryIndex: 3, canResume: false }), false)
})

test('continuations render as product markers, not user bubbles', () => {
  assert.match(msgContent, /isContinuationMessage\(msg\)/,
    'legacy automatic and current continuation rows share the marker branch')
  assert.match(msgContent, /<ContinuationCard msg=\{msg\}/,
    'manual, restart, and limit continuations share the marker renderer')
  assert.match(continuationCard, /Resumed manually/)
  assert.match(continuationCard, /Server restarted — continuing automatically/)
  assert.match(continuationCard, /Usage available again — continuing automatically/)
  assert.match(msgContent, /onResume\('continue', \{[\s\S]*continuation: 'manual',[\s\S]*pin: false/,
    'Resume must mark its provider-facing prompt as a product action')
  assert.match(chatView, /chat__msg--\$\{continuationMarker \? 'marker' : msg\.role\}/,
    'the row shell must not inherit owner-user alignment')
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
  // Every mounted chat surface is now a PaneChatView (one per visible chat pane,
  // including the single-pane case): Shell selects per-chat run activity BEFORE
  // the memo boundary, so another chat's Map update cannot rerender this pane.
  assert.match(shell, /externalRunSignal=\{chatRunSignal\(chatRunSignals, chatId\)\}/,
    'Shell must forward only this pane chat’s monotonic run activity')
  assert.doesNotMatch(shell, /chatRunSignals=\{chatRunSignals\}/,
    'the replacement run-signal Map must not cross every pane memo boundary')
  assert.match(shell, /const stablePaneNavTo = useCallback\([\s\S]*navToRef\.current/,
    'the pane navigation facade must keep a stable identity while reaching current routing')
  assert.match(shell, /navTo=\{stablePaneNavTo\}/,
    'per-render navigation identity must not defeat the pane memo boundary')
  assert.match(paneChatView, /externalRunSignal=\{externalRunSignal\}/,
    'PaneChatView must forward per-chat monotonic run activity to its ChatView')
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
  // A drain-restart carries pause.kind but no resets_at; it must get
  // the soft .chat__text--parked treatment and a "Paused" label. Red "Error"
  // is reserved for genuine failures (no pause at all).
  assert.match(errorCard, /benign = !!block\.pause/,
    'ANY pause gets the soft treatment')
  assert.match(errorCard, /block\.pause \? 'Paused' : 'Error'/,
    'a benign pause reads "Paused"; only genuine failures read "Error"')
  assert.match(errorCard, /\) : vm\.benign \? \([\s\S]*className="chat__recovery-title"/,
    'a benign pause uses the neutral recovery hierarchy rather than the red error label')
  assert.match(errorCard, /Möbius will continue automatically when the restart is complete\./,
    'the restart pause briefly states its expected automatic outcome')
  assert.match(chatView, /Response paused for restart\. Möbius will continue automatically\./,
    'the screen-reader status matches the visible automatic continuation promise')
  assert.match(chatView, /Paused for restart — continuing automatically/,
    'the offscreen-card nudge keeps the same concise informational language')
  assert.match(errorCard, /role=\{vm\.benign \? undefined : 'alert'\}/,
    'the global live region announces waits; only genuine failures alert here')
  assert.match(errorCard, /className="chat__error-status"[\s\S]*<\/div>\s*\{children\}/,
    'interactive recovery controls must remain separate from the error body')
})

test('the park card keeps provider mechanics behind progressive disclosure', () => {
  assert.match(errorCard, /chat__recovery-copy/,
    'the plain-language outcome is the visible supporting copy')
  assert.match(errorCard, /Technical details/,
    'the raw provider payload stays available on demand')
  assert.match(errorCard, /Möbius will continue automatically/,
    'the enabled state promises only the behavior the policy owns')
  assert.match(css, /\.chat__recovery-details\s*\{/,
    'technical detail has a quiet disclosure style')
  assert.match(errorCard, /chat__recovery-details-chevron/,
    'technical detail has an explicit visible disclosure indicator')
  assert.match(css, /\[open\] \.chat__recovery-details-chevron[\s\S]*rotate\(90deg\)/,
    'the disclosure indicator reflects its open state')
})
