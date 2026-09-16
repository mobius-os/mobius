/* Saved close answers never manufacture a model turn or disturb owner intent. */
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import { createMockChatRuntime } from './_mockChatRuntime.mjs'

const BASE = process.env.MOBIUS_URL || process.env.API_BASE_URL || 'http://localhost:8001'
const CHAT = process.env.MOBIUS_RECOVERY_CHAT_ID || 'ffffffff-1111-4222-8333-444444444444'
const path = `/api/chats/${CHAT}`
const question = 'Would you like another explanation?'
const draft = 'Keep this unrelated draft'

async function mount(page, { reject = false, acknowledgement = 'response', restart = null, pauseMessage = false } = {}) {
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
  const restartVersion = restart?.version ?? 2
  const restartStatus = restart?.status ?? 'awaiting_owner'
  if (restart) {
    block.question_id = 'restart-fixture'
    block.questions = [{ id: 'restart', header: 'Restart', question, options: [
      { id: 'restart-option', label: 'Restart now', description: 'Load the tested changes.' },
      ...(restartVersion === 1 ? [{ id: 'defer-option', label: 'Not now', description: 'Wait for a later approved restart.' }] : []),
    ] }]
    block.platform_action = { type: 'restart', version: restartVersion, status: restartStatus,
      restart_option_id: 'restart-option',
      ...(restartVersion === 1 ? { cancel_option_id: 'defer-option' } : {}),
      action_id: 'platform-restart:fixture' }
  }
  const messages = [{ role: 'user', content: 'Original A', ts: 1788800000100 },
    { role: 'assistant', id: 'assistant-a', ts: 1788800000200, blocks: [
      { type: 'text', content: 'Here is the completed explanation.\n\n' + 'A stable answer. '.repeat(70) }, block,
    ] }]
  const unansweredMessages = structuredClone(messages)
  let answerWrites = 0
  const pendingMessages = []
  const initiallyAnswered = restartStatus !== 'awaiting_owner'
  const runtime = createMockChatRuntime({
    pending_question_id: initiallyAnswered ? null : block.question_id,
  })
  const unansweredRuntime = runtime.snapshot()
  const attempts = []
  const mutations = []
  let releaseMessage
  const messageGate = pauseMessage ? new Promise(resolve => { releaseMessage = resolve }) : null
  let streams = 0
  const detail = () => {
    const awaitingReplay = acknowledgement === 'replay' && attempts.length === 1
    const answered = restartStatus !== 'awaiting_owner' || (block.answers && !awaitingReplay)
    const runtimeState = awaitingReplay ? unansweredRuntime : runtime.snapshot()
    return { id: CHAT, title: 'Quiet answer fixture', provider: 'codex',
      messages: awaitingReplay ? unansweredMessages : messages,
      total: messages.length, offset: 0,
      agent_settings_json: { model: 'gpt-6-astra' }, effective: { model: 'gpt-6-astra' },
      ...runtimeState,
      pending_question_id: answered ? null : block.question_id,
      updated_at: '2026-09-09T02:00:00Z' }
  }
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (!['GET', 'HEAD'].includes(req.method())) mutations.push({ method: req.method(), path: url.pathname })
    if (url.pathname === `${path}/messages` && req.method() === 'POST') {
      const body = req.postDataJSON(); attempts.push(body)
      if (!body.answers) {
        if (messageGate) await messageGate
        const pending = { role: 'user', content: body.content, cid: body.cid, ts: Date.now() }
        if (!pendingMessages.some(row => row.cid === body.cid)) {
          pendingMessages.push(pending)
          runtime.update({ pending_messages: [...pendingMessages] })
        }
        return route.fulfill({ status: 202, json: { status: 'queued', pending_message: pending, position: pendingMessages.length } })
      }
      if (reject && attempts.length === 1) return route.fulfill({ status: 409, json: {
        detail: 'This question is the only next step for an unfinished Goal.' } })
      if (!block.answers) answerWrites++
      block.answers = body.answers
      const closesWithoutReply = restart
        ? body.selected_options?.restart?.[0] === 'restart-option'
        : body.selected_options?.help?.[0] === '0'
      block.answer_turn = closesWithoutReply ? 'none' : 'new'
      if (restart) block.platform_action = { ...block.platform_action, status: body.selected_options?.restart?.[0] === 'restart-option' ? 'restart_requested' : 'responded' }
      runtime.update({
        running: !closesWithoutReply,
        pending_question_id: null,
      })
      if (acknowledgement !== 'response' && attempts.length === 1) return route.fulfill({ status: 503, json: { detail: 'Acknowledgement unavailable; your choice remains retryable.' } })
      return route.fulfill({ status: 200, json: { status: closesWithoutReply ? 'answered' : 'started', answer_turn: block.answer_turn, running: !closesWithoutReply, answers: block.answers, selected_options: body.selected_options, ...(restart ? { platform_action: block.platform_action } : {}) } })
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
  await serveRecoveryBuild(page)
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
  return { surface, card, composer, attachment, attempts, mutations, answerWrites: () => answerWrites, releaseMessage: () => releaseMessage?.(), streams: () => streams }
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



for (const acknowledgement of ['detail', 'replay']) {

}

for (const restartVersion of [1, 2]) {


}







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



for (const mode of ['quiet', 'reply', 'restart']) {
  test(`${mode} answer queues on its card offline, then confirms without touching the draft`, async ({ page }) => {
    const f = await mount(page, { restart: mode === 'restart' ? {} : null })
    const network = await disconnectDelivery(page)
    await f.card.getByRole('radio', { name: mode === 'restart' ? /Restart now/ : mode === 'quiet' ? /No thanks/ : /Yes please/ }).click()
    await f.card.getByRole('button', { name: mode === 'restart' ? 'Continue' : 'Submit', exact: true }).click()
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




for (const action of ['edit', 'cancel']) {

}





for (const action of ['edit', 'cancel']) {

}
