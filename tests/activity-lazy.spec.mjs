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

test('the first thinking event becomes interactive without moving the row', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill({ status: 202, contentType: 'application/json', body: '{"status":"started"}' })
  })
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' }))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )
  const chat = await createTaggedChat(page, 'thinking-handoff')
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })

  // The empty chat opens one terminal stream before a turn starts. Install
  // the gated handler only after that mount so the delayed response belongs to
  // the message below, leaving the real placeholder observable first.
  let releaseThinking
  const thinkingGate = new Promise(resolve => { releaseThinking = resolve })
  let streamCount = 0
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
    streamCount += 1
    if (streamCount > 1) return route.fulfill({ status: 204, body: '' })
    await thinkingGate
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: [
        `data: ${JSON.stringify({
          type: 'thinking',
          content: 'Checking the handoff.',
          ts: 1700000001000,
        })}\n\n`,
        'data: {"type":"done"}\n\n',
      ].join(''),
    })
  })

  await page.getByRole('textbox', { name: 'Message Möbius…' }).fill('Think about this')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(page.locator('.chat__msg--user')).toBeVisible()

  const placeholder = page.locator('.chat__thinking .chat__activity-header')
  await expect(placeholder).toBeVisible()
  await expect(placeholder).not.toHaveAttribute('aria-expanded', /.+/)
  const before = await placeholder.boundingBox()
  expect(before).not.toBeNull()

  releaseThinking()
  const thought = page.locator(
    '.chat__activity--direct-thought > .chat__activity-header[aria-expanded]',
  )
  await expect(thought).toBeVisible()
  await expect(thought).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('.chat__thinking')).toHaveCount(0)
  await page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const after = await thought.boundingBox()
  expect(after).not.toBeNull()

  expect(Math.abs(after.x - before.x)).toBeLessThanOrEqual(0.5)
  expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(0.5)
  expect(Math.abs(after.width - before.width)).toBeLessThanOrEqual(0.5)
  expect(Math.abs(after.height - before.height)).toBeLessThanOrEqual(0.5)
})

test('a lone activity is direct and sources render as local compact pills', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  const requestedSourceHosts = []
  page.on('request', request => {
    const host = new URL(request.url()).hostname
    if (host === 'claude.ai' || host === 'docs.mobius.test') {
      requestedSourceHosts.push(host)
    }
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )
  const chat = await createTaggedChat(page, 'direct-activity-sources')
  const blocks = [
    {
      type: 'tool',
      tool: 'Bash',
      status: 'done',
      input: 'pwd',
      output: '/workspace/mobius',
      tool_use_id: 'single-tool-ui-contract',
      sources: [
        { title: 'Claude', url: 'https://claude.ai/research' },
        { title: 'Möbius docs', url: 'https://docs.mobius.test/activity' },
      ],
    },
    { type: 'text', content: 'One command should not create two disclosures.' },
  ]
  const messages = [
    { role: 'user', content: 'Run one command.', ts: 1700000000000 },
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

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.getByText('One command should not create two disclosures.')).toBeVisible()

  const tools = page.locator('.chat__tools')
  await expect(tools.locator('.chat__activity')).toHaveCount(0)
  await expect(tools.locator(':scope > .chat__tool')).toHaveCount(1)
  const toolToggle = tools.locator('.chat__tool-header')
  await expect(toolToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(tools.locator('.chat__chevron')).toHaveCount(0)
  await toolToggle.click()
  await expect(tools.getByText('/workspace/mobius')).toBeVisible()

  const sources = page.locator('.chat__sources')
  await expect(sources).toBeVisible()
  await expect(sources.getByRole('listitem')).toHaveCount(2)
  await expect(sources.locator('img')).toHaveCount(0)
  await expect(sources.locator('.chat__source-icon')).toHaveText(['C', 'D'])
  for (const chip of await sources.locator('.chat__source-chip').all()) {
    const box = await chip.boundingBox()
    expect(box).not.toBeNull()
    expect(box.width).toBeLessThan(270)
    expect(box.height).toBeGreaterThanOrEqual(43)
    expect(box.height).toBeLessThan(52)
  }
  expect(requestedSourceHosts).toEqual([])
})

test('activity stays nested and lazy, aborts on close, and copies exact tool output', async ({ page }) => {
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
  const thoughtPreview = 'THINKING_PREVIEW_SENTINEL: bounded reasoning preview'
  const thoughtText = `${thoughtPreview}\nTHINKING_FULL_SENTINEL: explicit full trace`
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
  let thinkingFullRequests = 0
  await page.route(new RegExp(`/api/chats/${chat.id}/thinking-trace/${thinkingId}`), route => {
    const preview = new URL(route.request().url()).searchParams.get('preview') === '1'
    if (!preview) {
      thinkingFullRequests += 1
      return route.fulfill({ status: 200, contentType: 'text/plain', body: thoughtText })
    }
    thinkingRequests += 1
    if (thinkingRequests === 3) {
      return route.fulfill({ status: 503, contentType: 'text/plain', body: 'offline' })
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/plain',
      headers: {
        'X-Thinking-Complete': '1',
        'X-Thinking-Preview-Complete': '0',
      },
      body: thoughtPreview,
    })
  })

  let toolPreviewRequests = 0
  let toolCopyRequests = 0
  await page.route(new RegExp(`/api/chats/${chat.id}/tool-output/${toolUseId}`), async route => {
    const preview = new URL(route.request().url()).searchParams.get('preview') === '1'
    if (!preview) {
      toolCopyRequests += 1
      return route.fulfill({ status: 200, contentType: 'text/plain', body: fullOutput })
    }
    toolPreviewRequests += 1
    if (toolPreviewRequests === 1) {
      // Keep the first preview pending long enough to exercise explicit copy
      // and abort-on-collapse. A second open receives its bounded preview.
      await new Promise(resolve => setTimeout(resolve, 1800))
    }
    if (toolPreviewRequests === 2) {
      return route.fulfill({
        status: 202,
        headers: { 'Retry-After': '0' },
        body: '',
      })
    }
    if (toolPreviewRequests === 4) {
      return route.fulfill({ status: 503, contentType: 'text/plain', body: 'offline' })
    }
    try {
      await route.fulfill({
        status: 200,
        contentType: 'text/plain',
        headers: {
          'X-Tool-Output-Complete': '1',
        },
        body: fullOutput,
      })
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
  const timeline = activity.locator('.chat__activity-timeline')
  await expect(timeline).toBeHidden()
  const timelineId = await activityHeader.getAttribute('aria-controls')
  await expect(activity.locator(`[id="${timelineId}"]`)).toHaveCount(1)
  await page.waitForTimeout(250)
  expect(thinkingRequests).toBe(0)
  expect(thinkingFullRequests).toBe(0)
  expect(toolPreviewRequests).toBe(0)
  expect(toolCopyRequests).toBe(0)

  await activityHeader.click()
  await expect(activityHeader).toHaveAttribute('aria-expanded', 'true')
  await expect(activityHeader).toHaveAttribute('aria-controls', /.+/)
  const thoughtToggle = activity.locator('.chat__activity-think-toggle')
  const toolToggle = activity.locator('.chat__tool-header')
  await expect(thoughtToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(thoughtToggle).toHaveAttribute('aria-controls', /.+/)
  await expect(toolToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(toolToggle).toHaveAttribute('aria-controls', /.+/)
  expect(thinkingRequests).toBe(0)
  expect(thinkingFullRequests).toBe(0)
  expect(toolPreviewRequests).toBe(0)
  expect(toolCopyRequests).toBe(0)
  for (const toggle of [thoughtToggle, toolToggle]) {
    const controlledId = await toggle.getAttribute('aria-controls')
    await expect(activity.locator(`[id="${controlledId}"]`)).toHaveCount(1)
    await expect(activity.locator(`[id="${controlledId}"]`)).toBeHidden()
  }

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
  await expect(page.getByText(thoughtPreview)).toBeVisible()
  expect(thinkingRequests).toBe(1)
  await expect(page.getByText('THINKING_FULL_SENTINEL', { exact: false })).toHaveCount(0)
  await page.getByRole('button', { name: 'Load full thought' }).click()
  await expect(page.getByText('THINKING_FULL_SENTINEL', { exact: false })).toBeVisible()
  expect(thinkingFullRequests).toBe(1)
  await thoughtToggle.click()
  await expect(page.getByText(thoughtPreview)).toHaveCount(0)
  await thoughtToggle.click()
  await expect(page.getByText(thoughtPreview)).toBeVisible()
  expect(thinkingRequests).toBe(2)
  await thoughtToggle.click()

  await toolToggle.click()
  await expect(activity.getByRole('region')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Copy output' })).toBeEnabled()
  await expect(page.getByText(/loading output preview/i)).toBeVisible()
  await page.getByRole('button', { name: 'Copy output' }).click()
  await expect.poll(() => page.evaluate(() => window.__copiedToolText.at(-1))).toBe(fullOutput)
  expect(toolCopyRequests).toBe(1)

  // Collapse before the delayed response lands: the browser request is really
  // aborted, not merely ignored, and reopening starts a fresh bounded fetch.
  await toolToggle.click()
  await expect.poll(() => page.evaluate(() => window.__lazyRequestAborts.tool)).toBe(1)
  await toolToggle.click()
  await expect(page.getByText(fullOutput)).toBeVisible()
  expect(toolPreviewRequests).toBe(3)
  await expect(page.getByRole('button', { name: 'Copy output' })).toBeVisible()
  await page.getByRole('button', { name: 'Copy output' }).click()
  await expect.poll(() => page.evaluate(() => window.__copiedToolText.at(-1))).toBe(fullOutput)
  expect(toolCopyRequests).toBe(1)

  // The complete bounded preview is now the exact copy source, avoiding a
  // duplicate request. Clipboard failures remain visible and retryable.
  await page.evaluate(() => { window.__clipboardShouldFail = true })
  await activity.locator('.chat__tool-copy').click()
  await expect(activity.getByText('Copy failed')).toBeVisible()
  expect(toolCopyRequests).toBe(1)
  await page.evaluate(() => { window.__clipboardShouldFail = false })
  await page.getByRole('button', { name: 'Could not copy output' }).click()
  await expect(page.getByRole('button', { name: 'Output copied' })).toBeVisible()
  await expect(activity.locator('.chat__tool-copy + [role="status"]')).toHaveText(
    'Output copied',
  )
  expect(toolCopyRequests).toBe(1)

  // A transient sidecar failure is announced and retries in place. It should
  // not require collapsing the detail or retaining another hidden payload.
  await toolToggle.click()
  await toolToggle.click()
  await expect(
    activity.locator('.chat__tool .chat__lazy-status [role="status"]'),
  ).toHaveText('Couldn’t load output preview.')
  await activity.locator('.chat__tool').getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText(fullOutput)).toBeVisible()
  expect(toolPreviewRequests).toBe(5)

  await thoughtToggle.click()
  await expect(
    activity.locator('.chat__activity-think .chat__lazy-status [role="status"]'),
  ).toHaveText('Thought unavailable.')
  await activity.locator('.chat__activity-think').getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText(thoughtPreview)).toBeVisible()
  expect(thinkingRequests).toBe(4)
  await thoughtToggle.click()

  // Closing the outer stretch unmounts every nested payload. Reopening restores
  // each deliberate disclosure state: the thought was closed, while the tool
  // was open. The restored visible tool starts one fresh bounded preview fetch;
  // its previously loaded payload was released rather than retained offscreen.
  await activityHeader.click()
  await expect(activity.locator('.chat__activity-timeline')).toBeHidden()
  await activityHeader.click()
  await expect(activity.locator('.chat__activity-timeline')).toBeVisible()
  await expect(activity.locator('.chat__activity-think-toggle')).toHaveAttribute('aria-expanded', 'false')
  await expect(activity.locator('.chat__tool-header')).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText(fullOutput)).toBeVisible()
  expect(thinkingRequests).toBe(4)
  expect(thinkingFullRequests).toBe(1)
  expect(toolPreviewRequests).toBe(6)
  expect(toolCopyRequests).toBe(1)
})
