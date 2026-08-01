import { fetchBounded, fetchWithAppToken } from './network.js'
import { tokenMatchesRuntime } from './token.js'

const DB_NAME = 'mobius-outbox'
const SIGNAL_DB_NAME = 'mobius-signals'
const STORE = 'ops'
const SIGNAL_STORE = 'signals'
// Read-through mirror of last-known server values, so get() works offline.
// Keyed by `${appId}:${path}` (one shared DB across all apps, like the outbox).
const CACHE_STORE = 'cache'
const OUTCOME_STORE = 'write_outcomes'
const DB_VERSION = 3
const SIGNAL_DB_VERSION = 1
const MAX_WRITE_OUTCOMES = 200
const MAX_PENDING_SIGNALS = 500
const MAX_PENDING_SIGNAL_BYTES = 2 * 1024 * 1024
// The database is shared by every installed app. Per-app limits alone still
// let a large catalog multiply origin usage without bound, so retain a second
// owner-wide ceiling and evict the oldest telemetry across apps when needed.
const MAX_GLOBAL_PENDING_SIGNALS = 2000
const MAX_GLOBAL_PENDING_SIGNAL_BYTES = 8 * 1024 * 1024
const SIGNAL_SEND_BATCH = 100

// Per-blob ceiling for setBlob: rejected BEFORE any IDB/outbox/network write, so
// neither the local mirror nor the offline outbox ever holds an over-cap binary
// (a 40 MB offline blob write would otherwise sit in IndexedDB until drain). This
// is a LOCAL-mirror guard, deliberately below the backend's 50 MiB write cap —
// large media belongs in OPFS / a direct upload, not the offline outbox.
const MAX_BLOB_BYTES = 25 * 1024 * 1024

// PURE: given the outbox ops (FIFO by seq), the path, and a fallback value
// (the server/cache value), return what the caller should SEE — read-your-
// writes. The newest queued op for the path wins (a DELETE resolves to null);
// if none is queued, the fallback stands. Exported so the read-your-writes /
// LWW semantics are unit-testable without IndexedDB (the rest of the runtime
// needs a browser). Keep this the single source of truth for "what value now".
export function overlayPending(ops, path, fallback) {
  let pending
  for (const op of ops) if (op.path === path) pending = op   // last (newest) wins
  if (pending) return pending.method === 'DELETE' ? null : pending.data
  return fallback
}

// Bound a fetch so a stalled offline request (Android: navigator.onLine reads a
// stale `true`, so get() takes the online branch and the request hangs instead
// of failing fast) can't make a read wait seconds before falling back to the
// cache mirror. Aborts at READ_TIMEOUT_MS; the caller treats a throw as "use
// the cache". Mirrors the bounded-fetch the service worker uses for frame/
// module.
export class DurableWriteError extends Error {
  constructor(message, fields = {}) {
    super(message)
    this.name = 'DurableWriteError'
    this.code = fields.code || 'dead_letter'
    this.status = fields.status
    this.path = fields.path
    this.writeId = fields.writeId
    this.refusedValue = fields.refusedValue
    this.retryable = fields.retryable === true
  }
}

function openDb() {
  return new Promise((resolve, reject) => {
    let settled = false
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        // autoIncrement `seq` gives FIFO ordering for free. `appId` is a
        // stored field, filtered at read time (one shared DB, many apps).
        db.createObjectStore(STORE, { keyPath: 'seq', autoIncrement: true })
      }
      // v2: the read mirror. Additive — existing installs keep their outbox
      // and gain the cache store on the version bump.
      if (!db.objectStoreNames.contains(CACHE_STORE)) {
        db.createObjectStore(CACHE_STORE, { keyPath: 'key' })
      }
      if (!db.objectStoreNames.contains(OUTCOME_STORE)) {
        db.createObjectStore(OUTCOME_STORE, { keyPath: 'key' })
      }
    }
    req.onsuccess = () => {
      const db = req.result
      // If this open already lost a race to onblocked (we rejected), the
      // connection that arrives now would leak — close it immediately. The
      // `settled` guard tracks that (Codex review, Medium #1 follow-up).
      if (settled) { try { db.close() } catch (e) {} return }
      settled = true
      // If another context (or logout) requests a version change / delete,
      // close THIS connection so we don't block it indefinitely. Without this,
      // an open app iframe wedges deleteDatabase() on logout and a future
      // schema bump (Codex review, High #1). withStore also closes per-tx.
      db.onversionchange = () => { try { db.close() } catch (e) {} }
      resolve(db)
    }
    req.onerror = () => { if (!settled) { settled = true; reject(req.error) } }
    // A blocked open means an older-version connection is still around. Reject
    // so callers don't hang; if the open later succeeds anyway, onsuccess sees
    // `settled` and closes the late handle instead of leaking it.
    req.onblocked = () => { if (!settled) { settled = true; reject(new Error('mobius-outbox open blocked')) } }
  })
}

// Signals intentionally use their own version-1 database. Adding their store
// to mobius-outbox would force a schema upgrade that makes still-open cached
// runtimes unable to reopen the owner's user-data outbox during rollout.
function openSignalDb() {
  return new Promise((resolve, reject) => {
    let settled = false
    const req = indexedDB.open(SIGNAL_DB_NAME, SIGNAL_DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(SIGNAL_STORE)) {
        db.createObjectStore(SIGNAL_STORE, { keyPath: 'key' })
      }
    }
    req.onsuccess = () => {
      const db = req.result
      if (settled) { try { db.close() } catch (e) {} return }
      settled = true
      db.onversionchange = () => { try { db.close() } catch (e) {} }
      resolve(db)
    }
    req.onerror = () => { if (!settled) { settled = true; reject(req.error) } }
    req.onblocked = () => {
      if (!settled) { settled = true; reject(new Error('mobius-signals open blocked')) }
    }
  })
}

async function withSignalStore(mode, fn) {
  const db = await openSignalDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SIGNAL_STORE, mode)
    const box = {}
    fn(tx.objectStore(SIGNAL_STORE), box)
    const done = () => { try { db.close() } catch (e) {} }
    tx.oncomplete = () => { done(); resolve(box.value) }
    tx.onerror = () => { done(); reject(tx.error) }
    tx.onabort = () => { done(); reject(tx.error) }
  })
}

// Run `fn(store)` in one transaction on `storeName`. `fn` may stash a result
// on the returned object's `value`; we resolve with it on commit. Doing all
// IDB work inside the single synchronous `fn` call avoids the auto-close that
// bites when you await between operations on one tx.
async function withStore(storeName, mode, fn) {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode)
    const box = {}
    fn(tx.objectStore(storeName), box)
    // Close the connection when the tx settles so handles don't accumulate and
    // block a logout-time deleteDatabase() or a future version bump (Codex
    // review, High #1). Opening per-call is cheap relative to the IO.
    const done = () => { try { db.close() } catch (e) {} }
    tx.oncomplete = () => { done(); resolve(box.value) }
    tx.onerror = () => { done(); reject(tx.error) }
    tx.onabort = () => { done(); reject(tx.error) }
  })
}

async function withStores(storeNames, mode, fn) {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeNames, mode)
    const stores = {}
    for (const name of storeNames) stores[name] = tx.objectStore(name)
    const box = {}
    fn(stores, box)
    const done = () => { try { db.close() } catch (e) {} }
    tx.oncomplete = () => { done(); resolve(box.value) }
    tx.onerror = () => { done(); reject(tx.error) }
    tx.onabort = () => { done(); reject(tx.error) }
  })
}

// Exported so the stateful offline core (per-path serialization, the
// read-through cache, subscribe() fan-out, and the drain's poison-op
// dead-letter + reconcile) is unit-testable headless — driven by
// fake-indexeddb + a mocked fetch/navigator under node:test
// (mobiusRuntimeStore.test.js), the same way overlayPending exposes the
// PURE read-your-writes logic. `init()` is the only production caller.
export function makeStorage({ appId, appInstanceId = null, getToken, isOnline = null }) {
  const hostGetToken = getToken
  getToken = async (options) => {
    const token = await hostGetToken(options)
    return tokenMatchesRuntime(token, appId, appInstanceId) ? token : null
  }
  const deadLetterListeners = new Set()
  const instanceKey = appInstanceId || 'legacy'
  const onlineNow = () => {
    try {
      if (typeof isOnline === 'function') return isOnline() !== false
      return typeof navigator === 'undefined' ? true : navigator.onLine !== false
    } catch { return true }
  }
  const bridgeCall = (typeof window !== 'undefined'
    && typeof window.__mobiusStorageBridgeCall === 'function')
    ? window.__mobiusStorageBridgeCall
    : null
  const bridgeSubscribe = (typeof window !== 'undefined'
    && typeof window.__mobiusStorageBridgeSubscribe === 'function')
    ? window.__mobiusStorageBridgeSubscribe
    : null
  const bridgeUnsubscribers = new Set()
  // App frames deliberately run without `allow-same-origin`, which makes their
  // origin opaque. Chromium correctly denies IndexedDB in that context. The
  // shell hosts this same cache/outbox and exposes a narrow RPC bridge; a
  // standalone/degraded host with neither IDB nor that bridge falls back to
  // direct online-only scoped requests. Never loosen the iframe sandbox merely
  // to regain origin-bound storage.
  let indexedDbAvailable = null
  async function hasIndexedDb() {
    if (indexedDbAvailable !== null) return indexedDbAvailable
    try {
      const db = await openDb()
      try { db.close() } catch (e) {}
      indexedDbAvailable = true
    } catch (e) {
      indexedDbAvailable = false
    }
    return indexedDbAvailable
  }
  // Generation identity is exact. A legacy record has no proof that it belongs
  // to the current row after SQLite reuses a numeric id, while a legacy runtime
  // must never see/delete nonce-stamped records from a newer installation.
  // Quarantine ambiguity instead of guessing from client clocks or timestamps.
  const belongsToInstance = (record) => (
    record && record.appId === appId
    && (appInstanceId
      ? record.appInstanceId === appInstanceId
      : !record.appInstanceId)
  )
  // Telemetry is non-load-bearing, so prefer dropping an ambiguous legacy
  // record over attributing it to a different installation after SQLite reuses
  // an app ID. Cached old runtimes can still drain their own legacy records;
  // once a nonce-aware runtime loads it only handles nonce-stamped telemetry.
  const belongsToSignalInstance = (record) => (
    record && record.appId === appId
    && (appInstanceId
      ? record.appInstanceId === appInstanceId
      : !record.appInstanceId)
  )

  function outcomeKey(writeId) { return appId + ':' + instanceKey + ':' + String(writeId) }

  function outcomeFromOp(op, state, extra = {}) {
    const writeId = op.ver || op.seq
    return {
      key: outcomeKey(writeId),
      appId,
      appInstanceId,
      state,
      path: op.path,
      seq: op.seq,
      ver: op.ver || null,
      writeId,
      method: op.method,
      kind: op.kind || 'json',
      status: extra.status,
      version: extra.version,
      refusedValue: op.method === 'DELETE' ? null : op.data,
      ts: Date.now(),
      consumed: false,
    }
  }

  function putOutcomeInStore(store, outcome) {
    store.put(outcome)
    const seen = []
    store.openCursor().onsuccess = (e) => {
      const cursor = e.target.result
      if (cursor) {
        const v = cursor.value
        if (belongsToInstance(v)) seen.push({ key: v.key, ts: v.ts || 0 })
        cursor.continue()
        return
      }
      if (seen.length <= MAX_WRITE_OUTCOMES) return
      seen.sort((a, b) => a.ts - b.ts)
      for (const old of seen.slice(0, seen.length - MAX_WRITE_OUTCOMES)) {
        store.delete(old.key)
      }
    }
  }

  function recordWriteOutcome(outcome) {
    return withStore(OUTCOME_STORE, 'readwrite', (store) => {
      putOutcomeInStore(store, outcome)
    })
  }

  function getWriteOutcome(writeId) {
    if (writeId == null) return Promise.resolve(null)
    return withStore(OUTCOME_STORE, 'readonly', (store, box) => {
      const r = store.get(outcomeKey(writeId))
      r.onsuccess = () => { box.value = r.result || null }
    })
  }

  function markOutcomeConsumed(key) {
    if (!key) return Promise.resolve()
    return withStore(OUTCOME_STORE, 'readwrite', (store) => {
      const r = store.get(key)
      r.onsuccess = () => {
        const rec = r.result
        if (rec) store.put({ ...rec, consumed: true })
      }
    })
  }

  function dispatchDeadLetter(rec) {
    const payload = {
      path: rec.path,
      status: rec.status,
      refusedValue: rec.refusedValue,
      writeId: rec.writeId,
      ts: rec.ts,
    }
    for (const cb of [...deadLetterListeners]) {
      try { cb(payload) } catch (e) {}
    }
    if (deadLetterListeners.size > 0) markOutcomeConsumed(rec.key).catch(() => {})
  }

  function replayDeadLetters(cb) {
    withStore(OUTCOME_STORE, 'readwrite', (store) => {
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) return
        const rec = cursor.value
        if (belongsToInstance(rec) && rec.state === 'rejected' && !rec.consumed) {
          try {
            cb({ path: rec.path, status: rec.status, refusedValue: rec.refusedValue, writeId: rec.writeId, ts: rec.ts })
            cursor.update({ ...rec, consumed: true })
          } catch (err) {}
        }
        cursor.continue()
      }
    }).catch(() => {})
  }

  function onDeadLetter(cb) {
    if (typeof cb !== 'function') return () => {}
    deadLetterListeners.add(cb)
    replayDeadLetters(cb)
    return () => { deadLetterListeners.delete(cb) }
  }

  // Drop every queued op for this app + path in one transaction, then run
  // `after(store)` (if given) inside the SAME transaction. Used to enforce
  // last-write-wins at path granularity: a newer write for a path
  // supersedes any older queued write for it, so the stale op must not
  // survive to be replayed on drain. Filtering happens in the cursor
  // because the store is keyed by `seq` (FIFO), with `appId`/`path` as
  // plain fields. Doing the purge and the follow-up add in one tx keeps
  // the coalesce atomic — no window where the path has zero ops queued.
  function purgePath(path, after) {
    return withStores([STORE, OUTCOME_STORE], 'readwrite', (stores, box) => {
      const store = stores[STORE]
      const outcomeStore = stores[OUTCOME_STORE]
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) {
          if (after) after(store, box)
          return
        }
        const v = cursor.value
        if (belongsToInstance(v) && v.path === path) {
          putOutcomeInStore(outcomeStore, outcomeFromOp(v, 'superseded'))
          cursor.delete()
        }
        cursor.continue()
      }
    })
  }

  // Enqueue coalesces: the newest write for a path replaces any older
  // queued writes for it, so a stale op can never clobber a newer one
  // when the queue drains. (FIFO ordering across DIFFERENT paths is
  // still preserved — drainInner walks `seq` in order.)
  function enqueue(op) {
    return purgePath(op.path, (store, box) => {
      const queued = { ...op, appId, appInstanceId, ts: Date.now() }
      const r = store.add(queued)
      r.onsuccess = () => { box.value = { ...queued, seq: r.result } }
    })
  }

  function listOps() {
    return withStore(STORE, 'readonly', (store, box) => {
      box.value = []
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) return
        if (belongsToInstance(cursor.value)) box.value.push(cursor.value)
        cursor.continue()
      }
    })
  }

  function deleteOp(seq) {
    return withStore(STORE, 'readwrite', (store) => { store.delete(seq) })
  }

  // ── Read-through cache (the offline read mirror) ──────────────────────
  // get() mirrors every successful ONLINE read here; offline, get() serves
  // this last-known value (overlaid with any pending outbox write). Keyed by
  // `${appId}:${path}` so one shared DB holds every app's mirror. `present`
  // distinguishes a cached null/404 (key exists, value null) from "never
  // fetched" (no key) — so offline we don't claim a value we never had.
  function cacheKey(path) { return appId + ':' + instanceKey + ':' + path }

  function cacheGet(path) {
    return withStore(CACHE_STORE, 'readonly', (store, box) => {
      const r = store.get(cacheKey(path))
      r.onsuccess = () => { box.value = r.result || null }
    })
  }

  // Every write stamps a unique `ver` write-nonce. The poison reconcile CAS
  // matches on `ver`, NOT on value, so it never overwrites a newer write that
  // happens to carry identical bytes (the ABA gap) and never has to compare
  // Blobs or null. Browser-only runtime → crypto.randomUUID is available.
  let _verSeq = 0
  function nextVer() {
    const rnd = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID() : Math.random().toString(36).slice(2)
    return (++_verSeq) + '-' + rnd
  }

  // The record carries `kind` ('json'|'text'|'blob') + `contentType` so a read
  // SELF-DESCRIBES from storage (one server path = one typed value), plus a `ver`
  // write-nonce for the reconcile CAS. `data` holds the JSON value, the string,
  // or a native Blob (IndexedDB stores Blobs via structured clone).
  function cachePut(
    path, data, kind = 'json', contentType = null, ver = nextVer(),
    serverVersion = undefined,
  ) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      const put = (prior) => store.put({
        key: cacheKey(path), path, appId, appInstanceId, data, kind, contentType,
        present: data !== null,
        ver,
        serverVersion: serverVersion === undefined
          ? (prior?.serverVersion || null)
          : (serverVersion || null),
        ts: Date.now(),
      })
      if (serverVersion !== undefined) { put(null); return }
      const existing = store.get(cacheKey(path))
      existing.onsuccess = () => put(existing.result || null)
    })
  }

  function cacheConfirmVersion(path, expectedVer, serverVersion) {
    if (!serverVersion || expectedVer == null) return Promise.resolve(false)
    return withStore(CACHE_STORE, 'readwrite', (store, box) => {
      box.value = false
      const get = store.get(cacheKey(path))
      get.onsuccess = () => {
        const current = get.result
        if (!current || current.ver !== expectedVer) return
        store.put({ ...current, serverVersion, ts: Date.now() })
        box.value = true
      }
    })
  }

  // Tombstone the deletion (present:false, key kept) so an offline read after an
  // offline delete returns null. Preserve `kind` (so a fatal-DELETE reconcile /
  // re-delete re-reads with the right type) + stamp a `ver` for the CAS.
  function cacheDelete(path, kind = null, ver = nextVer()) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      store.put({ key: cacheKey(path), path, appId, appInstanceId, data: null, kind, contentType: null, present: false, ver, ts: Date.now() })
    })
  }

  // Restore the cache record to a prior snapshot (or remove the key if there was
  // none) — undoes an optimistic write whose outbox enqueue failed, WITHOUT the
  // data loss a blanket tombstone would cause.
  function restoreCache(path, prev) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      if (prev) store.put(prev)
      else store.delete(cacheKey(path))
    })
  }

  // ATOMIC compare-and-set on the write-nonce: replace the record ONLY if it
  // still carries `expectedVer` (the version the rejected op wrote), in ONE
  // transaction. Lets the poison reconcile re-sync the mirror without clobbering
  // any write that landed since — ver-based, so no ABA gap and no Blob/null
  // value comparison. Returns true iff it wrote.
  function cacheCompareSet(path, expectedVer, fresh, kind, contentType) {
    return withStore(CACHE_STORE, 'readwrite', (store, box) => {
      box.value = false
      const g = store.get(cacheKey(path))
      g.onsuccess = () => {
        const cur = g.result
        if (cur && expectedVer != null && cur.ver === expectedVer) {
          store.put({ key: cacheKey(path), path, appId, appInstanceId, data: fresh, kind, contentType, present: fresh !== null, ver: nextVer(), ts: Date.now() })
          box.value = true
        }
      }
    })
  }

  // Atomic repair for a LEGACY (pre-ver) rejected op: overwrite ONLY if the
  // mirror is absent or STILL ver-less — so a newer VERSIONED write that landed
  // during the reconcile fetch is never clobbered. One transaction (no TOCTOU).
  function cacheRepairLegacy(path, fresh, kind, contentType) {
    return withStore(CACHE_STORE, 'readwrite', (store, box) => {
      box.value = false
      const g = store.get(cacheKey(path))
      g.onsuccess = () => {
        const cur = g.result
        if (!cur || cur.ver == null) {
          store.put({ key: cacheKey(path), path, appId, appInstanceId, data: fresh, kind, contentType, present: fresh !== null, ver: nextVer(), ts: Date.now() })
          box.value = true
        }
      }
    })
  }

  // A typed read of the WRONG kind is an app bug — fail loud rather than hand a
  // string back to getBlob (→ URL.createObjectURL throws) or a Blob to get().
  // Records written before 083 (and JSON writes) have no `kind` field; treat
  // missing as 'json' so existing mirrors + every get()/set() app keep working.
  function assertReadKind(path, storedKind, wantKind) {
    const stored = storedKind || 'json'
    if (stored !== wantKind) {
      throw new Error(
        `mobius.storage: ${path} holds ${stored}; read it with ` +
        (stored === 'json' ? 'get()' : stored === 'text' ? 'getText()' : 'getBlob()')
      )
    }
  }

  // The backend serves a Blob's Content-Type from the FILE EXTENSION
  // (mimetypes.guess_type), not from what we PUT, so res.blob().type can diverge
  // from the contentType the app set. Re-stamp it from the stored contentType so
  // getBlob() always returns the intended type (for <img>/<embed>/object URLs).
  function normalizeBlob(value, contentType) {
    if (value instanceof Blob && contentType && value.type !== contentType) {
      return new Blob([value], { type: contentType })
    }
    return value
  }

  // Guard the FINAL returned value's JS type against the requested kind, and (for
  // blobs) re-stamp the MIME. assertReadKind only inspects the LOCAL mirror's
  // kind; this also catches the cross-runtime MIXED-KIND case where a pending op
  // of a DIFFERENT kind in the SHARED outbox overlays via effectiveValue (e.g. a
  // pending text write making a getBlob return a string). A type mismatch is an
  // app bug — fail loud rather than hand back the wrong JS type.
  function finalizeRead(value, kind, contentType, path) {
    if (value == null) return value
    if (kind === 'blob') {
      if (!(value instanceof Blob)) {
        throw new Error(`mobius.storage: ${path} does not hold a blob; read it with get()/getText()`)
      }
      return normalizeBlob(value, contentType)
    }
    if (kind === 'text' && typeof value !== 'string') {
      throw new Error(`mobius.storage: ${path} does not hold text; read it with get()/getBlob()`)
    }
    // json accepts any JSON value (object/array/string/number/bool/null) but NOT
    // a Blob — catches a cross-runtime pending blob op overlaid onto a get().
    if (kind === 'json' && value instanceof Blob) {
      throw new Error(`mobius.storage: ${path} holds a blob; read it with getBlob()`)
    }
    return value
  }

  // Lazy, memoized IndexedDB-Blob support probe. Some old WebKit builds throw
  // DataCloneError when storing a Blob in IDB; we must NOT silently base64-expand
  // (that would blow the size cap + corrupt the round-trip). Run it on the FIRST
  // setBlob (never at init — JSON-only apps pay nothing) and cache the verdict;
  // setBlob rejects up front on an unsupported browser.
  let _blobStorable
  function blobStorable() {
    if (_blobStorable === undefined) {
      _blobStorable = (async () => {
        const k = '\0mobius-blob-probe:' + appId   // \0 prefix can't collide with cacheKey()
        try {
          const probe = new Blob([new Uint8Array([1])], { type: 'application/octet-stream' })
          await withStore(CACHE_STORE, 'readwrite', (store) => { store.put({ key: k, data: probe }) })
          const r = await withStore(CACHE_STORE, 'readonly', (store, box) => {
            const g = store.get(k); g.onsuccess = () => { box.value = g.result }
          })
          await withStore(CACHE_STORE, 'readwrite', (store) => { store.delete(k) })
          return !!(r && r.data instanceof Blob)
        } catch (e) {
          try { await withStore(CACHE_STORE, 'readwrite', (store) => { store.delete(k) }) } catch (_) {}
          return false
        }
      })()
    }
    return _blobStorable
  }

  // ── Per-path serialization (in-tab) ─────────────────────────────────
  // All operations that read-or-write a path's value (get, set, remove) run
  // through a per-path promise chain, so within this runtime they execute
  // STRICTLY in call order and never interleave. This is the single, correct
  // fix for the whole race class Codex flagged: a slow GET can't overwrite the
  // cache after a newer set() (its cache-write is now ordered after the set),
  // and two set()s can't reorder their cache writes (server LWW is still by
  // arrival, but the LOCAL mirror — the source of truth for offline reads and
  // subscribers — is deterministic by call order). Cross-tab/iframe drains are
  // additionally serialized by the existing Web Lock in drain().
  //
  // SCOPE / known bound: pathChains is per-runtime (per makeStorage). It does
  // NOT serialize across two SEPARATE runtimes for the same app — e.g. the same
  // app open BOTH in the in-shell iframe AND a standalone PWA tab at once,
  // mutating the same path. There, the local mirrors can momentarily diverge by
  // op interleaving; the server still converges by arrival-order LWW and the
  // next online get() re-syncs each mirror. Adding a cross-context Web Lock to
  // every read/write would slow the common single-context path to harden a rare
  // one — deliberately not done (single-owner, server-arrival LWW is the
  // documented contract).
  const pathChains = new Map()
  function withPathLock(path, fn) {
    const prev = pathChains.get(path) || Promise.resolve()
    // Run fn after prev settles (success OR failure — never let one op's
    // rejection break the chain for the next).
    const next = prev.then(fn, fn)
    // Tail swallows rejections so the chain never becomes an unhandled
    // rejection (callers still see fn's real result via `next`), and removes
    // its own map entry once settled IF it's still the tail — so the map holds
    // entries only for paths with in-flight ops, not every path ever touched.
    const tail = next.then(() => {}, () => {})
    pathChains.set(path, tail)
    tail.then(() => { if (pathChains.get(path) === tail) pathChains.delete(path) })
    return next
  }

  // ── Reactivity: per-path subscribers ─────────────────────────────────
  // Notify a path's listeners whenever its value changes locally (set/remove)
  // or a sync lands (drain). Lets a UI re-render without polling. In-memory,
  // per runtime instance — not persisted (it's view wiring, not data).
  const subscribers = new Map()   // path -> Set<cb>

  function notify(path, data) {
    const set = subscribers.get(path)
    if (!set) return
    for (const cb of [...set]) {
      try { cb(data) } catch (e) { /* a listener throwing must not break others */ }
    }
  }

  // Send one queued op. Storage PUT/DELETE are idempotent by path. Signal
  // batches instead carry stable event IDs so their consumer can deduplicate a
  // replay after a successful response is lost. A DELETE
  // that 404s means the file is already absent — the intended end state
  // — so we treat it as success. A 401 means the token is stale; we
  // throw 'AUTH' so the drain stops WITHOUT discarding the op (a fresh
  // token on the next trigger retries it).
  async function send(op) {
    const url = `/api/storage/apps/${appId}/${op.path}`
    const init = { method: op.method, headers: {} }
    if (op.method === 'PUT') {
      if (op.ifMatch) init.headers['If-Match'] = op.ifMatch
      if (op.ifNoneMatch) init.headers['If-None-Match'] = '*'
      // Branch by kind: blob/text send raw bytes/text with their real
      // Content-Type (the backend stores raw bytes for non-JSON types and raw
      // UTF-8 for text/*); json keeps the exact old wire shape.
      if (op.kind === 'blob' || op.kind === 'text') {
        init.headers['Content-Type'] = op.contentType ||
          (op.kind === 'blob' ? 'application/octet-stream' : 'text/plain;charset=utf-8')
        init.body = op.data
      } else {
        init.headers['Content-Type'] = 'application/json'
        init.body = JSON.stringify(op.data)
      }
    }
    const res = await fetchWithAppToken(getToken, url, init) // network failure throws -> transient
    const version = res.headers && typeof res.headers.get === 'function'
      ? (res.headers.get('ETag') || res.headers.get('etag') || undefined)
      : undefined
    if (op.method === 'DELETE' && res.status === 404) return { version }  // already absent
    if (res.ok) return { version }
    // Classify so one bad op can't wedge the queue (drainInner reads
    // err.fatal): 401 auth / 408 timeout / 429 rate-limit / 5xx / network
    // are transient (keep + retry); 412 is a CAS conflict handled by the
    // bounded durableWrite/useDocument retry path; any other 4xx is fatal.
    const err = new Error(`HTTP ${res.status}`)
    err.status = res.status
    err.conflict = res.status === 412 && (op.ifMatch || op.ifNoneMatch)
    err.fatal = res.status >= 400 && res.status < 500 &&
      ![401, 408, 429].includes(res.status) && !err.conflict
    throw err
  }

  async function drainInner() {
    if (!onlineNow()) return
    const ops = await listOps()           // FIFO by seq
    for (const op of ops) {
      try {
        const sent = await send(op)
        await recordWriteOutcome(outcomeFromOp(op, 'confirmed', { version: sent && sent.version }))
        await cacheConfirmVersion(op.path, op.ver, sent && sent.version)
        await deleteOp(op.seq)
      } catch (e) {
        if (e && e.conflict) {
          const conflict = outcomeFromOp(op, 'conflict', { status: e.status })
          await recordWriteOutcome(conflict)
          await deleteOp(op.seq)
          continue
        }
        if (e && e.fatal) {
          // Poison op — a malformed/forbidden request that will never
          // succeed on replay. Drop it (dead-letter) and keep draining
          // so it can't head-of-line-block every later write forever.
          console.warn('mobius: dropping un-syncable write', op.method, op.path, e.message)
          const rejected = outcomeFromOp(op, 'rejected', { status: e.status })
          await recordWriteOutcome(rejected)
          await deleteOp(op.seq)
          dispatchDeadLetter(rejected)
          // The optimistic mirror still holds the value the server REFUSED.
          // Re-sync it to the authoritative value — KIND-AWARE (fetchValue with
          // op.kind so a rejected blob/text path is re-read correctly, not via
          // JSON get() which would throw assertReadKind on the mirror), and
          // LOCK-FREE (a path-locked get() here would re-enter the lock a writer
          // may hold across this drain → the deadlock this whole restructure
          // avoids). Best-effort; offline → skip, the next online read re-syncs.
          if (onlineNow()) {
            try {
              const fresh = await fetchValue(op.path, op.kind || 'json')
              const ct = fresh instanceof Blob ? fresh.type : null
              if (op.ver != null) {
                // ATOMIC compare-and-set on the write-nonce: re-sync the mirror to
                // the authoritative value ONLY if it still carries the rejected
                // op's ver. A newer same-path write does its cachePut BEFORE its
                // enqueue and OUTSIDE the path lock, so a non-atomic check could
                // clobber it; the one-tx ver-CAS can't (its own send later just
                // deletes its op — never re-cachePuts — so clobbering loses it).
                const wrote = await cacheCompareSet(op.path, op.ver, fresh, op.kind || 'json', ct)
                if (wrote) notify(op.path, fresh)
              } else {
                // LEGACY op (queued by a pre-ver runtime, drained once after the
                // upgrade) — no nonce to CAS on. Repair atomically ONLY if the
                // mirror is absent or still ver-less, so a newer VERSIONED write
                // that landed during the fetch isn't clobbered.
                const wrote = await cacheRepairLegacy(op.path, fresh, op.kind || 'json', ct)
                if (wrote) notify(op.path, fresh)
              }
            } catch (re) { /* best-effort reconciliation */ }
          }
          continue
        }
        // Transient (offline / 5xx / 401): stop so order is preserved
        // (a later op may depend on an earlier one) and retry on the
        // next trigger. The op is NOT discarded.
        break
      }
    }
  }

  // Web Locks serializes draining across contexts (an in-shell iframe
  // and a standalone page for the same app can both be open). Falls
  // back to a plain drain where Web Locks is unavailable.
  // Returns the drain promise so a caller that needs to know the pass
  // FINISHED can await it; the event/init callers fire-and-forget and ignore
  // the return. Each branch's .catch keeps a rejected drain from surfacing as
  // an unhandled rejection whether or not anyone awaits.
  function drain() {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request(
        `mobius-outbox-${appId}`, { ifAvailable: true },
        async (lock) => { if (lock) await drainInner() },
      ).catch(() => {})
    } else {
      // Route through drainNow so this event-triggered drain shares the in-tab
      // _drainChain with set()/remove()'s drainNow — otherwise an event drain and
      // a write's drain could run drainInner concurrently in a no-Web-Locks
      // browser and send a stale snapshot. (Cross-tab in that fallback stays
      // unprotected, bounded by idempotent PUT/DELETE + server-arrival LWW.)
      return drainNow().catch(() => {})
    }
  }

  // Awaiting drain for set()/remove(). ALWAYS-ENQUEUE routes every server write
  // through the outbox + this drain, so the drain is the SOLE server-write path.
  // Acquiring the SAME `mobius-outbox-${appId}` lock WITHOUT ifAvailable (wait,
  // don't skip) serializes it across ALL contexts (iframe + standalone) AND
  // against the background drain — so there is no longer a direct-send path to
  // race a drain, and the 081 drain-vs-direct-write data-loss class is closed by
  // construction. Fallback (no Web Locks): an in-tab promise chain serializes
  // drains within the tab so concurrent set()s can't double-drain.
  let _drainChain = Promise.resolve()
  function drainNow() {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request(`mobius-outbox-${appId}`, drainInner)
    }
    _drainChain = _drainChain.then(drainInner, drainInner)
    return _drainChain
  }

  const drainOnWake = () => { drain(); drainSignals() }
  const drainOnVisible = () => {
    if (document.visibilityState === 'visible') { drain(); drainSignals() }
  }
  for (const ev of ['online', 'focus', 'pageshow']) {
    window.addEventListener(ev, drainOnWake)
  }
  document.addEventListener('visibilitychange', drainOnVisible)

  // The value the caller should see for a path RIGHT NOW: a pending outbox
  // write wins over the server/cache (read-your-writes), else the cache mirror,
  // else null. Used to overlay offline reads and to compute subscriber payloads.
  async function effectiveValue(path, fallback) {
    const ops = await listOps()
    return overlayPending(ops, path, fallback)
  }

  // Named local functions (not `this`-bound methods) so subscribe() can call
  // get() directly and the API survives destructuring — `const {get} = ...`.
  // Each runs inside withPathLock so operations on the same path are strictly
  // ordered within this runtime — no GET-vs-write or write-vs-write interleave.
  function get(path) {
    return withPathLock(path, async () => (
      await hasIndexedDb() ? getInner(path, 'json') : getDirect(path, 'json')
    ))
  }
  function getText(path) {
    return withPathLock(path, async () => (
      await hasIndexedDb() ? getInner(path, 'text') : getDirect(path, 'text')
    ))
  }
  function getBlob(path) {
    return withPathLock(path, async () => (
      await hasIndexedDb() ? getInner(path, 'blob') : getDirect(path, 'blob')
    ))
  }
  // Writers: the LOCAL mutation runs under the path lock (ordered vs reads + other
  // writes); the server drain runs in settle() OUTSIDE that lock (deadlock-safe).
  function set(path, data) {
    return withPathLock(path, async () => {
      if (!await hasIndexedDb()) {
        return { directResult: await writeDirect(path, data, 'json', null) }
      }
      return writeLocal(path, data, 'json', null)
    }).then((op) => op?.directResult
      ? (op.directResult.durability === 'queued' ? { queued: true } : { synced: true })
      : (op ? settle(path, op.writeId, true) : { synced: true }))
  }
  async function setText(path, text, opts) {
    if (typeof text !== 'string') {
      throw new Error('mobius.storage.setText: value must be a string')
    }
    const ct = (opts && opts.contentType) || 'text/plain;charset=utf-8'
    const op = await withPathLock(path, async () => {
      if (!await hasIndexedDb()) {
        return { directResult: await writeDirect(path, text, 'text', ct) }
      }
      return writeLocal(path, text, 'text', ct)
    })
    if (op?.directResult) {
      return op.directResult.durability === 'queued' ? { queued: true } : { synced: true }
    }
    return op ? settle(path, op.writeId, true) : { synced: true }
  }
  // setBlob guards BEFORE any lock/IDB/network: reject a non-Blob, an over-cap
  // blob, or a browser that can't store Blobs in IDB — so neither the mirror nor
  // the outbox ever holds an unstorable or over-cap binary.
  async function setBlob(path, blob, opts) {
    if (!(blob instanceof Blob)) {
      throw new Error('mobius.storage.setBlob: value must be a Blob or File')
    }
    if (blob.size > MAX_BLOB_BYTES) {
      throw new Error(
        `mobius.storage.setBlob: ${path} is ${blob.size} bytes, over the ` +
        `${MAX_BLOB_BYTES}-byte limit (use OPFS or a direct upload for large media)`
      )
    }
    const hasIdb = await hasIndexedDb()
    if (hasIdb && !(await blobStorable())) {
      throw new Error('mobius.storage.setBlob: this browser cannot store Blobs offline')
    }
    const ct = (opts && opts.contentType) || blob.type || 'application/octet-stream'
    const op = await withPathLock(path, async () => {
      if (!hasIdb) {
        return { directResult: await writeDirect(path, blob, 'blob', ct) }
      }
      return writeLocal(path, blob, 'blob', ct)
    })
    if (op?.directResult) {
      return op.directResult.durability === 'queued' ? { queued: true } : { synced: true }
    }
    return op ? settle(path, op.writeId, true) : { synced: true }
  }
  function remove(path) {
    return withPathLock(path, async () => {
      if (!await hasIndexedDb()) {
        return { directResult: await removeDirect(path) }
      }
      return removeLocal(path)
    }).then((op) => op?.directResult
      ? (op.directResult.durability === 'queued' || op.directResult.queued
          ? { queued: true }
          : { synced: true })
      : (op ? settle(path, op.writeId, true) : { synced: true }))
  }

  function listSignals() {
    return withSignalStore('readonly', (store, box) => {
      box.value = []
      store.openCursor().onsuccess = (event) => {
        const cursor = event.target.result
        if (!cursor) {
          box.value.sort((a, b) => a.queuedAt - b.queuedAt)
          return
        }
        if (belongsToSignalInstance(cursor.value)) box.value.push(cursor.value)
        cursor.continue()
      }
    })
  }

  function deleteSignals(records) {
    return withSignalStore('readwrite', (store) => {
      for (const record of records) store.delete(record.key)
    })
  }

  // Signals have a separate bounded queue and drain. Telemetry can therefore
  // never block, dead-letter, or inflate pendingCount() for user data writes.
  let _signalRetryTimer = null
  let _signalRetryDelay = 5000
  let _signalNextAttemptAt = 0

  function scheduleSignalRetry(response = null) {
    if (_signalRetryTimer !== null) return
    let delay = _signalRetryDelay
    const retryAfter = response?.headers?.get?.('Retry-After')
    if (retryAfter) {
      const seconds = Number(retryAfter)
      const dateDelay = Date.parse(retryAfter) - Date.now()
      if (Number.isFinite(seconds)) delay = Math.max(delay, seconds * 1000)
      else if (Number.isFinite(dateDelay)) delay = Math.max(delay, dateDelay)
    }
    const backoffDelay = Math.min(Math.max(_signalRetryDelay, 1000), 5 * 60 * 1000)
    delay = Math.min(Math.max(delay, backoffDelay), 24 * 60 * 60 * 1000)
    _signalRetryDelay = Math.min(backoffDelay * 2, 5 * 60 * 1000)
    _signalNextAttemptAt = Date.now() + delay
    _signalRetryTimer = setTimeout(() => {
      _signalRetryTimer = null
      drainSignals(true).catch(() => {})
    }, delay)
    _signalRetryTimer?.unref?.()
  }

  async function deliverSignalRecords(records) {
    try {
      const response = await fetchWithAppToken(getToken, '/api/client-signal', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ signals: records.map((record) => record.event) }),
      })
      if (response.ok) {
        await deleteSignals(records)
        _signalRetryDelay = 5000
        _signalNextAttemptAt = 0
        return true
      }
      // 404/405 are deployment-compatibility failures, not poison events: the
      // platform serves rebuilt frontend assets before a backend restart, so a
      // new runtime can briefly reach an old process without this route. Keep
      // the durable queue instead of bisecting and deleting every singleton.
      if ([401, 404, 405, 408, 429].includes(response.status) || response.status >= 500) {
        scheduleSignalRetry(response)
        return false
      }
      if (response.status === 403) {
        await deleteSignals(records)
        return true
      }
      // Isolate a poison/future-schema record instead of dropping an otherwise
      // valid 100-event batch. Only the irreducible rejected record is removed.
      if (records.length === 1) {
        await deleteSignals(records)
        return true
      }
      const middle = Math.floor(records.length / 2)
      if (!await deliverSignalRecords(records.slice(0, middle))) return false
      return deliverSignalRecords(records.slice(middle))
    } catch (e) {
      scheduleSignalRetry()
      return false
    }
  }

  async function drainSignalsInner() {
    if (!onlineNow()) return
    for (;;) {
      const records = (await listSignals()).slice(0, SIGNAL_SEND_BATCH)
      if (!records.length) return
      if (!await deliverSignalRecords(records)) return
    }
  }

  let _signalDrainChain = Promise.resolve()
  function drainSignals(force = false) {
    if (!force && Date.now() < _signalNextAttemptAt) return Promise.resolve()
    if (force && _signalRetryTimer !== null) {
      clearTimeout(_signalRetryTimer)
      _signalRetryTimer = null
    }
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request(`mobius-signals-${appId}`, drainSignalsInner)
        .catch(() => {})
    }
    _signalDrainChain = _signalDrainChain.then(drainSignalsInner, drainSignalsInner)
    return _signalDrainChain
  }

  // Internal transport for window.mobius.signal(). The IndexedDB transaction
  // is the durability boundary. Stable IDs make put() and server replay safe;
  // the per-app cap prevents noisy offline telemetry from filling shared IDB.
  async function queueSignals(signals) {
    if (!Array.isArray(signals) || signals.length === 0) return
    await withSignalStore('readwrite', (store) => {
      let queuedAt = Date.now()
      for (const event of signals) {
        const serialized = JSON.stringify(event)
        store.put({
          key: `${appId}:${appInstanceId || 'legacy'}:${event.id}`,
          appId,
          appInstanceId,
          event,
          bytes: new Blob([serialized]).size,
          queuedAt: queuedAt++,
        })
      }
      const records = []
      const ownRecords = []
      store.openCursor().onsuccess = (cursorEvent) => {
        const cursor = cursorEvent.target.result
        if (cursor) {
          records.push(cursor.value)
          if (belongsToSignalInstance(cursor.value)) ownRecords.push(cursor.value)
          cursor.continue()
          return
        }
        const oldestFirst = (a, b) => (a.queuedAt || 0) - (b.queuedAt || 0)
        const deleteKeys = new Set()

        ownRecords.sort(oldestFirst)
        let removeCount = Math.max(0, ownRecords.length - MAX_PENDING_SIGNALS)
        let totalBytes = ownRecords.slice(removeCount)
          .reduce((sum, record) => sum + (record.bytes || 0), 0)
        while (
          removeCount < ownRecords.length
          && totalBytes > MAX_PENDING_SIGNAL_BYTES
        ) {
          totalBytes -= ownRecords[removeCount].bytes || 0
          removeCount += 1
        }
        for (const record of ownRecords.slice(0, removeCount)) deleteKeys.add(record.key)

        const retained = records.filter((record) => !deleteKeys.has(record.key)).sort(oldestFirst)
        let globalRemoveCount = Math.max(0, retained.length - MAX_GLOBAL_PENDING_SIGNALS)
        let globalBytes = retained.slice(globalRemoveCount)
          .reduce((sum, record) => sum + (record.bytes || 0), 0)
        while (
          globalRemoveCount < retained.length
          && globalBytes > MAX_GLOBAL_PENDING_SIGNAL_BYTES
        ) {
          globalBytes -= retained[globalRemoveCount].bytes || 0
          globalRemoveCount += 1
        }
        for (const record of retained.slice(0, globalRemoveCount)) deleteKeys.add(record.key)
        for (const key of deleteKeys) store.delete(key)
      }
    })
    drainSignals().catch(() => {})
  }

  // Page the server's authoritative listing of the immediate children under
  // `prefix`. Returns the entries ARRAY (each {name, path, type, size,
  // modified_at, mime_type?}), `[]` for an empty/unknown dir, and `null` on
  // network failure (offline/transient). Walks every page so list() is true
  // enumeration, not just the server's first page (capped at 500); the guard
  // bounds a pathological/looping cursor.
  async function listServer(prefix, options = {}) {
    try {
      const entries = []
      let cursor = null
      for (let guard = 0; guard < 10000; guard++) {
        const include = options.includeContent ? '&include_content=true' : ''
        // The server's include-content contract allows at most 64 KiB per
        // JSON file and 1 MiB per page. Ask for at most 16 content-bearing
        // entries so every valid file selected for the page can fit the I/O
        // budget. This turns list({includeContent:true}) into a complete,
        // bounded batch primitive instead of forcing callers to rediscover an
        // N+1 fallback when a 500-entry metadata page exhausts the byte cap.
        const pageLimit = options.includeContent ? 16 : 500
        const q = `?limit=${pageLimit}${include}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
        const res = await fetchWithAppToken(
          getToken,
          `/api/storage/apps-list/${appId}/${prefix || ''}${q}`,
          {},
          fetchBounded,
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const body = await res.json()
        for (const e of body.entries || []) entries.push(e)
        cursor = body.next_cursor
        if (!cursor) break
      }
      return entries
    } catch (e) {
      return null
    }
  }

  // The offline listing source: every PRESENT (non-tombstone) path this app has
  // mirrored into the read-through cache. Mirrors listOps' cursor pattern over
  // the cache store. We derive offline listings from these per-PATH entries —
  // each carrying a present=false tombstone once removed/404'd — NOT from a
  // cached listing blob, which is the design that WOULD resurrect deleted
  // children (see list() below).
  function listCachePresent() {
    return withStore(CACHE_STORE, 'readonly', (store, box) => {
      box.value = []
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) return
        const v = cursor.value
        if (belongsToInstance(v) && v.present) {
          box.value.push({
            path: v.path,
            kind: v.kind,
            contentType: v.contentType,
            data: v.data,
          })
        }
        cursor.continue()
      }
    })
  }

  // Enumerate the immediate children of a stored directory (the platform
  // alternative to brute-force-probing filenames). Offline-capable: when the
  // server is reachable its listing is authoritative; otherwise the listing is
  // derived from the per-path read-through cache (tombstones excluded, so
  // deletes don't resurrect). EITHER source is then overlaid with the outbox —
  // a pending write shows, a pending delete drops — so list() is
  // read-your-writes, the same contract get() exposes. Always returns an ARRAY
  // (`[]` when empty/unknown), never null, since offline now has a real source.
  // Offline-derived entries carry name/path/type (+ mime_type when known) but
  // not size/modified_at, which only the server stat provides.
  async function listInner(prefix, options = {}) {
    const norm = (prefix || '').replace(/^\/+|\/+$/g, '')
    const base = norm ? norm + '/' : ''
    // The child name of `path` directly under `base`, or null if not under it.
    const restUnder = (path) => {
      if (base) return path.startsWith(base) ? path.slice(base.length) : null
      return path
    }
    // Direct-children map keyed by child name. A server entry (rich metadata)
    // is never downgraded by a derived entry of the same name.
    const byName = new Map()
    const addDerived = (path, meta) => {
      const rest = restUnder(path)
      if (!rest) return
      const slash = rest.indexOf('/')
      if (slash === -1) {
        if (byName.has(rest)) {
          // A queued JSON write is newer than the server listing. Keep the
          // server's richer metadata, but overlay the value just as get() does.
          if (options.includeContent && meta && meta.kind === 'json') {
            byName.get(rest).content = meta.data
          }
          return
        }
        const mime = (meta && meta.contentType)
          || (meta && meta.kind === 'json' ? 'application/json' : null)
        const entry = { name: rest, path: base + rest, type: 'file', mime_type: mime }
        if (options.includeContent && meta && meta.kind === 'json') {
          entry.content = meta.data
        }
        byName.set(rest, entry)
      } else {
        const dname = rest.slice(0, slash)
        if (!byName.has(dname)) {
          byName.set(dname, { name: dname, path: base + dname, type: 'directory' })
        }
      }
    }

    const server = await listServer(norm, options)
    if (server) {
      for (const e of server) byName.set(e.name, e)
    } else {
      for (const c of await listCachePresent()) addDerived(c.path, c)
    }

    // Overlay state-changing storage ops from the user-data outbox (those
    // coalesce to <=1 op per path): a PUT ensures its child shows even
    // before the drain reaches the server; a DELETE drops a direct-file child
    // the server/cache still lists.
    for (const op of await listOps()) {
      if (op.method === 'DELETE') {
        const rest = restUnder(op.path)
        if (rest && rest.indexOf('/') === -1) byName.delete(rest)
      } else {
        addDerived(op.path, op)
      }
    }

    return [...byName.values()].sort((a, b) =>
      a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
  }

  const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b)

  // Fetch the authoritative server value for a path. 404 → null (known-absent);
  // any other non-OK → throw (transient/auth — the caller keeps the mirror).
  // Bounded so a stale-`true` navigator.onLine (Android offline) can't hang it.
  async function fetchValueWithVersion(path, kind = 'json', wantVersion = false) {
    const headers = {}
    if (wantVersion) headers['X-Mobius-Version'] = '1'
    const res = await fetchWithAppToken(
      getToken,
      `/api/storage/apps/${appId}/${path}`,
      { headers },
      fetchBounded,
    )
    const version = res.headers && typeof res.headers.get === 'function'
      ? (res.headers.get('ETag') || res.headers.get('etag') || undefined)
      : undefined
    if (res.status === 404) return { value: null, version: undefined }
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    let value
    if (kind === 'blob') value = await res.blob()
    else if (kind === 'text') value = await res.text()
    else value = await res.json()
    return { value, version }
  }

  async function fetchValue(path, kind = 'json') {
    return (await fetchValueWithVersion(path, kind, false)).value
  }

  // Background refresh after a cache-first json/text get() (blobs never
  // revalidate — getInner skips this for kind 'blob'). Re-fetch and, if the
  // server value changed, update the mirror + notify. Runs under the per-path
  // chain so its write stays ordered against a concurrent set(). Skips when a
  // local write for the path is queued — that write owns the value until the
  // outbox drains (read-your-writes).
  //
  // ACCEPTED BOUND (cross-runtime, self-healing): if the SAME app is open in two
  // runtimes, R1's fetch can read the pre-write server value in the instant
  // before R2 enqueues+drains a newer write, and R1's later cachePut then briefly
  // shows the stale value to R1's subscribers. It costs one stale read, self-
  // heals on R1's next revalidate, and the server is always correct. ALWAYS-
  // ENQUEUE narrows it (a cross-runtime write is observable in the shared outbox
  // for the whole enqueue→drain window, so the pending-op guards below catch most
  // of it). Fully closing it would need a per-path cross-context READ lock — not
  // worth slowing every read for single-owner, server-arrival-LWW data.
  function scheduleRevalidate(path, kind = 'json') {
    withPathLock(path, async () => {
      if ((await listOps()).some((op) => op.path === path)) return
      let refreshed
      try { refreshed = await fetchValueWithVersion(path, kind, true) } catch (e) { return }
      const { value: data, version } = refreshed
      if ((await listOps()).some((op) => op.path === path)) return
      const prev = await cacheGet(path)
      if (prev && sameJson(prev.data, data)) {
        await cacheConfirmVersion(path, prev.ver, version)
        return
      }
      await cachePut(
        path, data, kind, prev ? prev.contentType : null, nextVer(), version,
      )
      notify(path, data)   // no pending op → effective value === server value
    }).catch(() => {})
  }

  async function getInner(path, kind = 'json') {
    // STALE-WHILE-REVALIDATE read for json/text: with a cached mirror, serve it
    // INSTANTLY (overlaid with any pending write — read-your-writes) and refresh
    // in the background, notifying subscribers if the value changed. BLOBS are
    // CACHE-FIRST with NO revalidate (re-fetching a large binary every read is
    // wasteful + the change-detector can't diff a Blob). A first-ever read awaits
    // the network online, or resolves null offline.
    const cached = await cacheGet(path)
    // A not-present BLOB tombstone is RE-CHECKED against the server when online
    // rather than trusted forever. Blobs are never background-revalidated (the
    // guard below skips them), so a blob that was absent at its first read — a
    // PDF probed before its build compiled it, an image before the agent wrote
    // it — would otherwise read as missing for good, even after the build/agent
    // writes it to the server filesystem (which never touches this IndexedDB
    // mirror). Treating the tombstone as a cache miss lets the network branch
    // below re-fetch it; a PRESENT blob still serves from cache (no wasteful
    // re-download of a large binary).
    const staleBlobTombstone =
      cached && cached.present === false && kind === 'blob' && onlineNow()
    if (cached && !staleBlobTombstone) {
      // Present value: the stored kind is authoritative — a wrong-typed read
      // throws (loud) instead of handing back a string-as-Blob. A tombstone
      // (present:false) has no value to type-check; it resolves null below.
      if (cached.present !== false) assertReadKind(path, cached.kind, kind)
      if (kind !== 'blob' && onlineNow()) scheduleRevalidate(path, kind)
      return finalizeRead(await effectiveValue(path, cached.data), kind, cached.contentType, path)
    }
    if (onlineNow()) {
      try {
        const data = await fetchValue(path, kind)
        const ct = kind === 'blob'
          ? (data instanceof Blob ? data.type : null)
          : (kind === 'text' ? 'text/plain;charset=utf-8' : null)
        await cachePut(path, data, kind, ct)
        return finalizeRead(await effectiveValue(path, data), kind, ct, path)
      } catch (e) {
        // Network blip with nothing cached — fall through to the empty mirror.
      }
    }
    return finalizeRead(await effectiveValue(path, null), kind, null, path)
  }

  // Opaque sandbox frames cannot persist the offline cache/outbox in IndexedDB.
  // In the normal shell they delegate to the parent-hosted makeStorage runtime;
  // only a degraded host without that bridge takes the honest online-only
  // direct path below.
  async function getDirect(path, kind = 'json') {
    if (bridgeCall) {
      const method = kind === 'text' ? 'getText' : (kind === 'blob' ? 'getBlob' : 'get')
      return bridgeCall(method, [path])
    }
    if (!onlineNow()) return null
    try {
      return finalizeRead(await fetchValue(path, kind), kind, null, path)
    } catch (e) {
      return null
    }
  }

  async function writeDirect(path, data, kind, contentType, opts = {}) {
    if (bridgeCall) {
      return bridgeCall('durableWrite', [path, data, {
        kind,
        contentType,
        ...(opts.ifMatch ? { ifMatch: opts.ifMatch } : {}),
        ...(opts.ifNoneMatch === true ? { ifNoneMatch: true } : {}),
      }])
    }
    if (!onlineNow()) {
      throw new Error('mobius.storage: offline saving is unavailable in this sandbox')
    }
    const sent = await send({
      method: 'PUT', path, data, kind, contentType,
      ifMatch: opts.ifMatch || null,
      ifNoneMatch: opts.ifNoneMatch === true,
    })
    notify(path, data)
    return sent || {}
  }

  async function removeDirect(path) {
    if (bridgeCall) return bridgeCall('remove', [path])
    if (!onlineNow()) {
      throw new Error('mobius.storage: offline saving is unavailable in this sandbox')
    }
    const sent = await send({ method: 'DELETE', path, kind: 'json' })
    notify(path, null)
    return sent || {}
  }

  // ALWAYS-ENQUEUE write path (081). Update the mirror + notify synchronously so
  // the UI + a subsequent get() are correct immediately, then route the server
  // write through the outbox + the awaiting drainNow() — the SOLE server-write
  // path. There is deliberately NO direct-send fast path: the old design sent
  // directly under the per-path promise chain while the drain sent under the
  // outbox Web Lock (two locks), so a queued op could be drained AFTER a fresh
  // direct write landed → the newer write was lost. With one outbox-lock-
  // serialized path, a superseded op (enqueue coalesces via purgePath) can only
  // ever be sent BEFORE its successor in a strictly-ordered later pass, so the
  // latest write always wins. {synced} vs {queued} is computed from whether the
  // op survived the drain (offline/transient → still queued, auto-syncs later).
  // Local mutation ONLY (mirror + notify + enqueue), run under the path lock so
  // it is ordered against reads + other writes. The server write is the DRAIN,
  // run by settle() OUTSIDE this lock. Keeping the drain off the path lock is
  // what avoids the reentrant-lock DEADLOCK: the drain's dead-letter reconcile,
  // and any concurrent get(), must be able to take the path lock while a write's
  // drain is in flight.
  // Both writers: snapshot the prior record, do the optimistic local mutation,
  // enqueue the outbox op, and ONLY notify after a durable enqueue. If enqueue
  // fails (IDB error) restore the EXACT prior record (not a lossy tombstone) so
  // the mirror never shows a value with no outbox op + no server write (a
  // "ghost") and never loses the previously-stored value.
  async function writeLocal(path, data, kind, contentType, opts = {}) {
    const prev = await cacheGet(path)
    const ver = nextVer()                 // same nonce on the mirror + the op, for the reconcile CAS
    await cachePut(path, data, kind, contentType, ver)
    let queued
    try {
      queued = await enqueue({
        method: 'PUT',
        path,
        data,
        kind,
        contentType,
        ver,
        ifMatch: opts.ifMatch || null,
        ifNoneMatch: opts.ifNoneMatch === true,
      })
    } catch (e) {
      try { await restoreCache(path, prev) } catch (_) {}
      throw e
    }
    notify(path, data)
    return { path, writeId: ver, ver, seq: queued && queued.seq }
  }

  async function removeLocal(path) {
    // Carry the existing record's kind onto the tombstone + DELETE op so a
    // fatal-DELETE reconcile (or a re-delete) re-reads the server value with the
    // right type — a blob/text path re-fetched as json would throw.
    const prev = await cacheGet(path)
    const kind = prev ? prev.kind : null
    const ver = nextVer()
    await cacheDelete(path, kind, ver)
    let queued
    try {
      queued = await enqueue({ method: 'DELETE', path, kind: kind || 'json', ver })
    } catch (e) {
      try { await restoreCache(path, prev) } catch (_) {}
      throw e
    }
    notify(path, null)
    return { path, writeId: ver, ver, seq: queued && queued.seq }
  }

  // Drain OUTSIDE the path lock, then report whether the path's op survived
  // (offline/transient → still queued, auto-syncs on the next online/focus
  // drain; sent → synced). NOTE: a fatal-rejected write is dead-lettered (op
  // removed), so it reports {synced} though the server refused it — no consumer
  // reads this flag, and the dead-letter reconcile re-syncs the mirror.
  async function settle(path, writeId, legacyShape = false) {
    await drainNow()
    const outcome = writeId ? await getWriteOutcome(writeId) : null
    const ops = await listOps()
    const stillQueued = writeId
      ? ops.some((op) => op.ver === writeId || op.seq === writeId)
      : ops.some((op) => op.path === path)
    if (legacyShape) return stillQueued ? { queued: true } : { synced: true }
    if (outcome && (outcome.state === 'rejected' || outcome.state === 'conflict')) {
      return {
        rejected: true,
        status: outcome.status,
        path: outcome.path,
        writeId: outcome.writeId,
        refusedValue: outcome.refusedValue,
      }
    }
    if (outcome && outcome.state === 'superseded') {
      return { superseded: true, path: outcome.path, writeId: outcome.writeId }
    }
    if (stillQueued) return { queued: true, path, writeId }
    return { synced: true, path, writeId, version: outcome && outcome.version }
  }

  function throwIfAborted(signal) {
    if (signal && signal.aborted) {
      const err = new Error('The operation was aborted')
      err.name = 'AbortError'
      throw err
    }
  }

  function normalizeDurableKind(value, opts) {
    if (opts && opts.kind) return opts.kind
    if (value instanceof Blob) return 'blob'
    if (typeof value === 'string') return 'text'
    return 'json'
  }

  async function durableWrite(path, value, opts = {}) {
    throwIfAborted(opts.signal)
    const kind = normalizeDurableKind(value, opts)
    let contentType = null
    if (kind === 'text') contentType = opts.contentType || 'text/plain;charset=utf-8'
    if (kind === 'blob') {
      if (!(value instanceof Blob)) throw new Error('mobius.storage.durableWrite: blob writes require a Blob or File value')
      contentType = opts.contentType || value.type || 'application/octet-stream'
    }
    const op = await withPathLock(path, async () => {
      if (!await hasIndexedDb()) {
        const sent = await writeDirect(path, value, kind, contentType, {
          ifMatch: opts.ifMatch,
          ifNoneMatch: opts.ifNoneMatch,
        })
        return { direct: true, sent }
      }
      return writeLocal(path, value, kind, contentType, {
        ifMatch: opts.ifMatch,
        ifNoneMatch: opts.ifNoneMatch,
      })
    })
    throwIfAborted(opts.signal)
    if (op.direct) {
      if (op.sent?.durability) return op.sent
      return {
        durability: 'synced', path, writeId: null,
        ...(op.sent?.version ? { version: op.sent.version } : {}),
      }
    }
    const result = await settle(path, op.writeId, false)
    throwIfAborted(opts.signal)
    if (result.rejected) {
      const code = result.status === 412 ? 'conflict' : 'dead_letter'
      throw new DurableWriteError(`mobius.storage.durableWrite: ${path} rejected (${result.status})`, {
        code,
        status: result.status,
        path,
        writeId: op.writeId,
        refusedValue: result.refusedValue,
        retryable: code === 'conflict',
      })
    }
    if (result.superseded) {
      throw new DurableWriteError(`mobius.storage.durableWrite: ${path} was superseded`, {
        code: 'superseded',
        path,
        writeId: op.writeId,
        retryable: false,
      })
    }
    return {
      durability: result.queued ? 'queued' : 'synced',
      path,
      writeId: op.writeId,
      ...(result.version ? { version: result.version } : {}),
    }
  }

  async function getWithVersion(path, kind = 'json') {
    return withPathLock(path, async () => {
      const hasIdb = await hasIndexedDb()
      if (!hasIdb && bridgeCall) {
        return bridgeCall('getWithVersion', [path, kind])
      }
      if (!onlineNow()) {
        if (!hasIdb) return { value: null, version: null, offline: true }
        const cached = await cacheGet(path)
        if (cached && cached.present !== false) assertReadKind(path, cached.kind, kind)
        return {
          value: finalizeRead(
            await effectiveValue(path, cached ? cached.data : null),
            kind,
            cached?.contentType || null,
            path,
          ),
          version: cached?.serverVersion || null,
          offline: true,
        }
      }
      const { value, version } = await fetchValueWithVersion(path, kind, true)
      if (!hasIdb) {
        return { value: finalizeRead(value, kind, null, path), version }
      }
      const ct = kind === 'blob'
        ? (value instanceof Blob ? value.type : null)
        : (kind === 'text' ? 'text/plain;charset=utf-8' : null)
      await cachePut(path, value, kind, ct, nextVer(), version)
      return {
        value: finalizeRead(await effectiveValue(path, value), kind, ct, path),
        version,
      }
    })
  }

  // Subscribe to local changes for a path: cb(value) fires immediately with the
  // current value (read via the kind-appropriate getter), then on every
  // set/remove for that path. Returns an unsubscribe fn. (A successful background
  // drain does NOT re-fire — it confirms the already-notified value server-side
  // without changing it.) NOTE for subscribeBlob: each fire delivers a fresh
  // Blob; the APP owns object-URL lifetime (revoke the previous URL on the next
  // fire / on unmount).
  function subscribeWith(path, cb, getter, kind = 'json') {
    if (bridgeSubscribe) {
      const detachBridge = bridgeSubscribe(kind, path, cb)
      let active = true
      const unsubscribe = () => {
        if (!active) return
        active = false
        bridgeUnsubscribers.delete(unsubscribe)
        try { detachBridge?.() } catch {}
      }
      bridgeUnsubscribers.add(unsubscribe)
      return unsubscribe
    }
    let set = subscribers.get(path)
    if (!set) { set = new Set(); subscribers.set(path, set) }
    // Fire the initial value once, but never let a slow initial get() resolve
    // AFTER a set() already pushed a newer value to this cb (stale-last).
    // `delivered` flips the moment notify() reaches this cb; the initial get()
    // then suppresses itself. notify() wins ties.
    let delivered = false
    const wrapped = (v) => { delivered = true; cb(v) }
    set.add(wrapped)
    getter(path).then((v) => {
      if (set.has(wrapped) && !delivered) { delivered = true; cb(v) }
    }).catch(() => {})
    return () => {
      const s = subscribers.get(path)
      if (s) { s.delete(wrapped); if (!s.size) subscribers.delete(path) }
    }
  }

  return {
    get, getText, getBlob,
    set, setText, setBlob,
    durableWrite,
    onDeadLetter,
    remove,
    async list(prefix, options = {}) {
      if (await hasIndexedDb()) return listInner(prefix, options)
      if (bridgeCall) return bridgeCall('list', [prefix, options])
      const norm = (prefix || '').replace(/^\/+|\/+$/g, '')
      return (await listServer(norm, options)) || []
    },
    subscribe(path, cb) { return subscribeWith(path, cb, get, 'json') },
    subscribeText(path, cb) { return subscribeWith(path, cb, getText, 'text') },
    subscribeBlob(path, cb) { return subscribeWith(path, cb, getBlob, 'blob') },
    async pendingCount() {
      if (await hasIndexedDb()) return (await listOps()).length
      return bridgeCall ? bridgeCall('pendingCount', []) : 0
    },
    getWithVersion,
    _queueSignals: bridgeCall
      ? (records) => bridgeCall('queueSignals', [records])
      : queueSignals,
    _pendingSignalCount: bridgeCall
      ? () => bridgeCall('pendingSignalCount', [])
      : async () => (await listSignals()).length,
    _drainSignals: bridgeCall
      ? () => bridgeCall('drainSignals', [])
      : drainSignals,
    _drain: bridgeCall ? () => bridgeCall('drain', []) : drain,
    _notify: notify,
    _receiveDeadLetter: dispatchDeadLetter,
    _destroy() {
      for (const unsubscribe of [...bridgeUnsubscribers]) unsubscribe()
      for (const ev of ['online', 'focus', 'pageshow']) {
        window.removeEventListener(ev, drainOnWake)
      }
      document.removeEventListener('visibilitychange', drainOnVisible)
      if (_signalRetryTimer !== null) {
        clearTimeout(_signalRetryTimer)
        _signalRetryTimer = null
      }
      subscribers.clear()
      deadLetterListeners.clear()
    },
  }
}

// Explicit data wipe is the destructive lifecycle boundary for browser-local
// app state. Soft uninstall deliberately does NOT call this: its server row,
// nonce, storage tree, and Undo window all remain the same installation.
export async function purgeAppRuntimeData(appId) {
  const sameApp = (record) => record && String(record.appId) === String(appId)
  const deleteMatching = (store) => {
    store.openCursor().onsuccess = (event) => {
      const cursor = event.target.result
      if (!cursor) return
      if (sameApp(cursor.value)) cursor.delete()
      cursor.continue()
    }
  }
  await Promise.all([
    withStores([STORE, CACHE_STORE, OUTCOME_STORE], 'readwrite', (stores) => {
      deleteMatching(stores[STORE])
      deleteMatching(stores[CACHE_STORE])
      deleteMatching(stores[OUTCOME_STORE])
    }),
    withSignalStore('readwrite', (store) => { deleteMatching(store) }),
  ])
}

function stableStringify(value) {
  if (value == null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']'
  const keys = Object.keys(value).sort()
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + stableStringify(value[k])).join(',') + '}'
}

function defaultIdentity(item) {
  if (item && typeof item === 'object') {
    if (item.clientKey != null) return String(item.clientKey)
    if (item.key != null) return String(item.key)
    if (item.id != null) return String(item.id)
  }
  return stableStringify(item)
}

function reconcileIdentity(current, incoming, identity = defaultIdentity) {
  if (!Array.isArray(incoming) || !Array.isArray(current)) return incoming
  const localByIdentity = new Map()
  for (const item of current) localByIdentity.set(identity(item), item)
  return incoming.map((item) => {
    const local = localByIdentity.get(identity(item))
    if (!local || !item || typeof item !== 'object' || typeof local !== 'object') return item
    if (!Object.prototype.hasOwnProperty.call(local, 'id')) return item
    return { ...item, id: local.id }
  })
}

function defaultDocumentMerge(base, mine, theirs, identity = defaultIdentity) {
  if (!Array.isArray(mine) || !Array.isArray(theirs)) return mine
  const merged = []
  const seen = new Set()
  for (const item of theirs) {
    const key = identity(item)
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(item)
  }
  for (const item of mine) {
    const key = identity(item)
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(item)
  }
  return reconcileIdentity(mine, merged, identity)
}

export function createUseDocument(storage, reactProvider = null) {
  return function useDocument(path, opts = {}) {
    const React = reactProvider || (typeof window !== 'undefined' ? window.React : null)
    if (!React || !React.useCallback || !React.useEffect || !React.useMemo || !React.useRef || !React.useState) {
      throw new Error('useDocument needs React — bind it via window.mobius.createUseDocument(React)')
    }
    const initialOpt = Object.prototype.hasOwnProperty.call(opts, 'initial') ? opts.initial : null
    // A hook cannot be called conditionally, so apps need a first-class way to
    // represent "there is no document right now" (for example, an editor with
    // no open item). Null/empty paths and enabled:false are idle controllers:
    // they perform no read, subscription, or write. This keeps that state out of
    // the storage namespace instead of making each app invent a sentinel file.
    const enabled = opts.enabled !== false && typeof path === 'string' && path.length > 0
    // Every path owns an isolated document controller. A hook instance survives
    // prop changes, so keeping value/base/version refs outside this boundary lets
    // document B inherit document A while B's refresh is still in flight. Late A
    // refreshes and subscription callbacks can then overwrite B as well. Besides
    // painting the wrong value, an immediate update can write A's document to B's
    // path. The controller is replaced synchronously during the B render; async A
    // work retains its old controller and may finish, but cannot mutate B.
    //
    // Resolving a lazy initial value here also keeps its identity stable for every
    // rerender of one path. A new path is the only event that establishes a new
    // initial value and write chain.
    const controller = React.useMemo(() => {
      const initialValue = typeof initialOpt === 'function' ? initialOpt() : initialOpt
      return {
        path,
        enabled,
        initialValue,
        value: initialValue,
        base: null,
        version: null,
        chain: Promise.resolve(),
      }
      // The path/enable pair deliberately owns controller identity. A changing
      // initializer must not reset a live document, while navigation or an
      // explicit enable transition creates a new isolated controller.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [path, enabled])
    const initialValue = controller.initialValue
    const identity = opts.identity || defaultIdentity
    const customMerge = opts.merge
    const mode = opts.mode || 'cas'
    const maxRetries = opts.maxRetries == null ? 3 : opts.maxRetries
    const onError = opts.onError
    const [state, setState] = React.useState(() => ({
      controller,
      value: initialValue,
      status: enabled ? 'loading' : 'idle',
      lastError: null,
    }))

    const setValue = React.useCallback((owner, value, status = 'ready', lastError = null) => {
      owner.value = value
      setState((previous) => previous.controller === owner
        ? { controller: owner, value, status, lastError }
        : previous)
    }, [])

    const refresh = React.useCallback(async () => {
      if (!enabled) return controller.value
      try {
        const loaded = storage.getWithVersion
          ? await storage.getWithVersion(path, 'json')
          : { value: await storage.get(path), version: undefined }
        const next = loaded.value == null ? initialValue : loaded.value
        const reconciled = reconcileIdentity(controller.value, next, identity)
        controller.base = reconciled
        controller.version = loaded.version || null
        setValue(controller, reconciled, 'ready', null)
        return reconciled
      } catch (e) {
        setState((previous) => previous.controller === controller
          ? { controller, value: controller.value, status: 'error', lastError: e }
          : previous)
        if (typeof onError === 'function') {
          onError(e, { path, phase: 'refresh' })
        }
        throw e
      }
    }, [path, initialValue, identity, controller, enabled, onError, setValue])

    React.useEffect(() => {
      let alive = true
      // Commit ownership before any synchronous subscription callback can land.
      // Until this effect runs, visibleState below already presents the new
      // controller's initial value rather than the previous path's React state.
      const status = enabled ? 'loading' : 'idle'
      setState((previous) => (
        previous.controller === controller && previous.status === status && previous.lastError == null
          ? previous
          : { controller, value: controller.value, status, lastError: null }
      ))
      if (!enabled) return undefined
      refresh().catch(() => {})
      const unsub = storage.subscribe(path, (next) => {
        if (!alive) return
        const value = next == null ? initialValue : reconcileIdentity(controller.value, next, identity)
        controller.base = value
        setValue(controller, value, 'ready', null)
      })
      return () => { alive = false; if (unsub) unsub() }
    }, [path, initialValue, identity, controller, enabled, refresh, setValue])

    const update = React.useCallback((fn) => {
      if (!enabled) {
        return Promise.reject(new Error('useDocument is idle; provide a document path before updating'))
      }
      const run = async () => {
        let attempt = 0
        const previous = controller.value
        const mine = fn(previous)
        setValue(controller, mine, 'saving', null)
        for (;;) {
          const base = controller.base
          let theirs = base
          let version = controller.version
          if (mode === 'cas' && storage.getWithVersion) {
            const loaded = await storage.getWithVersion(path, 'json')
            theirs = loaded.value == null ? initialValue : loaded.value
            version = loaded.version || null
          } else if (mode === 'lww') {
            try { theirs = (await storage.get(path)) ?? initialValue } catch (e) {}
          }
          const merged = customMerge
            ? customMerge(base, mine, theirs == null ? initialValue : theirs)
            : defaultDocumentMerge(base, mine, theirs == null ? initialValue : theirs, identity)
          const reconciled = reconcileIdentity(mine, merged, identity)
          try {
            const result = await storage.durableWrite(path, reconciled, {
              kind: 'json',
              ...(mode === 'cas' && version ? { ifMatch: version } : {}),
              ...(mode === 'cas' && !version ? { ifNoneMatch: true } : {}),
            })
            controller.base = reconciled
            controller.version = result.version || version || null
            setValue(controller, reconciled, result.durability === 'queued' ? 'saving' : 'ready', null)
            return result
          } catch (e) {
            if (e && e.code === 'conflict' && mode === 'cas' && attempt < maxRetries) {
              attempt += 1
              continue
            }
            setState((previous) => previous.controller === controller
              ? { controller, value: controller.value, status: 'error', lastError: e }
              : previous)
            if (typeof onError === 'function') {
              onError(e, { path, phase: 'update' })
            }
            throw e
          }
        }
      }
      const next = controller.chain.then(run, run)
      controller.chain = next.then(() => {}, () => {})
      return next
    }, [path, initialValue, identity, customMerge, mode, maxRetries, controller, enabled, onError, setValue])

    const setDoc = React.useCallback((next) => update(() => next), [update])

    // React state still belongs to the previous path during the first render
    // after a path change. Never expose that stale value while the new path's
    // refresh effect is being scheduled.
    const visibleState = state.controller === controller
      ? state
      : { value: controller.value, status: enabled ? 'loading' : 'idle', lastError: null }

    return {
      value: visibleState.value,
      status: visibleState.status,
      lastError: visibleState.lastError,
      update,
      set: setDoc,
      refresh,
    }
  }
}
