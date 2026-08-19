import { readFile } from 'node:fs/promises'
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8000'
const SOURCE = '/tmp/goal-hierarchical-chat.json'
const SURFACE_W = Number(process.env.TRACE_SURFACE_W || 0)
const SURFACE_H = Number(process.env.TRACE_SURFACE_H || 0)
const DELAY_TERMINAL = process.env.TRACE_DELAY_TERMINAL === '1'

attachCleanup()
test.use({
  serviceWorkers: 'block',
  viewport: { width: 1512, height: 861 },
  deviceScaleFactor: 2,
})

async function installDelayedStream(page, firstResponseItems) {
  await page.addInitScript(({ priorItems }) => {
    const realFetch = window.fetch.bind(window)
    let streamIndex = 0
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) return realFetch(input, init)
      const current = streamIndex++
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          if (current === 0) {
            setTimeout(() => controller.enqueue(encoder.encode(
              `data: ${JSON.stringify({ type: 'stream_snapshot', items: priorItems })}\n\n`,
            )), 5)
            setTimeout(() => controller.enqueue(encoder.encode(
              `data: ${JSON.stringify({ type: 'catch_up_done' })}\n\n`,
            )), 20)
            setTimeout(() => {
              controller.enqueue(encoder.encode(
                `data: ${JSON.stringify({ type: 'done' })}\n\n`,
              ))
              controller.close()
            }, 400)
            return
          }
          setTimeout(() => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({ type: 'catch_up_done' })}\n\n`,
          )), 300)
          setTimeout(() => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({
              type: 'thinking',
              content: '**Planning investigation on UI message handling**',
              thinking_id: 'goal-send-trace-thinking',
            })}\n\n`,
          )), 320)
          setTimeout(() => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({
              type: 'text',
              content: 'I’ll check the exact delivery and rendering path for those completion notices, then give you a candid confidence assessment separated into architecture, tested behavior, and remaining risks.',
            })}\n\n`,
          )), 340)
          setTimeout(() => {
            controller.enqueue(encoder.encode(
              `data: ${JSON.stringify({ type: 'done' })}\n\n`,
            ))
            controller.close()
          }, 2000)
        },
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { priorItems: firstResponseItems })
}

test('trace exact Goal chat desktop send', async ({ page }) => {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 6 })
  const source = JSON.parse(await readFile(SOURCE, 'utf8'))
  const original = source.messages
  const sentText = original[48].content
  let messages = original.slice(26, 46)
  let running = false
  let sendCount = 0
  let terminalReadDelayed = false
  const detailReads = []

  await installDelayedStream(page, original[47].blocks)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'goal-desktop-send-trace')
  expect(chat?.id).toBeTruthy()

  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => (
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        running,
        active_goal_objective: null,
        pending_messages: [],
        pending_question_id: null,
      }),
    })
  ))
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), async route => {
    if (route.request().method() !== 'GET') return route.continue()
    detailReads.push({ at: Date.now(), sendCount, running, url: route.request().url() })
    if (DELAY_TERMINAL && sendCount === 1 && !running && !terminalReadDelayed) {
      terminalReadDelayed = true
      await new Promise(resolve => setTimeout(resolve, 3000))
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: chat.id,
        title: 'Goal desktop send trace',
        messages,
        total: messages.length,
        offset: 0,
        running,
        pending_messages: [],
        pending_question_id: null,
        provider: 'codex',
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/messages$`), async route => {
    const request = route.request().postDataJSON()
    sendCount += 1
    running = true
    const message = {
      role: 'user',
      content: request.content,
      blocks: [{ type: 'text', content: request.content }],
      ts: sendCount === 1 ? 1787138457031 : 1787139223950,
      cid: request.cid,
    }
    messages = [...messages, message]
    if (sendCount === 1) {
      setTimeout(() => {
        messages = [...messages, original[47]]
        running = false
      }, 350)
    }
    await new Promise(resolve => setTimeout(resolve, 120))
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started', message }),
    })
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const scroll = surface.locator('.chat__scroll')
  await expect(scroll).toBeVisible({ timeout: 60000 })
  await expect(surface.locator('.chat__msg')).toHaveCount(20, { timeout: 60000 })
  if (SURFACE_W > 0 || SURFACE_H > 0) {
    await page.evaluate(({ width, height }) => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      if (width > 0) {
        surface.style.width = `${width}px`
        surface.style.right = 'auto'
      }
      if (height > 0) {
        surface.style.height = `${height}px`
        surface.style.bottom = 'auto'
      }
    }, { width: SURFACE_W, height: SURFACE_H })
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
  }

  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await page.evaluate(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    scroll.scrollTop = scroll.scrollHeight
  })
  await input.fill(original[46].content)
  await page.keyboard.press('Enter')
  await expect(surface.getByText('The continuity patch is live', { exact: false }).first())
    .toBeVisible({ timeout: 30000 })
  await page.waitForFunction(() => (
    !document.querySelector('[data-chat-surface="painted"] .chat__stop')
  ), undefined, { timeout: 30000 })
  await expect(surface.locator('.chat__msg')).toHaveCount(22, { timeout: 30000 })
  await page.evaluate(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    scroll.scrollTop = scroll.scrollHeight
  })
  await input.fill(sentText)
  await page.waitForFunction(() => {
    const surface = document.querySelector('[data-chat-surface="painted"]')
    const scroll = surface?.querySelector('.chat__scroll')
    return scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 4
  })

  const priorUserCount = await surface.locator('.chat__msg--user').count()
  await page.evaluate(() => {
    window.__goalSendFrames = []
    window.__goalSendSampling = true
    const startedAt = performance.now()
    const sample = () => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const scroll = surface?.querySelector('.chat__scroll')
      const users = surface?.querySelectorAll('.chat__msg--user') || []
      const row = users[users.length - 1]
      const prior = row?.previousElementSibling
      const sr = scroll?.getBoundingClientRect()
      const rr = row?.getBoundingClientRect()
      window.__goalSendFrames.push({
        t: Math.round(performance.now() - startedAt),
        users: users.length,
        top: sr && rr ? Math.round(rr.top - sr.top) : null,
        scrollTop: Math.round(scroll?.scrollTop || 0),
        scrollHeight: Math.round(scroll?.scrollHeight || 0),
        clientHeight: Math.round(scroll?.clientHeight || 0),
        listHeight: Math.round(surface?.querySelector('.chat__list')?.offsetHeight || 0),
        spacer: Math.round(surface?.querySelector('.spacer-dynamic')?.offsetHeight || 0),
        mode: scroll?.dataset.scrollMode || null,
        priorKey: prior?.dataset?.key || null,
        priorHeight: Math.round(prior?.getBoundingClientRect()?.height || 0),
        priorImages: [...(prior?.querySelectorAll('img') || [])].map(img => ({
          complete: img.complete,
          naturalHeight: img.naturalHeight,
          height: Math.round(img.getBoundingClientRect().height),
        })),
      })
      if (window.__goalSendSampling) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })

  await page.keyboard.press('Enter')
  await page.waitForTimeout(1200)
  const evidence = await page.evaluate(priorCount => {
    window.__goalSendSampling = false
    const frames = window.__goalSendFrames || []
    const visible = frames.filter(frame => frame.users > priorCount && frame.top != null)
    const changes = visible.filter((frame, index) => {
      if (!index) return true
      const before = visible[index - 1]
      return frame.top !== before.top
        || frame.scrollTop !== before.scrollTop
        || frame.scrollHeight !== before.scrollHeight
        || frame.spacer !== before.spacer
        || frame.priorHeight !== before.priorHeight
        || frame.mode !== before.mode
    })
    return { changes, trace: window.__mobiusChatScrollTrace || null }
  }, priorUserCount)
  console.log(JSON.stringify({ ...evidence, detailReads }, null, 2))
  expect(evidence.changes.length).toBeGreaterThan(0)
})
