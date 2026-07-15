/**
 * Global setup: create the test account if needed, log in, and save auth state.
 *
 * On a fresh container (CI), the setup endpoint creates the owner account.
 * On an existing container, it 409s harmlessly.
 *
 * This setup must never enumerate or delete an owner's chats. Every
 * browser run is required to identify itself as an isolated test runtime,
 * and individual specs clean up only the fixture IDs they created.
 */
import { test as setup } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const USER = process.env.MOBIUS_USER || 'admin'
const PASS = process.env.MOBIUS_PASS || 'admin'
const AUTH_FILE = process.env.MOBIUS_AUTH_FILE || 'tests/.auth/state.json'

// First guard: only local-family hosts on a non-production port.
const allowedHosts = ['localhost', '127.0.0.1', '0.0.0.0', '172.17.0.1']
const baseUrl = new URL(BASE)
if (!allowedHosts.includes(baseUrl.hostname) || baseUrl.port === '8000') {
  throw new Error(
    `auth.setup refuses to run against ${BASE} — only local mobius-test ` +
    `(non-prod port, localhost-family host) is allowed. ` +
    `Setup writes test-owner state and must only target an isolated runtime.`
  )
}

setup('authenticate', async ({ page, request }) => {
  // Authoritative guard: a localhost URL can still be a proxy to the live
  // backend. Verify the backend itself opted into test mode before making
  // any authenticated or write request.
  const versionRes = await request.get(`${BASE}/api/version`, {
    failOnStatusCode: false,
  })
  const version = versionRes.ok() ? await versionRes.json() : null
  if (version?.test_runtime !== true) {
    throw new Error(
      `auth.setup refuses ${BASE}: /api/version did not report ` +
      `test_runtime=true. Use the isolated local E2E runner or hosted CI.`
    )
  }

  // Ensure the owner account exists (idempotent — 409 if already set up).
  await request.post(`${BASE}/api/auth/setup`, {
    data: { username: USER, password: PASS },
  })

  // Fetch the test-owner token so the walkthrough cannot intercept clicks.
  const tokRes = await request.post(`${BASE}/api/auth/token`, {
    form: { username: USER, password: PASS },
    failOnStatusCode: false,
  })
  if (tokRes.ok()) {
    const { access_token } = await tokRes.json()
    const headers = { Authorization: `Bearer ${access_token}` }
    // Mark walkthrough complete server-side so the WalkthroughOverlay
    // doesn't render during tests. The overlay's centered-modal +
    // backdrop intercepts every pointer event in the shell; without
    // this, every spec that clicks shell UI (Send button, drawer
    // items, chat rows) times out with "...wt__overlay intercepts
    // pointer events". The walkthrough is a first-sign-in feature
    // not relevant to any of these tests' contracts. POST is
    // idempotent (write-once on the server) — re-running is safe.
    await request.post(`${BASE}/api/owner/walkthrough/complete`, {
      headers, failOnStatusCode: false,
    })
  }

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })

  // Wait for either login form or already-authenticated shell.
  const state = await page.evaluate(() => new Promise(resolve => {
    const check = () => {
      if (document.querySelector('.login')) return resolve('login')
      if (document.querySelector('.chat__empty-wrap')
        || document.querySelector('.chat__scroll')
        || document.querySelector('.chat__form')) return resolve('app')
      requestAnimationFrame(check)
    }
    check()
    setTimeout(() => resolve('timeout'), 15000)
  }))

  if (state === 'login') {
    await page.getByRole('textbox', { name: 'Username' }).fill(USER)
    await page.getByRole('textbox', { name: 'Password' }).fill(PASS)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__scroll')
            || document.querySelector('.chat__form')),
      { timeout: 15000 }
    )
  }

  await page.context().storageState({ path: AUTH_FILE })
})
