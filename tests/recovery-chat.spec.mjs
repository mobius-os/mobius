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
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/recovery-chat.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Recovery now runs in its OWN recoveryd container and serves /recover* there,
// not from the app. /api/* still lives on the app (BASE); /recover* must target
// recoveryd. In the e2e compose recoveryd is exposed on 8011 (RECOVERY_TEST_PORT).
const RECOVER = process.env.MOBIUS_RECOVER_URL || 'http://localhost:8011'


// First-boot claim gate: the fixed value docker-compose.test.yml presets as
// MOBIUS_SETUP_CLAIM. Overridable to match a custom compose value.
const SETUP_CLAIM = process.env.MOBIUS_SETUP_CLAIM || 'mobius-test-setup-claim'

async function ensureOwner(page) {
  // Idempotent setup — first run creates admin/admin (presenting the claim),
  // later runs 400 (owner already exists) and we ignore.
  await page.request.post(`${BASE}/api/auth/setup`, {
    data: { username: 'admin', password: 'admin', claim: SETUP_CLAIM },
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
  if (!_cachedRecoverCookie) {
    const r = await page.request.post(`${RECOVER}/recover/auth`, {
      form: { username: 'admin', password: 'admin' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(200)
    // recoveryd sets its session cookie `Secure` UNCONDITIONALLY (it expects
    // TLS behind the reverse proxy in prod), so a browser context talking to
    // the plain-http e2e endpoint never stores it. Read the token straight out
    // of the Set-Cookie header and inject it below with secure:false —
    // recoveryd only reads the cookie VALUE on the way in, so this still
    // exercises the real auth + gated-route path over http.
    const setCookie = r.headers()['set-cookie'] || ''
    const m = setCookie.match(/moebius_recover=([^;]+)/)
    expect(m, 'recoveryd issued a moebius_recover cookie').toBeTruthy()
    _cachedRecoverCookie = m[1]
    expect(_cachedRecoverCookie.length).toBeGreaterThan(20)
  }
  await page.context().addCookies([{
    name: 'moebius_recover',
    value: _cachedRecoverCookie,
    domain: new URL(RECOVER).hostname,
    path: '/',
    httpOnly: true,
    secure: false,
    sameSite: 'Lax',
  }])
}


async function createChat(page, provider = 'claude') {
  // Helper for tests 3+4 that need an existing chat. Returns the
  // chat_id. Idempotent w.r.t. provider configuration — the route
  // creates a chat regardless of whether credentials are present
  // (the underlying runner writes a _meta line and an empty log).
  const r = await page.request.post(`${RECOVER}/recover/chat/new`, {
    data: { provider },
    failOnStatusCode: false,
  })
  expect(r.status(), 'recover/chat/new should succeed').toBe(200)
  const body = await r.json()
  expect(body.chat_id, 'chat_id returned').toBeTruthy()
  return body.chat_id
}


test.describe('Recovery chat — minimum-frozen UI', () => {

  test('1. /recover/chat without a session shows the login gate, not the chat', async ({ page }) => {
    // Clear any leftover cookies from prior tests in the same session.
    await page.context().clearCookies()
    const r = await page.request.get(`${RECOVER}/recover/chat`, {
      maxRedirects: 0,
      failOnStatusCode: false,
    })
    // recoveryd renders the login page inline for an unauthenticated request
    // (it used to 302 to /recover). The gate still holds — the chat surface
    // must NOT be exposed without a session.
    expect(r.status()).toBe(200)
    const html = await r.text()
    expect(html).toMatch(/password/i)
    expect(html).not.toContain('rc-newchat')
  })

  test('2. /recover/chat with cookie renders the picker page', async ({ page }) => {
    await loginToRecover(page)
    await page.goto(`${RECOVER}/recover/chat`)
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
    const r = await page.request.post(`${RECOVER}/recover/chat/send`, {
      data: { chat_id: chatId, text: 'lock-in test message' },
    })
    expect(r.status()).toBe(200)
    const body = await r.json()
    expect(typeof body.turn_id).toBe('number')

    // Reload the chat view — server-rendered history must include
    // our message.
    await page.goto(`${RECOVER}/recover/chat?chat=${chatId}`)
    await expect(
      page.locator('.rc-log .rc-msg.rc-user').last(),
    ).toContainText('lock-in test message')
  })

  test('4. Reset wipes the chat log', async ({ page }) => {
    await loginToRecover(page)
    const chatId = await createChat(page)
    // Seed: send a message first.
    const sendR = await page.request.post(`${RECOVER}/recover/chat/send`, {
      data: { chat_id: chatId, text: 'will be wiped' },
    })
    expect(sendR.status()).toBe(200)
    // POST reset.
    const r = await page.request.post(`${RECOVER}/recover/chat/reset`, {
      data: { chat_id: chatId },
    })
    expect(r.status()).toBe(200)
    // Reload — chat log should be empty.
    await page.goto(`${RECOVER}/recover/chat?chat=${chatId}`)
    await expect(page.locator('.rc-log .rc-msg')).toHaveCount(0)
  })

  test('5. Send rejects empty message', async ({ page }) => {
    await loginToRecover(page)
    const chatId = await createChat(page)
    const r = await page.request.post(`${RECOVER}/recover/chat/send`, {
      data: { chat_id: chatId, text: '   ' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(400)
  })

  test('6. /recover dashboard links to /recover/chat', async ({ page }) => {
    await loginToRecover(page)
    await page.goto(`${RECOVER}/recover`)
    const link = page.locator('a[href="/recover/chat"]')
    await expect(link).toBeVisible()
    await expect(link).toContainText('Run Recovery Agent')
  })

  test('7. /recover/chat/send requires cookie', async ({ page }) => {
    await page.context().clearCookies()
    const r = await page.request.post(`${RECOVER}/recover/chat/send`, {
      data: { chat_id: 'any', message: 'hi' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(401)
  })

  test('8. /recover/chat/send rejects missing chat_id', async ({ page }) => {
    await loginToRecover(page)
    const r = await page.request.post(`${RECOVER}/recover/chat/send`, {
      data: { message: 'no chat_id here' },
      failOnStatusCode: false,
    })
    // _extract_chat_id raises 400 with "chat_id required".
    expect(r.status()).toBe(400)
  })
})
