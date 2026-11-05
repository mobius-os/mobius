/** Replies preserve continuous prose and own one safe, draft-independent footer. */
import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import { checkHiddenReplyContinuity } from './_replyContinuityFixture.mjs'
import { mountReplyFooter } from './_replyFooterFixture.mjs'
import { settledBox } from './_geometry.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
test.use({ serviceWorkers: 'block' })
attachCleanup()

for (const provider of ['claude', 'codex', 'mobius', 'responses']) {
  test(`${provider}: hidden replay stays continuous through live, saved and partial source reads`, async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const chat = await createTaggedChat(page, `hidden-reply-${provider}`)
    await checkHiddenReplyContinuity(page, chat, BASE, provider)
  })
}

test.describe('reply footer', () => {
  test('Copy writes the answer as shown, with its Markdown for the composer', async ({ page }) => {
    await page.addInitScript(() => {
      window.__copied = []
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        async write(items) {
          const types = {}
          for (const item of items) {
            for (const type of item.types) types[type] = await (await item.getType(type)).text()
          }
          window.__copied.push(types)
        },
      } })
    })
    const latestAnswer = '## Result\n\nIt costs **\\$5** — see [the docs](https://example.com).\n\n'
      + '| Plan | Price |\n|---|---|\n| Pro | \\$5 |\n\n```\nnpm test\n```'
    const f = await mountReplyFooter(page, BASE, { latestAnswer })
    await f.composer.fill('Keep my unfinished message')
    await f.replies.last().getByRole('button', { name: 'Copy message' }).click()
    await expect(f.replies.last().getByRole('button', { name: 'Copied' })).toBeVisible()
    const [copied] = await page.evaluate(() => window.__copied)
    expect(copied['text/plain']).toBe(
      'Result\n\nIt costs $5 — see the docs.\n\nPlan\tPrice\nPro\t$5\n\nnpm test')
    expect(decodeURIComponent(copied['text/html'].match(/data-mobius-markdown="([^"]*)"/)[1]))
      .toBe(latestAnswer)
    await expect(f.composer).toHaveValue('Keep my unfinished message')
  })

  for (const state of ['settled', 'running', 'pendingQuestion']) {
    test(`a ${state} conversation has Copy but no footer Try again`, async ({ page }) => {
      const f = await mountReplyFooter(page, BASE, state === 'settled' ? {} : { [state]: true })
      await expect(f.replies.last().getByRole('button', { name: 'Copy message' })).toBeVisible()
      await expect(f.replies.getByRole('button', { name: 'Try again', includeHidden: true })).toHaveCount(0)
    })
  }

  test('older reply actions reveal on tap, never on hover', async ({ page }) => {
    const f = await mountReplyFooter(page, BASE)
    const older = f.replies.first()
    const copy = older.getByRole('button', { name: 'Copy message' })
    await older.hover()
    await expect(copy).toBeHidden()
    await older.locator('.chat__text--assistant').last().click()
    await expect(copy).toBeVisible()
    await expect(copy).toBeHidden({ timeout: 7000 })
    await expect(f.replies.last().getByRole('button', { name: 'Copy message' })).toBeVisible()
  })

  for (const width of [412, 1512]) {
    test(`the inset reply footer keeps Copy beside the time without overhanging the foot at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 861 })
      const f = await mountReplyFooter(page, BASE)
      const latest = f.replies.last()
      const footer = latest.locator('.chat__msg-meta--start')
      await f.surface.locator('.chat__scroll').evaluate(el => { el.scrollTop = el.scrollHeight })
      await settledBox(footer)
      const geometry = await latest.evaluate(reply => {
        const row = reply.querySelector('.chat__msg-meta--start').getBoundingClientRect()
        const copy = reply.querySelector('.chat__msg-meta--start .chat__msg-copy').getBoundingClientRect()
        const time = reply.querySelector('.chat__msg-meta--start .chat__ts').getBoundingClientRect()
        const paragraph = reply.querySelector('.md-paragraph')
        const textLeft = paragraph.getBoundingClientRect().left + parseFloat(getComputedStyle(paragraph).paddingLeft)
        const prose = reply.querySelector('.chat__reply-rows').getBoundingClientRect()
        const parent = reply.getBoundingClientRect()
        const foot = reply.closest('[data-chat-surface]').querySelector('.chat__foot').getBoundingClientRect()
        return { height: row.height, top: row.top, bottom: row.bottom,
          copyLeft: copy.left, timeRight: time.right, timeLeft: time.left, textLeft,
          proseBottom: prose.bottom, replyBottom: parent.bottom, footTop: foot.top }
      })
      expect(Math.abs(geometry.timeLeft - geometry.textLeft)).toBeLessThanOrEqual(0.5)
      expect(geometry.copyLeft).toBeGreaterThan(geometry.timeRight)
      expect(geometry.copyLeft - geometry.timeRight).toBeLessThanOrEqual(8)
      expect(geometry.height).toBeGreaterThan(0)
      expect(geometry.top).toBeGreaterThanOrEqual(geometry.proseBottom)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.replyBottom + 0.5)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.footTop + 0.5)
    })
  }
})
