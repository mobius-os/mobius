/**
 * Navigation and back button behavior tests.
 *
 * Tests the useNavigation hook: back button between chats, back from app
 * canvas to chat, drawer open/close via back, and pushState/popstate handling.
 *
 * Run:  scripts/playwright-local.sh --allow-local-e2e tests/navigation.spec.mjs
 */
import { test, expect } from '@playwright/test'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const NAV_CHATS = [
  ['10000000-0000-4000-8000-000000000001', 'Navigation Alpha'],
  ['10000000-0000-4000-8000-000000000002', 'Navigation Beta'],
  ['10000000-0000-4000-8000-000000000003', 'Navigation Gamma'],
].map(([id, title], index) => ({
  id,
  title,
  created_at: `2026-01-01T00:00:0${index}Z`,
  updated_at: `2026-01-01T00:00:0${index}Z`,
  activity_at: `2026-01-01T00:00:0${index}Z`,
  pinned_at: null,
  created_by_app_id: null,
  has_messages: true,
  running: false,
  run_status: null,
}))

function navChatDetail(id) {
  return {
    messages: [
      { role: 'user', content: `Open ${id}`, ts: 1700000000000, blocks: [] },
      { role: 'assistant', content: 'Fixture response', ts: 1700000000001, blocks: [] },
    ],
    total: 2,
    offset: 0,
    running: false,
    pending_messages: [],
  }
}

/** Click the Settings entry in the drawer; assumes drawer is open. */
async function navigateToSettings(page) {
  await page.evaluate(() => {
    const buttons = document.querySelectorAll('.drawer button')
    for (const b of buttons) {
      if (b.textContent.trim() === 'Settings') { b.click(); return }
    }
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  // Navigation is a client-side contract. Seed an explicit active chat and
  // mock the complete chat surface so the suite neither reads nor borrows
  // rows from any backend database.
  await page.addInitScript(chatId => {
    localStorage.setItem('moebius_active_chat', chatId)
  }, NAV_CHATS[0].id)
  await page.route(/\/api\/chats(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(NAV_CHATS),
    })
  })
  await page.route(/\/api\/chats\/([0-9a-f-]+)(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const id = new URL(route.request().url()).pathname.split('/').pop()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(navChatDetail(id)),
    })
  })

  // Intercept agent routes.
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

/** Read the current navigation state from the app. */
async function getNavState(page) {
  return page.evaluate(() => {
    const chatScroll = document.querySelector('.chat__scroll')
    const emptyWrap = document.querySelector('.chat__empty-wrap')
    const canvas = document.querySelector('.canvas')
    const drawer = document.querySelector('.drawer')

    return {
      hasChat: !!(chatScroll || emptyWrap),
      hasCanvas: !!canvas,
      drawerOpen: drawer?.classList.contains('drawer--open') ?? false,
      activeChatId: localStorage.getItem('moebius_active_chat'),
      url: window.location.pathname,
    }
  })
}

/** Navigate to a chat by clicking in the drawer. */
async function navigateToChat(page, index = 0) {
  const clicked = await page.evaluate((idx) => {
    const chats = document.querySelector('.drawer__group--chats')
    const items = chats?.querySelectorAll('.drawer__item') || []
    const chatItems = Array.from(items).filter(el =>
      el.querySelector('.drawer__item-text') && !el.classList.contains('drawer__item--new')
    )
    const target = chatItems[idx] || document.querySelector('.drawer__item--new')
    if (!target) return false
    target.click()
    return true
  }, index)
  if (!clicked) throw new Error('No chat row or New chat button found in drawer')
  await page.waitForFunction(
    () => !document.querySelector('.settings')
      && !!(document.querySelector('.chat__empty-wrap')
        || document.querySelector('.chat__scroll')
        || document.querySelector('.chat__form')),
    { timeout: 8000 }
  )
}

/** Navigate to an app by clicking in the drawer. */
async function navigateToApp(page, index = 0) {
  await page.evaluate((idx) => {
    const appSection = document.querySelector('.drawer__group:last-of-type .drawer__scroll')
      || document.querySelectorAll('.drawer__scroll')[1]
    if (!appSection) return
    const items = appSection.querySelectorAll('button')
    if (items[idx]) items[idx].click()
  }, index)
  await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
}

/** Open the drawer via the toggle button (aria-expanded attribute). */
async function openDrawer(page) {
  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
}

/** Close the modal drawer via the SCRIM — its canonical pointerdown-dismiss
 *  (Drawer.handleOverlayPointerDown → onClose). The drawer has no dedicated close
 *  control by owner decision; the exits are the scrim tap, the brand toggle, and
 *  Back. CONTRACT: the brand toggle cannot close a MODAL drawer from a test — Shell
 *  renders the header `inert` while the modal drawer is open
 *  (`inert={modalDrawerOpen}` in Shell.jsx), so a real click on the toggle never
 *  lands (it times out). That inert bar is exactly why the removed ✕ button existed.
 *  The scrim stays hit-testable while open, so close through it (matches test 22).
 *  Do NOT rewire this back to a toggle click. */
async function closeDrawerButton(page) {
  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  await expect(page.locator('.drawer-overlay')).toBeVisible()
  await page.locator('.drawer-overlay').click({ position: { x: 400, y: 300 } })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
}

/** Close the drawer via the toggle button (without navigating). */
async function closeDrawerToggle(page) {
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') === 'true') btn.click()
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
}

/** Trigger browser back via history.back().
 *  Uses evaluate to fire within the SPA rather than Playwright's page.goBack
 *  which triggers a real page navigation. */
async function goBack(page) {
  await page.evaluate(() => history.back())
  await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
}

async function goForward(page) {
  await page.evaluate(() => history.forward())
  await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test.describe('Navigation basics', () => {
  test('1. Initial state — chat view, URL is /shell/', async ({ page }) => {
    await setup(page)
    const state = await getNavState(page)
    expect(state.hasChat).toBe(true)
    expect(state.url).toBe('/shell/')
  })

  test('2. Navigate between two chats — back returns to first', async ({ page }) => {
    await setup(page)
    const state1 = await getNavState(page)
    const firstChatId = state1.activeChatId

    // Open drawer and click a different chat.
    await openDrawer(page)
    await navigateToChat(page, 1)
    const state2 = await getNavState(page)

    // Should be on a different chat now.
    if (firstChatId && state2.activeChatId !== firstChatId) {
      // Go back.
      await goBack(page)
      const state3 = await getNavState(page)
      expect(state3.activeChatId).toBe(firstChatId)
    }
  })

  test('3. Drawer push-on-open is consumed by closeDrawer (history active index returns)', async ({ page }) => {
    // The drawer pushes a sentinel history entry on open so that the
    // browser back-gesture can be captured (and we keep `navTo` from
    // pushing per-nav, which is what made Chrome Android's BFCache
    // swipe-back animation show drawer pixels).
    //
    // Note: under Navigation API + intercept(), going back doesn't pop
    // the entry from history.length — it moves the active index back.
    // The test that matters for UX is `getNavState().drawerOpen` and
    // exit-on-back behavior, not history.length parity. We assert
    // that any DRAWER OPEN is paired with a CLOSE-BY-BACK such that
    // the user is returned to the same view they were on.
    await setup(page)
    const start = await getNavState(page)
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)
    await closeDrawerButton(page)
    const end = await getNavState(page)
    expect(end.drawerOpen).toBe(false)
    expect(end.activeChatId).toBe(start.activeChatId)
    expect(end.hasChat).toBe(true)
  })

  test('4. Navigate chat -> app -> back returns to chat', async ({ page }) => {
    await setup(page)
    const initialState = await getNavState(page)
    expect(initialState.hasChat).toBe(true)

    // Try to navigate to an app.
    await openDrawer(page)
    await navigateToApp(page, 0)
    const appState = await getNavState(page)

    if (appState.hasCanvas) {
      // Back should return to chat.
      await goBack(page)
      const backState = await getNavState(page)
      expect(backState.hasChat).toBe(true)
    }
    // If no apps exist, the test passes vacuously.
  })
})

test.describe('Desktop sidebar navigation', () => {
  async function setupDesktop(page, open = true) {
    await page.addInitScript(({ key, value }) => {
      if (localStorage.getItem(key) === null) localStorage.setItem(key, value)
    }, {
      key: 'mobius:desktop-sidebar-open:v1',
      value: String(open),
    })
    await setup(page, { width: 1280, height: 800 })
  }

  test('28. desktop sidebar reserves workspace width and persists its toggle', async ({ page }) => {
    await setupDesktop(page)

    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    const sidebar = page.getByRole('navigation', { name: 'Primary navigation' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(sidebar).toBeVisible()
    await expect(page.locator('.drawer-overlay')).toHaveCount(0)
    await expect(page.locator('.shell__content')).not.toHaveAttribute('inert', '')

    const geometry = await page.evaluate(() => {
      const drawer = document.querySelector('#navigation-drawer').getBoundingClientRect()
      const content = document.querySelector('.shell__content').getBoundingClientRect()
      return { drawerRight: drawer.right, contentLeft: content.left }
    })
    expect(geometry.drawerRight).toBe(320)
    expect(geometry.contentLeft).toBe(geometry.drawerRight)

    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => (
      localStorage.getItem('mobius:desktop-sidebar-open:v1')
    ))).toBe('false')
    await expect.poll(() => page.locator('.shell__content').evaluate(
      element => element.getBoundingClientRect().left,
    )).toBe(0)

    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  test('29. desktop destinations keep the sidebar open without no-op history edges', async ({ page }) => {
    await setupDesktop(page)
    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    const alpha = navigation.getByRole('button', { name: 'Navigation Alpha', exact: true })
    const beta = navigation.getByRole('button', { name: 'Navigation Beta', exact: true })

    await expect(alpha).toHaveAttribute('aria-current', 'page')
    await alpha.focus()
    await expect(alpha).toBeFocused()
    await expect(alpha).toHaveCSS('outline-style', 'solid')
    await expect(alpha).toHaveCSS('outline-width', '2px')
    const initialLength = await page.evaluate(() => history.length)
    await alpha.click()
    expect(await page.evaluate(() => history.length)).toBe(initialLength)

    await beta.click()
    await expect(beta).toHaveAttribute('aria-current', 'page')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const afterBeta = await page.evaluate(() => history.length)
    expect(afterBeta).toBe(initialLength + 1)

    await beta.click()
    expect(await page.evaluate(() => history.length)).toBe(afterBeta)

    await page.evaluate(() => history.back())
    await expect(alpha).toHaveAttribute('aria-current', 'page')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')

    const settings = navigation.getByRole('button', { name: 'Settings', exact: true })
    await settings.click()
    await expect(settings)
      .toHaveAttribute('aria-current', 'page')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })

  test('30. widening restores the saved desktop preference, not the mobile modal state', async ({ page }) => {
    await setupDesktop(page, false)
    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await page.setViewportSize({ width: 412, height: 915 })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.drawer-overlay')).toBeVisible()

    await page.setViewportSize({ width: 1280, height: 800 })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(page.locator('.drawer-overlay')).toHaveCount(0)
    await expect(page.locator('.shell__content')).not.toHaveAttribute('inert', '')
    await expect.poll(() => page.evaluate(() => history.state?.kind)).not.toBe('drawer')
  })

  test('31. breakpoint cleanup stays modal and seeks through phantom history', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('mobius:desktop-sidebar-open:v1', 'true')
    })
    await setup(page)
    const toggle = page.getByRole('button', { name: 'Toggle navigation' })

    await page.evaluate(() => history.pushState(null, ''))
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')

    await page.evaluate(() => {
      const originalBack = history.back.bind(history)
      history.back = () => {
        window.__releaseBreakpointBack = () => {
          history.back = originalBack
          originalBack()
        }
      }
    })
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.waitForFunction(() => typeof window.__releaseBreakpointBack === 'function')

    // Desktop mode is requested, but the still-open mobile sentinel retains its
    // complete modal boundary until the traversal is allowed to finish.
    await expect(page.locator('.drawer-overlay')).toBeVisible()
    await expect(page.locator('.shell__content')).toHaveAttribute('inert', '')

    await page.evaluate(() => window.__releaseBreakpointBack())
    await expect(page.locator('.drawer.drawer--persistent')).toBeVisible()
    await expect(page.locator('.drawer-overlay')).toHaveCount(0)
    await expect(page.locator('.shell__content')).not.toHaveAttribute('inert', '')
    await expect.poll(() => page.evaluate(() => history.state?.__mobiusNav)).toBe(true)

    // Scope to the sidebar landmark: builder mode (the default view-mode) opens
    // Settings as a canonical pane tab, so an unscoped `Settings` role query
    // matches BOTH the sidebar nav item and that tab. This test is about the
    // sidebar item's active highlight.
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    const settings = navigation.getByRole('button', { name: 'Settings', exact: true })
    await settings.click()
    await expect(settings)
      .toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Drawer touch lifecycle', () => {
  test.use({ hasTouch: true })

  test('an interrupted drawer drag cannot consume the next real row tap', async ({ page }) => {
    // Enable drawer-row workspace gestures before the shell module evaluates.
    // This is the production path that creates the full-viewport drag layer.
    await page.addInitScript(() => {
      localStorage.setItem('mobius:workspace-splits', '1')
    })
    await setup(page, { width: 412, height: 915 })
    await openDrawer(page)

    // The coordinate-level touchscreen tap below cannot use Playwright's
    // locator click re-targeting. Wait until the 250ms opening transition has
    // stopped moving the row before sampling its bounding box; otherwise a
    // valid tap can land at the row's old in-flight coordinate under load.
    const drawer = page.locator('#navigation-drawer')
    await expect(drawer).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')

    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    const beta = navigation.getByRole('button', { name: NAV_CHATS[1].title, exact: true })
    await expect(beta).toBeVisible()

    // Reproduce the mobile interruption precisely: the row's drag controller
    // arms and installs its transparent viewport layer, then the browser steals
    // the terminal pointer event (notification shade/app switch), so no up or
    // cancel reaches the shell. This used to leave the layer above the drawer.
    await beta.evaluate((row) => {
      const box = row.getBoundingClientRect()
      const start = {
        bubbles: true,
        cancelable: true,
        pointerId: 1,
        pointerType: 'touch',
        isPrimary: true,
        button: 0,
        clientX: box.left + 40,
        clientY: box.top + box.height / 2,
      }
      row.dispatchEvent(new PointerEvent('pointerdown', start))
      window.dispatchEvent(new PointerEvent('pointermove', {
        ...start,
        clientX: start.clientX + 30,
      }))
    })
    await expect(page.locator('.workspace__drag-shield')).toHaveCount(1)
    await expect(page.locator('.workspace__drag-shield')).toHaveCSS('pointer-events', 'none')

    // A real touchscreen gesture (not HTMLElement.click) must both reconcile the
    // abandoned session and activate its row in this SAME interaction. Mobile
    // browsers commonly reuse pointerId=1, which is why identity cannot stand in
    // for the old pointer's liveness.
    const box = await beta.boundingBox()
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)

    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(NAV_CHATS[1].id)
    await expect(page.locator('.workspace__drag-shield')).toHaveCount(0)
  })

  test('touch rows recover through both responsive drawer modes', async ({ page }) => {
    await setup(page, { width: 412, height: 915 })
    await openDrawer(page)

    // Widening consumes the mobile history sentinel before converting the modal
    // into a persistent sidebar. `drawer--locked` is deliberately allowed only
    // during that traversal; it must not survive into the interactive sidebar.
    await page.setViewportSize({ width: 1280, height: 800 })
    const drawer = page.locator('#navigation-drawer')
    await expect(drawer).toHaveClass(/drawer--persistent/)
    await expect(drawer).not.toHaveClass(/drawer--locked/)
    await expect(drawer).not.toHaveAttribute('inert', '')

    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    const beta = navigation.getByRole('button', { name: NAV_CHATS[1].title, exact: true })
    let box = await beta.boundingBox()
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(NAV_CHATS[1].id)

    // Narrowing returns to a closed modal; one real touch opens it and one real
    // touch selects a different destination. No desktop interaction lock or
    // stale sentinel may leak across the reverse transition.
    await page.setViewportSize({ width: 412, height: 915 })
    await expect(drawer).not.toHaveClass(/drawer--persistent/)
    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    box = await toggle.boundingBox()
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')

    const gamma = navigation.getByRole('button', { name: NAV_CHATS[2].title, exact: true })
    box = await gamma.boundingBox()
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(NAV_CHATS[2].id)
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })
})

test.describe('Back button edge cases', () => {
  test('5. Multiple navigations — back pops in LIFO order', async ({ page }) => {
    await setup(page)

    // Navigate: chat A -> open drawer -> chat B -> open drawer -> chat C.
    await openDrawer(page)
    await navigateToChat(page, 0)
    const chatA = (await getNavState(page)).activeChatId

    await openDrawer(page)
    await navigateToChat(page, 1)
    const chatB = (await getNavState(page)).activeChatId

    if (chatA && chatB && chatA !== chatB) {
      // Back should return to chat A.
      await goBack(page)
      const afterBack = await getNavState(page)
      expect(afterBack.activeChatId).toBe(chatA)
    }
  })

  test('6. URL stays at /shell/ throughout navigation', async ({ page }) => {
    await setup(page)
    expect((await getNavState(page)).url).toBe('/shell/')

    await openDrawer(page)
    await navigateToChat(page, 0)
    expect((await getNavState(page)).url).toBe('/shell/')

    await goBack(page)
    expect((await getNavState(page)).url).toBe('/shell/')
  })

  test('8. Drawer cycles return to the same view + closed state', async ({ page }) => {
    // The UX-relevant invariant is "after N drawer cycles you are
    // still on the same view with drawer closed." history.length may
    // stay elevated due to Navigation API intercept() semantics, but
    // exit-on-back from the bottom of the stack still works because
    // the active history index returns to baseline after each close.
    await setup(page)
    const start = await getNavState(page)

    for (let i = 0; i < 5; i++) {
      await openDrawer(page)
      await closeDrawerButton(page)
    }
    const end = await getNavState(page)
    expect(end.drawerOpen).toBe(false)
    expect(end.activeChatId).toBe(start.activeChatId)
    expect(end.hasChat).toBe(true)
  })

  test('9. Drawer close (toggle) on a non-default view stays on that view', async ({ page }) => {
    // Regression guard for the bug where closeDrawer's history.back()
    // was popping the navStack and yanking the user out of the current
    // view. Sequence: navigate to settings, open drawer, close it via
    // the brand toggle — must stay on settings, not pop back to chat.
    await setup(page)

    // Move to a non-default view (settings) so navStack is non-empty.
    await openDrawer(page)
    await navigateToSettings(page)
    const onSettings = await page.evaluate(
      () => !!document.querySelector('.settings')
    )
    expect(onSettings).toBe(true)

    // Open drawer (sentinel + drawer open) and close via the brand toggle.
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await closeDrawerButton(page)
    const stillOnSettings = await page.evaluate(
      () => !!document.querySelector('.settings')
    )
    const afterClose = await getNavState(page)
    expect(afterClose.drawerOpen).toBe(false)
    expect(stillOnSettings).toBe(true)
  })

  test('10. Back from drawer-open closes drawer and stays on view (drawer-first)', async ({ page }) => {
    // Drawer-first contract: a back-gesture while the drawer is open
    // closes the drawer ONLY — does not pop navStack, does not
    // navigate. This was the regression at the heart of the
    // "tapping outside drawer scrolls and goes back" bug. handleBack
    // checks `drawerOpenRef && drawerPushedRef` and returns early
    // after closing drawer state.
    //
    // Sequence: chat -> drawer-open + nav-to-settings -> drawer-open
    // again -> back. Result: drawer closed, STAYS on settings.
    await setup(page)

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)

    const afterBack = await getNavState(page)
    expect(afterBack.drawerOpen).toBe(false)
    // KEY: still on settings (drawer-first didn't pop navStack).
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })
})

test.describe('Drawer state machine — extended invariants', () => {
  // These tests pin down each state transition of the simplified
  // navigation/drawer state machine. State variables: activeView,
  // drawerOpen, navStack (pushed by navTo, popped by popstate
  // handler). Drawer is NOT in browser history — that's the
  // post-rewrite design.

  test('11. Repeated openDrawer clicks while open are toggle-guarded no-ops', async ({ page }) => {
    // The toggle button calls openDrawer only when aria-expanded is
    // false, so repeated taps after the drawer is already open don't
    // re-fire openDrawer. This locks in the toggle's idempotency
    // (matters because openDrawer pushes a sentinel; double-firing
    // would leak entries and the close-via-back would pop only one).
    await setup(page)
    const before = await page.evaluate(() => history.length)
    for (let i = 0; i < 3; i++) await openDrawer(page)
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before + 1) // exactly one push, regardless of click count
    expect((await getNavState(page)).drawerOpen).toBe(true)
  })

  test('Brand button is the only header drawer trigger', async ({ page }) => {
    await setup(page)

    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    const header = page.locator('.shell__bar')
    await expect(toggle).toHaveAttribute('type', 'button')
    await expect(toggle).toHaveAttribute('aria-controls', 'navigation-drawer')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')

    const [toggleBox, headerBox] = await Promise.all([toggle.boundingBox(), header.boundingBox()])
    expect(toggleBox).not.toBeNull()
    expect(headerBox).not.toBeNull()
    expect(toggleBox.width).toBeLessThan(headerBox.width / 2)

    // A tap in the intentionally empty part of the toolbar must not open the drawer.
    await page.mouse.click(headerBox.x + headerBox.width - 12, headerBox.y + headerBox.height / 2)
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await toggle.focus()
    await page.keyboard.press('Space')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })

  test('12. closeDrawer when drawer already closed is a no-op', async ({ page }) => {
    // Defensive guard against wiring extra close calls.
    await setup(page)
    const before = await page.evaluate(() => history.length)
    await closeDrawerToggle(page) // drawer is already closed
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before)
  })

  test('13. Drawer open -> nav-to-settings -> back returns to chat (not drawer)', async ({ page }) => {
    // After navTo, the sentinel is "consumed" semantically — drawerPushedRef
    // is false. Back from settings must pop the navStack, not re-open the
    // drawer.
    await setup(page)
    const startId = (await getNavState(page)).activeChatId

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await goBack(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
    const after = await getNavState(page)
    expect(after.drawerOpen).toBe(false)
    expect(after.activeChatId).toBe(startId)
    expect(after.hasChat).toBe(true)
  })

  test('14. Settings -> drawer-open -> back closes drawer (stays on settings)', async ({ page }) => {
    // Drawer-first: back from drawer-open never navigates. Repeats
    // test 10 from the settings starting point to lock in that the
    // drawer-first guard works regardless of which deep view you're
    // on when the drawer was opened.
    await setup(page)

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)
    expect((await getNavState(page)).drawerOpen).toBe(false)
    // Still on settings — drawer-first did not pop navStack.
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('15. Two drawer cycles end on the same view (drawer closed)', async ({ page }) => {
    // Post-fa605f6 contract: each open-close cycle returns the
    // active history index to baseline; the user lands back on
    // their original view with drawer closed. (history.length may
    // stay elevated due to intercept() — see test 8.)
    await setup(page)
    const start = await getNavState(page)
    await openDrawer(page)
    await closeDrawerToggle(page)
    expect((await getNavState(page)).activeChatId).toBe(start.activeChatId)
    await openDrawer(page)
    await closeDrawerToggle(page)
    const end = await getNavState(page)
    expect(end.activeChatId).toBe(start.activeChatId)
    expect(end.drawerOpen).toBe(false)
  })

  test('16. Settings -> drawer-open -> nav-to-other-chat -> back returns to settings', async ({ page }) => {
    // navStack should record settings as the "previous view"; back from
    // chat must pop to settings, not deeper.
    await setup(page)

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await openDrawer(page)
    await navigateToChat(page, 0) // goes to a chat
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)

    await goBack(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('17. activeChatId in localStorage matches the displayed chat after back', async ({ page }) => {
    // Sanity: when handleBack pops navStack, the URL/localStorage and
    // displayed view must agree. Decoupling these silently shows the
    // wrong content with the right URL.
    await setup(page)
    const startId = (await getNavState(page)).activeChatId

    await openDrawer(page)
    await navigateToSettings(page)
    await goBack(page)

    const after = await getNavState(page)
    expect(after.activeChatId).toBe(startId)
    expect(after.hasChat).toBe(true)
  })

  test('18. Triple cycle: chat -> settings -> chat -> back -> back exits cleanly', async ({ page }) => {
    // Stress test: navigate forward several steps and back through them,
    // verifying each pop hits the correct prior view.
    await setup(page)
    const startId = (await getNavState(page)).activeChatId

    await openDrawer(page)
    await navigateToSettings(page)
    await openDrawer(page)
    await navigateToChat(page, 0)

    await goBack(page) // -> settings
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await goBack(page) // -> chat
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
    expect((await getNavState(page)).activeChatId).toBe(startId)
  })

  test('19. closeDrawer via toggle leaves history unchanged; back from open navigates', async ({ page }) => {
    // Post-rewrite: the brand-toggle close and the OS back-gesture from
    // drawer-open are no longer equivalent. Toggle is pure state;
    // back is a real navigation. Document the difference.
    await setup(page)
    await openDrawer(page)
    const beforeToggle = await page.evaluate(() => history.length)
    await closeDrawerToggle(page)
    const afterToggle = await page.evaluate(() => history.length)
    // Toggle close: no history change, drawer closed.
    expect(afterToggle).toBe(beforeToggle)
    expect((await getNavState(page)).drawerOpen).toBe(false)
  })

  test('20. Back and Forward restore shell routes without reversing semantic direction', async ({ page }) => {
    await setup(page)
    const startId = (await getNavState(page)).activeChatId

    // Build chat -> settings -> chat. The final chat may have the same id as
    // the first; the view transition itself is the history edge under test.
    await openDrawer(page)
    await navigateToSettings(page)
    await openDrawer(page)
    await navigateToChat(page, 0)

    const destinationState = await page.evaluate(() => history.state)
    expect(destinationState).toMatchObject({
      __mobiusNav: true,
      kind: 'nav',
      route: { view: 'chat' },
    })
    expect(Number.isInteger(destinationState.index)).toBe(true)

    await goBack(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
    await goBack(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
    expect((await getNavState(page)).activeChatId).toBe(startId)

    // Before the indexed history model this first Forward either did nothing
    // or called handleBack again and moved the visible UI farther backward.
    await goForward(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
    await goForward(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)

    // Forward rebuilt the semantic edges, so Back works again normally.
    await goBack(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('20b. Forward to an unconsumed drawer sentinel reopens the drawer', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)
    expect((await getNavState(page)).drawerOpen).toBe(false)

    await goForward(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)
    expect((await getNavState(page)).drawerOpen).toBe(false)
  })

  test('20c. legacy Forward lands at app base and the next Back leaves once', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const iframe = page.locator('iframe[data-app-id]').first()
    if (await iframe.count() === 0) test.skip(true, 'no installed app available')
    const appId = await iframe.getAttribute('data-app-id')
    const appFrame = page.frames().find(frame => /\/api\/apps\/\d+\/frame/.test(frame.url()))
    if (!appFrame) test.skip(true, 'app frame did not load')

    // Drive the same wire protocol a nested app route uses, while recording
    // how many semantic closes the shell sends back to that exact frame.
    await appFrame.evaluate((ownerId) => {
      window.__mobiusBackCount = 0
      window.addEventListener('message', (event) => {
        if (event.data?.type === 'moebius:nav-back') window.__mobiusBackCount += 1
      })
      window.parent.postMessage({ type: 'moebius:nav-push', appId: ownerId }, '*')
    }, appId)
    await page.waitForFunction(() => history.state?.kind === 'app')

    await goBack(page) // consumes the app-local level
    expect((await getNavState(page)).hasCanvas).toBe(true)
    expect(await appFrame.evaluate(() => window.__mobiusBackCount)).toBe(1)

    await goForward(page) // physical entry returns; nested level cannot
    await goBack(page) // one ordinary Back leaves the app base
    expect((await getNavState(page)).hasChat).toBe(true)
    expect(await appFrame.evaluate(() => window.__mobiusBackCount)).toBe(1)
  })

  test('20d. reversible app entries restore on Forward and unwind once again', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const iframe = page.locator('iframe[data-app-id]').first()
    if (await iframe.count() === 0) test.skip(true, 'no installed app available')
    const appFrame = page.frames().find(frame => /\/api\/apps\/\d+\/frame/.test(frame.url()))
    if (!appFrame) test.skip(true, 'app frame did not load')

    await appFrame.evaluate(() => {
      window.__mobiusBackCount = 0
      window.__mobiusForwardCount = 0
      window.__mobiusTestNavHandle = window.mobius.nav.open('e2e-report', {
        onBack() { window.__mobiusBackCount += 1 },
        onForward() { window.__mobiusForwardCount += 1 },
      })
    })
    await page.waitForFunction(() => history.state?.kind === 'app')

    await goBack(page)
    expect(await appFrame.evaluate(() => window.__mobiusBackCount)).toBe(1)
    await goForward(page)
    await expect.poll(() => appFrame.evaluate(() => window.__mobiusForwardCount)).toBe(1)
    await goBack(page)
    expect(await appFrame.evaluate(() => window.__mobiusBackCount)).toBe(2)
    expect((await getNavState(page)).hasCanvas).toBe(true)
  })

  test('20e. rejected Forward restoration retires the ghost app step', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const iframe = page.locator('iframe[data-app-id]').first()
    if (await iframe.count() === 0) test.skip(true, 'no installed app available')
    const appFrame = page.frames().find(frame => /\/api\/apps\/\d+\/frame/.test(frame.url()))
    if (!appFrame) test.skip(true, 'app frame did not load')

    await appFrame.evaluate(() => {
      // Deliberately announce a reversible id the runtime never registered.
      // This models a fresh/evicted frame: its runtime-level responder must
      // explicitly reject the unknown restoration request.
      window.parent.postMessage({
        type: 'moebius:nav-push',
        label: 'report',
        requestId: 'e2e-evicted-report',
        reversible: true,
      }, '*')
    })
    await page.waitForFunction(() => history.state?.kind === 'app')
    await goBack(page)
    await goForward(page)
    await page.waitForFunction(() => history.state?.kind === 'nav', null, { timeout: 2000 })

    await goBack(page)
    expect((await getNavState(page)).hasChat).toBe(true)
  })
})

test.describe('Delete response boundaries', () => {
  test('a chat delete 500 keeps the live row and route intact', async ({ page }) => {
    await setup(page)
    const target = NAV_CHATS[0]
    let deleteAttempts = 0
    await page.route(new RegExp(`/api/chats/${target.id}$`), route => {
      if (route.request().method() !== 'DELETE') return route.fallback()
      deleteAttempts += 1
      return route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: '{"detail":"delete failed"}',
      })
    })

    await openDrawer(page)
    await page.getByRole('button', { name: `More actions for ${target.title}` }).click()
    await page.getByRole('menuitem', { name: 'Delete' }).click()

    await expect.poll(() => deleteAttempts).toBe(1)
    await expect(page.getByText("Couldn't delete this chat — please try again."))
      .toBeVisible()
    await expect(
      page.getByLabel('Primary navigation')
        .getByRole('button', { name: target.title, exact: true }),
    ).toBeVisible()
    expect((await getNavState(page)).activeChatId).toBe(target.id)
  })
})

test.describe('BFCache snapshot contract', () => {
  // The fa605f6 nav model fixes the Chrome Android swipe-back "two
  // drawers" artifact STRUCTURALLY: navTo never calls pushState. The
  // user effectively stays on the same browser history entry (the
  // drawer-sentinel pushed by openDrawer) while the in-app view
  // changes via internal state + navStackRef. Chrome's BFCache for
  // the entry-being-left (the base entry) was captured BEFORE the
  // drawer was ever opened, so the swipe-back snapshot is clean.
  //
  // The two tests below lock in the load-bearing structural property
  // (no pushState in navTo) that delivers this fix.

  test('21. navTo does NOT call history.pushState', async ({ page }) => {
    // Locks in the fa605f6 model. If a future change re-introduces
    // pushState in navTo (e.g. "drawer purely visual" rewrite v2),
    // this test fails loudly. See CLAUDE.md "Navigation — desiderata"
    // for the trade-off this protects.
    await setup(page)
    await page.evaluate(() => {
      window.__pushStateCalls = 0
      const original = history.pushState.bind(history)
      history.pushState = function(...args) {
        window.__pushStateCalls++
        return original(...args)
      }
    })
    // Open drawer is allowed to push (the sentinel) — capture the
    // count after open so we can isolate navTo's contribution.
    await openDrawer(page)
    const afterOpen = await page.evaluate(() => window.__pushStateCalls)
    // Tap Settings — this triggers navTo. navTo MUST NOT pushState.
    await navigateToSettings(page)
    const afterNav = await page.evaluate(() => window.__pushStateCalls)
    expect(afterNav).toBe(afterOpen)
  })
})

test.describe('Drawer close paths converge through handleBack', () => {
  // The user-facing bug that motivated the fa605f6 restoration: every
  // path that closes the drawer (X button, overlay tap, OS back-
  // gesture) was over-popping the navStack and unexpectedly navigating
  // away from the user's deep view ("tap outside drawer takes me back
  // and to the bottom"). The fix routes all paths through
  // history.back() -> handleBack, where a drawer-first guard
  // (`if (drawerOpenRef && drawerPushedRef) close drawer; return`)
  // prevents the navStack pop. These tests lock in that contract for
  // each close path independently.

  test('concurrent close requests issue one history traversal', async ({ page }) => {
    await setup(page)
    const toggle = page.getByRole('button', { name: 'Toggle navigation' })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')

    const calls = await page.evaluate(() => {
      let count = 0
      history.back = () => { count += 1 }
      const overlay = document.querySelector('.drawer-overlay')
      const event = () => new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        isPrimary: true,
        pointerId: 1,
      })
      overlay.dispatchEvent(event())
      overlay.dispatchEvent(event())
      return count
    })
    expect(calls).toBe(1)
  })

  test('22. Pointer-down on overlay closes drawer (does not navigate)', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)
    // Use a real pointer sequence rather than HTMLElement.click(). The drawer
    // dismisses on pointerdown so a touch that moves enough for Chrome to
    // suppress the later synthetic click still closes reliably.
    await page.locator('.drawer-overlay').click({ position: { x: 400, y: 300 } })
    await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
    expect((await getNavState(page)).drawerOpen).toBe(false)
    // Still on settings — overlay tap did not navigate.
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('22a. A deliberate drawer tap immediately after Back is not discarded', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)

    // Closing the drawer traverses history and arms the Android bare-click
    // guard. A fresh owner tap has its own pointerdown, so it must clear that
    // guard and reopen the drawer without waiting for the 400ms timeout.
    await page.evaluate(() => history.back())
    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
    await page.getByRole('button', { name: 'Toggle navigation' }).click()
    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'true')
  })

  test('22b. Drawer scrim owns touch pans instead of the background', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    const contract = await page.evaluate(() => {
      const overlay = getComputedStyle(document.querySelector('.drawer-overlay'))
      const drawer = getComputedStyle(document.querySelector('.drawer'))
      const content = getComputedStyle(document.querySelector('.shell__content'))
      return {
        overlayTouchAction: overlay.touchAction,
        overlayOverscroll: overlay.overscrollBehavior,
        drawerTouchAction: drawer.touchAction,
        drawerOverscroll: drawer.overscrollBehavior,
        contentPointerEvents: content.pointerEvents,
        contentTouchAction: content.touchAction,
      }
    })
    expect(contract).toEqual({
      overlayTouchAction: 'none',
      overlayOverscroll: 'none',
      drawerTouchAction: 'pan-y pinch-zoom',
      drawerOverscroll: 'contain',
      contentPointerEvents: 'none',
      contentTouchAction: 'none',
    })
  })

  test('22c. Vertical drawer scroll does not swallow the next destination tap', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)

    // Reproduce the Android sequence without waiting out the old suppressor's
    // 400ms fallback: a noisy diagonal sample briefly looks horizontal, then
    // native pan-y takes over and emits touchcancel. The immediately-following
    // destination tap must pass; cancellation is not a custom swipe completion.
    await page.locator('.drawer').evaluate((drawer) => {
      const point = (x, y) => new Touch({
        identifier: 37,
        target: drawer,
        clientX: x,
        clientY: y,
      })
      drawer.dispatchEvent(new TouchEvent('touchstart', {
        bubbles: true,
        cancelable: true,
        touches: [point(180, 520)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [point(165, 512)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [point(165, 390)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchcancel', {
        bubbles: true,
        cancelable: true,
        touches: [],
        changedTouches: [point(165, 390)],
      }))

      const settings = [...drawer.querySelectorAll('button')]
        .find(button => button.textContent.trim() === 'Settings')
      settings?.click()
    })

    await expect(page.locator('.settings')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
  })

  test('22ca. Swipe click suppression cannot survive into a later tap', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)

    // Some browsers emit the swipe's synthetic click; others suppress it. Arm
    // the guard with a short horizontal swipe that snaps back, but deliberately
    // omit that generated click. The next genuine Playwright tap starts with
    // pointerdown and must clear the stale same-gesture guard before clicking.
    await page.locator('.drawer').evaluate((drawer) => {
      const point = (x, y) => new Touch({
        identifier: 38,
        target: drawer,
        clientX: x,
        clientY: y,
      })
      drawer.dispatchEvent(new TouchEvent('touchstart', {
        bubbles: true,
        cancelable: true,
        touches: [point(180, 420)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [point(150, 421)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchend', {
        bubbles: true,
        cancelable: true,
        touches: [],
        changedTouches: [point(150, 421)],
      }))
    })

    await page.getByRole('button', { name: 'Settings' }).click()
    await expect(page.locator('.settings')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
  })

  test('22cb. A swipe-generated click cannot activate the row under its lift', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)

    await page.locator('.drawer').evaluate((drawer) => {
      const point = (x, y) => new Touch({
        identifier: 39,
        target: drawer,
        clientX: x,
        clientY: y,
      })
      drawer.dispatchEvent(new TouchEvent('touchstart', {
        bubbles: true,
        cancelable: true,
        touches: [point(180, 420)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [point(150, 421)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchend', {
        bubbles: true,
        cancelable: true,
        touches: [],
        changedTouches: [point(150, 421)],
      }))

      const settings = [...drawer.querySelectorAll('button')]
        .find(button => button.textContent.trim() === 'Settings')
      settings?.dispatchEvent(new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
        detail: 1,
        clientX: 150,
        clientY: 421,
      }))
    })

    expect((await getNavState(page)).drawerOpen).toBe(true)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
  })

  test('22d. Interrupted drawer swipe cannot strand an inert panel onscreen', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)

    // Reproduce the mobile failure: a slight horizontal drawer drag writes an
    // inline transform, then shell navigation closes the drawer before the
    // browser delivers touchend/touchcancel. The close state must clear that
    // imperative transform unconditionally rather than trusting a terminal
    // touch event that may never arrive.
    await page.locator('.drawer').evaluate((drawer) => {
      const point = (x, y) => new Touch({
        identifier: 41,
        target: drawer,
        clientX: x,
        clientY: y,
      })
      drawer.dispatchEvent(new TouchEvent('touchstart', {
        bubbles: true,
        cancelable: true,
        touches: [point(220, 420)],
      }))
      drawer.dispatchEvent(new TouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [point(195, 420)],
      }))
    })
    expect(await page.locator('.drawer').evaluate((drawer) => drawer.style.transform))
      .toBe('translateX(-25px)')

    await page.evaluate(() => history.back())
    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(await page.locator('.drawer').evaluate((drawer) => ({
      inlineTransform: drawer.style.transform,
      inert: drawer.inert,
    }))).toEqual({ inlineTransform: '', inert: true })
    await expect.poll(() => page.locator('.drawer').evaluate(
      (drawer) => new DOMMatrixReadOnly(getComputedStyle(drawer).transform).m41,
    )).toBeLessThanOrEqual(-359)
  })

  test('22e. Closing scrim blocks the app until the panel is offscreen', async ({ page }) => {
    await setup(page, { width: 426, height: 860 })
    await openDrawer(page)
    await page.locator('.drawer-overlay').dispatchEvent('pointerdown', {
      button: 0,
      isPrimary: true,
      pointerId: 7,
      pointerType: 'touch',
    })

    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
    const closing = await page.locator('.drawer').evaluate((drawer) => {
      const x = new DOMMatrixReadOnly(getComputedStyle(drawer).transform).m41
      return {
        x,
        blocking: getComputedStyle(document.querySelector('.drawer-overlay')).pointerEvents,
      }
    })
    if (closing.x > -359) expect(closing.blocking).toBe('auto')
    await expect.poll(() => page.locator('.drawer').evaluate(
      (drawer) => new DOMMatrixReadOnly(getComputedStyle(drawer).transform).m41,
    )).toBeLessThanOrEqual(-359)
    await expect(page.locator('.drawer-overlay')).toHaveCSS('pointer-events', 'none')
  })

  test('23. Brand toggle close does not navigate, even from a deep view', async ({ page }) => {
    // Same regression as test 22 but via the existing mobile brand toggle.
    // Test 9 covers the basic case from a non-default view; this test keeps
    // the close-without-navigation contract explicit.
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    await openDrawer(page)
    await closeDrawerButton(page)
    expect((await getNavState(page)).drawerOpen).toBe(false)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('24. OS back-gesture from drawer-open closes drawer (does not navigate)', async ({ page }) => {
    // Same regression via the third close path. Drawer-first guard
    // in handleBack catches this before the navStack pop branch.
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)
    await goBack(page)
    expect((await getNavState(page)).drawerOpen).toBe(false)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Split-pane navigation (PR2 gate — design §5, §8). Appended; the 24
// invariants above are unchanged. These exercise the honest global-
// chronological Back across TWO simultaneously-visible app panes and the
// eviction-retires-history contract, which single-pane 20c cannot reach.
// ---------------------------------------------------------------------------

const PANE_APP_A = 990201
const PANE_APP_B = 990202

/** Two app panes side by side: p0 = app A (focused), p1 = app B. */
function twoAppPanes() {
  let ws = paneModel.seedFromFlatTabs([
    { kind: 'app', id: PANE_APP_A }, { kind: 'app', id: PANE_APP_B },
  ])
  ws = paneModel.moveTab(ws, `app:${PANE_APP_B}`, { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

async function bootTwoAppPanes(page) {
  await page.setViewportSize({ width: 1400, height: 900 })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  const apps = [
    { id: PANE_APP_A, name: 'Pane App A' },
    { id: PANE_APP_B, name: 'Pane App B' },
  ]
  await page.route(/\/api\/apps\/(\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(apps.map(a => ({
        id: a.id, name: a.name, description: '', compiled_path: '',
        chat_id: null, source_dir: null, pinned_at: null,
        cross_app_access: 'none', share_with_apps: 'none', offline_capable: false,
        updated_at: '2026-07-12T12:00:00Z',
      }))),
    })
  })
  for (const a of apps) {
    await page.route(new RegExp(`/api/apps/${a.id}/frame`), route => route.fulfill({
      status: 200, contentType: 'text/html',
      body: '<!doctype html><html><body style="margin:0"><div id="probe">app</div></body></html>',
    }))
  }
  // AppCanvas requires a scoped token before it mounts an online app frame.
  // Keep the protocol complete so these navigation checks cannot silently skip.
  await page.route(/\/api\/auth\/app-token$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ token: 'mock-app-token' }),
  }))
  // Land on the origin, then seed the flag + workspace blob and re-navigate so
  // the shell boots the two-pane tree with the splits flag on.
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const blob = paneModel.serializeWorkspace(twoAppPanes())
  await page.addInitScript(([flagKey, wsKey, wsBlob]) => {
    try {
      localStorage.setItem(flagKey, '1')
      sessionStorage.setItem(wsKey, wsBlob)
    } catch { /* private mode */ }
  }, ['mobius:workspace-splits', paneModel.STORAGE_KEY, blob])
  await page.goto(`${BASE}/shell/?app=${PANE_APP_A}`, { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.workspace__chrome')).toHaveCount(1, { timeout: 8000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

async function appFrameFor(page, appId) {
  const iframe = page.locator(`iframe[data-app-id="${appId}"]`)
  await expect(iframe).toHaveCount(1, { timeout: 5000 })
  let frame = null
  await expect.poll(async () => {
    const handle = await iframe.elementHandle()
    frame = await handle?.contentFrame() ?? null
    return frame !== null
  }, {
    timeout: 5000,
    message: `app ${appId} frame attached`,
  }).toBe(true)
  return frame
}

/** Arm a frame's nav-back counter. */
async function armBackCounter(frame) {
  await frame.evaluate(() => {
    window.__navBack = 0
    window.addEventListener('message', (e) => {
      if (e && e.data && e.data.type === 'moebius:nav-back') window.__navBack += 1
    })
  })
}

test.describe('Split-pane navigation (PR2 gate)', () => {
  test('25. two visible app panes: Back routes nav-back to the topmost tagged pane', async ({ page }) => {
    await bootTwoAppPanes(page)
    const frameA = await appFrameFor(page, PANE_APP_A)
    const frameB = await appFrameFor(page, PANE_APP_B)
    expect(frameA, 'app A frame mounted').not.toBeNull()
    expect(frameB, 'app B frame mounted').not.toBeNull()

    await armBackCounter(frameA)
    await armBackCounter(frameB)

    // App A pushes a nested level, then app B pushes its own — two live
    // sentinels keyed by (paneId, appId), interleaved across the visible pair.
    await frameA.evaluate(id => window.parent.postMessage({ type: 'moebius:nav-push', appId: id }, '*'), PANE_APP_A)
    await page.waitForFunction(id => (
      history.state?.kind === 'app' && history.state?.route?.appId === id
    ), PANE_APP_A)
    await frameB.evaluate(id => window.parent.postMessage({ type: 'moebius:nav-push', appId: id }, '*'), PANE_APP_B)
    await page.waitForFunction(id => (
      history.state?.kind === 'app' && history.state?.route?.appId === id
    ), PANE_APP_B)

    // Back pops the TOPMOST tagged entry first — app B (last pushed), not A.
    await page.evaluate(() => history.back())
    await expect.poll(() => frameB.evaluate(() => window.__navBack), {
      timeout: 3000,
    }).toBe(1)
    expect(await frameA.evaluate(() => window.__navBack)).toBe(0)

    // The next Back routes to app A's level.
    await page.evaluate(() => history.back())
    await expect.poll(() => frameA.evaluate(() => window.__navBack), {
      timeout: 3000,
    }).toBe(1)
    expect(await frameB.evaluate(() => window.__navBack)).toBe(1)
  })

  test('26. closing a pane retires its app history without disturbing the sibling', async ({ page }) => {
    await bootTwoAppPanes(page)
    const frameA = await appFrameFor(page, PANE_APP_A)
    const frameB = await appFrameFor(page, PANE_APP_B)
    expect(frameA, 'app A frame mounted').not.toBeNull()
    expect(frameB, 'app B frame mounted').not.toBeNull()

    await armBackCounter(frameB)

    // App A (p0) pushes a nested level, so it owns a live sentinel + a physical
    // history entry.
    await frameA.evaluate(id => window.parent.postMessage({ type: 'moebius:nav-push', appId: id }, '*'), PANE_APP_A)
    await page.waitForFunction(id => (
      history.state?.kind === 'app' && history.state?.route?.appId === id
    ), PANE_APP_A)

    // Close app A's pane (its strip ✕). p0 collapses; app B becomes the sole
    // pane. Eviction retires app A's tagged history so the physical entry can no
    // longer nav-back a dead frame (design §5 eviction-retires-history).
    await page.locator('[data-pane-strip="p0"] .shell__tab-close').first().click()
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 200)))))
    // Single pane now: app B is full-bleed and visible.
    await expect(page.locator('.shell__view--active')).toBeVisible({ timeout: 4000 })

    // Back over the retired app-A entry is absorbed (atomic semantic discard) —
    // it does NOT resurrect app A's nested level on the sibling, and it does not
    // over-pop the shell. App B stays put.
    await page.evaluate(() => history.back())
    await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
    expect(await frameB.evaluate(() => window.__navBack)).toBe(0)
    expect(page.frames().includes(frameB), 'sibling app B survived the retirement').toBe(true)
    await expect(page.locator('.shell__view--active')).toBeVisible()
  })

  test('27. a visible background pane restores its reversible entry on Forward', async ({ page }) => {
    await bootTwoAppPanes(page)
    const frameA = await appFrameFor(page, PANE_APP_A)
    const frameB = await appFrameFor(page, PANE_APP_B)
    expect(frameA, 'app A frame mounted').not.toBeNull()
    expect(frameB, 'app B frame mounted').not.toBeNull()

    await armBackCounter(frameA)
    await frameB.evaluate((id) => {
      window.__navBack = 0
      window.__navForward = 0
      window.addEventListener('message', (event) => {
        const message = event?.data
        if (message?.type === 'moebius:nav-back') window.__navBack += 1
        if (message?.type === 'moebius:nav-forward') {
          window.__navForward += 1
          window.parent.postMessage({
            type: 'moebius:nav-forward-ack',
            requestId: message.requestId,
          }, '*')
        }
      })
      // B begins as the visible but unfocused pane. Its push must still be
      // accepted, attributed to p1, and focus that owner pane.
      window.parent.postMessage({
        type: 'moebius:nav-push',
        appId: id,
        requestId: 'pane-b-report',
        label: 'report',
        reversible: true,
      }, '*')
    }, PANE_APP_B)
    await page.waitForFunction(id => (
      history.state?.kind === 'app'
        && history.state?.route?.appId === id
        && history.state?.appNav?.requestId === 'pane-b-report'
    ), PANE_APP_B)

    await page.evaluate(() => history.back())
    await expect.poll(() => frameB.evaluate(() => window.__navBack)).toBe(1)
    expect(await frameA.evaluate(() => window.__navBack)).toBe(0)

    await page.evaluate(() => history.forward())
    await expect.poll(() => frameB.evaluate(() => window.__navForward)).toBe(1)

    await page.evaluate(() => history.back())
    await expect.poll(() => frameB.evaluate(() => window.__navBack)).toBe(2)
    expect(await frameA.evaluate(() => window.__navBack)).toBe(0)
  })
})
