import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('accepted deferred steers leave the queue and appear inline immediately', () => {
  assert.match(
    source,
    /pendingQueue\.reserveForSteer\(consumePendingCids\)[\s\S]*?await streamSend/,
    'the tray should reserve steered rows before the request without removing them',
  )
  assert.doesNotMatch(
    source,
    /pendingQueue\.promoteManyByCid\(consumePendingCids\)/,
    'starting a steer must not mutate the durable queue merely to hide its tray rows',
  )
  const deferred = source.slice(
    source.indexOf('if (result.cut_deferred) {', source.indexOf('async function steerRowsImpl')),
    source.indexOf('} else if (', source.indexOf('if (result.cut_deferred) {', source.indexOf('async function steerRowsImpl'))),
  )
  assert.doesNotMatch(
    deferred,
    /releaseSteerReservation|hydrate|promote|cancelByCid/,
    'an accepted deferred cut should keep its reservation until the cut lands',
  )
  assert.match(
    source,
    /if \(result\?\.status !== 'steered'\)[\s\S]*?pendingQueue\.releaseSteerReservation\(consumePendingCids\)/,
    'a rejected steer must make the unchanged queue visible again',
  )
  assert.match(
    source,
    /pendingQueue\.steerReservedMessages\.map\(\(msg, i\) => \{/,
    'reserved steering rows need an immediate inline presentation',
  )
  const activeSurfaceAt = source.indexOf('{showActiveAssistantSurface && (')
  const pendingSteerAt = source.indexOf('{pendingQueue.steerReservedMessages.map')
  const footerAt = source.indexOf('<div ref={footRef} className="chat__foot">')
  assert.ok(
    activeSurfaceAt >= 0 && pendingSteerAt > activeSurfaceAt && pendingSteerAt < footerAt,
    'the provisional user row belongs after the active assistant and before the footer',
  )
  assert.match(
    source,
    /data-steer-pending="true"[\s\S]*?<MsgContent[\s\S]*?messageKey=\{dataKey\}/,
    'the provisional row uses the ordinary user-message renderer and stable cid target',
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

test('the modified-Enter submit uses one direct request and presents it inline', () => {
  assert.match(
    source,
    /const directSteer = opts\.directSteer === true && queuesBehindActiveTurn/,
    'the send path must distinguish direct steering from ordinary queueing',
  )
  assert.match(
    source,
    /directSteer\s*\? \{ directSteer: true, cid \}\s*: \{ queueOnly: true, cid \}/,
    'Cmd/Ctrl+Enter must make one direct-steer POST instead of queue then force-steer',
  )
  assert.match(
    source,
    /if \(directSteer\) \{[\s\S]*?pendingQueue\.releaseSteerReservation\(\[cid\]\)/,
    'a rejected direct steer moves the same durable row back to the queue',
  )
  assert.doesNotMatch(
    source,
    /steerAfterQueue|handleSteerOneRef/,
    'the former two-request queue-then-steer mechanism must be gone',
  )
  assert.match(
    source,
    /function handleSubmitSteer\(e\) \{[\s\S]*?if \(submitSteerInFlightRef\.current\) return[\s\S]*?submitSteerInFlightRef\.current = true[\s\S]*?doSend\(input\.trim\(\), \{ directSteer: true \}\)[\s\S]*?\.finally\(\(\) => \{ submitSteerInFlightRef\.current = false \}\)/,
    'the keyboard handler must synchronously guard the one direct request',
  )
})
