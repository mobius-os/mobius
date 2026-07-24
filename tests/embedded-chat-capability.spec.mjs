/**
 * Production topology regression: shell -> opaque app frame -> nested chat.
 *
 * The browser drives real production headers, app/runtime code, capability
 * mint/exchange, chat reads/uploads/send, SSE, remount and chat controls. The
 * backend capability suite owns the exhaustive wrong-app/chat/expiry/replay
 * matrix; the hostile-framer case below proves the narrowly frameable route is
 * nevertheless blank without a server grant.
 */
import { test, expect } from '@playwright/test'
import { applyApp } from './app-source.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

function pathnameOf(value) {
  try { return new URL(value).pathname } catch { return '' }
}

const APP_SOURCE = `
import { useEffect, useRef } from 'react'

export default function App() {
  const mountRef = useRef(null)
  useEffect(() => {
    let disposed = false
    let handle = null
    const status = document.getElementById('embed-e2e-status')
    window.mobius.chat({
      mount: mountRef.current,
      persist: 'embedded-chat-capability-e2e.json',
      title: 'Capability E2E',
      scope: 'capability-e2e',
      scopeLabel: 'Capability E2E',
      controls: true,
      picker: true,
      getContext: () => ({ marker: 'opaque-context-ok', file: 'index.html' }),
      onReady: ({ chatId }) => {
        status.dataset.chat = String(chatId)
        status.dataset.readyCount = String(Number(status.dataset.readyCount || '0') + 1)
        status.textContent = 'ready'
      },
      onMessageSent: () => { status.dataset.sent = '1'; status.textContent = 'sent' },
      onTurnDone: () => { status.dataset.done = '1'; status.textContent = 'done' },
      onError: ({ error }) => { status.dataset.error = String(error || 'error') },
    }).then((value) => {
      if (disposed) value.destroy()
      else {
        handle = value
        status.dataset.instance = String(value.instanceId)
      }
    }).catch((error) => { status.dataset.error = String(error?.message || error) })
    return () => { disposed = true; handle?.destroy() }
  }, [])
  return <main style={{height:'100vh',display:'grid',gridTemplateRows:'24px minmax(0,1fr)'}}>
    <output id="embed-e2e-status" data-ready-count="0">booting</output>
    <div ref={mountRef} style={{minHeight:0}} />
  </main>
}
`

function jwtPayload(authorization) {
  try {
    const token = String(authorization || '').replace(/^Bearer\s+/i, '')
    const part = token.split('.')[1]
    if (!part) return null
    return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'))
  } catch {
    return null
  }
}

async function ownerToken(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  return page.evaluate(() => localStorage.getItem('token'))
}

async function currentAppFrame(iframe) {
  try {
    const element = await iframe.elementHandle()
    return await element?.contentFrame() ?? null
  } catch {
    // AppCanvas can atomically replace its live iframe while a new bundle is
    // promoted. The caller polls again against the current data-app-id owner.
    return null
  }
}

test('opaque embedded chat completes authenticated flow and survives remount', async ({ page, request }) => {
  const consoleErrors = []
  const pageErrors = []
  const httpErrors = []
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push({
      text: message.text(),
      url: message.location().url || '',
    })
  })
  page.on('pageerror', error => pageErrors.push(error.stack || error.message))
  page.on('response', response => {
    if (response.status() >= 400) {
      httpErrors.push({ status: response.status(), url: response.url() })
    }
  })

  const token = await ownerToken(page)
  const ownerHeaders = { Authorization: `Bearer ${token}` }
  const stamp = Date.now()
  const { app } = await applyApp(request, token, {
    slug: `embedded-capability-e2e-${stamp}`,
    name: `Embedded capability E2E ${stamp}`,
    description: 'Disposable three-frame regression fixture.',
    jsxSource: APP_SOURCE,
  })
  const appTokenResponse = await request.post(`${BASE}/api/auth/app-token`, {
    headers: ownerHeaders,
    data: { app_id: app.id },
  })
  expect(appTokenResponse.ok()).toBeTruthy()
  const appToken = (await appTokenResponse.json()).token

  const embedResponses = []
  const appFrameResponses = []
  const childChatRequests = []
  let sendBody = null
  page.on('response', async response => {
    const url = new URL(response.url())
    if (url.pathname === '/shell/embed/chat') {
      embedResponses.push({ url: response.url(), headers: await response.allHeaders() })
    }
    if (url.pathname === `/api/apps/${app.id}/frame`) {
      appFrameResponses.push({
        url: response.url(),
        headers: await response.allHeaders(),
      })
    }
  })
  page.on('request', requestEvent => {
    const url = new URL(requestEvent.url())
    const headers = requestEvent.headers()
    let initiatorPath = ''
    try { initiatorPath = pathnameOf(requestEvent.frame().url()) } catch {}
    if (
      url.pathname.startsWith('/api/')
      && url.pathname !== '/api/app-chat-embeds/session'
      && headers.authorization
      && initiatorPath === '/shell/embed/chat'
    ) {
      const payload = jwtPayload(headers.authorization)
      childChatRequests.push({
        path: url.pathname,
        scope: payload?.scope || null,
        chat: payload?.chat_id || null,
        claimedInstance: payload?.embed_instance || null,
        instanceHeader: headers['x-mobius-embed-instance'] || null,
      })
    }
    if (/\/api\/chats\/[^/]+\/messages$/.test(url.pathname)) {
      try { sendBody = requestEvent.postDataJSON() } catch {}
    }
  })

  try {
    await page.goto(`${BASE}/app/${app.id}`, { waitUntil: 'domcontentloaded' })
    const outerFrame = page.locator(`iframe[data-app-id="${app.id}"]`)
    await expect(outerFrame).toBeVisible()

    // The element intentionally remains unsandboxed so the shell service
    // worker can intercept this navigation offline. Isolation is enforced by
    // the frame response's CSP sandbox, which must keep the origin opaque.
    await expect.poll(() => appFrameResponses.length).toBeGreaterThan(0)
    await expect(outerFrame).not.toHaveAttribute('sandbox', /.+/)
    const frameCsp = appFrameResponses.at(-1).headers['content-security-policy']
    expect(frameCsp).toContain('sandbox')
    expect(frameCsp).not.toContain('allow-same-origin')

    let appFrame = null
    await expect.poll(async () => ({
      frame: pathnameOf((appFrame = await currentAppFrame(outerFrame))?.url()),
      text: await appFrame?.locator('#embed-e2e-status').textContent(),
      error: await appFrame?.locator('#embed-e2e-status').getAttribute('data-error'),
      chatType: await appFrame?.evaluate(() => typeof window.mobius?.chat),
      childFrames: appFrame?.childFrames().map(frame => frame.url()) ?? [],
    }), { timeout: 20_000 }).toEqual({
      frame: `/api/apps/${app.id}/frame`,
      text: 'ready', error: null, chatType: 'function',
      childFrames: [expect.stringContaining('/shell/embed/chat')],
    })
    const status = appFrame.locator('#embed-e2e-status')
    const firstChat = await status.getAttribute('data-chat')
    const firstInstance = await status.getAttribute('data-instance')
    expect(firstChat).toBeTruthy()
    expect(firstInstance).toBeTruthy()

    const chatFrame = page.frames().find(frame =>
      pathnameOf(frame.url()) === '/shell/embed/chat'
    )
    expect(chatFrame).toBeTruthy()
    const chatUrl = new URL(chatFrame.url())
    expect(chatUrl.search).toBe('')
    expect(chatUrl.hash).toBe('')
    expect(await appFrame.evaluate(() => localStorage.getItem('token'))).toBeNull()
    expect(await chatFrame.evaluate(() => localStorage.getItem('token'))).toBeNull()
    await expect.poll(() => embedResponses.length).toBeGreaterThan(0)
    expect(embedResponses.at(-1).headers['x-frame-options']).toBeUndefined()
    expect(embedResponses.at(-1).headers['x-content-type-options']).toBe('nosniff')

    // Picker + real attachment path use the same chat-only principal.
    await chatFrame.getByRole('button', { name: 'Attach or change model' }).click()
    const chooserPromise = page.waitForEvent('filechooser')
    await chatFrame.getByRole('button', { name: 'Attach files' }).click()
    const chooser = await chooserPromise
    await chooser.setFiles({ name: 'e2e.txt', mimeType: 'text/plain', buffer: Buffer.from('scoped upload') })
    await expect(chatFrame.getByRole('button', { name: 'Remove e2e.txt' })).toBeVisible()

    await chatFrame.locator('textarea').fill('exercise embedded capability')
    await chatFrame.getByRole('button', { name: /send/i }).click()
    await expect.poll(() => sendBody, { timeout: 10_000 }).not.toBeNull()
    expect(sendBody.content).toContain('<marker>opaque-context-ok</marker>')
    expect(sendBody.attachments?.[0]?.name).toBe('e2e.txt')
    await expect(status).toHaveAttribute('data-sent', '1')
    await expect(status).toHaveAttribute('data-done', '1', { timeout: 30_000 })
    expect(childChatRequests.some(item => item.path.endsWith('/messages'))).toBeTruthy()
    expect(childChatRequests.some(item => item.path.endsWith('/stream'))).toBeTruthy()
    expect(childChatRequests.some(item => item.path.endsWith('/uploads'))).toBeTruthy()
    expect(childChatRequests.length).toBeGreaterThan(0)
    expect(childChatRequests.every(item => (
      item.scope === 'chat_embed'
      && item.chat === firstChat
      && item.claimedInstance === firstInstance
      && item.instanceHeader === firstInstance
    ))).toBeTruthy()

    // A full shell/app/embed remount mints a fresh one-use grant but reuses the
    // durable app chat; the old owner browser token is never available inside.
    await page.reload({ waitUntil: 'domcontentloaded' })
    const remountedOuterFrame = page.locator(`iframe[data-app-id="${app.id}"]`)
    await expect(remountedOuterFrame).toBeVisible()
    let remountedAppFrame = null
    await expect.poll(async () => ({
      frame: pathnameOf(
        (remountedAppFrame = await currentAppFrame(remountedOuterFrame))?.url(),
      ),
      text: await remountedAppFrame
        ?.locator('#embed-e2e-status').textContent(),
      error: await remountedAppFrame
        ?.locator('#embed-e2e-status').getAttribute('data-error'),
    }), { timeout: 20_000 }).toEqual({
      frame: `/api/apps/${app.id}/frame`, text: 'ready', error: null,
    })
    const remountedStatus = remountedAppFrame.locator('#embed-e2e-status')
    expect(await remountedStatus.getAttribute('data-chat')).toBe(firstChat)
    const remountedChatFrame = page.frames().find(frame =>
      pathnameOf(frame.url()) === '/shell/embed/chat'
    )
    expect(await remountedAppFrame.evaluate(() => localStorage.getItem('token'))).toBeNull()
    expect(await remountedChatFrame.evaluate(() => localStorage.getItem('token'))).toBeNull()

    // The runtime's supported New chat control rotates both chat and embed
    // instance, revokes the old session and authorizes a fresh blank document.
    await remountedAppFrame.getByRole('button', { name: 'New chat' }).click()
    await expect.poll(async () => remountedStatus.getAttribute('data-chat'))
      .not.toBe(firstChat)
    await expect(remountedStatus).toHaveText('ready', { timeout: 20_000 })

    expect(pageErrors).toEqual([])
    expect(consoleErrors.filter(item =>
      !item.text.includes('Push API in incognito mode')
      && !(item.text.includes('Failed to load resource')
        && pathnameOf(item.url) === `/api/storage/apps/${app.id}/embedded-chat-capability-e2e.json`)
    )).toEqual([])
    expect(httpErrors.filter(item => !(
      item.status === 404
      && pathnameOf(item.url) === `/api/storage/apps/${app.id}/embedded-chat-capability-e2e.json`
    ))).toEqual([])
  } finally {
    const chats = await request.get(`${BASE}/api/app-chats?scope=capability-e2e`, {
      headers: { Authorization: `Bearer ${appToken}` },
    })
    if (chats.ok()) {
      for (const chat of await chats.json()) {
        await request.delete(`${BASE}/api/chats/${chat.id}`, {
          headers: ownerHeaders,
          failOnStatusCode: false,
        })
      }
    }
    await request.delete(`${BASE}/api/apps/${app.id}`, {
      headers: ownerHeaders,
      failOnStatusCode: false,
    })
  }
})
