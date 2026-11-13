import { expectComposerValue } from './_composer.mjs'
/**
 * A mini-app crash reaches the owner with its real message.
 *
 * React reports errors through the browser's window.reportError. The frame
 * must leave that API intact, and an error that unmounts the whole app must
 * show the frame's error panel with the real message, which Report to agent
 * then hands to the shell as a crash draft.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/app-frame-crash.spec.mjs
 */
import { expect, test } from '@playwright/test'
import { applyApp } from './app-source.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

const CRASHING_APP = `
import { useState } from 'react'

export default function App() {
  const [crashed, setCrashed] = useState(false)
  if (crashed) {
    const helpers = {}
    helpers.missingHelper()
  }
  return <main>
    <h1 id="ready">ready</h1>
    <button id="crash" onClick={() => setCrashed(true)}>Crash</button>
  </main>
}
`

async function ownerToken(page) {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token
}

async function liveFrame(page, appId) {
  const selector = `iframe.canvas--live[data-app-id="${appId}"]`
  let frame = null
  await expect.poll(async () => {
    const handle = await page.locator(selector).last().elementHandle()
    frame = await handle?.contentFrame()
    return frame ? await frame.locator('#ready').textContent().catch(() => null) : null
  }).toBe('ready')
  return frame
}

test('an app that crashes after mount shows and reports its real error', async ({
  page,
  request,
}) => {
  const token = await ownerToken(page)
  const stamp = Date.now()
  const { app } = await applyApp(request, token, {
    slug: `frame-crash-e2e-${stamp}`,
    name: `Frame crash E2E ${stamp}`,
    description: 'Disposable crash-reporting fixture.',
    jsxSource: CRASHING_APP,
  })

  try {
    await page.goto(`${BASE}/shell/?app=${app.id}`, { waitUntil: 'domcontentloaded' })
    const frame = await liveFrame(page, app.id)

    expect(await frame.evaluate(() => String(window.reportError)))
      .toContain('[native code]')

    await frame.locator('#crash').press('Enter')
    await expect(frame.locator('#error-panel')).toBeVisible()
    await expect(frame.locator('#error-title')).toHaveText('Runtime error')
    await expect(frame.locator('#error-detail'))
      .toContainText('missingHelper is not a function')

    await frame.locator('#report-btn').press('Enter')
    await expectComposerValue(page.getByRole('textbox', { name: 'Message Möbius…' }), /crashed with this error[\s\S]*missingHelper is not a function/, {
        timeout: 8000,
      })
  } finally {
    await request.delete(`${BASE}/api/apps/${app.id}`, {
      headers: { Authorization: `Bearer ${token}` },
      failOnStatusCode: false,
    })
  }
})
