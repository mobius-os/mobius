/**
 * Browser contract for sticky question / paused-turn attention nudges.
 *
 * The chat scroller extends behind an absolutely-positioned composer. The old
 * `scrollIntoView({ block: 'nearest' })` actions stopped once their card
 * intersected the scroll viewport, even though the composer still covered the
 * card's Submit or Resume action, leaving the reader roughly one composer-
 * height short of the tail.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/attention-nudges.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

const SCENARIOS = [
  {
    name: 'paused-turn',
    label: 'resume-nudge-tail',
    cardSelector: '.chat__resume',
    nudgeSelector: '.chat__resume-nudge',
    actionSelector: '.chat__resume',
    tailBlock: {
      type: 'error',
      message: 'Paused for a platform update.',
      resumable: true,
      pause: { kind: 'restart' },
    },
  },
  {
    name: 'question',
    label: 'question-nudge-tail',
    cardSelector: '.qcard',
    nudgeSelector: '.chat__question-nudge',
    actionSelector: '.qcard__submit',
    tailBlock: {
      type: 'question',
      question_id: 'attention-nudge-question',
      questions: [{
        id: 'attention_nudge_answer',
        question: 'Which option should we use?',
        header: 'Choice',
        multiSelect: false,
        options: [
          { label: 'First', description: 'Choose the first option.' },
          { label: 'Second', description: 'Choose the second option.' },
        ],
      }],
    },
  },
]

for (const scenario of SCENARIOS) {
  test(`${scenario.name} nudge clears the composer and lands at the physical tail`, async ({ page }) => {
    await page.setViewportSize({ width: 1512, height: 861 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
        || document.querySelector('.chat__scroll')
        || document.querySelector('.chat__form')),
      { timeout: 10000 },
    )

    const chat = await createTaggedChat(page, scenario.label)
    expect(chat?.id).toBeTruthy()

    const paragraphs = Array.from({ length: 42 }, (_, i) => ({
      type: 'text',
      content: `Attention history ${i + 1}. ${'Enough content to overflow the viewport. '.repeat(8)}`,
    }))
    const messages = [
      {
        role: 'user',
        content: 'Please continue after the long review.',
        ts: 1700000200000,
        cid: `${scenario.label}-user`,
        blocks: [{
          type: 'text',
          content: 'Please continue after the long review.',
        }],
      },
      {
        role: 'assistant',
        content: '',
        ts: 1700000200001,
        blocks: [...paragraphs, scenario.tailBlock],
      },
    ]

    await page.route(new RegExp(`/api/chats/${chat.id}\\?limit=`), route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages,
          total: messages.length,
          offset: 0,
          // The build-phase rail is a live-run surface. Keep the fixture's
          // stream active so the mocked build_phase event below is actually
          // consumed before we assert its layout beside the nudge.
          running: true,
          pending_messages: [],
        }),
      })
    })
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'build_phase',
        label: 'Guarded service boundary ready — Tandoor migrated safely',
        ts: 1700000200002,
      })}\n\n`,
      'data: {"type":"catch_up_done"}\n\n',
      'data: {"type":"done"}\n\n',
    ].join('')
    await page.route(new RegExp(`/api/chats/${chat.id}/stream$`), route =>
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: streamBody,
      }))

    await page.evaluate(chatId => {
      localStorage.setItem('moebius_active_chat', chatId)
    }, chat.id)
    await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })

    await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 15000 })
    await expect(page.locator(scenario.cardSelector)).toBeVisible({ timeout: 5000 })
    await page.waitForFunction(() => {
      const scroll = document.querySelector('.chat__scroll')
      return !!scroll && scroll.scrollHeight > scroll.clientHeight + 1000
    }, { timeout: 5000 })

    // Put the attention card well below the viewport with the same gesture
    // signal the controller receives from a human scroll.
    await page.evaluate(() => {
      const scroll = document.querySelector('.chat__scroll')
      if (!scroll) return
      scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      scroll.scrollTop = Math.max(
        0,
        scroll.scrollHeight - scroll.clientHeight - 1200,
      )
    })
    const nudge = page.locator(scenario.nudgeSelector)
    await expect(nudge).toBeVisible({ timeout: 5000 })
    const rail = page.locator('.chat__build-rail')
    const composer = page.locator('.chat__pill')
    await expect(rail).toBeVisible({ timeout: 5000 })

    const [nudgeBox, railBox, composerBox] = await Promise.all([
      nudge.boundingBox(),
      rail.boundingBox(),
      composer.boundingBox(),
    ])
    // Order contract: nudge above rail, rail above composer. The nudge and
    // rail are flush rows on one shared transient surface (2026-07-17 foot
    // redesign), so their boundary is a shared edge — assert ≤, not <. The
    // composer is a separate element below the surface and stays strictly
    // lower.
    expect((nudgeBox?.y ?? 0) + (nudgeBox?.height ?? 0)).toBeLessThanOrEqual(railBox?.y)
    expect((railBox?.y ?? 0) + (railBox?.height ?? 0)).toBeLessThan(composerBox?.y)

    await nudge.click()

    await page.waitForFunction(nudgeSelector => {
      const scroll = document.querySelector('.chat__scroll')
      if (!scroll || document.querySelector(nudgeSelector)) return false
      return Math.abs(scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop) <= 1
    }, scenario.nudgeSelector, { timeout: 5000 })

    const geometry = await page.evaluate(({ chatId, actionSelector }) => {
      const scroll = document.querySelector('.chat__scroll')
      const action = document.querySelector(actionSelector)
      const composer = document.querySelector('.chat__pill')
      let mode = null
      try {
        mode = JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[chatId]
      } catch { /* assertion below reports null */ }
      const actionRect = action?.getBoundingClientRect()
      const composerRect = composer?.getBoundingClientRect()
      return {
        remaining: scroll
          ? scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop
          : null,
        actionBottom: actionRect?.bottom ?? null,
        composerTop: composerRect?.top ?? null,
        modeKind: mode?.kind ?? null,
      }
    }, { chatId: chat.id, actionSelector: scenario.actionSelector })

    expect(Math.abs(geometry.remaining)).toBeLessThanOrEqual(1)
    expect(geometry.actionBottom).toBeLessThan(geometry.composerTop)
    expect(geometry.modeKind).toBe('ANCHOR_AT')
  })
}
