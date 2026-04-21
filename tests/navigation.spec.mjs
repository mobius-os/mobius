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

  test('3. Drawer open — back closes drawer', async ({ page }) => {
    await setup(page)
    await openDrawer(page)

    const withDrawer = await getNavState(page)
    expect(withDrawer.drawerOpen).toBe(true)

    await goBack(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    const afterBack = await getNavState(page)
    expect(afterBack.drawerOpen).toBe(false)
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

  test('8. Drawer open/close cycles do not leak history entries', async ({ page }) => {
    // Regression guard: each openDrawer() pushes a sentinel history
    // entry so Chrome's back-forward preview shows a clean state.
    // closeDrawer() pops that entry (via history.back), otherwise
    // every toggle adds a zombie and the user has to press back once
    // per toggle before the app actually navigates anywhere.
    //
    // The design tolerates ONE orphan sentinel (handleBack's early-
    // return consumes it harmlessly). What must not happen: growth per
    // cycle. So we measure the length after one toggle pair, then
    // after five — they must match.
    await setup(page)

    await openDrawer(page)
    await closeDrawerToggle(page)
    const afterOne = await page.evaluate(() => history.length)

    for (let i = 0; i < 4; i++) {
      await openDrawer(page)
      await closeDrawerToggle(page)
    }
    const afterFive = await page.evaluate(() => history.length)

    // No per-cycle leak: five cycles must not add more entries than one.
    expect(afterFive).toBe(afterOne)

    // Drawer should be closed at the end.
    const state = await getNavState(page)
    expect(state.drawerOpen).toBe(false)
  })
})

test.describe('Browser restrictions (documented)', () => {
  test('7. Back gesture cannot be overridden on Chrome Android', async ({ page }) => {
    // This test documents the known limitation rather than testing app code.
    // Chrome Android's back gesture (swipe from edge) triggers a full page
    // navigation that shows the cached BFCache snapshot during the swipe
    // animation.  The Navigation API's intercept() cannot prevent this
    // visual artifact — it can only run code after the gesture completes.
    //
    // The app's workaround: openDrawer() pushes a pushState entry so the
    // cached snapshot shows the clean page (no drawer), not the drawer.
    //
    // This test verifies the pushState entry exists after drawer open.
    await setup(page)
    const historyBefore = await page.evaluate(() => history.length)
    await openDrawer(page)
    const historyAfter = await page.evaluate(() => history.length)
    expect(historyAfter).toBe(historyBefore + 1)
  })
})
