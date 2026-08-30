// Tier 2 — durable chat intent while offline. The shell owns the one persistent
// connection warning, while the composer accepts the send into the chat outbox.
// Reconnecting must deliver that exact cid once and reconcile the restored draft
// against the durable chat row. Needs an isolated mobius-test runtime.
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

function readChatOutbox(page) {
  return page.evaluate(() => new Promise((resolve, reject) => {
    const open = indexedDB.open('mobius-chat-outbox')
    open.onerror = () => reject(open.error)
    open.onsuccess = () => {
      const db = open.result
      if (!db.objectStoreNames.contains('intents-v1')) {
        db.close()
        resolve([])
        return
      }
      const tx = db.transaction('intents-v1', 'readonly')
      const request = tx.objectStore('intents-v1').getAll()
      request.onerror = () => reject(request.error)
      request.onsuccess = () => resolve(request.result)
      tx.oncomplete = () => db.close()
    }
  }))
}

test('offline Send is retained once and drains into the chat after reconnect', async ({ page, context }) => {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'offline-outbox')
  const chatPath = `/api/chats/${chat.id}`
  const messagePath = `${chatPath}/messages`

  let networkUp = false
  let delivered = null
  const attemptedBodies = []
  const acceptedBodies = []

  await page.route(url => url.pathname === messagePath, async route => {
    const body = route.request().postDataJSON()
    attemptedBodies.push(body)
    if (!networkUp) return route.abort('internetdisconnected')
    acceptedBodies.push(body)
    delivered = { ...body, ts: Date.now() }
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started' }),
    })
  })
  await page.route(
    url => url.pathname === chatPath && url.searchParams.has('limit'),
    async route => {
      const response = await route.fetch()
      const detail = await response.json()
      if (delivered) {
        detail.messages = [{
          role: 'user',
          content: delivered.content,
          cid: delivered.cid,
          ts: delivered.ts,
        }]
        detail.pending_messages = []
        detail.running = false
      }
      return route.fulfill({ response, json: detail })
    },
  )
  await page.route(url => url.pathname === `${chatPath}/stream`, route => (
    route.fulfill({ status: 204, body: '' })
  ))

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  const send = surface.getByRole('button', { name: 'Send' })
  const deliveryNote = surface.locator('.chat__offline-note--error')
  const message = `offline intent ${Date.now()}`
  await input.fill(message)
  await expect(send).toBeEnabled()

  await context.setOffline(true)
  await expect(page.locator('.shell__connection-status')).toHaveCount(1)
  await expect(page.locator('.shell__connection-status')).toHaveText(/Offline/i)
  await expect(surface.getByText("You're offline — chat needs a connection.")).toHaveCount(0)

  await send.click()
  await expect(deliveryNote).toHaveText(
    'You’re offline. Your message is queued and will send when you reconnect.',
  )
  await expect(input).toHaveValue(message)
  await expect(send).toBeEnabled()

  await expect.poll(() => readChatOutbox(page), { timeout: 5000 }).toHaveLength(1)
  const retained = await readChatOutbox(page)
  expect(retained[0]).toMatchObject({
    chatId: String(chat.id),
    type: 'message',
    body: { content: message },
  })
  expect(retained[0].cid).toBeTruthy()
  expect(retained[0].body.cid).toBe(retained[0].cid)

  networkUp = true
  await context.setOffline(false)
  await expect(page.locator('.shell__connection-status')).toHaveCount(0)
  await expect.poll(() => acceptedBodies, { timeout: 5000 }).toHaveLength(1)
  expect(acceptedBodies[0]).toEqual(retained[0].body)
  await expect.poll(() => readChatOutbox(page), { timeout: 5000 }).toHaveLength(0)

  await expect(surface.locator('.chat__msg--user')).toHaveCount(1)
  await expect(surface.locator('.chat__msg--user')).toContainText(message)
  await expect(input).toHaveValue('')
  await expect(deliveryNote).toHaveCount(0)

  // Focus and online recovery are both valid drain triggers. Once the exact cid
  // is retired, repeating either trigger must not manufacture another POST.
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'))
    window.dispatchEvent(new Event('focus'))
  })
  await page.waitForTimeout(250)
  expect(acceptedBodies).toHaveLength(1)
  expect(new Set(attemptedBodies.map(body => body.cid)))
    .toEqual(new Set([retained[0].cid]))
})
