/** An image must explain its reserved space without moving surrounding text. */
import { test, expect } from '@playwright/test'
import { runtimeSnapshot } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT_ID = '70000000-0000-4000-8000-000000000009'
const HREF = `/api/chats/${CHAT_ID}/media/pending.png`

test.use({ serviceWorkers: 'block' })

for (const viewport of [{ width: 1180, height: 820 }, { width: 390, height: 844 }]) {
  for (const outcome of ['loaded', 'failed']) {
    test(`image ${outcome} preserves its pending frame at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.route(/\/api\/chats(?:\?.*)?$/, route => route.fulfill({
        json: [{ id: CHAT_ID, title: 'Image loading fixture', has_messages: true, running: false }],
      }))
      await page.route(new RegExp(`/api/chats/${CHAT_ID}(?:\\?.*)?$`), route => route.fulfill({
        json: {
          messages: [
            { role: 'user', cid: 'image-prompt', content: 'Show a preview.', ts: 1700000000000 },
            {
              role: 'assistant', id: 'image-answer', ts: 1700000000001,
              content: `Before image.\n\n![Preview](${HREF})\n\nAfter image.`,
              media_dimensions: { [HREF]: { width: 800, height: 600 } },
            },
          ],
          total: 2, offset: 0, ...runtimeSnapshot(),
        },
      }))
      await page.route(new RegExp(`/api/chats/${CHAT_ID}/stream$`), route =>
        route.fulfill({ status: 204, body: '' }))
      let releaseToken
      const tokenGate = new Promise(resolve => { releaseToken = resolve })
      await page.route(new RegExp(`/api/chats/${CHAT_ID}/media-token$`), async route => {
        await tokenGate
        await route.fulfill({ json: { token: 'image-fixture' } })
      })
      let releaseImage
      const imageGate = new Promise(resolve => { releaseImage = resolve })
      await page.route(new RegExp(`${HREF.replace('.', '\\.')}\\?.*$`), async route => {
        await imageGate
        if (outcome === 'failed') return route.abort('failed')
        return route.fulfill({
          contentType: 'image/svg+xml',
          body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="#657589"/></svg>',
        })
      })
      await page.goto(`${BASE}/shell/?chat=${CHAT_ID}`, { waitUntil: 'domcontentloaded' })
      const painted = page.locator('[data-chat-surface="painted"]')
      const frame = painted.locator('.md-image-frame')
      await expect(frame).toBeVisible()
      await expect(frame).toHaveAttribute('aria-busy', 'true')
      await expect(frame).toContainText('Loading image…')
      const before = await frame.boundingBox()
      const afterParagraph = painted.getByText('After image.', { exact: true })
      const paragraphBefore = await afterParagraph.boundingBox()
      releaseToken()
      await expect(frame.locator('img')).toHaveCount(1)
      await expect(frame).toHaveAttribute('data-preview-state', 'pending')
      releaseImage()
      await expect(frame).toHaveAttribute('data-preview-state', outcome)
      await expect(frame).not.toHaveAttribute('aria-busy')
      if (outcome === 'loaded') {
        await expect(frame.locator('.md-image-placeholder')).toHaveCount(0)
      } else {
        await expect(frame).toContainText('Preview unavailable')
      }
      const after = await frame.boundingBox()
      const paragraphAfter = await afterParagraph.boundingBox()
      expect(after.height).toBeCloseTo(before.height, 1)
      expect(after.width).toBeCloseTo(before.width, 1)
      expect(paragraphAfter.y).toBeCloseTo(paragraphBefore.y, 1)
    })
  }
}
