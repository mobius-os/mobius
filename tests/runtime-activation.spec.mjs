import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import { installMockAgentProvider, testChatAgentSettings } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT = 'eeeeeeee-1111-4111-8111-111111111111'

for (const revision of [undefined, '3']) {
  test(`invalid activation revision ${String(revision)} shows retry without caching rejected history`, async ({ page }) => {
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    let response = 'invalid'
    let reads = 0
    const chat = {
      id: CHAT,
      title: 'Runtime activation validation',
      has_messages: true,
      running: false,
      pending_messages: [],
      pending_question_id: null,
      updated_at: '2026-09-20T12:00:00Z',
      provider: 'claude',
      ...testChatAgentSettings(),
    }
    await page.route('**/api/**', route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      // This fixture never mutates the shared backend, including workspace
      // bookkeeping. Runtime/detail responses are owned entirely by this test.
      if (!['GET', 'HEAD'].includes(request.method())) return route.fulfill({ json: {} })
      if (path === '/api/chats') return route.fulfill({ json: [chat] })
      if (path === `/api/chats/${CHAT}` || path === `/api/chats/${CHAT}/runtime`) {
        reads += 1
        if (response === 'offline') return route.abort('internetdisconnected')
        return route.fulfill({ json: {
          ...chat,
          runtime_revision: response === 'valid' ? 3 : revision,
          messages: [{
            role: 'assistant',
            content: response === 'valid' ? 'Accepted history' : 'Rejected history',
            ts: 1700000000000,
          }],
          total: 1,
          offset: 0,
        } })
      }
      if (path === `/api/chats/${CHAT}/stream`) return route.fulfill({ status: 204, body: '' })
      return route.continue()
    })
    await installMockAgentProvider(page)
    await serveRecoveryBuild(page)
    await page.goto(`${BASE}/shell/?chat=${CHAT}`, { waitUntil: 'domcontentloaded' })
    const surface = page.locator('[data-chat-surface="painted"]')
    const error = surface.getByText("Couldn't load this chat.")
    await expect(error).toBeVisible()
    await expect(surface.getByText('Rejected history', { exact: true })).toHaveCount(0)
    expect(errors).toEqual([])

    // A failed retry must still show the load error. If the rejected detail
    // polluted the cache, this path would instead present it as offline history.
    response = 'offline'
    const previousReads = reads
    await surface.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect.poll(() => reads).toBeGreaterThan(previousReads)
    await expect(error).toBeVisible()
    await expect(surface.getByText('Rejected history', { exact: true })).toHaveCount(0)

    response = 'valid'
    await surface.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(surface.getByText('Accepted history', { exact: true })).toBeVisible()
    await expect(error).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
