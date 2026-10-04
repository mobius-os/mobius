import { readFileSync } from 'node:fs'
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { upsertTerminalErrorItem } from '../streamReducers.js'
import { ownsRecoveryAction } from '../recoveryCard.js'
import { isProviderLimitPause, pauseTiming } from '../resetTime.js'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: ErrorCard } = await vite.ssrLoadModule(
  '/src/components/ChatView/ErrorCard.jsx',
)
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)

after(() => vite.close())

test('new, legacy, and unknown provider timestamps keep check and reset distinct', () => {
  const check = '2035-03-06T08:45:00Z'
  const reset = '2035-05-20T11:05:00Z'
  assert.deepEqual(pauseTiming({ kind: 'limit', check_at: check, resets_at: reset }), {
    checkAt: check, resetAt: reset,
  })
  assert.deepEqual(pauseTiming({ kind: 'limit', check_at: check }), {
    checkAt: check, resetAt: null,
  })
  assert.deepEqual(pauseTiming({ kind: 'limit', resets_at: check }), {
    checkAt: check, resetAt: null,
  })
  assert.deepEqual(pauseTiming({ kind: 'limit', check_at: 'bad', resets_at: reset }), {
    checkAt: null, resetAt: null,
  })
})

test('real producer limit kinds and legacy limit retain rate-limit recovery', () => {
  for (const kind of ['usage_limit', 'rate_limit', 'limit']) {
    assert.equal(isProviderLimitPause({ kind }), true)
    const block = {
      type: 'error', resumable: true,
      pause: { kind, check_at: '2099-09-14T12:00:00Z' },
    }
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: { role: 'assistant', content: '', blocks: [block] },
      isLastMsg: true, onResume() {}, onAutoResumeChange() {},
      autoResumeAvailable: true,
    }))
    assert.match(html, /Provider limit reached/)
    assert.match(html, /Provider reset time unknown/)
    assert.match(html, /Turn on auto-continue/)
    assert.match(html, />Try now<\/button>/)
  }
  for (const kind of ['memory', 'storage', 'model_capacity', 'restart']) {
    assert.equal(isProviderLimitPause({ kind }), false)
  }
})

test('provider date beyond bounded check remains separate and elapsed checks do not prove usage', () => {
  const check = '2035-03-06T08:45:00Z'
  const reset = '2035-05-20T11:05:00Z'
  const block = { type: 'error', pause: { kind: 'limit', check_at: check, resets_at: reset } }
  const html = renderToStaticMarkup(createElement(ErrorCard, { block, resetElapsed: true }))
  assert.match(html, /Ready to retry/)
  assert.match(html, /the provider may still be limited/)
  assert.match(html, /Provider reports the limit resets/)
  assert.match(html, /Next retry check/)
  assert.doesNotMatch(html, /Usage is available again|at the reset/)

  const unknown = renderToStaticMarkup(createElement(ErrorCard, {
    block: { ...block, pause: { kind: 'limit', check_at: check } },
  }))
  assert.match(unknown, /Provider reset time unknown/)
  assert.match(unknown, /Next retry check/)
  const legacy = renderToStaticMarkup(createElement(ErrorCard, {
    block: { ...block, pause: { kind: 'limit', resets_at: check } },
  }))
  assert.match(legacy, /Provider reset time unknown/)

  const past = new Date(Date.now() - 3 * 86400000).toISOString()
  const pastReset = renderToStaticMarkup(createElement(ErrorCard, {
    block: { ...block, pause: { kind: 'limit', check_at: check, resets_at: past } },
    resetElapsed: true,
  }))
  assert.match(pastReset, /Provider reports the limit resets/)
  assert.doesNotMatch(pastReset, /Usage is available again|Provider reset time unknown/)
})

test('resource and model-capacity checks never claim provider quota resets', () => {
  const check_at = '2099-09-14T12:00:00Z'
  for (const kind of ['memory', 'storage', 'model_capacity']) {
    const block = { type: 'error', pause: { kind, check_at } }
    const html = renderToStaticMarkup(createElement(ErrorCard, { block }))
    assert.doesNotMatch(html, /Provider reports the limit resets|Provider reset time unknown|Rate limit/)
    if (kind === 'model_capacity') assert.match(html, /Trying again/)
  }
})

test('retry clocks and embedded arming use check_at, never the provider reset', () => {
  assert.match(chatView, /resetDeadlineState\(pendingLimitCheckAt\)/)
  assert.match(chatView, /resetDeadlineDelay\(pendingLimitCheckAt\)/)
  assert.match(chatView, /armedEmbeddedResetRef\.current = pendingLimitCheckAt/)
  assert.match(chatView, /enabled: Boolean\(pendingLimitPark && pendingLimitProvider\)/)
  assert.doesNotMatch(chatView, /resetDeadline(?:State|Delay)\(pendingLimitResetAt\)/)
})

for (const autoResumeEnabled of [true, false]) {
  test(`future limit exposes an explicit retry with auto-continue ${autoResumeEnabled}`, () => {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: { role: 'assistant', content: '', blocks: [{
        type: 'error', message: '', resumable: true,
        pause: { kind: 'limit', resets_at: '2099-09-14T12:00:00Z' },
      }] },
      isLastMsg: true, onResume() {}, limitResetElapsed: false,
      autoResumeEnabled, autoResumeAvailable: true, onAutoResumeChange() {},
    }))
    assert.match(html, />Try now<\/button>/)
  })
}

for (const [label, overrides, kind] of [
  ['historical card', { isLastMsg: false }, 'limit'],
  ['memory wait', {}, 'memory'],
  ['storage wait', {}, 'storage'],
]) {
  test(`early retry does not bypass ${label} ownership`, () => {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: { role: 'assistant', content: '', blocks: [{
        type: 'error', message: '', resumable: true,
        pause: { kind, resets_at: '2099-09-14T12:00:00Z' },
      }] },
      isLastMsg: true, onResume() {}, autoResumeEnabled: true,
      ...overrides,
    }))
    assert.doesNotMatch(html, /class="chat__resume /)
  })
}

// Provider-limit parking (design §2.4): a limit-killed turn persists an error
// block carrying a single `pause` descriptor ({kind, resets_at?}), which
// renders as one calm queued/paused recovery card. That one
// field must survive all three client seams: the live stream reducer,
// promote-to-block, and the shared ErrorCard renderer. MsgContent owns the
// block tree for BOTH persisted and live data, so those sources cannot diverge.
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const streamingMessage = readFileSync(new URL('../AssistantReply.jsx', import.meta.url), 'utf8')
const errorCard = readFileSync(new URL('../ErrorCard.jsx', import.meta.url), 'utf8')
const resetTime = readFileSync(new URL('../resetTime.js', import.meta.url), 'utf8')
const promotion = readFileSync(new URL('../streamPromotion.js', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const shell = readFileSync(new URL('../../Shell/Shell.jsx', import.meta.url), 'utf8')
const shellChatRunLifecycle = readFileSync(
  new URL('../../Shell/useShellChatRunLifecycle.js', import.meta.url),
  'utf8',
)
const chatSettingsPanel = readFileSync(new URL('../ChatSettingsPanel.jsx', import.meta.url), 'utf8')
const continuationCard = readFileSync(
  new URL('../ContinuationCard.jsx', import.meta.url), 'utf8',
)
const settingsView = readFileSync(
  new URL('../../SettingsView/SettingsView.jsx', import.meta.url), 'utf8',
)
const waitingChip = readFileSync(new URL('../WaitingChip.jsx', import.meta.url), 'utf8')

test('ErrorCard renders a parked card for a provider-limit pause', () => {
  assert.match(errorCard, /isProviderLimitPause\(block\.pause\)/,
    'the card must key provider-limit classification on the shared predicate')
  assert.match(errorCard, /Provider reports the limit resets/,
    'a parked block must lead with a plain-language reset outcome')
  assert.match(errorCard, /Queued to retry/,
    'enabled automatic continuation is the authoritative state')
  assert.match(msgContent, /recoveryCredit\?\.actionLabel \|\| 'Try now'/,
    'a park names a reported paid continuation while retaining a safe retry fallback')
})

test('the rendered limit card explains automatic and early recovery states', () => {
  const block = {
    type: 'error',
    message: '',
    pause: { kind: 'limit', resets_at: '2026-09-04T09:00:00Z' },
  }
  const automatic = renderToStaticMarkup(createElement(ErrorCard, {
    block,
    autoResume: true,
  }))
  assert.match(automatic, /Queued to retry/)
  assert.match(automatic, /check again/)
  assert.doesNotMatch(automatic, /Added credits/)

  const manual = renderToStaticMarkup(createElement(ErrorCard, {
    block,
    autoResume: false,
  }))
  assert.match(manual, /Provider reset time unknown/)
  assert.match(manual, /Turn on auto-continue, or try again manually/)

  const withCredits = renderToStaticMarkup(createElement(ErrorCard, {
    block,
    autoResume: false,
    recoveryCredit: { label: 'Paid extra usage is available' },
  }))
  assert.match(withCredits, /Paid extra usage is available/)
  assert.match(withCredits, /Continuing now may use it/)

  const elapsed = renderToStaticMarkup(createElement(ErrorCard, {
    block,
    autoResume: false,
    resetElapsed: true,
  }))
  assert.match(elapsed, /Ready to retry/)
  assert.match(elapsed, /the provider may still be limited/)
})

test('a restart pause promises continuation only until it falls back to manual', () => {
  const block = {
    type: 'error', message: 'Paused for a platform update.', resumable: true,
    pause: { kind: 'restart' },
  }
  const planned = renderToStaticMarkup(createElement(ErrorCard, { block }))
  assert.match(planned, /continue automatically when the restart is complete/)

  const manual = renderToStaticMarkup(createElement(ErrorCard, {
    block: { ...block, pause: { kind: 'restart', manual: true } },
  }))
  assert.match(manual, /Your work is saved\. Resume to continue\./)
  assert.doesNotMatch(manual, /continue automatically/)
})

test('a busy selected-model card explains its short automatic retry', (t) => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'https://mobius.test/' } }
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  })
  const html = renderToStaticMarkup(createElement(ErrorCard, {
    block: {
      type: 'error',
      message: 'Selected model is at capacity. Please try a different model.',
      pause: { kind: 'model_capacity', resets_at: '2099-09-14T12:00:00Z' },
    },
  }))
  assert.match(html, /Trying again/)
  assert.match(html, /up to five times/)
  assert.match(html, /choose another model/)
  assert.doesNotMatch(html, /Paid extra usage|Turn on auto-continue/)
})

test('an exhausted busy-model card stops promising retries and offers Resume', () => {
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: { role: 'assistant', content: '', blocks: [{
      type: 'error', resumable: true,
      message: 'The selected model is still busy after five automatic retries.',
      pause: { kind: 'model_capacity_exhausted' },
    }] },
    isLastMsg: true,
    onResume() {},
  }))
  assert.match(html, /Model still busy/)
  assert.match(html, /Five automatic retries were used/)
  assert.match(html, />Resume<\/button>/)
  assert.doesNotMatch(html, /Trying again/)
})

const refusalBlock = {
  type: 'error', resumable: true,
  message: 'API Error: safeguards flagged this message. Details: `[category]`',
  pause: { kind: 'provider_refusal', provider: 'claude' },
}

function withWindow(t) {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'https://mobius.test/' } }
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  })
}

test('a provider refusal offers moves that change the request, never a timed retry', (t) => {
  withWindow(t)
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: { role: 'assistant', content: '', blocks: [refusalBlock] },
    isLastMsg: true,
    onResume() {},
    onRefusalModelChoice() {},
    onRefusalFreshSession() {},
  }))
  assert.match(html, /This model declined to continue/)
  assert.match(html, /usually fails again/)
  assert.match(html, />Switch model<\/button>/)
  assert.match(html, />Start fresh session<\/button>/)
  assert.match(html, />Resume<\/button>/)
  // The provider's raw report stays available but is not the headline.
  assert.match(html, /Technical details/)
  assert.doesNotMatch(html, /Trying again|auto-continue|role="alert"/)
})

test('a refusal card away from the transcript tail offers no actions', (t) => {
  withWindow(t)
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: { role: 'assistant', content: '', blocks: [refusalBlock] },
    isLastMsg: false,
    onResume() {},
    onRefusalModelChoice() {},
    onRefusalFreshSession() {},
  }))
  assert.match(html, /This model declined to continue/)
  assert.doesNotMatch(html, /Switch model<\/button>|Start fresh session<\/button>|>Resume</)
})

for (const [continuationWait, title, explanation] of [
  ['restoring_edits', 'Waiting for the platform update', 'while the update restores unfinished work'],
  ['restart_required', 'Waiting for a server restart', 'until a server restart loads the restored work'],
]) {
  test(`busy-model retry names ${continuationWait} instead of promising a deadline`, (t) => {
    const previousWindow = globalThis.window
    globalThis.window = { location: { href: 'https://mobius.test/' } }
    t.after(() => {
      if (previousWindow === undefined) delete globalThis.window
      else globalThis.window = previousWindow
    })
    const msg = { role: 'assistant', content: '', blocks: [{
      type: 'error', resumable: true,
      message: 'Selected model is at capacity.',
      pause: { kind: 'model_capacity', resets_at: '2026-09-04T09:00:00Z' },
    }] }
    const props = { msg, isLastMsg: true, onResume() {}, continuationWait, handoff: { kind: 'automatic', reason: 'model_capacity' } }
    const blocked = renderToStaticMarkup(createElement(MsgContent, props))
    assert.ok(blocked.includes(title))
    assert.ok(blocked.includes(explanation))
    assert.match(blocked, /Automatic retries are paused/)
    assert.match(blocked, /Technical details/)
    assert.match(blocked, /Selected model is at capacity/)
    assert.doesNotMatch(blocked, /Trying again|up to five times/)

    // Clearing the live hold restores the ordinary retry presentation without
    // changing the saved provider failure or its retry deadline.
    const cleared = renderToStaticMarkup(createElement(MsgContent, {
      ...props, continuationWait: null,
    }))
    assert.match(cleared, /Trying again/)
    assert.doesNotMatch(cleared, /Automatic retries are paused/)
    const historical = renderToStaticMarkup(createElement(MsgContent, {
      ...props, isLastMsg: false,
    }))
    assert.doesNotMatch(historical, /Automatic retries are paused/)
  })
}

test('the one block renderer owns ErrorCard for both active sources', () => {
  // The live/catch-up surface once hardcoded a red "Error" card, so a benign
  // pause flashed red until promotion. AssistantReply owns the stable
  // source rows and delegates all blocks to MsgContent.
  assert.match(msgContent, /import ErrorCard(?:, \{[^}]*\})? from '\.\/ErrorCard\.jsx'/,
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
  assert.match(resetTime, /formatTime\(d\)/,
    'the reset uses the shared local 24-hour clock formatter')
  assert.match(resetTime, /tomorrow at/,
    'the label is day-aware — a 7-day park must not read as a bare time')
  assert.match(errorCard,
    /import \{ formatResetTime, isProviderLimitPause, pauseTiming \} from '\.\/resetTime\.js'/,
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
  const block = upsertTerminalErrorItem([], {
    message: 'Usage limit reached.',
    resumable: true,
    pause: { kind: 'limit', resets_at: '2026-08-24T09:00:00Z' },
  })[0]
  assert.deepEqual(block, {
    type: 'error',
    message: 'Usage limit reached.',
    resumable: true,
    pause: { kind: 'limit', resets_at: '2026-08-24T09:00:00Z' },
  }, 'a live limit note must render as the pause card before promote too')
})

test('the parked card has styling distinct from a plain error', () => {
  assert.match(css, /\.chat__text--parked\s*\{/,
    'a .chat__text--parked style must exist (wait state, not failure)')
  assert.match(css, /\.chat__recovery-title\s*\{/,
    'the authoritative recovery outcome has its own hierarchy')
})

test('the rate-limit card keeps automatic recovery and an explicit early retry', () => {
  assert.match(msgContent, /recoveryOwner && parked && !modelCapacity && autoResumeAvailable && onAutoResumeChange/,
    'the action must require the tail resumable rate-limit state')
  assert.match(msgContent, /Auto-continue this chat/,
    'a future reset names the persistent chat policy')
  assert.match(msgContent, /Turn off auto-continue/,
    'an enabled policy stays reversible without a competing retry')
  const earlyRetry = renderToStaticMarkup(createElement(MsgContent, {
    msg: { role: 'assistant', blocks: [{ type: 'error', resumable: true, pause: { kind: 'usage_limit' } }] },
    isLastMsg: true, onResume() {}, handoff: { kind: 'automatic' },
  }))
  assert.match(earlyRetry, /class="chat__resume chat__recovery-action"/,
    'manual continuation remains available when credits restore usage early')
  assert.match(errorCard, /Continuing now may use it/,
    'paid recovery makes potential provider charges explicit')
  assert.doesNotMatch(msgContent, /<Switch/,
    'the card must not present a switch beside a competing action')
  assert.match(css, /\.chat__recovery-actions\s*\{/,
    'the in-card action has a dedicated layout')
  assert.doesNotMatch(settingsView, /auto_resume_on_limit|Auto.?resume/i,
    'the removed global automatic option must not reappear in Settings')
  assert.match(chatSettingsPanel, /Automatically continue<br \/>after usage limits/,
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
  assert.equal(
    ownsRecoveryAction({ ...context, entryIndex: 3, questionOwnsTurn: true }),
    false,
    'an unanswered question owns the turn ahead of a resumable tail pause',
  )
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
  assert.match(continuationCard, /Retry check due — trying the provider again/)
  assert.match(msgContent, /onClick=\{onResume\}/,
    'Resume delegates the lifecycle action instead of manufacturing owner text')
  assert.match(chatView, /chat__msg--\$\{continuationMarker \? 'marker' : msg\.role\}/,
    'the row shell must not inherit owner-user alignment')
  assert.match(chatView, /supersedeResumedPauseBlocks\(messages,/,
    'a completed continuation replaces its stale actionable pause in the render projection')
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
  assert.match(shell, /externalRunSignal=\{chatRunSignalFor\(chatId\)\}/,
    'Shell must forward only this pane chat’s monotonic run activity')
  assert.match(
    shellChatRunLifecycle,
    /chatId => chatRunSignal\(chatRunSignals, chatId\)/,
    'the lifecycle owner must select one chat before the pane memo boundary',
  )
  assert.doesNotMatch(shell, /chatRunSignals=\{chatRunSignals\}/,
    'the replacement run-signal Map must not cross every pane memo boundary')
  assert.match(shell, /openAppWithIntent=\{openAppWithIntent\}/,
    'the stable app-intent navigator must not defeat the pane memo boundary')
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
  assert.match(errorCard, /\) : vm\.benign \? \([\s\S]*chat__recovery-title--paused/,
    'a benign pause uses the neutral recovery hierarchy rather than the red error label')
  assert.match(css, /\.chat__recovery-title--paused\s*\{[\s\S]*?color: var\(--accent\)/,
    'the Paused heading uses the exact same accent token as Resume')
  assert.match(errorCard, /Möbius will continue automatically when the restart is complete\./,
    'the restart pause briefly states its expected automatic outcome')
  assert.match(errorCard, /block\.pause\.manual\s*\?\s*'Your work is saved\. Resume to continue\.'/,
    'a crash or manual-fallback restart pause offers Resume instead of promising continuation')
  assert.match(chatView, /pause\?\.kind === 'restart' && !pendingResumeBlock\.pause\.manual/,
    'the status and nudge fall back to Resume wording for a manual restart pause')
  assert.match(errorCard, /block\.resumable[\s\S]*?This response is paused\./,
    'a question-held restart cannot promise continuation before the owner answers')
  assert.match(chatView, /Response paused for restart\. Möbius will continue automatically\./,
    'the screen-reader status matches the visible automatic continuation promise')
  assert.match(chatView, /Paused for restart — continuing automatically/,
    'the offscreen-card nudge keeps the same concise informational language')
  assert.match(errorCard, /role=\{vm\.benign \? undefined : 'alert'\}/,
    'the global live region announces waits; only genuine failures alert here')
  assert.match(errorCard, /className="chat__error-status"[\s\S]*<\/div>\s*\{children\}/,
    'interactive recovery controls must remain separate from the error body')
})

test('resource parks appear in the standard Waiting surface', () => {
  assert.match(chatView, /const resourcePause = isResourcePause\(pendingResumeBlock\)/,
    'the durable tail pause must drive the live waiting presentation')
  assert.match(chatView, /classifyChatHandoff\(\{[\s\S]*resourcePause: pendingResumeBlock,[\s\S]*authoritativeHandoff: serverHandoff,[\s\S]*\}\)/,
    'resource waits must share the self-resuming handoff visibility rule')
  assert.match(chatView, /<WaitingChip[\s\S]*resourcePause=\{resourcePause \|\| \(modelCapacityPause/,
    'the standard Waiting component must receive the resource handoff')
  assert.match(waitingChip, /function ResourceCard/,
    'the shared Waiting surface should explain resource ownership and wake-up')
  assert.match(chatView, /const hasPendingResume = !!pendingResumeBlock[\s\S]*&& !resourcePause/,
    'an automatically managed resource wait must not advertise a manual resume nudge')
  assert.match(chatView, /Waiting for storage headroom\. This chat will resume automatically\./,
    'the screen-reader status must describe the actual automatic handoff')
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


test('Goal handoff pauses use calm actionable copy, including saved legacy notes', () => {
  for (const block of [
    { type: 'error', resumable: true, pause: { kind: 'goal_handoff' } },
    { type: 'error', resumable: true, message: 'This Goal paused repeatedly without a visible owner for the next action. Resume it to continue; before pausing again, use a question card, a durable wait, or a wake-enabled helper.' },
  ]) {
    const html = renderToStaticMarkup(createElement(ErrorCard, { block }))
    assert.match(html, /chat__text--parked/)
    assert.match(html, /Goal paused/)
    assert.match(html, /Your progress is saved/)
    assert.match(html, /Resume to continue this Goal/)
    assert.doesNotMatch(html, /wake-enabled|visible owner|role="alert"|continue automatically/)
  }
})

test('a genuine resumable failure remains an error, not a Goal pause', (t) => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'https://mobius.test/' } }
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  })
  const html = renderToStaticMarkup(createElement(ErrorCard, {
    block: { type: 'error', resumable: true, message: 'Connection failed' },
  }))
  assert.match(html, /role="alert"/)
  assert.match(html, /Connection failed/)
  assert.doesNotMatch(html, /chat__text--parked|Goal paused/)
})


for (const [continuationWait, expected] of [
  ['restoring_edits', /Waiting for the update to restore unfinished work/],
  ['restart_required', /Waiting for a server restart to load the restored work/],
  ['restart', /continue automatically when the restart is complete/],
]) {
  test(`restart card names the current ${continuationWait} blocker`, () => {
    const msg = { role: 'assistant', content: '', blocks: [{
      type: 'error', message: 'Paused for a platform update.', resumable: true,
      pause: { kind: 'restart' },
    }] }
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg, isLastMsg: true, onResume() {}, continuationWait,
      handoff: { kind: 'automatic', reason: 'restart' },
    }))
    assert.match(html, expected)
    assert.doesNotMatch(html, /chat__recovery-title[^>]*>Error/)
    const history = renderToStaticMarkup(createElement(MsgContent, {
      msg, isLastMsg: false, onResume() {}, continuationWait,
    }))
    assert.doesNotMatch(history, /Waiting for (the update|a server restart)/)
  })
}

test('manual restart recovery never promises automatic continuation', () => {
  const html = renderToStaticMarkup(createElement(ErrorCard, {
    block: { type: 'error', resumable: true, pause: { kind: 'restart', manual: true } },
    continuationWait: 'restart_required',
  }))
  assert.match(html, /Your work is saved. Resume to continue/)
  assert.doesNotMatch(html, /automatically|Waiting for a server restart/)
})

test('continuation blocker changes survive mounted and cached runtime reconciliation', () => {
  assert.match(msgContent, /prev\.continuationWait === next\.continuationWait/)
  assert.match(chatView, /setContinuationWait\(activationCache\?\.continuationWait \|\| null\)/)
  for (const source of ['data', 'runtime']) {
    assert.ok(chatView.includes(`setContinuationWait(${source}.continuation_wait || null)`))
    assert.ok(chatView.includes(`continuationWait: ${source}.continuation_wait || null`))
  }
})

test('manual resource recovery retains the existing Resume action while automatic waits do not compete', () => {
  for (const kind of ['memory', 'storage', 'model_capacity']) {
    const props = {
      msg: { role: 'assistant', blocks: [{ type: 'error', resumable: true, pause: { kind } }] },
      isLastMsg: true, onResume() {}, resumeState: {},
    }
    const manual = renderToStaticMarkup(createElement(MsgContent, { ...props, handoff: { kind: 'recovery' } }))
    assert.match(manual, /class="chat__resume chat__recovery-action"/)
    assert.doesNotMatch(manual, /will continue automatically|Trying again shortly/)
    const unknown = renderToStaticMarkup(createElement(MsgContent, props))
    assert.doesNotMatch(unknown, /class="chat__resume chat__recovery-action"/)
    const automatic = renderToStaticMarkup(createElement(MsgContent, { ...props, handoff: { kind: 'automatic', reason: kind } }))
    assert.doesNotMatch(automatic, /class="chat__resume chat__recovery-action"/)
    const answered = renderToStaticMarkup(createElement(MsgContent, {
      ...props,
      msg: { ...props.msg, blocks: [{ type: 'question', question_id: 'open-question', questions: [] }, ...props.msg.blocks] },
      liveQuestionId: 'open-question', onQuestionAnswer() {}, handoff: { kind: 'recovery' },
    }))
    assert.doesNotMatch(answered, /class="chat__resume chat__recovery-action"/)
  }
})

for (const kind of ['memory', 'storage', 'model_capacity', 'restart']) {
  for (const [name, props] of [
    ['historical', { isLastMsg: false, handoff: { kind: 'recovery' } }],
    ['awaiting runtime', { isLastMsg: true }],
    ['stale active snapshot', { isLastMsg: true, handoff: { kind: 'working' } }],
  ]) {
    test(`${name} ${kind} pause does not invent a manual recovery failure`, () => {
      const html = renderToStaticMarkup(createElement(MsgContent, {
        msg: { role: 'assistant', content: '', blocks: [{ type: 'error', resumable: true, pause: { kind } }] },
        onResume() {}, ...props,
      }))
      assert.doesNotMatch(html, /recovery is unavailable|recovery needs attention|This restart needs manual recovery|Recovery needed/)
      assert.match(html, /Trying again|continue automatically/)
    })
  }
}
