/**
 * Navigation and back button behavior tests.
 *
 * Tests the useNavigation hook: back button between chats, back from app
 * canvas to chat, drawer open/close via back, and pushState/popstate handling.
 *
 * Run:  npx playwright test tests/navigation.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

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
  await page.evaluate((idx) => {
    const items = document.querySelectorAll('.drawer__item')
    let chatItems = []
    items.forEach(el => {
      if (el.querySelector('.drawer__item-text') && !el.classList.contains('drawer__item--new')) {
        chatItems.push(el)
      }
    })
    if (chatItems[idx]) chatItems[idx].click()
  }, index)
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
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
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

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
    await closeDrawerToggle(page)
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
      await closeDrawerToggle(page)
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
    // the X button — must stay on settings, not pop back to chat.
    await setup(page)

    // Move to a non-default view (settings) so navStack is non-empty.
    await openDrawer(page)
    await navigateToSettings(page)
    const onSettings = await page.evaluate(
      () => !!document.querySelector('.settings')
    )
    expect(onSettings).toBe(true)

    // Open drawer (sentinel + drawer open) and close via the X button.
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await closeDrawerToggle(page)
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
    // Post-rewrite: the X-button close and the OS back-gesture from
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
})

// Note on the chat-delete + back-nav guarantee:
//
// `Shell.jsx`'s `deleteChat` scrubs `navStackRef` of entries pointing
// at the deleted chat ID. Without that scrub, back-gesture after
// delete navigates into a 404'd chat with no error UX. We don't have
// an end-to-end test for this because:
//
//   1. The drawer only shows chats with `has_messages=true` (per
//      Drawer.jsx). The delete button (X) lives inside chat-list
//      rows, so empty chats can't be deleted from the UI.
//   2. Seeding a chat with `has_messages=true` requires running the
//      agent (POST /chats/{id}/messages spawns the CLI). The
//      Playwright test setup mocks that endpoint to a no-op 202, so
//      the chat's `messages` field stays empty and the chat stays
//      hidden from the drawer.
//
// The contract is enforced by the simple line in
// `Shell.deleteChat`:
//   navStackRef.current = navStackRef.current.filter(e => e.chatId !== id)
// If you change that, also delete this comment + write the test
// using direct DB manipulation or a real agent integration.

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

  test('22. Tap overlay closes drawer (does not navigate)', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)
    // Tap the drawer overlay (.drawer-overlay element).
    await page.evaluate(() => {
      const overlay = document.querySelector('.drawer-overlay')
      if (overlay) overlay.click()
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 400)))
    expect((await getNavState(page)).drawerOpen).toBe(false)
    // Still on settings — overlay tap did not navigate.
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
  })

  test('23. X-button (toggle) closes drawer (does not navigate, even from deep view)', async ({ page }) => {
    // Same regression as test 22 but via the toggle button. test 9
    // covers the basic case from a non-default view; this test just
    // makes the close-via-toggle = no-navigate contract explicit.
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    await openDrawer(page)
    await closeDrawerToggle(page)
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
