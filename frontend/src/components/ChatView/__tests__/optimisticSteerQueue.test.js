import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('optimistic steer restore only hydrates when no queue mutation won the race', () => {
  assert.match(
    source,
    /queueAfterOptimisticPromote = pendingQueue\.pendingMessagesRef\.current/,
    'handleSteer should snapshot the queue array immediately after optimistic promote',
  )
  assert.match(
    source,
    /function restoreOptimisticSteerQueue|const restoreOptimisticSteerQueue = \(\) =>/,
    'handleSteer should route failed optimistic restores through a helper',
  )
  const restoreHelper = source.indexOf('function restoreOptimisticSteerQueue')
  // Indent-agnostic: the steer core's nesting depth changed when per-row
  // steer extracted it out of handleSteer; the contract is only that a try
  // block FOLLOWS the helper declaration (so catch can call it).
  const guardedRequest = source.slice(restoreHelper).search(/\n\s+try \{/)
  assert.ok(
    restoreHelper >= 0 && guardedRequest > 0,
    'the restore helper must be declared outside the request try block so catch can call it',
  )
  assert.match(
    source,
    // The snapshot identifier is whatever the steer core names it (it became
    // fullConfirmedSnapshot when per-row steer landed) — the contract is the
    // identity check gating a preserveMissing hydrate, not the name.
    /pendingQueue\.pendingMessagesRef\.current === queueAfterOptimisticPromote[\s\S]*?pendingQueue\.hydrate\(\w+, \{ preserveMissing: true \}\)/,
    'the restore helper must hydrate the stale snapshot only if the queue array identity is unchanged',
  )
})

test('Stop serializes behind an in-flight steer; steer bails under a committed Stop', () => {
  // The Stop×steer race (review 2026-07-17): Stop snapshotting mid-steer
  // loses the optimistically-hidden rows on a not_steered resolution. The
  // contract is two-sided — handleStop awaits steerInFlightRef (bounded)
  // BEFORE its queue snapshot, and steerRows refuses to start once a Stop
  // owns the teardown.
  const stopIdx = source.indexOf('async function handleStop()')
  const awaitIdx = source.indexOf('steerInFlightRef.current', stopIdx)
  const snapshotIdx = source.indexOf('Snapshot the queue before doing anything destructive', stopIdx)
  assert.ok(stopIdx >= 0 && awaitIdx > stopIdx && snapshotIdx > awaitIdx,
    'handleStop must await the in-flight steer before snapshotting the queue')
  assert.match(
    source,
    /async function steerRows\(steerRowsList\) \{[\s\S]*?if \(handlingStopRef\.current\) return/,
    'steerRows must bail when a Stop has already committed to the teardown',
  )
})

test('the foot stack hides only on the TERMINAL disconnect, not retrying blips', () => {
  // 'retrying' is a ~300ms transparent auto-reconnect; gating on it would
  // blank and pop the rail/tray on every mobile blip (review 2026-07-17).
  assert.match(
    source,
    /connectionError !== 'disconnected' && \(/,
    'the foot gate must key on the terminal disconnected state only',
  )
  assert.doesNotMatch(
    source,
    /\{!connectionError && \(\s*<>/,
    'the broad !connectionError gate must not return',
  )
})

test('a steer request disables sibling row actions until it settles', () => {
  assert.match(source, /const \[steerBusy, setSteerBusy\] = useState\(false\)/)
  assert.match(source, /handlingSteerRef\.current = true\s+setSteerBusy\(true\)/)
  assert.match(source, /handlingSteerRef\.current = false\s+setSteerBusy\(false\)/)
  assert.match(source, /steerBusy=\{steerBusy\}/,
    'the queued tray should receive the in-flight state for its row buttons')
})

test('the modified-Enter submit waits for durability, then reuses per-row steer', () => {
  const refDeclaration = source.indexOf('const handleSteerOneRef = useRef(null)')
  const doSendDeclaration = source.indexOf('const doSend = useCallback')
  assert.ok(
    refDeclaration >= 0 && refDeclaration < doSendDeclaration,
    'the stable doSend callback must reach the current steer implementation through a ref',
  )
  assert.match(
    source,
    /pendingQueue\.confirmQueued\(cid,[\s\S]*?else if \(opts\.steerAfterQueue\) \{[\s\S]*?await handleSteerOneRef\.current\?\.\(cid\)/,
    'the composed message must be server-confirmed before the existing row steer consumes it',
  )
  assert.doesNotMatch(
    source,
    /else if \(opts\.steerAfterQueue\) \{[\s\S]*?await handleSteerOne\(cid\)/,
    'doSend must not capture a render-local steer function across the queue request',
  )
  const steerOneDeclaration = source.indexOf('async function handleSteerOne(cid)')
  const currentAssignment = source.indexOf('handleSteerOneRef.current = handleSteerOne')
  assert.ok(
    steerOneDeclaration >= 0 && currentAssignment > steerOneDeclaration,
    'each render must publish the current per-row steer implementation',
  )
  assert.match(
    source,
    /function handleSubmitSteer\(e\) \{[\s\S]*?if \(submitSteerInFlightRef\.current\) return[\s\S]*?submitSteerInFlightRef\.current = true[\s\S]*?doSend\(input\.trim\(\), \{ steerAfterQueue: true \}\)[\s\S]*?\.finally\(\(\) => \{ submitSteerInFlightRef\.current = false \}\)/,
    'the keyboard handler must synchronously guard the full queue-to-steer operation',
  )
})
