/**
 * Recovery chat — minimum-frozen UI smoke.
 *
 * Verifies the page renders, the auth cookie carries through from
 * /recover/auth, and the in-page widgets are wired. The full agent
 * roundtrip (SSE stream of real Claude tokens) is not tested here
 * because mobius-test has no Claude credentials by default — that
 * lives in scripts/live-test.sh.
 *
 * Updated 2026-05-27 for the multi-chat /recover/chat API: each
 * mutating endpoint now requires a `chat_id` in the body. The tests
 * exercise the create-a-chat-then-use-it flow rather than the old
 * single-chat shortcut.
 *
 * Run: npx playwright test tests/recovery-chat.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'


async function ensureOwner(page) {
  // Idempotent setup — first run creates admin/admin, later runs
  // 4xx (owner already exists) and we ignore.
  await page.request.post(`${BASE}/api/auth/setup`, {
    data: { username: 'admin', password: 'admin' },
    failOnStatusCode: false,
  })
}


// /recover/auth is rate-limited to 5/minute. With 6+ tests each
// doing their own fresh login, the limit fires part-way through the
// suite and the rest get 429s. Cache the cookie value at module
// level after the first successful login and inject it into each
// subsequent test's context — one network login per file run, the
// rate limit stays happy.
let _cachedRecoverCookie = null

async function loginToRecover(page) {
  await ensureOwner(page)
  if (_cachedRecoverCookie) {
    // Reuse the previously-issued cookie. The PWA cookie has a
    // long enough TTL to span an entire test suite run; we only
    // pay the auth round-trip once.
    await page.context().addCookies([{
      name: 'moebius_recover',
      value: _cachedRecoverCookie,
      domain: new URL(BASE).hostname,
      path: '/',
      httpOnly: true,
      secure: BASE.startsWith('https://'),
      sameSite: 'Lax',
    }])
    return
  }
  const r = await page.request.post(`${BASE}/recover/auth`, {
    form: { username: 'admin', password: 'admin' },
    failOnStatusCode: false,
  })
  expect(r.status()).toBe(200)
  const cookies = await page.context().cookies()
  const ck = cookies.find(c => c.name === 'moebius_recover')
  expect(ck, 'moebius_recover cookie set').toBeTruthy()
  expect(ck.value.length).toBeGreaterThan(20)
  _cachedRecoverCookie = ck.value
}


async function createChat(page, provider = 'claude') {
  // Helper for tests 3+4 that need an existing chat. Returns the
  // chat_id. Idempotent w.r.t. provider configuration — the route
  // creates a chat regardless of whether credentials are present
  // (the underlying runner writes a _meta line and an empty log).
  const r = await page.request.post(`${BASE}/recover/chat/new`, {
    data: { provider },
    failOnStatusCode: false,
  })
  expect(r.status(), 'recover/chat/new should succeed').toBe(200)
  const body = await r.json()
  expect(body.chat_id, 'chat_id returned').toBeTruthy()
  return body.chat_id
}


test.describe('Recovery chat — minimum-frozen UI', () => {

  test('1. /recover/chat without cookie 302s to /recover', async ({ page }) => {
    // Clear any leftover cookies from prior tests in the same session.
    await page.context().clearCookies()
    const r = await page.request.get(`${BASE}/recover/chat`, {
      maxRedirects: 0,
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(302)
  })

  test('2. /recover/chat with cookie renders the picker page', async ({ page }) => {
    await loginToRecover(page)
    await page.goto(`${BASE}/recover/chat`)
    await expect(page).toHaveTitle('Mobius recovery chat')
    // Banner is shared across picker + chat views — must be visible
    // on either entry point.
    await expect(page.locator('.rc-banner')).toContainText('Recovery mode')
    // No `?id=` means the picker view is the primary content. The
    // chat-view container is in the DOM but hidden via inline
    // `display: none` until a chat is selected. Both the picker
    // and the chat view use IDs (not classes) for the section
    // wrapper, so `#rc-picker-view` is the right selector.
    await expect(page.locator('#rc-picker-view')).toBeVisible()
    // The "new chat" form must always be reachable from the picker.
    await expect(page.locator('.rc-newchat')).toBeVisible()
  })

  test('3. Sending a message persists to the chat log', async ({ page }) => {
    await loginToRecover(page)
    const chatId = await createChat(page)
    // POST /recover/chat/send with the multi-chat schema.
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { chat_id: chatId, message: 'lock-in test message' },
    })
    expect(r.status()).toBe(200)
    const body = await r.json()
    expect(body.status).toBe('queued')

    // Reload the chat view — server-rendered history must include
    // our message.
    await page.goto(`${BASE}/recover/chat?id=${chatId}`)
    await expect(
      page.locator('.rc-log .rc-msg.rc-user').last(),
    ).toContainText('lock-in test message')
  })

  test('4. Reset wipes the chat log', async ({ page }) => {
    await loginToRecover(page)
    const chatId = await createChat(page)
    // Seed: send a message first.
    const sendR = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { chat_id: chatId, message: 'will be wiped' },
    })
    expect(sendR.status()).toBe(200)
    // POST reset.
    const r = await page.request.post(`${BASE}/recover/chat/reset`, {
      data: { chat_id: chatId },
    })
    expect(r.status()).toBe(200)
    // Reload — chat log should be empty.
    await page.goto(`${BASE}/recover/chat?id=${chatId}`)
    await expect(page.locator('.rc-log .rc-msg')).toHaveCount(0)
  })

  test('5. Send rejects empty message', async ({ page }) => {
    await loginToRecover(page)
    const chatId = await createChat(page)
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { chat_id: chatId, message: '   ' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(400)
  })

  test('6. /recover dashboard links to /recover/chat', async ({ page }) => {
    await loginToRecover(page)
    await page.goto(`${BASE}/recover`)
    const link = page.locator('a[href="/recover/chat"]')
    await expect(link).toBeVisible()
    await expect(link).toContainText('Open recovery chat')
  })

  test('7. /recover/chat/send requires cookie', async ({ page }) => {
    await page.context().clearCookies()
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { chat_id: 'any', message: 'hi' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(401)
  })

  test('8. /recover/chat/send rejects missing chat_id', async ({ page }) => {
    await loginToRecover(page)
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { message: 'no chat_id here' },
      failOnStatusCode: false,
    })
    // _extract_chat_id raises 400 with "chat_id required".
    expect(r.status()).toBe(400)
  })
})
