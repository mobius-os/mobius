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

async function mockPickerData(page, { providerStatus, providers }) {
  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(providerStatus),
  }))
  await page.route(/\/api\/models$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ providers }),
  }))
  await page.route(/\/api\/owner\/model-prefs$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ hidden_ids: [] }),
  }))
}

async function openPicker(page, chatId) {
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chatId)}`, {
    waitUntil: 'domcontentloaded',
  })
  const paintedChat = page.locator('[data-chat-surface="painted"]')
  await expect(paintedChat.locator('.chat__form')).toBeVisible()
  await paintedChat.locator('.chat__brain-usage').click()
}

test('picker exposes configured providers without leaking unavailable registry rows', async ({ page }) => {
  await mockPickerData(page, {
    providerStatus: {
      codex: { configured: true, authenticated: true },
      claude: { configured: false, authenticated: false },
    },
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
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(
    page,
    'provider-availability',
    { mockProvider: false },
  )
  expect(chat?.id).toBeTruthy()
  await openPicker(page, chat.id)

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

test('model and effort choices stay interactive while saves remain ordered', async ({ page }) => {
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

  await mockPickerData(page, {
    providerStatus: {
      claude: { configured: true, authenticated: true },
      codex: { configured: true, authenticated: true },
    },
    providers: modelsByProvider,
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(
    page,
    'rapid-model-choice',
    { mockProvider: false },
  )
  expect(chat?.id).toBeTruthy()
  const provider = chat.detail?.provider || chat.provider
  expect(['claude', 'codex']).toContain(provider)
  const providerLabel = provider === 'claude' ? 'Claude Code' : 'OpenAI Codex'
  const modelIds = modelIdsByProvider[provider]

  let releaseFirst
  const firstHeld = new Promise(resolve => { releaseFirst = resolve })
  const startedSettings = []
  let persistedSettings = {}
  await page.route(new RegExp(`/api/chats/${chat.id}$`), async route => {
    if (route.request().method() !== 'PATCH') return route.continue()
    const body = route.request().postDataJSON()
    startedSettings.push(body.agent_settings_json)
    if (startedSettings.length === 1) await firstHeld
    persistedSettings = { ...persistedSettings, ...body.agent_settings_json }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        provider,
        agent_settings_json: persistedSettings,
        effective: persistedSettings,
      }),
    })
  })

  await openPicker(page, chat.id)

  const rows = page.locator('button.csp-row').filter({ hasText: providerLabel })
  await expect(rows).toHaveCount(3)
  const first = rows.filter({ hasText: 'Test model 1' })
  const second = rows.filter({ hasText: 'Test model 2' })
  let alternateEffort
  try {
    await first.click()
    await expect.poll(() => startedSettings.map(settings => settings.model))
      .toEqual([modelIds[0]])
    await expect(second).toBeEnabled({ timeout: 500 })
    await second.click()
    await expect(second).toHaveAttribute('aria-pressed', 'true')

    const effortGroup = page.getByRole('radiogroup', { name: 'Reasoning effort' })
    alternateEffort = effortGroup.locator('[role="radio"][aria-checked="false"]').first()
    const alternateEffortName = await alternateEffort.getAttribute('aria-label')
    alternateEffort = effortGroup.getByRole('radio', {
      name: alternateEffortName,
      exact: true,
    })
    await expect(alternateEffort).toBeEnabled()
    await alternateEffort.click()
    await expect(alternateEffort).toHaveAttribute('aria-checked', 'true')

    await page.waitForTimeout(100)
    expect(startedSettings.map(settings => settings.model)).toEqual([modelIds[0]])
  } finally {
    releaseFirst()
  }

  await expect.poll(() => startedSettings.length).toBe(3)
  expect(startedSettings.slice(0, 2).map(settings => settings.model))
    .toEqual(modelIds.slice(0, 2))
  expect(startedSettings[2].model).toBeUndefined()
  expect(startedSettings[2].effort).toBeTruthy()
  await expect(second).toHaveAttribute('aria-pressed', 'true')
  await expect(alternateEffort).toHaveAttribute('aria-checked', 'true')
})
