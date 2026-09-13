/* Isolated recovery browser fixtures share authentication and private build loading. */
import { test as base, expect, chromium } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { resolve, sep } from 'node:path'

// An authenticated screenshot-helper browser may run these fully intercepted
// fixtures against a live build. No fixture request may mutate the real chat.
export const test = process.env.MOBIUS_RECOVERY_CDP ? base.extend({
  context: async ({}, use) => {
    // Export standard Playwright auth state from the authenticated screenshot
    // helper before starting fixtures. Never sample changing/closing test tabs.
    if (!process.env.MOBIUS_RECOVERY_AUTH_STATE) {
      throw new Error('Provide the screenshot helper auth export in MOBIUS_RECOVERY_AUTH_STATE')
    }
    const auth = JSON.parse(await readFile(process.env.MOBIUS_RECOVERY_AUTH_STATE, 'utf8'))
    const browser = await chromium.connectOverCDP(process.env.MOBIUS_RECOVERY_CDP)
    let context
    try {
      context = await browser.newContext({
        serviceWorkers: 'block',
        storageState: { ...auth, origins: auth.origins.map(origin => ({
          origin: origin.origin,
          localStorage: origin.localStorage.filter(item => item.name === 'token'),
        })) },
      })
      await use(context)
    } finally {
      try { await context?.close() } finally { await browser.close() }
    }
  },
}) : base
test.use({ serviceWorkers: 'block' })


export { expect }

// Serve the reviewed private build only inside the intercepted browser.
export async function serveRecoveryBuild(page) {
  if (process.env.MOBIUS_FIXTURE_DIST) {
    const dist = resolve(process.env.MOBIUS_FIXTURE_DIST)
    await page.route(/\/(?:shell|assets)\//, async route => {
      const pathname = decodeURIComponent(new URL(route.request().url()).pathname)
      const filename = resolve(dist, pathname.replace(/^\/shell\//, '').replace(/^\//, '') || 'index.html')
      if (!filename.startsWith(dist + sep)) return route.abort()
      const contentType = filename.endsWith('.js') ? 'application/javascript'
        : filename.endsWith('.css') ? 'text/css'
          : filename.endsWith('.html') ? 'text/html' : 'application/octet-stream'
      await route.fulfill({ body: await readFile(filename), contentType })
    })
  }
}
