// Native location tracking bridge.
//
// The Möbius mobile app injects `window.MobiusNative` into the top frame on the
// owner's own origin (never in iframes, never in a plain browser). It can keep
// tracking the device location while the app is backgrounded, but it only
// delivers positions to a page that has called `startLocation` in its current
// document, and the page is the ONLY route to the server. This module owns that
// whole page-side contract:
//
//   - feature-detect `MobiusNative`;
//   - start / stop / resume tracking and surface the OS status string. Only
//     the phone app can change tracking; other devices can only view it;
//   - receive `mobius:location` batches, dedupe them, and queue them. The
//     in-memory queue is authoritative for this document; localStorage is a
//     best-effort copy so a reload or relaunch can still upload a backlog;
//   - upload the queue to the backend in batches, retrying on failure, and drop
//     positions from the queue only once the server has accepted them;
//   - report this phone's own tracking state to the server (one record per
//     phone, so other surfaces can show whether any phone is tracking), and
//     remember in sessionStorage whether it is running in THIS app session, so
//     a reload re-arms it but a relaunch does not.
//
// Resuming is deliberately session-scoped. A reloaded page must call
// `startLocation` again to keep receiving positions, but after the owner closes
// the app the native layer stops tracking — and with Android's "Only this time"
// grant the permission is revoked too, so calling `startLocation` would put an
// unrequested permission prompt in front of them. sessionStorage survives a
// web-view reload and is empty after a relaunch, so it is the one signal that
// tells the two apart. Outside of that reload resume, `startLocation` is only
// ever called because someone tapped Start.
//
// It is a single shell-owned controller with a small getState surface; apps
// reach it through the `location.tracking` capability
// (locationTrackingProvider.js). All of its heavy lifting happens whether or
// not any UI is mounted, because positions can arrive at any time the app is
// open.

import { apiFetch } from '../api/client.js'

const QUEUE_KEY = 'mobius.location.queue.v1'
const ENABLED_KEY = 'mobius.location.enabled.v1'
const OPTIONS_KEY = 'mobius.location.options.v1'
// Recently delivered fix timestamps, so a re-delivered batch is dropped before
// it re-enters the queue (the server is idempotent too; this just avoids pointless
// uploads). Bounded ring — the app itself only retains 5000 positions.
const DELIVERED_KEY = 'mobius.location.delivered.v1'
const DELIVERED_MAX = 6000
// sessionStorage: present only while tracking was started in this app session.
const SESSION_ACTIVE_KEY = 'mobius.location.session-active.v1'
// A stable, opaque id for this phone, so the server keeps each phone's fixes
// and tracking report apart. It holds no location data and survives sign-out,
// so the phone can still correct its own tracking report later.
const DEVICE_KEY = 'mobius.location.device.v1'

const UPLOAD_CHUNK = 500
const RETRY_BASE_MS = 2000
const RETRY_MAX_MS = 60_000

const DEFAULT_OPTIONS = { background: true, interval: 10000, distance: 0 }

// The server's bounds for one fix (routes/location.py `Position`). A fix outside
// them can never be stored, so it is dropped on arrival instead of poisoning an
// upload.
const TS_MIN_MS = 1_000_000_000_000
const TS_MAX_MS = 4_102_444_800_000

// ---- storage helpers (every access is defensive: private mode, quota, etc.) --

function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const value = JSON.parse(raw)
    return value == null ? fallback : value
  } catch {
    return fallback
  }
}

// Returns whether the value was saved. Out of quota or storage denied leaves
// the in-memory state authoritative for this document; durability across
// reloads is best-effort by contract.
function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

// The session flag: set when startLocation resolves "started", cleared on stop
// or on any other result.
function hasSessionActive() {
  try { return sessionStorage.getItem(SESSION_ACTIVE_KEY) === '1' } catch { return false }
}

function setSessionActive(active) {
  try {
    if (active) sessionStorage.setItem(SESSION_ACTIVE_KEY, '1')
    else sessionStorage.removeItem(SESSION_ACTIVE_KEY)
  } catch {
    // Without sessionStorage a reload cannot resume; the owner taps Start again.
  }
}

// Keep one identity for this document even when storage is denied or full.
// Persistence across documents is best-effort; sign-out does not reset it.
let deviceId = null

function getDeviceId() {
  if (deviceId) return deviceId
  try { deviceId = localStorage.getItem(DEVICE_KEY) } catch { /* ignore */ }
  if (!deviceId) {
    deviceId = globalThis.crypto?.randomUUID?.()
      || `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`
    try { localStorage.setItem(DEVICE_KEY, deviceId) } catch { /* ignore */ }
  }
  return deviceId
}

// ---- feature detection ------------------------------------------------------

export function isNativeLocationAvailable() {
  return typeof window !== 'undefined'
    && !!window.MobiusNative
    && typeof window.MobiusNative.startLocation === 'function'
}

// ---- controller -------------------------------------------------------------

let state = {
  available: isNativeLocationAvailable(),
  // A stored "on" without this session's flag is left over from a closed app.
  enabled: readJSON(ENABLED_KEY, false) === true && hasSessionActive(),
  status: null,        // last OS status string: started | denied | disabled | unavailable | stopped
  queueLength: 0,
  // false when the last queue change could not be saved on the device, so a
  // reload now would lose the not-yet-uploaded fixes.
  queuePersisted: true,
  lastError: null,
}

let listenerBound = false
let draining = false
let drainRequested = false
let retryTimer = null
let retryDelay = RETRY_BASE_MS
let uploadSize = UPLOAD_CHUNK

// Every start, stop, live cadence change and sign-out takes the next turn. A
// native answer that arrives after a newer turn began is stale: it never writes
// state, and a stale start that left the phone recording after a newer stop is
// stopped again. `wantTracking` is what the newest turn asked for.
let turn = 0
let wantTracking = false
// Bumped when the owner's session ends, so an upload in flight at sign-out
// cannot touch the cleared queue, and set so late deliveries are not queued for
// whoever signs in next. Starting tracking again clears it.
let sessionEpoch = 0
let signedOut = false

function setState(patch) {
  state = { ...state, ...patch }
}

export function getLocationState() {
  return state
}

// ---- queue ------------------------------------------------------------------

// The queue and the recently-delivered ring live in memory for the life of the
// document and are copied to localStorage after every change. Uploads read only
// the in-memory copy, so a storage failure can neither lose a fresh fix nor
// resend an already accepted chunk.
function loadStoredArray(key) {
  const value = readJSON(key, [])
  return Array.isArray(value) ? value : []
}

let queue = loadStoredArray(QUEUE_KEY)
let delivered = loadStoredArray(DELIVERED_KEY)
setState({ queueLength: queue.length })

function setQueue(next) {
  queue = next
  const persisted = writeJSON(QUEUE_KEY, queue)
  setState({ queueLength: queue.length, queuePersisted: persisted })
}

// Normalize a raw device position to exactly the fields the backend stores,
// dropping anything malformed. latitude/longitude are mandatory; timestamp is
// the dedup identity, so a fix without a usable one cannot be stored.
function normalizePosition(raw) {
  if (!raw || typeof raw !== 'object') return null
  const lat = Number(raw.latitude)
  const lon = Number(raw.longitude)
  const ts = Number(raw.timestamp)
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) return null
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) return null
  if (!Number.isInteger(ts) || ts < TS_MIN_MS || ts > TS_MAX_MS) return null
  const opt = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v))
  return {
    latitude: lat,
    longitude: lon,
    timestamp: ts,
    accuracy: opt(raw.accuracy),
    altitude: opt(raw.altitude),
    speed: opt(raw.speed),
    heading: opt(raw.heading),
  }
}

// Enqueue a freshly received batch, deduped by timestamp against both what is
// already queued and what was recently delivered.
function enqueuePositions(rawPositions) {
  if (!Array.isArray(rawPositions) || rawPositions.length === 0) return
  const seen = new Set(delivered)
  for (const p of queue) seen.add(p.timestamp)
  const added = []
  for (const raw of rawPositions) {
    const pos = normalizePosition(raw)
    if (!pos || seen.has(pos.timestamp)) continue
    seen.add(pos.timestamp)
    added.push(pos)
  }
  if (added.length === 0) return
  // Keep the queue ordered by fix time, not arrival time.
  setQueue(queue.concat(added).sort((a, b) => a.timestamp - b.timestamp))
  void drain()
}

function markDelivered(timestamps) {
  if (!timestamps.length) return
  delivered = delivered.concat(timestamps)
  if (delivered.length > DELIVERED_MAX) {
    delivered = delivered.slice(delivered.length - DELIVERED_MAX)
  }
  writeJSON(DELIVERED_KEY, delivered)
}

// ---- upload -----------------------------------------------------------------

function scheduleRetry() {
  if (retryTimer) return
  retryTimer = setTimeout(() => {
    retryTimer = null
    void drain()
  }, retryDelay)
  retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS)
}

function clearRetry() {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
  retryDelay = RETRY_BASE_MS
}

function removeFromQueue(chunk) {
  const timestamps = new Set(chunk.map((p) => p.timestamp))
  // A concurrent enqueue may have appended while the request was in flight.
  setQueue(queue.filter((p) => !timestamps.has(p.timestamp)))
  return [...timestamps]
}

// Upload the queue in chunks, oldest first, removing each chunk only after the
// server accepts it. Coalesces concurrent requests into one in-flight drain.
//
// Fixes are normalized to the server's bounds on arrival, so a rejected (422)
// chunk is unexpected. It is bisected rather than dropped: the upload size
// halves until the rejected fix is isolated and only that one is discarded;
// the next accepted upload restores the full size.
async function drain() {
  drainRequested = true
  if (draining) return
  draining = true
  try {
    while (drainRequested) {
      drainRequested = false
      if (queue.length === 0) { clearRetry(); break }
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        scheduleRetry()
        break
      }
      const chunk = queue.slice(0, uploadSize)
      const epoch = sessionEpoch
      let res
      try {
        res = await apiFetch('/location/batch', {
          method: 'POST',
          body: JSON.stringify({ positions: chunk, device_id: getDeviceId() }),
        })
      } catch (err) {
        if (epoch !== sessionEpoch) break
        setState({ lastError: String(err?.message || err) })
        scheduleRetry()
        break
      }
      if (epoch !== sessionEpoch) break
      if (res.ok) {
        markDelivered(removeFromQueue(chunk))
        uploadSize = UPLOAD_CHUNK
        clearRetry()
        setState({ lastError: null })
        drainRequested = true // keep draining any remaining chunks
      } else if (res.status === 422) {
        if (chunk.length > 1) {
          uploadSize = Math.ceil(chunk.length / 2)
        } else {
          // This one fix can never be stored; retrying it would wedge the queue.
          removeFromQueue(chunk)
          setState({ lastError: 'A position was rejected and dropped.' })
        }
        drainRequested = true
      } else {
        // 5xx / 429 / transient: keep the chunk and back off.
        setState({ lastError: `Upload failed (${res.status}).` })
        scheduleRetry()
        break
      }
    }
  } finally {
    draining = false
  }
}

// ---- server-side tracking report ---------------------------------------------

// Report this phone's own tracking state for other surfaces to display. Part of
// the transition that decided it: a turn superseded before it publishes stays
// silent, and the decision time lets the server ignore a report that a slower
// network delivers after a newer one.
async function reportTracking(enabled, options, myTurn, { timeoutMs } = {}) {
  if (myTurn !== turn) return
  try {
    await apiFetch('/location/tracking', {
      method: 'PUT',
      timeoutMs,
      body: JSON.stringify({
        device_id: getDeviceId(),
        enabled,
        options: options || {},
        reported_at: Date.now(),
      }),
    })
  } catch {
    // Best-effort record for other surfaces; this phone's own state is local.
  }
}

// What the owner's phones last reported: on when any phone is tracking. For
// surfaces that cannot track themselves (a laptop); it never drives tracking.
export async function fetchReportedTracking() {
  try {
    const res = await apiFetch('/location/tracking')
    if (!res.ok) return null
    const body = await res.json()
    return { enabled: body?.enabled === true, options: body?.options || {} }
  } catch {
    return null
  }
}

// ---- public control surface -------------------------------------------------

function bindListener() {
  if (listenerBound || typeof window === 'undefined') return
  window.addEventListener('mobius:location', (e) => {
    if (signedOut) return
    const positions = e?.detail?.positions
    if (Array.isArray(positions)) enqueuePositions(positions)
  })
  listenerBound = true
}

function persistEnabled(enabled, options) {
  writeJSON(ENABLED_KEY, enabled)
  if (options) writeJSON(OPTIONS_KEY, options)
  setState({ enabled })
}

function savedOptions() {
  const opts = readJSON(OPTIONS_KEY, DEFAULT_OPTIONS)
  return { ...DEFAULT_OPTIONS, ...(opts && typeof opts === 'object' ? opts : {}) }
}

// Clamp caller-supplied cadence to sane bounds: 1s..1h between fixes, and a
// non-negative minimum-distance filter in metres. Unknown keys are dropped.
const MIN_INTERVAL_MS = 1000
const MAX_INTERVAL_MS = 3_600_000
function sanitizeOptions(options) {
  const out = {}
  const interval = Number(options?.interval)
  if (Number.isFinite(interval)) {
    out.interval = Math.max(MIN_INTERVAL_MS, Math.min(MAX_INTERVAL_MS, Math.round(interval)))
  }
  const distance = Number(options?.distance)
  if (Number.isFinite(distance)) {
    out.distance = Math.max(0, Math.round(distance))
  }
  return out
}

export function getTrackingOptions() {
  return savedOptions()
}

// Change the recording cadence (and/or distance filter). Only the phone app can
// change tracking: on any other device this does nothing and returns the
// unchanged options. The choice is saved, and if this phone is tracking, native
// tracking is re-armed so the new cadence takes effect live.
export async function setTrackingOptions(options) {
  if (!isNativeLocationAvailable()) return savedOptions()
  const merged = { ...savedOptions(), ...sanitizeOptions(options) }
  writeJSON(OPTIONS_KEY, merged)
  // A phone that is not tracking has nothing to re-arm or report; its next
  // start uses the saved cadence.
  if (state.status !== 'started') return merged
  const myTurn = ++turn
  wantTracking = true
  const status = await callStartLocation({ ...DEFAULT_OPTIONS, ...merged }, myTurn)
  if (status === 'started') void reportTracking(true, merged, myTurn)
  return merged
}

// Best-effort native stop for a recording nobody wants any more (a stale start,
// or sign-out). Never writes state.
function stopNativeQuietly() {
  try {
    return Promise.resolve(window.MobiusNative.stopLocation()).catch(() => {})
  } catch {
    return Promise.resolve()
  }
}

// The single place that calls MobiusNative.startLocation, so every result is
// handled the same way. "started" marks this session as tracking; anything else
// ("denied", "disabled", "unavailable", or a throw) leaves tracking stopped and
// clears the flag, and the status drives the UI's explanation. Returns null when
// a newer turn superseded this one before the phone answered.
async function callStartLocation(opts, myTurn) {
  let status
  let error = null
  try {
    status = await window.MobiusNative.startLocation(opts)
  } catch (err) {
    status = 'denied'
    error = String(err?.message || err)
  }
  if (myTurn !== turn) {
    if (status === 'started' && !wantTracking) void stopNativeQuietly()
    return null
  }
  if (error) setState({ lastError: error })
  if (status === 'started') {
    setSessionActive(true)
    setState({ status, available: true })
    return status
  }
  wantTracking = false
  setSessionActive(false)
  const wasEnabled = state.enabled
  persistEnabled(false, null)
  setState({ status, available: true })
  // If this device was tracking (a reload resume or a cadence re-arm that failed),
  // record that it has stopped so no surface keeps showing it as on.
  if (wasEnabled) void reportTracking(false, savedOptions(), myTurn)
  return status
}

// Start (or change) tracking. Returns the OS status string. Only persists the
// "enabled" preference when tracking actually started.
export async function startTracking(options = savedOptions()) {
  bindListener()
  if (!isNativeLocationAvailable()) {
    setState({ status: 'unavailable', available: false })
    return 'unavailable'
  }
  const opts = { ...DEFAULT_OPTIONS, ...options }
  const myTurn = ++turn
  wantTracking = true
  signedOut = false
  const status = await callStartLocation(opts, myTurn)
  // A newer start or stop owns the outcome now.
  if (status === null) return state.status ?? 'stopped'
  if (status === 'started') {
    persistEnabled(true, opts)
    void reportTracking(true, opts, myTurn)
    // Positions recorded while the page was gone are delivered right after this
    // call; make sure any still-queued ones from a prior session go out too.
    void drain()
  }
  return status
}

// Only the phone app can stop tracking: on any other device this changes
// nothing (the phone that is tracking keeps tracking) and reports 'unavailable'.
//
// Tracking is recorded as off only once the phone confirms it stopped. If the
// native stop fails, or answers that it is still recording, nothing is
// changed — this phone keeps uploading what it records and every surface keeps
// showing tracking on — and the call rejects with code `stop_failed`. A start
// still waiting for the phone is superseded, and stopped if it ends up started.
export async function stopTracking() {
  if (!isNativeLocationAvailable()) {
    setState({ status: 'unavailable', available: false })
    return 'unavailable'
  }
  const myTurn = ++turn
  wantTracking = false
  let status
  let failure = null
  try {
    status = await window.MobiusNative.stopLocation()
    if (status === 'started') failure = 'The phone is still recording.'
  } catch (err) {
    failure = String(err?.message || err || 'The phone did not stop recording.')
  }
  // A newer start owns the outcome now.
  if (myTurn !== turn) return state.status ?? 'stopped'
  if (failure) {
    setState({ lastError: `Stopping failed: ${failure}` })
    const error = new Error('The phone could not confirm that tracking stopped.')
    error.code = 'stop_failed'
    throw error
  }
  setSessionActive(false)
  persistEnabled(false, null)
  void reportTracking(false, savedOptions(), myTurn)
  setState({ status: status || 'stopped', lastError: null })
  return status || 'stopped'
}

// The owner's session ended (sign-out or an expired credential): stop recording
// and erase everything this phone kept for them — queued precise positions, the
// delivered ring, the tracking flags and cadence — so none of it is uploaded
// under, or shown to, whoever signs in next. In-flight native calls and uploads
// become stale. An explicit sign-out has already stopped and reported this
// phone through `stopLocationForSignOut`; when the credential simply expired,
// the report below cannot land and the phone corrects its own report the next
// time it opens. Returns once the native stop settles, so callers can await it
// before reloading.
export function endLocationSession() {
  const myTurn = ++turn
  wantTracking = false
  sessionEpoch += 1
  signedOut = true
  clearRetry()
  uploadSize = UPLOAD_CHUNK
  const recording = isNativeLocationAvailable()
    && (state.enabled === true || state.status === 'started')
  if (recording) void reportTracking(false, {}, myTurn)
  queue = []
  delivered = []
  for (const key of [QUEUE_KEY, DELIVERED_KEY, ENABLED_KEY, OPTIONS_KEY]) {
    try { localStorage.removeItem(key) } catch { /* ignore */ }
  }
  setSessionActive(false)
  setState({
    enabled: false,
    status: recording ? 'stopped' : state.status,
    queueLength: 0,
    queuePersisted: true,
    lastError: null,
  })
  return recording ? stopNativeQuietly() : Promise.resolve()
}

// Explicit sign-out, while the owner's credential still works: stop recording
// and report this phone stopped, so other surfaces stop showing it as tracking.
// `endLocationSession` then erases what the phone kept. Best effort and bounded:
// a failed, stalled, or slow stop or report never holds up signing out; the
// report is aborted at the deadline, and the phone corrects its own report the
// next time it opens.
const SIGN_OUT_DEADLINE_MS = 5000

export async function stopLocationForSignOut({ deadlineMs = SIGN_OUT_DEADLINE_MS } = {}) {
  if (!isNativeLocationAvailable()) return
  if (state.enabled !== true && state.status !== 'started') return
  const myTurn = ++turn
  wantTracking = false
  setSessionActive(false)
  persistEnabled(false, null)
  setState({ status: 'stopped' })
  let timer
  const deadline = new Promise((resolve) => { timer = setTimeout(resolve, deadlineMs) })
  try {
    await Promise.race([
      Promise.all([
        stopNativeQuietly(),
        reportTracking(false, {}, myTurn, { timeoutMs: deadlineMs }),
      ]),
      deadline,
    ])
  } finally {
    clearTimeout(timer)
  }
}

// ---- boot ---------------------------------------------------------------------
//
// Tracking is controlled only on the phone that tracks. Each phone's server
// report records what that phone last did (other surfaces display it); it
// never starts, stops, or re-paces a phone. So boot never reads the server — a
// phone acts only on its owner's taps and its own reload — and it corrects only
// its own report, never another phone's.

// Called once at shell boot. Registers the listener immediately, flushes any
// queue left from a prior session, and — if tracking was on in this app session
// (a reload) — re-arms it so the app resumes delivering positions to this new
// document. A relaunch after the app was closed starts stopped.
let initialized = false

export async function initLocationTracking() {
  if (typeof window === 'undefined' || initialized) return
  initialized = true
  bindListener()
  setState({ available: isNativeLocationAvailable() })
  // Always try to flush a leftover queue, even in a plain browser where an old
  // backlog might still be sitting in localStorage.
  void drain()

  if (typeof window !== 'undefined') {
    window.addEventListener('online', () => { clearRetry(); void drain() })
  }

  if (!isNativeLocationAvailable()) return

  // Resume only a reload within the same app session: the session flag alone
  // says this phone was tracking before the reload. A stored "on" without it
  // means the app was closed while tracking — the native side has stopped (and
  // may have lost permission) — so record it as stopped rather than calling
  // startLocation and prompting unasked.
  if (hasSessionActive()) {
    setState({ enabled: true })
    // The page is visible right after load, so an Android background start is
    // allowed here. Resume with this phone's own saved cadence.
    await startTracking(savedOptions())
    return
  }
  // A relaunch: whatever this phone last reported, it is not tracking now.
  // Correct its own report (left on if the app was closed or signed out while
  // tracking, or the stop report never landed); other phones are untouched.
  const storedOn = readJSON(ENABLED_KEY, false) === true
  persistEnabled(false, null)
  if (storedOn) setState({ status: 'stopped' })
  void reportTracking(false, savedOptions(), turn)
}
