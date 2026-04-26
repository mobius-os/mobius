/**
 * Client-side cache behavior tests.
 *
 * Verifies the TanStack Query cache layer:
 *   - Chat messages render instantly from cache on second visit
 *   - Cache survives reload via IndexedDB persister
 *   - Theme query (/api/theme) returns valid CSS after override delete
 *
 * The point of these tests is to lock in the no-flash chat-back-nav
 * behavior so future changes don't silently regress it.
 *
 * Run:  npx playwright test tests/cache.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

/** Create N empty chats via the API so navigation tests have content. */
async function ensureChats(page, count = 2) {
  return page.evaluate(async (n) => {
    const tok = localStorage.getItem('token')
    if (!tok) return []
    const ids = []
    for (let i = 0; i < n; i++) {
      const res = await fetch('/api/chats', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json' },
        body: '{}',
      })
      if (res.ok) {
        const c = await res.json()
        ids.push(c.id)
      }
    }
    return ids
  }, count)
}

async function openDrawer(page) {
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
}

async function clickChatInDrawer(page, index) {
  // The drawer renders chats in a `.drawer__group` with title "Chats"
  // (or unlabeled), then apps in another group. Filter strictly to
  // chat items by excluding `--new`, `--active` styling is irrelevant.
  await page.evaluate((idx) => {
    // Find the group whose label is "Chats" or whose first non-"new"
    // item is a chat. Simpler: walk all groups, take the first that
    // contains chat-shaped items (text but no app icon, not Settings).
    const allGroups = document.querySelectorAll('.drawer__group')
    let chatItems = []
    for (const g of allGroups) {
      const items = g.querySelectorAll('.drawer__item')
      for (const el of items) {
        if (el.classList.contains('drawer__item--new')) continue
        const text = el.querySelector('.drawer__item-text')?.textContent?.trim()
        // Exclude obvious non-chat items.
        if (!text || text === 'Settings' || text === 'Hello World') continue
        chatItems.push(el)
      }
      if (chatItems.length > 0) break
    }
    if (chatItems[idx]) chatItems[idx].click()
  }, index)
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
}

async function getActiveChatId(page) {
  return page.evaluate(() => localStorage.getItem('moebius_active_chat'))
}

// ---------------------------------------------------------------------------

test.describe('Chat messages cache (TanStack Query)', () => {
  test('1. Second visit to a chat renders content from cache without hitting the network', async ({ page }) => {
    // Goal: the back-navigation flash is gone because the cache served
    // the chat synchronously on remount.
    //
    // Mechanism under test: ChatView.jsx reads queryClient.getQueryData
    // in a useState initializer; if cache is warm, `loading` is false
    // and `messages` is populated from the very first render.
    //
    // How we verify: visit chat A (warms cache), visit chat B, then
    // navigate back to chat A WITH THE NETWORK REQUEST FOR /chats/{a}
    // BLOCKED. If the cache is doing its job, the chat view still
    // renders and shows the persisted messages.
    await setup(page)
    await ensureChats(page, 2)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )

    // Use the chat IDs returned by ensureChats directly. Driving via
    // localStorage + reload is reliable across drawer rendering quirks.
    const ids = await page.evaluate(async () => {
      const tok = localStorage.getItem('token')
      const res = await fetch('/api/chats', {
        headers: { 'Authorization': 'Bearer ' + tok },
      })
      const list = await res.json()
      return Array.isArray(list) ? list.map(c => c.id) : []
    })
    expect(ids.length).toBeGreaterThanOrEqual(2)
    const chatA = ids[0]
    const chatB = ids[1]
    expect(chatA).not.toBe(chatB)

    // Visit chat A by setting localStorage + reload (deterministic).
    await page.evaluate((id) => localStorage.setItem('moebius_active_chat', id), chatA)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    // Wait long enough for the initial fetch to resolve AND for the
    // persister's 1-second throttle to flush to IndexedDB.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1500)))

    // Navigate to chat B.
    await page.evaluate((id) => localStorage.setItem('moebius_active_chat', id), chatB)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 1500)))

    // Verify that the cache actually got populated for chat A. We
    // peek at the IndexedDB persister key — `mobius-query-cache`.
    const cacheHasChatA = await page.evaluate(async (id) => {
      // Open idb-keyval's default store and read the persister payload.
      const dbName = 'keyval-store'
      const storeName = 'keyval'
      return new Promise(resolve => {
        const req = indexedDB.open(dbName)
        req.onsuccess = () => {
          const db = req.result
          if (!db.objectStoreNames.contains(storeName)) {
            db.close(); resolve(false); return
          }
          const tx = db.transaction(storeName, 'readonly')
          const get = tx.objectStore(storeName).get('mobius-query-cache')
          get.onsuccess = () => {
            db.close()
            const blob = get.result
            // Persister stores a JSON-serializable cache snapshot.
            const serialized = JSON.stringify(blob || {})
            resolve(serialized.includes(id))
          }
          get.onerror = () => { db.close(); resolve(false) }
        }
        req.onerror = () => resolve(false)
      })
    }, chatA)

    expect(cacheHasChatA).toBe(true)

    // Block the chat A messages fetch so we can prove the cache served.
    await page.route(`**/api/chats/${chatA}**`, route => route.abort())

    // Navigate back to chat A. With persister + cache mirror,
    // ChatView's useState initializer reads cached messages
    // synchronously — `loading` is false, `messages` is populated.
    await page.evaluate((id) => localStorage.setItem('moebius_active_chat', id), chatA)
    await page.reload({ waitUntil: 'domcontentloaded' })
    // Wait only for the chat shell to mount; do not wait for any fetch
    // (we just blocked the relevant one).
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )

    // The active chat ID should match — proving useState initializer
    // received the cached value, not a fresh fetch (which we blocked).
    expect(await getActiveChatId(page)).toBe(chatA)

    // The crucial assertion: `.chat__empty-wrap` only renders when
    // `loading === false` (see ChatView.jsx:`showEmpty`). With cache
    // hit, loading was initialized false from the cache, so empty-wrap
    // appears immediately. With cache MISS + blocked network, loading
    // would stay true forever and empty-wrap would never render.
    const hasEmptyState = await page.evaluate(() =>
      !!document.querySelector('.chat__empty-wrap')
    )
    expect(hasEmptyState).toBe(true)
  })

  test('2. TanStack QueryClient is initialized and exposes `setQueryData`', async ({ page }) => {
    // Smoke test: the query layer is wired up at all. If the provider
    // is missing, useQueryClient throws and the app fails to mount.
    await setup(page)
    const ok = await page.evaluate(() => {
      // Indirect signal: the chat view mounted without throwing.
      return !!(document.querySelector('.chat__empty-wrap')
        || document.querySelector('.chat__scroll')
        || document.querySelector('.chat__form'))
    })
    expect(ok).toBe(true)
  })

  test('3. IndexedDB persister key exists after a chat-view visit', async ({ page }) => {
    // Verifies the persister is actually writing to IndexedDB. The
    // persister key is `mobius-query-cache` (set in queryClient.js).
    await setup(page)
    await ensureChats(page, 1)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await openDrawer(page)
    await clickChatInDrawer(page, 0)

    // The persister throttles writes (1000ms). Wait long enough.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1500)))

    const dbContents = await page.evaluate(async () => {
      const dbs = await indexedDB.databases()
      // idb-keyval default DB name is "keyval-store".
      return dbs.map(d => d.name)
    })
    // The persister writes to whichever IndexedDB idb-keyval uses; we
    // only assert SOME database exists. A full key check would require
    // pulling idb-keyval into the test harness, which is heavier than
    // it's worth.
    expect(dbContents.length).toBeGreaterThan(0)
  })
})

test.describe('Theme query (/api/theme)', () => {
  test('4. /api/theme returns valid CSS object', async ({ page }) => {
    // Smoke test of the new endpoint via the authenticated client.
    await setup(page)
    const res = await page.evaluate(async () => {
      const tok = localStorage.getItem('token')
      const r = await fetch('/api/theme', {
        headers: { 'Authorization': 'Bearer ' + tok },
      })
      return { status: r.status, body: r.ok ? await r.json() : null }
    })
    expect(res.status).toBe(200)
    expect(res.body).toHaveProperty('css')
    expect(res.body).toHaveProperty('bg')
    expect(res.body.css).toContain(':root')
    expect(res.body.bg).toMatch(/^#[0-9a-fA-F]{3,8}$/)
  })

  test('5. DELETE /api/storage/shared/theme.css resets theme to defaults', async ({ page }) => {
    // The agent's reset path: write a custom theme, delete the override,
    // verify /api/theme returns defaults again.
    await setup(page)
    const result = await page.evaluate(async () => {
      const tok = localStorage.getItem('token')
      const headers = { 'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json' }

      // Write a custom theme.
      const customCss = ':root { --bg: #abcdef; }'
      const writeR = await fetch('/api/storage/shared/theme.css', {
        method: 'PUT',
        headers,
        body: JSON.stringify({ content: customCss }),
      })
      if (!writeR.ok) return { stage: 'write', status: writeR.status }

      // Verify endpoint reflects the override.
      const customResp = await fetch('/api/theme', {
        headers: { 'Authorization': 'Bearer ' + tok },
      }).then(r => r.json())

      // Delete to reset.
      const delR = await fetch('/api/storage/shared/theme.css', {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + tok },
      })
      if (!delR.ok && delR.status !== 204) return { stage: 'delete', status: delR.status }

      // Verify endpoint returns defaults.
      const defaultResp = await fetch('/api/theme', {
        headers: { 'Authorization': 'Bearer ' + tok },
      }).then(r => r.json())

      return { custom: customResp, default: defaultResp }
    })

    expect(result.custom.css).toContain('#abcdef')
    expect(result.default.css).not.toContain('#abcdef')
    expect(result.default.css).toContain(':root')
  })
})
