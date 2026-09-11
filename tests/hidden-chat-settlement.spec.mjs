import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
attachCleanup()
test.use({ serviceWorkers: 'block', viewport: { width: 1400, height: 900 } })

test('returning to a retained hidden chat settles a missed terminal stream event', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const a = await createTaggedChat(page, 'hidden-settlement')
  const b = await createTaggedChat(page, 'visible-sibling')
  let ws = paneModel.setViewMode(paneModel.seedFromFlatTabs([
    { kind: 'chat', id: a.id }, { kind: 'chat', id: b.id },
  ]), 'panes')
  ws = paneModel.moveTab(ws, `chat:${b.id}`, { root: true, edge: 'right' })
  ws = paneModel.focusPane(ws, 'p0')
  await page.addInitScript(({ key, workspace, chatId }) => {
    localStorage.setItem(key, workspace)
    const realFetch = window.fetch.bind(window)
    const encoder = new TextEncoder()
    window.fetch = (input, init) => {
      const path = new URL(String(input?.url || input), location.origin).pathname
      if (path === '/api/events/system' || path === `/api/chats/${chatId}/stream`) {
        return Promise.resolve(new Response(new ReadableStream({
          start(controller) {
            const emit = event => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
            if (path === '/api/events/system') window.emitSettlementEvent = emit
            else {
              emit({ type: 'thinking', content: 'Waiting for the saved result.' })
              emit({ type: 'catch_up_done' })
              // Deliberately never deliver the terminal event or close this stream.
            }
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } }))
      }
      return realFetch(input, init)
    }
  }, { key: paneModel.STORAGE_KEY, workspace: paneModel.serializeWorkspace(ws), chatId: a.id })

  let running = false
  let messages = []
  let idleRuntimeReads = 0
  await page.route(new RegExp(`/api/chats/${a.id}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({ json: {
      id: a.id, title: 'Hidden settlement', provider: 'codex',
      messages, total: messages.length, offset: 0, running,
      pending_messages: [], pending_question_id: null,
    } })
  })
  await page.route(new RegExp(`/api/chats/${a.id}/runtime(?:\\?.*)?$`), route => {
    if (!running && messages.length > 1) idleRuntimeReads += 1
    return route.fulfill({ json: { running, pending_messages: [], pending_question_id: null } })
  })
  await page.route(new RegExp(`/api/chats/${a.id}/messages$`), route => {
    const body = route.request().postDataJSON()
    running = true
    const message = { role: 'user', content: body.content, cid: body.cid, ts: 1700001000000 }
    messages = [message]
    return route.fulfill({ status: 202, json: { status: 'started', message } })
  })
  await page.clock.install()
  await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator(`[data-tab-key="chat:${a.id}"]`)
  await expect(surface.getByRole('textbox', { name: 'Message Möbius…' })).toBeVisible()
  await surface.getByRole('textbox', { name: 'Message Möbius…' }).fill('Run the settlement check')
  await page.keyboard.press('Enter')
  await expect(surface.locator('.chat__stop')).toBeVisible()
  await page.waitForFunction(() => typeof window.emitSettlementEvent === 'function')
  await page.evaluate(chatId => {
    window.retainedSettlementRoot = document.querySelector(`[data-tab-key="chat:${chatId}"] .chat`)
  }, a.id)
  expect(await page.evaluate(() => Boolean(window.retainedSettlementRoot))).toBe(true)
  await page.locator('[data-pane-strip="p1"]').getByRole('button', { name: 'Focus pane' }).click()
  await expect(surface).toBeHidden()

  // Freeze interval recovery: only the observed finish + reveal can settle this
  // test, not a later runtime polling tick masking broken event wiring.
  await page.clock.pauseAt(new Date(Date.now() + 1000))
  messages = [...messages, {
    role: 'assistant', content: 'The saved final answer.', ts: 1700001000001,
    blocks: [{ type: 'text', content: 'The saved final answer.' }],
  }]
  running = false
  await page.evaluate(chatId => window.emitSettlementEvent({ type: 'chat_run_finished', chatId }), a.id)
  await page.getByRole('button', { name: 'Show all panes' }).click()
  await expect.poll(() => idleRuntimeReads).toBeGreaterThan(0)
  await expect(surface.getByText('The saved final answer.', { exact: true })).toBeVisible()
  await expect(surface.locator('.chat__stop')).toHaveCount(0)
  await expect(surface.locator('.chat__thinking')).toHaveCount(0)
  expect(await page.evaluate(chatId => (
    window.retainedSettlementRoot === document.querySelector(`[data-tab-key="chat:${chatId}"] .chat`)
  ), a.id)).toBe(true)
})
