/**
 * Bootstrap-seam integration test.
 *
 * The shell auto-creates a starter chat for an authenticated owner who
 * has no chats and no active chat. The seam lives in Shell.jsx's
 * bootstrap effect: once the live `/api/chats` fetch confirms zero
 * chats AND `activeChatId === null` AND the chat view is showing, it
 * calls `newChat()`, which POSTs `/api/chats` and sets
 * `moebius_active_chat` to the created chat's id.
 *
 * This test exercises that seam directly: mock the chats list empty,
 * capture the bootstrap POST, and assert a chat was created and made
 * active. Mocking (rather than hitting the shared mobius-test DB)
 * keeps the test deterministic regardless of concurrent Playwright
 * workers creating chats in the same SQLite file — the seam is a
 * client-side state machine, and the mock pins its inputs exactly.
 *
 * Three correctness requirements this test is careful about (each was
 * a flake or false-pass in the draft this replaces):
 *
 *  (a) AWAITED pre-navigation cleanup. Deleting the TanStack persister
 *      IndexedDB via `addInitScript` is fire-and-forget — React can
 *      mount and start hydrating the persisted cache before the delete
 *      lands, so a stale chats snapshot pre-populates the list and the
 *      bootstrap never fires. We delete the IDB on a blank page,
 *      `await` the deletion, THEN navigate to the app.
 *
 *  (b) CLEAR `moebius_active_chat` explicitly. The storageState
 *      (tests/.auth/state.json) can carry `moebius_active_chat` from a
 *      prior run. Asserting it's non-null afterward would then prove
 *      nothing — the value could be the leftover. We clear it in
 *      cleanup so a non-null value at the end can only mean bootstrap
 *      created the chat.
 *
 *  (c) The logout cache wipe covers BOTH `mobius-*` and `workbox-*`
 *      Cache Storage prefixes (api/client.js `wipeSwCaches`). A test
 *      asserting only `mobius-*` would miss a regression that dropped
 *      the workbox-precache purge. We seed both and assert both go.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/bootstrap.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

/** Route the shell's network surface so the bootstrap seam sees a
 *  deterministic "authenticated owner, zero chats" world:
 *   - GET /api/chats → [] (the empty-list precondition)
 *   - POST /api/chats → a stub chat (the bootstrap create); we record
 *     that it fired and with what title via `created`
 *   - /messages, /stream, /stop → benign stubs so a follow-on send
 *     (not exercised here) can't 500 and pollute the run
 *  Returns the `created` accumulator so the test can assert on it. */
async function routeShell(page) {
  await page.setViewportSize({ width: 412, height: 915 })

  const created = []
  await page.route(/\/api\/chats$/, async route => {
    const req = route.request()
    if (req.method() === 'GET') {
      // Reflect the bootstrap-created chats back in the list, exactly
      // as the real server would. Returning a hardcoded `[]` here would
      // make Shell's demote effect (which runs after the bootstrap
      // refetch) see the just-created chat as "not in the list",
      // demote activeChatId back to null, and unmount ChatView — so
      // the create would fire but never stick. The list must agree
      // with the POSTs.
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(created),
      })
    }
    if (req.method() === 'POST') {
      let body = {}
      try { body = JSON.parse(req.postData() || '{}') } catch { /* ignore */ }
      const chat = {
        id: `bootstrap-${created.length}-${Date.now()}`,
        title: body.title || 'New chat',
        has_messages: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        pending_messages: [],
      }
      created.push(chat)
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(chat),
      })
    }
    return route.fallback()
  })

  // A just-created chat's detail fetch (ChatView mounts on it and
  // fetches /chats/{id}?limit=20). Echo the requested id back and
  // return an empty message list — the chat is brand new.
  await page.route(/\/api\/chats\/[^/?]+(\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const m = route.request().url().match(/\/api\/chats\/([^/?]+)/)
    const id = m ? decodeURIComponent(m[1]) : 'bootstrap'
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id, title: 'New chat', messages: [],
        pending_messages: [], has_messages: false,
      }),
    })
  })

  await page.route(/\/api\/chats\/[^/]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  return created
}

/** Awaited pre-navigation cleanup (fix a + fix b).
 *
 *  Goes to a blank, same-origin page first so localStorage/IndexedDB
 *  access is scoped to the app origin, then:
 *   - deletes the TanStack persister IDB (`keyval-store`, holding
 *     `mobius-query-cache`) and the offline outbox IDB, AWAITING each
 *     delete's completion before returning — so the subsequent app
 *     mount can't hydrate a stale chats snapshot mid-deletion;
 *   - clears `moebius_active_chat` while KEEPING `token` (we still
 *     need to be authenticated).
 *
 *  Uses a real blank document on the app origin (`${BASE}/favicon.ico`
 *  404 is fine — we only need same-origin storage access) rather than
 *  `about:blank`, whose opaque origin has no access to the app's
 *  IndexedDB/localStorage. */
async function cleanSession(page) {
  // Serve a truly blank document for the cleanup URL so the SPA does
  // NOT mount here (the catch-all SPA route would otherwise return
  // index.html, boot the app, and fire bootstrap on the cleanup page).
  // The page stays on the app ORIGIN, so IndexedDB + localStorage are
  // the app's — `about:blank`'s opaque origin can't reach them.
  await page.route(/\/__blank-for-cleanup$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/html' },
      body: '<!doctype html><html><head></head><body></body></html>',
    })
  )
  await page.goto(`${BASE}/__blank-for-cleanup`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(async () => {
    const delDb = (name) => new Promise(resolve => {
      try {
        const req = indexedDB.deleteDatabase(name)
        req.onsuccess = req.onerror = req.onblocked = () => resolve()
      } catch { resolve() }
    })
    // Await the deletions — the whole point of fix (a).
    await delDb('keyval-store')
    await delDb('mobius-outbox')
    // fix (b): drop the active-chat pointer but keep the auth token.
    try { localStorage.removeItem('moebius_active_chat') } catch { /* ignore */ }
  })
}

async function waitForShell(page) {
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

test.describe('Bootstrap seam: empty-chat auto-create', () => {

  test('authenticated owner with no chats + no active chat bootstraps one', async ({ page }) => {
    const created = await routeShell(page)

    // fix (a) + (b): awaited IDB cleanup + cleared moebius_active_chat,
    // BEFORE the app mounts. After this, the only way the list can be
    // non-empty or activeChatId non-null is the bootstrap itself.
    await cleanSession(page)

    // Sanity: the precondition actually holds — no leftover active chat.
    expect(await page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBeNull()

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await waitForShell(page)

    // The bootstrap effect gates on the LIVE fetch
    // (isFetchedAfterMount), which can lag the first render — poll for
    // the POST rather than asserting synchronously.
    await expect.poll(() => created.length, { timeout: 8000 })
      .toBeGreaterThanOrEqual(1)

    // Exactly one starter chat — the in-flight guard + length===0 gate
    // must not double-fire. (A burst of bootstrap POSTs is the
    // "two empty chats on first boot" regression.)
    expect(created.length).toBe(1)
    expect(created[0].title).toBe('New chat')

    // fix (b) pays off here: moebius_active_chat is non-null ONLY
    // because bootstrap created the chat and setActiveChatId persisted
    // it — we cleared any leftover above, so this can't be stale.
    const active = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(active).toBe(created[0].id)
  })
})

test.describe('Logout cache wipe', () => {

  test('clears BOTH mobius-* and workbox-* Cache Storage entries (fix c)', async ({ page }) => {
    // The real wipe runs inside api/client.js's 401 handler
    // (clearQueryCache → wipeSwCaches), which is the only production
    // path that fires it (single-owner app, no logout button). We
    // drive that genuine path: seed both cache prefixes, force a 401
    // on an authenticated API call, and assert both prefixes are gone.
    await page.setViewportSize({ width: 412, height: 915 })

    // Let the shell mount normally first (real chats endpoint is fine —
    // we only need a same-origin authenticated page to run client code).
    await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await waitForShell(page)

    // Seed Cache Storage with a representative entry under each prefix
    // the wipe must cover. `mobius-vendor` mirrors src/sw.js runtime
    // caches; `workbox-precache-v2-...` mirrors vite-plugin-pwa's
    // precache. A control cache under an unrelated prefix proves the
    // wipe is scoped, not a blanket caches.delete-everything.
    await page.evaluate(async () => {
      await caches.open('mobius-vendor')
      await caches.open('workbox-precache-v2-https://example/')
      await caches.open('unrelated-cache-keep-me')
    })

    const before = await page.evaluate(() => caches.keys())
    expect(before).toContain('mobius-vendor')
    expect(before).toContain('workbox-precache-v2-https://example/')
    expect(before).toContain('unrelated-cache-keep-me')

    // Force the next authenticated /api/chats call to 401. apiFetch's
    // 401 branch runs clearToken() + (awaited) clearQueryCache() →
    // wipeSwCaches, then defers a window.location.reload() 100ms later.
    // This is the genuine production path (single-owner app, the only
    // way the wipe ever fires). We DON'T stub the reload — Cache
    // Storage is origin-scoped and persists ACROSS the reload, so the
    // wipe's effect is observable on the reloaded (now logged-out)
    // page. Stubbing window.location.reload is unreliable in Chromium
    // anyway (the property resists reassignment).
    await page.route(/\/api\/chats\/?$/, route =>
      route.fulfill({ status: 401, body: '{"detail":"Not signed in"}' })
    )

    // Trigger a real apiFetch through the live client: opening the
    // drawer runs Shell's `if (drawerOpen) refreshChats()` effect,
    // which calls api.chats.list() → apiFetch('/chats') → 401 → wipe.
    // (No internal query-client handle needed.) The 401 clears the
    // token, so the post-reload page lands on the login form.
    await page.evaluate(() => {
      const btn = document.querySelector('[aria-expanded]')
      if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
    })

    // Wait through the apiFetch-deferred reload. The token was cleared,
    // so the reloaded page shows the login form — wait for it so we read
    // caches on a stable, post-wipe page rather than racing the reload.
    await expect(page.locator('.login')).toBeVisible({ timeout: 10000 })

    // Both targeted prefixes are gone after the wipe; the unrelated
    // cache survives — the wipe is prefix-scoped, not a blanket purge.
    const afterKeys = await page.evaluate(() => caches.keys())
    expect(afterKeys).not.toContain('mobius-vendor')
    expect(afterKeys).not.toContain('workbox-precache-v2-https://example/')
    expect(afterKeys).toContain('unrelated-cache-keep-me')
  })
})
