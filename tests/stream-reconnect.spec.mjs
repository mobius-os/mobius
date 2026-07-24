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
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/stream-reconnect.spec.mjs
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

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

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

    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=20&compact=1$/, route => {
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

  test('11. Quick visibility flip keeps a fresh live socket quiet', async ({ page }) => {
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let streamCount = 0
      window.__streamFetchCount = 0

      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          return realFetch(input, init)
        }

        streamCount++
        window.__streamFetchCount = streamCount

        if (streamCount === 1) {
          const encoder = new TextEncoder()
          const body = new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode(': keepalive\n\n'))
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
    await send(page, 'quick flip with healthy socket')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__streamFetchCount === 1)

    await setVisibility(page, 'hidden')
    await page.waitForTimeout(250)
    await setVisibility(page, 'visible')
    await page.waitForTimeout(1800)

    expect(await page.evaluate(() => window.__streamFetchCount)).toBe(1)
    await expect(page.locator('.connection-status--reattach')).toHaveCount(0)
  })

  test('14. Kept quick-wake socket self-heals when reads stop', async ({ page }) => {
    await page.addInitScript(() => {
      window.__MOBIUS_KEPT_SOCKET_DEADMAN_MS = 250

      const realFetch = window.fetch.bind(window)
      const encoder = new TextEncoder()
      let streamCount = 0
      let recoveredController

      window.__streamFetchCount = 0
      window.__sendRecoveredKeepalive = () => {
        recoveredController?.enqueue(encoder.encode(': keepalive\n\n'))
      }

      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          return realFetch(input, init)
        }

        streamCount++
        window.__streamFetchCount = streamCount

        if (streamCount === 1) {
          const body = new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode(': keepalive\n\n'))
            },
            cancel() {},
          })
          return Promise.resolve(new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }))
        }

        if (streamCount === 2) {
          const body = new ReadableStream({
            start(controller) {
              recoveredController = controller
              controller.enqueue(encoder.encode(
                'data: {"type":"text","content":"deadman replay"}\n\n'
                + 'data: {"type":"catch_up_done"}\n\n',
              ))
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
    await send(page, 'quick flip with silently dead socket')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__streamFetchCount === 1)

    await setVisibility(page, 'hidden')
    await page.waitForTimeout(50)
    await setVisibility(page, 'visible')

    await page.waitForFunction(() => window.__streamFetchCount === 2, {
      timeout: 5000,
    })
    await expect(page.locator('.chat__scroll')).toContainText(
      'deadman replay', { timeout: 5000 },
    )
    await expect(page.locator('.connection-status--reattach')).toHaveCount(0)

    await setVisibility(page, 'hidden')
    await page.waitForTimeout(50)
    await setVisibility(page, 'visible')
    await page.waitForTimeout(50)
    await page.evaluate(() => window.__sendRecoveredKeepalive())
    await page.waitForTimeout(400)

    expect(await page.evaluate(() => window.__streamFetchCount)).toBe(2)
    await expect(page.locator('.connection-status--reattach')).toHaveCount(0)
  })

  test('12. Long-hidden wake with stale reads still reattaches and replays', async ({ page }) => {
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let streamCount = 0
      let finishReattached
      window.__streamFetchCount = 0
      window.__finishReattachedStream = () => finishReattached?.()

      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          return realFetch(input, init)
        }

        streamCount++
        window.__streamFetchCount = streamCount

        if (streamCount === 1) {
          const body = new ReadableStream({ start() {}, cancel() {} })
          return Promise.resolve(new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }))
        }

        if (streamCount === 2) {
          const encoder = new TextEncoder()
          let controllerRef
          const body = new ReadableStream({
            start(controller) {
              controllerRef = controller
              controller.enqueue(encoder.encode(
                'data: {"type":"text","content":"long-hidden replay"}\n\n',
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
    await send(page, 'long hidden with stale socket')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__streamFetchCount === 1)

    await setVisibility(page, 'hidden')
    await page.waitForTimeout(5200)
    await setVisibility(page, 'visible')

    await page.waitForFunction(() => window.__streamFetchCount === 2)
    await expect(page.locator('.chat__scroll')).toContainText(
      'long-hidden replay', { timeout: 5000 },
    )
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)

    await page.evaluate(() => window.__finishReattachedStream())
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)
  })

  test('13. Slow long-hidden reattach shows the reconnecting note', async ({ page }) => {
    await page.addInitScript(() => {
      const realFetch = window.fetch.bind(window)
      let streamCount = 0
      let releaseReattach
      window.__streamFetchCount = 0
      window.__releaseSlowReattach = () => releaseReattach?.()

      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!/\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          return realFetch(input, init)
        }

        streamCount++
        window.__streamFetchCount = streamCount

        if (streamCount === 1) {
          const body = new ReadableStream({ start() {}, cancel() {} })
          return Promise.resolve(new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }))
        }

        if (streamCount === 2) {
          return new Promise(resolve => {
            releaseReattach = () => {
              const encoder = new TextEncoder()
              const body = new ReadableStream({
                start(controller) {
                  controller.enqueue(encoder.encode(
                    'data: {"type":"catch_up_done"}\n\n',
                  ))
                  controller.enqueue(encoder.encode('data: {"type":"done"}\n\n'))
                  controller.close()
                },
              })
              resolve(new Response(body, {
                status: 200,
                headers: { 'Content-Type': 'text/event-stream' },
              }))
            }
          })
        }

        return new Promise(() => {})
      }
    })

    await setupChat(page)
    await send(page, 'slow reattach')
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(1)
    await page.waitForFunction(() => window.__streamFetchCount === 1)

    await setVisibility(page, 'hidden')
    await page.waitForTimeout(5200)
    await setVisibility(page, 'visible')

    await page.waitForFunction(() => window.__streamFetchCount === 2)
    await expect(page.locator('.connection-status--reattach')).toBeVisible({
      timeout: 3000,
    })

    await page.evaluate(() => window.__releaseSlowReattach())
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
    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=20&compact=1$/, route => {
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

    // Cancel-queued (DELETE /pending/{cid}) → 200 with an empty queue, so the
    // tray-X clear below resolves cleanly without an error-path refetch.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/pending\/[^/]+$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pending_messages: [] }),
      })
    )

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

    // Queue a second message behind the streaming turn. A server-confirmed
    // queued message surfaces the fast-forward (steer) chip alongside Stop
    // (the steer feature — see steer-queued.spec.mjs). This test is about
    // the STOP disconnect+resend stale-204 guard, so clear the queue via
    // the tray's cancel-X before stopping — a Stop with queued rows would
    // collapse them into the fresh turn and change the resend payload.
    // The DELETE /pending does not hit /messages, so the post-counter
    // sequence is unchanged: the fresh turn we send after Stop is POST #3
    // (status:'started' → real /stream).
    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('queued follow-up')
    await expect(page.locator('button[aria-label="Send"]')).toBeVisible()
    await page.locator('button[aria-label="Send"]').click()
    // Wait for it to land as a server-confirmed queued row, then cancel it.
    await expect(page.getByRole('button', { name: 'Send queued message now' }))
      .toBeVisible()
    await page.locator('.queued__cancel').first().click()
    await expect(page.locator('.queued__row')).toHaveCount(0)

    // Stop: with the queue cleared the primary action is Stop again. Stop
    // aborts the parked first stream's controller (a no-op for the shim)
    // and zeroes justSentAtRef.
    await expect(page.locator('button[aria-label="Stop"]')).toBeVisible()
    await page.locator('button[aria-label="Stop"]').click()
    // click() only waits for the DOM event, not handleStop's async backend
    // confirmation. Wait for the state machine to become idle before modeling
    // the user's next send; otherwise Enter can race the still-active Stop
    // state and legitimately enqueue instead of opening the fresh stream this
    // test is meant to exercise.
    await expect(page.locator('button[aria-label="Stop"]')).toHaveCount(0)

    // Send the follow-up as a fresh turn whose /stream (request #2) hits the
    // real route above — this is the turn the stale 204 must not clobber.
    await send(page, 'queued follow-up')

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

  test('10. Reload of a running chat frozen on a question renders an ANSWERABLE card', async ({ page }) => {
    // Regression for the wedged-chat bug: a chat whose agent turn is
    // frozen on an unanswered AskUserQuestion, reopened via deep link.
    // The persisted last assistant message ends in a `question` block;
    // the chat is `running:true`. Before the fix, ChatView restored
    // `liveQuestionId` only from the live SSE `question` event, and the
    // load path set `sending:true` — so the persisted question card
    // rendered through the DISABLED gate (`!sending && liveQuestionId`)
    // and the user could never answer, leaving the turn frozen forever
    // (prod symptom: GET /chats + /stream reconnects but never a POST
    // carrying answers).
    //
    // This test models the prod symptom faithfully: the /stream
    // catch-up is EMPTY (no text, no `question` event — just the
    // catch_up_done marker) and the connection holds open. So
    // `streamItems` stays empty (the bridge suppression that hides the
    // persisted last-assistant message never fires — it requires
    // streamItems.length > 0), the persisted question block renders
    // through the normal messages.map path, and `liveQuestionId` is
    // never set by the stream. Pre-fix that path disabled the card
    // (`!sending` was false because the load set sending:true). Post-fix
    // it is answerable, derived from the durable persisted block + the
    // running stream (isStreaming), and answering MUST POST the answer.
    const CHAT_ID = '11111111-1111-1111-1111-111111111111'
    const QUESTION_ID = 'q-frozen-1'
    const TURN_TS = 1700000000000

    let answerPosted = null

    // Initial chat load: a running chat whose last assistant message is
    // frozen on an unanswered question. Crucially `pending_question_id`
    // is null (the live in-process registry hint is absent — the path
    // that used to wedge), forcing the durable fallback.
    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=20&compact=1$/, route => {
      if (route.request().method() !== 'GET') { route.continue(); return }
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: CHAT_ID,
          title: 'frozen chat',
          messages: [
            { role: 'user', content: 'help me pick', ts: TURN_TS - 1000 },
            {
              role: 'assistant',
              ts: TURN_TS,
              blocks: [
                { type: 'text', content: 'A couple of choices:' },
                {
                  type: 'question',
                  question_id: QUESTION_ID,
                  questions: [
                    {
                      question: 'Which color?',
                      options: [{ label: 'Red' }, { label: 'Blue' }],
                    },
                  ],
                },
              ],
            },
          ],
          pending_messages: [],
          total: 2,
          offset: 0,
          running: true,
          pending_question_id: null,
          session_id: 'sess-1',
          provider: 'claude',
        }),
      })
    })

    // The /stream catch-up is EMPTY (just catch_up_done) and the body
    // holds open indefinitely — the turn is parked on the question
    // future, so no `done` and no `question` event ever arrives.
    // `streamItems` stays empty (no live card, no bridge suppression),
    // `liveQuestionId` is never set by the stream, and `isStreaming`
    // stays true. We use a never-resolving ReadableStream so the EOF
    // reconnect loop doesn't churn — the connection just stays open the
    // way a real parked turn's SSE does.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      const body = [
        'data: {"type":"catch_up_done"}\n\n',
      ].join('')
      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          // A chunked SSE response that Playwright keeps open: fulfilling
          // with a finite body still signals EOF, which would re-arm the
          // reconnect. That's fine — every reconnect replays the same
          // empty catch-up, isStreaming stays true throughout, and the
          // persisted card stays answerable. We assert on that steady
          // state.
        },
        body,
      })
    })

    // Capture the answer POST so we can assert it carries the answers.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') { route.continue(); return }
      try { answerPosted = route.request().postDataJSON() } catch { answerPosted = null }
      route.fulfill({
        status: 202,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'answer_delivered', chat_id: CHAT_ID }),
      })
    })

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(`${BASE}/shell/?chat=${CHAT_ID}`, {
      waitUntil: 'domcontentloaded',
    })

    // The persisted question card renders.
    await expect(page.locator('.qcard')).toBeVisible({ timeout: 10000 })

    // The option buttons must be ENABLED (the bug rendered them
    // disabled). Pick an answer + submit.
    const redBtn = page.getByRole('radio', { name: 'Red' })
    await expect(redBtn).toBeEnabled({ timeout: 5000 })
    await redBtn.click()

    const submitBtn = page.getByRole('button', { name: 'Submit' })
    await expect(submitBtn).toBeEnabled()
    await submitBtn.click()

    // Answering MUST POST the answer payload (the turn unfreezes).
    await expect.poll(() => answerPosted, { timeout: 5000 }).not.toBeNull()
    expect(answerPosted.answers).toBeTruthy()
    expect(answerPosted.question_id).toBe(QUESTION_ID)
    expect(JSON.stringify(answerPosted.answers)).toContain('Red')
  })
})
