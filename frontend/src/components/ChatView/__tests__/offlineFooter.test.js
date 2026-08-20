import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const chatInputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const connectionStatus = readFileSync(new URL('../ConnectionStatus.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const scrollMode = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
const shell = readFileSync(new URL('../../Shell/Shell.jsx', import.meta.url), 'utf8')

test('floating actions precede the measured rail → connection → queued → composer stack', () => {
  const footStart = chatView.indexOf('<div ref={footRef} className="chat__foot">')
  const composer = chatView.indexOf('<ChatInputBar', footStart)
  const foot = chatView.slice(footStart, composer)
  const rail = foot.indexOf('<ProgressRail')
  const queued = foot.indexOf('<QueuedMessages')
  const connection = foot.indexOf('<ConnectionStatus')

  assert.ok(
    footStart >= 0 && composer > footStart && rail >= 0 && queued >= 0
      && connection >= 0,
    'the complete footer stack must be present',
  )
  assert.ok(rail < connection, 'the progress rail stacks above connection/retry')
  assert.ok(connection < queued, 'connection/retry stacks directly above the queued input tray')
  const floatingActions = foot.indexOf('className="chat__floating-actions"')
  assert.ok(floatingActions >= 0 && floatingActions < rail,
    'transient post-turn actions must render in the separate floating layer')
  const transientLane = foot.indexOf('className="chat__floating-transients"')
  const offscreenNudges = foot.indexOf('className="chat__offscreen-nudges"')
  const openApp = foot.indexOf('className="chat__open-app"')
  const contribution = foot.indexOf('<ContributionReviewCard')
  assert.ok(
    transientLane > floatingActions
      && offscreenNudges > transientLane
      && openApp > transientLane
      && contribution > openApp,
    'every transient footer action must render above the stable contribution anchor',
  )
  assert.ok(contribution < rail,
    'the contribution anchor must stay outside measured footer flow')
  assert.match(
    chatCss,
    /\.chat__floating-actions\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?bottom:\s*calc\(100% \+ var\(--chat-foot-card-gap\)\);[\s\S]*?pointer-events:\s*none;/,
    'transient-only actions must stay clear of the composer and outside measured footer flow',
  )
  assert.match(
    chatCss,
    /\.chat__floating-actions:has\(> \.contrib-card-stack\)\s*\{[\s\S]*?bottom:\s*calc\([\s\S]*?100% - var\(--chat-foot-pad-block\) - var\(--chat-foot-pad-block\)[\s\S]*?\+ var\(--chat-foot-card-gap\)[\s\S]*?\);/,
    'only a rendered contribution card may activate the closer goal-rail-like dock',
  )
  assert.match(
    chatCss,
    /\.chat__foot\s*\{[\s\S]*?--chat-foot-pad-block:\s*8px;[\s\S]*?padding:\s*var\(--chat-foot-pad-block\) 12px;/,
    'floating and measured footer surfaces must share the same top-padding owner',
  )
  assert.match(
    chatCss,
    /\.chat__floating-transients\s*\{[\s\S]*?display:\s*flex;[\s\S]*?flex-direction:\s*column;[\s\S]*?gap:\s*var\(--chat-foot-card-gap\);/,
    'transient footer actions must share one explicit stack',
  )
  assert.match(
    chatCss,
    /\.chat__open-app\s*\{[\s\S]*?width:\s*min\(100%,\s*720px\);[\s\S]*?align-items:\s*flex-end;/,
    'Open app keeps its original right edge inside the composer column',
  )
  assert.doesNotMatch(
    scrollMode,
    /floatingAction|floatingActions/,
    'floating cards must not add or retain transcript spacer height',
  )
})

test('the shell is the one persistent connection owner while send failures stay contextual', () => {
  assert.match(shell, /ReachabilityPhase\.CHECKING[\s\S]*?'Reconnecting…'/)
  assert.match(shell, /ReachabilityPhase\.OFFLINE \? 'Offline'/)
  assert.match(
    shell,
    /const connectionStatusLabel = reachabilityLabel[\s\S]*?restartPending \? 'Restarting…'/,
  )
  assert.match(shell, /\{connectionStatusLabel && \([\s\S]*?className="shell__connection-status"[\s\S]*?shell__sr-only/)
  assert.match(shell, /ev\.type === 'server_restarting'[\s\S]*?setRestartPending\(\)/)
  assert.match(
    shell,
    /const reconcileSystemStateOnOpen = useCallback\(\(\) => \{[\s\S]*?clearRestartPending\(\)/,
  )
  assert.doesNotMatch(chatView, /You're offline — chat needs a connection\./)
  assert.doesNotMatch(chatInputBar, /You're offline — chat needs a connection\./)
  assert.match(
    chatInputBar,
    /\{sendFailure && \([\s\S]*?chat__offline-note--error[\s\S]*?\{sendFailure\}/,
    'moving the persistent offline state must not remove a failed-send explanation',
  )
})

test('connection failure hides queued actions and disables composer steering', () => {
  assert.match(chatView, /\{connectionError !== 'disconnected' && \([\s\S]*?<QueuedMessages/,
    'the lost-connection state should own the footer stack until Retry succeeds')
  assert.match(chatView, /const showSteer = !hasPendingQuestion[\s\S]*?connectionError !== 'disconnected'[\s\S]*?turnActive[\s\S]*?pendingQueue\.visiblePendingMessages\.length > 0/,
    'the visible composer steer identity must be gated by pending QA and connection health')
  assert.match(chatView, /const canSteer = canRequestSteer[\s\S]*?canFastForwardQueue/,
    'server-confirmed steering must remain stricter than the optimistic visual identity')
  assert.match(chatView, /const canSubmitSteer = !hasPendingQuestion[\s\S]*?connectionError !== 'disconnected'[\s\S]*?!steerBusy[\s\S]*?turnActive/,
    'the composed-text keyboard steer path must be gated by pending QA and connection health too')
  assert.match(chatView, /const canRequestSteer = showSteer && !steerBusy/,
    'the empty-composer keyboard path must share the optimistic visible steer gate')
})

test('connection status matches the composer column while send failures stay compact', () => {
  assert.match(
    chatCss,
    /\.connection-status\s*\{[\s\S]*?width:\s*100%;[\s\S]*?max-width:\s*720px;/,
    'connection status should fill the bounded composer column',
  )
  assert.match(
    chatCss,
    /\.chat__form\s*\{[\s\S]*?max-width:\s*720px;/,
    'connection status and composer must share the same maximum width',
  )
  assert.match(
    chatCss,
    /\.chat__offline-note\s*\{[\s\S]*?box-sizing:\s*border-box;[\s\S]*?width:\s*fit-content;[\s\S]*?max-width:\s*min\(680px,\s*100%\);/,
    'the contextual send failure must be bounded by its pane rather than the global viewport',
  )
})

test('Retry is a non-submitting button with a visible keyboard focus ring', () => {
  assert.match(
    connectionStatus,
    /<button[\s\S]*?type="button"[\s\S]*?className="connection-status__retry"/,
  )
  assert.match(
    chatCss,
    /\.connection-status__retry:focus-visible\s*\{[\s\S]*?outline:\s*2px solid var\(--accent\);/,
  )
})
