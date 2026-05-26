/**
 * Recovery chat — minimum-frozen UI smoke.
 *
 * Verifies the page renders, the auth cookie carries through from
 * /recover/auth, and the in-page widgets are wired. The full agent
 * roundtrip (SSE stream of real Claude tokens) is not tested here
 * because mobius-test has no Claude credentials by default — that
 * lives in scripts/live-test.sh.
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


async function loginToRecover(page) {
  await ensureOwner(page)
  const r = await page.request.post(`${BASE}/recover/auth`, {
    form: { username: 'admin', password: 'admin' },
    failOnStatusCode: false,
  })
  expect(r.status()).toBe(200)
  const cookies = await page.context().cookies()
  const ck = cookies.find(c => c.name === 'moebius_recover')
  expect(ck, 'moebius_recover cookie set').toBeTruthy()
  expect(ck.value.length).toBeGreaterThan(20)
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

  test('2. /recover/chat with cookie renders the chat page', async ({ page }) => {
    await loginToRecover(page)
    await page.goto(`${BASE}/recover/chat`)
    await expect(page).toHaveTitle('Mobius recovery chat')
    await expect(page.locator('.rc-banner')).toContainText('Recovery mode')
    await expect(page.locator('#rc-input')).toBeVisible()
    await expect(page.locator('#rc-send')).toBeVisible()
    await expect(page.locator('#rc-restart-btn')).toBeVisible()
    await expect(page.locator('#rc-reset-btn')).toBeVisible()
  })

  test('3. Sending a message persists to /data/recovery_chat.jsonl', async ({ page }) => {
    await loginToRecover(page)
    // POST /recover/chat/send directly (skip the SSE part — no creds).
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { message: 'lock-in test message' },
    })
    expect(r.status()).toBe(200)
    const body = await r.json()
    expect(body.status).toBe('queued')

    // Reload the page — server-rendered history must include our message.
    await page.goto(`${BASE}/recover/chat`)
    await expect(
      page.locator('.rc-log .rc-msg.rc-user').last(),
    ).toContainText('lock-in test message')
  })

  test('4. Reset wipes the rendered log', async ({ page }) => {
    await loginToRecover(page)
    // Seed: send a message first.
    await page.request.post(`${BASE}/recover/chat/send`, {
      data: { message: 'will be wiped' },
    })
    // POST reset.
    const r = await page.request.post(`${BASE}/recover/chat/reset`)
    expect(r.status()).toBe(200)
    // Reload — should be empty.
    await page.goto(`${BASE}/recover/chat`)
    await expect(page.locator('.rc-log .rc-msg')).toHaveCount(0)
  })

  test('5. Send rejects empty message', async ({ page }) => {
    await loginToRecover(page)
    const r = await page.request.post(`${BASE}/recover/chat/send`, {
      data: { message: '   ' },
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
      data: { message: 'hi' },
      failOnStatusCode: false,
    })
    expect(r.status()).toBe(401)
  })
})
