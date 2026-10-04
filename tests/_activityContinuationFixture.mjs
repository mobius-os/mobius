/** Provider-free live cuts exercise the real activity renderer without sending messages. */
import { expect } from '@playwright/test'
import { runtimeSnapshot, testChatAgentSettings, mockDeliveryReady } from './_chatTestPrerequisites.mjs'

export async function checkActivityContinuation(page, chat, base, { compact = false } = {}) {
  const root = `rt-activity-${compact ? 'compact' : 'raw'}`
  const firstId = `${root}:assistant:1`, nextId = `${root}:assistant:2`
  const tool = (id, status = 'done') => ({ type: 'tool', tool: 'Bash', input: `echo ${id}`, output: `${id} output`, tool_use_id: id, status })
  const originals = [tool('inspect'), tool('test')]
  const saved = compact ? [{ type: 'activity', activity_id: 'activity-saved', start: 0, end: 2,
    message_index: 1, tool_count: 2, entries: originals.map((item, idx) => ({ item, idx })) }] : originals
  const carrier = { role: 'user', hidden: true, steered: true, kind: 'delegation_result',
    source_work_id: 'logical-goal-root', cid: 'helper-cut', content: 'Hidden result', ts: 3 }
  let messages = [{ role: 'user', cid: 'activity-request', content: 'Verify continuous activity', ts: 1 },
    { role: 'assistant', id: firstId, content: '', blocks: saved, ts: 2 }]
  let runtime = runtimeSnapshot({ running: true, run_status: 'running', run_id: root,
    active_assistant_message_id: firstId, runtime_revision: 10000000 })
  if (compact) {
    messages.push(carrier)
    runtime.active_assistant_message_id = nextId
  }
  let detailReads = 0
  await mockDeliveryReady(page)
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => route.fulfill({ json: {
    ...chat, provider: 'codex', ...testChatAgentSettings(), ...runtime, messages, total: messages.length, offset: 0,
  } }))
  await page.route(`**/api/chats/${chat.id}/runtime`, route => route.fulfill({ json: runtime }))
  await page.route(`**/api/chats/${chat.id}/activity*`, route => {
    if (route.request().url().includes('/activity-detail')) {
      detailReads++
      return route.fulfill({ json: { entries: originals.map((item, idx) => ({ item, idx })) } })
    }
    return route.fulfill({ json: { events: [], next_before: null } })
  })
  await page.route(`**/api/chats/${chat.id}/messages`, route => route.abort())
  await page.addInitScript(({ chatId, firstId, originals }) => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, options) => {
      const url = typeof input === 'string' ? input : input.url
      if (!url.includes(`/api/chats/${chatId}/stream`)) return nativeFetch(input, options)
      return Promise.resolve(new Response(new ReadableStream({ start(controller) {
        const emit = event => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
        window.__emitActivityFixture = emit
        emit({ type: 'stream_snapshot', assistant_message_id: firstId, items: originals })
        emit({ type: 'catch_up_done' })
      } }), { headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId: chat.id, firstId: compact ? nextId : firstId, originals: compact ? [] : originals })
  await page.goto(`${base}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => typeof window.__emitActivityFixture === 'function')
  const surface = page.locator('[data-chat-surface="painted"]')
  const headers = surface.locator('.chat__tools > .chat__activity > .chat__activity-header[aria-expanded]')
  await expect(headers).toHaveCount(1)
  if (await headers.getAttribute('aria-expanded') !== 'true') await headers.click()
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await headers.evaluate(element => { window.__activityHeader = element })
  if (!compact) messages = [...messages, carrier]
  runtime = { ...runtime, active_assistant_message_id: nextId, runtime_revision: runtime.runtime_revision + 1 }
  if (!compact) await page.evaluate(({ firstId, nextId, originals, carrier }) => window.__emitActivityFixture({
    type: 'steered_into_turn', assistant_message_id: firstId, next_assistant_message_id: nextId,
    sealed_items: originals, items: [], messages: [carrier], ts: carrier.ts,
  }), { firstId, nextId, originals, carrier })
  const current = [tool('continue', 'running')]
  await page.evaluate(current => {
    for (const item of current) window.__emitActivityFixture({ ...item, type: 'tool_start' })
  }, current)
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await expect(headers).toHaveAttribute('aria-label', /in progress/)
  await expect(surface.getByText(/echo continue/, { exact: false }).first()).toBeVisible()
  expect(await headers.evaluate(element => element === window.__activityHeader)).toBe(true)
  // A saved partial is older than the selected stream. Returning to saved
  // source must not hide or duplicate the current tool on the way to done.
  const readsBeforeAppend = detailReads
  const finalTools = [tool('continue'), tool('verify')]
  messages = [...messages, { role: 'assistant', id: nextId, content: '', blocks: finalTools, ts: 4 }]
  await page.evaluate(() => {
    window.__emitActivityFixture({ type: 'tool_end', tool_use_id: 'continue' })
    window.__emitActivityFixture({ type: 'tool_start', tool: 'Bash', input: 'echo verify', tool_use_id: 'verify' })
    window.__emitActivityFixture({ type: 'tool_end', tool_use_id: 'verify' })
  })
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  expect(await headers.evaluate(element => element === window.__activityHeader)).toBe(true)
  expect(detailReads).toBe(readsBeforeAppend)
  await page.evaluate(() => window.__emitActivityFixture({ type: 'text', content: 'Verified, with every step preserved.', text_item_id: 'result' }))
  await expect(surface.getByText('Verified, with every step preserved.', { exact: true })).toBeVisible()
  await expect(headers).not.toHaveAttribute('aria-label', /in progress/)
  expect(detailReads).toBe(readsBeforeAppend)
  messages.at(-1).blocks.push({ type: 'text', content: 'Verified, with every step preserved.' })
  runtime = { ...runtime, running: false, run_status: 'completed', runtime_revision: runtime.runtime_revision + 1 }
  await page.evaluate(() => window.__emitActivityFixture({ type: 'done' }))
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(headers).toHaveCount(1)
  await expect(headers).not.toHaveAttribute('aria-label', /loading details/)
  if (await headers.getAttribute('aria-expanded') !== 'true') await headers.click()
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await expect(surface.getByText(/echo verify/, { exact: false }).first()).toBeVisible()
  return { headers, surface, detailReads }
}

/** Lifecycle metadata has a real position: waking waits lead an answer;
 * outcomes trail it, including when all of that row's tools move earlier. */
export async function checkActivityBoundaries(page, chat, base, { outcome = false } = {}) {
  const root = 'rt-activity-boundaries'
  const tool = id => ({ type: 'tool', tool: 'Bash', input: `echo ${id}`, tool_use_id: id, status: 'done' })
  const carrier = n => ({ role: 'user', hidden: true, steered: true, kind: 'delegation_result',
    source_work_id: 'logical-goal', cid: `cut-${n}`, content: '', ts: n })
  const wait = { id: 'boundary-wait', description: 'Check completed', status: outcome ? 'cancelled' : 'met', delivery_pending: false }
  const messages = [
    { role: 'user', cid: 'request', content: 'Check activity boundaries', ts: 1 },
    { role: 'assistant', id: root, content: '', ts: 2,
      blocks: [{ type: 'text', content: 'Independent work stays together.' }, tool('first'), tool('second')],
      ...(!outcome && { wait_summaries: [wait] }) },
    carrier(3),
    { role: 'assistant', id: `${root}:assistant:1`, content: '', ts: 4,
      blocks: [tool('third'), tool('fourth')], ...(outcome && { wait_summaries: [wait] }) },
    carrier(5),
    { role: 'assistant', id: `${root}:assistant:2`, content: '', ts: 6,
      blocks: [tool('fifth'), tool('sixth')] },
  ]
  const runtime = runtimeSnapshot({ running: false, run_status: 'completed', runtime_revision: 10000000 })
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => route.fulfill({ json: {
    ...chat, provider: 'codex', ...testChatAgentSettings(), ...runtime, messages, total: messages.length, offset: 0,
  } }))
  await page.route(`**/api/chats/${chat.id}/runtime`, route => route.fulfill({ json: runtime }))
  await page.route(`**/api/chats/${chat.id}/activity*`, route => route.fulfill({ json: { events: [], next_before: null } }))
  await page.route(`**/api/chats/${chat.id}/stream`, route => route.fulfill({ status: 204, body: '' }))
  await page.route(`**/api/chats/${chat.id}/messages`, route => route.abort())
  await page.goto(`${base}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const headers = surface.locator('.chat__activity-header[aria-expanded]')
  await expect(headers).toHaveCount(outcome ? 2 : 1)
  const waitCard = surface.locator('.chat__wait-history')
  await expect(waitCard).toHaveCount(1)
  const firstBox = await headers.first().boundingBox()
  const waitBox = await waitCard.boundingBox()
  if (outcome) {
    const lastBox = await headers.last().boundingBox()
    expect(waitBox.y).toBeGreaterThanOrEqual(firstBox.y + firstBox.height)
    expect(lastBox.y).toBeGreaterThanOrEqual(waitBox.y + waitBox.height)
  } else {
    expect(firstBox.y).toBeGreaterThanOrEqual(waitBox.y + waitBox.height)
  }
  // Empty source anchors must not accumulate visual gaps or obscure the rows.
  const margins = await surface.locator('.chat__reply-rows > .chat__msg').evaluateAll(rows => rows.map(row => parseFloat(getComputedStyle(row).marginBlockStart)))
  expect(margins.every(margin => margin >= 0)).toBe(true)
  return { headers, surface }
}

/** Exercise both owners of the same row markup with the loaded shell CSS. */
export async function checkActivitySpacing(page) {
  const gaps = await page.evaluate(() => {
    const host = document.createElement('div')
    host.style.cssText = 'position:fixed;left:-2000px;top:0;width:600px;visibility:hidden'
    const row = '<li class="chat__msg chat__msg--assistant"><div class="chat__assistant-copy-surface"><div class="chat__tools"><div class="chat__activity"><div class="chat__activity-header">Tool activity</div></div></div></div></li>'
    const anchor = '<li class="chat__msg chat__msg--assistant"><div class="chat__assistant-copy-surface"></div></li>'
    host.innerHTML = `<ul class="chat__list">${row}${row}</ul><ul class="chat__reply-rows">${row}${row}</ul><ul class="chat__reply-rows">${row}${anchor}${row}</ul>`
    document.body.append(host)
    try {
      return [...host.children].map(list => {
        const headers = [...list.querySelectorAll('.chat__activity-header')].map(element => element.getBoundingClientRect())
        const scale = list.getBoundingClientRect().width / list.offsetWidth
        return (headers[1].top - headers[0].bottom) / scale
      })
    } finally { host.remove() }
  })
  expect(gaps[0]).toBeCloseTo(4, 1)
  expect(gaps[1]).toBeCloseTo(8, 1)
  expect(gaps[2]).toBe(gaps[1])
}
