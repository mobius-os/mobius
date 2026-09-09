/* Rendered recovery contracts: Resume is acknowledged control, never a queued owner message. */
import { test as base, expect, chromium } from '@playwright/test'

// An authenticated screenshot-helper browser may run these fully intercepted
// fixtures against a live build. No fixture request may mutate the real chat.
const test = process.env.MOBIUS_RECOVERY_CDP ? base.extend({
  page: async ({}, use) => {
    const browser = await chromium.connectOverCDP(process.env.MOBIUS_RECOVERY_CDP)
    // Reuse the helper's authenticated identity, not its retained workspace,
    // service worker, in-flight requests, or prior fixture outbox.
    const authPage = browser.contexts()[0].pages()[0]
    const token = await authPage.evaluate(() => localStorage.getItem('token'))
    if (!token) throw new Error('Run the authenticated screenshot helper first')
    const context = await browser.newContext({
      serviceWorkers: 'block',
      storageState: { cookies: [], origins: [{
        origin: new URL(authPage.url()).origin, localStorage: [{ name: 'token', value: token }],
      }] },
    })
    const page = await context.newPage()
    try { await use(page) } finally {
      await context.close()
      await browser.close()
    }
  },
}) : base
test.use({ serviceWorkers: 'block' })
const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT = process.env.MOBIUS_RECOVERY_CHAT_ID || 'ffffffff-1111-4222-8333-444444444444'
const path = `/api/chats/${CHAT}`
const draft = 'Unrelated draft for a later question'
const queued = { role: 'user', content: 'Queued B stays behind A', cid: 'queued-b', ts: 1788800000500 }
const answer = Array.from({ length: 28 }, (_, i) => `Paragraph ${i + 1}: A is still answering the original question. ${'Saved partial response. '.repeat(6)}`).join('\n\n')
const partial = { id: 'assistant-a', role: 'assistant', ts: 1788800000200, content: answer, blocks: [
  { type: 'text', content: answer },
  { type: 'error', message: 'Interrupted work is ready to resume.', resumable: true },
] }

function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}

async function mount(page, { rejectFirst = false, loseFirstAck = false } = {}) {
  await page.setViewportSize({
    width: Number(process.env.MOBIUS_RECOVERY_WIDTH || 1512),
    height: Number(process.env.MOBIUS_RECOVERY_HEIGHT || 911),
  })
  let resumed = false
  const requested = deferred()
  const accepted = deferred()
  const detailRequested = deferred()
  const detailAccepted = deferred()
  let holdDetail = false
  let rejectDetail = false
  let runtimeGate = null
  let runtimeRequests = 0
  const attempts = []
  const unexpected = []
  const messages = [{ role: 'user', content: 'Original question A', cid: 'original-a', ts: 1788800000100 }, partial]
  const detail = () => ({
    id: CHAT, title: 'Recovery fixture', provider: 'codex', messages,
    total: messages.length, offset: 0, running: resumed, pending_messages: [queued],
    pending_question_id: null, active_goal_objective: null,
    recovery_run_id: resumed ? null : 'interrupted-a',
    active_assistant_message_id: resumed ? 'assistant-resumed-a' : null,
    updated_at: '2026-09-08T17:00:00Z',
  })
  // Block mutations globally, not only the expected Resume request. This also
  // keeps read receipts, preference writes, uploads, and accidental sends local.
  await page.route('**/api/**', async route => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.pathname === `${path}/messages` && req.method() === 'POST') {
      attempts.push(req.postDataJSON())
      requested.resolve()
      if (rejectFirst && attempts.length === 1) return route.fulfill({ status: 409, json: { detail: 'Recovery state changed; retry with current details.' } })
      await accepted.promise
      if (resumed) return route.fulfill({ status: 200, json: {
        status: 'duplicate', running: true,
        message: messages.find(message => message.cid === attempts.at(-1).cid),
      } })
      resumed = true
      const message = { role: 'user', kind: 'continuation', continuation_reason: 'manual',
        content: 'continue', cid: attempts.at(-1).cid, ts: 1788800000600 }
      messages.push(message)
      if (loseFirstAck && attempts.length === 1) return route.abort('connectionreset')
      return route.fulfill({ status: 202, json: { status: 'started', message, run_id: 'resumed-a' } })
    }
    if (req.method() !== 'GET' && req.method() !== 'HEAD') {
      if (url.pathname.includes('upload')) return route.fulfill({ json: {
        name: 'draft-note.txt', filename: 'draft-note.txt', size: 16,
        mime_type: 'text/plain', url: `${path}/uploads/draft-note.txt`,
      } })
      unexpected.push(`${req.method()} ${url.pathname}`)
      return route.fulfill({ json: {} })
    }
    if (url.pathname === path || url.pathname === `${path}/runtime`) {
      if (url.pathname === `${path}/runtime` && runtimeGate) {
        runtimeRequests++
        runtimeGate.requested.resolve()
        await runtimeGate.accepted.promise
      }
      if (holdDetail) { detailRequested.resolve(); await detailAccepted.promise }
      if (rejectDetail) return route.fulfill({ status: 503, json: { detail: 'Fixture transcript unavailable' } })
      return route.fulfill({ json: detail() })
    }
    if (url.pathname === `${path}/stream`) return route.fulfill({ status: 204, body: '' })
    if (url.pathname === '/api/chats') return route.fulfill({ json: [detail()] })
    return route.continue()
  })
  await page.addInitScript(({chatPath}) => {
    sessionStorage.setItem('mobius:visual-content-only', '1')
    const realFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = new URL(String(input?.url || input), location.href)
      if (url.pathname !== `${chatPath}/stream`) return realFetch(input, init)
      if (window.__recoveryNoStream) return Promise.resolve(new Response(null, { status: 204 }))
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          window.__recoveryStream = controller
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({type:'stream_snapshot', assistant_message_id:'assistant-resumed-a', items:[]})}\n\n`))
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({type:'catch_up_done'})}\n\n`))
        },
      }), { status:200, headers:{'Content-Type':'text/event-stream'} }))
    }
  }, {chatPath:path})
  if (page.url() !== 'about:blank') await page.evaluate(() => sessionStorage.clear())
  await page.goto(`${BASE}/shell/?chat=${CHAT}`, { waitUntil: 'domcontentloaded' })
  await page.bringToFront()
  const surface = page.locator('[data-chat-surface="painted"]')
  await expect(surface.getByRole('button', { name: 'Resume', exact: true })).toBeVisible({ timeout: 15000 })
  const composer = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await composer.fill(draft)
  await surface.locator('input[type="file"]').setInputFiles({ name: 'draft-note.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') })
  const attachment = surface.getByRole('button', { name: 'Remove draft-note.txt' })
  await expect(attachment).toBeVisible()
  await expect.poll(() => page.evaluate(id => {
    try { return JSON.parse(sessionStorage.getItem(`draft:${id}`)).attachments?.length } catch { return 0 }
  }, CHAT)).toBe(1)
  return { surface, composer, attachment, requested, accepted, attempts, unexpected,
    detailRequested, detailAccepted,
    holdRuntime() {
      runtimeRequests = 0
      runtimeGate = { requested: deferred(), accepted: deferred() }
      return runtimeGate
    },
    runtimeRequestCount: () => runtimeRequests,
    allowDetail() { rejectDetail = false },
    parkWithDetail(text, { reject = false } = {}) {
      rejectDetail = reject
      resumed = false
      holdDetail = true
      messages.push({ id: 'assistant-resumed-a', role: 'assistant', ts: 1788800000700,
        content: text, blocks: [{ type: 'text', content: text },
          { type: 'error', message: 'Interrupted work is ready to resume.', resumable: true }] })
    },
  }
}

async function sampleGeometry(page, prefix = 'Paragraph 28:') {
  return page.evaluate(prefix => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const response = [...scroll.querySelectorAll('p')].find(p => p.textContent.startsWith(prefix))
    return { top: scroll.scrollTop, height: scroll.scrollHeight, anchor: response.getBoundingClientRect().top }
  }, prefix)
}

test('Resume waits for acknowledgement without changing draft, attachment, queue, or reading position', async ({ page }) => {
  const state = await mount(page)
  const resume = state.surface.getByRole('button', { name: 'Resume', exact: true })
  await resume.evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
  await state.surface.locator('.chat__scroll').hover()
  const beforeWheel = await state.surface.locator('.chat__scroll').evaluate(element => element.scrollTop)
  // Establish reader hold without moving the nearby Resume control beneath
  // the floating composer/attachment tray before the action even starts.
  await page.mouse.wheel(0, -8)
  await expect.poll(() => state.surface.locator('.chat__scroll').evaluate(element => element.scrollTop)).toBeLessThan(beforeWheel)
  await page.evaluate(() => new Promise(resolve => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    let previous = null, stable = 0
    const frame = () => {
      stable = scroll.scrollTop === previous ? stable + 1 : 0
      previous = scroll.scrollTop
      if (stable >= 8) resolve(); else requestAnimationFrame(frame)
    }
    requestAnimationFrame(frame)
  }))
  // Paint sampling catches a one-frame collapse that a final coordinate alone misses.
  await page.evaluate(() => {
    window.__recoveryFrames = []
    window.__sampleRecovery = true
    const sample = () => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const anchor = [...scroll.querySelectorAll('p')].find(p => p.textContent.startsWith('Paragraph 28:'))
      window.__recoveryFrames.push({ top: scroll.scrollTop, height: scroll.scrollHeight,
        anchor: anchor?.getBoundingClientRect().top, resumed: document.body.textContent.includes('Resumed manually') })
      if (window.__sampleRecovery) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })
  const before = await sampleGeometry(page)

  const button = await resume.boundingBox()
  const target = await resume.evaluate(element => {
    const box = element.getBoundingClientRect()
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    return { receivesPointer: element.contains(hit), box: box.toJSON(), blocker: hit?.className }
  })
  expect(target.receivesPointer, JSON.stringify(target)).toBe(true)
  await page.mouse.click(button.x + button.width / 2, button.y + button.height / 2)
  await state.requested.promise
  await expect(state.surface.getByRole('button', { name: 'Resuming…', exact: true })).toBeDisabled()
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
  await expect(state.surface.getByText('continue', { exact: true })).toHaveCount(0)
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toHaveCount(0)
  const pending = await sampleGeometry(page)

  expect(Math.abs(pending.top - before.top)).toBeLessThanOrEqual(1)
  expect(Math.abs(pending.anchor - before.anchor)).toBeLessThanOrEqual(1)
  expect(state.attempts).toHaveLength(1)
  expect(state.attempts[0]).toMatchObject({ continuation: 'manual', resume_run_id: 'interrupted-a' })
  expect(state.attempts[0].attachments || []).toHaveLength(0)
  state.accepted.resolve()
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toBeVisible()
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
  await expect(state.surface.getByText('continue', { exact: true })).toHaveCount(0)
  // The marker may legitimately replace the taller recovery card; the reader's
  // response anchor must stay put rather than jumping to a synthetic send pin.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const frames = await page.evaluate(() => { window.__sampleRecovery = false; return window.__recoveryFrames })

  expect(frames.length).toBeGreaterThan(0)
  expect(frames.every(frame => Math.abs(frame.anchor - before.anchor) <= 1)).toBe(true)
})


test('a rejected Resume leaves recovery retryable without changing owner intent', async ({ page }) => {
  const state = await mount(page, { rejectFirst: true })
  await state.surface.getByRole('button', { name: 'Resume', exact: true }).click()
  await expect(state.surface.getByText('Could not confirm Resume. Your draft and queued messages are unchanged. Try again.', { exact: true })).toBeVisible()
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toHaveCount(0)
  await expect(state.surface.getByText('continue', { exact: true })).toHaveCount(0)
  await state.surface.getByRole('button', { name: 'Resume', exact: true }).click()
  await expect.poll(() => state.attempts.length).toBe(2)
  await expect(state.surface.getByRole('button', { name: 'Resuming…', exact: true })).toBeDisabled()
  expect(state.attempts[1]).toMatchObject({ continuation: 'manual', resume_run_id: 'interrupted-a' })
  expect(state.attempts[1].attachments || []).toHaveLength(0)
  state.accepted.resolve()
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toBeVisible()
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
})


test('a delayed no-stream reconciliation keeps the partial answer painted until detail replaces it', async ({ page }) => {
  const state = await mount(page)
  await state.surface.getByRole('button', { name: 'Resume', exact: true }).click()
  await state.requested.promise
  state.accepted.resolve()
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toBeVisible()
  const liveText = answer.replaceAll('Paragraph', 'Recovered paragraph')
  await page.waitForFunction(() => !!window.__recoveryStream)
  await page.evaluate(content => {
    window.__recoveryStream.enqueue(new TextEncoder().encode(
      `data: ${JSON.stringify({ type: 'text_final', content, text_item_id: 'resumed-text' })}\n\n`,
    ))
  }, liveText)
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toBeVisible()
  await state.surface.getByText(/^Recovered paragraph 28:/).scrollIntoViewIfNeeded()
  await state.surface.locator('.chat__scroll').hover()
  const beforeWheel = await state.surface.locator('.chat__scroll').evaluate(element => element.scrollTop)
  // Establish reader hold without moving the nearby Resume control beneath
  // the floating composer/attachment tray before the action even starts.
  await page.mouse.wheel(0, -8)
  await expect.poll(() => state.surface.locator('.chat__scroll').evaluate(element => element.scrollTop)).toBeLessThan(beforeWheel)
  await page.evaluate(() => new Promise(resolve => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    let previous = null, stable = 0
    const frame = () => {
      stable = scroll.scrollTop === previous ? stable + 1 : 0
      previous = scroll.scrollTop
      if (stable >= 8) resolve(); else requestAnimationFrame(frame)
    }
    requestAnimationFrame(frame)
  }))
  const before = await sampleGeometry(page, 'Recovered paragraph 28:')
  state.parkWithDetail(liveText)
  await page.evaluate(() => {
    window.__recoveryNoStream = true
    window.__recoveryStream.close()
  })
  await state.detailRequested.promise
  // The authoritative detail read is deliberately unresolved here. Its absence
  // must not clear the stream row, its answer, or the reader's held position.
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toBeVisible()
  const waiting = await sampleGeometry(page, 'Recovered paragraph 28:')
  expect(Math.abs(waiting.anchor - before.anchor)).toBeLessThanOrEqual(1)
  // A reconnect note may add height; the partial answer must not collapse.
  expect(waiting.height).toBeGreaterThanOrEqual(before.height)
  state.detailAccepted.resolve()
  await expect(state.surface.getByRole('button', { name: 'Resume', exact: true })).toBeVisible()
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toHaveCount(1)
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
  const after = await sampleGeometry(page, 'Recovered paragraph 28:')
  expect(Math.abs(after.anchor - before.anchor)).toBeLessThanOrEqual(1)
})


test('a lost Resume acknowledgement retries the same control without exposing continue or consuming B', async ({ page }) => {
  const state = await mount(page, { loseFirstAck: true })
  await state.surface.getByRole('button', { name: 'Resume', exact: true }).click()
  await state.requested.promise
  state.accepted.resolve()
  await expect.poll(() => state.attempts.length).toBeGreaterThanOrEqual(2)
  expect(state.attempts.every(attempt => JSON.stringify(attempt) === JSON.stringify(state.attempts[0]))).toBe(true)
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toHaveCount(1)
  await expect(state.surface.getByText('continue', { exact: true })).toHaveCount(0)
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
})


test('failed transcript replacement retains the answer and a successful Retry reconciles it once', async ({ page }) => {
  const state = await mount(page)
  await state.surface.getByRole('button', { name: 'Resume', exact: true }).click()
  await state.requested.promise
  state.accepted.resolve()
  await expect(state.surface.getByText('Resumed manually', { exact: true })).toBeVisible()
  const liveText = answer.replaceAll('Paragraph', 'Recovered paragraph')
  await page.waitForFunction(() => !!window.__recoveryStream)
  await page.evaluate(content => {
    window.__recoveryStream.enqueue(new TextEncoder().encode(
      `data: ${JSON.stringify({ type: 'text_final', content, text_item_id: 'resumed-text' })}\n\n`,
    ))
  }, liveText)
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toBeVisible()
  state.parkWithDetail(liveText, { reject: true })
  await page.evaluate(() => {
    window.__recoveryNoStream = true
    window.__recoveryStream.close()
  })
  await state.detailRequested.promise
  state.detailAccepted.resolve()
  const retry = state.surface.getByRole('button', { name: 'Retry', exact: true })
  await expect(retry).toBeVisible()
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toHaveCount(1)
  await expect(state.composer).toHaveValue(draft)
  await expect(state.attachment).toBeVisible()
  state.allowDetail()
  await retry.click()
  await expect(state.surface.getByRole('button', { name: 'Resume', exact: true })).toBeVisible()
  await expect(state.surface.getByText(/^Recovered paragraph 28:/)).toHaveCount(1)
  await expect(state.surface.getByText('Queued B stays behind A', { exact: true })).toBeVisible()
  await expect(retry).toHaveCount(0)
})


test('foreground and active-queue refreshes share one bounded runtime read', async ({ page }) => {
  const fixture = await mount(page)
  const gate = fixture.holdRuntime()
  const wake = () => page.evaluate(() => {
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event('pageshow'))
    window.dispatchEvent(new Event('online'))
    document.dispatchEvent(new Event('visibilitychange'))
  })
  try {
    await wake()
    await gate.requested.promise
    await wake()
    // Keep the response held across a real queue-poll interval as well.
    await page.waitForTimeout(1200)
    expect(fixture.runtimeRequestCount()).toBe(1)
    await expect(fixture.composer).toHaveValue(draft)
    await expect(fixture.attachment).toBeVisible()
    await expect(fixture.surface.getByText(queued.content, { exact: true })).toBeVisible()
    expect(fixture.attempts).toHaveLength(0)
  } finally { gate.accepted.resolve() }
})
