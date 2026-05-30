/**
 * AppCanvas iframe-mount contract.
 *
 * The user-visible "spinner forever" failure mode (see commit
 * 664e34f + the broader Bug 4 thread) had multiple causes, but the
 * load-bearing invariant the regressions all violated is the same:
 *
 *   The loading overlay (.canvas-loading) MUST hide the moment the
 *   iframe posts `moebius:frame-mounted` to the parent, and MUST
 *   stay visible until then.
 *
 * If that contract holds, a healthy app load completes the
 * handshake within a few hundred ms and the user sees content; a
 * broken app load shows the iframe's own error panel after its 10 s
 * internal timeout — never an indefinite spinner. The bug class
 * the spinner kept producing was: parent waits for a message it
 * never gets, iframe is otherwise fine but the parent's hook is
 * not wired.
 *
 * This test mocks the frame endpoint so we control the iframe's
 * postMessage behavior end-to-end and assert the parent overlay
 * reacts correctly.
 *
 * Run: npx playwright test tests/app-canvas.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'


/** Minimal mock frame HTML: listens for moebius:frame-init and
 *  posts moebius:frame-mounted back to the parent. Mirrors the
 *  real frame's protocol shape exactly (same event names, same
 *  origin handling) so we test the wire contract, not just the
 *  parent's React state. */
function mockFrameHTML(appId, opts = {}) {
  const { sendMounted = true, mountDelayMs = 0 } = opts
  return `<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<div id="root">mock app ${appId}</div>
<script>
  var initialized = false;
  window.addEventListener('message', function (e) {
    if (e.origin !== window.location.origin) return;
    var msg = e.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'moebius:frame-init' && !initialized) {
      initialized = true;
      ${sendMounted ? `
      setTimeout(function () {
        window.parent.postMessage(
          { type: 'moebius:frame-mounted', appId: ${JSON.stringify(String(appId))} },
          window.location.origin
        );
      }, ${mountDelayMs});` : '/* deliberately never post mounted */'}
    }
  });
</script>
</body></html>`
}


/** Set up the routes Shell needs to render an app canvas:
 *   - chats list (empty is fine — we land directly via /app/:id)
 *   - apps list with our test app
 *   - theme + setup status (idle but must respond)
 *   - app-token POST returns a dummy token
 *   - the frame endpoint returns our mock HTML
 */
async function setupAppRoutes(page, appId, frameHTML) {
  await page.setViewportSize({ width: 412, height: 915 })

  await page.route(/\/api\/chats(\/[^?]*)?(\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: '[]',
    })
  })
  await page.route(/\/api\/apps\/$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify([{
        id: appId,
        name: 'mock-app',
        description: 'test',
        compiled_path: `/data/compiled/app-${appId}.js`,
        chat_id: null,
        source_dir: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }]),
    })
  })
  await page.route(/\/api\/auth\/app-token$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'mock-app-token' }),
    })
  )
  await page.route(new RegExp(`/api/apps/${appId}/frame`), route =>
    route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-cache',
      },
      body: frameHTML,
    })
  )
}


test.describe('AppCanvas: iframe-mount contract', () => {

  test('loading spinner hides as soon as frame posts moebius:frame-mounted', async ({ page }) => {
    const appId = 99
    await setupAppRoutes(page, appId, mockFrameHTML(appId, { mountDelayMs: 50 }))

    await page.goto(`${BASE}/app/${appId}`, { waitUntil: 'domcontentloaded' })

    // Spinner must appear initially (the iframe hasn't posted mounted
    // yet because there's a 50ms delay). 10s timeout because CI's
    // bundled chromium + cold container can take 3-4s to reach this
    // state on first-app-load; local Chrome hits it in ~200ms.
    await expect(page.locator('.canvas-loading')).toBeVisible({ timeout: 10000 })

    // And it must hide once the mounted event lands. The contract
    // is sub-second locally; bumped to 6s for CI cold-cache iframe
    // boot. If this fails after 6s the listener genuinely never
    // matched the message (origin mismatch, source mismatch, or
    // appId stringify drift) — not a timing flake.
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 6000 })
  })

  test('spinner stays visible when frame never posts mounted', async ({ page }) => {
    // Negative case: confirms the spinner is genuinely gated on
    // frame-mounted rather than hiding on iframe.onLoad (which
    // fires too early — document loaded != React rendered). This
    // is exactly the regression that historically would replace
    // mounted-gated logic with onload-gated logic and silently
    // hide the spinner before the app was actually ready.
    const appId = 99
    await setupAppRoutes(page, appId, mockFrameHTML(appId, { sendMounted: false }))

    await page.goto(`${BASE}/app/${appId}`, { waitUntil: 'domcontentloaded' })

    await expect(page.locator('.canvas-loading')).toBeVisible({ timeout: 5000 })

    // Give the iframe a full second to fire onLoad and any
    // alternative signals — spinner must still be there.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))
    await expect(page.locator('.canvas-loading')).toBeVisible()
  })
})
