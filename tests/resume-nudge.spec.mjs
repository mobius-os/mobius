/**
 * Browser contract for the sticky paused-turn recovery nudge.
 *
 * The chat scroller extends behind an absolutely-positioned composer. The old
 * `scrollIntoView({ block: 'nearest' })` action stopped once Resume intersected
 * the scroll viewport, even though the composer still covered it, leaving the
 * reader roughly one composer-height short of the tail.
 *
 * Run: npx playwright test tests/resume-nudge.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

test('paused-turn nudge clears the composer and lands at the physical tail', async ({ page }) => {
  await page.setViewportSize({ width: 1512, height: 861 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )

  const chat = await createTaggedChat(page, 'resume-nudge-tail')
  expect(chat?.id).toBeTruthy()

  const paragraphs = Array.from({ length: 42 }, (_, i) => ({
    type: 'text',
    content: `Paused-turn history ${i + 1}. ${'Enough content to overflow the viewport. '.repeat(8)}`,
  }))
  const messages = [
    {
      role: 'user',
      content: 'Please continue the long task.',
      ts: 1700000200000,
      cid: 'resume-nudge-user',
      blocks: [{ type: 'text', content: 'Please continue the long task.' }],
    },
    {
      role: 'assistant',
      content: '',
      ts: 1700000200001,
      blocks: [
        ...paragraphs,
        {
          type: 'error',
          message: 'Paused for a platform update.',
          resumable: true,
          pause: { kind: 'restart' },
        },
      ],
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
        running: false,
        pending_messages: [],
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/stream$`), route =>
    route.fulfill({ status: 204, body: '' }))

  await page.evaluate(chatId => {
    localStorage.setItem('moebius_active_chat', chatId)
  }, chat.id)
  await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })

  await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 15000 })
  await expect(page.locator('.chat__resume')).toBeVisible({ timeout: 5000 })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('.chat__scroll')
    return !!scroll && scroll.scrollHeight > scroll.clientHeight + 1000
  }, { timeout: 5000 })

  // Put the paused card well below the viewport with the same gesture signal
  // the controller receives from a human scroll.
  await page.evaluate(() => {
    const scroll = document.querySelector('.chat__scroll')
    if (!scroll) return
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = Math.max(
      0,
      scroll.scrollHeight - scroll.clientHeight - 1200,
    )
  })
  const nudge = page.locator('.chat__resume-nudge')
  await expect(nudge).toBeVisible({ timeout: 5000 })

  await nudge.click()

  await page.waitForFunction(() => {
    const scroll = document.querySelector('.chat__scroll')
    if (!scroll || document.querySelector('.chat__resume-nudge')) return false
    return Math.abs(scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop) <= 1
  }, { timeout: 5000 })

  const geometry = await page.evaluate(chatId => {
    const scroll = document.querySelector('.chat__scroll')
    const resume = document.querySelector('.chat__resume')
    const composer = document.querySelector('.chat__pill')
    let mode = null
    try {
      mode = JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[chatId]
    } catch { /* assertion below reports null */ }
    const resumeRect = resume?.getBoundingClientRect()
    const composerRect = composer?.getBoundingClientRect()
    return {
      remaining: scroll
        ? scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop
        : null,
      resumeBottom: resumeRect?.bottom ?? null,
      composerTop: composerRect?.top ?? null,
      modeKind: mode?.kind ?? null,
    }
  }, chat.id)

  expect(Math.abs(geometry.remaining)).toBeLessThanOrEqual(1)
  expect(geometry.resumeBottom).toBeLessThan(geometry.composerTop)
  expect(geometry.modeKind).toBe('ANCHOR_AT')
})
