import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'
import { applyApp, applySource, writeAppSource } from './app-source.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CONTAINER = process.env.MOBIUS_CONTAINER || 'mobius-test'

function git(slug, ...args) {
  return execFileSync(
    'docker',
    [
      'exec', '--user', 'mobius', CONTAINER,
      'git', '-C', `/data/apps/${slug}`, ...args,
    ],
    { encoding: 'utf8' },
  ).trim()
}

async function ownerToken(page) {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token
}

async function currentFrame(page, appId) {
  const selector = `iframe[data-app-id="${appId}"]`
  let frame = null
  await expect.poll(async () => {
    const handle = await page.locator(selector).last().elementHandle()
    frame = await handle?.contentFrame()
    return frame ? await frame.locator('#revision').textContent().catch(() => null) : null
  }).not.toBeNull()
  return frame
}

function source(label) {
  return `
import { suffix } from './copy.js'
import { useState } from 'react'

export default function App() {
  const [count, setCount] = useState(0)
  return <main>
    <h1 id="revision">${label} {suffix}</h1>
    <button id="increment" onClick={() => setCount(value => value + 1)}>
      Count <span id="count">{count}</span>
    </button>
  </main>
}
`
}

test('explicit apply owns draft, publication, iframe refresh, and rollback', async ({
  page,
  request,
}) => {
  const errors = []
  page.on('console', message => {
    const text = message.text()
    // Chrome emits this browser-level warning as a console error in every
    // Playwright incognito context. It is not produced by the shell or app.
    const incognitoPushWarning = (
      text.includes('does not support the Push API in incognito mode')
      && text.includes('crbug.com/41124656')
    )
    if (message.type() === 'error' && !incognitoPushWarning) {
      errors.push(`console: ${text}`)
    }
  })
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`))

  const token = await ownerToken(page)
  const stamp = Date.now()
  const slug = `explicit-apply-e2e-${stamp}`
  const base = {
    slug,
    name: `Explicit apply E2E ${stamp}`,
    description: 'Disposable explicit-apply lifecycle fixture.',
    offlineCapable: true,
    files: { 'copy.js': "export const suffix = 'ready'\n" },
  }
  const created = await applyApp(request, token, {
    ...base,
    jsxSource: source('revision one'),
  })
  const { app, sourceDir } = created
  const headers = { Authorization: `Bearer ${token}` }

  try {
    expect(created.mode).toBe('created')
    expect(git(slug, 'status', '--porcelain')).toBe('')
    const firstHead = git(slug, 'rev-parse', 'main')
    const firstCount = Number(git(slug, 'rev-list', '--count', 'main'))

    await page.goto(`${BASE}/shell/?app=${app.id}`, {
      waitUntil: 'domcontentloaded',
    })
    let frame = await currentFrame(page, app.id)
    await expect(frame.locator('#revision')).toHaveText('revision one ready')
    await frame.locator('#increment').click()
    await expect(frame.locator('#count')).toHaveText('1')

    await page.waitForTimeout(1_000)
    await page.evaluate(appId => {
      const liveFrame = document.querySelector(
        `iframe[data-app-id="${appId}"]`,
      )
      const canvasWrap = liveFrame?.closest('.canvas-wrap')
      if (!liveFrame || !canvasWrap) {
        throw new Error('live app frame was not mounted')
      }
      window.__explicitApplyInitialFrame = liveFrame
      window.__explicitApplyFrameAdds = 0
      window.__explicitApplyObserver?.disconnect()
      window.__explicitApplyObserver = new MutationObserver(records => {
        for (const record of records) {
          for (const node of record.addedNodes) {
            if (!(node instanceof Element)) continue
            const frames = [
              ...(node.matches?.('iframe.canvas') ? [node] : []),
              ...node.querySelectorAll?.('iframe.canvas') || [],
            ]
            window.__explicitApplyFrameAdds += frames.length
          }
        }
      })
      window.__explicitApplyObserver.observe(canvasWrap, {
        childList: true,
        subtree: true,
      })
    }, String(app.id))

    writeAppSource({
      ...base,
      jsxSource: source('revision two'),
      files: {
        'copy.js': "export const suffix = 'multi-file'\n",
        'details.js': "export const untouched = true\n",
      },
    })
    await page.waitForTimeout(1_500)
    frame = await currentFrame(page, app.id)
    await expect(frame.locator('#revision')).toHaveText('revision one ready')
    expect(git(slug, 'rev-parse', 'main')).toBe(firstHead)
    expect(git(slug, 'status', '--porcelain')).not.toBe('')
    expect(await page.evaluate(() => window.__explicitApplyFrameAdds)).toBe(0)

    const updated = await applySource(request, token, sourceDir)
    expect(updated.response.ok()).toBeTruthy()
    expect(updated.body.mode).toBe('updated')
    await expect.poll(async () => {
      frame = await currentFrame(page, app.id)
      return frame.locator('#revision').textContent()
    }).toBe('revision two multi-file')
    await expect.poll(
      () => page.evaluate(() => window.__explicitApplyFrameAdds),
    ).toBe(1)
    expect(await page.evaluate(appId => (
      document.querySelector(`iframe[data-app-id="${appId}"]`)
        !== window.__explicitApplyInitialFrame
    ), String(app.id))).toBe(true)
    expect(Number(git(slug, 'rev-list', '--count', 'main'))).toBe(firstCount + 1)
    expect(git(slug, 'status', '--porcelain')).toBe('')

    writeAppSource({
      ...base,
      jsxSource: 'export default function App( {',
      files: { 'copy.js': "export const suffix = 'broken'\n" },
    })
    const rejected = await applySource(request, token, sourceDir)
    expect(rejected.response.status()).toBe(422)
    expect(rejected.body.detail.code).toBe('compile_failed')
    frame = await currentFrame(page, app.id)
    await expect(frame.locator('#revision')).toHaveText('revision two multi-file')
    expect(git(slug, 'status', '--porcelain')).not.toBe('')

    const recovered = await applyApp(request, token, {
      ...base,
      jsxSource: source('revision three'),
      files: { 'copy.js': "export const suffix = 'recovered'\n" },
    })
    expect(recovered.mode).toBe('updated')
    await expect.poll(async () => {
      frame = await currentFrame(page, app.id)
      return frame.locator('#revision').textContent()
    }).toBe('revision three recovered')
    expect(git(slug, 'status', '--porcelain')).toBe('')
    expect(errors).toEqual([])
  } finally {
    await request.delete(`${BASE}/api/apps/${app.id}`, {
      headers,
      failOnStatusCode: false,
    })
  }
})
