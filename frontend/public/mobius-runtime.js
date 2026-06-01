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
// API — intentionally small; grow it when a real app needs more:
//   window.mobius.appId
//   window.mobius.online                      -> navigator.onLine
//   window.mobius.storage.get(path)           -> data | null  (offline-capable)
//   window.mobius.storage.set(path, data)     -> {synced} | {queued}
//   window.mobius.storage.remove(path)        -> {synced} | {queued}
//   window.mobius.storage.list(prefix)        -> entries[] | null
//   window.mobius.storage.subscribe(path, cb) -> unsubscribe fn (cb(value))
//   window.mobius.storage.onSyncError(cb)     -> unsubscribe fn (cb({path,method,message}))
//   window.mobius.storage.pendingCount()      -> Promise<number>
//
// "No walls": this runtime is the easy DEFAULT, not a cage. An app is free to
// ignore it and use raw IndexedDB / OPFS / SQLite-wasm directly (same-origin
// iframe → all browser storage works), or talk to its own backend. The
// platform provides the on-ramp; it never gates the escape hatch.
//
// Conflict policy: last-write-wins at the path granularity. The newest
// write for a path supersedes any earlier one — enforced by coalescing
// the outbox on every write (a queued op for a path is dropped when a
// newer write for that path is enqueued OR sent directly online), so a
// stale queued op can never replay over a newer value on drain. An app
// that needs per-record LWW stores one file per record
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
const DB_VERSION = 2

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

function makeStorage({ appId, getToken }) {
  // Drop every queued op for this app + path in one transaction, then run
  // `after(store)` (if given) inside the SAME transaction. Used to enforce
  // last-write-wins at path granularity: a newer write for a path
  // supersedes any older queued write for it, so the stale op must not
  // survive to be replayed on drain. Filtering happens in the cursor
  // because the store is keyed by `seq` (FIFO), with `appId`/`path` as
  // plain fields. Doing the purge and the follow-up add in one tx keeps
  // the coalesce atomic — no window where the path has zero ops queued.
  function purgePath(path, after) {
    return withStore(STORE, 'readwrite', (store) => {
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) {
          if (after) after(store)
          return
        }
        const v = cursor.value
        if (v.appId === appId && v.path === path) cursor.delete()
        cursor.continue()
      }
    })
  }

  // Run `fn` holding the per-app outbox Web Lock — the SAME lock drain()
  // takes. Every outbox mutation (offline enqueue, online direct-send +
  // coalesce, and drain) runs under it, so across ALL contexts (in-shell
  // iframe + standalone tab for the same app) they are strictly serialized
  // and never interleave. This is what makes the post-send unbounded
  // purgePath correct: inside the lock no other context can enqueue, so
  // every queued op for the path is genuinely older than the write we just
  // sent and is safe to drop; a newer write can only enqueue AFTER we
  // release (later in the lock order), so it survives and wins — closing the
  // cross-context last-write-wins race (Codex review #7). Falls back to a
  // plain call where Web Locks is unavailable (same posture as drain()).
  function withOutboxLock(fn) {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request(`mobius-outbox-${appId}`, fn)
    }
    return fn()
  }

  // Enqueue coalesces: the newest write for a path replaces any older
  // queued writes for it, so a stale op can never clobber a newer one
  // when the queue drains. (FIFO ordering across DIFFERENT paths is
  // still preserved — drainInner walks `seq` in order.)
  function enqueue(op) {
    // Under the outbox lock so an enqueue can't interleave with a concurrent
    // context's direct-send+purge (which would otherwise drop this op).
    return withOutboxLock(() => purgePath(op.path, (store) => {
      store.add({ ...op, appId, ts: Date.now() })
    }))
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

  function cachePut(path, data) {
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      store.put({ key: cacheKey(path), path, appId, data, present: data !== null, ts: Date.now() })
    })
  }

  function cacheDelete(path) {
    // Record the deletion as a present:false tombstone rather than dropping the
    // key, so an offline read after an offline delete correctly returns null
    // (the deletion is the last-known state) instead of falling through.
    return withStore(CACHE_STORE, 'readwrite', (store) => {
      store.put({ key: cacheKey(path), path, appId, data: null, present: false, ts: Date.now() })
    })
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
  // or a dead-lettered write is reconciled to server truth. A SUCCESSFUL
  // background drain does NOT notify — it confirms the already-notified value
  // server-side without changing it. In-memory, per runtime instance — not
  // persisted (it's view wiring, not data).
  const subscribers = new Map()   // path -> Set<cb>

  function notify(path, data) {
    const set = subscribers.get(path)
    if (!set) return
    for (const cb of [...set]) {
      try { cb(data) } catch (e) { /* a listener throwing must not break others */ }
    }
  }

  // ── Sync-error listeners ──────────────────────────────────────────────
  // A write can be permanently rejected by the server (a "poison op": an
  // invalid path, a malformed body — any non-retryable 4xx). The outbox
  // dead-letters it so it can't block the queue forever, but the app needs to
  // KNOW its write was lost rather than silently believing it persisted.
  // onSyncError(cb) registers a listener; emitSyncError fires it with
  // {path, method, message}. In-memory, per runtime instance.
  const errorListeners = new Set()

  function emitSyncError(info) {
    for (const cb of [...errorListeners]) {
      try { cb(info) } catch (e) { /* one bad listener can't break the rest */ }
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
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(op.data)
    }
    const res = await fetch(url, init)   // network failure throws -> transient
    if (op.method === 'DELETE' && res.status === 404) return  // already absent
    if (res.ok) return
    // Classify so one bad op can't wedge the queue (drainInner reads
    // err.fatal): 401 auth / 408 timeout / 429 rate-limit / 5xx / network
    // are transient (keep + retry); any other 4xx is fatal (drop poison op).
    const err = new Error(`HTTP ${res.status}`)
    err.fatal = res.status >= 400 && res.status < 500 &&
      ![401, 408, 429].includes(res.status)
    throw err
  }

  async function drainInner() {
    if (!navigator.onLine) return
    const ops = await listOps()           // FIFO by seq
    for (const op of ops) {
      try {
        await send(op)
        await deleteOp(op.seq)
      } catch (e) {
        if (e && e.fatal) {
          // Poison op — a malformed/forbidden request that will never
          // succeed on replay. Drop it (dead-letter) and keep draining
          // so it can't head-of-line-block every later write forever.
          // eslint-disable-next-line no-console
          console.warn('mobius: dropping un-syncable write', op.method, op.path, e.message)
          await deleteOp(op.seq)
          // The optimistic cache still holds this rejected write's value, and
          // for an un-GETtable path (e.g. an invalid name that 400s on read
          // too) the reconciling get() would fall straight back to that very
          // value. INVALIDATE the mirror first so the reconcile reads server
          // truth (or genuine absence), never the rejected optimistic value
          // (Codex review #8). Then notify subscribers and surface a
          // sync-error so the app can tell the user the write was lost.
          try { await cacheDelete(op.path) } catch (ce) { /* best effort */ }
          let fresh = null
          try { fresh = await get(op.path) } catch (re) { /* best-effort */ }
          notify(op.path, fresh)
          emitSyncError({ path: op.path, method: op.method, message: e.message })
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
  function drain() {
    if (navigator.locks && navigator.locks.request) {
      navigator.locks.request(
        `mobius-outbox-${appId}`, { ifAvailable: true },
        async (lock) => { if (lock) await drainInner() },
      ).catch(() => {})
    } else {
      drainInner().catch(() => {})
    }
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
  function get(path) { return withPathLock(path, () => getInner(path)) }
  function set(path, data) { return withPathLock(path, () => setInner(path, data)) }
  function remove(path) { return withPathLock(path, () => removeInner(path)) }

  // Enumerate the immediate children of a stored directory (the platform
  // alternative to brute-force-probing filenames). Returns the entries ARRAY
  // (each {name, path, type, size, modified_at, mime_type?}), `[]` for an empty
  // or not-yet-created directory, and `null` on network failure — the same
  // online→data / offline→null contract get() exposes, so an app falls back to
  // its own snapshot on null but treats `[]` as a real (empty) result. No read
  // mirror: listings aren't a per-path value, and a stale cached listing would
  // resurrect deleted children; an app that wants offline listing keeps its own
  // snapshot keyed off the last successful call.
  async function listInner(prefix) {
    try {
      const token = await getToken()
      // Page through the whole listing so list() is true enumeration, not
      // just the server's first page (it caps a page at 500). Two guards keep
      // this bounded and HONEST rather than silently truncating (Codex review
      // #9): a page cap (MAX_LIST_PAGES × 500 entries) and a non-advancing
      // cursor check. Hitting either THROWS — which surfaces as null, the same
      // contract a network error uses — so the app falls back to its own
      // snapshot instead of acting on half a directory. Any HTTP error
      // likewise surfaces as null (offline/transient).
      const MAX_LIST_PAGES = 200   // ×500/page = 100k entries, far past single-owner scale
      const entries = []
      let cursor = null
      let prevCursor = null
      for (let page = 0; ; page++) {
        if (page >= MAX_LIST_PAGES) {
          throw new Error(`mobius: listing exceeded ${MAX_LIST_PAGES * 500} entries`)
        }
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
        // A server that returns the same cursor twice would loop forever; fail
        // fast rather than spin.
        if (cursor === prevCursor) {
          throw new Error('mobius: listing cursor did not advance')
        }
        prevCursor = cursor
      }
      return entries
    } catch (e) {
      return null
    }
  }

  async function getInner(path) {
    // Read-through: online, fetch and MIRROR into the cache so the value is
    // available offline later. Offline or on any error, serve the last-known
    // mirror, overlaid with any pending write (read-your-writes). Returns null
    // only when we have genuinely never cached the path (or it's known-absent).
    // Serialized per path, so the cache write below can't land after a later
    // set()'s write (that set() is queued behind this GET on the same chain).
    if (navigator.onLine) {
      try {
        const token = await getToken()
        const res = await fetchBounded(`/api/storage/apps/${appId}/${path}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (res.status === 404) {
          await cachePut(path, null)
        } else if (res.ok) {
          const data = await res.json()
          await cachePut(path, data)
        } else {
          throw new Error(`HTTP ${res.status}`)
        }
        const cached = await cacheGet(path)
        return await effectiveValue(path, cached ? cached.data : null)
      } catch (e) {
        // Network blip while "online" — fall back to the mirror below.
      }
    }
    const cached = await cacheGet(path)
    return await effectiveValue(path, cached ? cached.data : null)
  }

  async function setInner(path, data) {
    // Update the mirror + notify synchronously so the UI and a subsequent
    // get() are correct immediately, regardless of network outcome. Serialized
    // per path: a concurrent get() or another set() runs strictly before/after.
    await cachePut(path, data)
    notify(path, data)
    if (navigator.onLine) {
      try {
        // Hold the outbox lock across send + coalesce so a concurrent
        // context can't enqueue a newer write that this purge would drop, and
        // a drain can't replay a stale op mid-send (Codex review #7).
        return await withOutboxLock(async () => {
          await send({ method: 'PUT', path, data })
          await purgePath(path)
          return { synced: true }
        })
      } catch (e) { /* fall through to queue */ }
    }
    await enqueue({ method: 'PUT', path, data })
    drain()
    return { queued: true }
  }

  async function removeInner(path) {
    await cacheDelete(path)
    notify(path, null)
    if (navigator.onLine) {
      try {
        // Same lock-held send + coalesce as setInner (Codex review #7).
        return await withOutboxLock(async () => {
          await send({ method: 'DELETE', path })
          await purgePath(path)
          return { synced: true }
        })
      } catch (e) { /* fall through to queue */ }
    }
    await enqueue({ method: 'DELETE', path })
    drain()
    return { queued: true }
  }

  return {
    get,
    set,
    remove,
    list: listInner,
    // Subscribe to local changes for a path: cb(value) fires immediately with
    // the current value, then on every set/remove for that path. Returns an
    // unsubscribe fn. (A successful background drain does NOT re-fire — it
    // confirms the already-notified value server-side without changing it.)
    subscribe(path, cb) {
      let set = subscribers.get(path)
      if (!set) { set = new Set(); subscribers.set(path, set) }
      set.add(cb)
      // Fire the initial value once, but never let a slow initial get() resolve
      // AFTER a set() already pushed a newer value to this cb (which would
      // deliver stale data last). `delivered` flips the moment notify() reaches
      // this cb; the initial get() then suppresses itself. notify() wins ties.
      let delivered = false
      const wrapped = (v) => { delivered = true; cb(v) }
      set.delete(cb); set.add(wrapped)   // store the wrapper so notify flips the flag
      get(path).then((v) => {
        if (set.has(wrapped) && !delivered) { delivered = true; cb(v) }
      }).catch(() => {})
      return () => {
        const s = subscribers.get(path)
        if (s) { s.delete(wrapped); if (!s.size) subscribers.delete(path) }
      }
    },
    // Register a listener for permanently-failed writes (poison ops the outbox
    // had to dead-letter). Fires cb({path, method, message}) so the app can
    // tell the user a write was lost instead of silently assuming it synced.
    // Returns an unsubscribe fn.
    onSyncError(cb) {
      errorListeners.add(cb)
      return () => errorListeners.delete(cb)
    },
    async pendingCount() {
      return (await listOps()).length
    },
    _drain: drain,
    _notify: notify,
  }
}

export function init({ appId, getToken }) {
  const storage = makeStorage({ appId, getToken })
  window.mobius = {
    appId,
    get online() { return navigator.onLine },
    storage,
  }
  storage._drain()    // flush anything left from a previous offline session
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
// - The cache is unbounded in principle (one entry per app:path). For the
//   personal-app scale this is fine; if an app writes thousands of distinct
//   paths, add an LRU/size cap. Not built now (YAGNI).
// - list() has NO offline mirror (returns null offline), unlike get(). A
//   cached listing would resurrect deleted children once a sibling delete
//   synced, so an app that needs offline enumeration keeps its own snapshot of
//   the last successful list() and falls back to it on null. Revisit only if
//   offline directory enumeration becomes a common need.
