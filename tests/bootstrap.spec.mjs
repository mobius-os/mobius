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

// These tests use page.route() to pin the shell's API responses. Once the
// production service worker claims the page, its fetch handler bypasses those
// routes and makes the mocked 401 race-dependent. Cache Storage itself remains
// available with workers blocked, so the logout test still exercises the real
// apiFetch 401 -> clearQueryCache -> reload flow and observes its cache wipe.
test.use({ serviceWorkers: 'block' })

/** Route the shell's network surface so the bootstrap seam sees a
 *  deterministic "authenticated owner, zero chats" world:
 *   - GET /api/chats → [] (the empty-list precondition)
 *   - POST /api/chats → a stub chat (the bootstrap create); we record
 *     that it fired and with what title via `created`
 *   - /messages, /stream, /stop → benign stubs so a follow-on send
 *     (not exercised here) can't 500 and pollute the run
 *  Returns the `created` accumulator so the test can assert on it. */
async function routeShell(page, {
  createStatus = 200,
  gatePostCreateList = false,
  gateDetail = false,
} = {}) {
  await page.setViewportSize({ width: 412, height: 915 })

  const created = []
  created.attempts = 0
  let releasePostCreateList = () => {}
  const postCreateListGate = gatePostCreateList
    ? new Promise(resolve => { releasePostCreateList = resolve })
    : null
  created.releasePostCreateList = releasePostCreateList
  let releaseDetail = () => {}
  const detailGate = gateDetail
    ? new Promise(resolve => { releaseDetail = resolve })
    : null
  created.releaseDetail = releaseDetail
  await page.route(/\/api\/chats$/, async route => {
    const req = route.request()
    if (req.method() === 'GET') {
      if (created.length > 0 && postCreateListGate) await postCreateListGate
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
        body: JSON.stringify(created.map(({ messages, detail, ...row }) => row)),
      })
    }
    if (req.method() === 'POST') {
      created.attempts += 1
      if (createStatus !== 200) {
        return route.fulfill({
          status: createStatus,
          headers: { 'Content-Type': 'application/json' },
          body: '{"detail":"create unavailable"}',
        })
      }
      let body = {}
      try { body = JSON.parse(req.postData() || '{}') } catch { /* ignore */ }
      const id = `bootstrap-${created.length}-${Date.now()}`
      const title = body.title || 'New chat'
      const detail = {
        id,
        title,
        messages: [],
        pending_messages: [],
        total: 0,
        offset: 0,
        running: false,
        pending_question_id: null,
        session_id: null,
        provider: 'claude',
        created_by_app_id: null,
        auto_resume_on_limit: true,
        agent_settings_json: null,
        effective_agent_settings: { model: 'claude-current', effort: 'medium' },
        has_assistant_turns: false,
      }
      const chat = {
        id,
        title,
        has_messages: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        activity_at: null,
        pinned_at: null,
        created_by_app_id: null,
        run_status: null,
        running: false,
        messages: [],
        detail,
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
  await page.route(/\/api\/chats\/[^/?]+(\?.*)?$/, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    if (detailGate) await detailGate
    const m = route.request().url().match(/\/api\/chats\/([^/?]+)/)
    const id = m ? decodeURIComponent(m[1]) : 'bootstrap'
    const detail = created.find(chat => chat.id === id)?.detail
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(detail || {
        id, title: 'New chat', messages: [],
        pending_messages: [], total: 0, offset: 0, running: false,
        pending_question_id: null, session_id: null, provider: 'claude',
        effective_agent_settings: {}, has_assistant_turns: false,
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

  test('paints the created chat before background detail and list refreshes resolve', async ({ page }) => {
    const created = await routeShell(page, {
      gatePostCreateList: true,
      gateDetail: true,
    })
    await cleanSession(page)

    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' })
      await expect.poll(() => created.length, { timeout: 8000 }).toBe(1)
      await expect.poll(
        () => page.evaluate(() => localStorage.getItem('moebius_active_chat')),
        { timeout: 2000 },
      ).toBe(created[0].id)
      await expect(page.locator('.chat__empty-wrap')).toBeVisible()
    } finally {
      created.releasePostCreateList()
      created.releaseDetail()
    }

    await waitForShell(page)
  })

  test('failed starter-chat creation never navigates to an undefined chat', async ({ page }) => {
    const created = await routeShell(page, { createStatus: 503 })
    await cleanSession(page)

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => created.attempts, { timeout: 8000 }).toBeGreaterThan(0)
    await expect(page.getByText(/couldn't start a new chat/i)).toBeVisible()

    expect(created).toHaveLength(0)
    expect(await page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBeNull()
    expect(await page.evaluate(() => sessionStorage.getItem('draft:undefined')))
      .toBeNull()
  })
})

test.describe('Unauthenticated startup', () => {

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.removeItem('token')
      sessionStorage.removeItem('auth_expired')
    })
  })

  test('invalid credentials stay on the short-height login form', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 420 })
    await page.route(/\/api\/auth\/setup\/status$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ configured: true }),
      })
    )
    await page.route(/\/api\/auth\/token$/, route =>
      route.fulfill({
        status: 401,
        headers: { 'Content-Type': 'application/json' },
        body: '{"detail":"Incorrect username or password"}',
      })
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const username = page.getByLabel('Username')
    const password = page.getByLabel('Password')
    await expect(username).toBeVisible({ timeout: 10000 })

    // Safe top alignment keeps the beginning of an overflowing card reachable
    // when a short viewport or software keyboard reduces the visual height.
    expect(await page.locator('.login__card').evaluate(el => el.getBoundingClientRect().top))
      .toBeGreaterThanOrEqual(0)

    await username.fill('owner')
    await password.fill('wrong password')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('alert')).toHaveText('Incorrect username or password.')
    await expect(page.getByText(/session expired/i)).toHaveCount(0)
    await expect(username).toHaveValue('owner')
    expect(await page.evaluate(() => sessionStorage.getItem('auth_expired'))).toBeNull()
  })

  test('a login service failure is not mislabeled as a bad password', async ({ page }) => {
    await page.route(/\/api\/auth\/setup\/status$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ configured: true }),
      })
    )
    await page.route(/\/api\/auth\/token$/, route =>
      route.fulfill({
        status: 503,
        headers: { 'Content-Type': 'application/json' },
        body: '{"detail":"Authentication service is starting"}',
      })
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.getByLabel('Username').fill('owner')
    await page.getByLabel('Password').fill('correct password')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('alert'))
      .toHaveText('Authentication service is starting')
    await expect(page.getByText(/incorrect username or password/i)).toHaveCount(0)
  })

  test('setup-status failure pauses startup until a successful retry', async ({ page }) => {
    let checks = 0
    await page.route(/\/api\/auth\/setup\/status$/, route => {
      checks += 1
      if (checks === 1) {
        return route.fulfill({
          status: 503,
          headers: { 'Content-Type': 'application/json' },
          body: '{"detail":"starting"}',
        })
      }
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ configured: false }),
      })
    })

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Couldn’t reach Möbius' }))
      .toBeVisible({ timeout: 10000 })
    await expect(page.locator('.login')).toHaveCount(0)

    await page.getByRole('button', { name: 'Try again' }).click()
    await expect.poll(() => checks).toBe(2)
    await expect(page.getByRole('heading', { name: 'Create your home key' }))
      .toBeVisible({ timeout: 10000 })
  })

  test('managed deployment goes straight to Möbius sign-in', async ({ page }) => {
    let setupChecks = 0
    await page.route(/\/api\/auth\/setup\/status$/, route => {
      setupChecks += 1
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          configured: false,
          auth_mode: 'mobius_sso',
        }),
      })
    })
    await page.route(/\/api\/auth\/sso\/start(\?.*)?$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/html' },
        body: '<!doctype html><title>Managed sign-in</title>',
      })
    )

    const started = page.waitForRequest(/\/api\/auth\/sso\/start(\?.*)?$/)
    await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
    const request = await started

    expect(setupChecks).toBe(1)
    expect(new URL(request.url()).searchParams.get('return_path')).toBe('/shell/')
    await expect(page.getByRole('heading', { name: 'Create your home key' }))
      .toHaveCount(0)
    await expect(page.locator('.login')).toHaveCount(0)
  })

  test('managed handoff signs in and skips separate account setup', async ({ page }) => {
    let handoffCalls = 0
    let setupChecks = 0
    await page.route(/\/api\/auth\/setup\/status$/, route => {
      setupChecks += 1
      return route.fulfill({ status: 500, body: '{}' })
    })
    await page.route(/\/api\/auth\/sso\/session$/, route => {
      handoffCalls += 1
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          access_token: 'managed-owner-token',
          token_type: 'bearer',
          new_owner: true,
          return_path: '/',
        }),
      })
    })
    await page.route(/\/api\/auth\/providers\/status$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
    )

    await page.goto(`${BASE}/shell/?mobius_sso=1`, { waitUntil: 'domcontentloaded' })

    await expect(page.getByRole('heading', { name: 'Wake up your AI' }))
      .toBeVisible({ timeout: 10000 })
    expect(handoffCalls).toBe(1)
    expect(setupChecks).toBe(0)
    expect(await page.evaluate(() => localStorage.getItem('token')))
      .toBe('managed-owner-token')
    await expect(page.getByRole('heading', { name: 'Create your home key' }))
      .toHaveCount(0)
    await expect(page).toHaveURL(
      new RegExp(`${BASE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/shell/?$`)
    )
  })
})

test.describe('Logout cache wipe', () => {

  test('Settings sign-out clears local owner state without an expired-session banner', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 915 })
    await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await waitForShell(page)

    await page.evaluate(async () => {
      sessionStorage.setItem('draft:sign-out-test', 'private draft')
      await caches.open('mobius-sign-out-test')
      await caches.open('unrelated-cache-keep-me')
    })

    const nav = page.getByRole('button', { name: 'Toggle navigation' })
    if (await nav.getAttribute('aria-expanded') !== 'true') await nav.click()
    await page.getByRole('button', { name: 'Settings' }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    await page.getByRole('button', { name: 'Sign out', exact: true }).click()
    await expect(page.getByText(/clears chats, drafts, and app sessions/i)).toBeVisible()
    await page.getByRole('button', { name: 'Sign out', exact: true }).click()

    await expect(page.locator('.login')).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(/session expired/i)).toHaveCount(0)
    expect(await page.evaluate(() => localStorage.getItem('token'))).toBeNull()
    expect(await page.evaluate(() => sessionStorage.getItem('draft:sign-out-test'))).toBeNull()
    const cachesAfter = await page.evaluate(() => caches.keys())
    expect(cachesAfter).not.toContain('mobius-sign-out-test')
    expect(cachesAfter).toContain('unrelated-cache-keep-me')
  })

  test('clears BOTH mobius-* and workbox-* Cache Storage entries (fix c)', async ({ page }) => {
    // The real wipe runs inside api/client.js's 401 handler
    // (clearQueryCache → wipeSwCaches), which is the only production
    // path that fires it automatically when an authenticated request expires. We
    // drive that genuine path: seed both cache prefixes, force a 401
    // on an authenticated API call, and assert both prefixes are gone.
    await page.setViewportSize({ width: 412, height: 915 })

    // Let the shell mount normally first (real chats endpoint is fine —
    // we only need a same-origin authenticated page to run client code).
    await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )
    const initialChatsResponse = page.waitForResponse(response =>
      response.request().method() === 'GET'
      && /\/api\/chats\/?$/.test(response.url()),
    )
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const initialChats = await initialChatsResponse
    await initialChats.finished()
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
    let forced401Count = 0
    await page.route(/\/api\/chats\/?(?:\?.*)?$/, route => {
      forced401Count += 1
      return route.fulfill({ status: 401, body: '{"detail":"Not signed in"}' })
    })

    // Trigger a real apiFetch through the live client: opening the
    // drawer runs Shell's `if (drawerOpen) refreshChats()` effect,
    // which calls api.chats.list() → apiFetch('/chats') → 401 → wipe.
    // (No internal query-client handle needed.) The 401 clears the
    // token, so the post-reload page lands on the login form.
    await page.evaluate(() => {
      const btn = document.querySelector('[aria-expanded]')
      if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
    })
    await expect.poll(() => forced401Count, { timeout: 5000 }).toBeGreaterThan(0)

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
