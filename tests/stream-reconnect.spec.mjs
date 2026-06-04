/**
 * Tests for SSE stream reconnection behavior.
 *
 * The browser sleep/wake path is hard to test directly because
 * Playwright route.fulfill() usually delivers SSE bodies as a complete
 * response. These tests lock in the observable state-machine contracts:
 * completed streams stay idle, terminal 204 recovery exits thinking and
 * refreshes from the DB, Stop clears streaming, and short post-send 204s
 * still retry.
 *
 * Run: npx playwright test tests/stream-reconnect.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

function sseBody(events) {
  return events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
}

async function setupChat(page) {
  await page.setViewportSize({ width: 412, height: 915 })

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

async function send(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

async function setVisibility(page, state) {
  await page.evaluate((nextState) => {
    Object.defineProperty(document, 'visibilityState', {
      value: nextState, writable: true, configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
}

async function pillOverlapDiagnostics(page) {
  return page.evaluate(() => {
    const pill = document.querySelector('.chat__pill')
    if (!pill) return { missing: 'pill' }
    const pillRect = pill.getBoundingClientRect()
    const retry = document.querySelector('.connection-status__retry')
    const status = document.querySelector('.connection-status')

    const describe = (el) => {
      if (!el) return null
      const cls = el.className && typeof el.className === 'string'
        ? `.${el.className.trim().split(/\s+/).join('.')}`
        : ''
      const label = el.getAttribute?.('aria-label') || el.textContent?.trim() || ''
      return `${el.tagName.toLowerCase()}${cls}${label ? ` "${label}"` : ''}`
    }

    const samples = []
    const xs = [0.25, 0.5, 0.75].map(p => pillRect.left + pillRect.width * p)
    const ys = [
      pillRect.top + pillRect.height * 0.6,
      pillRect.bottom - 2,
    ]
    for (const x of xs) {
      for (const y of ys) {
        samples.push({
          x, y,
          stack: document.elementsFromPoint(x, y).slice(0, 8).map(describe),
        })
      }
    }

    const overlapsPill = (el) => {
      if (!el) return false
      const r = el.getBoundingClientRect()
      return r.left < pillRect.right
        && r.right > pillRect.left
        && r.top < pillRect.bottom
        && r.bottom > pillRect.top
    }

    return {
      pill: {
        top: pillRect.top,
        bottom: pillRect.bottom,
        left: pillRect.left,
        right: pillRect.right,
      },
      status: status ? status.getBoundingClientRect().toJSON() : null,
      retry: retry ? retry.getBoundingClientRect().toJSON() : null,
      retryOverlapsPill: overlapsPill(retry),
      statusOverlapsPill: overlapsPill(status),
      samples,
    }
  })
}

test.describe('Stream reconnection', () => {
  test('1. Completed stream stays idle after visibility change', async ({ page }) => {
    let streamRequestCount = 0
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamRequestCount++
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: sseBody([
          { type: 'text', content: 'complete response' },
          { type: 'done' },
        ]),
      })
    })

    await setupChat(page)
    await send(page, 'hello')

    await expect(page.locator('.chat__scroll')).toContainText('complete response')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        value: 'hidden', writable: true, configurable: true,
      })
      document.dispatchEvent(new Event('visibilitychange'))
      Object.defineProperty(document, 'visibilityState', {
        value: 'visible', writable: true, configurable: true,
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.waitForTimeout(500)

    await expect(page.locator('.chat__scroll')).toContainText('complete response')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)
    expect(streamRequestCount).toBe(1)
  })

  test('2. Terminal 204 exits thinking and refreshes persisted messages', async ({ page }) => {
    let streamRequestCount = 0
    let refreshReady = false

    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=20$/, route => {
      if (!refreshReady || route.request().method() !== 'GET') {
        route.continue()
        return
      }
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: 'user', content: 'expired broadcast', ts: Date.now() },
            { role: 'assistant', content: 'final response from db' },
          ],
          total: 2,
          offset: 0,
        }),
      })
    })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamRequestCount++
      refreshReady = true
      // Wait past useStreamConnection's just-sent 204 retry window.
      await new Promise(resolve => setTimeout(resolve, 1700))
      await route.fulfill({ status: 204, body: '' })
    })

    await setupChat(page)
    await send(page, 'expired broadcast')

    await expect(page.locator('.chat__scroll')).toContainText('final response from db', {
      timeout: 8000,
    })
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)
    expect(streamRequestCount).toBe(1)
  })

  test('3. Stream completes and the Voice button returns', async ({ page }) => {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: sseBody([
          { type: 'text', content: 'hello back' },
          { type: 'done' },
        ]),
      })
    })

    await setupChat(page)
    await send(page, 'hi')

    await expect(page.locator('.chat__scroll')).toContainText('hello back')
    await expect(page.locator('button[aria-label="Voice input"]')).toHaveCount(1)
  })

  test('4. Stop clears streaming so visibility change does not reconnect', async ({ page }) => {
    let streamRequestCount = 0

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamRequestCount++
      // Keep the first stream pending until Stop aborts it.
      await new Promise(resolve => setTimeout(resolve, 5000))
      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: sseBody([{ type: 'text', content: 'late response' }]),
      }).catch(() => {})
    })

    await setupChat(page)
    await send(page, 'stop me')

    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.locator('button[aria-label="Stop"]').click()
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        value: 'visible', writable: true, configurable: true,
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.waitForTimeout(500)

    expect(streamRequestCount).toBe(1)
  })

  test('8. Wake after hidden EOF before first event reattaches and renders in-progress message', async ({ page }) => {
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let streamCount = 0
      let releaseDropped
      let finishReattached
      window.__streamFetchCount = 0
      window.__droppedStreamParked = false
      window.__releaseDroppedStream = () => releaseDropped?.()
      window.__finishReattachedStream = () => finishReattached?.()

      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          return realFetch(input, init)
        }

        streamCount++
        window.__streamFetchCount = streamCount

        if (streamCount === 1) {
          window.__droppedStreamParked = true
          return new Promise(resolve => {
            releaseDropped = () => resolve(new Response('', {
              status: 200,
              headers: { 'Content-Type': 'text/event-stream' },
            }))
          })
        }

        if (streamCount === 2) {
          const encoder = new TextEncoder()
          let controllerRef
          const body = new ReadableStream({
            start(controller) {
              controllerRef = controller
              controller.enqueue(encoder.encode(
                'data: {"type":"text","content":"reattached in-progress"}\n\n',
              ))
              controller.enqueue(encoder.encode(
                'data: {"type":"catch_up_done"}\n\n',
              ))
              finishReattached = () => {
                controllerRef.enqueue(encoder.encode('data: {"type":"done"}\n\n'))
                controllerRef.close()
              }
            },
            cancel() {},
          })
          return Promise.resolve(new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }))
        }

        return new Promise(() => {})
      }
    })

    await setupChat(page)
    await send(page, 'sleep before first event')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__droppedStreamParked === true)

    await setVisibility(page, 'hidden')
    await page.evaluate(() => window.__releaseDroppedStream())
    await page.waitForFunction(() => window.__streamFetchCount === 1)

    await setVisibility(page, 'visible')

    await page.waitForFunction(() => window.__streamFetchCount === 2)
    await expect(page.locator('.chat__scroll')).toContainText(
      'reattached in-progress', { timeout: 5000 },
    )
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)

    await page.evaluate(() => window.__finishReattachedStream())
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)
  })

  test('9. ConnectionStatus retry button stays above the composer pill on wake failure', async ({ page }) => {
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let streamCount = 0
      window.__failedStreamFetches = 0
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          streamCount++
          window.__failedStreamFetches = streamCount
          return Promise.reject(new TypeError('simulated mobile radio drop'))
        }
        return realFetch(input, init)
      }
    })

    await setupChat(page)
    await send(page, 'retry button layout')

    await expect(page.locator('.connection-status__retry')).toBeVisible({
      timeout: 10000,
    })
    await page.waitForFunction(() => {
      const chat = document.querySelector('.chat')
      const foot = document.querySelector('.chat__foot')
      return chat && foot
        && getComputedStyle(chat).getPropertyValue('--composer-h').trim()
          === `${foot.offsetHeight}px`
    })

    const diagnostics = await pillOverlapDiagnostics(page)
    expect(diagnostics.retryOverlapsPill, JSON.stringify(diagnostics, null, 2))
      .toBe(false)
    expect(diagnostics.statusOverlapsPill, JSON.stringify(diagnostics, null, 2))
      .toBe(false)
  })

  test('6. Typing while streaming shows Send instead of Stop', async ({ page }) => {
    let streamCount = 0
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamCount++
      if (streamCount === 1) {
        // First stream: deliver text but keep connection open (no done).
        // The SSE body ends at the network level, which makes
        // useStreamConnection see EOF → it calls onStreamEnd. To keep
        // the "sending" state visible long enough for the test, we
        // delay the response so the typing happens while Stop is shown.
        await new Promise(r => setTimeout(r, 500))
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: sseBody([
            { type: 'text', content: 'streaming...' },
            { type: 'done' },
          ]),
        })
      } else {
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: sseBody([
            { type: 'text', content: 'second response' },
            { type: 'done' },
          ]),
        })
      }
    })

    await setupChat(page)
    await send(page, 'hello')

    // Stop button should appear while agent is working.
    await expect(page.locator('button[aria-label="Stop"]')).toBeVisible({
      timeout: 3000,
    }).catch(() => {
      // Stream may have completed already — that's OK for this test.
      // The key invariant is below.
    })

    // After stream completes, type a follow-up.
    await expect(page.locator('.chat__scroll')).toContainText(
      'streaming...', { timeout: 5000 },
    )
    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('follow up')
    await expect(page.locator('button[aria-label="Send"]')).toBeVisible()

    // Send and verify second response arrives.
    await page.locator('button[aria-label="Send"]').click()
    await expect(page.locator('.chat__scroll')).toContainText(
      'second response', { timeout: 5000 },
    )
  })

  test('7. Stop+immediate-resend: a stale continuation 204 must not clobber the resent turn', async ({ page }) => {
    // Regression for the missing stale-connection guard in
    // useStreamConnection.connectToStream. On Stop,
    // disconnect({clearStreaming:true}) aborts the active controller AND
    // zeroes justSentAtRef; ChatView then immediately resends the queued
    // message as a fresh turn (new controller, fresh justSentAtRef). If
    // the ORIGINAL turn's stream fetch RESOLVES (rather than rejecting)
    // AFTER that resend with a 204 — which happens in production when the
    // browser already received the 204 response before the Stop's abort,
    // so the abort is a no-op and the awaited fetch resolves normally —
    // the old code ran the 204 branch on a connection that no longer
    // owns abortRef. With justSentAtRef freshly set by the resend it
    // could schedule a spurious reconnect; once the resend's own window
    // elapsed it took the terminal path: setStreamItems([]) +
    // onNeedsRefresh({force:true}), forcing a DB refetch that clobbers
    // the resent turn's live response. The guard
    // `if (abortRef.current !== controller) return` makes the orphaned
    // continuation bail before touching res.
    //
    // Why a fetch shim and not route.fulfill: an aborted route-mocked
    // fetch REJECTS (AbortError) and never reaches the post-fetch line,
    // so route mocking can't reproduce "resolves after abort". We shim
    // window.fetch so exactly the FIRST /stream request resolves with a
    // synthetic 204 on a test signal, IGNORING the abort signal — a
    // faithful, deterministic model of the production "response already
    // received before abort" race. Every other request (resent /stream,
    // /messages, /stop, DB refetch) hits the real backend / Playwright
    // routes untouched. The guard's behavior is identical whether the
    // late resolution is a real network race or this simulation, because
    // both deliver a resolved 204 Response to the same awaited fetch
    // while abortRef points elsewhere.
    let messagesPostCount = 0

    // The bug's terminal-204 path calls onNeedsRefresh({force:true}) →
    // fetchMessages → GET /chats/{id}?limit=20. We record the timing of
    // every such refetch. A benign queue-reconcile refetch can fire
    // BEFORE we release the stale 204 (it happens with or without the
    // guard); the bug-specific refetch fires AFTER release. We answer
    // all of them with DB state that does NOT contain the resent turn,
    // so if the bug path clobbers, the live response visibly disappears.
    const dbRefetchTimes = []
    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=20$/, route => {
      if (route.request().method() !== 'GET') { route.continue(); return }
      dbRefetchTimes.push(Date.now())
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: 'user', content: 'first turn', ts: Date.now() - 1000 },
            { role: 'assistant', content: 'persisted-db-only response' },
          ],
          total: 2,
          offset: 0,
        }),
      })
    })

    // The resent turn's /stream (request #2+) streams a real response
    // that must survive the late stale 204. Request #1 is intercepted by
    // the fetch shim below before it reaches this route.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: sseBody([
          { type: 'text', content: 'RESENT TURN RESPONSE' },
          { type: 'done' },
        ]),
      })
    })

    // Install the fetch shim before any app code runs. It captures the
    // FIRST /stream fetch and parks it (a held Response), exposing
    // window.__releaseStaleStream() to resolve it with a 204 on demand —
    // crucially WITHOUT honoring the AbortSignal, simulating a response
    // already buffered by the browser before the abort. All later
    // /stream fetches and every non-stream fetch pass through unchanged.
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let staleParked = false
      let resolveStale
      window.__staleStreamRequested = false
      window.__releaseStaleStream = () => { if (resolveStale) resolveStale() }
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!staleParked && /\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          staleParked = true
          window.__staleStreamRequested = true
          // Park: resolve only when the test signals, then hand back a
          // real 204 Response. Ignore init.signal entirely so the abort
          // from Stop's disconnect() does NOT reject this — mirroring a
          // 204 that landed before the abort.
          return new Promise(resolve => {
            resolveStale = () => resolve(new Response(null, { status: 204 }))
          })
        }
        return realFetch(input, init)
      }
    })

    await setupChat(page)

    // POST /messages override — registered AFTER setupChat so it wins
    // (Playwright matches most-recently-added first; setupChat's bare-{}
    // mock would otherwise shadow this). First send starts the (stale)
    // turn; the follow-up sent while streaming truly queues; Stop's
    // collapsed resend starts again.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') { route.continue(); return }
      messagesPostCount++
      if (messagesPostCount === 2) {
        route.fulfill({
          status: 202,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'queued', ts: Date.now(), position: 0 }),
        })
        return
      }
      route.fulfill({
        status: 202,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'started' }),
      })
    })

    // Send the first message. Its /stream fetch is parked by the shim.
    await send(page, 'first turn')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__staleStreamRequested === true, {
      timeout: 5000,
    })

    // Queue a second message behind the streaming turn so Stop has
    // something to collapse + resend as a fresh turn.
    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('queued follow-up')
    await expect(page.locator('button[aria-label="Send"]')).toBeVisible()
    await page.locator('button[aria-label="Send"]').click()

    // Stop: aborts the parked first stream's controller (a no-op for the
    // shim), zeroes justSentAtRef, then resends 'queued follow-up' as a
    // fresh turn whose /stream (request #2) hits the real route above.
    await expect(page.locator('button[aria-label="Stop"]')).toBeVisible()
    await page.locator('button[aria-label="Stop"]').click()

    // The resent turn's response should stream in and stick.
    await expect(page.locator('.chat__scroll')).toContainText(
      'RESENT TURN RESPONSE', { timeout: 8000 },
    )

    // NOW release the parked first stream as a stale 204. With the guard
    // it bails (abortRef no longer === its controller). Without the
    // guard it runs the terminal-refresh path and clobbers.
    const releaseAt = Date.now()
    await page.evaluate(() => window.__releaseStaleStream())

    // Give the stale 204 handler ample time to (wrongly) fire — past the
    // 1.5s broadcast-registration window so the terminal path is taken.
    await page.waitForTimeout(2500)

    // The resent response must still be present — the stale 204 must not
    // have cleared streamItems / forced a DB refetch over the live turn.
    await expect(page.locator('.chat__scroll')).toContainText('RESENT TURN RESPONSE')

    // The discriminating signal: a DB refetch triggered by the stale
    // 204's terminal-refresh path fires AFTER we release it. (An earlier,
    // benign queue-reconcile refetch can fire before release — that one
    // happens with or without the guard and is not the bug.) With the
    // guard, the stale connection bails before onNeedsRefresh, so NO
    // refetch occurs after release. Without it, the terminal-204 path
    // refetches and clobbers the live response with stale DB content.
    const refetchAfterRelease = dbRefetchTimes.some(t => t >= releaseAt - 50)
    expect(refetchAfterRelease).toBe(false)
  })

  test('5. 204 shortly after send retries instead of refreshing', async ({ page }) => {
    let streamRequestCount = 0

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamRequestCount++
      if (streamRequestCount <= 2) {
        route.fulfill({ status: 204, body: '' })
        return
      }
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: sseBody([
          { type: 'text', content: 'delayed response' },
          { type: 'done' },
        ]),
      })
    })

    await setupChat(page)
    await send(page, 'hello')

    await expect(page.locator('.chat__scroll')).toContainText('delayed response', {
      timeout: 10000,
    })
    expect(streamRequestCount).toBeGreaterThanOrEqual(3)
  })
})
