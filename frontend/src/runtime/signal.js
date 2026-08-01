
// ── App analytics: window.mobius.signal() (design §3) ──────────────
//
// Fire-and-forget telemetry for Reflection. Events receive stable client IDs,
// buffer briefly in memory, then enter a dedicated bounded IndexedDB queue
// that cannot block or upgrade the app's user-data outbox. The server appends them to the
// platform activity stream. This preserves simultaneous-tab and offline events;
// the old whole-file signals.jsonl overwrite lost one tab's batch.
//
// Placement note: this block lives adjacent to the storage machinery
// (makeStorage above) to minimize merge conflicts with sibling agents
// working on other mobius-runtime.js features. The signal() impl is
// self-contained — it calls makeStorage's storage object methods but
// shares no mutable state with the storage internals above.
//
// Implementation invariants:
//   - never throws; all async work is fire-and-forget
//   - no-ops when storage is unavailable (null storage arg)
//   - name: any non-empty string is accepted (kebab-case recommended but
//     NOT enforced); only non-string or empty names are dropped silently
//   - payload values: primitives only (string/number/boolean); non-
//     primitive values (objects, arrays) are dropped with no error
//   - pending memory cap 500; oldest unqueued entries evicted when full
//   - debounce: at most one flush per 5 seconds; a final flush fires on
//     pagehide and visibilitychange-hidden so no events are lost on tab close
//   - a flush removes entries only after IndexedDB durably accepts them
//
// Exported as makeSignal(appId, storage) → the signal() fn, so
// init() can wire it and tests can drive it without a full init().

const SIGNAL_BUF_CAP = 500
const SIGNAL_BATCH_CAP = 100
const SIGNAL_FLUSH_INTERVAL_MS = 5000
const SIGNAL_NAME_MAX = 80
const SIGNAL_PAYLOAD_KEYS_MAX = 20
const SIGNAL_PAYLOAD_KEY_MAX = 80
const SIGNAL_PAYLOAD_STRING_MAX = 500
// Stay below the server's 4096-byte ceiling. The 96-byte margin covers the
// bounded difference between JavaScript and Python number formatting across at
// most 20 payload fields (for example 1e-7 vs 1e-07 and -0 vs -0.0).
const SIGNAL_EVENT_BYTES_MAX = 4000

// Match Python json.dumps(..., ensure_ascii=True) byte-for-byte for the signal
// shapes we permit. JSON.stringify has already escaped ASCII controls/quotes;
// every remaining non-ASCII UTF-16 code unit becomes one six-byte \uXXXX
// escape server-side (a supplementary character is two code units / 12 bytes).
function _signalServerBytes(value) {
  const json = JSON.stringify(value)
  let bytes = 0
  for (let index = 0; index < json.length; index += 1) {
    bytes += json.charCodeAt(index) <= 0x7f ? 1 : 6
  }
  return bytes
}

export function makeSignal(appId, storage, appInstanceId = null) {
  if (!storage || !appId || typeof storage._queueSignals !== 'function') return () => {}

  let _buf = []
  let _flushTimer = null
  let _flushInFlight = false
  let _flushAgain = false
  let _visibilityHandler = null

  function _signalId() {
    try {
      if (crypto && crypto.randomUUID) return crypto.randomUUID()
    } catch (e) {}
    return `${appId}-${Date.now()}-${Math.random().toString(36).slice(2)}`
  }

  // Validate and normalise one signal call. Returns null if invalid.
  function _prepare(name, payload) {
    if (typeof name !== 'string' || !name.trim()) return null
    const entry = {
      id: _signalId(),
      occurred_at: new Date().toISOString(),
      name: name.trim().slice(0, SIGNAL_NAME_MAX),
      payload: {},
    }
    if (typeof appInstanceId === 'string' && appInstanceId) {
      entry.app_instance_id = appInstanceId.slice(0, 64)
    }
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      for (const [rawKey, rawValue] of Object.entries(payload).slice(0, SIGNAL_PAYLOAD_KEYS_MAX)) {
        const key = String(rawKey).slice(0, SIGNAL_PAYLOAD_KEY_MAX)
        if (!key) continue
        let value = rawValue
        if (typeof value === 'string') value = value.slice(0, SIGNAL_PAYLOAD_STRING_MAX)
        const t = typeof value
        if (t === 'string' || t === 'boolean' || (t === 'number' && Number.isFinite(value))) {
          entry.payload[key] = value
          // Payload is optional telemetry context. Drop the field that crosses
          // the server's total event budget so helper-produced events can never
          // become singleton poison records and be silently discarded.
          if (_signalServerBytes(entry) > SIGNAL_EVENT_BYTES_MAX) delete entry.payload[key]
        }
      }
    }
    return entry
  }

  // Add an entry to the ring buffer, evicting oldest if over cap.
  function _push(entry) {
    _buf.push(entry)
    if (_buf.length > SIGNAL_BUF_CAP) {
      _buf = _buf.slice(_buf.length - SIGNAL_BUF_CAP)
    }
  }

  async function _flush() {
    if (_flushInFlight) { _flushAgain = true; return }
    if (_buf.length === 0) return
    _flushInFlight = true
    const pending = _buf
    _buf = []
    let queued = 0
    try {
      while (queued < pending.length) {
        const batch = pending.slice(queued, queued + SIGNAL_BATCH_CAP)
        await storage._queueSignals(batch)
        queued += batch.length
      }
    } catch (e) {
      // Only batches not yet accepted by IndexedDB return to memory. Earlier
      // batches are already durable and safe for the outbox to replay.
      _buf = [...pending.slice(queued), ..._buf].slice(-SIGNAL_BUF_CAP)
    } finally {
      _flushInFlight = false
      const runAgain = _flushAgain
      _flushAgain = false
      if (runAgain && _buf.length) _flushNow()
      else if (_buf.length) _scheduleFlush()
    }
  }

  // Schedule a debounced flush. At most one flush every 5 seconds.
  function _scheduleFlush() {
    if (_flushTimer !== null) return
    _flushTimer = setTimeout(() => {
      _flushTimer = null
      _flush().catch(() => {})
    }, SIGNAL_FLUSH_INTERVAL_MS)
  }

  // Immediate flush (for pagehide / visibilitychange-hidden).
  function _flushNow() {
    if (_flushTimer !== null) { clearTimeout(_flushTimer); _flushTimer = null }
    _flush().catch(() => {})
  }

  // Register page-lifecycle hooks once (on first call) to drain the buffer
  // when the tab is about to close or go to background.
  let _hooksRegistered = false
  function _ensureHooks() {
    if (_hooksRegistered) return
    _hooksRegistered = true
    try {
      window.addEventListener('pagehide', _flushNow)
      _visibilityHandler = () => {
        if (document.visibilityState === 'hidden') _flushNow()
      }
      document.addEventListener('visibilitychange', _visibilityHandler)
    } catch (e) {}
  }

  // The public signal() function remains fire-and-forget.
  function signal(name, payload) {
    try {
      const entry = _prepare(name, payload)
      if (!entry) return
      _ensureHooks()
      _push(entry)
      _scheduleFlush()
    } catch (e) {
      // signal() must never propagate exceptions
    }
  }

  signal._destroy = () => {
    if (_flushTimer !== null) {
      clearTimeout(_flushTimer)
      _flushTimer = null
    }
    if (_hooksRegistered) {
      try { window.removeEventListener('pagehide', _flushNow) } catch (e) {}
      try {
        if (_visibilityHandler) {
          document.removeEventListener('visibilitychange', _visibilityHandler)
        }
      } catch (e) {}
    }
    _hooksRegistered = false
    _visibilityHandler = null
  }

  return signal
}
