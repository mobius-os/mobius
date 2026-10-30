/* The passive block session controller binds every app report to one
   document generation and lets exactly one owner event be in flight. */
import assert from 'node:assert/strict'
import test, { mock } from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useAppBlockSession, { CONFIRM_ARM_MS } from '../useAppBlockSession.js'
import { appBlockFromToken } from '../../markdown/appBlock.js'
import { createBlockDocuments, forwardBlockState } from '../../../AppCanvas/appBlockSessionHost.js'
import { appBlockRoute, passiveBlockMode } from '../../markdown/blockOwnership.js'

const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
  app: 'example', intent: 'open:a', title: 'Fix', interaction: 'inline',
  action: { label: 'Send all', intent: 'send-batch:sel-1' },
  items: [{ title: 'Row', intent: 'open:b', action: { label: 'Send', intent: 'send:b' } }] }) })

/* Like PassiveAppBlock, each observation names the showing it was made for:
   the one committed when the test supplied it. A new showing is unseen until
   the test observes again (`h.observe()`), as an IntersectionObserver reports
   only after the new layout. */
function renderObserved(initial) {
  let committed = null
  let lastProps = null
  let observation = { showing: null }
  const h = renderHook(props => {
    if (props !== lastProps) {
      lastProps = props
      observation = { showing: committed, confirm: props.confirmVisible === true, end: props.listEndVisible === true }
    }
    const { confirmVisible: _c, listEndVisible: _l, ...rest } = props
    // The unkeyed booleans are what 5272c66 read; they show the old head
    // borrowing a stale observation. The keyed views are what counts now.
    const session = useAppBlockSession({ ...rest,
      confirmVisible: observation.confirm, listEndVisible: observation.end,
      confirmView: { showing: observation.showing, visible: observation.confirm },
      listEndView: { showing: observation.showing, visible: observation.end } })
    committed = session.showing
    return session
  }, initial)
  h.observe = () => h.rerender({ ...lastProps })
  return h
}

function mounted(near = true) {
  const h = renderObserved( { block, near, confirmVisible: true, listEndVisible: true })
  const raw = (fields, generation = h.result.current.generation) => h.result.current.onBlockState({
    type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [{ key: 'send-batch:sel-1', label: 'Send all' }, { key: 'send:b', label: 'Send' }], ...fields,
  }, { generation })
  // A report, then the observers report the new layout with the same visibility.
  const report = (...args) => { raw(...args); h.observe() }
  return { h, report, raw, capability: (supported, generation, reset = true) => h.result.current.onBlockCapability(supported, { generation, reset }) }
}

test('the session envelope names every block action and no state is shown before a report', () => {
  const { h, capability, report } = mounted()
  assert.deepEqual(h.result.current.blockSession.actions.map(action => action.key), ['send-batch:sel-1', 'send:b'])
  assert.equal(h.result.current.frameMounted, true)
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false, 'no event before the document negotiates')
  capability(true, 1)
  report({})
  assert.equal(h.result.current.state.actions.length, 2)
})

test('a double click before acknowledgement sends one event; only its exact ack releases the lock', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
  const first = h.result.current.event
  assert.equal(first.generation, 1)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), false)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false)
  assert.equal(h.result.current.event, first)
  report({ ackNonce: 'not-the-reserved-nonce' })
  assert.equal(h.result.current.awaitingAck, true)
  report({ ackNonce: first.nonce, checkpoint: { id: '', data: 'malformed' } })
  assert.equal(h.result.current.awaitingAck, true, 'an invalid report cannot acknowledge')
  report({ ackNonce: first.nonce })
  assert.equal(h.result.current.awaitingAck, false)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'cancel'), true)
  assert.notEqual(h.result.current.event.nonce, first.nonce)
  assert.equal(h.result.current.dispatch('unknown:key', 'activate'), false)
})

test('state and acknowledgement from another document generation are ignored', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  h.result.current.dispatch('send:b', 'activate')
  const sent = h.result.current.event
  capability(null, 2)
  // A document reload discards idle controls and the unacknowledged activate:
  // activate may not cause an effect (only confirm may), so nothing is unresolved.
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.event, null)
  report({ ackNonce: sent.nonce, summary: 'stale' }, 1)
  assert.equal(h.result.current.state, null)
  capability(true, 2, false)
  report({ summary: 'stale document' }, 1)
  assert.equal(h.result.current.state, null)
  report({ summary: 'current' }, 2)
  assert.equal(h.result.current.state.summary, 'current')
})

test('a reload during an unacknowledged Confirm holds ownership instead of offering controls', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    report({ actions: [{ key: 'send:b', confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }] })
    mock.timers.tick(CONFIRM_ARM_MS)
  } finally { mock.timers.reset() }
  assert.equal(h.result.current.dispatch('send:b', 'confirm'), true)
  capability(null, 2)
  const held = h.result.current.state
  assert.equal(held.recoveryPending, true)
  assert.ok(held.actions.every(action => action.disabled && !action.confirming))
  assert.equal(h.result.current.retained, true)
})

test('releasing an idle frame offscreen discards its negotiation; re-entry waits for fresh state', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  assert.ok(h.result.current.state)
  h.rerender({ block, near: false })
  assert.equal(h.result.current.frameMounted, false)
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.supported, null)
  h.rerender({ block, near: true })
  assert.equal(h.result.current.frameMounted, true)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false, 'stale controls cannot act during renegotiation')
  report({}, 1)
  assert.equal(h.result.current.state, null, 'the released document cannot report again')
})

test('an unresolved owner stays mounted offscreen', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({ retain: true, checkpoint: { id: 'c1', data: 'x' } })
  h.rerender({ block, near: false })
  assert.equal(h.result.current.frameMounted, true)
  assert.deepEqual(h.result.current.blockSession.checkpoint, { id: 'c1', data: 'x' })
  assert.equal(h.result.current.blockSession.retain, true)
})

test('an unsupported revision unmounts the hidden frame and stays static', () => {
  const { h, capability } = mounted()
  capability(false, 1)
  assert.equal(h.result.current.supported, false)
  assert.equal(h.result.current.frameMounted, false)
  h.rerender({ block, near: false })
  h.rerender({ block, near: true })
  assert.equal(h.result.current.supported, false, 'scrolling does not remount an unsupported session')
  assert.equal(h.result.current.frameMounted, false)
})

test('through the AppCanvas route, an old document cannot resurrect Confirm after a same-iframe reload', () => {
  const { h, capability } = mounted()
  const documents = createBlockDocuments()
  const deliver = message => forwardBlockState({ message: { type: 'moebius:app-block-state',
    sessionId: h.result.current.sessionId, ...message }, session: h.result.current.blockSession,
  document: documents.get('v1'), isLive: true, onState: h.result.current.onBlockState })
  const confirming = { actions: [{ key: 'send:b', confirming: true, confirmation: [{ title: 'Old target', facts: [] }] }] }
  const old = documents.load('v1')
  documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: old.generation })
  capability(true, old.generation)
  assert.equal(deliver({ generation: old.generation, ...confirming }), true)
  h.result.current.dispatch('send:b', 'cancel')
  const cancel = h.result.current.event
  deliver({ generation: old.generation, actions: [{ key: 'send:b', label: 'Send' }], ackNonce: cancel.nonce })
  assert.equal(h.result.current.state.actions[0].confirming, false)

  // Reload: same window, new document. Its predecessor's queued reports follow.
  const next = documents.load('v1')
  capability(null, next.generation)
  assert.equal(documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: old.generation }), null,
    'the old mount report does not negotiate for the new document')
  documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: next.generation })
  capability(true, next.generation, false)
  for (const generation of [old.generation, undefined, next.generation + 1]) {
    assert.equal(deliver({ generation, ...confirming, ackNonce: cancel.nonce }), false)
  }
  assert.equal(h.result.current.state, null, 'no old Confirm and no old acknowledgement')
  assert.equal(deliver({ generation: next.generation, actions: [{ key: 'send:b', label: 'Send' }] }), true)
  assert.equal(h.result.current.state.actions[0].confirming, false)
})

const confirmList = title => ({ actions: [{ key: 'send-batch:sel-1', confirming: true, confirmation: [{ title, facts: [] }] }] })

test('two quick clicks on the action deliver no confirm: Confirm arms only after its list was shown', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report({})
    // First click: activate. The app acknowledges and asks for confirmation at once.
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
    report({ ackNonce: h.result.current.event.nonce, ...confirmList('Target') })
    // Second click lands where Confirm now is, tens of milliseconds later.
    mock.timers.tick(40)
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    assert.equal(h.result.current.event, null, 'no confirm reached the app')
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true)
    assert.equal(h.result.current.dispatch('send:b', 'confirm'), false, 'only the shown action can confirm')
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

test('replacing the confirmation contents re-arms Confirm', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report(confirmList('First'))
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true)
    report(confirmList('Swapped'))
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    mock.timers.tick(CONFIRM_ARM_MS - 1)
    assert.equal(h.result.current.confirmArmed, false)
    // The same list reported again does not restart the delay.
    report(confirmList('Swapped'))
    mock.timers.tick(1)
    assert.equal(h.result.current.confirmArmed, true)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

// Arming belongs to one continuous visible showing. Each sequence first arms
// list A, then shows it again some other way: a fresh 500 ms is required.
for (const mode of ['repeated activation', 'A-B-A', 'reload', 'changed offscreen', 'document hidden']) {
  test(`a previously armed list must earn a fresh visible delay: ${mode}`, () => {
    mock.timers.enable({ apis: ['setTimeout'] })
    try {
      const { h, capability, report } = mounted()
      capability(true, 1)
      report(confirmList('A'))
      mock.timers.tick(CONFIRM_ARM_MS)
      assert.equal(h.result.current.confirmArmed, true)
      if (mode === 'repeated activation') {
        // Not now, then a double-click on the action that returns the same list.
        assert.equal(h.result.current.dispatch('send-batch:sel-1', 'cancel'), true)
        report({ ackNonce: h.result.current.event.nonce })
        assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
        report({ ...confirmList('A'), ackNonce: h.result.current.event.nonce })
        mock.timers.tick(40)
      } else if (mode === 'A-B-A') {
        report(confirmList('B'))
        mock.timers.tick(40)
        report(confirmList('A'))
      } else if (mode === 'reload') {
        capability(null, 2)
        capability(true, 2, false)
        report(confirmList('A'), 2)
      } else if (mode === 'changed offscreen') {
        report({ ...confirmList('A'), retain: true })
        h.rerender({ block, near: false, confirmVisible: false, listEndVisible: false })
        report({ ...confirmList('Changed while unseen'), retain: true })
        mock.timers.tick(CONFIRM_ARM_MS)
        h.rerender({ block, near: true, confirmVisible: true, listEndVisible: true })
      } else {
        h.rerender({ block, near: true, confirmVisible: false, listEndVisible: false })
        mock.timers.tick(CONFIRM_ARM_MS)
        h.rerender({ block, near: true, confirmVisible: true, listEndVisible: true })
      }
      assert.equal(h.result.current.confirmArmed, false)
      assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
      assert.equal(h.result.current.event?.event === 'confirm', false, 'no confirm reached the app')
      mock.timers.tick(CONFIRM_ARM_MS)
      assert.equal(h.result.current.confirmArmed, true, 'the current showing arms after its own delay')
      assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
    } finally { mock.timers.reset() }
  })
}

test('a report that swaps the list disarms before the next render', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report(confirmList('A'))
    mock.timers.tick(CONFIRM_ARM_MS)
    const { dispatch, onBlockState, sessionId } = h.result.current
    // Call the callbacks captured before the swap re-renders.
    onBlockState({ type: 'moebius:app-block-state', sessionId, ...confirmList('B') }, { generation: 1 })
    assert.equal(dispatch('send-batch:sel-1', 'confirm'), false)
  } finally { mock.timers.reset() }
})

test('a Confirm press spends its showing: an acknowledgement does not allow a second confirm', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report(confirmList('Same target'))
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
    const first = h.result.current.event
    assert.equal(h.result.current.confirmArmed, false, 'the press disarms before any report')
    report({ ...confirmList('Same target'), ackNonce: 'wrong' })
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    // The app acknowledges but still reports the same list; a second click 40 ms later.
    report({ ...confirmList('Same target'), ackNonce: first.nonce })
    mock.timers.tick(40)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    assert.equal(h.result.current.event, null, 'no second confirm reached the app')
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true, 'only a new showing re-arms, after its own delay')
  } finally { mock.timers.reset() }
})

test('Confirm arms only after the list end has been on screen during this showing, and Confirm is on screen', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const h = renderObserved( { block, near: true, confirmVisible: true, listEndVisible: false })
    const report = fields => h.result.current.onBlockState({ type: 'moebius:app-block-state',
      sessionId: h.result.current.sessionId, ...fields }, { generation: 1 })
    h.result.current.onBlockCapability(true, { generation: 1, reset: true })
    // The action sits at the bottom edge: Confirm is on screen, the list below it is not.
    report(confirmList('Twelve targets'))
    mock.timers.tick(CONFIRM_ARM_MS * 4)
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    // The reader scrolls the list's end into view; Confirm leaves the top.
    h.rerender({ block, near: true, confirmVisible: false, listEndVisible: true })
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, false, 'Confirm itself must be on screen')
    // Back up to Confirm: the end stays seen for this showing; a fresh delay follows.
    h.rerender({ block, near: true, confirmVisible: true, listEndVisible: false })
    mock.timers.tick(CONFIRM_ARM_MS - 1)
    assert.equal(h.result.current.confirmArmed, false)
    mock.timers.tick(1)
    assert.equal(h.result.current.confirmArmed, true)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

test('an unsupported verdict belongs to one applied revision; a newer revision negotiates again', () => {
  const h = renderObserved( { block, near: true, revision: 'r1' })
  h.result.current.onBlockCapability(false, { generation: 1, reset: true })
  assert.equal(h.result.current.supported, false)
  assert.equal(h.result.current.frameMounted, false)
  h.rerender({ block, near: true, revision: 'r1' })
  assert.equal(h.result.current.supported, false, 'the same revision stays static')
  h.rerender({ block, near: true, revision: 'r2' })
  assert.equal(h.result.current.supported, null)
  assert.equal(h.result.current.frameMounted, true, 'the new revision gets a hidden frame to negotiate')
})

test('a retained session keeps its unsupported negotiation across a revision change', () => {
  const h = renderObserved( { block, near: true, revision: 'r1' })
  h.result.current.onBlockCapability(true, { generation: 1, reset: true })
  h.result.current.onBlockState({ type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [], retain: true, checkpoint: { id: 'c1', data: 'x' } }, { generation: 1 })
  h.result.current.onBlockCapability(false, { generation: 2, reset: true })
  assert.equal(h.result.current.retained, true)
  h.rerender({ block, near: true, revision: 'r2' })
  assert.equal(h.result.current.supported, false)
  assert.equal(h.result.current.frameMounted, true, 'the unresolved owner keeps its frame')
  assert.deepEqual(h.result.current.blockSession.checkpoint, { id: 'c1', data: 'x' })
})

test('a replacement list cannot borrow the old sentinel\'s intersection (round-3 review)', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report, raw } = mounted()
    capability(true, 1)
    report(confirmList('Single old item'))
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true)
    // 256 new items push the same sentinel far below the fold. The old
    // "visible" still names the previous showing until the observer reports.
    raw({ actions: [{ key: 'send-batch:sel-1', confirming: true,
      confirmation: Array.from({ length: 256 }, (_, i) => ({ title: `Unseen new ${i}`, facts: [] })) }] })
    mock.timers.tick(CONFIRM_ARM_MS * 2)
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false, 'the new list end was never observed on screen')
    // The observer reports the new position: Confirm visible, the list end not.
    h.rerender({ block, near: true, confirmVisible: true, listEndVisible: false })
    mock.timers.tick(CONFIRM_ARM_MS * 2)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    // Only once the new end is seen, and then Confirm, does it arm.
    h.rerender({ block, near: true, confirmVisible: true, listEndVisible: true })
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

for (const unresolved of ['busy after Confirm', 'unknown outcome']) {
  test(`withdrawn admission mid-session keeps the unresolved outcome and offers no new action: ${unresolved}`, () => {
    mock.timers.enable({ apis: ['setTimeout'] })
    try {
      const { h, capability, report } = mounted()
      capability(true, 1)
      report(confirmList('Publish'))
      mock.timers.tick(CONFIRM_ARM_MS)
      assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
      const nonce = h.result.current.event.nonce
      report(unresolved === 'busy after Confirm'
        ? { ackNonce: nonce, retain: true, actions: [{ key: 'send-batch:sel-1', busy: true, disabled: true }] }
        : { ackNonce: nonce, retain: true, checkpoint: { id: 'attempt-1', data: '{"phase":"unknown"}' } })
      assert.equal(h.result.current.retained, true)
      // An applied update drops the capability: AppBlock keeps this session, held.
      h.rerender({ block, near: true, confirmVisible: true, listEndVisible: true, admitted: false })
      assert.equal(h.result.current.retained, true, 'the unresolved outcome is not dropped')
      assert.equal(h.result.current.frameMounted, true, 'its document keeps settling')
      if (unresolved === 'unknown outcome') {
        assert.deepEqual(h.result.current.blockSession.checkpoint, { id: 'attempt-1', data: '{"phase":"unknown"}' })
      }
      // The app may still report; no new owner event can be sent.
      report({ ...confirmList('Publish again'), retain: true })
      mock.timers.tick(CONFIRM_ARM_MS * 2)
      for (const kind of ['activate', 'confirm', 'cancel']) {
        assert.equal(h.result.current.dispatch('send-batch:sel-1', kind), false, `${kind} after admission is withdrawn`)
      }
    } finally { mock.timers.reset() }
  })
}

test('the first retain report and the admission withdrawal in the same tick never drop the session', () => {
  // AppBlock and PassiveAppBlock's decisions, made from the same render's state.
  const h = renderObserved({ block, near: true, confirmVisible: true, listEndVisible: true, admitted: true })
  const decide = (admitted, session) => ({
    route: appBlockRoute({ requested: true, installed: true, admitted, started: true }),
    mode: passiveBlockMode({ admitted, supported: session.supported, near: true, granted: true, retained: session.retained, staticOpen: false }),
  })
  h.result.current.onBlockCapability(true, { generation: 1, reset: true })
  h.result.current.onBlockState({ type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [{ key: 'send-batch:sel-1', label: 'Ready' }] }, { generation: 1 })
  assert.deepEqual(decide(true, h.result.current), { route: 'passive', mode: 'passive' })
  // Co-delivered: the app's first retain + checkpoint report, then the app-list
  // refresh that withdraws admission, before any effect can mirror state.
  h.result.current.onBlockState({ type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [{ key: 'send-batch:sel-1', label: 'Ready' }], retain: true, checkpoint: { id: 'race', data: 'unknown' } }, { generation: 1 })
  h.rerender({ block, near: true, confirmVisible: true, listEndVisible: true, admitted: false })
  assert.deepEqual(decide(false, h.result.current), { route: 'passive', mode: 'passive' }, 'held, not a fresh static open')
  assert.deepEqual(h.result.current.blockSession.checkpoint, { id: 'race', data: 'unknown' })
  assert.equal(h.result.current.frameMounted, true)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), false)
  // When the outcome settles, the same owner shows the static form.
  h.result.current.onBlockState({ type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [{ key: 'send-batch:sel-1', label: 'Ready' }], retain: false, checkpoint: null, checkpointAck: 'race' }, { generation: 1 })
  assert.deepEqual(decide(false, h.result.current), { route: 'passive', mode: 'static' })
})

test('the session init carries the parsed item intents, in card order, intents only', () => {
  const card = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'example', intent: 'review:sel-1', title: 'Ready', interaction: 'inline',
    action: { label: 'Publish all', intent: 'send-batch:sel-1' },
    items: [
      { title: 'B change', intent: 'review:b', facts: [{ label: 'Project', value: 'example/web' }] },
      { title: 'Bad', intent: 'not an intent' },
      { title: 'x'.repeat(241), intent: 'review:too-long-title' },
      { title: 'A change', intent: 'review:a' },
      { title: 'B again', intent: 'review:b' },
    ] }) })
  const h = renderObserved({ block: card, near: true })
  assert.deepEqual(h.result.current.blockSession.items, [{ intent: 'review:b' }, { intent: 'review:a' }])
  assert.deepEqual(h.result.current.blockSession.actions.map(action => action.key), ['send-batch:sel-1'])
  const bare = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'example', intent: 'open:a', title: 'Bare', interaction: 'inline', action: { label: 'Go', intent: 'go:a' } }) })
  assert.deepEqual(renderObserved({ block: bare, near: true }).result.current.blockSession.items, [])
})
