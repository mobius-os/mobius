/**
 * Browser contract for the live-response cursor: terminal removal must not
 * change transcript height or move a conversation that is following output.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { runtimeSnapshot } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

async function installChunkedStream(page, events) {
  await page.addInitScript((streamEvents) => {
    const realFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) {
        return realFetch(input, init)
      }
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          for (const [delayMs, event] of streamEvents) {
            setTimeout(() => {
              controller.enqueue(encoder.encode(
                `data: ${JSON.stringify(event)}\n\n`,
              ))
              if (event.type === 'done') {
                controller.close()
              }
            }, delayMs)
          }
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  }, events)
}

test('terminal cursor removal keeps followed geometry unchanged', async ({ page }) => {
  const responseContent = `${'Cursor geometry line.\n\n'.repeat(35)}END_CURSOR_GEOMETRY`
  await installChunkedStream(page, [
    [0, {
      type: 'stream_snapshot',
      items: [{
        type: 'text',
        content: responseContent,
      }],
    }],
    [20, { type: 'catch_up_done' }],
    // Leave the fully revealed answer live long enough to measure it before
    // terminal promotion removes the cursor.
    [8000, { type: 'done' }],
  ])
  await page.route('**/api/chat/stop', route => (
    route.fulfill({ status: 200, body: '{}' })
  ))

  await page.setViewportSize({ width: 1512, height: 861 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'streaming-cursor-geometry')
  expect(chat?.id).toBeTruthy()
  const userMessage = {
    role: 'user',
    content: 'Keep the final line still',
    blocks: [{ type: 'text', content: 'Keep the final line still' }],
    ts: 1700000500000,
    cid: 'cursor-user',
  }
  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...runtimeSnapshot({ running: true }),
        active_goal_objective: null,
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: chat.id,
        messages: [userMessage],
        total: 1,
        offset: 0,
        ...runtimeSnapshot({ running: true }),
        provider: 'claude',
      }),
    })
  })
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  const surface = page.locator('[data-chat-surface="painted"]')
  await expect(surface.locator('.chat__stop')).toBeVisible({ timeout: 3000 })

  await expect(surface.locator('.chat__msg--assistant'))
    .toContainText('END_CURSOR_GEOMETRY', { timeout: 3000 })
  // A cold return deliberately restores as a hold. Use the real reader-gesture
  // entrance before measuring the terminal transition.
  await page.evaluate(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    if (!scroll) return
    scroll.scrollTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight - 80)
  })
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)))
  await page.evaluate(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    if (!scroll) return
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = scroll.scrollHeight
  })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    return scroll?.dataset.scrollMode === 'FOLLOW_BOTTOM'
      && (spacer?.offsetHeight || 0) <= 1
  }, undefined, { timeout: 5000 })
  const cursor = surface.locator('.chat__cursor')
  await expect(cursor).toBeVisible()
  const cursorGeometry = await cursor.evaluate(el => ({
    position: getComputedStyle(el).position,
    animationName: getComputedStyle(el).animationName,
    width: el.getBoundingClientRect().width,
    height: el.getBoundingClientRect().height,
  }))
  expect(cursorGeometry.position).toBe('absolute')
  expect(cursorGeometry.animationName).toBe('cursorPulse')
  expect(cursorGeometry.width).toBeGreaterThan(4)
  expect(cursorGeometry.height).toBeGreaterThan(4)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect.poll(() => cursor.evaluate(el => ({
    animationName: getComputedStyle(el).animationName,
    opacity: getComputedStyle(el).opacity,
  }))).toEqual({
    animationName: 'none',
    opacity: '0.65',
  })
  await page.evaluate(() => new Promise(resolve => (
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  )))

  const measure = () => page.evaluate(() => {
    const painted = document.querySelector('[data-chat-surface="painted"]')
    const scroll = painted?.querySelector('.chat__scroll')
    const rows = painted?.querySelectorAll('.chat__msg--assistant') || []
    const paragraphs = rows[rows.length - 1]?.querySelectorAll('.md-paragraph') || []
    const paragraph = paragraphs[paragraphs.length - 1]
    const row = rows[rows.length - 1]
    const blocks = row?.querySelector('.md-blocks')
    const rowRect = row?.getBoundingClientRect()
    const paragraphRect = paragraph?.getBoundingClientRect()
    const meta = row?.querySelector('.chat__msg-meta')
    const metaStyle = meta ? getComputedStyle(meta) : null
    return {
      blocksHeight: blocks?.getBoundingClientRect().height ?? -1,
      rowHeight: rowRect?.height ?? -1,
      // The metadata row's net contribution to the answer row's flow.
      metaFlow: metaStyle
        ? parseFloat(metaStyle.marginTop) + meta.getBoundingClientRect().height
          + parseFloat(metaStyle.marginBottom)
        : 0,
      paragraphOffset: paragraphRect && rowRect
        ? paragraphRect.top - rowRect.top
        : null,
      bottomGap: scroll
        ? scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight
        : -1,
    }
  })
  const live = await measure()

  await expect(surface.locator('.chat__stop')).toHaveCount(0, { timeout: 10000 })
  await expect(surface.locator('.chat__cursor')).toHaveCount(0)
  await page.evaluate(() => new Promise(resolve => (
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  )))
  const settled = await measure()

  // Removing the cursor leaves the answer's own flow alone: the rendered blocks
  // keep their height and the followed paragraph keeps its place in the row.
  expect(Math.abs(settled.blocksHeight - live.blocksHeight)).toBeLessThanOrEqual(1)
  expect(Math.abs(settled.paragraphOffset - live.paragraphOffset)).toBeLessThanOrEqual(1)
  // The settled turn also mounts its metadata row, whose top margin is not
  // cancelled by its reserved height. That is the ONLY growth accepted: the row
  // grows by exactly the metadata row's flow, and the followed view stays
  // pinned to the tail (bottomGap derives from fractional scrollTop).
  expect(Math.abs((settled.rowHeight - live.rowHeight) - (settled.metaFlow - live.metaFlow)))
    .toBeLessThanOrEqual(1)
  expect(Math.abs(settled.bottomGap - live.bottomGap)).toBeLessThanOrEqual(1.5)
})
