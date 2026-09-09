/* Saved close answers never manufacture a model turn or disturb owner intent. */
import { test as base, expect, chromium } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { resolve, sep } from 'node:path'

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
const question = 'Would you like another explanation?'
const draft = 'Keep this unrelated draft'

async function mount(page, { reject = false, loseAck = false, restart = false, activated = false } = {}) {
  page.on('console', msg => { if (msg.type() === 'error') console.error('Fixture console:', msg.text()) })
  page.on('requestfailed', req => console.error('Fixture request failed:', req.url(), req.failure()))
  page.on('pageerror', error => console.error('Fixture page error:', error.message))
  await page.setViewportSize({ width: Number(process.env.MOBIUS_RECOVERY_WIDTH || 1512), height: 911 })
  const block = { type: 'question', question_id: 'quiet-fixture', response_mode: 'continuation', questions: [
    { id: 'help', header: 'Next step', question, options: [
      { id: '0', label: 'No thanks', description: 'Save this answer without another reply.', on_answer: 'close' },
      { id: '1', label: 'Yes please', description: 'Continue with more detail.' },
    ] },
  ] }
  if (restart) {
    block.question_id = 'restart-fixture'
    block.questions = [{ id: 'restart', header: 'Restart', question, options: [
      { id: 'restart-option', label: 'Restart now', description: 'Load the tested changes.' },
      { id: 'defer-option', label: 'Not now', description: 'Wait for a later approved restart.' },
    ] }]
    block.platform_action = { type: 'restart', version: 1, status: activated ? 'activated' : 'awaiting_owner',
      restart_option_id: 'restart-option', cancel_option_id: 'defer-option',
      action_id: 'platform-restart:fixture' }
  }
  const messages = [{ role: 'user', content: 'Original A', ts: 1788800000100 },
    { role: 'assistant', id: 'assistant-a', ts: 1788800000200, blocks: [
      { type: 'text', content: 'Here is the completed explanation.\n\n' + 'A stable answer. '.repeat(70) }, block,
    ] }]
  const attempts = []
  let streams = 0
  const detail = () => ({ id: CHAT, title: 'Quiet answer fixture', provider: 'codex', messages,
    total: messages.length, offset: 0, running: false, pending_messages: [],
    pending_question_id: (block.answers || activated) ? null : block.question_id, active_goal_objective: null,
    recovery_run_id: null, active_assistant_message_id: null, updated_at: '2026-09-09T02:00:00Z' })
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (url.pathname === `${path}/messages` && req.method() === 'POST') {
      const body = req.postDataJSON(); attempts.push(body)
      if (reject && attempts.length === 1) return route.fulfill({ status: 409, json: {
        detail: 'This question is the only next step for an unfinished Goal.' } })
      block.answers = body.answers
      block.answer_turn = 'none'
      if (restart) block.platform_action = { ...block.platform_action, status: 'deferred' }
      if (loseAck && attempts.length === 1) return route.fulfill({ status: 503, json: { detail: 'Acknowledgement unavailable; your choice remains retryable.' } })
      return route.fulfill({ status: 200, json: { status: 'answered', answer_turn: 'none', running: false, answers: block.answers, selected_options: body.selected_options, ...(restart ? { platform_action: block.platform_action } : {}) } })
    }
    if (!['GET', 'HEAD'].includes(req.method())) {
      if (url.pathname.includes('upload')) return route.fulfill({ json: {
        name: 'draft-note.txt', filename: 'draft-note.txt', size: 16,
        mime_type: 'text/plain', url: `${path}/uploads/draft-note.txt` } })
      return route.fulfill({ json: {} }) // No fixture mutation reaches live data.
    }
    if (url.pathname === path || url.pathname === `${path}/runtime`) return route.fulfill({ json: detail() })
    if (url.pathname === `${path}/stream`) { streams++; return route.fulfill({ status: 204, body: '' }) }
    if (url.pathname === '/api/chats') return route.fulfill({ json: [detail()] })
    return route.continue()
  })
  if (process.env.MOBIUS_FIXTURE_DIST) {
    const dist = resolve(process.env.MOBIUS_FIXTURE_DIST)
    await page.route(/\/(?:shell|assets)\//, async route => {
      const pathname = decodeURIComponent(new URL(route.request().url()).pathname)
      const filename = resolve(dist, pathname.replace(/^\/shell\//, '').replace(/^\//, '') || 'index.html')
      if (!filename.startsWith(dist + sep)) return route.abort()
      const contentType = filename.endsWith('.js') ? 'application/javascript'
        : filename.endsWith('.css') ? 'text/css'
          : filename.endsWith('.html') ? 'text/html' : 'application/octet-stream'
      await route.fulfill({ body: await readFile(filename), contentType })
    })
  }
  await page.addInitScript(() => sessionStorage.setItem('mobius:visual-content-only', '1'))
  await page.goto(`${BASE}/shell/?chat=${CHAT}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const card = surface.locator('.qcard')
  await expect(card.getByText(question, { exact: true })).toBeVisible({ timeout: 15000 })
  const composer = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await composer.fill(draft)
  await surface.locator('input[type="file"]').setInputFiles({ name: 'draft-note.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') })
  const attachment = surface.getByRole('button', { name: 'Remove draft-note.txt' })
  await expect(attachment).toBeVisible()
  return { surface, card, composer, attachment, attempts, streams: () => streams }
}

test('quiet acknowledgement preserves draft and attachment without starting a stream', async ({ page }) => {
  const f = await mount(page)
  await f.card.getByRole('radio', { name: /No thanks/ }).click()
  const beforeStreams = f.streams()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  expect(f.attempts).toHaveLength(1)
  expect(f.attempts[0].selected_options).toEqual({ help: ['0'] })
  expect(f.streams()).toBe(beforeStreams)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
  await expect(f.surface.getByText('continue', { exact: true })).toHaveCount(0)
})

test('quiet rejection keeps selected choice and draft retryable with its explanation', async ({ page }) => {
  const f = await mount(page, { reject: true })
  await f.card.getByRole('radio', { name: /No thanks/ }).click()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('status')).toContainText('unfinished Goal')
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  expect(f.attempts).toHaveLength(2)
  expect(f.attempts[1].selected_options).toEqual({ help: ['0'] })
})

test('lost quiet acknowledgement settles from outbox replay without clearing draft', async ({ page }) => {
  const f = await mount(page, { loseAck: true })
  await f.card.getByRole('radio', { name: /No thanks/ }).click()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('status')).toContainText('Acknowledgement unavailable')
  // Existing shell wake delivery owns retries; no synthetic retry timer.
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect.poll(() => f.attempts.length, { timeout: 35000 }).toBeGreaterThanOrEqual(2)
  await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  expect(f.attempts[1].cid).toBe(f.attempts[0].cid)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
})


test('deferred Restart retains its action receipt without a model turn or lost draft', async ({ page }) => {
  const f = await mount(page, { restart: true })
  await f.card.getByRole('radio', { name: /Not now/ }).click()
  const beforeStreams = f.streams()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('button', { name: 'Waiting for a later restart', exact: true })).toBeDisabled()
  expect(f.attempts).toHaveLength(1)
  expect(f.attempts[0].selected_options).toEqual({ restart: ['defer-option'] })
  expect(f.streams()).toBe(beforeStreams)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
  await expect(f.surface.getByText('continue', { exact: true })).toHaveCount(0)
})

test('independent activation settles a Restart card without fabricating an owner answer', async ({ page }) => {
  const f = await mount(page, { restart: true, activated: true })
  await expect(f.card.getByRole('button', { name: 'Changes loaded', exact: true })).toBeDisabled()
  await expect(f.card.locator('[aria-checked="true"]')).toHaveCount(0)
  await expect(f.card.getByRole('button', { name: 'Submit', exact: true })).toHaveCount(0)
  expect(f.attempts).toHaveLength(0)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
})
