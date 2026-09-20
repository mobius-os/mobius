/**
 * Stop must clear its pending-queue ref before awaiting the backend. A terminal
 * SSE event during that await must not refetch and resurrect the old queue;
 * after confirmation, the captured owner message starts exactly one new turn.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
attachCleanup()

// API/stream mocks must remain the owner throughout this test.
test.use({ serviceWorkers: 'block' })

test('Stop clears the queue before terminal SSE reconciliation and resends it once', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'stop-sync-ordering')
  const path = `/api/chats/${chat.id}`
  const runtime = {
    runtime_revision: 0,
    running: false,
    run_id: null,
    run_status: null,
    pending_messages: [],
  }
  let messages = []
  let stopHits = 0
  let steerHits = 0
  let compactReads = 0
  let stopRefetchHits = 0
  let stopPending = false
  let staleDetail = null
  const sends = []
  let releaseStop
  const stopGate = new Promise(resolve => { releaseStop = resolve })
  let releaseSteer
  const steerGate = new Promise(resolve => { releaseSteer = resolve })

  await page.route(`${BASE}${path}/runtime`, route => route.fulfill({
    status: 200, contentType: 'application/json', json: runtime,
  }))
  await page.route(new RegExp(`${path}\\?`), async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const url = new URL(route.request().url())
    const compact = url.searchParams.get('limit') === '20'
      && url.searchParams.get('compact') === '1'
    if (compact) compactReads++
    if (compact && stopPending) stopRefetchHits++
    // Preserve identity and explicit model settings from the real fixture;
    // the mocked turn owns both transcript and runtime projections.
    const response = await route.fetch()
    const detail = await response.json()
    return route.fulfill({
      response,
      json: {
        ...detail, ...runtime, messages, offset: 0,
        ...(compact && stopPending ? staleDetail : {}),
      },
    })
  })
  await page.route(`${BASE}${path}/messages`, async route => {
    const body = route.request().postDataJSON()
    if (body.force_steer) {
      steerHits++
      await steerGate
      return route.fulfill({
        status: 200, contentType: 'application/json', json: { status: 'not_steered' },
      })
    }
    sends.push(body)
    const message = { role: 'user', content: body.content, ts: Date.now(), cid: body.cid }
    runtime.runtime_revision++
    if (sends.length === 2) {
      runtime.pending_messages = [message]
      return route.fulfill({
        status: 202, contentType: 'application/json',
        json: { status: 'queued', ts: message.ts, position: 1, pending_message: message },
      })
    }
    runtime.running = true
    runtime.run_id = `fixture-run-${sends.length}`
    runtime.run_status = 'running'
    runtime.pending_messages = []
    messages = [...messages, message]
    return route.fulfill({
      status: 202, contentType: 'application/json', json: { status: 'started' },
    })
  })
  await page.addInitScript(streamPath => {
    const nativeFetch = window.fetch.bind(window)
    window.__stopFixtureStreams = 0
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input?.url
      if (!String(url).endsWith(streamPath)) return nativeFetch(input, init)
      window.__stopFixtureStreams++
      const encoder = new TextEncoder()
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(
            'data: {"type":"text","content":"streaming response"}\n\n'
            + 'data: {"type":"catch_up_done"}\n\n',
          ))
          window.__finishStopFixtureStream = () => {
            controller.enqueue(encoder.encode('data: {"type":"done"}\n\n'))
            controller.close()
          }
        },
      })
      return Promise.resolve(new Response(stream, {
        status: 200, headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  }, `${path}/stream`)
  await page.route('**/api/chat/stop', async route => {
    stopHits++
    stopPending = true
    staleDetail = { ...runtime, pending_messages: [...runtime.pending_messages], messages }
    const cleared = runtime.pending_messages.map(message => message.cid)
    runtime.runtime_revision++
    runtime.running = false
    runtime.run_status = 'completed'
    runtime.pending_messages = []
    await stopGate
    stopPending = false
    return route.fulfill({
      status: 200, contentType: 'application/json',
      json: { stopped: true, cleared_pending_cids: cleared },
    })
  })

  await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(input).toBeEnabled()
  // A positive owning-resource check: a renamed query cannot leave the trap
  // silently unused, as the former ?limit=1 route did.
  await expect.poll(() => compactReads).toBeGreaterThan(0)
  await input.fill('first message')
  await input.press('Enter')
  await expect(surface.locator('.chat__stop')).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.__stopFixtureStreams)).toBe(1)
  await expect(surface.locator('.chat__cursor')).toBeVisible()
  await input.fill('queued message')
  await input.press('Enter')
  await expect(surface.locator('.queued__text')).toContainText('queued message')

  // Steer temporarily reserves the confirmed row, making Stop reachable.
  // Stop waits for that exact request before it snapshots the restored queue.
  await surface.locator('.chat__steer').click()
  await expect.poll(() => steerHits).toBe(1)
  await surface.locator('.chat__stop').click()
  releaseSteer()
  await expect.poll(() => stopHits).toBe(1)
  await expect(surface.locator('.queued__row')).toHaveCount(0)
  try {
    await page.evaluate(() => window.__finishStopFixtureStream())
    // Observe terminal stream presentation while the Stop response is still
    // held, rather than assuming a 250ms backend delay overlapped the test.
    await expect(surface.locator('.chat__cursor')).toHaveCount(0)
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(
      () => requestAnimationFrame(resolve),
    )))
    expect(stopPending).toBe(true)
    expect(stopRefetchHits, 'terminal reconciliation must see the synchronously cleared queue').toBe(0)
    await expect(surface.locator('.queued__row')).toHaveCount(0)
  } finally {
    releaseStop()
  }
  await expect.poll(() => sends.map(body => body.content)).toEqual([
    'first message', 'queued message', 'queued message',
  ])
  await expect.poll(() => page.evaluate(() => window.__stopFixtureStreams)).toBe(2)
  await expect(surface.locator('.chat__stop')).toBeVisible()
  expect(stopHits).toBe(1)
})
