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
  const files = new Map()           // path -> { value, kind, contentType, etag }
  const signalEvents = []
  let signalStatus = 204
  let online = true
  // path -> status to force on the NEXT matching write (poison/transient tests).
  const forcedWriteStatus = new Map()
  const log = []                    // every fetch the runtime made
  // Monotonic version token per successful write, quoted like a real strong
  // ETag. The CAS path (getWithVersion + If-Match) round-trips this exactly as
  // the backend's file_version_token does.
  let etagSeq = 0
  const nextEtag = () => `"${++etagSeq}"`
  // Case-insensitive read of a header the runtime set on `init.headers`.
  const reqHeader = (init, name) => {
    const h = (init && init.headers) || {}
    const target = name.toLowerCase()
    for (const k of Object.keys(h)) if (k.toLowerCase() === target) return h[k]
    return undefined
  }

  function res(status, body, headers = {}) {
    const ok = status >= 200 && status < 300
    // Case-insensitive header lookup, mirroring a real fetch Response's Headers
    // (the runtime reads res.headers.get('ETag') || .get('etag')).
    const lower = {}
    for (const k of Object.keys(headers)) lower[k.toLowerCase()] = headers[k]
    return {
      ok,
      status,
      headers: { get(name) { const v = lower[String(name).toLowerCase()]; return v == null ? null : v } },
      async json() { return body === undefined ? null : body },
      async text() { return body == null ? '' : String(body) },
      async blob() { return body instanceof Blob ? body : new Blob([]) },
    }
  }

  async function fetchImpl(url, init = {}) {
    const method = init.method || 'GET'
    log.push({ url, method, headers: (init && init.headers) || {}, body: init.body })
    if (!online) {
      // A real offline fetch rejects; the runtime catches it as transient.
      throw new TypeError('Failed to fetch (offline)')
    }
    if (method === 'POST' && url === '/api/client-signal') {
      if (signalStatus !== 204) return res(signalStatus)
      const body = JSON.parse(init.body)
      signalEvents.push(...(body.signals || []))
      return res(204)
    }
    const m = url.match(/\/api\/storage\/apps\/[^/]+\/(.+?)(\?.*)?$/)
    const path = m ? decodeURIComponent(m[1]) : null

    if (method === 'PUT' && path != null) {
      const forced = forcedWriteStatus.get(path)
      if (forced !== undefined) {
        forcedWriteStatus.delete(path)
        return res(forced)
      }
      // Conditional-write preconditions, mirroring backend storage.py: an
      // If-None-Match:* fails when the file exists (create-only), an If-Match
      // fails when the file is absent or its version differs. A request that
      // carried EITHER is a CAS write, so the 200 echoes the new ETag.
      const ifMatch = reqHeader(init, 'if-match')
      const ifNoneMatch = reqHeader(init, 'if-none-match')
      const existing = files.get(path)
      if (ifNoneMatch === '*' && existing) return res(412)
      if (ifMatch !== undefined && (!existing || existing.etag !== ifMatch)) return res(412)
      let value
      const ct = init.headers && init.headers['Content-Type']
      if (ct === 'application/json') value = JSON.parse(init.body)
      else value = init.body
      const kind = ct === 'application/json' ? 'json'
        : (ct && ct.startsWith('text/')) ? 'text' : 'blob'
      const etag = nextEtag()
      files.set(path, { value, kind, contentType: ct, etag })
      const wantsCas = ifMatch !== undefined || ifNoneMatch !== undefined
      return res(200, undefined, wantsCas ? { ETag: etag } : {})
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
    // GET — echo the current ETag only when the caller opted in with the
    // X-Mobius-Version:1 header, exactly like the backend read route.
    if (path != null) {
      if (!files.has(path)) return res(404)
      const rec = files.get(path)
      const headers = reqHeader(init, 'x-mobius-version') === '1' ? { ETag: rec.etag } : {}
      return res(200, rec.value, headers)
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
    // Each seed bumps the version, so re-seeding an existing path models a
    // concurrent writer moving the ETag out from under a held version.
    seed(path, value, kind = 'json', contentType = 'application/json') {
      files.set(path, { value, kind, contentType, etag: nextEtag() })
    },
    serverValue(path) { return files.has(path) ? files.get(path).value : undefined },
    serverHas(path) { return files.has(path) },
    signalEvents,
    setSignalStatus(status) { signalStatus = status },
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
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    writable: true,
    value: { onLine: true },
  })
  globalThis.fetch = server.fetch
  return { server }
}

// A small barrier: resolve after `ms` of the macrotask queue. The runtime's
// SWR revalidate + drain are scheduled, not awaited by the caller in some
// paths; tests use this to let those settle deterministically.
export function tick(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// Wait until `pred()` is truthy, polling every `step` ms up to `timeout` ms.
// A fixed `tick(N)` barrier is timing-flaky: it passes only if the scheduled
// work happens to land inside N ms, which a loaded CI box can blow past, so the
// test fails for reasons unrelated to the code under test (card 079's whole
// point was a DETERMINISTIC core). Polling the actual end-state condition makes
// the wait as long as it needs to be and no longer — it resolves the instant
// the condition holds, and only the (generous) timeout is a wall-clock value,
// hit only on a genuine hang. Rejects on timeout so a real regression still
// fails loudly rather than hanging the suite.
export async function waitFor(pred, { timeout = 1000, step = 1 } = {}) {
  const deadline = Date.now() + timeout
  for (;;) {
    // `await` so an async predicate (e.g. one that reads pendingCount()) is
    // resolved before the truthiness check — a bare Promise is always truthy.
    if (await pred()) return
    if (Date.now() >= deadline) {
      throw new Error(`waitFor: condition not met within ${timeout}ms`)
    }
    await tick(step)
  }
}

// Collect the values a subscriber sees, in order. Returns { values, unsub }.
export function recordSubscription(subscribeFn) {
  const values = []
  const unsub = subscribeFn((v) => values.push(v))
  return { values, unsub }
}
