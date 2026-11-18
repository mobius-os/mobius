/** Exercise the real chat renderer with deterministic, provider-free hidden cuts. */
import { expect } from '@playwright/test'
import { runtimeSnapshot, testChatAgentSettings, mockDeliveryReady } from './_chatTestPrerequisites.mjs'

export async function checkHiddenReplyContinuity(page, chat, base, provider = 'codex') {
  const firstId = `rt-reply-${provider}`
  const nextId = `${firstId}:assistant:1`
  const prefix = 'This sentence continues;'
  const answer = `${prefix} without an artificial paragraph boundary.`
  const carrier = { role: 'user', content: 'Hidden helper result', hidden: true,
    kind: 'delegation_result', steered: true, source_work_id: firstId, cid: 'reply-carrier', ts: 3 }
  const first = { role: 'assistant', id: firstId, content: prefix, ts: 2,
    blocks: [{ type: 'text', content: prefix }], source_ref: { message_index: 1, count: 2 } }
  let messages = [{ role: 'user', content: 'Formatting fixture', cid: 'reply-request', ts: 1 }, first]
  let runtime = runtimeSnapshot({ running: true, active_assistant_message_id: firstId,
    runtime_revision: 10000000, run_id: firstId, run_status: 'running' })
  let secondSourceFails = true
  const sourceReads = []
  await mockDeliveryReady(page)
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => route.fulfill({ json: {
    ...chat, provider, ...testChatAgentSettings(), ...runtime,
    messages, total: messages.length, offset: 0,
  } }))
  await page.route(`**/api/chats/${chat.id}/runtime`, route => route.fulfill({ json: runtime }))
  await page.route(`**/api/chats/${chat.id}/activity*`, route => route.fulfill({ json: { events: [], next_before: null } }))
  await page.route(`**/api/chats/${chat.id}/message-sources*`, route => {
    const index = Number(new URL(route.request().url()).searchParams.get('message_index'))
    sourceReads.push(index)
    if (index === 3 && secondSourceFails) return route.fulfill({ status: 503, json: { detail: 'Fixture failure' } })
    const common = { url: 'https://example.org/shared', title: 'Shared source' }
    return route.fulfill({ json: { sources: [common, {
      url: `https://example.org/${index}`, title: index === 1 ? 'First source' : 'Second source',
    }] } })
  })
  // This is only a read fixture; neither a provider run nor any write may escape.
  await page.route(`**/api/chats/${chat.id}/messages`, route => route.abort())
  await page.addInitScript(({ chatId, firstId, prefix }) => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, options) => {
      const url = typeof input === 'string' ? input : input.url
      if (!url.includes(`/api/chats/${chatId}/stream`)) return nativeFetch(input, options)
      const stream = new ReadableStream({ start(controller) {
        const emit = event => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
        window.__emitReplyFixture = emit
        emit({ type: 'stream_snapshot', assistant_message_id: firstId,
          items: [{ type: 'text', content: prefix, text_item_id: 'reply-text' }] })
        emit({ type: 'catch_up_done' })
      } })
      return Promise.resolve(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId: chat.id, firstId, prefix })
  await page.goto(`${base}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => typeof window.__emitReplyFixture === 'function')
  const surface = page.locator('[data-chat-surface="painted"]')
  const paragraph = surface.locator('.chat__text p').filter({ hasText: prefix })
  await expect(paragraph).toHaveText(prefix)
  await expect(surface.locator('.chat__sources')).toHaveCount(0)
  const firstKey = await paragraph.evaluate(element => {
    window.__originalReplyRow = element.closest('[data-key]')
    return window.__originalReplyRow.dataset.key
  })

  messages = [...messages, carrier]
  runtime = { ...runtime, active_assistant_message_id: nextId, runtime_revision: runtime.runtime_revision + 1 }
  await page.evaluate(({ firstId, nextId, carrier, prefix }) => window.__emitReplyFixture({
    type: 'steered_into_turn', assistant_message_id: firstId, next_assistant_message_id: nextId,
    sealed_items: [{ type: 'text', content: prefix, text_item_id: 'reply-text' }],
    items: [], messages: [carrier], ts: carrier.ts,
  }), { firstId, nextId, carrier, prefix })
  await expect(surface.locator('.chat__sources')).toHaveCount(0)
  await page.evaluate(() => window.__emitReplyFixture({ type: 'text', content: 'This sentence', text_item_id: 'reply-next' }))
  await expect(paragraph).toHaveText(prefix)
  await page.evaluate(suffix => window.__emitReplyFixture({ type: 'text', content: suffix, text_item_id: 'reply-next' }), answer.slice('This sentence'.length))
  await expect(paragraph).toHaveText(answer)
  // The owner's own message renders Markdown paragraphs too; count the reply's.
  await expect(surface.locator('.chat__text:not(.chat__text--user) p')).toHaveCount(1)
  expect(await page.evaluate(firstKey => window.__originalReplyRow === [...document.querySelectorAll('[data-chat-surface="painted"] [data-key]')].find(row => row.dataset.key === firstKey), firstKey)).toBe(true)
  const liveKey = await surface.locator('[data-active-assistant="true"]').getAttribute('data-key')
  await expect(surface.locator('.chat__sources')).toHaveCount(0)
  expect(sourceReads).toEqual([])

  messages = [...messages, { role: 'assistant', id: nextId, ts: 4, content: answer,
    blocks: [{ type: 'text', content: answer }], source_ref: { message_index: 3, count: 2 } }]
  runtime = { ...runtime, running: false, run_status: 'completed', runtime_revision: runtime.runtime_revision + 1 }
  await page.evaluate(() => window.__emitReplyFixture({ type: 'done' }))
  await expect(paragraph).toHaveText(answer)
  await expect(surface.locator('.chat__sources')).toHaveCount(1)
  await expect(surface.locator(`[data-key="${liveKey}"]`)).toHaveCount(1)
  await expect(surface.locator('.chat__source-chip')).toHaveCount(0)
  expect(sourceReads).toEqual([])
  // The terminal promotion retains the mounted live keys above. A cold saved
  // read also supplies compact metadata for every original physical segment.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(paragraph).toHaveText(answer)
  await expect(surface.locator('.chat__sources')).toHaveCount(1)
  await expect(surface.locator('.chat__source-chip')).toHaveCount(0)
  expect(sourceReads).toEqual([])
  // References sit in the reply's auto-hiding footer: a tap on its prose
  // reveals it, and the open disclosure then holds it shown.
  await paragraph.click()
  await surface.getByRole('button', { name: /^References/ }).click()
  await expect(surface.getByText('Some references could not load.')).toBeVisible()
  await expect(surface.locator('.chat__source-chip')).toHaveCount(2)
  secondSourceFails = false
  await surface.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(surface.locator('.chat__source-chip')).toHaveCount(3)
  expect(sourceReads.filter(index => index === 1)).toHaveLength(1)
  expect(sourceReads.filter(index => index === 3)).toHaveLength(2)
  await expect(surface.getByRole('button', { name: 'References 3', exact: true })).toBeVisible()
  await surface.getByRole('button', { name: 'References 3', exact: true }).click()
  await expect(surface.locator('.chat__source-chip')).toHaveCount(0)
  const selection = await paragraph.evaluate(element => {
    const range = document.createRange(); range.selectNodeContents(element)
    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range)
    const clipboard = new DataTransfer()
    element.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, clipboardData: clipboard }))
    const text = clipboard.getData('text/plain'); selection.removeAllRanges(); return text
  })
  expect(selection).toBe(answer)
  // The saved hit belongs to A2, but the suffix is now rendered inside A1.
  await page.route('**/api/chats/search?*', route => route.fulfill({ json: [{
    id: chat.id, title: chat.title, anchor_key: nextId,
    snippet: 'without an \ue000artificial\ue001 paragraph boundary',
  }] }))
  await page.getByRole('button', { name: 'Search and commands', exact: true }).click()
  await page.getByRole('combobox', { name: 'Search commands, chats, projects, apps, and app details' }).fill('artificial')
  const result = page.locator('.global-search__result').filter({ hasText: chat.title })
  await expect(result).toBeVisible()
  await result.click()
  await expect(surface.locator('.chat__msg--search-reveal')).toContainText(answer)
  expect(await page.evaluate(() => {
    const highlight = CSS.highlights?.get('chat-search-result')
    return highlight ? [...highlight].map(range => range.toString()) : []
  })).toContain('artificial')
  return { firstId, nextId, answer, liveKey, sourceReads }
}
