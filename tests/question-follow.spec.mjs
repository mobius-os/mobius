/**
 * Browser contract for in-process Q&A continuation: a card that was already
 * following may resume follow only when its post-answer response begins. The
 * temporary submit anchor holds through answer acceptance, so blank tail room
 * cannot move the card before the continuation actually renders.
 */
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import { testChatAgentSettings, installMockAgentProvider } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

async function installQuestionStream(page, questionBlock, chatId, { longTurn = false } = {}) {
  const prefix = `${'Question follow line.\n\n'.repeat(longTurn ? 140 : 2)}READY_FOR_QUESTION`
  await page.addInitScript(({ question, prefix, suffix, chatId }) => {
    const realFetch = window.fetch.bind(window)
    const subscribers = new Set()
    let answers = null
    let continued = false
    const publish = event => {
      for (const emit of subscribers) emit(event)
    }
    // This fixture models one chat's server broadcast, not the last view that
    // happened to subscribe. Retained/hidden views must not steal its output.
    window.__answerQuestionStream = value => { answers = value }
    window.__continueQuestionStream = () => {
      if (continued) throw new Error('Question continuation already emitted')
      continued = true
      publish({ type: 'text_boundary' })
      publish({ type: 'text_final', text_item_id: 'after-question', content: suffix })
    }
    window.fetch = (input, init) => {
      const url = new URL(String(input?.url || input), location.href)
      if (url.pathname !== `/api/chats/${chatId}/stream`) return realFetch(input, init)
      const signal = init?.signal || input?.signal
      if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
      const encoder = new TextEncoder()
      let emit
      let abort
      const cleanup = () => {
        subscribers.delete(emit)
        signal?.removeEventListener('abort', abort)
      }
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          emit = event => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
          abort = () => {
            cleanup()
            controller.error(new DOMException('Aborted', 'AbortError'))
          }
          subscribers.add(emit)
          signal?.addEventListener('abort', abort, { once: true })
          emit({
            type: 'stream_snapshot',
            items: [
              { type: 'text', content: prefix },
              { ...question, ...(answers ? { answers } : {}) },
              ...(continued ? [{ type: 'text', text_item_id: 'after-question', content: suffix }] : []),
            ],
          })
          emit({ type: 'catch_up_done' })
          emit({ ...question, ...(answers ? { answers } : {}) })
        },
        cancel: cleanup,
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, {
    chatId,
    question: questionBlock,
    // The long turn exercises the real display:contents copy wrapper, while
    // short turns leave tail room that must survive answer-only acceptance.
    prefix,
    suffix: `${'Continued answer line.\n\n'.repeat(24)}AFTER_QUESTION_END`,
  })
  return prefix
}

const questionFollowScenarios = [
  {
    name: 'followed question holds its reserved tail through acceptance',
    readerScrollBeforeSubmit: false,
    expectedMode: 'FOLLOW_BOTTOM',
    viewport: {
      width: 412,
      initialHeight: 520,
      expandedHeight: 861,
      postSubmitHeight: 915,
    },
  },
  {
    name: 'long wrapped question stays fixed from activation until response activity',
    readerScrollBeforeSubmit: false,
    expectedMode: 'FOLLOW_BOTTOM',
    longTurn: true,
    viewport: {
      width: 412,
      // Model the actual keyboard cycle: the controller has seen the full
      // viewport, the keyboard reduces it, then Submit dismisses the keyboard.
      initialHeight: 1015,
      expandedHeight: 520,
      preClickHeight: 1015,
    },
  },
  {
    name: 'reader scroll immediately before Submit cancels stale follow restoration',
    readerScrollBeforeSubmit: true,
    expectedMode: 'ANCHOR_AT',
    viewport: { width: 1512, initialHeight: 520, expandedHeight: 861 },
  },
]

const coldQuestionScenario = {
  name: 'cold queued send hydrates its saved question and accepts the answer without moving it',
  queuedUntilReady: true,
  viewport: { width: 412, initialHeight: 861 },
}

for (const scenario of [...questionFollowScenarios, coldQuestionScenario]) test(scenario.name, async ({ page }) => {
  const chat = { id: crypto.randomUUID() }
  await serveRecoveryBuild(page)
  await installMockAgentProvider(page)
  await page.route('**/api/**', route => route.request().method() === 'GET'
    ? route.fallback()
    : route.fulfill({ status: 501, json: { detail: 'Unexpected fixture mutation' } }))
  let releaseReadiness
  const readyGate = scenario.queuedUntilReady
    ? new Promise(resolve => { releaseReadiness = resolve }) : Promise.resolve()
  try {
    await page.route('**/api/ready', async route => {
      await readyGate
      return route.fulfill({ json: { ready: true, boot_id: 'question-follow-boot' } })
    })
    await page.route(/\/api\/chats(?:\?.*)?$/, route => route.request().method() === 'GET'
      ? route.fulfill({ json: [{ id: chat.id, title: 'Question follow fixture', has_messages: true, running: false }] })
      : route.fallback())
    await page.route(/\/api\/apps\/?(?:\?.*)?$/, route => route.fulfill({ json: [] }))
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
    const prefix = await installQuestionStream(page, questionBlock, chat.id, scenario)

    let turnStarted = false
    let acceptedMessage = null
    let messagePosts = 0
    let pendingQuestionId = null
    await page.route(new RegExp(`/api/chats/${chat.id}/messages$`), async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      const body = route.request().postDataJSON()
      if (!body.answers) {
        messagePosts += 1
        acceptedMessage = { role: 'user', content: body.content, cid: body.cid, ts: 1700000600000 }
        turnStarted = true
        pendingQuestionId = questionBlock.question_id
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'started' }),
        })
      }
      pendingQuestionId = null
      questionBlock.answers = body.answers
      await page.evaluate(answers => window.__answerQuestionStream(answers), body.answers)
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

    // Each scenario begins at the geometry needed to establish its original
    // mode, then changes to the submit-time geometry without reader intent. The
    // long case starts full-height and shrinks first so the controller knows the
    // real keyboard-closed height before Submit restores it.
    await page.setViewportSize({
      width: scenario.viewport.width,
      height: scenario.viewport.initialHeight,
    })
    const runtimeState = () => ({
      running: turnStarted,
      active_goal_objective: null,
      pending_messages: [],
      pending_question_id: pendingQuestionId,
      updated_at: null,
    })
    await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(runtimeState()),
      })
    })
    await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...runtimeState(),
          id: chat.id,
          // A published question is already durable. Reconnecting views read
          // this card instead of replaying the entire pre-question stream.
          messages: acceptedMessage ? [acceptedMessage, {
            role: 'assistant', id: 'question-follow-assistant', ts: 1700000600100,
            blocks: [{ type: 'text', content: prefix }, questionBlock],
          }] : [],
          total: turnStarted ? 2 : 0,
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
    if (releaseReadiness) {
      await expect(surface.locator('.queued__row').filter({ hasText: 'Ask while I follow' })).toBeVisible()
      expect(messagePosts).toBe(0)
      releaseReadiness()
    }
    await expect.poll(() => messagePosts).toBe(1)

    const card = surface.locator('.qcard')
    await expect(card).toBeVisible({ timeout: 5000 })
    if (scenario.queuedUntilReady) {
      // A cold delivery preserves reading intent; it does not implicitly enter
      // FOLLOW_BOTTOM like the deliberate live send in the scenarios below.
      await card.getByRole('radio', { name: /^Yes/ }).click()
      const topBefore = (await card.boundingBox()).y
      await card.getByRole('button', { name: 'Submit', exact: true }).click()
      await expect(card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
      expect(Math.abs((await card.boundingBox()).y - topBefore)).toBeLessThanOrEqual(2)
      await page.evaluate(() => window.__continueQuestionStream())
      await expect(surface.locator('.chat__msg--assistant')).toContainText('AFTER_QUESTION_END')
      await expect(surface.locator('.chat__msg--user')).toHaveCount(1)
      expect(messagePosts).toBe(1)
      return
    }
    await page.waitForFunction(() => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
    }, undefined, { timeout: 5000 })
    await page.setViewportSize({
      width: scenario.viewport.width,
      height: scenario.viewport.expandedHeight,
    })
    await page.waitForFunction(({ longTurn }) => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
      return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
        && (longTurn || (spacer?.offsetHeight || 0) >= 80)
    }, scenario, { timeout: 5000 })

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
      if (scenario.viewport.preClickHeight) {
        // Capture the real pointerdown boundary first, then model native
        // keyboard-close geometry before click dispatch. The click must commit
        // the prepared card coordinate rather than capturing the browser's
        // intermediate clamp.
        await page.evaluate(() => {
          const cardElement = document.querySelector(
            '[data-chat-surface="painted"] .qcard',
          )
          window.__questionActivationTops = []
          window.__sampleQuestionActivation = true
          const sample = () => {
            if (!window.__sampleQuestionActivation) return
            window.__questionActivationTops.push(
              cardElement.getBoundingClientRect().top,
            )
            requestAnimationFrame(sample)
          }
          sample()
        })
        await submit.hover()
        await page.mouse.down()
        const preparedReservation = await surface.locator('.spacer-dynamic')
          .evaluate(element => element.offsetHeight)
        expect(preparedReservation).toBeGreaterThanOrEqual(
          scenario.viewport.preClickHeight - scenario.viewport.expandedHeight - 5,
        )
        await page.setViewportSize({
          width: scenario.viewport.width,
          height: scenario.viewport.preClickHeight,
        })
        await page.evaluate(() => new Promise(resolve => (
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        )))
        const cardTopDuringActivation = await card.evaluate(
          element => element.getBoundingClientRect().top,
        )
        expect(cardTopDuringActivation).toBeCloseTo(cardTopBeforeSubmit, 0)
        await page.mouse.up()
      } else {
        await submit.click()
      }
    }
    await expect(card.locator('.qcard__submit')).toHaveText('Submitted')
    await expect.poll(() => surface.locator('.chat__scroll').getAttribute(
      'data-scroll-mode',
    )).toBe('ANCHOR_AT')
    if (scenario.viewport.preClickHeight) {
      const activationTops = await page.evaluate(() => new Promise(resolve => {
        requestAnimationFrame(() => {
          window.__sampleQuestionActivation = false
          resolve(window.__questionActivationTops || [])
        })
      }))
      expect(activationTops.length).toBeGreaterThan(1)
      expect(Math.max(...activationTops.map(top => (
        Math.abs(top - activationTops[0])
      )))).toBeLessThanOrEqual(1)
    }
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
    await page.evaluate(async chatId => {
      // Another view may attach after this one, then another may close. Neither
      // may redirect or break the visible view's continuation.
      const url = `/api/chats/${chatId}/stream`
      window.__questionObserver = await fetch(url)
      const cancelled = await fetch(url)
      await cancelled.body.cancel()
      window.__continueQuestionStream()
    }, chat.id)
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
    await page.evaluate(() => window.__questionObserver.body.cancel())
    expect(messagePosts).toBe(1)
  } finally {
    releaseReadiness?.()
  }
})
