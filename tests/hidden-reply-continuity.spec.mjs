/** Replies preserve continuous prose and own one safe, draft-independent footer. */
import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import { checkHiddenReplyContinuity } from './_replyContinuityFixture.mjs'
import { mountReplyFooter } from './_replyFooterFixture.mjs'
import { settledBox } from './_geometry.mjs'
import { waitForComposerSendable } from './_chatSession.mjs'

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
    await f.replies.last().getByRole('button', { name: 'Copy message' }).click()
    await expect(f.replies.last().getByRole('button', { name: 'Copied' })).toBeVisible()
    const [copied] = await page.evaluate(() => window.__copied)
    expect(copied['text/plain']).toBe(
      'Result\n\nIt costs $5 — see the docs.\n\nPlan\tPrice\nPro\t$5\n\nnpm test')
    expect(decodeURIComponent(copied['text/html'].match(/data-mobius-markdown="([^"]*)"/)[1]))
      .toBe(latestAnswer)
  })

  test('only the latest settled reply offers Try again', async ({ page }) => {
    const f = await mountReplyFooter(page, BASE)
    // includeHidden also catches an accidentally actionable earlier reply
    // whose action row is presently concealed until tap/hover.
    await expect(f.replies.first().getByRole('button', { name: 'Try again', includeHidden: true })).toHaveCount(0)
    await expect(f.replies.last().getByRole('button', { name: 'Try again' })).toBeVisible()
  })

  for (const state of ['running', 'pendingQuestion']) {
    test(`a ${state} conversation does not offer Try again on its settled reply`, async ({ page }) => {
      const f = await mountReplyFooter(page, BASE, { [state]: true })
      // Copy remains: this checks ChatView's caller policy, not the child's
      // own suppression on a streaming reply or one whose card owns recovery.
      await expect(f.replies.last().getByRole('button', { name: 'Copy message' })).toBeVisible()
      await expect(f.surface.getByRole('button', { name: 'Try again', includeHidden: true })).toHaveCount(0)
    })
  }

  for (const rejected of [false, true]) {
    test(`Try again sends its own text without draft attachments and preserves the draft on ${rejected ? 'failure' : 'success'}`, async ({ page }) => {
      const f = await mountReplyFooter(page, BASE)
      const draft = 'Keep my unrelated unfinished message'
      await f.composer.fill(draft)
      await f.surface.locator('form.chat__form input[type="file"]').setInputFiles({
        name: 'draft-note.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment'),
      })
      const attachment = f.surface.getByRole('button', { name: 'Remove draft-note.txt' })
      await expect(attachment).toBeVisible()
      await waitForComposerSendable(f.surface)
      const attempts = []
      let release
      const responseGate = new Promise(resolve => { release = resolve })
      await page.route(`**${f.path}/messages`, async route => {
        const body = route.request().postDataJSON()
        attempts.push(body)
        await responseGate
        return rejected
          ? route.fulfill({ status: 400, json: { detail: 'Retry fixture rejected this message.' } })
          : route.fulfill({ status: 202, json: f.accept(body) })
      })
      try {
        await f.replies.last().getByRole('button', { name: 'Try again' }).click()
        await expect.poll(() => attempts.length).toBe(1)
        expect(attempts[0].content).toBe('Try again')
        // Empty attachments are omitted at the real wire boundary.
        expect(attempts[0].attachments ?? []).toEqual([])
        await expect(f.composer).toHaveValue(draft)
        await expect(attachment).toBeVisible()
        release()
        if (rejected) {
          const failure = f.surface.getByRole('status').filter({ hasText: 'Möbius couldn’t send the message.' })
          await expect(failure).toBeVisible()
          await expect(failure).not.toContainText('composer')
          await expect(f.surface.locator('.chat__msg--user').filter({ hasText: 'Try again' })).toHaveCount(0)
          // The failed send stays retryable, under the same identity so an
          // unconfirmed attempt that did land cannot start a second turn.
          await f.replies.last().getByRole('button', { name: 'Try again' }).click()
          await expect.poll(() => attempts.length).toBe(2)
          expect(attempts[1].cid).toBe(attempts[0].cid)
          await expect(f.composer).toHaveValue(draft)
          // A failed Try again leaves no record that would replace the draft
          // with its own text when the chat is opened again.
          await page.reload({ waitUntil: 'domcontentloaded' })
          await expect(f.replies).toHaveCount(2)
        } else {
          await expect(f.surface.getByText('Retried answer.', { exact: true })).toBeVisible()
          await expect(f.surface.locator('.chat__msg--user').last().locator('.chat__text--user')).toHaveText('Try again')
          expect(attempts).toHaveLength(1)
        }
        await expect(f.composer).toHaveValue(draft)
        await expect(attachment).toBeVisible()
      } finally { release() }
    })
  }

  for (const width of [412, 1512]) {
    test(`the reply action row occupies its full height without overhanging the foot at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 861 })
      const f = await mountReplyFooter(page, BASE)
      const latest = f.replies.last()
      const footer = latest.locator('.chat__msg-meta--start')
      await f.surface.locator('.chat__scroll').evaluate(el => { el.scrollTop = el.scrollHeight })
      await settledBox(footer)
      const geometry = await latest.evaluate(reply => {
        const row = reply.querySelector('.chat__msg-meta--start').getBoundingClientRect()
        const prose = reply.querySelector('.chat__reply-rows').getBoundingClientRect()
        const parent = reply.getBoundingClientRect()
        const foot = reply.closest('[data-chat-surface]').querySelector('.chat__foot').getBoundingClientRect()
        return { height: row.height, top: row.top, bottom: row.bottom,
          proseBottom: prose.bottom, replyBottom: parent.bottom, footTop: foot.top }
      })
      expect(geometry.height).toBeGreaterThan(0)
      expect(geometry.top).toBeGreaterThanOrEqual(geometry.proseBottom)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.replyBottom + 0.5)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.footTop + 0.5)
    })
  }
})
