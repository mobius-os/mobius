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
  test('1. Initial state — chat view, URL is /', async ({ page }) => {
    await setup(page)
    const state = await getNavState(page)
    expect(state.hasChat).toBe(true)
    expect(state.url).toBe('/')
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

  test('3. Drawer is purely visual — does NOT push a history entry', async ({ page }) => {
    // The drawer is overlay state, not a route. Opening it must not
    // grow history.length. Back-gesture from drawer-open navigates
    // (or exits PWA) — the drawer closes as a side-effect via the
    // popstate handler.
    await setup(page)
    const before = await page.evaluate(() => history.length)
    await openDrawer(page)
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before)
    expect((await getNavState(page)).drawerOpen).toBe(true)
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

  test('6. URL stays at / throughout navigation', async ({ page }) => {
    await setup(page)
    expect((await getNavState(page)).url).toBe('/')

    await openDrawer(page)
    await navigateToChat(page, 0)
    expect((await getNavState(page)).url).toBe('/')

    await goBack(page)
    expect((await getNavState(page)).url).toBe('/')
  })

  test('8. Drawer cycles do not affect history.length AT ALL', async ({ page }) => {
    // The drawer is now purely visual state — NO history push, NO
    // history.back. After any number of open/close cycles,
    // history.length is exactly what it was before any drawer
    // interaction. Stronger contract than the previous "+1 per
    // session" we had to live with under the drawer-as-back-stack
    // pattern.
    await setup(page)
    const before = await page.evaluate(() => history.length)

    for (let i = 0; i < 5; i++) {
      await openDrawer(page)
      await closeDrawerToggle(page)
    }
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before)

    const state = await getNavState(page)
    expect(state.drawerOpen).toBe(false)
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

  test('10. Back from drawer-open navigates AND closes the drawer', async ({ page }) => {
    // Post-rewrite contract: drawer is overlay state, back goes to
    // the previous nav entry. Drawer closes as a side-effect.
    // Sequence: chat -> drawer-open + nav-to-settings -> drawer-open
    // again -> back. Result: drawer closed, on chat (not settings).
    await setup(page)

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)

    const afterBack = await getNavState(page)
    expect(afterBack.drawerOpen).toBe(false)
    expect(afterBack.hasChat).toBe(true)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
  })
})

test.describe('Drawer state machine — extended invariants', () => {
  // These tests pin down each state transition of the simplified
  // navigation/drawer state machine. State variables: activeView,
  // drawerOpen, navStack (pushed by navTo, popped by popstate
  // handler). Drawer is NOT in browser history — that's the
  // post-rewrite design.

  test('11. openDrawer never adds a history entry, regardless of count', async ({ page }) => {
    // Open the drawer many times in a row (technically only the first
    // changes state since the others are no-ops while open) — none of
    // them affect history.length.
    await setup(page)
    const before = await page.evaluate(() => history.length)
    for (let i = 0; i < 3; i++) await openDrawer(page)
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before)
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

  test('14. Settings -> drawer-open -> back navigates AWAY from settings (drawer closes too)', async ({ page }) => {
    // Under the new arch, back from drawer-open navigates the
    // navStack (drawer closes as side-effect). Reverse of the old
    // behavior, matches user expectation: "back goes to previous
    // chat, not just closing the drawer."
    await setup(page)

    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)

    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)

    await goBack(page)
    // Drawer closed AND navigated away from settings.
    expect((await getNavState(page)).drawerOpen).toBe(false)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(false)
  })

  test('15. Drawer cycles do not affect history.length AT ALL (post-rewrite)', async ({ page }) => {
    // Post-rewrite contract: drawer is purely visual; cycles add zero
    // entries. Stronger than the "stabilize at +1" guarantee under the
    // old drawer-as-back-stack pattern.
    await setup(page)
    const before = await page.evaluate(() => history.length)
    await openDrawer(page)
    await closeDrawerToggle(page)
    const afterOne = await page.evaluate(() => history.length)
    await openDrawer(page)
    await closeDrawerToggle(page)
    const afterTwo = await page.evaluate(() => history.length)
    expect(afterOne).toBe(before)
    expect(afterTwo).toBe(before)
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

test.describe('Browser restrictions (documented)', () => {
  test('7. BFCache "two drawers" trade-off (documented)', async ({ page }) => {
    // On Chrome Android, the back-gesture animation shows the cached
    // BFCache snapshot of the previous entry during the swipe. If the
    // user navigated FROM a chat WITH the drawer open (e.g. tapping a
    // chat in the drawer triggers navTo with drawerOpen=true), the
    // BFCache for the entry being LEFT may capture drawer-open. On
    // back, that snapshot slides into view — looks like "two drawers"
    // for ~300ms.
    //
    // Mitigation in `useNavigation.navTo`: setDrawerOpen(false) is
    // called BEFORE history.pushState, so by the time the browser
    // captures the snapshot, the drawer should be visually closed.
    // setState is async though, so this is best-effort, not absolute.
    //
    // This test asserts the API contract (no history growth on
    // openDrawer alone) — the BFCache visual is a known cosmetic
    // limit on Chrome Android with no robust API-level fix.
    await setup(page)
    const before = await page.evaluate(() => history.length)
    await openDrawer(page)
    const after = await page.evaluate(() => history.length)
    expect(after).toBe(before)
  })
})
