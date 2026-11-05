/**
 * Native location tracking: resuming on page load is scoped to one app session.
 *
 * A reload inside the mobile app must re-arm tracking, but a relaunch after the
 * owner closed the app must not call startLocation (that would show the OS
 * permission prompt unasked). sessionStorage is what tells the two apart.
 */
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'

const SESSION_ACTIVE_KEY = 'mobius.location.session-active.v1'
const ENABLED_KEY = 'mobius.location.enabled.v1'
const DEVICE_KEY = 'mobius.location.device.v1'
const THIS_PHONE = 'this-phone'
const OTHER_PHONE = 'other-phone'

class MemoryStorage {
  constructor(entries = {}) { this.map = new Map(Object.entries(entries)) }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null }
  setItem(key, value) { this.map.set(key, String(value)) }
  removeItem(key) { this.map.delete(key) }
}

const saved = {}
for (const key of ['window', 'document', 'localStorage', 'sessionStorage', 'fetch', 'indexedDB']) {
  saved[key] = Object.getOwnPropertyDescriptor(globalThis, key)
}

afterEach(() => {
  for (const [key, desc] of Object.entries(saved)) {
    if (desc) Object.defineProperty(globalThis, key, desc)
    else delete globalThis[key]
  }
})

function setGlobal(key, value) {
  Object.defineProperty(globalThis, key, { value, configurable: true, writable: true })
}

// Installs a fake mobile-app environment and loads a fresh controller, as a
// page load would. `session` is this web view's sessionStorage contents.
async function loadPage({
  serverEnabled = false,
  localEnabled = serverEnabled,
  otherPhoneTracking = false,
  session = {},
  startResult = 'started',
  stopResult = 'stopped',
  native = true,
  batch = () => new Response('{}', { status: 200 }),
  storage = null,
} = {}) {
  const calls = { start: [], stop: 0, puts: [], reports: [], batches: [], batchDeviceIds: [] }
  // The server keeps one tracking report per phone, newest decision wins, and
  // is "on" when any phone is.
  const server = {
    devices: {
      [THIS_PHONE]: { enabled: serverEnabled, options: {}, reported_at: 0 },
      ...(otherPhoneTracking
        ? { [OTHER_PHONE]: { enabled: true, options: { interval: 10000 }, reported_at: 0 } }
        : {}),
    },
    get enabled() { return Object.values(this.devices).some((d) => d.enabled) },
    get options() {
      return Object.values(this.devices).find((d) => d.enabled)?.options || {}
    },
  }
  const win = new EventTarget()
  if (native) {
    win.MobiusNative = {
      async startLocation(opts) {
        calls.start.push(opts)
        return typeof startResult === 'function' ? startResult(opts) : startResult
      },
      async stopLocation() {
        calls.stop += 1
        if (stopResult instanceof Error) throw stopResult
        return stopResult
      },
    }
  }
  const sessionStorage = new MemoryStorage(session)
  const localStorage = storage || new MemoryStorage({
    [DEVICE_KEY]: THIS_PHONE,
    ...(localEnabled ? { [ENABLED_KEY]: 'true' } : {}),
  })
  setGlobal('window', win)
  setGlobal('document', new EventTarget())
  setGlobal('localStorage', localStorage)
  setGlobal('sessionStorage', sessionStorage)
  setGlobal('fetch', async (url, init = {}) => {
    const path = String(url).replace(/^.*\/api/, '')
    if (path === '/location/tracking' && (init.method || 'GET') === 'PUT') {
      const body = JSON.parse(init.body)
      calls.puts.push(body.enabled)
      calls.reports.push(body)
      const stored = server.devices[body.device_id]
      if (!stored || body.reported_at >= stored.reported_at) {
        server.devices[body.device_id] = {
          enabled: body.enabled, options: body.options, reported_at: body.reported_at,
        }
      }
      return new Response('{}', { status: 200 })
    }
    if (path === '/location/batch') {
      const body = JSON.parse(init.body)
      calls.batches.push(body.positions)
      calls.batchDeviceIds.push(body.device_id)
      return batch(body.positions, body.device_id)
    }
    if (path === '/location/tracking') {
      return Response.json({ enabled: server.enabled, options: server.options })
    }
    return Response.json({})
  })
  const mod = await import(new URL(`../nativeLocation.js?t=${Math.random()}`, import.meta.url))
  return { mod, calls, server, sessionStorage, localStorage }
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

test('a reload with tracking on in this app session resumes tracking', async () => {
  const page = await loadPage({ serverEnabled: true, session: { [SESSION_ACTIVE_KEY]: '1' } })
  assert.equal(page.mod.getLocationState().enabled, true)

  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, true)
  assert.equal(state.status, 'started')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), '1')
})

test('a fresh launch with stale "on" state stays stopped and never calls startLocation', async () => {
  const page = await loadPage({ serverEnabled: true, session: {} })
  // Even before boot finishes, the UI must not show the stale "on".
  assert.equal(page.mod.getLocationState().enabled, false)

  await page.mod.initLocationTracking()
  await flush()

  assert.equal(page.calls.start.length, 0)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'stopped')
  // The stored state is corrected too, so nothing keeps claiming tracking is on.
  assert.deepEqual(page.calls.puts, [false])
  assert.equal(page.server.enabled, false)
  assert.equal(page.localStorage.getItem(ENABLED_KEY), 'false')
})

test('a fresh launch corrects this phone\'s own leftover "on" without asking the server', async () => {
  const page = await loadPage({ serverEnabled: false, localEnabled: true, session: {} })
  setGlobal('fetch', async () => { throw new Error('offline') })

  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 0)
  assert.equal(page.mod.getLocationState().enabled, false)
  assert.equal(page.localStorage.getItem(ENABLED_KEY), 'false')
})

test('tapping Start sets the session flag and Stop clears it', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  assert.equal(page.calls.start.length, 0)

  assert.equal(await page.mod.startTracking({ background: true }), 'started')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), '1')
  assert.equal(page.mod.getLocationState().enabled, true)

  assert.equal(await page.mod.stopTracking(), 'stopped')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  assert.equal(page.calls.stop, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'stopped')
})

for (const result of ['denied', 'disabled', 'unavailable']) {
  test(`a "${result}" start shows stopped with that status and clears the flag`, async () => {
    const page = await loadPage({ startResult: result, session: { [SESSION_ACTIVE_KEY]: '1' } })

    assert.equal(await page.mod.startTracking({ background: true }), result)

    const state = page.mod.getLocationState()
    assert.equal(state.enabled, false)
    assert.equal(state.status, result)
    assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  })
}

test('a reload resume that is refused ends stopped and records it', async () => {
  const page = await loadPage({
    serverEnabled: true,
    startResult: 'denied',
    session: { [SESSION_ACTIVE_KEY]: '1' },
  })

  await page.mod.initLocationTracking()
  await flush()

  assert.equal(page.calls.start.length, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'denied')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  assert.equal(page.server.enabled, false)
})

test('regaining focus after a fresh launch does not start tracking', async () => {
  const page = await loadPage({ serverEnabled: true, session: {} })
  await page.mod.initLocationTracking()
  // Simulate another phone (or a lost write) leaving the server report on.
  page.server.devices[OTHER_PHONE] = { enabled: true, options: {}, reported_at: Date.now() }

  window.dispatchEvent(new Event('focus'))
  await flush()

  assert.equal(page.calls.start.length, 0)
})

test('a device without the phone app cannot stop tracking or change its interval', async () => {
  const page = await loadPage({ native: false, serverEnabled: true })
  await page.mod.initLocationTracking()

  assert.equal(await page.mod.stopTracking(), 'unavailable')
  await page.mod.setTrackingOptions({ interval: 60_000 })
  assert.equal(await page.mod.startTracking({ background: true }), 'unavailable')
  await flush()

  assert.deepEqual(page.calls.puts, [])
  assert.equal(page.server.enabled, true)
})

test('a phone keeps tracking when the saved setting is changed elsewhere', async () => {
  const page = await loadPage({ serverEnabled: true, session: { [SESSION_ACTIVE_KEY]: '1' } })
  await page.mod.initLocationTracking()
  page.server.devices[THIS_PHONE] = { enabled: false, options: { interval: 60_000 }, reported_at: Date.now() }

  window.dispatchEvent(new Event('focus'))
  document.dispatchEvent(new Event('visibilitychange'))
  await flush()

  assert.equal(page.calls.stop, 0)
  assert.equal(page.calls.start.length, 1)
  assert.equal(page.mod.getLocationState().status, 'started')
})

test('a reload in the same app session resumes even if the saved setting says off', async () => {
  const page = await loadPage({ serverEnabled: false, session: { [SESSION_ACTIVE_KEY]: '1' } })
  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 1)
  assert.equal(page.mod.getLocationState().enabled, true)
})

// ---- upload queue and stop confirmation --------------------------------------

const T0 = 1_800_000_000_000
const fix = (i, extra = {}) => ({ latitude: 43.85, longitude: 18.41, timestamp: T0 + i * 1000, ...extra })
const deliver = (positions) => {
  globalThis.window.dispatchEvent(Object.assign(new Event('mobius:location'), { detail: { positions } }))
}
const settle = async () => { for (let i = 0; i < 20; i += 1) await flush() }

// localStorage that reads fine but refuses every write (quota full, or denied).
class FullStorage extends MemoryStorage {
  setItem() { throw new Error('QuotaExceededError') }
}

class BlockedStorage extends FullStorage {
  getItem() { throw new Error('SecurityError') }
}

for (const [label, storage, persistedId] of [
  ['writes fail', () => new FullStorage()],
  ['reads and writes fail', () => new BlockedStorage()],
  ['writes fail with an existing id', () => new FullStorage({ [DEVICE_KEY]: THIS_PHONE }), THIS_PHONE],
]) {
  test(`one device id covers tracking, upload retries and sign-out when storage ${label}`, async (t) => {
    // Model the server's (device_id, timestamp) dedupe. The first request is
    // stored but its acknowledgement is lost, so the controller must retry it.
    const stored = new Map()
    let attempts = 0
    const page = await loadPage({
      storage: storage(),
      batch: (positions, deviceId) => {
        for (const position of positions) stored.set(`${deviceId}:${position.timestamp}`, position)
        if (++attempts === 1) throw new Error('Acknowledgement lost')
        return new Response('{}', { status: 200 })
      },
    })
    t.after(() => page.mod.endLocationSession())
    await page.mod.initLocationTracking()
    assert.equal(await page.mod.startTracking(), 'started')
    await settle()
    const deviceId = page.calls.reports.at(-1).device_id
    assert.ok(deviceId)
    if (persistedId) assert.equal(deviceId, persistedId)
    assert.equal(page.server.devices[deviceId].enabled, true)

    deliver([fix(1)])
    await settle()
    assert.equal(page.mod.getLocationState().queueLength, 1)
    // Regaining connectivity retries immediately, without waiting for backoff.
    window.dispatchEvent(new Event('online'))
    await settle()
    assert.equal(attempts, 2)
    assert.equal(page.mod.getLocationState().queueLength, 0)
    assert.deepEqual(page.calls.batchDeviceIds, [deviceId, deviceId])
    assert.equal(stored.size, 1, 'the retry must not create a second server fix')

    assert.equal(await page.mod.stopTracking(), 'stopped')
    await settle()
    assert.equal(page.server.devices[deviceId].enabled, false)
    assert.equal(page.server.enabled, false, 'Stop must clear the original tracking report')

    // Clearing an owner's data must not reset this document's opaque phone id.
    await page.mod.startTracking()
    await page.mod.stopLocationForSignOut()
    await page.mod.endLocationSession()
    await page.mod.startTracking()
    await page.mod.stopTracking()
    await settle()
    assert.deepEqual([...new Set(page.calls.reports.map((r) => r.device_id))], [deviceId])
    assert.equal(page.server.enabled, false)
  })
}

test('fixes still upload once each when the device cannot save the queue', async () => {
  let accept = false
  const page = await loadPage({
    storage: new FullStorage(),
    batch: () => new Response('{}', { status: accept ? 200 : 503 }),
  })
  await page.mod.initLocationTracking()
  deliver([fix(1), fix(2)])
  await settle()
  // The upload failed, yet the fixes are still held for retry.
  assert.equal(page.mod.getLocationState().queueLength, 2)
  assert.equal(page.mod.getLocationState().queuePersisted, false)

  accept = true
  deliver([fix(3)])
  await settle()
  const uploaded = page.calls.batches.slice(1).flat().map((p) => p.timestamp)
  assert.deepEqual(uploaded, [fix(1), fix(2), fix(3)].map((p) => p.timestamp))
  assert.equal(page.mod.getLocationState().queueLength, 0)

  // A re-delivered fix is not uploaded again.
  const before = page.calls.batches.length
  deliver([fix(3)])
  await settle()
  assert.equal(page.calls.batches.length, before)
})

test('fixes outside the server bounds are dropped on arrival', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  deliver([
    fix(1, { latitude: 91 }),
    fix(2, { longitude: -180.5 }),
    { latitude: 1, longitude: 1, timestamp: 12345 },
    fix(4),
  ])
  await settle()
  assert.deepEqual(page.calls.batches.flat().map((p) => p.timestamp), [fix(4).timestamp])
})

test('a rejected upload drops only the fix the server refuses', async () => {
  const bad = fix(3).timestamp
  const page = await loadPage({
    batch: (positions) => new Response('{}', {
      status: positions.some((p) => p.timestamp === bad) ? 422 : 200,
    }),
  })
  await page.mod.initLocationTracking()
  deliver([1, 2, 3, 4, 5, 6].map((i) => fix(i)))
  await settle()
  const accepted = page.calls.batches
    .filter((b) => !b.some((p) => p.timestamp === bad))
    .flat().map((p) => p.timestamp)
  assert.deepEqual(accepted.sort(), [1, 2, 4, 5, 6].map((i) => fix(i).timestamp))
  // The refused fix was retried alone exactly once, then dropped.
  assert.equal(page.calls.batches.filter((b) => b.length === 1 && b[0].timestamp === bad).length, 1)
  assert.equal(page.mod.getLocationState().queueLength, 0)
})

for (const [label, stopResult] of [
  ['the native stop fails', new Error('bridge gone')],
  ['the phone says it is still recording', 'started'],
]) {
  test(`tracking stays on and Stop rejects when ${label}`, async () => {
    const page = await loadPage({ stopResult })
    await page.mod.initLocationTracking()
    await page.mod.startTracking({ background: true })
    page.calls.puts.length = 0

    await assert.rejects(page.mod.stopTracking(), { code: 'stop_failed' })

    const state = page.mod.getLocationState()
    assert.equal(state.enabled, true)
    assert.equal(state.status, 'started')
    assert.match(state.lastError, /Stopping failed/)
    assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), '1')
    assert.deepEqual(page.calls.puts, [])
    // Fixes the phone keeps recording are still uploaded.
    deliver([fix(1)])
    await settle()
    assert.equal(page.calls.batches.flat().length, 1)
  })
}

// ---- transitions, sign-out, and other phones ------------------------------------

function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

test('a Start the phone answers after a later Stop cannot turn tracking back on', async () => {
  const pending = deferred()
  const page = await loadPage({ startResult: () => pending.promise })
  await page.mod.initLocationTracking()

  const start = page.mod.startTracking({ background: true })
  await flush()
  assert.equal(await page.mod.stopTracking(), 'stopped')
  pending.resolve('started')
  await start
  await settle()

  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'stopped')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  assert.equal(page.server.enabled, false)
  assert.ok(!page.calls.puts.includes(true))
  // The late native start was stopped again, so the phone is not left recording.
  assert.equal(page.calls.stop, 2)
})

test('a newer Start wins over an older one still waiting for the phone', async () => {
  const first = deferred()
  let n = 0
  const page = await loadPage({ startResult: () => (n++ === 0 ? first.promise : 'started') })
  await page.mod.initLocationTracking()

  const older = page.mod.startTracking({ interval: 5000 })
  await flush()
  assert.equal(await page.mod.startTracking({ interval: 60000 }), 'started')
  first.resolve('denied')
  await older
  await settle()

  const state = page.mod.getLocationState()
  assert.equal(state.enabled, true)
  assert.equal(state.status, 'started')
  assert.equal(page.mod.getTrackingOptions().interval, 60000)
  assert.equal(page.calls.stop, 0)
})

test('opening another phone never marks the recording phone as stopped', async () => {
  // Another phone is tracking; this phone never was.
  const page = await loadPage({ otherPhoneTracking: true, session: {} })
  await page.mod.initLocationTracking()
  await settle()

  // This phone reports only itself.
  assert.deepEqual(page.calls.reports.map((r) => [r.device_id, r.enabled]), [[THIS_PHONE, false]])
  assert.equal(page.server.devices[OTHER_PHONE].enabled, true)
  assert.equal(page.server.enabled, true)
  assert.equal(page.calls.start.length, 0)
  assert.equal(page.mod.getLocationState().enabled, false)
})

test('reopening a phone left tracking clears only its own report', async () => {
  const page = await loadPage({
    serverEnabled: true, localEnabled: true, otherPhoneTracking: true, session: {},
  })
  await page.mod.initLocationTracking()
  await settle()

  assert.equal(page.server.devices[THIS_PHONE].enabled, false)
  assert.equal(page.server.devices[OTHER_PHONE].enabled, true)
  assert.equal(page.server.enabled, true)
})

test('a pace change the phone answers after a newer Start never reaches the server', async () => {
  const older = deferred()
  let n = 0
  const page = await loadPage({
    // 1: the initial Start; 2: the slow pace change; 3: the newer Start.
    startResult: () => (++n === 2 ? older.promise : 'started'),
  })
  await page.mod.initLocationTracking()
  await page.mod.startTracking({ background: true, interval: 10000 })
  await settle()

  const paceChange = page.mod.setTrackingOptions({ interval: 30000 })
  await flush()
  assert.equal(await page.mod.startTracking({ background: true, interval: 60000 }), 'started')
  older.resolve('started')
  await paceChange
  await settle()

  assert.ok(!page.calls.reports.some((r) => r.options.interval === 30000))
  assert.equal(page.server.devices[THIS_PHONE].options.interval, 60000)
})

test('ending the session stops recording and erases queued positions and tracking state', async () => {
  const page = await loadPage({ batch: () => new Response('{}', { status: 503 }) })
  await page.mod.initLocationTracking()
  await page.mod.startTracking({ background: true, interval: 30000 })
  deliver([fix(1), fix(2)])
  await settle()
  assert.equal(page.mod.getLocationState().queueLength, 2)

  await page.mod.endLocationSession()

  assert.equal(page.calls.stop, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.queueLength, 0)
  for (const key of [
    'mobius.location.queue.v1', 'mobius.location.delivered.v1', ENABLED_KEY,
    'mobius.location.options.v1',
  ]) assert.equal(page.localStorage.getItem(key), null, key)
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  // The opaque phone id stays, so this phone can still correct its own report;
  // it tried to report itself stopped on the way out.
  assert.equal(page.localStorage.getItem(DEVICE_KEY), THIS_PHONE)
  assert.equal(page.server.devices[THIS_PHONE].enabled, false)

  // Fixes the phone still delivers afterwards are not kept for the next owner.
  const before = page.calls.batches.length
  deliver([fix(3)])
  await settle()
  assert.equal(page.mod.getLocationState().queueLength, 0)
  assert.equal(page.calls.batches.length, before)
})

test('signing out through the shell cleanup erases a queued backlog', async () => {
  const page = await loadPage({
    storage: new MemoryStorage({ 'mobius.location.queue.v1': JSON.stringify([fix(1)]) }),
    batch: () => new Response('{}', { status: 503 }),
  })
  setGlobal('indexedDB', new (await import('fake-indexeddb')).IDBFactory())
  // The shell's sign-out path reaches the controller by its plain module URL.
  const controller = await import(new URL('../nativeLocation.js', import.meta.url))
  assert.equal(controller.getLocationState().queueLength, 1)
  const { clearQueryCache } = await import(new URL('../../api/client.js', import.meta.url))

  await clearQueryCache()

  assert.equal(page.localStorage.getItem('mobius.location.queue.v1'), null)
  assert.equal(controller.getLocationState().queueLength, 0)
})

test('signing out stops a tracking phone and reports it off before the credential goes', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  await page.mod.startTracking({ background: true })
  await settle()
  assert.equal(page.server.devices[THIS_PHONE].enabled, true)

  await page.mod.stopLocationForSignOut()

  assert.equal(page.calls.stop, 1)
  assert.equal(page.server.devices[THIS_PHONE].enabled, false)
  assert.equal(page.mod.getLocationState().enabled, false)
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)

  // The cleanup that follows has nothing left to stop or report.
  const reports = page.calls.reports.length
  await page.mod.endLocationSession()
  assert.equal(page.calls.stop, 1)
  assert.equal(page.calls.reports.length, reports)
})

test('signing out a phone that is not tracking sends nothing', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  await settle()
  const reports = page.calls.reports.length

  await page.mod.stopLocationForSignOut()

  assert.equal(page.calls.stop, 0)
  assert.equal(page.calls.reports.length, reports)
})

test('a stalled sign-out report gives up at its deadline and is aborted', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  await page.mod.startTracking({ background: true })
  await settle()
  let reportSignal = null
  setGlobal('fetch', (url, init = {}) => {
    reportSignal = init.signal || null
    return new Promise(() => {}) // the connection never answers
  })

  const started = Date.now()
  await page.mod.stopLocationForSignOut({ deadlineMs: 30 })

  assert.ok(Date.now() - started < 1000)
  assert.equal(page.calls.stop, 1)
  assert.equal(page.mod.getLocationState().enabled, false)
  assert.ok(reportSignal, 'the report carries an abort signal')
  // The request's own timer aborts it, releasing the connection.
  await new Promise((resolve) => setTimeout(resolve, 50))
  assert.equal(reportSignal.aborted, true)
})
