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
import { streamSnapshotKey } from '../frontend/src/components/ChatView/streamSnapshotCache.js'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { createMockChatRuntime } from './_mockChatRuntime.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const FIXTURE_RUNTIME_REVISION = 1_000_000

attachCleanup()

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
    undefined, { timeout: 10000 }
  )
  const chat = await createTaggedChat(page, 'stream-reconnect')
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await page.waitForFunction(
    () => !!document.querySelector('[data-chat-surface="painted"] .chat__form'),
    undefined, { timeout: 10000 },
  )
  return chat
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
    const pill = document.querySelector('[data-chat-surface="painted"] .chat__pill')
    if (!pill) return { missing: 'pill' }
    const pillRect = pill.getBoundingClientRect()
    const retry = document.querySelector('[data-chat-surface="painted"] .connection-status__retry')
    const status = document.querySelector('[data-chat-surface="painted"] .connection-status')

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










  test('13. Slow long-hidden wake reattaches and delivers the resumed stream', async ({ page }) => {
    const resumedMarker = 'resumed after a long hidden wake'
    let streamRequestCount = 0
    let dropFirstStream
    const firstStreamDropped = new Promise(resolve => { dropFirstStream = resolve })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamRequestCount += 1
      if (streamRequestCount === 1) {
        await firstStreamDropped
        await route.abort('connectionreset').catch(() => {})
        return
      }
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: [
          `data: ${JSON.stringify({ type: 'text', content: resumedMarker })}\n\n`,
          'data: {"type":"catch_up_done"}\n\n',
          'data: {"type":"done"}\n\n',
        ].join(''),
      })
    })

    await setupChat(page)
    await send(page, 'slow reattach')
    await expect(page.locator('[data-chat-surface="painted"] button[aria-label="Stop"]')).toHaveCount(1)
    await expect.poll(() => streamRequestCount).toBe(1)

    await setVisibility(page, 'hidden')
    // Stay comfortably beyond the product's 5s quick-wake boundary. Hosted
    // timer scheduling can otherwise land exactly on that boundary and turn
    // this deliberate long-wake case into a quick-wake case.
    await page.waitForTimeout(6000)
    dropFirstStream()
    await setVisibility(page, 'visible')

    await expect.poll(() => streamRequestCount, { timeout: 5000 }).toBe(2)
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toContainText(resumedMarker, { timeout: 5000 })
    await expect(page.locator('[data-chat-surface="painted"] button[aria-label="Stop"]')).toHaveCount(0)
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

    await expect(page.locator('[data-chat-surface="painted"] .connection-status__retry')).toBeVisible({
      timeout: 10000,
    })
    await page.waitForFunction(() => {
      const chat = document.querySelector('[data-chat-surface="painted"] .chat')
      const foot = document.querySelector('[data-chat-surface="painted"] .chat__foot')
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
    // This test also carries the exact later regression: sessionStorage has a
    // source-rich pre-question stream prefix. Raw surface scoring used to let
    // that regenerable prefix hide the durable row, leaving no card or nudge
    // while the durable pending-question marker still blocked every send.
    const CHAT_ID = '11111111-1111-1111-1111-111111111111'
    const QUESTION_ID = 'q-frozen-1'
    const TURN_TS = 1700000000000
    const GOAL = 'Keep this question stable'

    let answerPosted = null
    let streamRequestCount = 0

    // Initial chat load: a running chat whose last assistant message is
    // frozen on an unanswered question. The durable marker was restored by
    // QuestionCommit (or the one-time migration for an in-flight old turn),
    // so the card stays answerable without an SSE question replay.
    const updatedAt = '2026-07-30T18:00:00'
    const staleStreamSnapshot = [
      { type: 'text', content: 'A couple of choices:' },
      {
        type: 'tool',
        tool: 'Bash',
        status: 'done',
        input: 'inspect the full pre-question state',
        output: 'source-rich output that is still only an older prefix',
      },
    ]
    const runtime = createMockChatRuntime({
      runtime_revision: FIXTURE_RUNTIME_REVISION,
      running: true,
      active_goal_objective: GOAL,
      pending_messages: [],
      pending_question_id: QUESTION_ID,
      updated_at: updatedAt,
    })
    const detail = runtime.detail({
      id: CHAT_ID,
      title: 'frozen chat',
      messages: [
        { role: 'user', content: `/goal ${GOAL}`, ts: TURN_TS - 1000 },
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
      total: 2,
      offset: 0,
      session_id: 'sess-1',
      provider: 'claude',
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=(?:1|20&compact=1)$/, route => {
      if (route.request().method() !== 'GET') { route.continue(); return }
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(detail),
      })
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/runtime$/, route => {
      if (route.request().method() !== 'GET') { route.continue(); return }
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runtime.snapshot()),
      })
    })

    // A parked question intentionally skips SSE attachment until its answer.
    // The first post-answer catch-up is empty (just catch_up_done), then later
    // reattachments fail so this test still reaches retry exhaustion without
    // inventing a second question event.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamRequestCount++
      if (streamRequestCount > 1) {
        await route.fulfill({ status: 503, body: 'temporary stream failure' })
        return
      }
      const body = [
        'data: {"type":"catch_up_done"}\n\n',
      ].join('')
      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          // The finite body signals EOF and re-arms reconnect; subsequent
          // requests deliberately fail above to exercise ambiguous loss.
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
        body: JSON.stringify({
          status: 'answer_delivered',
          answer_turn: 'same',
          chat_id: CHAT_ID,
        }),
      })
    })

    await page.addInitScript(({ key, items }) => {
      sessionStorage.setItem(key, JSON.stringify(items))
    }, {
      key: streamSnapshotKey(CHAT_ID),
      items: staleStreamSnapshot,
    })

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(`${BASE}/shell/?chat=${CHAT_ID}`, {
      waitUntil: 'domcontentloaded',
    })

    // The persisted question card renders.
    const questionCard = page.locator('[data-chat-surface="painted"] .qcard')
    await expect(questionCard).toHaveCount(1, { timeout: 10000 })
    await expect(questionCard).toBeVisible()
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toContainText('A couple of choices:')
    const goalRail = page.getByRole('group', { name: 'Goal progress' })
    await expect(goalRail).toContainText(`Goal · ${GOAL}`)

    // A preserved draft remains editable, but the question barrier owns the
    // action slot: offer Stop rather than a Send that can only receive 409.
    const activeComposer = page.locator('[data-chat-surface="painted"]')
      .getByLabel('Message Möbius…')
    await activeComposer.fill('keep this draft safe')
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Send' })).toHaveCount(0)

    // The option buttons must be ENABLED (the bug rendered them
    // disabled). Pick an answer + submit.
    const redBtn = page.getByRole('radio', { name: 'Red' })
    await expect(redBtn).toBeEnabled({ timeout: 5000 })
    await redBtn.click()

    const submitBtn = page.getByRole('button', { name: 'Submit' })
    await expect(submitBtn).toBeEnabled()
    await submitBtn.click()

    // The answer stays inside this same durable goal run. Even after this
    // browser exhausts its reconnects, the connection warning must not retire
    // the goal while the authoritative runtime still reports `running:true`.
    await expect(goalRail).toContainText(`Goal · ${GOAL}`)
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible({ timeout: 12000 })
    await expect(goalRail).toContainText(`Goal · ${GOAL}`)

    // Answering MUST POST the answer payload (the turn unfreezes).
    await expect.poll(() => answerPosted, { timeout: 5000 }).not.toBeNull()
    expect(answerPosted.answers).toBeTruthy()
    expect(answerPosted.question_id).toBe(QUESTION_ID)
    expect(JSON.stringify(answerPosted.answers)).toContain('Red')
    await expect(questionCard).toHaveCount(1)
    await expect(questionCard.locator('.qcard__submit')).toHaveText('Submitted')
    await expect(activeComposer)
      .toHaveValue('keep this draft safe')
    // The answer unfreezes the turn, but the authoritative runtime above still
    // reports `running:true`; reconnect loss must not invent an idle composer.
    // Keep the draft safe behind Stop until a later runtime snapshot settles.
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Send' })).toHaveCount(0)
  })
})
