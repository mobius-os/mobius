/**
 * Provider availability is authoritative in the chat model picker.
 * A registry can advertise models for installed CLIs that have no usable
 * credentials; those rows must never become actionable choices.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

test('picker exposes configured providers without leaking unavailable registry rows', async ({ page }) => {
  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      codex: { configured: true, authenticated: true },
      claude: { configured: false, authenticated: false },
    }),
  }))
  await page.route(/\/api\/models$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      providers: {
        codex: [
          { id: 'codex-fast', label: 'Codex Fast', available: true },
          { id: 'codex-deep', label: 'Codex Deep', available: true },
        ],
        claude: [
          { id: 'claude-one', label: 'Claude One', available: true },
          { id: 'claude-two', label: 'Claude Two', available: true },
          { id: 'claude-three', label: 'Claude Three', available: true },
        ],
      },
    }),
  }))
  await page.route(/\/api\/owner\/model-prefs$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ hidden_ids: [] }),
  }))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'provider-availability')
  expect(chat?.id).toBeTruthy()
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const paintedChat = page.locator('[data-chat-surface="painted"]')
  await expect(paintedChat.locator('.chat__form')).toBeVisible()

  await paintedChat.getByRole('button', { name: 'Attach or change model' }).click()

  const configuredRows = page.locator('button.csp-row:not([disabled])')
    .filter({ hasText: 'OpenAI Codex' })
  await expect(configuredRows).toHaveCount(2)

  // If Claude happens to be the owner's saved provider, its one selected row
  // may remain for context. The other registry rows must stay hidden and the
  // retained row must be disabled and clearly marked unavailable.
  const unavailableRows = page.locator('button.csp-row').filter({ hasText: 'Claude Code' })
  const unavailableCount = await unavailableRows.count()
  expect(unavailableCount).toBeLessThanOrEqual(1)
  if (unavailableCount === 1) {
    await expect(unavailableRows).toBeDisabled()
    await expect(unavailableRows).toContainText('Not connected')
  }
})

test('a second model choice stays selectable while the first choice saves', async ({ page }) => {
  const modelIdsByProvider = {
    claude: ['claude-test-one', 'claude-test-two', 'claude-test-three'],
    codex: ['gpt-test-one', 'gpt-test-two', 'gpt-test-three'],
  }
  const modelsByProvider = Object.fromEntries(
    Object.entries(modelIdsByProvider).map(([provider, ids]) => [
      provider,
      ids.map((id, index) => ({
        id,
        label: `Test model ${index + 1}`,
        provider,
        available: true,
      })),
    ]),
  )

  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      claude: { configured: true, authenticated: true },
      codex: { configured: true, authenticated: true },
    }),
  }))
  await page.route(/\/api\/models$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      providers: modelsByProvider,
    }),
  }))
  await page.route(/\/api\/owner\/model-prefs$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ hidden_ids: [] }),
  }))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'rapid-model-choice')
  expect(chat?.id).toBeTruthy()
  const provider = chat.detail?.provider || chat.provider
  expect(['claude', 'codex']).toContain(provider)
  const providerLabel = provider === 'claude' ? 'Claude Code' : 'OpenAI Codex'
  const modelIds = modelIdsByProvider[provider]

  let releaseFirst
  const firstHeld = new Promise(resolve => { releaseFirst = resolve })
  const savedModels = []
  await page.route(new RegExp(`/api/chats/${chat.id}$`), async route => {
    if (route.request().method() !== 'PATCH') return route.continue()
    const body = route.request().postDataJSON()
    const model = body.agent_settings_json?.model
    savedModels.push(model)
    if (savedModels.length === 1) await firstHeld
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        provider,
        agent_settings_json: body.agent_settings_json,
        effective: body.agent_settings_json,
      }),
    })
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const paintedChat = page.locator('[data-chat-surface="painted"]')
  await expect(paintedChat.locator('.chat__form')).toBeVisible()
  await paintedChat.getByRole('button', { name: 'Attach or change model' }).click()

  const rows = page.locator('button.csp-row').filter({ hasText: providerLabel })
  await expect(rows).toHaveCount(3)
  const first = rows.filter({ hasText: 'Test model 1' })
  const second = rows.filter({ hasText: 'Test model 2' })
  try {
    await first.click()
    await expect.poll(() => [...savedModels]).toEqual([modelIds[0]])
    await expect(second).toBeEnabled({ timeout: 500 })
    await second.click()
    await expect(second).toHaveAttribute('aria-pressed', 'true')
  } finally {
    releaseFirst()
  }

  await expect.poll(() => [...savedModels]).toEqual(modelIds.slice(0, 2))
  await expect(second).toHaveAttribute('aria-pressed', 'true')
})
