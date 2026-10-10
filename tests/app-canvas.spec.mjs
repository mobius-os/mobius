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
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/app-canvas.spec.mjs
 */
import { readFileSync } from 'node:fs'
import { test, expect } from '@playwright/test'
import { installMockProviderUsage, mockDeliveryReady, emptyChatPage } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Every scenario controls the shell API with page.route(). Letting the service
// worker handle those requests would bypass the mocks and make the spec hybrid.
test.use({ serviceWorkers: 'block' })


/** Minimal mock frame HTML: listens for moebius:frame-init and
 *  posts moebius:frame-mounted back to the parent. Mirrors the
 *  real frame's protocol shape exactly (same event names, same
 *  origin handling) so we test the wire contract, not just the
 *  parent's React state. */
function mockFrameHTML(appId, opts = {}) {
  // mountOnSignal: post frame-mounted only when the TEST sends a
  // 'moebius-test:mount' message — lets a test assert the spinner is
  // visible first and hidden after, deterministically, instead of racing
  // a timer-based auto-mount that can hide the spinner before the
  // assertion observes it.
  const { sendMounted = true, mountDelayMs = 0, mountOnSignal = false } = opts
  return `<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<div id="root">mock app ${appId}</div>
<script>
  var initialized = false;
  function postMounted() {
    window.parent.postMessage(
      { type: 'moebius:frame-mounted', appId: ${JSON.stringify(String(appId))} },
      window.location.origin
    );
  }
  window.addEventListener('message', function (e) {
    if (e.origin !== window.location.origin) return;
    var msg = e.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'moebius:frame-init' && !initialized) {
      initialized = true;
      ${sendMounted ? `setTimeout(postMounted, ${mountDelayMs});` : '/* deliberately never auto-post mounted */'}
    }
    ${mountOnSignal ? `if (msg.type === 'moebius-test:mount') postMounted();` : ''}
  });
</script>
</body></html>`
}

async function setupShellBasics(page) {
  // Register the safety net first: Playwright gives later, scenario-specific
  // routes precedence. Incidental shell reads get empty JSON and incidental
  // actions succeed without escaping this fully mocked spec.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
    }
    return route.fulfill({ status: 204, body: '' })
  })
  await page.route(/\/api\/health$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ok: true }),
    })
  )
  // With no chats, Shell auto-creates a starter chat; the catch-all's 204
  // would fail that create before the app view settles.
  await page.route(/\/api\/chats$/, route => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'bootstrap-chat', title: 'New chat', ...emptyChatPage(),
      }),
    })
  })
  // Without it the chat bootstrap renders the offline New Chat fallback instead of the canvas.
  await mockDeliveryReady(page)
  await page.route(/\/api\/theme$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ css: '', bg: '#000000' }),
    })
  )
  await page.route(/\/api\/models(\?.*)?$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: {} }),
    })
  )
  // An active chat prefetches these preferences during shell startup.
  await page.route(/\/api\/owner\/model-prefs$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden_ids: [] }),
    })
  )
  await page.route(/\/api\/owner\/walkthrough$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ completed: true, completed_at: new Date().toISOString() }),
    })
  )
  await page.route(/\/api\/auth\/providers\/status$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: {} }),
    })
  )
  await page.route(/\/api\/events\/system$/, route =>
    route.fulfill({
      status: 204,
      headers: { 'Content-Type': 'text/event-stream' },
      body: '',
    })
  )
  // A later route wins in Playwright. Keep app-opening chats from receiving
  // the generic catch-all response instead of a valid quota snapshot.
  await installMockProviderUsage(page)
}


/** Set up the routes Shell needs to render an app canvas:
 *   - chats list (empty is fine — we land directly via /app/:id)
 *   - apps list with our test app
 *   - theme + setup status (idle but must respond)
 *   - app-token POST returns a dummy token
 *   - the frame endpoint returns our mock HTML
 */
async function setupAppRoutes(page, appId, frameHTML) {
  // This endpoint is polled while the canvas is open. Production returns a
  // durable app revision; generating a new timestamp per request falsely tells
  // AppCanvas that the bundle changed and replaces its iframe every second.
  const appRevision = '2026-07-17T00:00:00.000Z'
  await page.setViewportSize({ width: 412, height: 915 })
  await page.addInitScript(() => {
    localStorage.setItem('token', 'mock-owner-token')
  })
  await setupShellBasics(page)

  await page.route(/\/api\/chats(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: '[]',
    })
  })
  await page.route(/\/api\/chats\/[^/?]+\/activity(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: [], next_before: null }),
    })
  })
  await page.route(/\/api\/chats\/[^/?]+(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const pathname = new URL(route.request().url()).pathname
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: pathname.split('/').pop(), title: 'New chat', ...emptyChatPage(),
      }),
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
        created_at: appRevision,
        updated_at: appRevision,
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

async function setupOpenAppRoutesWithStaleInitialList(
  page,
  sourceAppId,
  targetAppId,
) {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.addInitScript(() => {
    localStorage.setItem('token', 'mock-owner-token')
    localStorage.setItem('moebius_active_chat', 'open-app-chat')
  })
  await setupShellBasics(page)

  let appsFetches = 0
  const chatId = 'open-app-chat'
  const chat = {
    id: chatId,
    title: 'Last chat',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    has_messages: true,
    running: false,
  }
  const app = (id, name, slug) => ({
    id,
    name,
    slug,
    description: 'test',
    compiled_path: `/data/compiled/app-${id}.js`,
    chat_id: null,
    source_dir: `/data/apps/${slug}`,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })
  const sourceApp = app(sourceAppId, 'Launcher', 'launcher')
  const targetApp = app(targetAppId, 'CubeRun', 'cuberun')
  let targetVisible = false

  await page.route(/\/api\/chats$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify([chat]),
    })
  })
  await page.route(new RegExp(`/api/chats/${chatId}(\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...chat, messages: [] }),
    })
  })
  await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route(/\/api\/apps\/$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    appsFetches += 1
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(targetVisible ? [sourceApp, targetApp] : [sourceApp]),
    })
  })
  await page.route(/\/api\/auth\/app-token$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'mock-app-token' }),
    })
  )
  await page.route(/\/api\/apps\/(\d+)\/frame/, route => {
    const match = route.request().url().match(/\/api\/apps\/(\d+)\/frame/)
    const appId = Number(match?.[1])
    if (appId !== sourceAppId && appId !== targetAppId) return route.fallback()
    return route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-cache',
      },
      body: mockFrameHTML(appId, { sendMounted: true }),
    })
  })

  return {
    getAppsFetches: () => appsFetches,
    revealTarget: () => { targetVisible = true },
  }
}

/** Wait for the browser frame behind an iframe element, not just the DOM node.
 * React can commit the iframe before Chromium attaches its Frame; a one-shot
 * contentFrame() call in that window returns null. Re-querying the element also
 * survives a keyed iframe replacement while the app canvas settles. */
async function waitForContentFrame(page, selector, timeout = 10000) {
  let frame = null
  await expect.poll(async () => {
    const element = await page.$(selector)
    frame = element ? await element.contentFrame() : null
    return frame !== null
  }, {
    timeout,
    message: `browser frame did not attach for ${selector}`,
  }).toBe(true)
  return frame
}

test.describe('AppCanvas: iframe-mount contract', () => {
  test('loading spinner hides as soon as frame posts moebius:frame-mounted', async ({ page }) => {
    const appId = 99
    // Mount only on the test's signal — NOT a timer. The old
    // mountDelayMs:50 auto-mount made this flaky: on a fast/variable
    // container the spinner could hide before the toBeVisible below
    // observed it (a transient-state race, not a real timing bound).
    // With test-controlled mount the spinner is reliably visible first,
    // then reliably hidden after we trigger mount — deterministic.
    await setupAppRoutes(page, appId, mockFrameHTML(appId, { sendMounted: false, mountOnSignal: true }))

    await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })

    // No mount yet → the spinner is visible and STAYS visible (no race).
    // 10s covers CI's cold-container first-app mount; it won't hide on us.
    await expect(page.locator('.canvas-loading')).toBeVisible({ timeout: 10000 })

    // Opaque sandbox frames cannot be inspected through contentWindow.document
    // and a concrete targetOrigin cannot address them. Playwright can still
    // execute inside the frame, so wait for its own load state and dispatch the
    // deterministic test signal there. postMounted then reaches the parent with
    // the real frame as event.source and the opaque `null` event.origin.
    const frame = await waitForContentFrame(page, 'iframe.canvas')
    await frame.waitForLoadState('load')
    await frame.evaluate(() => {
      window.dispatchEvent(new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'moebius-test:mount' },
      }))
    })

    // Now it must hide. If this fails the listener genuinely never matched
    // the message (origin/source mismatch or appId stringify drift) — not
    // a timing flake, since the mount is now deterministic.
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 6000 })
  })

  test('drawer playback controls stay bound to the owning live frame', async ({ page }) => {
    const appId = 97
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 10000 })
    const frame = await waitForContentFrame(page, 'iframe.canvas--live')
    await frame.evaluate(() => {
      window.__mediaControls = []
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:media-control') {
          window.__mediaControls.push(event.data)
        }
      })
      window.parent.postMessage({
        type: 'moebius:media-session',
        event: 'open',
        sessionId: 'digest-one',
        title: 'Daily digest',
        subtitle: 'Spoofed app name',
        playbackState: 'playing',
      }, window.location.origin)
    })

    const drawerToggle = page.getByRole('button', { name: 'Toggle navigation' })
    await drawerToggle.click()
    const player = page.getByRole('region', { name: 'Now playing from mock-app' })
    await expect(player).toBeVisible()
    await expect(player).toContainText('Daily digest')
    await expect(player).toContainText('mock-app · Playing')
    await expect(player).not.toContainText('Spoofed app name')

    await player.getByRole('button', { name: 'Pause playback' }).click()
    await expect.poll(() => frame.evaluate(() => window.__mediaControls.at(-1)?.action))
      .toBe('pause')
    await frame.evaluate(() => window.parent.postMessage({
      type: 'moebius:media-session',
      event: 'update',
      sessionId: 'digest-one',
      title: 'Daily digest',
      playbackState: 'paused',
    }, window.location.origin))
    await expect(player.getByRole('button', { name: 'Resume playback' })).toBeVisible()

    await player.getByRole('button', { name: 'Stop playback' }).click()
    await expect.poll(() => frame.evaluate(() => window.__mediaControls.at(-1)?.action))
      .toBe('stop')
    // Stop is a request, not confirmation: controls remain until the app closes.
    await expect(player).toBeVisible()
    await frame.evaluate(() => window.parent.postMessage({
      type: 'moebius:media-session',
      event: 'close',
      sessionId: 'digest-one',
    }, window.location.origin))
    await expect(player).toBeHidden()

    // A same-element iframe reload replaces its document without firing a
    // callback-ref teardown. The load boundary must retire the old lease too.
    await frame.evaluate(() => window.parent.postMessage({
      type: 'moebius:media-session',
      event: 'open',
      sessionId: 'digest-two',
      title: 'Reloading digest',
      playbackState: 'playing',
    }, window.location.origin))
    await expect(player).toContainText('Reloading digest')
    await frame.evaluate(() => window.location.reload())
    await expect(player).toBeHidden({ timeout: 10000 })
  })

  test('app-token failure shows a retry that opens the app', async ({ page }) => {
    const appId = 98
    let tokenAttempts = 0
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    // Registered after the baseline route so this failure contract wins.
    // React Query retries once automatically; both automatic attempts fail,
    // then the explicit button succeeds on the third request.
    await page.route(/\/api\/auth\/app-token$/, route => {
      tokenAttempts += 1
      if (tokenAttempts <= 2) {
        return route.fulfill({
          status: 503,
          headers: { 'Content-Type': 'application/json' },
          body: '{"detail":"token service unavailable"}',
        })
      }
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: 'mock-app-token' }),
      })
    })

    await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })

    await expect(page.getByText('Couldn’t open mock-app')).toBeVisible({ timeout: 10000 })
    const retry = page.getByRole('button', { name: 'Try again' })
    await expect(retry).toBeVisible()
    await retry.click()

    await expect.poll(() => tokenAttempts).toBe(3)
    await expect(page.locator('iframe.canvas')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 10000 })
  })

  test('an app nav-pop consumes one sentinel without echoing nav-back', async ({ page }) => {
    const appId = 99
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    // Cold-restore the app while loading Vite's real root document. This keeps
    // the test valid against both the backend SPA fallback and a raw worktree
    // Vite server without relying on a direct deep-link dev response.
    await page.addInitScript((id) => {
      localStorage.setItem('moebius_active_view', 'canvas')
      localStorage.setItem('moebius_active_app', String(id))
    }, appId)
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 10000 })

    const frame = await waitForContentFrame(page, `iframe[data-app-id="${appId}"]`)
    await frame.evaluate(() => {
      window.__navBacks = 0
      window.__navAcks = []
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:nav-back') window.__navBacks += 1
        if (event.data?.type === 'moebius:nav-push-ack') {
          window.__navAcks.push(event.data.requestId)
        }
      })
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: 'first', requestId: 'first' },
        window.location.origin,
      )
    })
    await frame.waitForFunction(() => window.__navAcks.includes('first'))
    await frame.evaluate(() => {
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: 'second', requestId: 'second' },
        window.location.origin,
      )
    })
    await frame.waitForFunction(() => window.__navAcks.includes('second'))

    // The app has already closed its top nested view. The shell should only
    // remove that sentinel; reflecting nav-back would close the first level too.
    await frame.evaluate(() => {
      window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
    })
    await page.waitForTimeout(300)
    expect(await frame.evaluate(() => window.__navBacks)).toBe(0)

    // A genuine browser back still unwinds the remaining app level exactly once.
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => window.__navBacks === 1)
    expect(await frame.evaluate(() => window.__navBacks)).toBe(1)
  })

  test('an app nav-pop crosses an adjacent phantom entry without swallowing the next Back', async ({ page }) => {
    const appId = 99
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    await page.addInitScript((id) => {
      localStorage.setItem('moebius_active_view', 'canvas')
      localStorage.setItem('moebius_active_app', String(id))
    }, appId)
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    // The app list intentionally returns empty once before the fixture appears,
    // so React may replace the first iframe while the canvas settles. Wait for
    // the mounted handshake before retaining a Frame handle; otherwise a slow
    // CI worker can hand us the just-detached predecessor.
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 10000 })
    const frame = await waitForContentFrame(page, `iframe[data-app-id="${appId}"]`)
    await frame.evaluate(() => {
      window.__navBacks = 0
      window.__navAck = false
      // A descendant-frame history entry predates the shell sentinel. Closing
      // the sentinel therefore lands on an untagged destination first.
      history.pushState({}, '', '#phantom-before-shell-sentinel')
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:nav-back') window.__navBacks += 1
        if (event.data?.type === 'moebius:nav-push-ack') window.__navAck = true
      })
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: 'detail', requestId: 'phantom' },
        window.location.origin,
      )
    })
    await frame.waitForFunction(() => window.__navAck)
    await frame.evaluate(() => {
      window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
    })
    await page.waitForTimeout(300)
    expect(await frame.evaluate(() => window.__navBacks)).toBe(0)
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeVisible()

    // The local-pop marker is gone. One Back may clear the descendant frame's
    // own phantom history entry (the shell intentionally ignores that landing);
    // the following tagged Back must reach the seeded chat root rather than be
    // swallowed as a stale local close.
    await page.evaluate(() => history.back())
    await page.waitForTimeout(200)
    await page.evaluate(() => history.back())
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeHidden({ timeout: 5000 })
    expect(await frame.evaluate(() => window.__navBacks)).toBe(0)
  })

  test('a concurrent drawer close and app nav-pop perform exactly two traversals', async ({ page }) => {
    const appId = 99
    await page.setViewportSize({ width: 412, height: 915 })
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    await page.addInitScript((id) => {
      localStorage.setItem('moebius_active_view', 'canvas')
      localStorage.setItem('moebius_active_app', String(id))
    }, appId)
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    const frame = await waitForContentFrame(page, `iframe[data-app-id="${appId}"]`)
    await frame.evaluate(() => {
      window.__navBacks = 0
      window.__navAck = false
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:nav-back') window.__navBacks += 1
        if (event.data?.type === 'moebius:nav-push-ack') window.__navAck = true
      })
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: 'detail', requestId: 'drawer-race' },
        window.location.origin,
      )
    })
    await frame.waitForFunction(() => window.__navAck)
    const drawerToggle = page.getByRole('button', { name: 'Toggle navigation' })
    await drawerToggle.click()
    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'true')

    // Dispatch both requests in one parent-page task. Racing two independent
    // CDP evaluations lets the drawer render detach the iframe before the
    // frame-side evaluate is delivered, which tests Playwright scheduling
    // rather than the shell's traversal arbitration.
    await page.evaluate(id => {
      const appFrame = document.querySelector(`iframe[data-app-id="${id}"]`)
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'moebius:nav-pop' },
        origin: window.location.origin,
        source: appFrame?.contentWindow,
      }))
      document.querySelector('[aria-label="Toggle navigation"]')?.click()
    }, appId)
    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'false')
    await page.waitForTimeout(300)
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeVisible()

    // No third traversal was scheduled: the shell is still on the app until a
    // fresh user Back, which now reaches the seeded chat root.
    await page.evaluate(() => history.back())
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeHidden({ timeout: 5000 })
  })

  test('a drawer opened after app nav-pop starts waits for that traversal', async ({ page }) => {
    const appId = 99
    await page.setViewportSize({ width: 412, height: 915 })
    await setupAppRoutes(page, appId, mockFrameHTML(appId))
    await page.addInitScript((id) => {
      localStorage.setItem('moebius_active_view', 'canvas')
      localStorage.setItem('moebius_active_app', String(id))
    }, appId)
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    const frame = await waitForContentFrame(page, `iframe[data-app-id="${appId}"]`)
    await frame.evaluate(() => {
      window.__navBacks = 0
      window.__navAck = false
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:nav-back') window.__navBacks += 1
        if (event.data?.type === 'moebius:nav-push-ack') window.__navAck = true
      })
      window.parent.postMessage(
        { type: 'moebius:nav-push', label: 'detail', requestId: 'drawer-after-pop' },
        window.location.origin,
      )
    })
    await frame.waitForFunction(() => window.__navAck)

    // Hold the traversal after the shell has marked it in-flight. This makes
    // the ordering deterministic: the drawer request definitely arrives after
    // nav-pop starts but before its history entry commits.
    await page.evaluate(() => {
      const originalBack = history.back.bind(history)
      window.__localBackStarted = false
      window.__releaseLocalBack = null
      history.back = () => {
        window.__localBackStarted = true
        window.__releaseLocalBack = () => {
          history.back = originalBack
          originalBack()
        }
      }
    })
    await frame.evaluate(() => {
      window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
    })
    await page.waitForFunction(() => window.__localBackStarted)

    const drawerToggle = page.getByRole('button', { name: 'Toggle navigation' })
    await drawerToggle.click()
    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'false')
    await page.evaluate(() => window.__releaseLocalBack())

    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeVisible()
    expect(await frame.evaluate(() => window.__navBacks)).toBe(0)
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

    await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })

    // 10s (was 5s) — this waits for the genuinely slow cold-CI first-app
    // mount to render the spinner, which is a real state, not a race; 5s
    // was too tight for the cold container and made this flaky.
    await expect(page.locator('.canvas-loading')).toBeVisible({ timeout: 10000 })

    // Give the iframe a full second to fire onLoad and any
    // alternative signals — spinner must still be there.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))
    await expect(page.locator('.canvas-loading')).toBeVisible()
  })

  test('open-app request from a live app frame refetches a stale app list', async ({ page }) => {
    const sourceAppId = 44
    const targetAppId = 55
    const routes = await setupOpenAppRoutesWithStaleInitialList(
      page,
      sourceAppId,
      targetAppId,
    )

    await page.goto(`${BASE}/shell/?app=${sourceAppId}`, { waitUntil: 'domcontentloaded' })
    const sourceSelector = `iframe[data-app-id="${sourceAppId}"]`
    await expect(page.locator(sourceSelector)).toBeVisible({ timeout: 8000 })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 8000 })
    const sourceFrame = await waitForContentFrame(page, sourceSelector)

    const fetchesBeforeRequest = routes.getAppsFetches()
    routes.revealTarget()
    await sourceFrame.evaluate((appId) => {
      window.parent.postMessage({ type: 'moebius:open-app', appId }, '*')
    }, targetAppId)

    await expect(page.locator(`iframe[data-app-id="${targetAppId}"]`)).toBeVisible({
      timeout: 8000,
    })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 8000 })
    expect(routes.getAppsFetches()).toBeGreaterThan(fetchesBeforeRequest)
  })

  test('app-error from a hidden incoming frame is swallowed; the live frame forwards a crash draft', async ({ page }) => {
    // The double-buffered version swap runs the app's NEW module in a hidden
    // incoming frame. A failed swap is usually a broken build, and the swap
    // machinery already keeps the old working frame live — so a hidden frame's
    // moebius:app-error must NOT plant a crash-report draft or yank the view
    // to a chat, while the LIVE frame's crash must. AppCanvas makes that call
    // by source attribution (which frame sent the message) and forwards only
    // the live frame's error up via onAppError — this replaced the old
    // module-global incomingFrames WeakSet, whose unit tests died with it.
    const appId = 77
    await page.setViewportSize({ width: 412, height: 915 })
    await page.addInitScript(() => {
      localStorage.setItem('token', 'mock-owner-token')
    })
    await setupShellBasics(page)

    // The row's frame_version is the frame version key (appFrameVersion passes
    // it through): while the app sits at '1000' the
    // live frame mounts at that version; once the test ARMS the swap the app
    // reports '2000', so the next refetch triggers the double-buffer swap and
    // mounts a hidden incoming frame.
    //
    // A flag — not a fetch counter — gates the version. React Query issues an
    // unpredictable number of mount-time apps fetches (a refetchOnMount or a
    // re-render can fire a second one milliseconds after the first), so keying
    // the '1000'→'2000' bump on "fetch #1 vs later" raced: an extra mount-time
    // fetch consumed the bump before the test's deliberate open-app trigger,
    // the live frame settled at '2000', and the swap never happened (the exact
    // "don't rely on request ordinal" anti-pattern the E2E triage checklist
    // in CLAUDE.md warns against). With the flag, every mount-time fetch —
    // however many — returns '1000', and only the post-arm refetch returns
    // '2000'.
    let swapArmed = false
    const appRow = (frameVersion) => ({
      id: appId,
      name: 'CrashToy',
      slug: 'crashtoy',
      description: 'test',
      compiled_path: `/data/compiled/app-${appId}.js`,
      chat_id: null,
      source_dir: null,
      created_at: '1000',
      updated_at: frameVersion,
      frame_version: frameVersion,
    })
    await page.route(/\/api\/apps\/$/, route => {
      if (route.request().method() !== 'GET') return route.fallback()
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify([appRow(swapArmed ? '2000' : '1000')]),
      })
    })
    await page.route(/\/api\/auth\/app-token$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: 'mock-app-token' }),
      })
    )
    // Auto-mount every frame EXCEPT the '2000' incoming one, keyed on the
    // version in the URL (?v=<version>-<frameHash>) rather than a fetch
    // counter. The live frame (mounted at '1000', and any transient pre-load
    // '0' frame) posts frame-mounted so the swap machinery sees a settled live
    // frame; the incoming '2000' frame NEVER posts it, so it stays hidden and
    // unpromoted for the whole test window (until the 10s incoming-timeout).
    // Version-keying is robust to how many frame fetches the swap actually
    // issues — the counter was, like the apps counter above, an ordinal
    // assumption that a stray extra fetch broke.
    await page.route(new RegExp(`/api/apps/${appId}/frame`), route => {
      const isIncoming = /[?&]v=2000\b/.test(route.request().url())
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/html; charset=utf-8',
          'Cache-Control': 'no-cache',
        },
        body: mockFrameHTML(appId, { sendMounted: !isIncoming }),
      })
    })
    // The forwarded crash routes to a NEW chat (the app has no chat_id):
    // Shell's handleAppError calls newChat({draft, forceNew}) → POST /api/chats.
    // Count the creates — the swallow assertion is that this never fires.
    let chatsCreated = 0
    await page.route(/\/api\/chats(\?.*)?$/, route => {
      if (route.request().method() === 'POST') {
        chatsCreated += 1
        return route.fulfill({
          status: 200,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: 'crash-chat',
            title: 'New chat',
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            has_messages: false,
            running: false,
          }),
        })
      }
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: '[]',
      })
    })
    await page.route(/\/api\/chats\/crash-chat(\?.*)?$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: 'crash-chat',
          title: 'New chat',
          messages: [],
          has_messages: false,
          running: false,
          provider: 'claude',
        }),
      })
    )
    await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )

    await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })
    // The live frame has mounted (spinner gated on frame-mounted).
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 10000 })
    // Confirm the LIVE frame settled at '1000' before arming, so the arm can't
    // race an apps query that hasn't resolved yet (a transient pre-load '0'
    // frame can hide the spinner first; arming while the app is still at '0'
    // would let the very next apps fetch jump straight to '2000' and make the
    // live frame settle there, so the open-app refetch would be a no-op change
    // and no swap would start).
    await page.waitForSelector('iframe.canvas--live[data-frame-version="1000"]', {
      state: 'attached', timeout: 8000,
    })

    // Arm the swap: the live frame has settled at '1000', so from here every
    // apps fetch reports '2000'. Then trigger an apps refetch (an unknown
    // open-app target refetches once before giving up) — the new frame_version
    // starts the swap and mounts the hidden incoming frame.
    swapArmed = true
    const settledLiveFrame = await waitForContentFrame(
      page,
      'iframe.canvas--live[data-frame-version="1000"]',
    )
    await settledLiveFrame.evaluate(() => {
      window.parent.postMessage(
        { type: 'moebius:open-app', appId: 'no-such-app' },
        window.location.origin,
      )
    })
    // state:'attached', not 'visible' — the incoming frame is visibility:hidden.
    await page.waitForSelector('iframe.canvas--incoming', {
      state: 'attached', timeout: 8000,
    })

    // Crash report from the HIDDEN incoming frame → swallowed: no chat
    // create, no navigation away from the canvas.
    const incomingFrame = await waitForContentFrame(page, 'iframe.canvas--incoming')
    await incomingFrame.evaluate((id) => {
      window.parent.postMessage(
        { type: 'moebius:app-error', appId: String(id), error: 'hidden-frame crash' },
        window.location.origin,
      )
    }, appId)
    await page.waitForTimeout(800)
    expect(chatsCreated).toBe(0)
    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeVisible()

    // Crash report from the LIVE frame → forwarded: Shell routes it to a new
    // chat with the report as a reviewable draft (not auto-sent).
    await page.waitForSelector('iframe.canvas--live', {
      state: 'attached', timeout: 4000,
    })
    const liveFrame = await waitForContentFrame(page, 'iframe.canvas--live')
    await liveFrame.evaluate((id) => {
      window.parent.postMessage(
        { type: 'moebius:app-error', appId: String(id), error: 'live-frame crash' },
        window.location.origin,
      )
    }, appId)
    await expect(page.getByRole('textbox', { name: 'Message Möbius…' }))
      .toHaveValue(/crashed with this error/, { timeout: 8000 })
    expect(chatsCreated).toBe(1)
  })
})

// These scenarios run the compiled navigation runtime inside a controlled frame,
// but leave AppCanvas promotion, source attribution, storage and shell history real.
const navRuntime = readFileSync(new URL('../frontend/public/mobius-runtime.js', import.meta.url), 'utf8')

function locationFrameHTML(appId, { manualMount = false, fallback = false } = {}) {
  return `<!doctype html><html><body><div id="root">booting</div>
<script type="module">
  import { makeNav } from '${BASE}/test-nav-runtime.js';
  window.documentId = Math.random();
  window.initCalls = 0;
  window.visible = false;
  window.detail = false;
  const post = data => window.parent.postMessage(data, window.location.origin);
  const render = () => { document.getElementById('root').textContent = window.detail ? 'detail: notes' : 'list'; };
  window.mount = () => post({ type: 'moebius:frame-mounted', appId: '${appId}' });
  window.fail = () => post({ type: 'moebius:frame-error', appId: '${appId}' });
  window.openApp = appId => post({ type: 'moebius:open-app', appId });
  window.addEventListener('message', e => {
    if (e.source !== window.parent || e.origin !== window.location.origin) return;
    const msg = e.data;
    if (msg?.type === 'moebius:frame-visibility') window.visible = msg.visible === true;
    if (msg?.type !== 'moebius:frame-init') return;
    window.initCalls++;
    window.lastToken = msg.token;
    if (window.nav) return;
    window.nav = makeNav({ location: msg.navLocation, waitForNavigationReady: msg.waitForNavigationReady });
    window.initialLocation = window.nav.location;
    window.report = () => window.nav.setLocation({ ...window.nav.location, detail: window.detail ? 'notes' : null });
    window.openDetail = async () => {
      window.restoring = true;
      const handle = window.nav.open('notes', () => { window.detail = false; render(); window.report(); });
      window.ownership = await handle.outcome;
      if (window.ownership.status === 'owned') window.detail = true;
      window.restoring = false;
      render();
      window.report();
    };
    render();
    if (window.initialLocation?.detail && !${fallback}) void window.openDetail();
    else window.report();
    if (!${manualMount}) window.mount();
  });
</script></body></html>`
}

async function setupLocationRoutes(page) {
  const appId = 81
  await setupAppRoutes(page, appId, '')
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'deviceMemory', { configurable: true, value: 4 })
  })
  const state = {
    version: '1000', instance: 'nonce-a', storageGeneration: 'generation-a', updatedAt: '1000', name: 'Location 0',
    manualVersions: new Set(), fallbackVersions: new Set(), fetches: 0,
  }
  await page.route('**/test-nav-runtime.js', route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/javascript', 'Access-Control-Allow-Origin': '*' },
    body: navRuntime,
  }))
  await page.route(/\/api\/apps\/$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    state.fetches++
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(Array.from({ length: 8 }, (_, index) => ({
        id: appId + index, name: index === 0 ? state.name : `Location ${index}`, slug: `location-${index}`,
        compiled_path: `/data/compiled/app-${appId + index}.js`, chat_id: null,
        created_at: '1000', updated_at: index === 0 ? state.updatedAt : '1000',
        frame_version: index === 0 ? state.version : '1000',
        storage_generation: index === 0 ? state.storageGeneration : `generation-${index}`,
      }))),
    })
  })
  await page.route(/\/api\/auth\/app-token$/, route => {
    const id = route.request().postDataJSON().app_id
    const payload = { scope: 'app', app_id: id, app_nonce: state.instance, exp: 4102444800 }
    const token = `e30.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.test`
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ token }) })
  })
  await page.route(/\/api\/apps\/\d+\/frame/, route => {
    const url = new URL(route.request().url())
    const id = Number(url.pathname.split('/')[3])
    const version = url.searchParams.get('v').split('-')[0]
    return route.fulfill({
      status: 200, contentType: 'text/html',
      body: locationFrameHTML(id, {
        manualMount: id === appId && state.manualVersions.has(version),
        fallback: id === appId && state.fallbackVersions.has(version),
      }),
    })
  })
  await page.goto(`${BASE}/shell/?app=${appId}`, { waitUntil: 'domcontentloaded' })
  const frame = await locationFrame(page, appId, 'live', '1000')
  await frame.waitForFunction(() => window.visible)
  return state
}

async function locationFrame(page, appId = 81, role = 'live', version = null) {
  const appSelector = role === 'live' ? `[data-app-id="${appId}"]` : ''
  const selector = `iframe.canvas--${role}${appSelector}${version ? `[data-frame-version="${version}"]` : ''}`
  await page.waitForSelector(selector, { state: 'attached', timeout: 8000 })
  const frame = await waitForContentFrame(page, selector)
  await frame.waitForFunction(() => Boolean(window.nav))
  return frame
}

async function refetchLocationApps(page, state, frame) {
  const before = state.fetches
  await frame.evaluate(() => window.openApp('no-such-app'))
  await expect.poll(() => state.fetches).toBeGreaterThan(before)
}

async function storedLocation(page) {
  return page.evaluate(() => JSON.parse(sessionStorage.getItem('mobius:app-nav-location:81') || 'null'))
}

async function expectDetail(frame) {
  await frame.waitForFunction(() => window.detail && !window.restoring)
  expect(await frame.evaluate(() => window.ownership.status)).toBe('owned')
}

test.describe('AppCanvas location lifecycle', () => {
  test('malformed location reports cannot erase the saved place', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const frame = await locationFrame(page)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    await expect.poll(async () => (await storedLocation(page))?.location)
      .toBe('{"detail":"notes"}')
    for (const location of [undefined, '{broken', { detail: 'object' }, 'null', '"' + 'x'.repeat(4096) + '"']) {
      await frame.evaluate(location => {
        window.parent.postMessage({ type: 'moebius:nav-location', location }, window.location.origin)
      }, location)
      // The subsequent app-open request crosses the same ordered message channel.
      await refetchLocationApps(page, state, frame)
      expect((await storedLocation(page)).location).toBe('{"detail":"notes"}')
    }
    await frame.evaluate(() => window.nav.setLocation(null))
    await expect.poll(() => storedLocation(page)).toBeNull()
  })

  test('bookmark restoration owns Back through version swap and shell refresh', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    let frame = await locationFrame(page)
    const entryBeforeDetail = await page.evaluate(() => history.state?.entryId)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    state.version = '2000'
    await refetchLocationApps(page, state, frame)
    frame = await locationFrame(page, 81, 'live', '2000')
    await expectDetail(frame)
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => !window.detail)
    await expect.poll(() => page.evaluate(() => history.state?.entryId)).toBe(entryBeforeDetail)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    await page.reload({ waitUntil: 'domcontentloaded' })
    frame = await locationFrame(page, 81, 'live', '2000')
    await expectDetail(frame)
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => !window.detail)
    await expect.poll(() => page.evaluate(() => history.state?.entryId)).toBe(entryBeforeDetail)
    expect(await frame.locator('#root').textContent()).toBe('list')
  })

  test('a same-version document reload restores one Back target, not a ghost from the old document', async ({ page }) => {
    await setupLocationRoutes(page)
    let frame = await locationFrame(page, 81, 'live', '1000')
    const entryBeforeDetail = await page.evaluate(() => history.state?.entryId)
    expect(typeof entryBeforeDetail).toBe('string')
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    const documentId = await frame.evaluate(() => window.documentId)
    await frame.goto(frame.url(), { waitUntil: 'load' })
    frame = await locationFrame(page, 81, 'live', '1000')
    expect(await frame.evaluate(() => window.documentId)).not.toBe(documentId)
    await expectDetail(frame)
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => !window.detail)
    // Restoration reused the physical detail slot, so one Back reaches its base.
    await expect.poll(() => page.evaluate(() => history.state?.entryId)).toBe(entryBeforeDetail)
  })

  test('bookmark survives actual warm-cache eviction and remount', async ({ page }) => {
    await setupLocationRoutes(page)
    let frame = await locationFrame(page)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    const documentId = await frame.evaluate(() => window.documentId)
    // Seven other apps exceed the six-frame low-memory LRU budget.
    for (let id = 82; id <= 88; id++) {
      await frame.evaluate(id => window.openApp(id), id)
      frame = await locationFrame(page, id)
      await frame.waitForFunction(() => window.visible)
    }
    await expect(page.locator('iframe[data-app-id="81"]')).toHaveCount(0)
    await frame.evaluate(() => window.openApp(81))
    frame = await locationFrame(page)
    expect(await frame.evaluate(() => window.documentId)).not.toBe(documentId)
    await expectDetail(frame)
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => !window.detail)
  })

  test('an update while hidden defers restoration until return, then Back stays in the app', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const original = await locationFrame(page)
    await original.evaluate(() => window.openDetail())
    await expectDetail(original)
    await original.evaluate(() => window.openApp(82))
    const other = await locationFrame(page, 82)
    await other.waitForFunction(() => window.visible)
    state.version = '2000'
    await refetchLocationApps(page, state, other)
    const replacement = await locationFrame(page, 81, 'live', '2000')
    expect(await replacement.evaluate(() => ({ visible: window.visible, restoring: window.restoring, detail: window.detail })))
      .toEqual({ visible: false, restoring: true, detail: false })
    // Return only after the old eager ownership budget would have expired.
    // This wait probes a contractual deadline, not app settling.
    await page.waitForTimeout(5200)
    expect(await replacement.evaluate(() => window.ownership)).toBeUndefined()
    await other.evaluate(() => window.openApp(81))
    await expectDetail(replacement)
    await page.evaluate(() => history.back())
    await replacement.waitForFunction(() => !window.detail)
    await expect(page.locator('iframe.canvas--live[data-app-id="81"]')).toBeVisible()
  })

  test('incoming fallback reports persist only on promotion, not a failed swap', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const original = await locationFrame(page)
    await original.evaluate(() => window.openDetail())
    await expectDetail(original)
    state.manualVersions.add('2000')
    state.fallbackVersions.add('2000')
    state.version = '2000'
    await refetchLocationApps(page, state, original)
    const failed = await locationFrame(page, 81, 'incoming', '2000')
    await failed.waitForFunction(() => window.nav.location?.detail === null)
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":"notes"}')
    await failed.evaluate(() => window.fail())
    await expect(page.locator('iframe[data-frame-version="2000"]')).toHaveCount(0)
    expect((await storedLocation(page)).location).toBe('{"detail":"notes"}')

    state.manualVersions.add('3000')
    state.fallbackVersions.add('3000')
    state.version = '3000'
    await refetchLocationApps(page, state, original)
    const incoming = await locationFrame(page, 81, 'incoming', '3000')
    await incoming.waitForFunction(() => window.nav.location?.detail === null)
    expect((await storedLocation(page)).location).toBe('{"detail":"notes"}')
    await incoming.evaluate(() => window.mount())
    await locationFrame(page, 81, 'live', '3000')
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":null}')
    await page.reload({ waitUntil: 'domcontentloaded' })
    const restored = await locationFrame(page, 81, 'live', '3000')
    expect(await restored.evaluate(() => window.initialLocation)).toEqual({ detail: null })
  })

  test('a live document fallback cannot overwrite the bookmark before mounting', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    let frame = await locationFrame(page)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    state.manualVersions.add('1000')
    state.fallbackVersions.add('1000')
    await frame.goto(frame.url(), { waitUntil: 'load' })
    frame = await locationFrame(page)
    await frame.waitForFunction(() => window.nav.location?.detail === null)
    expect((await storedLocation(page)).location).toBe('{"detail":"notes"}')
    await frame.evaluate(() => window.mount())
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":null}')
  })

  test('a newer live report supersedes a fallback staged before promotion', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const original = await locationFrame(page)
    await original.evaluate(() => window.openDetail())
    await expectDetail(original)
    state.manualVersions.add('2000')
    state.fallbackVersions.add('2000')
    state.version = '2000'
    await refetchLocationApps(page, state, original)
    const incoming = await locationFrame(page, 81, 'incoming', '2000')
    await incoming.waitForFunction(() => window.nav.location?.detail === null)
    // Report a newer location at the DOM promotion boundary, before a passive
    // effect may flush the staged fallback. The latest live report must win.
    await page.evaluate(() => {
      const frame = document.querySelector('iframe.canvas--incoming[data-frame-version="2000"]')
      const observer = new MutationObserver(() => {
        if (!frame.classList.contains('canvas--live')) return
        observer.disconnect()
        window.dispatchEvent(new MessageEvent('message', {
          source: frame.contentWindow, origin: window.location.origin,
          data: { type: 'moebius:nav-location', location: '{"detail":null,"filter":"latest"}' },
        }))
      })
      observer.observe(frame, { attributes: true, attributeFilter: ['class'] })
    })
    await incoming.evaluate(() => window.mount())
    await locationFrame(page, 81, 'live', '2000')
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":null,"filter":"latest"}')
    await page.reload({ waitUntil: 'domcontentloaded' })
    const restored = await locationFrame(page, 81, 'live', '2000')
    expect(await restored.evaluate(() => window.initialLocation)).toEqual({ detail: null, filter: 'latest' })
  })

  test('promotion saves the restored screen even when the outgoing frame reported a newer place', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const outgoing = await locationFrame(page)
    await outgoing.evaluate(() => window.openDetail())
    await expectDetail(outgoing)
    state.manualVersions.add('2000')
    state.version = '2000'
    await refetchLocationApps(page, state, outgoing)
    const incoming = await locationFrame(page, 81, 'incoming', '2000')
    expect(await incoming.evaluate(() => window.initialLocation)).toEqual({ detail: 'notes' })
    await outgoing.evaluate(() => window.nav.setLocation({ detail: 'notes', filter: 'changed-during-swap' }))
    await expect.poll(async () => (await storedLocation(page))?.location)
      .toBe('{"detail":"notes","filter":"changed-during-swap"}')
    await incoming.evaluate(() => window.mount())
    const promoted = await locationFrame(page, 81, 'live', '2000')
    await expectDetail(promoted)
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":"notes"}')
  })

  test('token rotation before a wipe swap cannot adopt an outgoing document bookmark', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const outgoing = await locationFrame(page)
    await outgoing.evaluate(() => window.openDetail())
    await expectDetail(outgoing)
    // A wipe rotates the token before the apps-list/version refresh. Hold that
    // interval open so the old document can still report and receive duplicate init.
    state.instance = 'nonce-b'
    const before = await outgoing.evaluate(() => window.initCalls)
    await outgoing.evaluate(() => window.parent.postMessage({ type: 'moebius:token-expired', appId: '81' }, window.location.origin))
    await outgoing.waitForFunction(count => {
      const payload = window.lastToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
      return window.initCalls > count && JSON.parse(atob(payload)).app_nonce === 'nonce-b'
    }, before)
    await outgoing.evaluate(() => window.nav.setLocation({ detail: 'outgoing-old-data' }))
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":"outgoing-old-data"}')
    expect((await storedLocation(page)).instance).toBe('generation-a')
    state.storageGeneration = 'generation-b'
    state.version = '2000'
    await refetchLocationApps(page, state, outgoing)
    const fresh = await locationFrame(page, 81, 'live', '2000')
    expect(await fresh.evaluate(() => window.initialLocation)).toBeNull()
    expect(await fresh.evaluate(() => window.detail)).toBe(false)
  })

  test('a wipe list refresh before token refresh binds the new frame to the new storage generation', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const outgoing = await locationFrame(page)
    await outgoing.evaluate(() => window.openDetail())
    await expectDetail(outgoing)
    const staleToken = await outgoing.evaluate(() => window.lastToken)
    // Refresh only the app list first. AppCanvas deliberately still holds the
    // cached pre-wipe token while mounting the new version.
    state.instance = 'nonce-b'
    state.storageGeneration = 'generation-b'
    state.version = '2000'
    await refetchLocationApps(page, state, outgoing)
    const fresh = await locationFrame(page, 81, 'live', '2000')
    expect(await fresh.evaluate(() => window.lastToken)).toBe(staleToken)
    expect(await fresh.evaluate(() => window.initialLocation)).toBeNull()
    expect(await fresh.evaluate(() => window.detail)).toBe(false)
    await expect.poll(async () => (await storedLocation(page))?.instance).toBe('generation-b')
    // Token-expiry recovery re-initializes this document but must not change
    // its app-row binding or resurrect the pre-wipe place.
    const before = await fresh.evaluate(() => window.initCalls)
    await fresh.evaluate(() => window.parent.postMessage({ type: 'moebius:token-expired', appId: '81' }, window.location.origin))
    await fresh.waitForFunction(count => window.initCalls > count, before)
    await fresh.evaluate(() => window.nav.setLocation({ detail: null, filter: 'fresh' }))
    await expect.poll(async () => (await storedLocation(page))?.location).toBe('{"detail":null,"filter":"fresh"}')
    expect((await storedLocation(page)).instance).toBe('generation-b')
    await fresh.goto(fresh.url(), { waitUntil: 'load' })
    const reloaded = await locationFrame(page, 81, 'live', '2000')
    expect(await reloaded.evaluate(() => window.initialLocation)).toEqual({ detail: null, filter: 'fresh' })
  })

  test('settings-only changes retain the same frame document and open view', async ({ page }) => {
    const state = await setupLocationRoutes(page)
    const frame = await locationFrame(page)
    await frame.evaluate(() => window.openDetail())
    await expectDetail(frame)
    const documentId = await frame.evaluate(() => window.documentId)
    state.updatedAt = 'settings-changed'
    state.name = 'Renamed by settings'
    await refetchLocationApps(page, state, frame)
    await expect(page.locator('iframe.canvas--live[data-app-id="81"]')).toHaveAttribute('title', state.name)
    expect(await frame.evaluate(() => window.documentId)).toBe(documentId)
    expect(await frame.evaluate(() => window.detail)).toBe(true)
    await expect(page.locator('iframe[data-app-id="81"]')).toHaveCount(1)
    await page.evaluate(() => history.back())
    await frame.waitForFunction(() => !window.detail)
  })
})
