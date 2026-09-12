/* Saved close answers never manufacture a model turn or disturb owner intent. */
import { test as base, expect, chromium } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { resolve, sep } from 'node:path'

// An authenticated screenshot-helper browser may run these fully intercepted
// fixtures against a live build. No fixture request may mutate the real chat.
const test = process.env.MOBIUS_RECOVERY_CDP ? base.extend({
  page: async ({}, use) => {
    const browser = await chromium.connectOverCDP(process.env.MOBIUS_RECOVERY_CDP)
    // Export standard Playwright auth state from the authenticated screenshot
    // helper before starting fixtures. Never sample changing/closing test tabs.
    if (!process.env.MOBIUS_RECOVERY_AUTH_STATE) {
      throw new Error('Provide the screenshot helper auth export in MOBIUS_RECOVERY_AUTH_STATE')
    }
    const context = await browser.newContext({
      serviceWorkers: 'block',
      storageState: process.env.MOBIUS_RECOVERY_AUTH_STATE,
    })
    const page = await context.newPage()
    try { await use(page) } finally {
      await context.close()
      await browser.close()
    }
  },
}) : base
test.use({ serviceWorkers: 'block' })

const BASE = process.env.MOBIUS_URL || process.env.API_BASE_URL || 'http://localhost:8001'
const CHAT = process.env.MOBIUS_RECOVERY_CHAT_ID || 'ffffffff-1111-4222-8333-444444444444'
const path = `/api/chats/${CHAT}`
const question = 'Would you like another explanation?'
const draft = 'Keep this unrelated draft'

async function mount(page, { reject = false, loseAck = false, restart = false, activated = false, pauseMessage = false } = {}) {
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
  const pendingMessages = []
  const attempts = []
  const mutations = []
  let releaseMessage
  const messageGate = pauseMessage ? new Promise(resolve => { releaseMessage = resolve }) : null
  let streams = 0
  const detail = () => ({ id: CHAT, title: 'Quiet answer fixture', provider: 'codex', messages,
    total: messages.length, offset: 0, running: false, pending_messages: pendingMessages,
    agent_settings_json: { model: 'gpt-6-astra' }, effective: { model: 'gpt-6-astra' },
    pending_question_id: (block.answers || activated) ? null : block.question_id, active_goal_objective: null,
    recovery_run_id: null, active_assistant_message_id: null, updated_at: '2026-09-09T02:00:00Z' })
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (!['GET', 'HEAD'].includes(req.method())) mutations.push({ method: req.method(), path: url.pathname })
    if (url.pathname === `${path}/messages` && req.method() === 'POST') {
      const body = req.postDataJSON(); attempts.push(body)
      if (!body.answers) {
        if (messageGate) await messageGate
        const pending = { role: 'user', content: body.content, cid: body.cid, ts: Date.now() }
        if (!pendingMessages.some(row => row.cid === body.cid)) pendingMessages.push(pending)
        return route.fulfill({ status: 202, json: { status: 'queued', pending_message: pending, position: pendingMessages.length } })
      }
      if (reject && attempts.length === 1) return route.fulfill({ status: 409, json: {
        detail: 'This question is the only next step for an unfinished Goal.' } })
      block.answers = body.answers
      block.answer_turn = 'none'
      if (restart) block.platform_action = { ...block.platform_action, status: body.selected_options?.restart?.[0] === 'defer-option' ? 'deferred' : 'restart_requested' }
      if (loseAck && attempts.length === 1) return route.fulfill({ status: 503, json: { detail: 'Acknowledgement unavailable; your choice remains retryable.' } })
      return route.fulfill({ status: 200, json: { status: 'answered', answer_turn: 'none', running: false, answers: block.answers, selected_options: body.selected_options, ...(restart ? { platform_action: block.platform_action } : {}) } })
    }
    if (!['GET', 'HEAD'].includes(req.method())) {
      if (url.pathname.includes('upload')) return route.fulfill({ json: {
        name: 'draft-note.txt', filename: 'draft-note.txt', size: 16,
        mime_type: 'text/plain', url: `${path}/uploads/draft-note.txt` } })
      return route.fulfill({ json: {} }) // No fixture mutation reaches live data.
    }
    if (url.pathname === '/api/ready') return route.fulfill({ json: { ready: true, boot_id: 'fixture-ready-boot' } })
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
  return { surface, card, composer, attachment, attempts, mutations, releaseMessage: () => releaseMessage?.(), streams: () => streams }
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
  // Confirmation may already arrive through authoritative detail before the
  // local queued label can be observed. Offline cases below own that hold;
  // this case verifies the same-cid replay and final saved answer.
  // Existing shell wake delivery owns retries; no synthetic retry timer.
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect.poll(() => f.attempts.length, { timeout: 35000 }).toBeGreaterThanOrEqual(2)
  await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  expect(f.attempts[1].cid).toBe(f.attempts[0].cid)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
})


test('Restart submits its exact action identity without a model turn or lost draft', async ({ page }) => {
  const f = await mount(page, { restart: true })
  await f.card.getByRole('radio', { name: /Restart now/ }).click()
  const beforeStreams = f.streams()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('status')).toContainText('Restart requested')
  expect(f.attempts).toHaveLength(1)
  expect(f.attempts[0].selected_options).toEqual({ restart: ['restart-option'] })
  expect(f.streams()).toBe(beforeStreams)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
  await expect(f.surface.getByText('continue', { exact: true })).toHaveCount(0)
})

test('deferred Restart retains its action receipt without a model turn or lost draft', async ({ page }) => {
  const f = await mount(page, { restart: true })
  await f.card.getByRole('radio', { name: /Not now/ }).click()
  const beforeStreams = f.streams()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('status')).toContainText('Waiting for a later restart')
  expect(f.attempts).toHaveLength(1)
  expect(f.attempts[0].selected_options).toEqual({ restart: ['defer-option'] })
  expect(f.streams()).toBe(beforeStreams)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
  await expect(f.surface.getByText('continue', { exact: true })).toHaveCount(0)
})

test('independent activation settles a Restart card without fabricating an owner answer', async ({ page }) => {
  const f = await mount(page, { restart: true, activated: true })
  await expect(f.card.getByRole('status')).toContainText('Changes loaded')
  await expect(f.card.locator('[aria-checked="true"]')).toHaveCount(0)
  await expect(f.card.getByRole('button', { name: 'Submit', exact: true })).toHaveCount(0)
  expect(f.attempts).toHaveLength(0)
  await expect(f.composer).toHaveValue(draft)
  await expect(f.attachment).toBeVisible()
})

async function disconnectDelivery(page) {
  let postAttempts = 0
  const blockNetwork = async route => {
    const request = route.request()
    if (request.method() === 'POST' && new URL(request.url()).pathname === `${path}/messages`) {
      postAttempts++
      return route.abort('internetdisconnected')
    }
    if (['/api/ready', '/api/health'].includes(new URL(request.url()).pathname)) return route.abort('internetdisconnected')
    return route.fallback()
  }
  await page.route('**/api/**', blockNetwork)
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false })
    window.dispatchEvent(new Event('offline'))
  })
  const status = page.locator('.shell__connection-status')
  await expect(status).toContainText('Offline', { timeout: 15000 })
  await expect(status).toBeVisible()
  const box = await status.boundingBox()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.y).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize().width)
  return { attempts: () => postAttempts, reconnect: async () => {
    await page.unroute('**/api/**', blockNetwork)
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true })
      window.dispatchEvent(new Event('online'))
    })
  } }
}

test('known-offline fresh send stays in its local queue without POST or transcript movement', async ({ page }) => {
  const f = await mount(page)
  await f.card.getByRole('radio', { name: /No thanks/ }).click()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  await f.attachment.click()
  const marker = 'OFFLINE-QUEUED-FOLLOWUP'
  await f.composer.focus()
  await f.composer.fill(marker)
  await expect(f.composer).toHaveValue(marker)
  const network = await disconnectDelivery(page)
  await page.evaluate(marker => {
    window.__offlineTrace = []
    const sample = () => {
      // Exclude the inert offscreen restoration/measurement copy; only the
      // painted chat is a user-visible transcript/queue.
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const users = [...(surface?.querySelectorAll('.chat__msg--user') || [])].filter(e => e.textContent.includes(marker))
      const queued = [...(surface?.querySelectorAll('.queued__row') || [])].filter(e => e.textContent.includes(marker))
      window.__offlineTrace.push({ users: users.length, queued: queued.length })
    }
    window.__offlineObserver = new MutationObserver(sample)
    window.__offlineObserver.observe(document.body, { childList: true, subtree: true, attributes: true })
    sample()
  }, marker)
  const readingTop = await f.card.evaluate(element => element.getBoundingClientRect().top)
  await f.surface.getByRole('button', { name: 'Send', exact: true }).click()
  const row = f.surface.locator('.queued__row').filter({ hasText: marker })
  await expect(row).toBeVisible()
  await expect(f.composer).toHaveValue('')
  const queuedTop = await f.card.evaluate(element => element.getBoundingClientRect().top)
  expect(Math.abs(queuedTop - readingTop)).toBeLessThanOrEqual(2)
  expect(network.attempts()).toBe(0)
  // A focus/runtime reconciliation must not mistake a local outbox row for a
  // server row that disappeared. Reconnect then acknowledges that same cid.
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(row).toBeVisible()
  await network.reconnect()
  await expect.poll(() => f.attempts.length).toBe(2)
  await expect(row).toBeVisible()
  const trace = await page.evaluate(() => {
    window.__offlineObserver.disconnect()
    return window.__offlineTrace
  })
  console.log('OFFLINE_PRESENTATION_TRACE', JSON.stringify(trace.filter((frame,i) => i === 0 || JSON.stringify(frame) !== JSON.stringify(trace[i-1]))))
  expect(trace.some(frame => frame.users > 0)).toBe(false)
  const firstQueued = trace.findIndex(frame => frame.queued === 1)
  expect(firstQueued).toBeGreaterThanOrEqual(0)
  expect(trace.slice(firstQueued).every(frame => frame.queued === 1)).toBe(true)
  expect(f.attempts[1].cid).toBeTruthy()
})

for (const mode of ['quiet', 'reply', 'restart']) {
  test(`${mode} answer queues on its card offline, then confirms without touching the draft`, async ({ page }) => {
    const f = await mount(page, { restart: mode === 'restart' })
    const network = await disconnectDelivery(page)
    await f.card.getByRole('radio', { name: mode === 'restart' ? /Restart now/ : mode === 'quiet' ? /No thanks/ : /Yes please/ }).click()
    await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
    await expect(f.card.getByRole('button', { name: 'Queued on this device' })).toBeDisabled()
    await expect(f.card.getByRole('status')).toContainText('saved here')
    expect(network.attempts()).toBe(0)
    expect(f.attempts).toHaveLength(0)
    await expect(f.surface.locator('.queued__row')).toHaveCount(0)
    await expect(f.surface.getByText('continue', { exact: true })).toHaveCount(0)
    await expect(f.composer).toHaveValue(draft)
    await expect(f.attachment).toBeVisible()
    if (mode === 'quiet') {
      // Durable local answer ownership survives a document replacement; it is
      // not a submitted flag trapped in the old QuestionCard instance.
      await page.reload({ waitUntil: 'domcontentloaded' })
      await expect(f.card.getByRole('button', { name: 'Queued on this device' })).toBeDisabled()
      await expect(f.card.getByRole('radio', { name: /No thanks/ })).toHaveAttribute('aria-checked', 'true')
      expect(f.attempts).toHaveLength(0)
    }
    await network.reconnect()
    await expect.poll(() => f.attempts.length).toBe(1)
    if (mode === 'restart') await expect(f.card.getByRole('status')).toContainText('Restart requested')
    else await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeDisabled()
    await expect(f.composer).toHaveValue(draft)
    await expect(f.attachment).toBeVisible()
  })
}

test('a follow-up can queue behind a locally saved answer without clearing its barrier', async ({ page }) => {
  const f = await mount(page)
  const network = await disconnectDelivery(page)
  await f.card.getByRole('radio', { name: /Yes please/ }).click()
  await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
  await expect(f.card.getByRole('button', { name: 'Queued on this device' })).toBeDisabled()
  await f.attachment.click()
  await f.composer.focus()
  await f.composer.fill('FOLLOWUP-B-AFTER-LOCAL-ANSWER')
  await expect(f.composer).toHaveValue('FOLLOWUP-B-AFTER-LOCAL-ANSWER')
  await f.surface.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(f.surface.locator('.queued__row')).toContainText('FOLLOWUP-B-AFTER-LOCAL-ANSWER')
  await expect(f.card.getByRole('button', { name: 'Queued on this device' })).toBeDisabled()
  expect(network.attempts()).toBe(0)
  expect(f.attempts).toHaveLength(0)
  await network.reconnect()
  await expect.poll(() => f.attempts.length).toBe(2)
  expect(f.attempts[0].answers).toEqual({ [question]: 'Yes please' })
  expect(f.attempts[1].content).toBe('FOLLOWUP-B-AFTER-LOCAL-ANSWER')
  await expect(f.surface.locator('.queued__row')).toContainText('FOLLOWUP-B-AFTER-LOCAL-ANSWER')
})


for (const action of ['edit', 'cancel']) {
  test(`a never-dispatched local message can ${action} offline without a server mutation`, async ({ page }) => {
    const f = await mount(page)
    await f.card.getByRole('radio', { name: /No thanks/ }).click()
    await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
    await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
    const marker = 'LOCAL-CONTROL-MESSAGE'
    await f.composer.focus()
    await f.composer.fill(marker)
    const network = await disconnectDelivery(page)
    await f.surface.getByRole('button', { name: 'Send', exact: true }).click()
    const row = f.surface.locator('.queued__row')
    await expect(row).toHaveCount(1)
    if (action === 'edit') {
      await row.getByRole('button', { name: 'Edit queued message' }).click()
      await row.getByRole('textbox', { name: 'Edit queued message' }).fill('REVISED-LOCAL-MESSAGE')
      await row.getByRole('button', { name: 'Save queued message edit' }).click()
      await expect(row).toContainText('REVISED-LOCAL-MESSAGE')
      await expect(row.getByRole('textbox')).toHaveCount(0)
    } else {
      await row.getByRole('button', { name: 'Cancel queued message' }).click()
      await expect(row).toHaveCount(0)
    }
    expect(network.attempts()).toBe(0)
    expect(f.mutations.some(request => request.path.includes('/pending/'))).toBe(false)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
    if (action === 'edit') await expect(row).toContainText('REVISED-LOCAL-MESSAGE')
    else await expect(row).toHaveCount(0)
    await network.reconnect()
    if (action === 'edit') {
      await expect.poll(() => f.attempts.length).toBe(2)
      expect(f.attempts[1].content).toBe('REVISED-LOCAL-MESSAGE')
      expect(f.attempts[1].attachments).toHaveLength(1)
    } else {
      await expect(page.locator('.shell__connection-status')).toHaveCount(0)
      expect(f.attempts).toHaveLength(1)
      await expect(row).toHaveCount(0)
    }
  })
}


test('cancellation cannot overtake a claimed background delivery', async ({ page }) => {
  const f = await mount(page, { pauseMessage: true })
  try {
    await f.card.getByRole('radio', { name: /No thanks/ }).click()
    await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
    await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
    await f.composer.focus()
    await f.composer.fill('DELIVERY-IN-FLIGHT')
    const network = await disconnectDelivery(page)
    await f.surface.getByRole('button', { name: 'Send', exact: true }).click()
    const row = f.surface.locator('.queued__row')
    await expect(row).toHaveCount(1)
    await network.reconnect()
    await expect.poll(() => f.attempts.length).toBe(2)
    await row.getByRole('button', { name: 'Cancel queued message' }).click()
    await expect(f.surface.getByText('Delivery is not confirmed yet. Try cancelling after delivery is confirmed.')).toBeVisible()
    expect(f.mutations.some(request => request.method === 'DELETE')).toBe(false)
    await expect(row).toHaveCount(1)
    f.releaseMessage()
    await expect(row).toContainText('DELIVERY-IN-FLIGHT')
  } finally { f.releaseMessage() }
})


for (const action of ['edit', 'cancel']) {
  test(`a busy replay owner prevents ${action} from racing a server-confirmed queue row`, async ({ page }) => {
    const f = await mount(page)
    await f.card.getByRole('radio', { name: /No thanks/ }).click()
    await f.card.getByRole('button', { name: 'Submit', exact: true }).click()
    await expect(f.card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
    await f.composer.focus()
    await f.composer.fill('CONFIRMED-QUEUE-MESSAGE')
    await f.surface.getByRole('button', { name: 'Send', exact: true }).click()
    const row = f.surface.locator('.queued__row')
    await expect(row).toHaveCount(1)
    await expect.poll(() => f.attempts.length).toBe(2)
    // Model an older tab which has captured a body while holding the existing
    // replay lock. Even canonical serverTs is not permission to overtake it.
    await page.evaluate(() => new Promise(resolve => {
      navigator.locks.request('mobius-chat-outbox', () => new Promise(release => {
        window.releaseFixtureReplayLock = release
        resolve()
      }))
    }))
    try {
      if (action === 'edit') {
        await row.getByRole('button', { name: 'Edit queued message' }).click()
        await row.getByRole('textbox', { name: 'Edit queued message' }).fill('UNCONFIRMED-EDIT')
        await row.getByRole('button', { name: 'Save queued message edit' }).click()
        await expect(row.getByRole('textbox', { name: 'Edit queued message' })).toHaveValue('UNCONFIRMED-EDIT')
        await expect(row.getByRole('status')).toBeVisible()
      } else {
        await row.getByRole('button', { name: 'Cancel queued message' }).click()
        await expect(f.surface.getByText('Delivery is not confirmed yet. Try cancelling after delivery is confirmed.')).toBeVisible()
      }
      expect(f.mutations.some(request => request.path.includes('/pending/'))).toBe(false)
      await expect(row).toHaveCount(1)
    } finally { await page.evaluate(() => window.releaseFixtureReplayLock()) }
  })
}
