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
import { createMockChatRuntime } from './_mockChatRuntime.mjs'

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
}))

const NAV_APP = {
  id: 990203,
  name: 'Navigation Fixture',
}

function navChatDetail(id, assistantContent = 'Fixture response') {
  return {
    messages: [
      { role: 'user', content: `Open ${id}`, ts: 1700000000000, blocks: [] },
      { role: 'assistant', content: assistantContent, ts: 1700000000001, blocks: [] },
    ],
    total: 2,
    offset: 0,
    running: false,
    pending_messages: [],
  }
}

function emptyChatDetail() {
  return {
    messages: [],
    total: 0,
    offset: 0,
    running: false,
    pending_messages: [],
    pending_question_id: null,
    session_id: null,
    provider: 'codex',
    created_by_app_id: null,
    agent_settings_json: { model: 'gpt-5.6-sol' },
    effective_agent_settings: { model: 'gpt-5.6-sol', effort: 'medium' },
    has_assistant_turns: false,
    auto_resume_on_limit: false,
    auto_resume_on_restart: true,
    updated_at: '2026-01-01T00:02:00Z',
  }
}

function createdChat(id, timestamp = '2026-01-01T00:02:00Z') {
  return {
    id,
    title: 'New chat',
    created_at: timestamp,
    updated_at: timestamp,
    activity_at: timestamp,
    pinned_at: null,
    created_by_app_id: null,
    has_messages: false,
    running: false,
    messages: [],
    detail: emptyChatDetail(),
  }
}

async function seedDurableNewChatDraft(page, { chatId, input, status = 'failed' }) {
  // Seed the independent store to completion on this origin before the app
  // boots. The later NewChatLanding read must not race the fixture's put.
  await page.goto(`${BASE}/manifest.webmanifest`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(async ({ id, draftInput, intentStatus }) => {
    sessionStorage.setItem('new-chat-intent', JSON.stringify({
      chatId: id,
      status: intentStatus,
    }))
    // Deliberately omit sessionStorage draft:<id>. This models a quota or
    // privacy-mode write failure where the independent durable write won.
    const raw = JSON.stringify({
      type: 'mobius-composer-draft',
      version: 2,
      updated_at: Date.now(),
      input: draftInput,
      attachments: [],
    })
    await new Promise((resolve, reject) => {
      const request = indexedDB.open('mobius-owner-drafts', 1)
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains('drafts-v1')) {
          request.result.createObjectStore('drafts-v1')
        }
      }
      request.onerror = () => reject(request.error)
      request.onsuccess = () => {
        const transaction = request.result.transaction('drafts-v1', 'readwrite')
        transaction.objectStore('drafts-v1').put(raw, id)
        transaction.oncomplete = resolve
        transaction.onerror = () => reject(transaction.error)
        transaction.onabort = () => reject(transaction.error)
      }
    })
  }, { id: chatId, draftInput: input, intentStatus: status })
}

/** Click the Settings entry in the drawer; assumes drawer is open. */
async function navigateToSettings(page) {
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await navigation.getByRole('button', { name: 'Settings', exact: true }).click()
  await expect(page.locator('.settings')).toBeVisible()
}

// New Chat owns the canonical composer before and after row allocation.
function newChatSurface(page, chatId = null) {
  return page.locator(chatId
    ? `[data-chat-surface="painted"][data-chat-id="${chatId}"]`
    : '[data-chat-surface="painted"]')
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function setup(
  page,
  viewport = { width: 412, height: 915 },
  {
    assistantContent = 'Fixture response',
    chatDetailGate = null,
    chats = NAV_CHATS,
    detailForChat = null,
    runtimeFixture = createMockChatRuntime(),
    chatListResponder = null,
    chatPatchResponder = null,
  } = {},
) {
  await page.setViewportSize(viewport)

  await page.route('**/api/auth/providers/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    json: {
      claude: { name: 'Claude Code', configured: false, authenticated: false },
      codex: { name: 'Codex', configured: true, authenticated: true, error: null },
    },
  }))

  // Navigation is a client-side contract. Seed an explicit active chat and
  // mock the complete chat surface so the suite neither reads nor borrows
  // rows from any backend database.
  await page.addInitScript(chatId => {
    localStorage.setItem('moebius_active_chat', chatId)
  }, chats[0].id)
  await page.route(/\/api\/chats(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    if (chatListResponder) return chatListResponder(route)
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(chats),
    })
  })
  await page.route(/\/api\/chats\/([0-9a-f-]+)(?:\?.*)?$/, route => {
    if (route.request().method() === 'PATCH' && chatPatchResponder) {
      return chatPatchResponder(route)
    }
    if (route.request().method() !== 'GET') return route.fallback()
    const id = new URL(route.request().url()).pathname.split('/').pop()
    // Capture the body when the request begins. A delayed cold read represents
    // that older server snapshot; later reads may observe a message accepted
    // while it was in flight without rewriting history inside the fixture.
    const detail = runtimeFixture.detail(
      detailForChat ? detailForChat(id) : navChatDetail(id, assistantContent),
    )
    const fulfill = () => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(detail),
    })
    return chatDetailGate?.id === id
      ? chatDetailGate.wait.then(fulfill)
      : fulfill()
  })
  await page.route('**/test-image.svg', route => route.fulfill({
    status: 200,
    contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="#567"/></svg>',
  }))

  // Intercept agent routes.
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/runtime(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtimeFixture.snapshot()),
    })
  })
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    undefined, { timeout: 10000 }
  )
}

// The app-history cases exercise the browser/app message boundary. They must
// not borrow whichever app happens to be installed in the runner database:
// that used to make the tests silently skip and provided no CI coverage.
async function installNavigationAppFixture(page) {
  await page.route(/\/api\/apps\/?(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{
        id: NAV_APP.id,
        name: NAV_APP.name,
        description: '',
        compiled_path: '',
        chat_id: null,
        source_dir: null,
        pinned_at: null,
        cross_app_access: 'none',
        share_with_apps: 'none',
        offline_capable: false,
        updated_at: '2026-01-01T00:00:00Z',
      }]),
    })
  })
  await page.route(new RegExp(`/api/apps/${NAV_APP.id}/frame`), route => route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: '<!doctype html><html><body><main>Navigation fixture</main></body></html>',
  }))
  await page.route(/\/api\/auth\/app-token$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ token: 'navigation-fixture-token' }),
  }))
}

for (const width of [412, 1512]) {

}

/** Read the current navigation state from the app. */
async function getNavState(page) {
  return page.evaluate(() => {
    const painted = document.querySelector('[data-chat-surface="painted"]')
    const chatScroll = painted?.querySelector('.chat__scroll')
    const emptyWrap = painted?.querySelector('.chat__empty-wrap')
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
  const expectedChat = NAV_CHATS[index]
  if (!expectedChat) throw new Error(`No navigation fixture at index ${index}`)

  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  const target = navigation.getByRole('button', { name: expectedChat.title, exact: true })
  await target.click()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
    .toBe(expectedChat.id)
  await page.waitForFunction(
    () => !document.querySelector('.settings')
      && !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
        || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    undefined, { timeout: 8000 }
  )
}

/** Navigate to an app by clicking in the drawer. */
async function navigateToApp(page, index = 0) {
  const app = index === 0 ? NAV_APP : null
  if (!app) throw new Error(`No navigation app fixture at index ${index}`)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await navigation.getByRole('button', { name: app.name, exact: true }).click()
  await expect(page.locator('.canvas')).toBeVisible()
}

/** Open the drawer via the toggle button (aria-expanded attribute). */
async function openDrawer(page) {
  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
}

async function dispatchDrawerPointerGesture(page, {
  pointerId,
  points,
  terminal = 'pointerup',
  compatibilityClickSelector = null,
}) {
  await page.locator('.drawer').evaluate((drawer, gesture) => {
    const init = ([clientX, clientY]) => ({
      bubbles: true,
      cancelable: true,
      pointerId: gesture.pointerId,
      pointerType: 'touch',
      isPrimary: true,
      button: 0,
      clientX,
      clientY,
    })
    const [start, ...moves] = gesture.points
    drawer.dispatchEvent(new PointerEvent('pointerdown', init(start)))
    for (const point of moves) {
      drawer.dispatchEvent(new PointerEvent('pointermove', init(point)))
    }
    const end = gesture.points.at(-1)
    if (gesture.terminal) {
      drawer.dispatchEvent(new PointerEvent(gesture.terminal, init(end)))
    }
    if (gesture.compatibilityClickSelector) {
      drawer.querySelector(gesture.compatibilityClickSelector)?.dispatchEvent(
        new MouseEvent('click', { ...init(end), detail: 1 }),
      )
    }
  }, { pointerId, points, terminal, compatibilityClickSelector })
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
  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  const wasOpen = await toggle.getAttribute('aria-expanded') === 'true'
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') === 'true') btn.click()
  })
  if (wasOpen) await expect(toggle).toHaveAttribute('aria-expanded', 'false')
}

/** Trigger browser back via history.back().
 *  Uses evaluate to fire within the SPA rather than Playwright's page.goBack
 *  which triggers a real page navigation. */
async function goBack(page) {
  await page.evaluate(() => history.back())
  // Wait from the test runner, not the page's old execution context: the assertion
  // below should report an accidental document navigation as the product failure,
  // rather than this helper racing the context swap with a second evaluate().
  await page.waitForTimeout(500)
}

async function goForward(page) {
  await page.evaluate(() => history.forward())
  await page.waitForTimeout(500)
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

  test('chat image preview consumes Back without leaving the chat', async ({ page }) => {
    await setup(page, { width: 412, height: 915 }, {
      assistantContent: '![Navigation preview](/test-image.svg)',
    })
    const initial = await getNavState(page)
    const image = page.locator('[data-chat-surface="painted"] .md-image-frame')
    await expect(image).toBeEnabled()

    await image.click()
    await expect(page.getByRole('dialog', { name: 'Navigation preview' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => history.state?.kind)).toBe('dismissible')

    await goBack(page)

    await expect(page.getByRole('dialog', { name: 'Navigation preview' })).toHaveCount(0)
    const afterBack = await getNavState(page)
    expect(afterBack.hasChat).toBe(true)
    expect(afterBack.activeChatId).toBe(initial.activeChatId)
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



test.describe('Touch navigation', () => {
  test.use({ hasTouch: true, isMobile: true })

  test('chat selection closes the drawer before the destination paints', async ({ page }) => {
    let releaseChatDetail
    const wait = new Promise(resolve => { releaseChatDetail = resolve })
    await setup(page, { width: 412, height: 915 }, {
      chatDetailGate: { id: NAV_CHATS[1].id, wait },
    })

    await openDrawer(page)
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    await navigation.getByRole('button', { name: NAV_CHATS[1].title, exact: true }).click()

    const drawer = page.locator('#navigation-drawer')
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(NAV_CHATS[1].id)
    await expect(drawer).not.toHaveClass(/drawer--open/)
    await expect(drawer).not.toHaveClass(/drawer--locked/)
    await expect(page.locator('.shell__content')).not.toHaveAttribute('inert', '')
    await expect.poll(() => page.evaluate(() => ({
      held: document.querySelector('.shell__chat-view--held')?.dataset.chatId,
      staging: document.querySelector('.shell__chat-view--staging')?.dataset.chatId,
    }))).toEqual({
      held: NAV_CHATS[0].id,
      staging: NAV_CHATS[1].id,
    })

    releaseChatDetail()

    await expect(page.locator('[data-chat-surface="painted"]'))
      .toHaveAttribute('data-chat-id', NAV_CHATS[1].id)
    await expect(drawer).not.toHaveClass(/drawer--open/)
    await expect(drawer).not.toHaveClass(/drawer--locked/)
  })

  test('New chat keeps the visible blank and its complete draft', async ({ page }) => {
    const blank = {
      ...NAV_CHATS[0],
      id: '10000000-0000-4000-8000-000000000098',
      title: 'Visible blank',
      has_messages: false,
    }
    await page.addInitScript(chatId => {
      sessionStorage.setItem('new-chat-intent', JSON.stringify({
        chatId,
        status: 'materialized',
      }))
      sessionStorage.setItem(`draft:${chatId}`, JSON.stringify({
        type: 'mobius-composer-draft',
        version: 2,
        updated_at: Date.now(),
        input: 'Keep this unfinished thought',
        attachments: [{
          name: 'reference.txt', size: 12, mime_type: 'text/plain',
        }],
      }))
    }, blank.id)
    await setup(page, undefined, {
      chats: [blank],
      detailForChat: emptyChatDetail,
    })

    const painted = page.locator('[data-chat-surface="painted"]')
    const composer = painted.getByRole('textbox', { name: 'Message Möbius…' })
    await expect(composer).toHaveValue('Keep this unfinished thought')
    await expect(painted.getByRole('button', { name: 'Remove reference.txt' })).toBeVisible()

    let createRequests = 0
    await page.route(/\/api\/chats(?:\?.*)?$/, async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      createRequests += 1
      return route.fulfill({ status: 500, body: '{}' })
    })
    await openDrawer(page)
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    await expect(navigation).toBeFocused()
    await navigation.getByRole('button', { name: 'New chat', exact: true }).click()

    await expect(page.getByRole('button', { name: 'Toggle navigation' }))
      .toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(blank.id)
    expect(createRequests).toBe(0)
    await expect(composer).toBeFocused()
    await expect(composer).toHaveValue('Keep this unfinished thought')
    await expect(painted.getByRole('button', { name: 'Remove reference.txt' })).toBeVisible()
  })















  test('New chat keeps options geometry, phone focus, and early typing through allocation', async ({ page }) => {
    await setup(page)
    await expect.poll(() => page.evaluate(() => (
      matchMedia('(hover: none) and (pointer: coarse)').matches
    ))).toBe(true)

    let releaseCreation
    const creationGate = new Promise(resolve => { releaseCreation = resolve })
    let requestedId = null
    await page.route(/\/api\/chats(?:\?.*)?$/, async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      requestedId = route.request().postDataJSON().id
      await creationGate
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(createdChat(requestedId)),
      })
    })

    await openDrawer(page)
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    // Let the drawer's opening focus frame settle before the New-chat tap.
    // The real regression is focus lost after this settled user interaction,
    // not a synthetic test click racing the drawer's own opening frame.
    await expect(navigation).toBeFocused()
    await navigation
      .getByRole('button', { name: 'New chat', exact: true })
      .click()

    const presentation = newChatSurface(page)
    const immediateComposer = presentation.getByRole('textbox', { name: 'Message Möbius…' })
    // Local attachment actions remain available; server reads wait for allocation.
    const pendingOptions = presentation.locator('.composer-plus > button')
    await expect(presentation).toBeVisible()
    await expect(presentation.getByText("What's on your mind?", { exact: true })).toBeVisible()
    await expect(pendingOptions).toHaveCount(1)
    await expect(pendingOptions).toBeVisible()
    await expect(pendingOptions.locator('svg').first()).toBeVisible()
    const pendingOptionsBox = await pendingOptions.boundingBox()
    await expect.poll(() => requestedId).not.toBeNull()
    const prematureReads = []
    const observeRead = request => {
      if (request.url().includes(`/api/chats/${requestedId}/`)) {
        prematureReads.push(new URL(request.url()).pathname)
      }
    }
    page.on('request', observeRead)
    await pendingOptions.click()
    const options = page.getByRole('dialog', { name: 'Chat options', exact: true })
    await expect(options.getByRole('button', { name: /Attach files/ })).toBeEnabled()
    await pendingOptions.click()
    await expect(options).toHaveCount(0)
    page.off('request', observeRead)
    expect(prematureReads).toEqual([])
    await expect(immediateComposer).toBeFocused()
    await page.keyboard.type('Typed while opening')
    await expect.poll(() => requestedId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    await expect.poll(() => page.evaluate(id => (
      JSON.parse(sessionStorage.getItem('new-chat-intent'))?.chatId === id
    ), requestedId)).toBe(true)
    await expect.poll(() => page.evaluate(id => (
      JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input
    ), requestedId)).toBe('Typed while opening')

    releaseCreation()

    const composer = page.locator('[data-chat-surface="painted"] textarea')
    await expect(composer).toBeFocused()
    await expect(composer).toHaveValue('Typed while opening')
    // Allocation keeps the same canonical composer instead of swapping in a
    // second surface: exactly one message composer exists throughout.
    await expect(page.getByRole('textbox', { name: 'Message Möbius…' })).toHaveCount(1)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(requestedId)
    await expect.poll(() => page.evaluate(id => (
      JSON.parse(sessionStorage.getItem('new-chat-intent'))
    ), requestedId)).toEqual({ chatId: requestedId, status: 'materialized' })
    await expect.poll(() => composer.evaluate(element => ({
      start: element.selectionStart,
      end: element.selectionEnd,
      length: element.value.length,
    }))).toEqual({ start: 19, end: 19, length: 19 })
    const readySurface = page.locator('[data-chat-surface="painted"]')
    const readyOptions = readySurface.locator('.composer-plus > button')
    await expect(readyOptions).toHaveCount(1)
    await expect(readyOptions).toBeVisible()
    await expect(readyOptions).toBeEnabled()
    const readyOptionsBox = await readyOptions.boundingBox()
    expect(pendingOptionsBox).not.toBeNull()
    expect(readyOptionsBox).not.toBeNull()
    expect(readyOptionsBox.width).toBe(pendingOptionsBox.width)
    expect(readyOptionsBox.height).toBe(pendingOptionsBox.height)
    expect(Math.abs(readyOptionsBox.x - pendingOptionsBox.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(readyOptionsBox.y - pendingOptionsBox.y)).toBeLessThanOrEqual(1)
    await page.keyboard.type(' after allocation')
    await expect(composer).toHaveValue('Typed while opening after allocation')
  })

  test('failed New chat allocation survives reload and retries the same id', async ({ page }) => {
    await setup(page)
    let createCount = 0
    let requestedId = null
    let releaseRetry
    const retryGate = new Promise(resolve => { releaseRetry = resolve })
    await page.route(/\/api\/chats(?:\?.*)?$/, async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      createCount += 1
      const body = route.request().postDataJSON()
      requestedId ||= body.id
      expect(body.id).toBe(requestedId)
      if (createCount === 1) {
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'temporarily unavailable' }),
        })
      }
      await retryGate
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(createdChat(requestedId)),
      })
    })

    await openDrawer(page)
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
    await expect(navigation).toBeFocused()
    await navigation.getByRole('button', { name: 'New chat', exact: true }).click()

    const presentation = newChatSurface(page)
    const composer = presentation.getByRole('textbox', { name: 'Message Möbius…' })
    await expect(presentation).toBeVisible()
    await expect(composer).toBeFocused()
    await composer.fill('Survives retry and reload')
    await expect(page.getByText('Couldn’t start a new chat — your draft is safe.'))
      .toBeVisible()
    await expect(presentation.getByRole('button', { name: 'Retry' })).toBeVisible()
    await expect.poll(() => page.evaluate(id => ({
      intent: JSON.parse(sessionStorage.getItem('new-chat-intent')),
      draft: JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input,
    }), requestedId)).toEqual({
      intent: { chatId: requestedId, status: 'failed' },
      draft: 'Survives retry and reload',
    })

    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector('.shell', { timeout: 10000 })
    await openDrawer(page)
    const reloadedNavigation = page.getByRole('navigation', { name: 'Primary navigation' })
    await expect(reloadedNavigation).toBeFocused()
    await reloadedNavigation.getByRole('button', { name: 'New chat', exact: true }).click()

    const restored = newChatSurface(page)
      .getByRole('textbox', { name: 'Message Möbius…' })
    await expect.poll(() => createCount).toBe(2)
    await expect(restored).toBeFocused()
    await expect(restored).toHaveValue('Survives retry and reload')
    await expect.poll(() => page.evaluate(id => ({
      intent: JSON.parse(sessionStorage.getItem('new-chat-intent')),
      draft: JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input,
    }), requestedId)).toEqual({
      intent: { chatId: requestedId, status: 'allocating' },
      draft: 'Survives retry and reload',
    })

    releaseRetry()
    const paintedComposer = page.locator('[data-chat-surface="painted"] textarea')
    await expect(paintedComposer).toBeFocused()
    await expect(paintedComposer).toHaveValue('Survives retry and reload')
    // The retried allocation resolves into the same canonical surface rather
    // than tearing down and remounting a separate presentation.
    await expect(page.getByRole('textbox', { name: 'Message Möbius…' })).toHaveCount(1)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('moebius_active_chat')))
      .toBe(requestedId)
  })
})

test.describe('Desktop sidebar navigation', () => {
  async function setupDesktop(page, open = true, options = {}) {
    await page.addInitScript(({ key, value }) => {
      if (localStorage.getItem(key) === null) localStorage.setItem(key, value)
    }, {
      key: 'mobius:desktop-sidebar-open:v1',
      value: String(open),
    })
    await setup(page, { width: 1280, height: 800 }, options)
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
      const drawerElement = document.querySelector('#navigation-drawer')
      const drawer = drawerElement.getBoundingClientRect()
      const content = document.querySelector('.shell__content').getBoundingClientRect()
      return {
        drawerRight: drawer.right,
        drawerWidth: drawer.width,
        drawerLayoutWidth: drawerElement.offsetWidth,
        contentLeft: content.left,
      }
    })
    const paintScale = geometry.drawerWidth / geometry.drawerLayoutWidth
    expect(geometry.drawerRight).toBeCloseTo(geometry.drawerLayoutWidth * paintScale, 1)
    expect(geometry.contentLeft).toBe(geometry.drawerRight)

    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => (
      localStorage.getItem('mobius:desktop-sidebar-open:v1')
    ))).toBe('false')
    await expect.poll(() => page.locator('.shell__content').evaluate(
      element => element.getBoundingClientRect().left,
    )).toBeCloseTo(58 * paintScale, 1)

    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
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


})

test.describe('Drawer touch lifecycle', () => {
  test.use({ hasTouch: true })






})

test.describe('Back button edge cases', () => {








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
  // These tests pin down each transition of the navigation/drawer state
  // machine. A modal drawer owns one history sentinel; destination navigation
  // retags that entry, and every close is consumed by the popstate handler.





















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





  test('20c. legacy Forward lands at app base and the next Back leaves once', async ({ page }) => {
    await installNavigationAppFixture(page)
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const appId = String(NAV_APP.id)
    const appFrame = await appFrameFor(page, NAV_APP.id)

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
    await installNavigationAppFixture(page)
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const appFrame = await appFrameFor(page, NAV_APP.id)

    await appFrame.evaluate(() => {
      window.__mobiusBackCount = 0
      window.__mobiusForwardCount = 0
      window.addEventListener('message', (event) => {
        const message = event?.data
        if (message?.type === 'moebius:nav-back') window.__mobiusBackCount += 1
        if (message?.type === 'moebius:nav-forward') {
          window.__mobiusForwardCount += 1
          window.parent.postMessage({
            type: 'moebius:nav-forward-ack',
            requestId: message.requestId,
          }, '*')
        }
      })
      window.parent.postMessage({
        type: 'moebius:nav-push',
        label: 'e2e-report',
        requestId: 'e2e-report',
        reversible: true,
      }, '*')
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
    await installNavigationAppFixture(page)
    await setup(page)
    await openDrawer(page)
    await navigateToApp(page, 0)
    const appFrame = await appFrameFor(page, NAV_APP.id)

    await appFrame.evaluate(() => {
      // Deliberately announce a reversible id the runtime never registered.
      // This models a fresh/evicted frame: its runtime-level responder must
      // explicitly reject the unknown restoration request.
      window.addEventListener('message', (event) => {
        if (event.data?.type !== 'moebius:nav-forward') return
        window.parent.postMessage({
          type: 'moebius:nav-forward-rejected',
          requestId: event.data.requestId,
        }, '*')
      })
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
    // The rejected restoration is an internal history repair. Its stable
    // browser contract is that it does not resurrect a nested app route and
    // the next physical Back leaves the app exactly once.
    await page.waitForFunction(() => history.state?.kind === 'nav')
    await goBack(page)
    await expect.poll(async () => (await getNavState(page)).hasChat).toBe(true)
  })
})

test.describe('Delete response boundaries', () => {

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



  test('22. Outside press closes drawer without activating revealed content', async ({ page }) => {
    await setup(page)
    await openDrawer(page)
    await navigateToSettings(page)
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
    await page.evaluate(() => {
      const probe = document.createElement('button')
      probe.id = 'drawer-underlay-probe'
      probe.textContent = 'Underlying action'
      probe.style.cssText = 'position:fixed;right:8px;top:280px;z-index:80'
      probe.addEventListener('click', () => {
        probe.dataset.clicks = String(Number(probe.dataset.clicks || 0) + 1)
      })
      document.body.appendChild(probe)
    })
    await openDrawer(page)
    expect((await getNavState(page)).drawerOpen).toBe(true)
    await page.locator('.drawer-overlay').dispatchEvent('pointerdown', {
      button: 0,
      isPrimary: true,
      pointerId: 4,
      pointerType: 'touch',
    })
    expect((await getNavState(page)).drawerOpen).toBe(false)

    // WebKit can retarget the compatibility click after the pointerdown has
    // removed the scrim. That click still belongs to the drawer dismissal.
    await page.locator('#drawer-underlay-probe').dispatchEvent('click', { detail: 1 })
    await expect(page.locator('#drawer-underlay-probe')).not.toHaveAttribute('data-clicks')

    // A genuinely new activation remains live. This also covers touch
    // sequences that never synthesize the compatibility click: pointerdown
    // releases any stale dismissal claim before the new click arrives.
    await page.locator('#drawer-underlay-probe').click()
    await expect(page.locator('#drawer-underlay-probe')).toHaveAttribute('data-clicks', '1')

    await openDrawer(page)
    await page.locator('.drawer-overlay').dispatchEvent('pointerdown', {
      button: 0,
      isPrimary: true,
      pointerId: 5,
      pointerType: 'touch',
    })
    await page.locator('#drawer-underlay-probe').click()
    await expect(page.locator('#drawer-underlay-probe')).toHaveAttribute('data-clicks', '2')

    // The close stays local to the drawer rather than navigating Settings.
    expect(await page.evaluate(() => !!document.querySelector('.settings'))).toBe(true)
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

















  test('22h. Desktop drawer resize follows pointer delta and settles lost capture', async ({ page }) => {
    await setup(page, { width: 1280, height: 800 })
    const drawer = page.locator('.drawer--persistent')
    const handle = page.getByRole('separator', { name: 'Resize navigation drawer' })
    await expect(handle).toBeVisible()

    const startWidth = await drawer.evaluate((element) => {
      element.style.left = '40px'
      return {
        client: element.getBoundingClientRect().width,
        layout: element.offsetWidth,
      }
    })
    await handle.evaluate((element) => {
      element.addEventListener('pointerdown', (event) => {
        element.dataset.testPointerId = String(event.pointerId)
        element.dataset.testPointerX = String(event.clientX)
        element.dataset.testPointerY = String(event.clientY)
      }, { once: true })
    })
    // Ask Playwright to resolve and hit-test the live handle after moving the
    // drawer. A cached bounding box can lag that style change under CI load.
    await handle.hover()
    await page.mouse.down()
    const pointer = await handle.evaluate((element) => ({
      id: Number(element.dataset.testPointerId),
      x: Number(element.dataset.testPointerX),
      y: Number(element.dataset.testPointerY),
    }))
    expect(Number.isInteger(pointer.id)).toBe(true)
    expect(Number.isFinite(pointer.x)).toBe(true)
    expect(Number.isFinite(pointer.y)).toBe(true)
    await page.mouse.move(pointer.x + 48, pointer.y)
    const released = await handle.evaluate((element) => {
      const pointerId = Number(element.dataset.testPointerId)
      if (!Number.isInteger(pointerId) || !element.hasPointerCapture(pointerId)) return false
      // A programmatic releasePointerCapture() only flushes lostpointercapture
      // on the next pointer-event dispatch, so dispatch the capture-loss event
      // the browser itself delivers when a drag's capture is interrupted.
      element.dispatchEvent(new PointerEvent('lostpointercapture', {
        pointerId,
        bubbles: true,
      }))
      return true
    })
    expect(released).toBe(true)

    await expect(drawer).not.toHaveClass(/drawer--resizing/)
    const settledWidth = await drawer.evaluate(element => ({
      client: element.getBoundingClientRect().width,
      stored: Number(localStorage.getItem('mobius:desktop-sidebar-width:v1')),
    }))
    const paintScale = startWidth.client / startWidth.layout
    expect(settledWidth.client).toBeCloseTo(startWidth.client + 48, 0)
    expect(settledWidth.stored * paintScale).toBeCloseTo(settledWidth.client, 0)
    await page.mouse.up()
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
  ws = paneModel.setViewMode(ws, 'panes')
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
  // Land on the origin, then seed the canonical workspace and re-navigate so
  // the shell boots the two-pane tree.
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const blob = paneModel.serializeWorkspace(twoAppPanes())
  await page.addInitScript(([wsKey, wsBlob]) => {
    try {
      localStorage.setItem(wsKey, wsBlob)
    } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, blob])
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
