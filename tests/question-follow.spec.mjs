/**
 * Browser contract for in-process Q&A continuation: a card that was already
 * following may resume follow only when its post-answer response begins. The
 * temporary submit anchor holds through answer acceptance, so blank tail room
 * cannot move the card before the continuation actually renders.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { testChatAgentSettings } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

async function installQuestionStream(page, questionBlock) {
  await page.addInitScript(({ question, prefix, suffix }) => {
    const realFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) {
        return realFetch(input, init)
      }
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          const emit = event => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify(event)}\n\n`,
          ))
          setTimeout(() => emit({
            type: 'stream_snapshot',
            items: [
              { type: 'text', content: prefix },
              {
                type: 'question',
                question_id: question.question_id,
                questions: question.questions,
              },
            ],
          }), 0)
          setTimeout(() => emit({ type: 'catch_up_done' }), 15)
          setTimeout(() => emit(question), 30)
          window.__continueQuestionStream = () => {
            // The test controls this boundary explicitly: acceptance and the
            // first renderable response are separate events.
            setTimeout(() => emit({ type: 'text_boundary' }), 80)
            setTimeout(() => emit({
              type: 'text_final',
              text_item_id: 'after-question',
              content: suffix,
            }), 110)
          }
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  }, {
    question: questionBlock,
    // Keep meaningful reserved tail beneath the card. The regression only
    // appears when an acceptance-time FOLLOW handoff can consume that blank
    // room before any continuation is visible.
    prefix: `${'Question follow line.\n\n'.repeat(2)}READY_FOR_QUESTION`,
    suffix: `${'Continued answer line.\n\n'.repeat(24)}AFTER_QUESTION_END`,
  })
}

const questionFollowScenarios = [
  {
    name: 'accepted same-turn answer waits for response activity before following',
    readerScrollBeforeSubmit: false,
    expectedMode: 'FOLLOW_BOTTOM',
    viewport: {
      width: 412,
      initialHeight: 520,
      expandedHeight: 915,
      postSubmitHeight: 1015,
    },
  },
  {
    name: 'reader scroll immediately before Submit cancels stale follow restoration',
    readerScrollBeforeSubmit: true,
    expectedMode: 'ANCHOR_AT',
    viewport: { width: 1512, initialHeight: 520, expandedHeight: 861 },
  },
]

for (const scenario of questionFollowScenarios) test(scenario.name, async ({ page }) => {
  const questionBlock = {
    type: 'question',
    question_id: 'q-follow',
    questions: [{
      question: 'Continue following?',
      header: 'Follow',
      multiSelect: false,
      options: [
        { label: 'Yes', description: 'Continue this response.' },
        { label: 'No', description: 'Keep it paused.' },
      ],
    }],
  }
  await installQuestionStream(page, questionBlock)

  let turnStarted = false
  let pendingQuestionId = null
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const body = route.request().postDataJSON()
    if (!body.answers) {
      turnStarted = true
      pendingQuestionId = questionBlock.question_id
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'started' }),
      })
    }
    pendingQuestionId = null
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'answer_delivered',
        answer_turn: 'same',
      }),
    })
  })
  await page.route('**/api/chat/stop', route => (
    route.fulfill({ status: 200, body: '{}' })
  ))

  // Begin at a keyboard-sized height so this short question can consume the
  // initial reservation and enter FOLLOW. Expanding the viewport afterward
  // recreates the real blank-tail condition without changing reader intent.
  await page.setViewportSize({
    width: scenario.viewport.width,
    height: scenario.viewport.initialHeight,
  })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(
    page,
    scenario.readerScrollBeforeSubmit
      ? 'question-follow-reader-override'
      : 'question-follow',
  )
  expect(chat?.id).toBeTruthy()
  const runtimeState = () => ({
    running: turnStarted,
    active_goal_objective: null,
    pending_messages: [],
    pending_question_id: pendingQuestionId,
    updated_at: null,
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtimeState()),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    const userMessage = {
      role: 'user',
      content: 'Ask while I follow',
      blocks: [{ type: 'text', content: 'Ask while I follow' }],
      ts: 1700000600000,
      cid: 'question-follow-user',
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...runtimeState(),
        id: chat.id,
        messages: turnStarted ? [userMessage] : [],
        total: turnStarted ? 1 : 0,
        offset: 0,
        provider: 'claude',
        ...testChatAgentSettings(),
      }),
    })
  })
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill('Ask while I follow')
  await page.keyboard.press('Enter')

  const card = surface.locator('.qcard')
  await expect(card).toBeVisible({ timeout: 5000 })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
  }, undefined, { timeout: 5000 })
  await page.setViewportSize({
    width: scenario.viewport.width,
    height: scenario.viewport.expandedHeight,
  })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
      && (spacer?.offsetHeight || 0) >= 80
  }, undefined, { timeout: 5000 })

  await card.getByRole('radio', { name: 'Yes' }).click()
  const submit = card.getByRole('button', { name: 'Submit' })
  await expect(submit).toBeEnabled()
  let cardTopBeforeSubmit
  if (scenario.readerScrollBeforeSubmit) {
    // Reproduce the real ordering boundary deterministically: the reader's
    // scroll event has landed, but its 250ms quiet settlement has not yet
    // converted the old FOLLOW_BOTTOM into an ordinary reading anchor when
    // Submit freezes the card-to-stream handoff.
    cardTopBeforeSubmit = await page.evaluate(() => {
      const scroll = document.querySelector(
        '[data-chat-surface="painted"] .chat__scroll',
      )
      const cardElement = document.querySelector(
        '[data-chat-surface="painted"] .qcard',
      )
      const submitButton = document.querySelector(
        '[data-chat-surface="painted"] .qcard__submit',
      )
      scroll.dispatchEvent(new WheelEvent('wheel', {
        bubbles: true,
        deltaY: -160,
      }))
      scroll.scrollTop = Math.max(0, scroll.scrollTop - 160)
      scroll.dispatchEvent(new Event('scroll'))
      const topAtSubmit = cardElement.getBoundingClientRect().top
      submitButton.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        pointerType: 'mouse',
      }))
      submitButton.click()
      return topAtSubmit
    })
  } else {
    cardTopBeforeSubmit = await card.evaluate(
      element => element.getBoundingClientRect().top,
    )
    await submit.click()
  }
  await expect(card.locator('.qcard__submit')).toHaveText('Submitted')
  await expect.poll(() => surface.locator('.chat__scroll').getAttribute(
    'data-scroll-mode',
  )).toBe('ANCHOR_AT')
  const cardTopAfterAcceptance = await card.evaluate(
    element => element.getBoundingClientRect().top,
  )
  expect(cardTopAfterAcceptance).toBeCloseTo(cardTopBeforeSubmit, 0)

  let cardTopBeforeResponse = cardTopAfterAcceptance
  if (scenario.viewport.postSubmitHeight) {
    // Model the software keyboard closing after answer acceptance but before
    // the continuation begins. Responsive geometry must preserve the same
    // submission anchor rather than restoring the pre-submit follow mode.
    await page.setViewportSize({
      width: scenario.viewport.width,
      height: scenario.viewport.postSubmitHeight,
    })
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
    await expect.poll(() => surface.locator('.chat__scroll').getAttribute(
      'data-scroll-mode',
    )).toBe('ANCHOR_AT')
    cardTopBeforeResponse = await card.evaluate(
      element => element.getBoundingClientRect().top,
    )
    expect(cardTopBeforeResponse).toBeCloseTo(cardTopAfterAcceptance, 0)
  }

  // Only now let the provider emit its continuation. A previously-followed
  // card may move with this new content, never with the answer-only commit.
  await page.evaluate(() => window.__continueQuestionStream?.())
  await expect(surface.locator('.chat__msg--assistant'))
    .toContainText('AFTER_QUESTION_END', { timeout: 5000 })

  await page.waitForFunction(({ expectedMode, readerScrollBeforeSubmit }) => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    if (!scroll || scroll.dataset.scrollMode !== expectedMode) return false
    const realContentGap = scroll.scrollHeight
      - (spacer?.offsetHeight || 0)
      - scroll.scrollTop
      - scroll.clientHeight
    return readerScrollBeforeSubmit
      ? realContentGap > 40
      : Math.abs(realContentGap) <= 4
  }, scenario, { timeout: 5000 })

  if (!scenario.readerScrollBeforeSubmit) {
    const cardTopAfterResponse = await card.evaluate(
      element => element.getBoundingClientRect().top,
    )
    expect(cardTopAfterResponse).toBeLessThan(cardTopBeforeResponse - 40)
  }
})
