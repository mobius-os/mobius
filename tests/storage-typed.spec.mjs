// Tier 4c — window.mobius.storage typed API (083) + always-enqueue write path
// (081). Drives the REAL runtime served by a mobius-test container, with
// /api/storage mocked by an in-memory store via page.route so we test the
// RUNTIME logic (typed serialize/parse, IDB mirror, offline queue+drain,
// read-your-writes, the fatal-write deadlock regression) without owner auth.
//
// Run against a container serving the NEW mobius-runtime.js:
//   MOBIUS_URL=http://localhost:8030 npx playwright test tests/storage-typed.spec.mjs
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8030'

function stubAppToken(appId) {
  const claims = Buffer.from(JSON.stringify({
    scope: 'app',
    app_id: String(appId),
  })).toString('base64url')
  return `stub.${claims}.stub`
}

// Install an in-memory /api/storage backend: PUT stores {body,ct} by path; GET
// returns it (404 if absent); DELETE removes. `mode` lets a test force offline
// (abort) or a fatal 422. Returns handles to flip mode + inspect.
async function installStore(page, opts = {}) {
  const state = { mode: 'up', puts: 0 }
  await page.route('**/api/storage/apps/**', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const key = url.pathname
    if (state.mode === 'down') return route.abort()
    if (state.mode === 'fatal') return route.fulfill({ status: 422, body: 'nope' })
    const m = req.method()
    if (m === 'PUT') {
      state.puts += 1
      const ct = (req.headers()['content-type'] || '').split(';')[0]
      globalThis.__store = globalThis.__store || {}
      globalThis.__store[key] = { body: req.postDataBuffer(), ct }
      return route.fulfill({ status: 204, body: '' })
    }
    if (m === 'DELETE') { if (globalThis.__store) delete globalThis.__store[key]; return route.fulfill({ status: 204, body: '' }) }
    // GET
    const rec = globalThis.__store && globalThis.__store[key]
    if (!rec) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, body: rec.body, headers: { 'content-type': rec.ct || 'application/octet-stream' } })
  })
  // The list endpoint: derive the immediate children under the prefix from the
  // in-memory store (so online list() has a real server source). `down` aborts
  // (offline) so the runtime falls back to its cache+outbox overlay.
  await page.route('**/api/storage/apps-list/**', async (route) => {
    if (state.mode === 'down') return route.abort()
    if (state.mode === 'fatal') return route.fulfill({ status: 422, body: 'nope' })
    const url = new URL(route.request().url())
    const mm = url.pathname.match(/\/api\/storage\/apps-list\/(\d+)\/(.*)$/)
    const appId = mm ? mm[1] : ''
    const prefix = mm ? mm[2] : ''
    const base = prefix ? prefix.replace(/\/+$/, '') + '/' : ''
    const appPrefix = `/api/storage/apps/${appId}/`
    const seenDir = new Set()
    const entries = []
    for (const k of Object.keys(globalThis.__store || {})) {
      if (!k.startsWith(appPrefix)) continue
      const rel = k.slice(appPrefix.length)
      if (base && !rel.startsWith(base)) continue
      const rest = base ? rel.slice(base.length) : rel
      const slash = rest.indexOf('/')
      if (slash === -1) {
        entries.push({ name: rest, path: base + rest, type: 'file', size: 1, modified_at: '2026-01-01T00:00:00Z', mime_type: 'application/json' })
      } else {
        const d = rest.slice(0, slash)
        if (!seenDir.has(d)) { seenDir.add(d); entries.push({ name: d, path: base + d, type: 'directory', size: 0, modified_at: '2026-01-01T00:00:00Z' }) }
      }
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ entries, next_cursor: null }) })
  })
  return state
}

async function initRuntime(page, appId) {
  await page.goto(`${BASE}/shell/`)
  const token = stubAppToken(appId)
  await page.evaluate(async ({ appId, token }) => {
    await new Promise((res) => { const r = indexedDB.deleteDatabase('mobius-outbox'); r.onsuccess = r.onerror = r.onblocked = () => res() })
    const rt = await import('/mobius-runtime.js')
    rt.init({ appId, getToken: async () => token })
  }, { appId, token })
}

test('json round-trip + back-compat get/set', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9101)
  const out = await page.evaluate(async () => {
    const s = window.mobius.storage
    const w = await s.set('t.json', { a: 1, nested: [1, 2] })
    const r = await s.get('t.json')
    return { w, r }
  })
  expect(out.w).toEqual({ synced: true })
  expect(out.r).toEqual({ a: 1, nested: [1, 2] })
})

test('text round-trip (the latex .tex/.md fix)', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9102)
  const out = await page.evaluate(async () => {
    const s = window.mobius.storage
    const tex = '\\documentclass{article}\n$$E=mc^2$$'
    await s.setText('doc.tex', tex)
    return { back: await s.getText('doc.tex'), typeofBack: typeof (await s.getText('doc.tex')) }
  })
  expect(out.typeofBack).toBe('string')
  expect(out.back).toBe('\\documentclass{article}\n$$E=mc^2$$')
})

test('blob round-trip preserves bytes + contentType', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9103)
  const out = await page.evaluate(async () => {
    const s = window.mobius.storage
    const bytes = new Uint8Array([137, 80, 78, 71, 1, 2, 3, 4])  // PNG-ish
    const blob = new Blob([bytes], { type: 'image/png' })
    await s.setBlob('pic.png', blob)
    const back = await s.getBlob('pic.png')
    const buf = new Uint8Array(await back.arrayBuffer())
    return { isBlob: back instanceof Blob, type: back.type, bytes: Array.from(buf) }
  })
  expect(out.isBlob).toBe(true)
  expect(out.type).toBe('image/png')
  expect(out.bytes).toEqual([137, 80, 78, 71, 1, 2, 3, 4])
})

test('typed-mismatch read throws (no string-as-Blob corruption)', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9104)
  const threw = await page.evaluate(async () => {
    const s = window.mobius.storage
    await s.setText('x.md', 'hi')
    try { await s.getBlob('x.md'); return false } catch (e) { return /does not hold a blob|holds text/.test(e.message) }
  })
  expect(threw).toBe(true)
})

test('setText rejects a non-string', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9105)
  const rejected = await page.evaluate(async () => {
    try { await window.mobius.storage.setText('y.md', { obj: 1 }); return false } catch (e) { return /must be a string/.test(e.message) }
  })
  expect(rejected).toBe(true)
})

test('offline queue + read-your-writes + drain on recovery (always-enqueue)', async ({ page }) => {
  const store = await installStore(page)
  await initRuntime(page, 9106)
  store.mode = 'down'
  const offline = await page.evaluate(async () => {
    const s = window.mobius.storage
    const w = await s.set('q.json', { n: 1 })
    const ryw = await s.get('q.json')           // read-your-writes while offline
    return { w, ryw, pending: await s.pendingCount() }
  })
  expect(offline.w).toEqual({ queued: true })
  expect(offline.ryw).toEqual({ n: 1 })
  expect(offline.pending).toBe(1)
  store.mode = 'up'
  await page.evaluate(() => window.mobius.storage._drain())
  await expect.poll(() => page.evaluate(() => window.mobius.storage.pendingCount()), { timeout: 8000 }).toBe(0)
})

test('FATAL write resolves (no deadlock) + leaves the path lock acquirable', async ({ page }) => {
  const store = await installStore(page)
  await initRuntime(page, 9107)
  store.mode = 'fatal'   // server 422 on every write → fatal dead-letter path
  const result = await page.evaluate(async () => {
    const s = window.mobius.storage
    // This must RESOLVE, not hang (the deadlock regression).
    const a = await Promise.race([
      s.set('bad.json', { x: 1 }).then(() => 'resolved'),
      new Promise((r) => setTimeout(() => r('HANG'), 6000)),
    ])
    // The outbox lock + path lock must be free for a subsequent write.
    store_mode_up_marker: void 0
    return { a }
  })
  expect(result.a).toBe('resolved')
  // A subsequent write to the SAME path must also complete (lock not stuck).
  store.mode = 'up'
  const after = await page.evaluate(async () => {
    return await Promise.race([
      window.mobius.storage.set('bad.json', { x: 2 }).then((r) => r),
      new Promise((res) => setTimeout(() => res('HANG'), 6000)),
    ])
  })
  expect(after).toEqual({ synced: true })
})

// ── 078: offline-capable list() (cache + outbox overlay) ──────────────────

test('list() online returns the server listing + entry shape', async ({ page }) => {
  await installStore(page)
  await initRuntime(page, 9108)
  const out = await page.evaluate(async () => {
    const s = window.mobius.storage
    await s.set('items/a.json', { n: 1 })
    await s.set('items/b.json', { n: 2 })
    const entries = await s.list('items/')
    return { entries }
  })
  expect(out.entries.map((e) => e.name).sort()).toEqual(['a.json', 'b.json'])
  expect(out.entries[0]).toMatchObject({ type: 'file', path: expect.stringContaining('items/') })
})

test('list() enumerates from the cache after going offline (the Notes reload case)', async ({ page }) => {
  const store = await installStore(page)
  await initRuntime(page, 9109)
  const out = await page.evaluate(async () => {
    const s = window.mobius.storage
    await s.set('items/x.json', { v: 1 })   // online → synced + mirrored to cache
    await s.set('items/y.json', { v: 2 })
    return { online: (await s.list('items/')).map((e) => e.name).sort() }
  })
  store.mode = 'down'  // offline — apps-list now aborts; list() falls to cache
  const offline = await page.evaluate(async () => {
    return (await window.mobius.storage.list('items/')).map((e) => e.name).sort()
  })
  expect(out.online).toEqual(['x.json', 'y.json'])
  expect(offline).toEqual(['x.json', 'y.json'])  // enumerable offline, no index file
})

test('list() offline reflects pending creates + deletes (read-your-writes)', async ({ page }) => {
  const store = await installStore(page)
  await initRuntime(page, 9110)
  await page.evaluate(async () => {
    const s = window.mobius.storage
    await s.set('items/keep.json', { v: 1 })
    await s.set('items/gone.json', { v: 2 })
  })
  store.mode = 'down'  // go offline; the next writes queue + mirror to cache
  const offline = await page.evaluate(async () => {
    const s = window.mobius.storage
    await s.set('items/fresh.json', { v: 3 })   // pending create
    await s.remove('items/gone.json')           // pending delete
    return (await s.list('items/')).map((e) => e.name).sort()
  })
  // fresh.json appears (pending PUT), gone.json is dropped (pending DELETE),
  // keep.json stays — and a tombstoned delete never resurrects.
  expect(offline).toEqual(['fresh.json', 'keep.json'])
})
