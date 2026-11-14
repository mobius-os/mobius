/* References share the reply footer's reveal and keep it present while open. */
import { test, expect } from '@playwright/test'
import { attachCleanup } from './_chatTracker.mjs'
import { mountReplyFooter } from './_replyFooterFixture.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const sources = [
  { url: 'https://example.com/docs', title: 'Documentation' },
  { url: 'https://example.org/guide', title: 'Guide' },
]
attachCleanup()

for (const touch of [false, true]) {
  test.describe(touch ? 'touch References footer' : 'desktop References footer', () => {
    test.use({ serviceWorkers: 'block', hasTouch: touch,
      viewport: { width: touch ? 320 : 1720, height: 844 } })

    test('one row, open past the reveal timeout, collapsed back to auto-hide', async ({ page }) => {
      const f = await mountReplyFooter(page, BASE, { sources })
      const reply = f.replies.last()
      const footer = reply.locator('.chat__msg-meta--start')
      const toggle = footer.getByRole('button', { name: 'References 2' })
      await page.mouse.move(0, 0)
      await expect.poll(() => footer.evaluate(el => getComputedStyle(el).opacity)).toBe('0')
      await reply.locator('.chat__text--assistant').first().click()
      await expect.poll(() => footer.evaluate(el => getComputedStyle(el).opacity)).toBe('1')
      const geometry = await footer.evaluate(el => {
        const box = selector => el.querySelector(selector).getBoundingClientRect()
        const time = box('time'), copy = box('.chat__msg-copy')
        const branch = box('.chat__msg-branch'), refs = box('.chat__sources-toggle')
        return { centerDelta: Math.abs(time.y + time.height / 2 - refs.y - refs.height / 2),
          ordered: time.right <= copy.left && copy.right <= branch.left && branch.right <= refs.left,
          fits: refs.right <= window.innerWidth }
      })
      expect(geometry.centerDelta).toBeLessThan(1)
      expect(geometry.ordered).toBe(true)
      expect(geometry.fits).toBe(true)
      await toggle.click()
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      await expect(footer.getByRole('link')).toHaveCount(2)
      await toggle.evaluate(el => el.blur())
      await page.mouse.move(0, 0)
      // The old tap reveal expires, but expansion must keep all actions shown.
      await expect(footer).not.toHaveClass(/chat__msg-meta--visible/, { timeout: 7000 })
      await expect.poll(() => footer.evaluate(el => getComputedStyle(el).opacity)).toBe('1')
      // An inactive mounted chat must never paint its pinned-open controls.
      await reply.evaluate(el => { el.style.visibility = 'hidden' })
      await expect.poll(() => footer.evaluate(el => getComputedStyle(el).visibility)).toBe('hidden')
      await reply.evaluate(el => { el.style.removeProperty('visibility') })
      await expect(toggle).toBeVisible()
      await toggle.click()
      await toggle.evaluate(el => el.blur())
      await page.mouse.move(0, 0)
      await expect(toggle).toHaveAttribute('aria-expanded', 'false')
      await expect(footer.locator('.chat__sources-body')).toBeHidden()
      await expect.poll(() => footer.evaluate(el => getComputedStyle(el).opacity),
        { timeout: 7000 }).toBe('0')
    })

    test('a historical answer keeps References in its own shared footer', async ({ page }) => {
      const f = await mountReplyFooter(page, BASE, { earlierSources: sources })
      const older = f.replies.first()
      const footer = older.locator('.chat__msg-meta--start')
      await expect(footer.getByRole('button', { name: 'Copy message' })).toHaveCount(1)
      const toggle = footer.getByRole('button', { name: 'References 2' })
      await toggle.click()
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      await expect(footer.getByRole('link')).toHaveCount(2)
      await expect(f.replies.last().getByRole('button', { name: 'References 2' })).toHaveCount(0)
    })

    test('lazy references hold the footer through loading, failure and retry', async ({ page }) => {
      let requests = 0
      let release
      const loading = new Promise(resolve => { release = resolve })
      await page.route('**/message-sources?*', async route => {
        requests += 1
        if (requests === 1) {
          await loading
          await route.fulfill({ status: 503, json: { detail: 'Fixture failure' } })
        } else await route.fulfill({ json: { sources } })
      })
      const f = await mountReplyFooter(page, BASE, { sourceRef: { message_index: 3, count: 2 } })
      const footer = f.replies.last().locator('.chat__msg-meta--start')
      const toggle = footer.getByRole('button', { name: 'References 2' })
      expect(requests).toBe(0)
      await f.replies.last().locator('.chat__text--assistant').first().click()
      await toggle.click()
      try {
        await expect(footer.getByText('Loading references…')).toBeVisible()
        await toggle.evaluate(el => el.blur())
        await page.mouse.move(0, 0)
        await expect(footer).not.toHaveClass(/chat__msg-meta--visible/, { timeout: 7000 })
        await expect.poll(() => footer.evaluate(el => getComputedStyle(el).opacity)).toBe('1')
      } finally { release() }
      await expect(footer.getByText('Some references could not load.')).toBeVisible()
      await footer.getByRole('button', { name: 'Retry', exact: true }).click()
      await expect(footer.getByRole('link')).toHaveCount(2)
      expect(requests).toBe(2)
    })

    test('keyboard expansion and saved open state preserve the full footer', async ({ page }) => {
      const f = await mountReplyFooter(page, BASE, { sources })
      const footer = f.replies.last().locator('.chat__msg-meta--start')
      const toggle = footer.getByRole('button', { name: 'References 2' })
      await toggle.focus()
      await page.keyboard.press('Enter')
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      await page.reload()
      const restored = page.locator('[data-chat-surface="painted"] .chat__reply').last()
        .locator('.chat__msg-meta--start')
      await expect(restored.getByRole('button', { name: 'References 2' }))
        .toHaveAttribute('aria-expanded', 'true')
      await page.mouse.move(0, 0)
      await expect.poll(() => restored.evaluate(el => getComputedStyle(el).opacity)).toBe('1')
    })
  })
}
