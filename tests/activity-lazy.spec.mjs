/**
 * Browser contract for the collapsed activity timeline and its lazy sidecars.
 * The transcript itself is bounded; full thinking/tool payloads are requested
 * only by the nested disclosure that needs them and are released on collapse.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/activity-lazy.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block', hasTouch: true })
attachCleanup()

test('activity stays nested and lazy, aborts on close, and copies excerpt or full output', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.addInitScript(() => {
    window.__copiedToolText = []
    window.__clipboardShouldFail = false
    window.__lazyRequestStarts = { thinking: 0, tool: 0 }
    window.__lazyRequestAborts = { thinking: 0, tool: 0 }

    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        async writeText(value) {
          if (window.__clipboardShouldFail) throw new Error('clipboard unavailable')
          window.__copiedToolText.push(value)
        },
      },
    })
    const nativeExecCommand = document.execCommand?.bind(document)
    document.execCommand = (command, ...args) => {
      if (command === 'copy' && window.__clipboardShouldFail) return false
      return nativeExecCommand ? nativeExecCommand(command, ...args) : false
    }

    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url || ''
      const kind = url.includes('/thinking-trace/')
        ? 'thinking'
        : url.includes('/tool-output/')
          ? 'tool'
          : null
      if (kind) {
        window.__lazyRequestStarts[kind] += 1
        const signal = init.signal || (typeof input !== 'string' ? input?.signal : null)
        signal?.addEventListener('abort', () => {
          window.__lazyRequestAborts[kind] += 1
        }, { once: true })
      }
      return nativeFetch(input, init)
    }
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )
  const chat = await createTaggedChat(page, 'lazy-activity')

  const thinkingId = 'thinking-ui-contract'
  const toolUseId = 'tool-ui-contract'
  const excerpt = 'EXCERPT_SENTINEL: bounded output already on the transcript'
  const fullOutput = 'FULL_OUTPUT_SENTINEL: lazily fetched command output'
  const thoughtText = 'THINKING_SENTINEL: lazily fetched reasoning trace'
  const blocks = [
    {
      type: 'thinking',
      content: '',
      duration_ms: 2400,
      thinking_id: thinkingId,
      thinking_deferred: true,
      thinking_revision: thoughtText.length,
    },
    {
      type: 'tool',
      tool: 'Bash',
      status: 'done',
      input: 'git status --short',
      output: excerpt,
      tool_use_id: toolUseId,
      output_truncated: true,
      output_full_len: 8192,
    },
    { type: 'text', content: 'The answer stays visually primary.' },
  ]
  const messages = [
    { role: 'user', content: 'Inspect the repository.', ts: 1700000000000 },
    { role: 'assistant', content: '', blocks, ts: 1700000001000 },
  ]

  await page.route(new RegExp(`/api/chats/${chat.id}\\?limit=`), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...chat,
        messages,
        total: messages.length,
        offset: 0,
        running: false,
        pending_messages: [],
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/stream$`), route =>
    route.fulfill({ status: 204, body: '' }))

  let thinkingRequests = 0
  await page.route(new RegExp(`/api/chats/${chat.id}/thinking-trace/${thinkingId}`), route => {
    thinkingRequests += 1
    return route.fulfill({ status: 200, contentType: 'text/plain', body: thoughtText })
  })

  let toolRequests = 0
  await page.route(new RegExp(`/api/chats/${chat.id}/tool-output/${toolUseId}$`), async route => {
    toolRequests += 1
    if (toolRequests === 1) {
      // Keep the first request pending long enough to exercise excerpt copying
      // and abort-on-collapse. A second open receives the full output at once.
      await new Promise(resolve => setTimeout(resolve, 1800))
    }
    try {
      await route.fulfill({ status: 200, contentType: 'text/plain', body: fullOutput })
    } catch {
      // Expected for the first request: the disclosure aborts it before this
      // delayed test response is released.
    }
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.getByText('The answer stays visually primary.')).toBeVisible()

  const activity = page.locator('.chat__activity')
  const activityHeader = activity.locator('.chat__activity-header')
  await expect(activityHeader).toHaveAttribute('aria-expanded', 'false')
  await expect(activity.locator('.chat__activity-timeline')).toHaveCount(0)
  await page.waitForTimeout(250)
  expect(thinkingRequests).toBe(0)
  expect(toolRequests).toBe(0)

  await activityHeader.click()
  await expect(activityHeader).toHaveAttribute('aria-expanded', 'true')
  const thoughtToggle = activity.locator('.chat__activity-think-toggle')
  const toolToggle = activity.locator('.chat__tool-header')
  await expect(thoughtToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(toolToggle).toHaveAttribute('aria-expanded', 'false')
  expect(thinkingRequests).toBe(0)
  expect(toolRequests).toBe(0)

  // The rail and leading type icons communicate hierarchy; activity rows carry
  // no trailing disclosure chevrons.
  await expect(activity.locator('.chat__chevron')).toHaveCount(0)
  expect(await activity.locator('svg').count()).toBeGreaterThanOrEqual(3)
  const [activityBox, timelineBox, toolBox] = await Promise.all([
    activityHeader.boundingBox(),
    activity.locator('.chat__activity-timeline').boundingBox(),
    toolToggle.boundingBox(),
  ])
  expect(activityBox).not.toBeNull()
  expect(timelineBox.x).toBeGreaterThan(activityBox.x + 12)
  expect(toolBox.x).toBeGreaterThan(timelineBox.x + 8)
  for (const control of [activityHeader, thoughtToggle, toolToggle]) {
    const box = await control.boundingBox()
    expect(box.height).toBeGreaterThanOrEqual(43)
  }

  await thoughtToggle.click()
  await expect(page.getByText(thoughtText)).toBeVisible()
  expect(thinkingRequests).toBe(1)
  await thoughtToggle.click()
  await expect(page.getByText(thoughtText)).toHaveCount(0)
  await thoughtToggle.click()
  await expect(page.getByText(thoughtText)).toBeVisible()
  expect(thinkingRequests).toBe(2)
  await thoughtToggle.click()

  await toolToggle.click()
  await expect(page.getByRole('button', { name: 'Copy excerpt' })).toBeEnabled()
  await expect(page.getByText(/loading full output/i)).toBeVisible()
  await page.getByRole('button', { name: 'Copy excerpt' }).click()
  await expect.poll(() => page.evaluate(() => window.__copiedToolText.at(-1))).toBe(excerpt)

  // Collapse before the delayed response lands: the browser request is really
  // aborted, not merely ignored, and reopening starts a fresh bounded fetch.
  await toolToggle.click()
  await expect.poll(() => page.evaluate(() => window.__lazyRequestAborts.tool)).toBe(1)
  await toolToggle.click()
  await expect(page.getByText(fullOutput)).toBeVisible()
  expect(toolRequests).toBe(2)
  await expect(page.getByRole('button', { name: 'Copy output' })).toBeVisible()
  await page.getByRole('button', { name: 'Copy output' }).click()
  await expect.poll(() => page.evaluate(() => window.__copiedToolText.at(-1))).toBe(fullOutput)

  // Clipboard failures are visible and retryable rather than hidden only in an
  // aria-label or leaving the owner unsure whether anything happened.
  await page.evaluate(() => { window.__clipboardShouldFail = true })
  await activity.locator('.chat__tool-copy').click()
  await expect(activity.getByText('Copy failed')).toBeVisible()
  await page.evaluate(() => { window.__clipboardShouldFail = false })
  await page.getByRole('button', { name: 'Could not copy output' }).click()
  await expect(activity.getByText('Copied')).toBeVisible()

  // Closing the outer stretch unmounts every nested payload; reopening proves
  // both sidecars are still closed and no hidden request is made.
  await activityHeader.click()
  await expect(activity.locator('.chat__activity-timeline')).toHaveCount(0)
  await activityHeader.click()
  await expect(activity.locator('.chat__activity-timeline')).toBeVisible()
  await expect(activity.locator('.chat__activity-think-toggle')).toHaveAttribute('aria-expanded', 'false')
  await expect(activity.locator('.chat__tool-header')).toHaveAttribute('aria-expanded', 'false')
  expect(thinkingRequests).toBe(2)
  expect(toolRequests).toBe(2)
})
