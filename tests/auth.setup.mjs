/**
 * Global setup: create the test account if needed, log in, and save auth state.
 *
 * On a fresh container (CI), the setup endpoint creates the owner account.
 * On an existing container, it 409s harmlessly.
 *
 * Also wipes chats from prior runs. The Playwright suite shares the
 * mobius-test DB across runs and tests; without this, chats pile up
 * (we've seen 400+) and every drawer-list fetch slows down until
 * tests start timing out.
 */
import { test as setup, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const USER = process.env.MOBIUS_USER || 'admin'
const PASS = process.env.MOBIUS_PASS || 'admin'
const AUTH_FILE = 'tests/.auth/state.json'

setup('authenticate', async ({ page, request }) => {
  // Ensure the owner account exists (idempotent — 409 if already set up).
  await request.post(`${BASE}/api/auth/setup`, {
    data: { username: USER, password: PASS },
  })

  // Wipe prior-run chats. Best-effort: get a token, list, delete each.
  // If the token fetch fails (fresh container, no owner yet) we just
  // skip — there's nothing to wipe anyway.
  const tokRes = await request.post(`${BASE}/api/auth/token`, {
    form: { username: USER, password: PASS },
    failOnStatusCode: false,
  })
  if (tokRes.ok()) {
    const { access_token } = await tokRes.json()
    const headers = { Authorization: `Bearer ${access_token}` }
    const listRes = await request.get(`${BASE}/api/chats`, { headers })
    if (listRes.ok()) {
      const chats = await listRes.json()
      await Promise.all(chats.map(c =>
        request.delete(`${BASE}/api/chats/${c.id}`, {
          headers, failOnStatusCode: false,
        })
      ))
    }
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
