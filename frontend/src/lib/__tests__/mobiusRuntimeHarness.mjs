// Headless harness for the STATEFUL offline core of the mini-app runtime
// (frontend/public/mobius-runtime.js → makeStorage). The pure read-your-writes
// logic (overlayPending) is unit-tested in mobiusRuntime.test.js; this harness
// lets the IndexedDB-backed pieces — per-path serialization, the read-through
// cache, subscribe() fan-out, and the drain's poison-op dead-letter + reconcile
// — run under node:test with NO browser, by:
//
//   - installing fake-indexeddb as the global `indexedDB` (real IDB semantics,
//     in memory: transactions, cursors, structured-clone of Blobs),
//   - stubbing the handful of browser globals makeStorage touches at
//     construction (window/document addEventListener, navigator, crypto,
//     AbortController, setTimeout), and
//   - giving each test a CONTROLLED `fetch`: an in-memory server map plus a
//     toggleable online flag, so writes/reads/drains are deterministic and the
//     network is whatever the test says it is.
//
// `setOnline(false)` flips BOTH navigator.onLine (the runtime's connectivity
// gate) AND the mock fetch (which then throws, like a real offline fetch) so a
// test can't accidentally pass by reading a "200 while offline".

import 'fake-indexeddb/auto'
import { IDBFactory } from 'fake-indexeddb'

// makeStorage registers window/document listeners and reads navigator at
// construction; provide just enough that the factory builds and behaves like a
// foregrounded, online tab. No Web Locks API (navigator.locks is undefined) so
// the drain takes the in-tab _drainChain fallback — which is exactly the path
// we want to pin deterministically (single-runtime ordering).
function installGlobals() {
  if (!globalThis.window) {
    globalThis.window = {
      location: { origin: 'https://mobius.test' },
      addEventListener() {},
      removeEventListener() {},
    }
  }
  if (!globalThis.document) {
    globalThis.document = {
      visibilityState: 'visible',
      addEventListener() {},
      removeEventListener() {},
    }
  }
  if (!globalThis.crypto || !globalThis.crypto.randomUUID) {
    // Deterministic-enough unique nonce; the runtime only needs uniqueness, not
    // unpredictability, for the reconcile CAS.
    let n = 0
    globalThis.crypto = { randomUUID: () => `uuid-${++n}` }
  }
  // AbortController + Blob + setTimeout exist natively on node 18+.
}

// A mutable server backing store + connectivity flag, behind one fetch mock.
// Routes the runtime actually hits:
//   GET    /api/storage/apps/{appId}/{path}   → 200 value | 404
//   PUT    /api/storage/apps/{appId}/{path}   → 200 (store) | scripted status
//   DELETE /api/storage/apps/{appId}/{path}   → 200 (remove) | 404 | scripted
//   GET    /api/storage/apps-list/{appId}/... → list() — not exercised here
export function makeServer() {
  const files = new Map()           // path -> { value, kind, contentType }
  let online = true
  // path -> status to force on the NEXT matching write (poison/transient tests).
  const forcedWriteStatus = new Map()
  const log = []                    // every fetch the runtime made

  function res(status, body) {
    const ok = status >= 200 && status < 300
    return {
      ok,
      status,
      async json() { return body === undefined ? null : body },
      async text() { return body == null ? '' : String(body) },
      async blob() { return body instanceof Blob ? body : new Blob([]) },
    }
  }

  async function fetchImpl(url, init = {}) {
    const method = init.method || 'GET'
    log.push({ url, method })
    if (!online) {
      // A real offline fetch rejects; the runtime catches it as transient.
      throw new TypeError('Failed to fetch (offline)')
    }
    const m = url.match(/\/api\/storage\/apps\/[^/]+\/(.+?)(\?.*)?$/)
    const path = m ? decodeURIComponent(m[1]) : null

    if (method === 'PUT' && path != null) {
      const forced = forcedWriteStatus.get(path)
      if (forced !== undefined) {
        forcedWriteStatus.delete(path)
        return res(forced)
      }
      let value
      const ct = init.headers && init.headers['Content-Type']
      if (ct === 'application/json') value = JSON.parse(init.body)
      else value = init.body
      const kind = ct === 'application/json' ? 'json'
        : (ct && ct.startsWith('text/')) ? 'text' : 'blob'
      files.set(path, { value, kind, contentType: ct })
      return res(200)
    }
    if (method === 'DELETE' && path != null) {
      const forced = forcedWriteStatus.get(path)
      if (forced !== undefined) {
        forcedWriteStatus.delete(path)
        return res(forced)
      }
      const had = files.delete(path)
      return res(had ? 200 : 404)
    }
    // GET
    if (path != null) {
      if (!files.has(path)) return res(404)
      const rec = files.get(path)
      return res(200, rec.value)
    }
    return res(404)
  }

  return {
    fetch: fetchImpl,
    get online() { return online },
    setOnline(v) {
      online = v
      if (globalThis.navigator) globalThis.navigator.onLine = v
    },
    // Seed the server directly (simulates a value another client/agent wrote).
    seed(path, value, kind = 'json', contentType = 'application/json') {
      files.set(path, { value, kind, contentType })
    },
    serverValue(path) { return files.has(path) ? files.get(path).value : undefined },
    serverHas(path) { return files.has(path) },
    // Force the NEXT write to `path` to return `status` (e.g. 422 poison, 503
    // transient). Consumed once.
    forceWrite(path, status) { forcedWriteStatus.set(path, status) },
    log,
  }
}

// Fresh IndexedDB + a fresh server + online navigator for one test. Returns
// { server, setOnline } and installs the controlled fetch. Call at the top of
// each test so no state leaks between them (fake-indexeddb is a global singleton
// otherwise).
export function freshEnv() {
  installGlobals()
  // Wipe any DB from a prior test — fake-indexeddb's factory is global.
  globalThis.indexedDB = new IDBFactory()
  const server = makeServer()
  globalThis.navigator = { onLine: true }
  globalThis.fetch = server.fetch
  return { server }
}

// A small barrier: resolve after `ms` of the macrotask queue. The runtime's
// SWR revalidate + drain are scheduled, not awaited by the caller in some
// paths; tests use this to let those settle deterministically.
export function tick(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// Collect the values a subscriber sees, in order. Returns { values, unsub }.
export function recordSubscription(subscribeFn) {
  const values = []
  const unsub = subscribeFn((v) => values.push(v))
  return { values, unsub }
}
