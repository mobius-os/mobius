/**
 * Slash picker — per-chat agent settings UI.
 *
 * Locks in:
 *   1. The `/` button is mounted in the composer when a chat is open.
 *   2. Clicking it opens the popover with provider/model/effort radios.
 *   3. Picking a model issues a PATCH and updates the badge.
 *
 * The whole flow is route-mocked so no real LLM tokens are spent.
 *
 * Run: npx playwright test tests/slash-picker.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

const CHAT_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


/** Mocks the minimal API surface the picker needs:
 *    - GET  /api/chats                  → [one chat]
 *    - GET  /api/chats/{id}             → chat row with default settings
 *    - PATCH /api/chats/{id}            → echo override + return effective
 *    - GET  /api/chats/{id}/stream      → 204 (idle)
 *    - POST /api/chats/{id}/messages    → 202 ack (never invoked here)
 */
async function setupRoutes(page) {
  // Capture per-test override state in closure so PATCH echoes it back.
  let override = null
  const baseEffective = {
    model: 'claude-opus-4-5-20251001',
    effort: 'medium',
  }

  await page.route(/\/api\/chats(?:\?.*)?$/, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{
        id: CHAT_ID,
        title: 'Test chat',
        updated_at: new Date().toISOString(),
        has_messages: false,
      }]),
    })
  )

  await page.route(new RegExp(`/api/chats/${CHAT_ID}(\\?.*)?$`), route => {
    const method = route.request().method()
    if (method === 'GET') {
      const effective = {
        ...baseEffective,
        ...(override || {}),
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: CHAT_ID,
          title: 'Test chat',
          messages: [],
          pending_messages: [],
          total: 0,
          offset: 0,
          running: false,
          session_id: null,
          provider: 'claude',
          agent_settings_json: override,
          effective_agent_settings: effective,
          has_assistant_turns: false,
        }),
      })
    }
    if (method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (body.clear_agent_settings) {
        override = null
      } else if (body.agent_settings_json) {
        override = { ...(override || {}), ...body.agent_settings_json }
      }
      const effective = { ...baseEffective, ...(override || {}) }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          agent_settings_json: override,
          effective,
        }),
      })
    }
    return route.continue()
  })

  await page.route(new RegExp(`/api/chats/${CHAT_ID}/stream$`), route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route(new RegExp(`/api/chats/${CHAT_ID}/messages$`), route =>
    route.fulfill({ status: 202, body: '{}' })
  )
}


/** Navigate to a chat by its id via the URL the shell understands. */
async function openChat(page) {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(`${BASE}/chat/${CHAT_ID}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!document.querySelector('.chat__form'),
    { timeout: 10000 },
  )
}


test('slash picker mounts in composer and changes model', async ({ page }) => {
  await setupRoutes(page)
  await openChat(page)

  // The `/` trigger button is icon-only (no text badge); we identify
  // it by class, and read the active model from its aria-label /
  // title (kept as the discoverability surface for hover / SR users).
  const slashBtn = page.locator('.slash__btn')
  await expect(slashBtn).toBeVisible()
  await expect(slashBtn).toHaveAttribute('aria-label', /Opus 4\.5/)

  // Click opens the popover.
  await slashBtn.click()
  await expect(page.locator('.slash__popover')).toBeVisible()
  await expect(page.locator('.pmp__group-title').first()).toHaveText('Model')

  // Pick a different model — first non-current radio (Opus 4.7).
  const opus47 = page.locator('.pmp__radio', { hasText: 'Opus 4.7' }).first()
  await opus47.click()

  // The aria-label must update to reflect the new selection.
  await expect(slashBtn).toHaveAttribute(
    'aria-label', /Opus 4\.7/, { timeout: 3000 },
  )
})


test('slash picker effort chip toggles for Claude provider', async ({ page }) => {
  await setupRoutes(page)
  await openChat(page)

  await page.locator('.slash__btn').click()
  await expect(page.locator('.slash__popover')).toBeVisible()

  // Effort heading present (shared across providers since round 6).
  await expect(page.locator('.pmp__group-title', { hasText: 'Effort' }))
    .toBeVisible()

  // Pick High effort. Use an exact-match regex — `hasText: 'High'`
  // would substring-match "Extra high" too and Playwright's strict
  // mode would fail with a two-element resolution.
  const high = page.locator('.pmp__radio--chip', { hasText: /^High$/ })
  await high.click()
  await expect(high).toHaveClass(/pmp__radio--on/)
})
