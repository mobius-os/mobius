// Tier 5 / Tier 1 — service-worker offline behavior. Needs a live
// mobius-test container (MOBIUS_URL / default :8001) served from a real
// build so the SW is registered. The deterministic build-output gate
// lives in frontend/scripts/check-offline-build.mjs (run after build);
// this spec covers runtime behavior the build check can't.
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Wait for the SW to control the page — offline behavior depends on it.
async function swReady(page) {
  await page.waitForFunction(
    () => navigator.serviceWorker && navigator.serviceWorker.controller,
    { timeout: 15000 },
  )
}

test('shell persists theme bg for the offline page', async ({ page }) => {
  await page.goto(`${BASE}/shell/`)
  // useTheme writes this on a successful theme load.
  await page.waitForFunction(
    () => !!localStorage.getItem('mobius-theme-bg'),
    { timeout: 15000 },
  )
  const bg = await page.evaluate(() => localStorage.getItem('mobius-theme-bg'))
  expect(bg).toBeTruthy()
})

test('shell loads offline from the SW cache (no native error page)', async ({ page, context }) => {
  await page.goto(`${BASE}/shell/`)
  await swReady(page)
  // Second navigation while offline should be served by the SW, not the
  // browser's native error page.
  await context.setOffline(true)
  await page.goto(`${BASE}/shell/`)
  // The Möbius root container renders from the cached shell.
  await expect(page.locator('#root')).toBeAttached()
  await context.setOffline(false)
})
