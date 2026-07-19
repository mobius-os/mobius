/**
 * Tab-strip tests for the shell.
 *
 * Tabs pin chats/apps to a strip; switching a tab is ordinary navTo, so the
 * back button rides the existing navStack. The strip shrinks .shell__content
 * by one row; the chat re-measures its spacer on the next layout event, and —
 * deliberately — does NOT remount (a remount would reset the send-reservation
 * and freeze stream-follow; see card 220).
 *
 * Runs against the deployed app with agent + apps routes intercepted — no
 * agent tokens consumed.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/tabs.spec.mjs --project=tests
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const APP_ID = 990001

test.use({ serviceWorkers: 'block' })
attachCleanup()

/** Mock agent routes, boot the shell (so localStorage/auth is reachable on
 *  the app origin), and create a worker-tagged chat. Returns the chat. */
async function bootAndCreateChat(page, label, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  const initialAppsResponse = page.waitForResponse(response =>
    response.request().method() === 'GET'
    && /\/api\/apps\/?$/.test(response.url()),
  )
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const initialApps = await initialAppsResponse
  await initialApps.finished()
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 })
  const chat = await createTaggedChat(page, label)
  return chat
}

/** Make the apps list report one app owned by `chatId`, plus a stubbed frame
 *  so the app iframe mounts cheaply. */
async function mockOwnedApp(page, chatId) {
  const state = { requests: 0 }
  await page.route(/\/api\/apps\/(\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    state.requests += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{
        id: APP_ID, name: 'Demo App', description: '', compiled_path: '',
        chat_id: chatId, source_dir: null, pinned_at: null,
        cross_app_access: 'none', share_with_apps: 'none', offline_capable: false,
        updated_at: '2026-07-12T12:00:00Z',
      }]),
    })
  })
  await page.route(new RegExp(`/api/apps/${APP_ID}/frame`), route => route.fulfill({
    status: 200, contentType: 'text/html',
    body: `<!doctype html><html><body style="margin:0"><div id="probe">app</div><script>
      document.addEventListener('pointerdown', function () {
        window.parent.postMessage({ type: 'moebius:frame-focus' }, window.location.origin)
      })
      window.addEventListener('message', function (event) {
        if (event.data && event.data.type === 'moebius:frame-interactivity') {
          document.body.dataset.interactive = String(event.data.interactive)
        }
      })
    </script></body></html>`,
  }))
  // AppCanvas deliberately waits for a scoped app token while online. Without
  // this protocol mock the shell renders its loading surface, so an iframe
  // lifecycle assertion would be testing an impossible half-mocked app.
  await page.route(/\/api\/auth\/app-token$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ token: 'mock-app-token' }),
  }))
  return state
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 4000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

async function measure(page) {
  return page.evaluate(() => {
    const content = document.querySelector('.shell__content')
    const chat = document.querySelector('.chat')
    const scroll = document.querySelector('.chat__scroll')
    const spacer = document.querySelector('.spacer-dynamic')
    return {
      contentH: content?.offsetHeight || 0,
      chatH: chat?.offsetHeight || 0,
      scrollClientH: scroll?.clientHeight || 0,
      spacerH: parseInt(spacer?.style.height) || 0,
    }
  })
}

/** Multi-pane strips and their active content wrappers share one projected
 *  pane rectangle: their left edges and widths match, and the strip ends where
 *  pane content begins. Report rounded deltas so the assertion remains about
 *  layout ownership rather than sub-pixel device-scale noise. */
async function paneChromeDeltas(page) {
  return page.locator('.workspace__strip').evaluateAll(strips => strips.map((strip) => {
    const active = strip.querySelector('.shell__tab--active .shell__tab-open')
    const key = active?.dataset.dragKey
    const pane = key
      ? document.querySelector(`.shell__view--paned[data-tab-key="${CSS.escape(key)}"]`)
      : null
    if (!pane) return null
    const stripRect = strip.getBoundingClientRect()
    const paneRect = pane.getBoundingClientRect()
    return {
      left: Math.round(stripRect.left - paneRect.left),
      width: Math.round(stripRect.width - paneRect.width),
      seam: Math.round(stripRect.bottom - paneRect.top),
    }
  }))
}

/** Sample pane/strip alignment on every animation frame while the navigation
 *  toggle changes the desktop content width. A settled-only assertion misses
 *  transient animation restarts, which is exactly when the pane can drift away
 *  from its strip. */
async function toggleAndSamplePaneChrome(page) {
  return page.evaluate(async () => {
    const maximum = { left: 0, width: 0, seam: 0 }
    let frames = 0
    let comparisons = 0
    const sample = () => {
      for (const strip of document.querySelectorAll('.workspace__strip')) {
        const active = strip.querySelector('.shell__tab--active .shell__tab-open')
        const key = active?.dataset.dragKey
        const pane = key
          ? document.querySelector(`.shell__view--paned[data-tab-key="${CSS.escape(key)}"]`)
          : null
        if (!pane) continue
        const stripRect = strip.getBoundingClientRect()
        const paneRect = pane.getBoundingClientRect()
        maximum.left = Math.max(maximum.left, Math.abs(stripRect.left - paneRect.left))
        maximum.width = Math.max(maximum.width, Math.abs(stripRect.width - paneRect.width))
        maximum.seam = Math.max(maximum.seam, Math.abs(stripRect.bottom - paneRect.top))
        comparisons += 1
      }
      frames += 1
    }

    const toggle = document.querySelector('button[aria-label="Toggle navigation"]')
    if (!toggle) throw new Error('navigation toggle not found')
    await new Promise(resolve => {
      let startedAt = null
      const tick = (now) => {
        if (startedAt === null) {
          startedAt = now
          sample()
          toggle.click()
        }
        sample()
        if (now - startedAt >= 650) resolve()
        else requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    return { maximum, frames, comparisons, expanded: toggle.getAttribute('aria-expanded') }
  })
}

async function seedTabs(page, tabs, { viewMode } = {}) {
  // Builder mode ('panes', the default) now ALWAYS shows the strip, so the
  // single-pane strip's pinning/unpinning contract (pin engages, unpin to zero
  // disengages -> strip retires) lives in SINGLE-screen mode; callers asserting
  // that flow seed viewMode: 'single'.
  let ws = paneModel.seedFromFlatTabs(tabs)
  if (viewMode) ws = paneModel.setViewMode(ws, viewMode)
  const workspace = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([workspaceKey, workspaceRaw, legacyKey, t]) => {
    try {
      // Match the shell's dual-write persistence contract. The versioned
      // workspace is authoritative; the flat key only supports one-release
      // rollback and may already contain an older projection.
      sessionStorage.setItem(workspaceKey, workspaceRaw)
      sessionStorage.setItem(legacyKey, JSON.stringify(t))
    } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, workspace, 'mobius-open-tabs', tabs])
}

// Single-SCREEN workspace holding just `chatId`, with an EMPTY legacy mirror so
// the strip is unengaged. A valid blob remains authoritative through a cold
// ?chat= deep-link; the target opens into that workspace without RESET_FLAT
// replacing its preserved view mode.
async function seedSingleModeChat(page, chatId) {
  const ws = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([{ kind: 'chat', id: chatId }]), 'single')
  const workspace = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([workspaceKey, workspaceRaw]) => {
    try {
      sessionStorage.setItem(workspaceKey, workspaceRaw)
      sessionStorage.setItem('mobius-open-tabs', '[]')
    } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, workspace])
}

test.describe('Tabs', () => {
  test('strip shows pinned tabs, switches, closes, and keeps the spacer sane', async ({ page }) => {
    const chat = await bootAndCreateChat(page, 'tabs')
    const appsMock = await mockOwnedApp(page, chat.id)
    // Single-screen: builder always shows the strip, so the "close the last tab ->
    // strip disappears -> chat back to full height" contract is a single-screen one.
    await seedTabs(page, [{ kind: 'chat', id: chat.id }, { kind: 'app', id: APP_ID }], { viewMode: 'single' })

    await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => appsMock.requests, { timeout: 5000 }).toBeGreaterThan(0)
    await sendMessage(page, 'build me a thing')

    // Strip renders both tabs; exactly one (the current chat) is active.
    await expect(page.locator('.shell__tabstrip')).toHaveCount(1)
    await expect(page.locator('.shell__tab')).toHaveCount(2)
    await expect(page.locator('.shell__tab--active')).toHaveCount(1)
    await expect(page.locator('.shell__tab', { hasText: 'Demo App' })).toHaveCount(1)

    // With the strip present, the chat spacer must not exceed the pane.
    await page.waitForTimeout(200)
    const withStrip = await measure(page)
    expect(withStrip.spacerH).toBeLessThanOrEqual(withStrip.scrollClientH)

    // Tap the app tab — ordinary navigation to the canvas view.
    await page.locator('.shell__tab', { hasText: 'Demo App' }).locator('.shell__tab-open').click()
    await expect(page.locator('.shell__view--active')).toBeVisible({ timeout: 3000 })

    // Tap the chat tab (the one that is NOT the app) — back to the chat.
    await page.getByRole('button', { name: chat.title, exact: true }).click()
    await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 3000 })

    // Close the app tab — one fewer tab, strip stays.
    await page.locator('.shell__tab', { hasText: 'Demo App' }).locator('.shell__tab-close').click()
    await expect(page.locator('.shell__tab')).toHaveCount(1)

    // Close the last tab — strip disappears, chat back to full height, spacer sane.
    await page.locator('.shell__tab-close').first().click()
    await expect(page.locator('.shell__tabstrip')).toHaveCount(0)
    await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 3000 })
    await page.waitForTimeout(300)
    const noStrip = await measure(page)
    expect(noStrip.chatH / noStrip.contentH).toBeGreaterThan(0.9)
    expect(noStrip.spacerH).toBeLessThanOrEqual(noStrip.scrollClientH)
  })

  test('no toggle/strip surface when nothing is pinned (single-screen)', async ({ page }) => {
    const chat = await bootAndCreateChat(page, 'notabs')
    const appsMock = await mockOwnedApp(page, chat.id)
    // "Nothing pinned -> no strip" is a SINGLE-screen contract now that builder mode
    // always surfaces the strip. Restore single-screen from a valid blob THROUGH
    // the real cold deep-link path; that blob must remain authoritative even though
    // its intentionally-empty legacy mirror looks like an implicit home fallback.
    await seedSingleModeChat(page, chat.id)
    await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => appsMock.requests, { timeout: 5000 }).toBeGreaterThan(0)
    await expect.poll(() => page.evaluate(
      key => JSON.parse(sessionStorage.getItem(key))?.viewMode,
      paneModel.STORAGE_KEY,
    )).toBe('single')
    await sendMessage(page, 'just a chat')
    await expect(page.locator('.shell__tabstrip')).toHaveCount(0)
    // The parked split view left no toggle behind.
    await expect(page.locator('.shell__split-toggle')).toHaveCount(0)
  })

  // Regression for the review's HIGH finding: an app opened numerically (drawer/
  // deep-link) then re-opened via its tab (string id) must not double-mount —
  // the LRU dedups on strict !==, so a string id would sit beside the number.
  test('switching to an app tab does not double-mount the iframe', async ({ page }) => {
    const chat = await bootAndCreateChat(page, 'dup')
    const appsMock = await mockOwnedApp(page, chat.id)
    await seedTabs(page, [{ kind: 'chat', id: chat.id }, { kind: 'app', id: APP_ID }])

    const keyErrors = []
    page.on('console', m => {
      if (m.type() === 'error' && /same key|two children/i.test(m.text())) keyErrors.push(m.text())
    })

    // Open the app numerically first — appCache holds a Number id.
    await page.goto(`${BASE}/shell/?app=${APP_ID}`, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => appsMock.requests, { timeout: 5000 }).toBeGreaterThan(0)
    await expect(page.locator('.shell__view--active')).toBeVisible({ timeout: 5000 })

    // Switch to the chat tab (wait for it to settle), then back to the app tab
    // (string id → Number()). Waiting between taps keeps the sequence
    // deterministic under multi-worker load.
    await page.getByRole('button', { name: chat.title, exact: true }).click()
    await expect(page.locator('.chat__scroll, .chat__empty-wrap')).toBeVisible({ timeout: 3000 })
    await page.locator('.shell__tab', { hasText: 'Demo App' }).locator('.shell__tab-open').click()
    await expect(page.locator('.shell__view--active')).toBeVisible({ timeout: 3000 })
    await page.waitForTimeout(400)

    // Exactly one iframe wrapper for the single app — no string/number duplicate.
    await expect(page.locator('.shell__view')).toHaveCount(1)
    expect(keyErrors, keyErrors.join('\n')).toHaveLength(0)
  })

  test('split mode tiles, focuses both content types, suspends apps, and collapses cleanly', async ({ page }) => {
    const chat = await bootAndCreateChat(page, 'split', { width: 1200, height: 800 })
    const appsMock = await mockOwnedApp(page, chat.id)
    await seedTabs(page, [{ kind: 'chat', id: chat.id }, { kind: 'app', id: APP_ID }])
    await page.addInitScript(() => localStorage.setItem('mobius:workspace-splits', '1'))

    await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
    await expect.poll(() => appsMock.requests, { timeout: 5000 }).toBeGreaterThan(0)

    const appTab = page.locator('.shell__tab', { hasText: 'Demo App' }).locator('.shell__tab-open')
    await appTab.click({ button: 'right' })
    await page.getByRole('menuitem', { name: 'Split right' }).click()

    await expect(page.locator('.workspace__strip')).toHaveCount(2)
    await expect(page.locator('.shell__view--paned')).toHaveCount(2)
    // Layout commits animate over 180ms (the pane bloom), so geometry must be
    // polled until it settles rather than read on the first post-click frame.
    await expect.poll(async () => {
      const rects = await page.locator('.shell__view--paned').evaluateAll(nodes => nodes.map(node => {
        const r = node.getBoundingClientRect()
        return { x: r.x, y: r.y, w: r.width, h: r.height }
      }))
      return rects.every(r => r.w >= 280 && r.h >= 200)
        && Math.abs(rects[0].x - rects[1].x) > 100
    }).toBe(true)

    // The sidebar offsets the outer content container exactly once. Per-pane
    // strips stay in that container's coordinate system through both toggle
    // directions instead of inheriting the global 320px margin themselves.
    const aligned = [
      { left: 0, width: 0, seam: 0 },
      { left: 0, width: 0, seam: 0 },
    ]
    const navigationToggle = page.getByRole('button', { name: 'Toggle navigation' })
    await expect(navigationToggle).toHaveAttribute('aria-expanded', 'true')
    await expect.poll(() => paneChromeDeltas(page)).toEqual(aligned)
    const closingMotion = await toggleAndSamplePaneChrome(page)
    expect(closingMotion.expanded).toBe('false')
    expect(closingMotion.frames).toBeGreaterThan(5)
    expect(closingMotion.comparisons).toBeGreaterThan(10)
    expect(Math.max(...Object.values(closingMotion.maximum))).toBeLessThanOrEqual(1)
    await expect.poll(() => paneChromeDeltas(page)).toEqual(aligned)
    const openingMotion = await toggleAndSamplePaneChrome(page)
    expect(openingMotion.expanded).toBe('true')
    expect(openingMotion.frames).toBeGreaterThan(5)
    expect(openingMotion.comparisons).toBeGreaterThan(10)
    expect(Math.max(...Object.values(openingMotion.maximum))).toBeLessThanOrEqual(1)
    await expect.poll(() => paneChromeDeltas(page)).toEqual(aligned)

    // Native chat content focuses its pane through wrapper capture.
    await page.locator('.chat').dispatchEvent('pointerdown')
    await expect(page.locator('.workspace__strip--focused')).toContainText(chat.title)

    // Opaque iframe input uses the explicit frame-focus bridge.
    await page
      .frameLocator(`iframe[data-app-id="${APP_ID}"]`)
      .locator('#probe')
      .dispatchEvent('pointerdown')
    await expect(page.locator('.workspace__strip--focused')).toContainText('Demo App')

    // A docked desktop sidebar is ordinary layout and deliberately leaves app
    // panes interactive. Switch to the modal breakpoint before asserting the
    // global-drawer suspension contract.
    await page.setViewportSize({ width: 900, height: 800 })
    await expect(page.locator('.drawer')).not.toHaveClass(/drawer--persistent/)
    await page.locator('.shell__brand').click()
    await expect(page.locator('.drawer')).toHaveClass(/drawer--open/)
    await expect(page
      .frameLocator(`iframe[data-app-id="${APP_ID}"]`)
      .locator('body'))
      .toHaveAttribute('data-interactive', 'false')
    await page.evaluate(() => history.back())
    await expect(page.locator('.drawer')).not.toHaveClass(/drawer--open/)

    // Closing the app's sole pane collapses back to the unchanged single-pane UI.
    await page.locator('.workspace__strip', { hasText: 'Demo App' }).locator('.shell__tab-close').click()
    await expect(page.locator('.workspace__strip')).toHaveCount(0)
    await expect(page.locator('.shell__tabstrip')).toHaveCount(1)
    await expect(page.locator('.chat')).toBeVisible()
  })
})
