import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const chatInputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const connectionStatus = readFileSync(new URL('../ConnectionStatus.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('footer stacks offline note → notices → rail → connection → queued → composer', () => {
  const footStart = chatView.indexOf('<div ref={footRef} className="chat__foot">')
  const composer = chatView.indexOf('<ChatInputBar', footStart)
  const foot = chatView.slice(footStart, composer)
  const rail = foot.indexOf('className="chat__build-rail"')
  const queued = foot.indexOf('<QueuedMessages')
  const connection = foot.indexOf('<ConnectionStatus')
  const offline = foot.indexOf('className="chat__offline-note"')

  assert.ok(
    footStart >= 0 && composer > footStart && rail >= 0 && queued >= 0
      && connection >= 0 && offline >= 0,
    'the complete footer stack must be present',
  )
  assert.ok(offline < connection, 'the offline explanation stacks above connection/retry')
  assert.ok(rail < connection, 'the build rail stacks above connection/retry')
  assert.ok(connection < queued, 'connection/retry stacks directly above the queued input tray')
  for (const notice of [
    'className="chat__open-app"',
    'className="chat__question-nudge"',
    'className="chat__resume-nudge"',
  ]) {
    const noticeIndex = foot.indexOf(notice)
    assert.ok(noticeIndex >= 0, `${notice} must be present in the footer`)
    assert.ok(noticeIndex < rail,
      `${notice} must stack above the build rail`)
  }
})

test('offline explanation has one owner while send failures stay in the composer', () => {
  const offlineText = "You're offline — chat needs a connection."

  assert.equal(chatView.split(offlineText).length - 1, 1)
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
  assert.match(chatView, /const canSteer = !hasPendingQuestion[\s\S]*?connectionError !== 'disconnected' && !steerBusy[\s\S]*?canFastForwardQueue/,
    'the visible composer steer action must be gated by pending QA and connection health')
  assert.match(chatView, /const canSubmitSteer = !hasPendingQuestion[\s\S]*?connectionError !== 'disconnected'[\s\S]*?!steerBusy[\s\S]*?turnActive/,
    'the composed-text keyboard steer path must be gated by pending QA and connection health too')
  assert.match(chatView, /const canRequestSteer = canSubmitSteer[\s\S]*?pendingQueue\.pendingMessages\.length > 0/,
    'the empty-composer keyboard path must share the same gate and require queued work')
})

test('connection status matches the composer column while the offline note stays compact', () => {
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
    'the compact note must be bounded by its pane rather than the global viewport',
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
