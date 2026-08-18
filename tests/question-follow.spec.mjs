/**
 * Browser contract for in-process Q&A continuation: a card that was already
 * following may resume follow after its accepted answer, while the temporary
 * submit anchor prevents the pending-card handoff from moving first.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

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
            // Let the accepted POST return and restore the pre-submit mode
            // before the same assistant row grows again.
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
    prefix: `${'Question follow line.\n\n'.repeat(30)}READY_FOR_QUESTION`,
    suffix: `${'Continued answer line.\n\n'.repeat(24)}AFTER_QUESTION_END`,
  })
}

const questionFollowScenarios = [
  {
    name: 'accepted same-turn answer keeps following when the question was followed',
    readerScrollBeforeSubmit: false,
    expectedMode: 'FOLLOW_BOTTOM',
  },
  {
    name: 'reader scroll immediately before Submit cancels stale follow restoration',
    readerScrollBeforeSubmit: true,
    expectedMode: 'ANCHOR_AT',
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
    await page.evaluate(() => window.__continueQuestionStream?.())
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

  await page.setViewportSize({ width: 1512, height: 861 })
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
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
      && (spacer?.offsetHeight || 0) <= 1
  }, undefined, { timeout: 5000 })

  await card.getByRole('radio', { name: 'Yes' }).click()
  const submit = card.getByRole('button', { name: 'Submit' })
  await expect(submit).toBeEnabled()
  if (scenario.readerScrollBeforeSubmit) {
    // Reproduce the real ordering boundary deterministically: the reader's
    // scroll event has landed, but its 250ms quiet settlement has not yet
    // converted the old FOLLOW_BOTTOM into an ordinary reading anchor when
    // Submit freezes the card-to-stream handoff.
    await page.evaluate(() => {
      const scroll = document.querySelector(
        '[data-chat-surface="painted"] .chat__scroll',
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
      submitButton.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        pointerType: 'mouse',
      }))
      submitButton.click()
    })
  } else {
    await submit.click()
  }
  await expect(card.locator('.qcard__submit')).toHaveText('Submitted')
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
})
