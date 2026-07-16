/** A hostile opaque parent may load the inert bootstrap document, but cannot
 * turn routing correlation or a successful postMessage into authority. This
 * project is intentionally unauthenticated and has no auth-setup dependency. */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const HOSTILE_URL = new URL(BASE)
HOSTILE_URL.hostname = HOSTILE_URL.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1'
HOSTILE_URL.pathname = '/offline.html'
HOSTILE_URL.search = ''
HOSTILE_URL.hash = ''

test('external hostile framer gets only an inert document without a grant', async ({ browser }) => {
  const context = await browser.newContext({ serviceWorkers: 'block' })
  const page = await context.newPage()
  const chatApiRequests = []
  const pageErrors = []
  const diagnostics = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') diagnostics.push(message.text())
  })
  page.on('requestfailed', request => diagnostics.push(
    `${request.url()}: ${request.failure()?.errorText || 'request failed'}`
  ))
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/chats/')) chatApiRequests.push(path)
  })
  try {
    await page.goto(HOSTILE_URL.href, { waitUntil: 'domcontentloaded' })
    const attackerDocument = encodeURIComponent(
      `<iframe id="victim" sandbox="allow-scripts" src="${BASE}/shell/embed/chat"></iframe>`,
    )
    await page.setContent(
      `<iframe id="attacker" sandbox="allow-scripts" src="data:text/html,${attackerDocument}"></iframe>`,
    )
    await expect.poll(() => ({
      frames: page.frames().map(frame => frame.url()), diagnostics,
    })).toEqual({
      frames: expect.arrayContaining([`${BASE}/shell/embed/chat`]),
      diagnostics: [],
    })
    const attacker = page.frames().find(frame => frame.url().startsWith('data:text/html,'))
    expect(attacker).toBeTruthy()
    const victim = page.frameLocator('#attacker').frameLocator('#victim')
    await expect(victim.locator('#root')).toHaveText('')

    const exchanges = []
    page.on('response', response => {
      if (new URL(response.url()).pathname === '/api/app-chat-embeds/session') {
        exchanges.push(response.status())
      }
    })
    await attacker.evaluate(() => {
      const frame = document.getElementById('victim')
      frame.contentWindow.postMessage({
        type: 'moebius:chat-embed:init',
        instanceId: 'hostile-instance-0001',
        chatId: 'forged-chat-id',
        authorizationId: 'hostile-authorization-0001',
        bootstrapCapability: 'forged-browser-handshake-is-not-authorization',
      }, '*')
    })
    await expect.poll(() => exchanges, { timeout: 5000 }).toEqual([401])
    await expect(victim.locator('#root')).toHaveText('')
    await expect(victim.getByRole('textbox')).toHaveCount(0)
    expect(chatApiRequests).toEqual([])
    expect(pageErrors).toEqual([])
  } finally {
    await context.close()
  }
})
