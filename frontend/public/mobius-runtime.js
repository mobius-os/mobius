// Shared mini-app runtime — exposes `window.mobius` to apps running in
// both the in-shell iframe (app-frame.html) and the standalone shell
// (routes/standalone.py). Imported at an absolute path
// (`/mobius-runtime.js`) so it resolves identically from
// `/api/apps/{id}/frame` and `/apps/{slug}/`. It lives in `public/`,
// so Vite copies it to the build root and Workbox precaches it
// (content-revisioned per deploy → fresh online, available offline).
//
// Purpose: let offline-capable apps (offline_capable flag, Tier 3)
// persist AND read through a network outage. Writes go to /api/storage; when
// offline or the request fails, they queue in IndexedDB (the outbox) and flush
// when the connection returns. Reads are read-through: an online get() mirrors
// the value into IndexedDB so a later offline get() serves the last-known
// value (overlaid with any pending write — read-your-writes). This is the
// SAME runtime for both hosts: the standalone PWA (standalone.py) and the
// in-shell iframe (app-frame.html) both `init()` it.
//
// API — intentionally small; grow it when a real app needs more. Reads/writes
// are TYPED: pick the method for your data shape (json is the default). A read
// of the wrong type for a path throws a clear error rather than corrupting:
//   window.mobius.appId
//   window.mobius.online                          -> probed reachability verdict (the shell's /api/health probe forwarded by AppCanvas; navigator.onLine is only the standalone-host seed/fallback)
//   window.mobius.storage.get(path)               -> JSON value | null  (offline-capable, SWR)
//   window.mobius.storage.set(path, data)         -> {synced} | {queued}
//   window.mobius.storage.getText(path)           -> string | null      (offline-capable, SWR)
//   window.mobius.storage.setText(path, str, opts?)-> {synced} | {queued}   opts.contentType
//   window.mobius.storage.getBlob(path)           -> Blob | null        (offline, cache-first)
//   window.mobius.storage.setBlob(path, blob, opts?)-> {synced} | {queued}  opts.contentType; <=25 MiB
//   window.mobius.storage.remove(path)            -> {synced} | {queued}
//   window.mobius.storage.list(prefix)            -> entries[]  (offline-capable: cache+outbox overlay)
//   window.mobius.storage.subscribe(path, cb)     -> unsubscribe fn (cb(json value))
//   window.mobius.storage.subscribeText(path, cb) -> unsubscribe fn (cb(string))
//   window.mobius.storage.subscribeBlob(path, cb) -> unsubscribe fn (cb(Blob); app revokes object URLs)
//   window.mobius.storage.pendingCount()          -> Promise<number>
//   window.mobius.storage.getWithVersion(path, kind?) -> {value, version}   read + its server ETag, for compare-and-swap
//   window.mobius.storage.durableWrite(path, data, opts?) -> {durability, path, writeId, version?}
//     opts.ifMatch=version makes it a CONDITIONAL write; a 412 rejects with DurableWriteError{code:'conflict', retryable:true}.
//     CAS a file with several writers (agent + cron + UI): getWithVersion -> merge -> durableWrite({ifMatch:version}); on a
//     'conflict' error re-read + retry (the app owns its merge; the runtime does NOT retry for you). See building-apps.md.
//   window.mobius.chat({mount, chatId?, picker?, ...}) -> Promise<handle>
//     Embeds the real agent chat (ChatView) in a nested iframe inside
//     `mount`. handle.on('ready'|'message-sent'|'turn-done'|'error', cb)
//     and handle.destroy(). See the "Agent-chat embed" block below.
//   window.mobius.nav.open(label, onBack)        -> { ready, close }  (shell-mediated back target; see building-apps.md)
//
// "No walls": this runtime is the easy DEFAULT, not a cage. An app is free to
// ignore it and use raw IndexedDB / OPFS / SQLite-wasm directly (same-origin
// iframe → all browser storage works), or talk to its own backend. The
// platform provides the on-ramp; it never gates the escape hatch.
//
// Conflict policy: last-write-wins at the path granularity. The newest
// write for a path supersedes any earlier one — enforced by coalescing
// the outbox on every write (enqueueing a newer write for a path purges the
// older queued op for it) and by routing ALL server writes through the single
// outbox-lock-serialized drain, so a stale queued op can never replay over a
// newer value. An app that needs per-record LWW stores one file per record
// (…/items/<uuid>.json) so concurrent edits to different records don't
// clobber each other. CRDTs are out of scope (overkill for single-owner
// personal apps).
//
// Smells: see the block at the bottom of this file.

const DB_NAME = 'mobius-outbox'
const STORE = 'ops'
// Read-through mirror of last-known server values, so get() works offline.
// Keyed by `${appId}:${path}` (one shared DB across all apps, like the outbox).
const CACHE_STORE = 'cache'
const OUTCOME_STORE = 'write_outcomes'
const DB_VERSION = 3
const MAX_WRITE_OUTCOMES = 200

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
const READ_TIMEOUT_MS = 2500
function fetchBounded(url, init) {
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  const opts = ctrl ? { ...init, signal: ctrl.signal } : init
  let timer
  if (ctrl) timer = setTimeout(() => ctrl.abort(), READ_TIMEOUT_MS)
  return fetch(url, opts).finally(() => { if (timer) clearTimeout(timer) })
}

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
export function makeStorage({ appId, getToken }) {
  const deadLetterListeners = new Set()

  function outcomeKey(writeId) { return appId + ':' + String(writeId) }

  function outcomeFromOp(op, state, extra = {}) {
    const writeId = op.ver || op.seq
    return {
      key: outcomeKey(writeId),
      appId,
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
        if (v && v.appId === appId) seen.push({ key: v.key, ts: v.ts || 0 })
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
        if (rec && rec.appId === appId && rec.state === 'rejected' && !rec.consumed) {
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
        if (v.appId === appId && v.path === path) {
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
      const queued = { ...op, appId, ts: Date.now() }
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
        if (cursor.value.appId === appId) box.value.push(cursor.value)
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
  function cacheKey(path) { return appId + ':' + path }

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
  function cachePut(path, data, kind = 'json', contentType = null, ver = nextVer()) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      store.put({ key: cacheKey(path), path, appId, data, kind, contentType, present: data !== null, ver, ts: Date.now() })
    })
  }

  // Tombstone the deletion (present:false, key kept) so an offline read after an
  // offline delete returns null. Preserve `kind` (so a fatal-DELETE reconcile /
  // re-delete re-reads with the right type) + stamp a `ver` for the CAS.
  function cacheDelete(path, kind = null, ver = nextVer()) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      store.put({ key: cacheKey(path), path, appId, data: null, kind, contentType: null, present: false, ver, ts: Date.now() })
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
          store.put({ key: cacheKey(path), path, appId, data: fresh, kind, contentType, present: fresh !== null, ver: nextVer(), ts: Date.now() })
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
          store.put({ key: cacheKey(path), path, appId, data: fresh, kind, contentType, present: fresh !== null, ver: nextVer(), ts: Date.now() })
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
        const k = ' mobius-blob-probe:' + appId   //   prefix can't collide with cacheKey()
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

  // Send one queued op to the storage API. PUT and DELETE are both
  // idempotent by path, so replaying a flushed op is safe. A DELETE
  // that 404s means the file is already absent — the intended end state
  // — so we treat it as success. A 401 means the token is stale; we
  // throw 'AUTH' so the drain stops WITHOUT discarding the op (a fresh
  // token on the next trigger retries it).
  async function send(op) {
    const token = await getToken()
    const url = `/api/storage/apps/${appId}/${op.path}`
    const init = { method: op.method, headers: { Authorization: `Bearer ${token}` } }
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
    const res = await fetch(url, init)   // network failure throws -> transient
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
    if (!navigator.onLine) return
    const ops = await listOps()           // FIFO by seq
    for (const op of ops) {
      try {
        const sent = await send(op)
        await recordWriteOutcome(outcomeFromOp(op, 'confirmed', { version: sent && sent.version }))
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
          // eslint-disable-next-line no-console
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
          if (navigator.onLine) {
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

  for (const ev of ['online', 'focus', 'pageshow']) {
    window.addEventListener(ev, drain)
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') drain()
  })

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
  function get(path) { return withPathLock(path, () => getInner(path, 'json')) }
  function getText(path) { return withPathLock(path, () => getInner(path, 'text')) }
  function getBlob(path) { return withPathLock(path, () => getInner(path, 'blob')) }
  // Writers: the LOCAL mutation runs under the path lock (ordered vs reads + other
  // writes); the server drain runs in settle() OUTSIDE that lock (deadlock-safe).
  function set(path, data) {
    return withPathLock(path, () => writeLocal(path, data, 'json', null))
      .then((op) => settle(path, op.writeId, true))
  }
  async function setText(path, text, opts) {
    if (typeof text !== 'string') {
      throw new Error('mobius.storage.setText: value must be a string')
    }
    const ct = (opts && opts.contentType) || 'text/plain;charset=utf-8'
    const op = await withPathLock(path, () => writeLocal(path, text, 'text', ct))
    return settle(path, op.writeId, true)
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
    if (!(await blobStorable())) {
      throw new Error('mobius.storage.setBlob: this browser cannot store Blobs offline')
    }
    const ct = (opts && opts.contentType) || blob.type || 'application/octet-stream'
    const op = await withPathLock(path, () => writeLocal(path, blob, 'blob', ct))
    return settle(path, op.writeId, true)
  }
  function remove(path) {
    return withPathLock(path, () => removeLocal(path))
      .then((op) => settle(path, op.writeId, true))
  }

  // Page the server's authoritative listing of the immediate children under
  // `prefix`. Returns the entries ARRAY (each {name, path, type, size,
  // modified_at, mime_type?}), `[]` for an empty/unknown dir, and `null` on
  // network failure (offline/transient). Walks every page so list() is true
  // enumeration, not just the server's first page (capped at 500); the guard
  // bounds a pathological/looping cursor.
  async function listServer(prefix) {
    try {
      const token = await getToken()
      const entries = []
      let cursor = null
      for (let guard = 0; guard < 10000; guard++) {
        const q = `?limit=500${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
        const res = await fetchBounded(
          `/api/storage/apps-list/${appId}/${prefix || ''}${q}`,
          { headers: { Authorization: `Bearer ${token}` } },
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
        if (v.appId === appId && v.present) {
          box.value.push({ path: v.path, kind: v.kind, contentType: v.contentType })
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
  async function listInner(prefix) {
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
        if (byName.has(rest)) return  // never downgrade an existing (server) entry
        const mime = (meta && meta.contentType)
          || (meta && meta.kind === 'json' ? 'application/json' : null)
        byName.set(rest, { name: rest, path: base + rest, type: 'file', mime_type: mime })
      } else {
        const dname = rest.slice(0, slash)
        if (!byName.has(dname)) {
          byName.set(dname, { name: dname, path: base + dname, type: 'directory' })
        }
      }
    }

    const server = await listServer(norm)
    if (server) {
      for (const e of server) byName.set(e.name, e)
    } else {
      for (const c of await listCachePresent()) addDerived(c.path, c)
    }

    // Overlay the outbox (the queue coalesces to <=1 op per path, so the last
    // intent for a path is the only one): a PUT ensures its child shows even
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
    const token = await getToken()
    const headers = { Authorization: `Bearer ${token}` }
    if (wantVersion) headers['X-Mobius-Version'] = '1'
    const res = await fetchBounded(`/api/storage/apps/${appId}/${path}`, { headers })
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
      let data
      try { data = await fetchValue(path, kind) } catch (e) { return }
      if ((await listOps()).some((op) => op.path === path)) return
      const prev = await cacheGet(path)
      if (prev && sameJson(prev.data, data)) return
      await cachePut(path, data, kind, prev ? prev.contentType : null)
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
      cached && cached.present === false && kind === 'blob' && navigator.onLine
    if (cached && !staleBlobTombstone) {
      // Present value: the stored kind is authoritative — a wrong-typed read
      // throws (loud) instead of handing back a string-as-Blob. A tombstone
      // (present:false) has no value to type-check; it resolves null below.
      if (cached.present !== false) assertReadKind(path, cached.kind, kind)
      if (kind !== 'blob' && navigator.onLine) scheduleRevalidate(path, kind)
      return finalizeRead(await effectiveValue(path, cached.data), kind, cached.contentType, path)
    }
    if (navigator.onLine) {
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
    const op = await withPathLock(path, () => writeLocal(path, value, kind, contentType, {
      ifMatch: opts.ifMatch,
      ifNoneMatch: opts.ifNoneMatch,
    }))
    throwIfAborted(opts.signal)
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
      const { value, version } = await fetchValueWithVersion(path, kind, true)
      const ct = kind === 'blob'
        ? (value instanceof Blob ? value.type : null)
        : (kind === 'text' ? 'text/plain;charset=utf-8' : null)
      await cachePut(path, value, kind, ct)
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
  function subscribeWith(path, cb, getter) {
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
    list: listInner,
    subscribe(path, cb) { return subscribeWith(path, cb, get) },
    subscribeText(path, cb) { return subscribeWith(path, cb, getText) },
    subscribeBlob(path, cb) { return subscribeWith(path, cb, getBlob) },
    async pendingCount() {
      return (await listOps()).length
    },
    getWithVersion,
    _drain: drain,
    _notify: notify,
  }
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
    if (!React || !React.useCallback || !React.useEffect || !React.useRef || !React.useState) {
      throw new Error('useDocument needs React — bind it via window.mobius.createUseDocument(React)')
    }
    const initialOpt = Object.prototype.hasOwnProperty.call(opts, 'initial') ? opts.initial : null
    const initialValue = typeof initialOpt === 'function' ? initialOpt() : initialOpt
    const identity = opts.identity || defaultIdentity
    const merge = opts.merge || ((base, mine, theirs) => defaultDocumentMerge(base, mine, theirs, identity))
    const mode = opts.mode || 'cas'
    const maxRetries = opts.maxRetries == null ? 3 : opts.maxRetries
    const [state, setState] = React.useState(() => ({ value: initialValue, status: 'loading', lastError: null }))
    const valueRef = React.useRef(initialValue)
    const baseRef = React.useRef(null)
    const versionRef = React.useRef(null)
    const chainRef = React.useRef(Promise.resolve())
    const optsRef = React.useRef(opts)
    optsRef.current = opts

    const setValue = React.useCallback((value, status = 'ready', lastError = null) => {
      valueRef.current = value
      setState({ value, status, lastError })
    }, [])

    const refresh = React.useCallback(async () => {
      try {
        const loaded = storage.getWithVersion
          ? await storage.getWithVersion(path, 'json')
          : { value: await storage.get(path), version: undefined }
        const next = loaded.value == null ? initialValue : loaded.value
        const reconciled = reconcileIdentity(valueRef.current, next, identity)
        baseRef.current = reconciled
        versionRef.current = loaded.version || null
        setValue(reconciled, 'ready', null)
        return reconciled
      } catch (e) {
        setState((prev) => ({ ...prev, status: 'error', lastError: e }))
        if (optsRef.current && typeof optsRef.current.onError === 'function') {
          optsRef.current.onError(e, { path, phase: 'refresh' })
        }
        throw e
      }
    }, [path, initialValue, identity, setValue])

    React.useEffect(() => {
      let alive = true
      refresh().catch(() => {})
      const unsub = storage.subscribe(path, (next) => {
        if (!alive) return
        const value = next == null ? initialValue : reconcileIdentity(valueRef.current, next, identity)
        baseRef.current = value
        setValue(value, 'ready', null)
      })
      return () => { alive = false; if (unsub) unsub() }
    }, [path, initialValue, identity, refresh, setValue])

    const update = React.useCallback((fn) => {
      const run = async () => {
        let attempt = 0
        const previous = valueRef.current
        const mine = fn(previous)
        setValue(mine, 'saving', null)
        for (;;) {
          const base = baseRef.current
          let theirs = base
          let version = versionRef.current
          if (mode === 'cas' && storage.getWithVersion) {
            const loaded = await storage.getWithVersion(path, 'json')
            theirs = loaded.value == null ? initialValue : loaded.value
            version = loaded.version || null
          } else if (mode === 'lww') {
            try { theirs = (await storage.get(path)) ?? initialValue } catch (e) {}
          }
          const merged = merge(base, mine, theirs == null ? initialValue : theirs)
          const reconciled = reconcileIdentity(mine, merged, identity)
          try {
            const result = await storage.durableWrite(path, reconciled, {
              kind: 'json',
              ...(mode === 'cas' && version ? { ifMatch: version } : {}),
              ...(mode === 'cas' && !version ? { ifNoneMatch: true } : {}),
            })
            baseRef.current = reconciled
            versionRef.current = result.version || version || null
            setValue(reconciled, result.durability === 'queued' ? 'saving' : 'ready', null)
            return result
          } catch (e) {
            if (e && e.code === 'conflict' && mode === 'cas' && attempt < maxRetries) {
              attempt += 1
              continue
            }
            setState({ value: valueRef.current, status: 'error', lastError: e })
            if (optsRef.current && typeof optsRef.current.onError === 'function') {
              optsRef.current.onError(e, { path, phase: 'update' })
            }
            throw e
          }
        }
      }
      const next = chainRef.current.then(run, run)
      chainRef.current = next.then(() => {}, () => {})
      return next
    }, [path, initialValue, identity, merge, mode, maxRetries, setValue])

    const setDoc = React.useCallback((next) => update(() => next), [update])

    return {
      value: state.value,
      status: state.status,
      lastError: state.lastError,
      update,
      set: setDoc,
      refresh,
    }
  }
}

// ── App analytics: window.mobius.signal() (design §3) ──────────────
//
// Fire-and-forget telemetry for Reflection. Buffers events in memory and
// debounce-flushes them to `signals.jsonl` in the app's own storage
// path, overwriting the full file on each flush (avoids read-append
// races). On first signal of a session the existing signals.jsonl is
// read once and its tail (≤400 lines) seeds the in-memory buffer, so
// entries from prior sessions are preserved across the overwrite.
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
//   - ring buffer cap 500; oldest entries evicted when full
//   - debounce: at most one flush per 5 seconds; a final flush fires on
//     pagehide and visibilitychange-hidden so no events are lost on tab close
//   - on first signal: read existing signals.jsonl once, seed buffer
//     with its tail ≤400 lines, then overwrite going forward
//
// Exported as makeSignal(appId, storage) → the signal() fn, so
// init() can wire it and tests can drive it without a full init().

const SIGNAL_PATH = 'signals.jsonl'
const SIGNAL_BUF_CAP = 500
const SIGNAL_SEED_CAP = 400
const SIGNAL_FLUSH_INTERVAL_MS = 5000

export function makeSignal(appId, storage) {
  if (!storage || !appId) return () => {}

  // State for this session: whether we've seeded the buffer from the
  // existing file, and the ring buffer of {ts, name, ...payload} lines.
  let _seeded = false
  let _seeding = false
  let _buf = []      // objects not yet serialised
  let _flushTimer = null
  let _flushPending = false

  // Validate and normalise one signal call. Returns null if invalid.
  function _prepare(name, payload) {
    if (typeof name !== 'string' || !name) return null
    // Kebab-case is recommended but we only reject non-strings, not bad format.
    const entry = { ts: new Date().toISOString(), name }
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      for (const [k, v] of Object.entries(payload)) {
        const t = typeof v
        if (t === 'string' || t === 'number' || t === 'boolean') {
          entry[k] = v
        }
        // non-primitives silently dropped
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

  // Flush the current buffer to storage as a full overwrite of signals.jsonl.
  // The overwrite-not-append design avoids read-append races (two flushes
  // from concurrent tabs would each read, append, and write back, potentially
  // losing one batch; a full overwrite means whichever tab wins last has the
  // authoritative snapshot of ITS buffer, which is already seeded from the
  // prior file). Both tabs share the same seeded history so no entries are lost.
  async function _flush() {
    _flushPending = false
    if (_buf.length === 0) return
    try {
      const lines = _buf.map((e) => JSON.stringify(e)).join('\n') + '\n'
      await storage.setText(SIGNAL_PATH, lines)
    } catch (e) {
      // storage unavailable (offline, token expired) — entries stay in
      // the buffer and will be included in the next flush attempt
    }
  }

  // Schedule a debounced flush. At most one flush every 5 seconds.
  function _scheduleFlush() {
    if (_flushTimer !== null || _flushPending) return
    _flushTimer = setTimeout(() => {
      _flushTimer = null
      _flushPending = true
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
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') _flushNow()
      })
    } catch (e) {}
  }

  // Seed the in-memory buffer from the existing signals.jsonl (≤400 tail
  // lines) so that prior-session entries survive the next overwrite. Called
  // once per session, before the first flush. Runs async; calls that arrive
  // while seeding are buffered normally and included in the eventual flush.
  async function _seed() {
    _seeding = true
    try {
      const text = await storage.getText(SIGNAL_PATH)
      if (text) {
        const tail = text
          .split('\n')
          .filter((l) => l.trim())
          .slice(-SIGNAL_SEED_CAP)
          .map((l) => { try { return JSON.parse(l) } catch (e) { return null } })
          .filter(Boolean)
        // Prepend the seeded tail; then add any entries buffered while seeding;
        // then apply the cap so the total stays within SIGNAL_BUF_CAP.
        _buf = [...tail, ..._buf].slice(-SIGNAL_BUF_CAP)
      }
    } catch (e) {
      // seed failed (file absent, storage error) — proceed with empty history
    }
    _seeded = true
    _seeding = false
  }

  // The public signal() function. Fire-and-forget: starts the async seed
  // path if needed, pushes the entry, schedules a debounced flush.
  function signal(name, payload) {
    try {
      const entry = _prepare(name, payload)
      if (!entry) return
      _ensureHooks()
      _push(entry)
      // Kick off the one-time seed from storage. The entry was already pushed
      // to _buf above; after seeding it gets prepended to the historical tail,
      // so it will be included in the next flush regardless of seed timing.
      if (!_seeded && !_seeding) {
        _seed().then(_scheduleFlush).catch(() => { _seeded = true; _scheduleFlush() })
        return
      }
      if (_seeded) _scheduleFlush()
      // While seeding: entry is in _buf, flush will be triggered by _seed().then
    } catch (e) {
      // signal() must never propagate exceptions
    }
  }

  return signal
}

// ── Agent-chat embed (capability A, design §1) ──────────────────────
//
// `window.mobius.chat(opts)` mounts the real ChatView (the shell's chat
// UI) inside a nested same-origin iframe at the shell embed route, so an
// app gets a live agent conversation WITHOUT reimplementing chat. The
// embed is a RENDERER, never the trust boundary (§0b): a same-origin app
// already holds the owner JWT, so enforcement is server-side.
// `picker` defaults true; set picker:false for a model-locked chat with
// no model/effort/provider picker while keeping attach files + send.
//
// This is the PARENT side of the embed postMessage protocol. The CHILD
// side is frontend/src/components/ChatEmbed/ChatEmbed.jsx, and the shapes
// are defined once in frontend/src/lib/chatEmbed.js. mobius-runtime.js is
// served verbatim from /public and can't import that bundled /src module,
// so the few constants below are MIRRORED (not imported) — keep them in
// sync, the way app-frame.html ↔ AppCanvas.jsx already are.
const EMBED_NS = 'moebius:chat-embed:'
const EMBED_INIT = EMBED_NS + 'init'
const EMBED_READY = EMBED_NS + 'ready'
const EMBED_MESSAGE_SENT = EMBED_NS + 'message-sent'
const EMBED_TURN_DONE = EMBED_NS + 'turn-done'
const EMBED_ERROR = EMBED_NS + 'error'
// Context protocol — mirrored from src/lib/chatEmbed.js; keep in sync.
const EMBED_CONTEXT_REQUEST = EMBED_NS + 'context-request'
const EMBED_CONTEXT_RESPONSE = EMBED_NS + 'context-response'

// The four embed handle events split into two kinds. 'ready' and 'error'
// are one-shot lifecycle events, but the child posts its mount-time READY
// before the app (which only gets the handle AFTER `await chat(...)`) can
// attach a listener — so a handler registered right after the await would
// miss it. We make those two STICKY: emit() records the latest detail and a
// late on('ready'|'error', cb) replays it synchronously. 'message-sent' and
// 'turn-done' are repeatable (once per turn) and NOT sticky — replaying a
// past one to a late listener would double-fire. This mirrors makeEmitter in
// frontend/src/lib/chatEmbed.js (served verbatim from /public, can't import
// the /src module); keep the two in sync.
// Handle events use the SHORT names ('ready' etc.) the app passes to
// handle.on() and makeChat passes to emit() — not the namespaced wire types.
const EMBED_STICKY = new Set(['ready', 'error'])

function makeEmbedEmitter() {
  // Known events only — an unknown name is ignored on both emit and on,
  // preserving the original `if (listeners[event])` guard.
  const listeners = { ready: [], 'message-sent': [], 'turn-done': [], error: [] }
  const lastEmit = {}
  function emit(name, detail) {
    if (EMBED_STICKY.has(name)) lastEmit[name] = detail
    const cbs = listeners[name]
    if (!cbs) return
    for (const cb of cbs) {
      try { cb(detail) } catch (e) {}
    }
  }
  function on(name, cb) {
    if (!listeners[name]) return
    listeners[name].push(cb)
    if (EMBED_STICKY.has(name) && hasOwn(lastEmit, name)) {
      try { cb(lastEmit[name]) } catch (e) {}
    }
  }
  return { emit, on }
}

let _embedSeq = 0

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj || {}, key)

export function appChatMetadataBody(opts = {}, { includeProvider = true } = {}) {
  const body = {}
  if (hasOwn(opts, 'systemPrompt')) {
    body.system_prompt = opts.systemPrompt == null ? '' : String(opts.systemPrompt)
  }
  if (hasOwn(opts, 'model')) {
    body.model = opts.model == null ? '' : String(opts.model)
  }
  if (includeProvider && hasOwn(opts, 'provider')) {
    const provider = opts.provider == null ? '' : String(opts.provider).trim()
    if (provider) body.provider = provider
  }
  // projectId scopes an embedded app chat to ONE of the app's projects
  // (feature 135): the backend stores it in agent_settings_json and points the
  // injected <app_context> at projects/<id>/. Meaningful only at create; the
  // PATCH path ignores it (AppChatPatch has no project_id), so it's harmless to
  // forward in both. Apps pair it with a per-project persist key
  // (e.g. persist: 'projects/<id>/chat_id.json') for create-once-per-project.
  if (hasOwn(opts, 'projectId')) {
    const pid = opts.projectId == null ? '' : String(opts.projectId).trim()
    if (pid) body.project_id = pid
  }
  if (hasOwn(opts, 'scope')) {
    const scope = opts.scope == null ? '' : String(opts.scope).trim()
    if (scope) body.scope = scope
  }
  if (hasOwn(opts, 'scopeLabel')) {
    const label = opts.scopeLabel == null ? '' : String(opts.scopeLabel).trim()
    if (label) body.scope_label = label
  }
  return body
}

function makeChat({ appId, getToken, storage }) {
  // Lazily create a chat the agent turn can be attributed to, via the
  // app-attributed backend contract (design §1.1: POST /api/app-chats).
  // The ordinary /api/chats create route is owner-only and intentionally
  // leaves created_by_app_id NULL.
  // /api/app-chats is APP-TOKEN-ONLY: the create + the resume PATCH both 403 an
  // owner JWT ("Use POST /api/chats for owner-created chats." / "App chat
  // metadata may only be changed by an app token."). The in-shell app frame's
  // getToken returns the OWNER JWT (app-frame.html posts the shell token), so
  // calling /api/app-chats with it 403d — surfacing as the embedded chat's
  // "create/update failed (403)". Mint a short-lived app-scoped token for THIS
  // app (the owner JWT is allowed to mint one for any of its apps via
  // /api/auth/app-token) and use it for the whole app-chat lifecycle. A
  // STANDALONE host's getToken already returns an app token; if the mint fails
  // there we fall back to it (still app-scoped, so app-chats accepts it).
  // Cached; re-minted on a 401 (the 8-hour app token expired mid-session).
  let _appChatToken = null
  async function appChatToken(refresh) {
    if (_appChatToken && !refresh) return _appChatToken
    const owner = await getToken()
    try {
      const res = await fetch('/api/auth/app-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${owner}` },
        body: JSON.stringify({ app_id: Number(appId) }),
      })
      if (res.ok) {
        const data = await res.json()
        if (data && data.token) { _appChatToken = data.token; return _appChatToken }
      }
    } catch (e) { /* fall through to the host token */ }
    return owner
  }

  // Call an /api/app-chats endpoint with the app-scoped token, re-minting once on
  // a 401 (the app token expired mid-session). init.headers is merged so the
  // caller sets Content-Type without clobbering Authorization.
  async function appChatFetch(url, init = {}) {
    const withAuth = (t) => ({ ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${t}` } })
    let res = await fetch(url, withAuth(await appChatToken()))
    if (res.status === 401) res = await fetch(url, withAuth(await appChatToken(true)))
    return res
  }

  async function listChats(opts = {}) {
    const scope = opts.scope == null ? '' : String(opts.scope).trim()
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : ''
    const res = await appChatFetch(`/api/app-chats${qs}`)
    if (!res.ok) {
      throw new Error(`window.mobius.chat: list failed (${res.status})`)
    }
    const data = await res.json()
    return Array.isArray(data) ? data : []
  }

  async function createChat(opts) {
    // Root-relative, same as storage above — the app frame is same-origin
    // with the shell, so /api/app-chats resolves regardless of the deploy
    // prefix the browser uses for the embed iframe src.
    const res = await appChatFetch('/api/app-chats', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        title: opts && opts.title ? opts.title : 'App chat',
        // systemPrompt / model / provider are part of the contract the
        // backend agent is shaping (per-app system prompt is its own
        // small design, design §1.5). Forward them so they're honored
        // the moment the backend accepts them; harmless extra fields
        // until then.
        ...appChatMetadataBody(opts, { includeProvider: true }),
      }),
    })
    if (!res.ok) {
      throw new Error(`window.mobius.chat: create failed (${res.status})`)
    }
    const data = await res.json()
    if (!data || !data.id) {
      throw new Error('window.mobius.chat: create failed (missing chat id)')
    }
    return String(data.id)
  }

  async function updateChat(chatId, opts) {
    if (!chatId || !opts) return
    const body = {}
    Object.assign(body, appChatMetadataBody(opts, { includeProvider: false }))
    if (!Object.keys(body).length) return
    const res = await appChatFetch(`/api/app-chats/${encodeURIComponent(chatId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      throw new Error(`window.mobius.chat: update failed (${res.status})`)
    }
  }

  // Open the embed in a nested iframe inside `mount` (an element the app
  // controls). Returns a handle: { chatId, instanceId, iframe, destroy,
  // on(event, cb) }. Events: 'ready' | 'message-sent' | 'turn-done' |
  // 'error', each carrying { chatId }.
  //
  // The helper owns the WHOLE app-chat lifecycle so apps don't hand-roll it:
  //   - `persist: '<storage-key>'` — create the app-chat once, save its id to
  //     that storage path, and REUSE it on every later mount (PATCHing the
  //     prompt on resume). Without it, an explicit `chatId` is used as-is, or
  //     an ephemeral chat is created (the original behavior).
  //   - `systemPrompt` / `title` / `model` / `provider` — shape the chat on
  //     create and re-apply (PATCH) on resume.
  //   - `onReady` / `onTurnDone` / `onMessageSent` / `onError` — handlers wired
  //     BEFORE the iframe mounts, so they never miss the mount-time READY.
  // So the common app usage is one call:
  //   const h = await window.mobius.chat({ mount, persist: 'chat_id.json',
  //     systemPrompt, picker: false, onTurnDone: refresh })  // h.destroy() on unmount
  return async function chat(opts = {}) {
    const mount = opts.mount
    if (!mount || typeof mount.appendChild !== 'function') {
      throw new Error('window.mobius.chat: opts.mount must be a DOM element')
    }
    // `persist` lets the helper own create-once-then-reuse. The id is stored as
    // `{ id }` (the shape apps already wrote to chat_id.json); we also accept a
    // bare string or `{ chatId }` on read for tolerance.
    const persistKey = typeof opts.persist === 'string' && opts.persist ? opts.persist : null
    async function loadPersistedId() {
      if (!persistKey || !storage) return null
      try {
        const saved = await storage.get(persistKey)
        const id = saved && (typeof saved === 'string' ? saved : (saved.id || saved.chatId))
        return id ? String(id) : null
      } catch (e) { return null }
    }
    function savePersistedId(id) {
      if (!persistKey || !storage || !id) return
      try { Promise.resolve(storage.set(persistKey, { id: String(id) })).catch(() => {}) } catch (e) {}
    }
    // Explicit chatId wins; else a persisted id (PATCH its prompt on resume);
    // else create one and persist it. With no persist + no chatId this is the
    // original "ephemeral chat" path.
    let chatId = opts.chatId ? String(opts.chatId) : await loadPersistedId()
    const fromPersist = !opts.chatId && !!chatId
    if (chatId) {
      try {
        await updateChat(chatId, opts)
      } catch (e) {
        // A persisted chat id can go stale: the empty-chat sweeper purges an
        // app-chat that never got a turn past its grace window, but the
        // persisted id (chat_id.json) still points at it, so the resume PATCH
        // 404s. Self-heal by dropping the dead id and creating a fresh chat —
        // only for a persisted id; an explicit caller-supplied chatId surfaces
        // the error (the caller named a specific chat and should hear it's gone).
        const dead = fromPersist && /\((?:404|410)\)/.test(String(e && e.message))
        if (!dead) throw e
        chatId = await createChat(opts)
        savePersistedId(chatId)
      }
    } else {
      chatId = await createChat(opts)
      savePersistedId(chatId)
    }
    const pickerOn = opts.picker !== false
    const scopeValue = hasOwn(opts, 'scope') && opts.scope != null
      ? String(opts.scope).trim()
      : ''
    const controlsOn = opts.controls === true || (opts.controls !== false && !!scopeValue)
    const instanceId = `${appId}:${++_embedSeq}:${Date.now()}`
    // Sticky 'ready'/'error' so a handler attached after `await chat(...)`
    // still observes the embed's mount-time READY (see makeEmbedEmitter).
    const { emit, on: onEvent } = makeEmbedEmitter()
    // opts handlers register before mount → they never miss the early READY.
    if (typeof opts.onReady === 'function') onEvent('ready', opts.onReady)
    if (typeof opts.onTurnDone === 'function') onEvent('turn-done', opts.onTurnDone)
    if (typeof opts.onMessageSent === 'function') onEvent('message-sent', opts.onMessageSent)
    if (typeof opts.onError === 'function') onEvent('error', opts.onError)

    function embedSrcFor(id) {
      const params = new URLSearchParams()
      if (id) params.set('chatId', id)
      if (!pickerOn) params.set('picker', '0')
      const qs = params.toString()
      return qs ? `/shell/embed/chat?${qs}` : '/shell/embed/chat'
    }

    const iframe = document.createElement('iframe')
    iframe.title = 'Agent chat'
    // Fixed-height panel (design §1.2): the app sizes `mount`; the iframe
    // fills it. We deliberately do NOT relay content height across the
    // three frames — ChatView owns its own scroll + spacer.
    iframe.style.cssText = 'width:100%;height:100%;border:0;display:block'
    // Same sandbox as the app frame so ChatView (which reads the owner
    // JWT from localStorage and hits /api/chats) works same-origin.
    iframe.setAttribute(
      'sandbox',
      'allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation',
    )
    iframe.src = embedSrcFor(chatId)

    // Sanitize quickActions: max 4, each must have string label + prompt.
    const quickActions = Array.isArray(opts.quickActions)
      ? opts.quickActions
          .filter(a => a && typeof a.label === 'string' && typeof a.prompt === 'string')
          .slice(0, 4)
      : undefined

    let controlsShell = null
    let frameMount = mount
    let selectEl = null
    let newChatButton = null
    let onChatSelectChange = null
    let onNewChatClick = null

    function errorText(err) {
      return err && err.message ? err.message : String(err || 'Unknown error')
    }

    function chatOptionLabel(chat) {
      const label = chat && typeof chat.scope_label === 'string' ? chat.scope_label.trim() : ''
      const title = chat && typeof chat.title === 'string' ? chat.title.trim() : ''
      if (label) return label
      if (title) return title
      return chat && chat.id ? `Chat ${String(chat.id).slice(0, 8)}` : 'Chat'
    }

    function renderChatOptions(chats) {
      if (!selectEl) return
      const options = []
      const seen = new Set()
      for (const chat of chats || []) {
        if (!chat || !chat.id) continue
        const id = String(chat.id)
        if (seen.has(id)) continue
        seen.add(id)
        options.push({ ...chat, id })
      }
      if (chatId && !seen.has(chatId)) {
        options.unshift({
          id: chatId,
          title: opts.title || 'Current chat',
          scope_label: opts.scopeLabel || opts.title || 'Current chat',
        })
      }
      selectEl.replaceChildren(...options.map((chat) => {
        const option = document.createElement('option')
        option.value = chat.id
        option.textContent = chatOptionLabel(chat)
        return option
      }))
      selectEl.value = chatId || ''
    }

    async function refreshChatOptions() {
      if (!controlsOn || !selectEl) return
      try {
        renderChatOptions(await listChats(opts))
      } catch (e) {
        emit('error', { chatId, error: errorText(e) })
      }
    }

    async function switchToChat(nextId) {
      nextId = nextId ? String(nextId) : ''
      if (!nextId || nextId === chatId) return
      const previousId = chatId
      if (selectEl) selectEl.disabled = true
      try {
        await updateChat(nextId, opts)
        chatId = nextId
        savePersistedId(chatId)
        iframe.src = embedSrcFor(chatId)
      } catch (e) {
        if (selectEl) selectEl.value = previousId || ''
        emit('error', { chatId: previousId, error: errorText(e) })
      } finally {
        if (selectEl) selectEl.disabled = false
      }
    }

    async function startNewChat() {
      if (newChatButton) newChatButton.disabled = true
      try {
        chatId = await createChat(opts)
        savePersistedId(chatId)
        iframe.src = embedSrcFor(chatId)
        await refreshChatOptions()
        if (selectEl) selectEl.value = chatId
      } catch (e) {
        emit('error', { chatId, error: errorText(e) })
      } finally {
        if (newChatButton) newChatButton.disabled = false
      }
    }

    if (controlsOn) {
      controlsShell = document.createElement('div')
      controlsShell.style.cssText = (
        'width:100%;height:100%;min-height:0;display:flex;flex-direction:column;'
      )
      const chrome = document.createElement('div')
      chrome.style.cssText = (
        'display:flex;align-items:center;gap:6px;flex:0 0 auto;'
        + 'padding:6px 8px;border-bottom:1px solid rgba(148,163,184,.28);'
        + 'background:rgba(248,250,252,.94);'
      )
      selectEl = document.createElement('select')
      selectEl.setAttribute('aria-label', 'Chat')
      selectEl.style.cssText = (
        'min-width:0;flex:1 1 auto;height:28px;border:1px solid rgba(148,163,184,.55);'
        + 'border-radius:6px;background:#fff;color:#111827;font:500 12px system-ui,sans-serif;'
        + 'padding:0 26px 0 8px;'
      )
      newChatButton = document.createElement('button')
      newChatButton.type = 'button'
      newChatButton.textContent = '+'
      newChatButton.title = 'New chat'
      newChatButton.setAttribute('aria-label', 'New chat')
      newChatButton.style.cssText = (
        'width:28px;height:28px;flex:0 0 28px;border:1px solid rgba(148,163,184,.55);'
        + 'border-radius:6px;background:#fff;color:#111827;font:600 18px/1 system-ui,sans-serif;'
        + 'display:grid;place-items:center;cursor:pointer;'
      )
      onChatSelectChange = () => { switchToChat(selectEl.value).catch(() => {}) }
      onNewChatClick = () => { startNewChat().catch(() => {}) }
      selectEl.addEventListener('change', onChatSelectChange)
      newChatButton.addEventListener('click', onNewChatClick)
      chrome.appendChild(selectEl)
      chrome.appendChild(newChatButton)
      frameMount = document.createElement('div')
      frameMount.style.cssText = 'min-height:0;flex:1 1 auto;'
      controlsShell.appendChild(chrome)
      controlsShell.appendChild(frameMount)
      renderChatOptions([])
    }

    function sendInit() {
      const w = iframe.contentWindow
      if (!w) return
      const msg = { type: EMBED_INIT, instanceId, chatId: chatId || undefined, picker: pickerOn }
      if (quickActions && quickActions.length > 0) msg.quickActions = quickActions
      w.postMessage(msg, window.location.origin)
    }

    function onMessage(e) {
      // §1.4 hardening: three same-origin frames share this origin, so
      // origin alone is insufficient — also require the message to come
      // from THIS embed's contentWindow and carry OUR instanceId.
      if (e.origin !== window.location.origin) return
      if (e.source !== iframe.contentWindow) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      if (typeof msg.type !== 'string' || !msg.type.startsWith(EMBED_NS)) return
      if (msg.instanceId && msg.instanceId !== instanceId) return
      if (msg.type === EMBED_READY) {
        // The embed resolved its chatId (e.g. it was opened without one
        // and INIT carried it, or a future lazy path). Adopt it, and
        // re-persist if it differs from what we saved.
        if (msg.chatId) {
          const resolved = String(msg.chatId)
          if (resolved !== chatId) { chatId = resolved; savePersistedId(chatId) }
        }
        emit('ready', { chatId })
      } else if (msg.type === EMBED_MESSAGE_SENT) {
        emit('message-sent', { chatId })
      } else if (msg.type === EMBED_TURN_DONE) {
        emit('turn-done', { chatId })
      } else if (msg.type === EMBED_ERROR) {
        emit('error', { chatId, error: msg.error })
      } else if (msg.type === EMBED_CONTEXT_REQUEST) {
        // The child is asking for current app state before submitting a message.
        // Call opts.getContext() if provided; reply even if absent (nonce
        // correlation lets the child match the response to its pending request).
        const nonce = msg.nonce
        const getContext = typeof opts.getContext === 'function' ? opts.getContext : null
        Promise.resolve(getContext ? getContext() : null).then((ctx) => {
          const w = iframe.contentWindow
          if (!w) return
          w.postMessage(
            { type: EMBED_CONTEXT_RESPONSE, instanceId, nonce, context: ctx || null },
            window.location.origin,
          )
        }).catch(() => {
          const w = iframe.contentWindow
          if (!w) return
          w.postMessage(
            { type: EMBED_CONTEXT_RESPONSE, instanceId, nonce, context: null },
            window.location.origin,
          )
        })
      }
    }

    // Register the message listener BEFORE appending the iframe, so it's
    // live before the embed can post its mount-time READY. INIT is sent
    // on the iframe's load event, which (per the HTML spec) fires after
    // the embed document's scripts have run and its own message listener
    // is registered — so the single INIT reaches it without a race, the
    // same handshake AppCanvas ↔ app-frame.html rely on.
    window.addEventListener('message', onMessage)
    iframe.addEventListener('load', sendInit)
    frameMount.appendChild(iframe)
    if (controlsShell) {
      mount.appendChild(controlsShell)
      refreshChatOptions().catch(() => {})
    }

    return {
      get chatId() { return chatId },
      instanceId,
      iframe,
      on(event, cb) {
        // Delegates to the sticky emitter: a 'ready'/'error' that already
        // fired (the mount-time READY) replays to a late handler.
        onEvent(event, cb)
        return this
      },
      destroy() {
        window.removeEventListener('message', onMessage)
        iframe.removeEventListener('load', sendInit)
        if (selectEl && onChatSelectChange) {
          selectEl.removeEventListener('change', onChatSelectChange)
        }
        if (newChatButton && onNewChatClick) {
          newChatButton.removeEventListener('click', onNewChatClick)
        }
        if (controlsShell && controlsShell.parentNode) {
          controlsShell.parentNode.removeChild(controlsShell)
        } else if (iframe.parentNode) {
          iframe.parentNode.removeChild(iframe)
        }
      },
    }
  }
}

// ── ChatSplit — window.mobius.split(opts) ────────────────────────────────────
//
// Manages a pill ↔ split ↔ full state machine for a mount element that holds
// both an app content area and the embedded chat panel. The helper owns all
// drag/touch/keyboard interaction and persists the ratio/state to sessionStorage
// so it survives tab navigation (not page refresh — sessionStorage is appropriate
// for UI transient state). CSS consumers read two custom properties the helper
// sets on `mount`:
//
//   --cs-content-h  (vertical / portrait mode)
//   --cs-content-w  (horizontal / wide mode)
//   data-split-state="pill|split|full"
//   data-orientation="portrait|side" (side when viewport ≥ 600px)
//
// Pure transition / threshold helpers are extracted into src/lib/splitHelper.js
// (testable under node:test) and mirrored as constants here.
//
// Keyboard-open behavior: the helper does NOT try to detect the keyboard. The
// browser compresses the visual viewport, which in turn shrinks `mount` — the
// CSS layout already adapts via the custom properties. No special handling needed.
//
// Wide viewports (≥ 600px): `data-orientation="side"` is applied; the helper
// reads `offsetWidth` rather than `offsetHeight` for drag calculations. The CSS
// consumer uses this attribute to switch between a column layout (portrait) and
// a row layout (side). Pill state is unavailable on wide viewports.

const SPLIT_WIDE_BP = 600
const SPLIT_FLICK_VEL = 0.4
const SPLIT_DEAD_ZONE = 24
const SPLIT_ARROW_STEP = 0.04

const SPLIT_STATES = { PILL: 'pill', SPLIT: 'split', FULL: 'full' }

function _splitClampRatio(ratio, totalPx, minContentPx, minChatPx) {
  if (totalPx <= 0) return ratio
  const lo = minContentPx / totalPx
  const hi = 1 - minChatPx / totalPx
  if (hi < lo) return 0.5
  return Math.min(hi, Math.max(lo, ratio))
}

function _splitResolveTransition(ratio, velocity, wide, totalPx, minContentPx, minChatPx) {
  if (velocity < -SPLIT_FLICK_VEL) return SPLIT_STATES.FULL
  if (velocity > SPLIT_FLICK_VEL) return wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
  const cr = _splitClampRatio(ratio, totalPx, minContentPx, minChatPx)
  if (cr <= minContentPx / totalPx + 0.01) return SPLIT_STATES.FULL
  if (cr >= 1 - minChatPx / totalPx - 0.01) return wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
  return SPLIT_STATES.SPLIT
}

export function makeSplit() {
  return function split(opts = {}) {
    const mount = opts.mount
    if (!mount || typeof mount.setAttribute !== 'function') {
      throw new Error('window.mobius.split: opts.mount must be a DOM element')
    }
    const defaultRatio = typeof opts.defaultRatio === 'number' ? opts.defaultRatio : 0.65
    const minContentPx = typeof opts.minContentPx === 'number' ? opts.minContentPx : 120
    const minChatPx = typeof opts.minChatPx === 'number' ? opts.minChatPx : 96
    const persistKey = typeof opts.persistKey === 'string' && opts.persistKey
      ? opts.persistKey : null

    // Restore from sessionStorage or use defaults.
    let ratio = defaultRatio
    let state = SPLIT_STATES.PILL
    if (persistKey) {
      try {
        const raw = JSON.parse(sessionStorage.getItem(persistKey) || 'null')
        if (raw && typeof raw.ratio === 'number' && Object.values(SPLIT_STATES).includes(raw.state)) {
          ratio = raw.ratio
          state = raw.state
        }
      } catch (e) {}
    }

    function persist() {
      if (!persistKey) return
      try { sessionStorage.setItem(persistKey, JSON.stringify({ ratio, state })) } catch (e) {}
    }

    function isWide() {
      return mount.offsetWidth >= SPLIT_WIDE_BP
    }

    function totalPx() {
      return isWide() ? mount.offsetWidth : mount.offsetHeight
    }

    function applyState() {
      const wide = isWide()
      const total = totalPx()
      mount.setAttribute('data-split-state', state)
      mount.setAttribute('data-orientation', wide ? 'side' : 'portrait')
      if (wide) {
        const w = state === SPLIT_STATES.FULL ? 0 : Math.round(ratio * total)
        mount.style.setProperty('--cs-content-w', `${w}px`)
        mount.style.removeProperty('--cs-content-h')
      } else {
        let h
        if (state === SPLIT_STATES.PILL) h = total
        else if (state === SPLIT_STATES.FULL) h = 0
        else h = Math.round(ratio * total)
        mount.style.setProperty('--cs-content-h', `${h}px`)
        mount.style.removeProperty('--cs-content-w')
      }
      // Disable content pane pointer events during chat-open states so
      // drag on the handle doesn't accidentally interact with app content.
      const contentEl = mount.querySelector('[data-split-role="content"]')
      if (contentEl) {
        contentEl.style.pointerEvents =
          state === SPLIT_STATES.FULL ? 'none' : 'auto'
      }
    }

    function setState(newState, newRatio) {
      if (Object.values(SPLIT_STATES).includes(newState)) state = newState
      if (typeof newRatio === 'number') {
        ratio = _splitClampRatio(newRatio, totalPx(), minContentPx, minChatPx)
      }
      applyState()
      persist()
    }

    // ── Drag handle element ───────────────────────────────────────────────
    const handle = document.createElement('div')
    handle.setAttribute('role', 'separator')
    handle.setAttribute('aria-label', 'Resize chat panel')
    handle.setAttribute('aria-valuenow', String(Math.round((1 - ratio) * 100)))
    handle.setAttribute('aria-valuemin', '0')
    handle.setAttribute('aria-valuemax', '100')
    handle.setAttribute('tabindex', '0')
    handle.setAttribute('data-split-role', 'handle')
    // 44px hit target with visible 4×40px bar inside.
    handle.style.cssText = [
      'position:absolute',
      'left:0', 'right:0',
      'height:44px',
      'display:flex',
      'align-items:center',
      'justify-content:center',
      'cursor:ns-resize',
      'touch-action:none',
      'z-index:10',
      'background:transparent',
    ].join(';')
    const bar = document.createElement('div')
    bar.style.cssText = 'width:40px;height:4px;border-radius:2px;background:var(--border,rgba(128,128,128,.5))'
    handle.appendChild(bar)

    // Update aria-valuenow (chat fraction %).
    function updateAria() {
      handle.setAttribute('aria-valuenow', String(Math.round((1 - ratio) * 100)))
    }

    // ── Keyboard resize ───────────────────────────────────────────────────
    function onKeyDown(e) {
      const wide = isWide()
      const total = totalPx()
      if (e.key === 'Home') {
        // Home → pill (content full) or split-max if wide.
        state = wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
        ratio = _splitClampRatio(defaultRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      } else if (e.key === 'End') {
        // End → full (chat full).
        setState(SPLIT_STATES.FULL)
        updateAria()
        e.preventDefault()
      } else if (e.key === 'ArrowUp') {
        // ArrowUp → grow content pane (shrink chat).
        const newRatio = Math.min(1, ratio + SPLIT_ARROW_STEP)
        const next = _splitResolveTransition(newRatio, SPLIT_FLICK_VEL + 0.1, wide, total, minContentPx, minChatPx)
        state = next === SPLIT_STATES.PILL || next === SPLIT_STATES.SPLIT ? next : SPLIT_STATES.SPLIT
        ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      } else if (e.key === 'ArrowDown') {
        // ArrowDown → shrink content pane (grow chat).
        const newRatio = Math.max(0, ratio - SPLIT_ARROW_STEP)
        const next = _splitResolveTransition(newRatio, -(SPLIT_FLICK_VEL + 0.1), wide, total, minContentPx, minChatPx)
        state = next
        ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      }
    }
    handle.addEventListener('keydown', onKeyDown)

    // ── Pointer/touch drag ────────────────────────────────────────────────
    let dragStartClient = null
    let dragStartRatio = null
    let dragLastClient = null
    let dragLastTime = null
    let dragVelocity = 0

    function clientAxisPos(e) {
      const wide = isWide()
      if (e.touches && e.touches.length > 0) {
        return wide ? e.touches[0].clientX : e.touches[0].clientY
      }
      return wide ? e.clientX : e.clientY
    }

    function onDragStart(e) {
      if (e.button != null && e.button !== 0) return
      dragStartClient = clientAxisPos(e)
      dragStartRatio = ratio
      dragLastClient = dragStartClient
      dragLastTime = Date.now()
      dragVelocity = 0
      // Disable text selection during drag.
      document.body.style.userSelect = 'none'
      document.body.style.webkitUserSelect = 'none'
      e.preventDefault()
    }

    function onDragMove(e) {
      if (dragStartClient === null) return
      const cur = clientAxisPos(e)
      const now = Date.now()
      const elapsed = now - dragLastTime || 1
      dragVelocity = (cur - dragLastClient) / elapsed
      dragLastClient = cur
      dragLastTime = now
      const delta = cur - dragStartClient
      // Dead zone: ignore the first SPLIT_DEAD_ZONE px of travel.
      if (Math.abs(delta) < SPLIT_DEAD_ZONE) return
      const total = totalPx()
      const newRatio = dragStartRatio + (delta - Math.sign(delta) * SPLIT_DEAD_ZONE) / total
      ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
      state = SPLIT_STATES.SPLIT
      applyState()
    }

    function onDragEnd(e) {
      if (dragStartClient === null) return
      document.body.style.userSelect = ''
      document.body.style.webkitUserSelect = ''
      const total = totalPx()
      const wide = isWide()
      const newState = _splitResolveTransition(
        ratio, dragVelocity, wide, total, minContentPx, minChatPx,
      )
      state = newState
      if (newState === SPLIT_STATES.SPLIT) {
        // Keep the dragged ratio; no snap needed.
      } else if (newState === SPLIT_STATES.PILL || (wide && newState === SPLIT_STATES.SPLIT)) {
        // Restore a sensible split ratio when snapping to pill/split-wide.
        ratio = _splitClampRatio(dragStartRatio, total, minContentPx, minChatPx)
      }
      applyState(); persist(); updateAria()
      dragStartClient = null
    }

    handle.addEventListener('pointerdown', onDragStart, { passive: false })
    handle.addEventListener('touchstart', onDragStart, { passive: false })
    window.addEventListener('pointermove', onDragMove, { passive: false })
    window.addEventListener('touchmove', onDragMove, { passive: false })
    window.addEventListener('pointerup', onDragEnd)
    window.addEventListener('pointercancel', onDragEnd)
    window.addEventListener('touchend', onDragEnd)

    // ── Viewport resize — reapply with current state ──────────────────────
    let _roCleanup = null
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => applyState())
      ro.observe(mount)
      _roCleanup = () => ro.disconnect()
    }

    mount.appendChild(handle)
    applyState()

    return {
      setState,
      destroy() {
        window.removeEventListener('pointermove', onDragMove)
        window.removeEventListener('touchmove', onDragMove)
        window.removeEventListener('pointerup', onDragEnd)
        window.removeEventListener('pointercancel', onDragEnd)
        window.removeEventListener('touchend', onDragEnd)
        handle.removeEventListener('keydown', onKeyDown)
        handle.removeEventListener('pointerdown', onDragStart)
        handle.removeEventListener('touchstart', onDragStart)
        if (handle.parentNode) handle.parentNode.removeChild(handle)
        if (_roCleanup) _roCleanup()
        mount.removeAttribute('data-split-state')
        mount.removeAttribute('data-orientation')
        mount.style.removeProperty('--cs-content-h')
        mount.style.removeProperty('--cs-content-w')
      },
    }
  }
}

export function makeNav() {
  const stack = []

  function open(label, onBack) {
    const entry = {
      owned: false,
      done: false,
      settled: false,
      readyResolve: null,
      onBack: typeof onBack === 'function' ? onBack : null,
    }
    const ready = new Promise((resolve) => {
      entry.readyResolve = resolve
    })
    const settleReady = (value) => {
      if (!entry.readyResolve) return
      entry.readyResolve(value)
      entry.readyResolve = null
    }
    const requestId = `nav-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const timer = setTimeout(() => {
      entry.settled = true
      settleReady(false)
      window.removeEventListener('message', onMessage)
    }, 5000)

    const close = (fromShell = false) => {
      if (entry.done) return
      entry.done = true
      const idx = stack.indexOf(entry)
      if (idx !== -1) stack.splice(idx, 1)
      if (!fromShell && entry.owned) {
        try {
          window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
        } catch (e) {}
      }
      entry.owned = false
      if (!entry.settled) settleReady(false)
      if (entry.settled || fromShell) {
        clearTimeout(timer)
        window.removeEventListener('message', onMessage)
      }
    }

    function onMessage(event) {
      if (event.origin !== window.location.origin) return
      if (event.source !== window.parent) return
      const msg = event.data
      if (msg?.type === 'moebius:nav-back') {
        if (stack[stack.length - 1] !== entry) return
        close(true)
        if (entry.onBack) entry.onBack()
        return
      }
      if (msg?.requestId !== requestId) return
      if (msg.type === 'moebius:nav-push-ack') {
        entry.settled = true
        clearTimeout(timer)
        if (entry.done) {
          try {
            window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
          } catch (e) {}
          settleReady(false)
          window.removeEventListener('message', onMessage)
          return
        }
        entry.owned = true
        stack.push(entry)
        settleReady(true)
      } else if (msg.type === 'moebius:nav-push-rejected') {
        entry.settled = true
        clearTimeout(timer)
        entry.owned = false
        settleReady(false)
        window.removeEventListener('message', onMessage)
      }
    }

    window.addEventListener('message', onMessage)
    if (window.parent === window) {
      clearTimeout(timer)
      entry.settled = true
      settleReady(false)
      window.removeEventListener('message', onMessage)
      return {
        ready,
        close() {
          close(false)
        },
      }
    }
    try {
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: label || 'app-detail', requestId },
        window.location.origin,
      )
    } catch (e) {
      clearTimeout(timer)
      entry.settled = true
      settleReady(false)
      window.removeEventListener('message', onMessage)
    }

    return {
      ready,
      close() {
        close(false)
      },
    }
  }

  return { open }
}

// ── P1-A: probed-online reactive backing ─────────────────────────────────────
// window.mobius.online returns this value (seeded from navigator.onLine).
// AppCanvas (the in-shell iframe host) posts `moebius:online-status` whenever
// the shell's probed reachability verdict changes; the message listener below
// updates _online and notifies subscribers. Standalone context (no AppCanvas)
// falls back to navigator.onLine via the seed — still a useful signal.
//
// Kept in a deliberately-delimited block so concurrent worktree merges stay
// clean — edits to this runtime should land near existing connectivity code.
// ─────────────────────────────────────────────────────────────────────────────
let _online = typeof navigator !== 'undefined' ? navigator.onLine : true
const _onlineListeners = new Set()

function _setOnline(next) {
  if (next === _online) return
  _online = next
  for (const cb of [..._onlineListeners]) {
    try { cb(next) } catch (e) {}
  }
}

// Listen for the probed verdict from AppCanvas. Ignored in standalone context
// (window.parent === window, no AppCanvas, navigator.onLine is the fallback).
if (typeof window !== 'undefined') {
  window.addEventListener('message', (e) => {
    if (e.origin !== window.location.origin) return
    const msg = e.data
    if (!msg || typeof msg !== 'object') return
    if (msg.type === 'moebius:online-status' && typeof msg.online === 'boolean') {
      _setOnline(msg.online)
    }
  })
  // Keep the seed roughly current while in the standalone host (no AppCanvas).
  // In the in-shell host AppCanvas drives _online; these are harmless extras.
  window.addEventListener('online', () => _setOnline(true))
  window.addEventListener('offline', () => _setOnline(false))
}
// ─────────────────────────────────────────────────────────────────────────────

export function init({ appId, getToken }) {
  const storage = makeStorage({ appId, getToken })
  window.mobius = {
    appId,
    // Returns the probed reachability verdict (not raw navigator.onLine).
    // In the in-shell iframe AppCanvas forwards the shell's /api/health probe
    // result; in the standalone PWA host it seeds from navigator.onLine.
    get online() { return _online },
    // Subscribe to online/offline changes. `cb(boolean)` fires immediately
    // with the current value and again whenever the value changes.
    // Returns an unsubscribe function (call it on component unmount).
    onOnlineChange(cb) {
      if (typeof cb !== 'function') return () => {}
      _onlineListeners.add(cb)
      try { cb(_online) } catch (e) {}
      return () => { _onlineListeners.delete(cb) }
    },
    storage,
    DurableWriteError,
    durableWrite: storage.durableWrite,
    onDeadLetter: storage.onDeadLetter,
    // useDocument is a React hook, so it must run on the APP's React instance.
    // The runtime is deliberately React-free (and headless-testable), and no
    // host sets window.React, so a self-binding window.mobius.useDocument would
    // throw. Expose the factory instead: apps bind it once at module top with
    // the React they already import — `const useDocument =
    // window.mobius.createUseDocument(React)`.
    createUseDocument: (React) => createUseDocument(storage, React),
    signal: makeSignal(appId, storage),
    chat: makeChat({ appId, getToken, storage }),
    nav: makeNav(),
    split: makeSplit(),
  }
  storage._drain()    // flush anything left from a previous offline session
  // Ask for durable storage so the offline mirror + queued blob writes survive
  // storage pressure. Fired here (not only in the shell's index.html) so a
  // standalone mini-app PWA opened WITHOUT the shell still gets it. Best-effort.
  try {
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persisted().then((p) => p || navigator.storage.persist()).catch(() => {})
    }
  } catch (e) {}
  return window.mobius
}

// # Smells / notes
// - RESOLVED (2026-06-01): get() now has an offline read path via the
//   read-through cache store (mirror-on-online-read, serve-offline, overlay
//   pending writes). The old "returns null offline" smell is gone.
// - The cache mirror is owner-scoped data; it lives in the shared
//   `mobius-outbox` IndexedDB (the `cache` store). client.js wipeSwCaches on
//   logout clears `mobius-*` CacheStorage but the OUTBOX/CACHE IndexedDB is a
//   separate DB — confirm logout also deletes it (delOutboxDb handles the
//   outbox DB; the cache store rides the same DB, so it's covered).
// - The standalone host passes a getToken that returns the boot-time
//   app token (or owner JWT fallback). On a long offline window the app
//   token can expire; the owner JWT fallback still authenticates as
//   owner, so the drain succeeds, but a future refinement could re-mint
//   the app token at drain time.
// - setBlob enforces a per-blob size cap (MAX_BLOB_BYTES) BEFORE any IDB/outbox
//   write, but the read-through cache has NO total-size eviction yet. A true LRU
//   needs a lastAccessed field + index the cache store lacks (cacheGet never
//   writes on read), and write-time eviction would drop hot entries — so the
//   eviction policy is deliberately deferred (filed under .pm/083). Fine at
//   personal-app scale; revisit if a blob-heavy app pressures the origin quota.
// - list() is offline-capable (078): when the server is unreachable it derives
//   direct children from the per-path read-through cache (present=false
//   tombstones excluded, so a synced delete does NOT resurrect — the hazard a
//   cached listing blob would have had), then overlays the outbox. Same
//   online/offline contract get() has. Offline entries omit size/modified_at,
//   which only the server stat provides.
