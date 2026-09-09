import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const chatInputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const streamConnection = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
const connectionStatus = readFileSync(new URL('../ConnectionStatus.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const scrollMode = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
const shell = readFileSync(new URL('../../Shell/Shell.jsx', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')
const systemStream = readFileSync(new URL('../../../hooks/useSystemEventStream.js', import.meta.url), 'utf8')
const settingsView = readFileSync(new URL('../../SettingsView/SettingsView.jsx', import.meta.url), 'utf8')

test('only transient nudges float above the measured rail → connection → queued → composer stack', () => {
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
  assert.ok(
    transientLane > floatingActions
      && offscreenNudges > transientLane,
    'every transient footer action must stay in the one short-lived lane',
  )
  assert.doesNotMatch(
    foot,
    /ContributionReviewCard|contrib-card-stack/,
    'durable contribution state must not persist above the composer',
  )
  assert.match(
    chatCss,
    /\.chat__floating-actions\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?bottom:\s*calc\(100% \+ var\(--chat-foot-card-gap\)\);[\s\S]*?pointer-events:\s*none;/,
    'transient-only actions must stay clear of the composer and outside measured footer flow',
  )
  assert.doesNotMatch(chatCss, /\.chat__floating-actions:has\(> \.contrib-card-stack\)/)
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
  assert.doesNotMatch(chatView, /className="chat__open-app"/)
  assert.doesNotMatch(chatCss, /\.chat__open-app(?:-btn)?\s*\{/)
  assert.doesNotMatch(
    scrollMode,
    /floatingAction|floatingActions/,
    'floating cards must not add or retain transcript spacer height',
  )
})

test('the shell is the one persistent connection owner while send failures stay contextual', () => {
  assert.match(shell, /ReachabilityPhase\.CHECKING[\s\S]*?'Reconnecting…'/)
  assert.match(shell, /ReachabilityPhase\.OFFLINE \? 'Offline'/)
  // The one dot now covers a planned restart too: a single element gated on
  // (reachabilityLabel || restartPending) so a drain and the process-down window
  // never render two indicators. See restartStore.
  assert.match(shell, /const connectionStatusLabel = reachabilityLabel[\s\S]*?restartPending \? 'Restarting…'/)
  assert.match(shell, /\{connectionStatusLabel && \([\s\S]*?className="shell__connection-status"[\s\S]*?shell__sr-only/)
  assert.doesNotMatch(chatView, /You're offline — chat needs a connection\./)
  assert.doesNotMatch(chatInputBar, /You're offline — chat needs a connection\./)
  assert.match(
    chatInputBar,
    /\{sendFailure && \([\s\S]*?chat__offline-note--error[\s\S]*?\{sendFailure\}/,
    'moving the persistent offline state must not remove a failed-send explanation',
  )
})

test('the composer accepts an offline send so it can queue in the durable outbox', () => {
  // The primary Send button must NOT gate on `offline`: an offline tap has to
  // reach doSend so the send is recorded in the durable outbox (chatOutbox) and
  // auto-replays on reconnect, exactly like the Enter path (canSubmit ignores
  // offline). Gating it on offline silently swallowed offline taps.
  assert.match(
    chatInputBar,
    /aria-label="Send"[\s\S]{0,700}?disabled=\{hasUploading \|\| submissionBlocked\}/,
    'the Send button must gate only on uploads / provider-switch, not offline',
  )
  assert.doesNotMatch(
    chatInputBar,
    /disabled=\{hasUploading \|\| offline \|\| submissionBlocked\}/,
    'the old offline-gated Send button swallowed offline taps',
  )
  assert.match(
    chatInputBar,
    /const canSubmit = !submissionBlocked && !questionBlocked/,
    'the keyboard submit path must stay offline-agnostic so the two align',
  )
  const sendFailure = readFileSync(new URL('../sendFailure.js', import.meta.url), 'utf8')
  assert.match(
    sendFailure,
    /queued and will send when you reconnect/,
    'the offline copy must promise the durable auto-replay, not a manual resend',
  )
  assert.match(shell, /useOutboxDrain\(\)/)
  assert.match(
    streamConnection,
    /outboxRetained = await enqueueIntent\([\s\S]*?err\.outboxRetained = true/,
    'automatic-replay copy must be gated by the successful durable write',
  )
})

test('retained sends move into the tray while authoritative failures restore the draft', () => {
  const freshStart = chatView.indexOf('// FRESH SEND PATH: no active turn, no queue.')
  const silentStart = chatView.indexOf('const doSendSilent = useCallback')
  const freshSend = chatView.slice(freshStart, silentStart)
  assert.match(
    freshSend,
    /const keepQueued = shouldKeepQueuedAfterSendFailure[\s\S]*?pendingQueue\.add\(\{ \.\.\.queuedUserMsg, queued: true \}, \{ inFlight: false \}\)[\s\S]*?clearFailedAttempt\(\)/,
    'a retained fresh send must leave the composer empty and own one queued cid row',
  )
  assert.match(
    freshSend,
    /else \{[\s\S]*?rememberFailedAttempt\(failedAttempt\)[\s\S]*?restoreComposerAfterFailedSend\(\)/,
    'only the non-retained branch may restore the composer',
  )
  assert.match(
    chatView,
    /const silentUserMsg[\s\S]*?shouldKeepQueuedAfterSendFailure[\s\S]*?const \{ hidden: _hidden, \.\.\.queuedAnswerMsg \} = silentUserMsg[\s\S]*?pendingQueue\.add\(\{ \.\.\.queuedAnswerMsg, queued: true \}, \{ inFlight: false \}\)[\s\S]*?return true/,
    'a retained question answer must stay visibly queued for outbox delivery',
  )
})

test('restart events verify shared connectivity so recovery generation advances', () => {
  assert.match(
    shell,
    /ev\.type === 'server_restarting'[\s\S]*?setRestartPending\(\)[\s\S]*?void verifyConnectivity\(\)/,
    'the restart edge must enter CHECKING before the new server answers',
  )
  assert.match(
    streamConnection,
    /getRecoverySnapshot[\s\S]*?subscribeRecovery[\s\S]*?retryCount\.current = 0[\s\S]*?wantsReconnectRef\.current[\s\S]*?connectRef\.current\?\.\(true\)/,
    'the stream hook must reopen a latched stream on a recovery generation',
  )
  assert.doesNotMatch(
    chatView,
    /useEffect\(\(\) => \{\s*if \(hidden\) return\s*let cancelled = false\s*let observedRecoveryGeneration/,
    'recovery subscription must remain active for hidden retained panes',
  )
})

test('credential expiry preserves principal-bound intent while explicit logout wipes it', () => {
  assert.match(
    apiClient,
    /function clearOwnerClientState\(\{ preserveChatOutbox \}\)/,
  )
  assert.match(
    apiClient,
    /preserveChatOutbox[\s\S]*?\? \[\][\s\S]*?: \[clearChatOutbox\(\)\]/,
    'explicit cleanup must clear through the live outbox store, not a blocked deleteDatabase',
  )
  assert.match(
    apiClient,
    /status === 401[\s\S]*?await clearExpiredOwnerSession\(\)/,
  )
  assert.match(
    systemStream,
    /res\.status === 401[\s\S]*?await clearExpiredOwnerSession\(\)/,
  )
  assert.match(
    settingsView,
    /await clearExplicitOwnerSession\(/,
    'owner-invoked logout must use the explicit session owner that performs the full outbox wipe',
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
