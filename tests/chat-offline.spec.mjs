// Durable offline delivery belongs to the client outbox. Every chat request is
// intercepted: this spec needs no persisted chat, provider turn, or cleanup job.
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import { installMockAgentProvider, testChatAgentSettings } from './_chatTestPrerequisites.mjs'
import { FAILURE_GRACE_MS, PROBE_TIMEOUT_MS } from '../frontend/src/lib/connectivityStore.js'

const BASE = process.env.MOBIUS_URL || process.env.API_BASE_URL || 'http://localhost:8001'
const CHAT = 'ffffffff-2222-4222-8333-444444444444'

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

test('offline Send survives reload and drains into the chat once after reconnect', async ({ page, context }) => {
  const chat = { id: CHAT, title: 'Offline outbox fixture', provider: 'claude',
    ...testChatAgentSettings(), running: false, pending_messages: [],
    pending_question_id: null, recovery_run_id: null,
    active_assistant_message_id: null, updated_at: '2026-09-12T00:00:00Z' }
  const chatPath = `/api/chats/${chat.id}`
  const messagePath = `${chatPath}/messages`
  let serverReachable = true
  let networkUp = false
  let delivered = null
  const attemptedBodies = []
  const acceptedBodies = []

  const detail = () => ({ ...chat, messages: delivered ? [{
    role: 'user', content: delivered.content, cid: delivered.cid, ts: delivered.ts,
  }] : [], total: delivered ? 1 : 0, offset: 0 })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname === messagePath && route.request().method() === 'POST') {
      const body = route.request().postDataJSON()
      attemptedBodies.push(body)
      if (!networkUp) return route.abort('internetdisconnected')
      acceptedBodies.push(body)
      delivered = { ...body, ts: Date.now() }
      return route.fulfill({ status: 202, json: { status: 'started' } })
    }
    // Unknown fixture mutations must never reach the real server, including
    // workspace bookkeeping when this spec uses an authenticated helper.
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      return route.fulfill({ json: {} })
    }
    if (!serverReachable) return route.abort('internetdisconnected')
    if (url.pathname === '/api/ready') return route.fulfill({ json: { ready: true, boot_id: 'offline-fixture-boot' } })
    if (url.pathname === '/api/chats') return route.fulfill({ json: [detail()] })
    if (url.pathname === chatPath || url.pathname === `${chatPath}/runtime`) return route.fulfill({ json: detail() })
    if (url.pathname === `${chatPath}/stream`) return route.fulfill({ status: 204, body: '' })
    return route.continue()
  })
  await installMockAgentProvider(page)
  await serveRecoveryBuild(page)
  await page.addInitScript(() => sessionStorage.setItem('mobius:visual-content-only', '1'))

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  const send = surface.getByRole('button', { name: 'Send', exact: true })
  const message = `offline intent ${Date.now()}`
  await input.fill(message)
  await expect(send).toBeEnabled()

  serverReachable = false
  await context.setOffline(true)
  await expect(page.locator('.shell__connection-status')).toHaveCount(1)
  await expect(page.locator('.shell__connection-status')).toHaveText(/Offline/i, {
    // The shared reachability owner first confirms a failure; an assertion
    // shorter than its deliberate grace races that contract.
    timeout: FAILURE_GRACE_MS + PROBE_TIMEOUT_MS + 5000,
  })
  await expect(surface.getByText("You're offline — chat needs a connection.")).toHaveCount(0)

  await send.click()
  const queued = surface.locator('.queued__row').filter({ hasText: message })
  await expect(queued).toBeVisible()
  await expect(input).toHaveValue('')
  await expect(surface.locator('.chat__msg--user')).toHaveCount(0)
  expect(attemptedBodies).toHaveLength(0)

  await expect.poll(() => readChatOutbox(page), { timeout: 5000 }).toHaveLength(1)
  const retained = await readChatOutbox(page)
  expect(retained[0]).toMatchObject({
    chatId: String(chat.id),
    type: 'message',
    body: { content: message },
  })
  expect(retained[0].cid).toBeTruthy()
  expect(retained[0].body.cid).toBe(retained[0].cid)

  // Restore document reachability while the message transport remains down,
  // then reload the complete shell. The durable outbox
  // row must retain automatic retry ownership without reviving the sent draft; an
  // authoritative empty transcript is not proof that the retained send failed.
  serverReachable = true
  await context.setOffline(false)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(queued).toBeVisible()
  await expect(input).toHaveValue('')
  await expect.poll(() => readChatOutbox(page), { timeout: 5000 }).toHaveLength(1)

  networkUp = true
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'))
    window.dispatchEvent(new Event('focus'))
  })
  await expect(page.locator('.shell__connection-status')).toHaveCount(0)
  await expect.poll(() => acceptedBodies, { timeout: 5000 }).toHaveLength(1)
  expect(acceptedBodies[0]).toEqual(retained[0].body)
  await expect.poll(() => readChatOutbox(page), { timeout: 5000 }).toHaveLength(0)

  await expect(surface.locator('.chat__msg--user')).toHaveCount(1)
  await expect(surface.locator('.chat__msg--user')).toContainText(message)
  await expect(input).toHaveValue('')
  await expect(queued).toHaveCount(0)

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
