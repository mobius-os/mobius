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
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'


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
  // Safety net registered FIRST so it has the LOWEST precedence (Playwright
  // matches routes last-registered-first). Any /api/ call the mounted Shell
  // makes that no specific mock below covers gets a benign 200 instead of
  // falling through to the real container, where the test's fake owner token
  // 401s and the api client reloads the page mid-test (the failure mode that
  // broke this whole spec when a new mount-time query — /api/owner/model-prefs
  // — was added upstream). The specific mocks below still win on their paths;
  // this only catches the gaps, so a future Shell-mount query can't silently
  // reintroduce the 401-reload flake.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
  })
  await page.route(/\/api\/health$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ok: true }),
    })
  )
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
  // Shell fires this on mount whenever a chat is active (the composer's
  // model picker prefetch — `modelQueries.prefs`, enabled on activeChatId).
  // The saved auth storageState restores `moebius_active_chat`, so it IS
  // active here. Leaving it unmocked sends the test's fake owner token to
  // the real container, which 401s, and the api client's 401 handler calls
  // `window.location.reload()` — that reload destroys the page mid-test
  // (iframe goes null → contentWindow throws; "execution context destroyed").
  // Mock EVERY endpoint the mounted Shell touches so no real 401 can fire.
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
  await page.addInitScript(() => {
    localStorage.setItem('token', 'mock-owner-token')
  })
  await setupShellBasics(page)

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

async function setupOpenAppRoutesWithStaleInitialList(page, appId, frameHTML) {
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
    run_status: null,
  }
  const app = {
    id: appId,
    name: 'CubeRun',
    slug: 'cuberun',
    description: 'test',
    compiled_path: `/data/compiled/app-${appId}.js`,
    chat_id: null,
    source_dir: `/data/apps/cuberun`,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }

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
      body: JSON.stringify(appsFetches === 1 ? [] : [app]),
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

  return { getAppsFetches: () => appsFetches }
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

async function postFromOpaqueCanvasFrame(page, data) {
  await page.evaluate(() => {
    const frame = document.createElement('iframe')
    frame.className = 'canvas canvas--test-sender'
    frame.setAttribute('sandbox', 'allow-scripts')
    frame.srcdoc = '<!doctype html><script>window.__ready = true<\/script>'
    document.body.appendChild(frame)
  })
  const frame = await waitForContentFrame(page, 'iframe.canvas--test-sender')
  await frame.waitForFunction(() => window.__ready === true)
  await frame.evaluate((message) => {
    window.parent.postMessage(message, '*')
  }, data)
}


test.describe('AppCanvas: iframe-mount contract', () => {
  // Block the service worker for this whole file. Every test here relies on
  // Playwright's page.route intercepting the /api calls the shell makes — but
  // sw.js serves /api/chats and /api/apps/ NetworkFirst and /api/theme
  // StaleWhileRevalidate, and once the SW activates + claims the page (~1s into
  // the first load) its own fetch()es go straight to the network, BYPASSING
  // page.route. A test that outlives that claim window then leaks a mount-time
  // GET to the real container, which 401s the fake owner token; the api
  // client's global 401 handler clears the token and reloads, and the reload
  // loop tears the page down mid-assertion. (The app-error swap test below is
  // the one long enough to trip it — its live-frame crash forwards correctly
  // and POSTs /api/chats, but the follow-up refreshApps/refreshChats GETs ride
  // the SW to a real 401 and the new chat's composer never survives the
  // reload.) Blocking the SW keeps every request on the page-route path — the
  // same reason auth.setup.mjs blocks it. None of these tests exercise SW
  // behavior, so nothing is lost.
  test.use({ serviceWorkers: 'block' })

  test('loading spinner hides as soon as frame posts moebius:frame-mounted', async ({ page }) => {
    const appId = 99
    // Mount only on the test's signal — NOT a timer. The old
    // mountDelayMs:50 auto-mount made this flaky: on a fast/variable
    // container the spinner could hide before the toBeVisible below
    // observed it (a transient-state race, not a real timing bound).
    // With test-controlled mount the spinner is reliably visible first,
    // then reliably hidden after we trigger mount — deterministic.
    await setupAppRoutes(page, appId, mockFrameHTML(appId, { sendMounted: false, mountOnSignal: true }))

    await page.goto(`${BASE}/app/${appId}`, { waitUntil: 'domcontentloaded' })

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

    await page.goto(`${BASE}/app/${appId}`, { waitUntil: 'domcontentloaded' })

    // 10s (was 5s) — this waits for the genuinely slow cold-CI first-app
    // mount to render the spinner, which is a real state, not a race; 5s
    // was too tight for the cold container and made this flaky.
    await expect(page.locator('.canvas-loading')).toBeVisible({ timeout: 10000 })

    // Give the iframe a full second to fire onLoad and any
    // alternative signals — spinner must still be there.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))
    await expect(page.locator('.canvas-loading')).toBeVisible()
  })

  test('open-app message refetches stale app list before staying on chat', async ({ page }) => {
    const appId = 55
    const routes = await setupOpenAppRoutesWithStaleInitialList(
      page,
      appId,
      mockFrameHTML(appId, { sendMounted: true })
    )

    await page.goto(`${BASE}/shell/?chat=open-app-chat`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => window.location.pathname === '/shell/')

    await postFromOpaqueCanvasFrame(page, {
      type: 'moebius:open-app', appId,
    })

    await expect(page.locator(`iframe[data-app-id="${appId}"]`)).toBeVisible({ timeout: 8000 })
    await expect(page.locator('.canvas-loading')).toBeHidden({ timeout: 8000 })
    expect(routes.getAppsFetches()).toBeGreaterThanOrEqual(2)
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

    // Digit-string updated_at values double as the frame version keys
    // (appVersionKey passes them through): while the app sits at '1000' the
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
    const appRow = (updatedAt) => ({
      id: appId,
      name: 'CrashToy',
      slug: 'crashtoy',
      description: 'test',
      compiled_path: `/data/compiled/app-${appId}.js`,
      chat_id: null,
      source_dir: null,
      created_at: '1000',
      updated_at: updatedAt,
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
            run_status: null,
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
          run_status: null,
          provider: 'claude',
        }),
      })
    )
    await page.route(/\/api\/chats\/[^/]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )

    await page.goto(`${BASE}/app/${appId}`, { waitUntil: 'domcontentloaded' })
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
    // open-app target refetches once before giving up) — the bumped updated_at
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
