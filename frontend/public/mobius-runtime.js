// Shared mini-app runtime — exposes `window.mobius` to apps running in
// both the in-shell iframe (app-frame.html) and the standalone shell
// (routes/standalone.py). Imported at an absolute path
// (`/mobius-runtime.js`) so it resolves identically from
// `/api/apps/{id}/frame` and `/apps/{slug}/`. It lives in `public/`,
// so Vite copies it to the build root and Workbox precaches it
// (content-revisioned per deploy → fresh online, available offline).
//
// Purpose: let offline-capable apps (offline_capable flag, Tier 3)
// persist through the network outage. Writes go to /api/storage; when
// offline or the request fails, they queue in IndexedDB and flush when
// the connection returns. Reads hit the network (the service worker
// serves cached app code, not storage data).
//
// API — intentionally small; grow it when a real app needs more:
//   window.mobius.appId
//   window.mobius.online                      -> navigator.onLine
//   window.mobius.storage.get(path)           -> data | null
//   window.mobius.storage.set(path, data)     -> {synced} | {queued}
//   window.mobius.storage.remove(path)        -> {synced} | {queued}
//   window.mobius.storage.pendingCount()      -> Promise<number>
//
// Conflict policy: last-write-wins at the path granularity. An app that
// needs per-record LWW stores one file per record (…/items/<uuid>.json)
// so concurrent edits to different records don't clobber each other.
// CRDTs are out of scope (overkill for single-owner personal apps).
//
// Smells: see the block at the bottom of this file.

const DB_NAME = 'mobius-outbox'
const STORE = 'ops'

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        // autoIncrement `seq` gives FIFO ordering for free. `appId` is a
        // stored field, filtered at read time (one shared DB, many apps).
        db.createObjectStore(STORE, { keyPath: 'seq', autoIncrement: true })
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

// Run `fn(store)` in one transaction. `fn` may stash a result on the
// returned object's `value`; we resolve with it on commit. Doing all
// IDB work inside the single synchronous `fn` call avoids the
// auto-close that bites when you await between operations on one tx.
async function withStore(mode, fn) {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode)
    const box = {}
    fn(tx.objectStore(STORE), box)
    tx.oncomplete = () => resolve(box.value)
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })
}

function makeStorage({ appId, getToken }) {
  function enqueue(op) {
    return withStore('readwrite', (store) => {
      store.add({ ...op, appId, ts: Date.now() })
    })
  }

  function listOps() {
    return withStore('readonly', (store, box) => {
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
    return withStore('readwrite', (store) => { store.delete(seq) })
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

  return {
    async get(path) {
      // Reads hit the network. Offline (or on any error) returns null
      // and the app shows its own empty/offline state — we deliberately
      // do not keep a local read-mirror (a second source of truth that
      // could silently diverge from the server and the outbox).
      try {
        const token = await getToken()
        const res = await fetch(`/api/storage/apps/${appId}/${path}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (res.status === 404) return null
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return await res.json()
      } catch (e) {
        return null
      }
    },
    async set(path, data) {
      if (navigator.onLine) {
        try { await send({ method: 'PUT', path, data }); return { synced: true } }
        catch (e) { /* fall through to queue */ }
      }
      await enqueue({ method: 'PUT', path, data })
      drain()
      return { queued: true }
    },
    async remove(path) {
      if (navigator.onLine) {
        try { await send({ method: 'DELETE', path }); return { synced: true } }
        catch (e) { /* fall through to queue */ }
      }
      await enqueue({ method: 'DELETE', path })
      drain()
      return { queued: true }
    },
    async pendingCount() {
      return (await listOps()).length
    },
    _drain: drain,
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

// # Smells
// - get() has no offline read path (returns null offline). Apps that
//   need to render persisted data offline must keep their own copy in
//   app state; a shell-managed read cache was cut as YAGNI until an app
//   needs it. Revisit if offline read of last-known data becomes common.
// - The standalone host passes a getToken that returns the boot-time
//   app token (or owner JWT fallback). On a long offline window the app
//   token can expire; the owner JWT fallback still authenticates as
//   owner, so the drain succeeds, but a future refinement could re-mint
//   the app token at drain time.
