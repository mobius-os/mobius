/**
 * Positive-behavior specs for the PR2 split-pane renderer (design §8 PR2 gate).
 *
 * These assert the load-bearing invariants the pure paneModel tests cannot see
 * — the renderer, DOM identity across a move, iframe re-init, and the persisted
 * tree across a projection flip (Codex finding 8):
 *
 *   (a) a pinned user message keeps its position across a divider drag;
 *   (b) a FOLLOW_BOTTOM chat keeps following AND does not remount across a
 *       divider resize and a cross-pane move (same root DOM object);
 *   (c) an app iframe survives a cross-pane move with no second frame-init;
 *   (d) split is absent from the context menu at caps;
 *   (e) a projection flip to phone preserves the persisted tree and pane focus;
 *
 * The flag is enabled per-test (localStorage 'mobius:workspace-splits' = '1')
 * and a 2-pane workspace blob is seeded in sessionStorage before the shell
 * boots, exactly like tabs.spec seeds the flat workspace. Agent + apps routes
 * are intercepted so no agent tokens are consumed.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/workspace-panes.spec.mjs --project=tests
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const DESKTOP_SIDEBAR_STORAGE_KEY = 'mobius:desktop-sidebar-open:v1'
const STREAM_ROUTE = /\/api\/chats\/[0-9a-f-]+\/stream$/
const WIDE = { width: 1400, height: 900 }
const PHONE = { width: 412, height: 760 }

test.use({ serviceWorkers: 'block' })
attachCleanup()

// A short, clean terminal stream: pins the user send with no streamed content.
const EMPTY_STREAM = [{ type: 'catch_up_done' }, { type: 'done' }]
// A long streamed reply so a chat can reach + hold FOLLOW_BOTTOM.
const FOLLOW_STREAM = [
  { type: 'catch_up_done' },
  { type: 'text', content: 'Streaming paragraph. '.repeat(80) },
  { type: 'done' },
]

async function replaceStreamRoute(page, events) {
  await page.unroute(STREAM_ROUTE)
  const body = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  await page.route(STREAM_ROUTE, route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    body,
  }))
}

/** Intercept the agent routes and land on the app origin so createTaggedChat +
 *  localStorage/sessionStorage are reachable. Returns nothing; per-test setup
 *  seeds the workspace and re-navigates. */
async function boot(page, viewport = WIDE) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await replaceStreamRoute(page, EMPTY_STREAM)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 })
}

async function ensureNavigationOpen(page) {
  const toggle = page.getByLabel('Toggle navigation')
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
}

/** Press-and-HOLD the logo past HOLD_MS (450ms) so the rAF completion fires — the
 *  real pointer path (not the deterministic Shift+Enter). The completed hold
 *  suppresses the trailing click, so it never also toggles the drawer. */
async function holdLogo(page, brand) {
  const box = await brand.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.waitForTimeout(650) // comfortably past the 450ms hold threshold
  await page.mouse.up()
}

/** Drawer rows intentionally exclude chats with no messages. These workspace
 *  drag tests create API-only chats so they can avoid agent runs; make only the
 *  requested fixtures satisfy the drawer-list contract while preserving the
 *  real backend response for every other field and chat. */
async function exposeChatsInDrawer(page, chatIds) {
  const visibleIds = new Set(chatIds.map(String))
  await page.route(/\/api\/chats(?:\?.*)?$/, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const chats = await response.json()
    await route.fulfill({
      response,
      json: chats.map(chat => visibleIds.has(String(chat.id))
        ? { ...chat, has_messages: true }
        : chat),
    })
  })
}

/** Report the apps list as `apps`, each with a stubbed frame that COUNTS the
 *  moebius:frame-init posts it receives (window.__fi). A cross-pane move that
 *  reparents the iframe would reload it and re-fire frame-init; the counter is
 *  how (c) proves the wrapper's contentWindow identity survived. */
async function mockApps(page, apps) {
  const state = { requests: 0 }
  await page.route(/\/api\/apps\/(\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    state.requests += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(apps.map(a => ({
        id: a.id, name: a.name, description: '', compiled_path: '',
        chat_id: a.chatId ?? null, source_dir: null, pinned_at: null,
        cross_app_access: 'none', share_with_apps: 'none', offline_capable: false,
        updated_at: '2026-07-12T12:00:00Z',
      }))),
    })
  })
  for (const a of apps) {
    await page.route(new RegExp(`/api/apps/${a.id}/frame`), route => route.fulfill({
      status: 200, contentType: 'text/html',
      body: '<!doctype html><html><body style="margin:0">'
        + '<div id="probe">app</div>'
        + '<script>window.__fi = 0;'
        + 'addEventListener("message", e => {'
        + ' if (e && e.data && e.data.type === "moebius:frame-init") window.__fi += 1;'
        + '});</script>'
        + '</body></html>',
    }))
  }
  // AppCanvas waits for an app-scoped token before mounting an online frame.
  // Without this half of the protocol the frame assertions silently skip.
  await page.route(/\/api\/auth\/app-token$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ token: 'mock-app-token' }),
  }))
  return state
}

/** Seed a workspace blob (authoritative) + the legacy flat mirror + the splits
 *  flag, all before the shell bundle evaluates on the next navigation. */
async function seedWorkspace(page, ws) {
  const blob = paneModel.serializeWorkspace(ws)
  const legacy = JSON.stringify(paneModel.flatten(ws).map(t => ({ kind: t.kind, id: t.id })))
  await page.addInitScript(([flagKey, wsKey, wsBlob, legKey, leg]) => {
    try {
      localStorage.setItem(flagKey, '1')
      sessionStorage.setItem(wsKey, wsBlob)
      sessionStorage.setItem(legKey, leg)
    } catch { /* private mode */ }
  }, ['mobius:workspace-splits', paneModel.STORAGE_KEY, blob, 'mobius-open-tabs', legacy])
}

/** Boot without a workspace blob and with an EMPTY legacy open-tabs mirror.
 *  The explicit deep link then creates the fallback's one leaf in builder mode
 *  with tabStripEngaged=false — the owner's real "one chat, strip never engaged"
 *  state where builder shows the drag surface and single-screen hides it. */
async function seedFallbackSingleLeaf(page) {
  await page.addInitScript(([flagKey, workspaceKey]) => {
    try {
      localStorage.setItem(flagKey, '1')
      sessionStorage.removeItem(workspaceKey)
      sessionStorage.setItem('mobius-open-tabs', '[]') // empty legacy -> strip not engaged
    } catch { /* private mode */ }
  }, ['mobius:workspace-splits', paneModel.STORAGE_KEY])
}

/** Two chat panes side by side: p0 = chatA (focused), p1 = chatB. */
function twoChatPanes(chatA, chatB) {
  let ws = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: chatA }, { kind: 'chat', id: chatB },
  ])
  ws = paneModel.moveTab(ws, `chat:${chatB}`, { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

/** Wait until the tiled chrome is up (its dividers laid out) and the panes have
 *  a real (post-ResizeObserver) width. */
async function waitTiled(page) {
  await expect(page.locator('.workspace__chrome')).toHaveCount(1, { timeout: 8000 })
  await expect(page.locator('.workspace__divider').first()).toBeVisible({ timeout: 8000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

/** Send a message inside a specific pane's own composer (multi-pane mounts one
 *  composer per pane, so the textbox must be scoped to the pane wrapper). */
async function sendInPane(page, chatId, text) {
  const pane = page.locator(`[data-tab-key="chat:${chatId}"]`)
  await pane.getByRole('textbox', { name: 'Message Möbius…' }).fill(text)
  await page.keyboard.press('Enter')
  await expect(pane.locator('.chat__scroll')).toBeVisible({ timeout: 4000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

/** Remember the actual ChatView root object. Comparing object identity after a
 *  move proves the no-reparent invariant without adding test-only markers to
 *  production markup. */
async function rememberChatRoot(page, chatId) {
  return page.evaluate((cid) => {
    window.__workspacePaneChatRoot = document.querySelector(
      `[data-tab-key="chat:${cid}"] .chat`,
    )
    return !!window.__workspacePaneChatRoot
  }, chatId)
}

async function rememberedChatRootIsCurrent(page) {
  return page.evaluate(() => {
    const root = window.__workspacePaneChatRoot
    // The same wrapper is --paned before collapse and --active afterward. Its
    // retained root object's connectivity is the cross-mode identity invariant.
    return !!root?.isConnected
  })
}

async function rememberedChatWrapperIsInert(page) {
  return page.evaluate(() => {
    const wrapper = window.__workspacePaneChatRoot?.closest('.shell__chat-view')
    return wrapper ? wrapper.hasAttribute('inert') : null
  })
}

/** Read scroll geometry through the remembered root. This keeps working after
 *  its pane wrapper moves or the layout collapses. */
async function rememberedChatScroll(page) {
  return page.evaluate(() => {
    const scroll = window.__workspacePaneChatRoot?.querySelector('.chat__scroll')
    if (!scroll) return null
    const gap = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight
    return { scrollTop: scroll.scrollTop, gap, nearBottom: gap < 60 }
  })
}

/** Engage FOLLOW_BOTTOM with a real gesture inside a specific pane's scroller
 *  (mirrors second-send-pin's gestureToBottom). */
async function gestureRememberedChatToBottom(page) {
  await page.evaluate(() => {
    const s = window.__workspacePaneChatRoot?.querySelector('.chat__scroll')
    if (!s) return
    s.scrollTop = s.scrollHeight
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
  await page.evaluate(() => {
    const s = window.__workspacePaneChatRoot?.querySelector('.chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.max(0, s.scrollTop - 1)
    s.scrollTop = s.scrollHeight
  })
}

/** Open a one-tab pane's context menu and click "Move to <label>". */
async function moveOnlyTabToOtherPane(page, paneId) {
  await page.locator(`[data-pane-strip="${paneId}"] .shell__tab-open`)
    .filter({ has: page.locator('.shell__tab-text') })
    .first()
    .click({ button: 'right' })
  await expect(page.locator('.workspace__menu')).toBeVisible({ timeout: 3000 })
  await page.locator('.workspace__menu-item', { hasText: /^Move to / }).first().click()
  await expect(page.locator('.workspace__menu')).toHaveCount(0)
}

test.describe('Workspace panes (PR2 gate)', () => {
  test('(a) a pinned user message keeps its position across a divider drag', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpA')
    const b = await createTaggedChat(page, 'wpB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    // Pin a message in pane A (first message pins). EMPTY_STREAM ends cleanly.
    await sendInPane(page, a.id, 'Pinned in pane A')
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))))

    const readTop = () => page.evaluate((cid) => {
      const wrap = document.querySelector(`[data-tab-key="chat:${cid}"]`)
      const scroll = wrap?.querySelector('.chat__scroll')
      const user = wrap?.querySelector('.chat__msg--user')
      if (!scroll || !user) return null
      return user.getBoundingClientRect().top - scroll.getBoundingClientRect().top
    }, a.id)

    const before = await readTop()
    expect(before, 'pinned message should be measurable').not.toBeNull()
    // The pin sits near the top of its pane.
    expect(before).toBeLessThanOrEqual(200)

    // Drag the vertical divider right — changes pane WIDTHS. A short top-pinned
    // message must not move vertically.
    const box = await page.locator('.workspace__divider').boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 140, box.y + box.height / 2, { steps: 6 })
    await page.mouse.up()
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))))

    const after = await readTop()
    expect(after, 'pinned message still measurable after drag').not.toBeNull()
    // Vertical position held (width change must not re-scroll the pin).
    expect(Math.abs(after - before)).toBeLessThanOrEqual(16)
  })

  test('(b) a following chat keeps following and does not remount across resize + cross-pane move', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpFollowA')
    const b = await createTaggedChat(page, 'wpFollowB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await replaceStreamRoute(page, FOLLOW_STREAM)
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    await sendInPane(page, a.id, 'Follow me')
    expect(await rememberChatRoot(page, a.id), 'chat A should have a root').toBe(true)

    // Engage FOLLOW_BOTTOM.
    await gestureRememberedChatToBottom(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 120)))

    // 1) Divider resize via the keyboard (SET_RATIO → re-project → paneResized).
    await page.locator('.workspace__divider').focus()
    await page.keyboard.press('ArrowRight')
    await page.keyboard.press('ArrowRight')
    // Keyboard divider steps bloom over 180ms (unlike pointer drags, which
    // suppress the transition), so wait past the animation before sampling.
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 250)))))
    expect(await rememberedChatRootIsCurrent(page), 'no remount across resize').toBe(true)
    const afterResize = await rememberedChatScroll(page)
    expect(afterResize, 'chat A scroller present after resize').not.toBeNull()
    await expect.poll(async () => (await rememberedChatScroll(page)).nearBottom,
      { message: 'still following after resize' }).toBe(true)

    // 2) Cross-pane move of chat A itself (p0 collapses to a single pane; the
    //    ChatView must not remount and FOLLOW must re-apply).
    await moveOnlyTabToOtherPane(page, 'p0')
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 150)))))
    await expect.poll(() => rememberedChatRootIsCurrent(page), {
      timeout: 4000,
      message: 'the same ChatView root survives the cross-pane move',
    }).toBe(true)
    const afterMove = await rememberedChatScroll(page)
    expect(afterMove, 'chat A scroller present after move').not.toBeNull()
    expect(afterMove.nearBottom, 'still following after cross-pane move').toBe(true)
    await expect(page.locator('.shell__chat-view--held')).toHaveCount(0, { timeout: 3000 })
    expect(await rememberedChatWrapperIsInert(page),
      'the retained one-pane wrapper is interactive after handoff').toBe(false)
  })

  test('pane focus is reversible, preserves sibling mounts, and exits to an interactive standard chat', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'focusPaneA')
    const b = await createTaggedChat(page, 'focusPaneB')
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await replaceStreamRoute(page, FOLLOW_STREAM)
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)
    await sendInPane(page, b.id, 'A long transcript remains scrollable after focused-pane exit')

    const baseline = await readWs(page)
    expect(await rememberChatRoot(page, a.id), 'sibling chat root is present').toBe(true)
    await expect(page.getByRole('button', { name: 'Focus pane' })).toHaveCount(2)

    await page.locator('[data-pane-strip="p1"]')
      .getByRole('button', { name: 'Focus pane' }).click()
    await expect(page.locator('[data-pane-strip]')).toHaveCount(1)
    await expect(page.locator('[data-pane-strip="p1"]')).toBeVisible()
    await expect(page.locator('.workspace__divider')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Show all panes' })).toHaveCount(1)
    expect(await rememberedChatRootIsCurrent(page),
      'the hidden sibling stays mounted while one pane is focused').toBe(true)

    const focused = await readWs(page)
    expect(focused.layout, 'focus does not rewrite the split tree').toEqual(baseline.layout)
    expect(focused.panes, 'focus does not rewrite tabs or ratios').toEqual(baseline.panes)
    expect(focused.focusedPaneId).toBe('p1')

    const contentBox = await page.locator('.shell__content').boundingBox()
    const focusedBox = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    expect(Math.abs(focusedBox.x - contentBox.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(focusedBox.width - contentBox.width)).toBeLessThanOrEqual(1)
    expect(focusedBox.y).toBeGreaterThan(contentBox.y)

    await page.getByRole('button', { name: 'Show all panes' }).click()
    await waitTiled(page)
    expect(await readWs(page), 'show-all restores presentation without another workspace write')
      .toEqual(focused)
    expect(await rememberedChatRootIsCurrent(page), 'the sibling root survives the round-trip').toBe(true)

    // Exit while focused: this is the reported trap path. The selected chat must
    // land as the one ordinary, scrollable surface with no stale inert cover, and
    // a subsequent drawer selection must still replace it.
    await page.locator('[data-pane-strip="p1"]')
      .getByRole('button', { name: 'Focus pane' }).click()
    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    await expect(page.locator('.workspace__chrome')).toHaveCount(0, { timeout: 3000 })
    await expect(page.locator('.shell__chat-view--held')).toHaveCount(0)
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1)
    await expect(page.locator('.shell__chat-view.shell__view--active')).not.toHaveAttribute('inert', '')
    await expect(page.locator('.shell__chat-view.shell__view--active .chat__scroll')).toBeVisible()

    await ensureNavigationOpen(page)
    await page.locator('.drawer__item').filter({ hasText: a.title }).click()
    await expect.poll(async () => String((await readWs(page)).singleScreen?.id), {
      timeout: 3000, message: 'standard mode remains navigable after focused-pane exit',
    }).toBe(String(a.id))
    await expect(page.locator('.shell__chat-view--held')).toHaveCount(0, { timeout: 3000 })
    await expect(page.locator('.shell__chat-view.shell__view--active')).not.toHaveAttribute('inert', '')
  })

  test('drawer activation focuses an already-open pane and reveals its clipped active tab', async ({ page }) => {
    await boot(page, PHONE)
    const a = await createTaggedChat(page, 'revealPaneA')
    const b = await createTaggedChat(page, 'revealPaneB')
    const c = await createTaggedChat(page, 'revealPaneC')
    const d = await createTaggedChat(page, 'revealPaneD')
    const e = await createTaggedChat(page, 'revealPaneE')
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id, d.id, e.id])

    let ws = paneModel.seedFromFlatTabs([
      { kind: 'chat', id: a.id }, { kind: 'chat', id: b.id },
      { kind: 'chat', id: c.id }, { kind: 'chat', id: d.id },
      { kind: 'chat', id: e.id },
    ])
    ws = paneModel.moveTab(ws, `chat:${b.id}`, { root: true, edge: 'right' })
    for (const chat of [c, d, e]) {
      ws = paneModel.moveTab(ws, `chat:${chat.id}`, { paneId: 'p1' })
    }
    ws = paneModel.setActiveTab(ws, 'p1', `chat:${b.id}`)
    ws = paneModel.focusPane(ws, 'p0')
    await seedWorkspace(page, ws)
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('[data-pane-strip="p1"]')).toBeVisible({ timeout: 8000 })

    const strip = page.locator('[data-pane-strip="p1"]')
    const target = strip.locator(`[data-drag-key="chat:${b.id}"]`)
    await expect.poll(() => strip.evaluate(el => el.scrollWidth > el.clientWidth), {
      timeout: 3000, message: 'the destination pane strip overflows',
    }).toBe(true)
    await strip.evaluate(el => { el.scrollLeft = el.scrollWidth })
    const clipped = await target.boundingBox()
    const stripBefore = await strip.boundingBox()
    expect(clipped.x + clipped.width).toBeLessThanOrEqual(stripBefore.x + 2)

    await ensureNavigationOpen(page)
    await page.locator('.drawer__item').filter({ hasText: b.title }).click()
    await expect.poll(async () => (await readWs(page)).focusedPaneId, {
      timeout: 3000, message: 'the existing tab owner becomes the focused pane',
    }).toBe('p1')
    await expect(target).toHaveAttribute('aria-selected', 'true')
    const revealed = await target.boundingBox()
    const stripAfter = await strip.boundingBox()
    expect(revealed.x).toBeGreaterThanOrEqual(stripAfter.x - 1)
    expect(revealed.x + revealed.width).toBeLessThanOrEqual(stripAfter.x + stripAfter.width + 1)

    // Repeating the current route remains history-free, but still re-reveals the
    // tab after the user has scrolled the strip away from it.
    await strip.evaluate(el => { el.scrollLeft = el.scrollWidth })
    await ensureNavigationOpen(page)
    await page.locator('.drawer__item').filter({ hasText: b.title }).click()
    const rerevealed = await target.boundingBox()
    const stripFinal = await strip.boundingBox()
    expect(rerevealed.x).toBeGreaterThanOrEqual(stripFinal.x - 1)
    expect(rerevealed.x + rerevealed.width).toBeLessThanOrEqual(stripFinal.x + stripFinal.width + 1)
  })

  test('(c) an app iframe survives a cross-pane move with no second frame-init', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpAppChatA')
    const b = await createTaggedChat(page, 'wpAppChatB')
    const APP_ID = 990101
    await mockApps(page, [{ id: APP_ID, name: 'Pane App', chatId: a.id }])

    // p0 = [chatA, app] (app active), p1 = [chatB]. Moving the app to p1 keeps
    // both panes (chatA survives in p0), so it is a true cross-pane move.
    let ws = paneModel.seedFromFlatTabs([
      { kind: 'chat', id: b.id }, { kind: 'chat', id: a.id }, { kind: 'app', id: APP_ID },
    ])
    ws = paneModel.moveTab(ws, `chat:${b.id}`, { root: true, edge: 'right' })
    ws = paneModel.focusPane(ws, 'p0')
    await seedWorkspace(page, ws)
    await page.goto(`${BASE}/shell/?app=${APP_ID}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    const iframe = page.locator(`iframe[data-app-id="${APP_ID}"]`)
    await expect(iframe).toHaveCount(1, { timeout: 5000 })
    // Let the parent's onLoad + token frame-init posts settle outside the page
    // execution context. The shell may still canonicalize its URL here; a
    // page-owned timer is destroyed by that navigation and makes the identity
    // check flaky before the move has even happened.
    await page.waitForTimeout(300)
    const iframeHandle = await iframe.elementHandle()
    const appFrame = await iframeHandle?.contentFrame()
    expect(appFrame, 'the mocked app frame is mounted').not.toBeNull()
    await appFrame.waitForFunction(() => typeof window.__fi === 'number', { timeout: 4000 })
    const initsBefore = await appFrame.evaluate(() => window.__fi)

    // Exactly one iframe wrapper for the app before the move.
    await expect(page.locator(`[data-tab-key="app:${APP_ID}"]`)).toHaveCount(1)

    // Move the app tab from p0 to p1 via its pane-strip context menu.
    await page.locator(`[data-pane-strip="p0"] .shell__tab--active .shell__tab-open`)
      .click({ button: 'right' })
    await expect(page.locator('.workspace__menu')).toBeVisible({ timeout: 3000 })
    await page.locator('.workspace__menu-item', { hasText: /^Move to / }).first().click()
    await expect(page.locator('.workspace__menu')).toHaveCount(0)
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 200)))))

    // Same frame object, and no additional frame-init: the iframe was never
    // reparented (a sandbox reparent = reload = fresh contentWindow + re-init).
    const stillSameFrame = page.frames().includes(appFrame)
    expect(stillSameFrame, 'the app frame object is identical after the move').toBe(true)
    const initsAfter = await appFrame.evaluate(() => window.__fi)
    expect(initsAfter, 'no second frame-init after the cross-pane move').toBe(initsBefore)
    await expect(page.locator(`[data-tab-key="app:${APP_ID}"]`)).toHaveCount(1)
  })

  test('(d) split is absent from the context menu at caps', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpCapA')
    const b = await createTaggedChat(page, 'wpCapB')
    const c = await createTaggedChat(page, 'wpCapC')
    const d = await createTaggedChat(page, 'wpCapD')
    await mockApps(page, [])

    // A depth-2 tree row(p0, col(p1, p2)) where p1 holds two tabs. p1 is at the
    // depth cap, so canSplit is false on every edge even though the pane has ≥2
    // tabs (which is what would otherwise offer a split).
    let ws = paneModel.seedFromFlatTabs([
      { kind: 'chat', id: a.id }, { kind: 'chat', id: c.id },
      { kind: 'chat', id: d.id }, { kind: 'chat', id: b.id },
    ])
    ws = paneModel.moveTab(ws, `chat:${c.id}`, { root: true, edge: 'right' })
    ws = paneModel.moveTab(ws, `chat:${b.id}`, { paneId: 'p1', edge: 'bottom' })
    ws = paneModel.moveTab(ws, `chat:${d.id}`, { paneId: 'p1' })
    // p1 = [c, d] at depth 2. Pre-validate the fixture at the model layer so a
    // wrong choreography fails loudly here, not as a confusing DOM assertion.
    expect(ws.panes.p1.tabs.length, 'p1 has two tabs').toBe(2)
    for (const edge of ['left', 'right', 'top', 'bottom']) {
      expect(
        paneModel.canSplit(ws, 'p1', edge, 'wide', { x: 0, y: 0, w: WIDE.width, h: WIDE.height }),
        `p1 cannot split ${edge} at the depth cap`,
      ).toBe(false)
    }
    ws = paneModel.focusPane(ws, 'p1')
    await seedWorkspace(page, ws)
    await page.goto(`${BASE}/shell/?chat=${c.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    // Open the context menu on p1's active tab.
    await page.locator(`[data-pane-strip="p1"] .shell__tab--active .shell__tab-open`)
      .click({ button: 'right' })
    await expect(page.locator('.workspace__menu')).toBeVisible({ timeout: 3000 })
    // No "Split *" item — the caps gate removed every direction.
    await expect(page.locator('.workspace__menu-item', { hasText: /^Split / })).toHaveCount(0)
    // The menu is still functional (Move / Close remain).
    await expect(page.locator('.workspace__menu-item', { hasText: /^Move to / }))
      .not.toHaveCount(0)
  })

  test('(e) a projection flip to phone preserves the persisted tree and pane focus', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpFlipA')
    const b = await createTaggedChat(page, 'wpFlipB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    // Baseline = the normalized blob the shell persisted after boot (a resize
    // must not rewrite it — geometry is projection, not persisted state).
    const beforeBlob = await page.evaluate(k => sessionStorage.getItem(k), paneModel.STORAGE_KEY)
    expect(beforeBlob, 'workspace blob persisted').toBeTruthy()

    // Flip the projection: wide → phone.
    await page.setViewportSize(PHONE)
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 200)))))

    const afterBlob = await page.evaluate(k => sessionStorage.getItem(k), paneModel.STORAGE_KEY)
    expect(afterBlob, 'the persisted tree is unchanged across the projection flip').toBe(beforeBlob)
    // The tree still parses to two panes (projection changed, tree did not).
    const leaves = await page.evaluate((k) => {
      const ws = JSON.parse(sessionStorage.getItem(k))
      return Object.keys(ws.panes).length
    }, paneModel.STORAGE_KEY)
    expect(leaves).toBe(2)

    // Focus still works: select the OTHER pane and verify the durable workspace
    // authority changes, rather than merely clicking the already-focused p0.
    await expect(page.locator('.workspace__chrome')).toHaveCount(1)
    const otherStrip = page.locator('[data-pane-strip="p1"]')
    await expect(otherStrip).toBeVisible({ timeout: 4000 })
    await otherStrip.click()
    await expect.poll(
      () => page.evaluate((k) => JSON.parse(sessionStorage.getItem(k)).focusedPaneId,
        paneModel.STORAGE_KEY),
      { timeout: 3000, message: 'phone projection still commits pane focus' },
    ).toBe('p1')
    await expect(otherStrip).toHaveClass(/workspace__strip--focused/)
  })

})

/**
 * PR3 drag controller (design §8 PR3 row). Mouse-path drags of a strip tab
 * exercise the whole binding end-to-end: the delegated pointerdown arms past
 * slop, geometric hit-testing picks the zone, and the drop dispatches exactly
 * one reducer action — asserted through the persisted workspace blob (the same
 * authority the PR2 cases read). Mobile cases use Chrome's real touch input, not
 * synthetic PointerEvents, so pointer-cancellation and touch-action are covered.
 */

// p0 = [chatA, chatC] (focused, C active), p1 = [chatB]. A two-tab source pane
// so a drag OUT of it leaves the pane alive and the moves are unambiguous.
function twoPanesThreeTabs(a, b, c) {
  let ws = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: a }, { kind: 'chat', id: b }, { kind: 'chat', id: c },
  ])
  ws = paneModel.moveTab(ws, `chat:${b}`, { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

function twoStackedPanesThreeTabs(a, b, c) {
  let ws = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: a }, { kind: 'chat', id: b }, { kind: 'chat', id: c },
  ])
  ws = paneModel.moveTab(ws, `chat:${b}`, { root: true, edge: 'bottom' })
  return paneModel.focusPane(ws, 'p0')
}

function singlePaneThreeTabs(a, b, c) {
  return paneModel.seedFromFlatTabs([
    { kind: 'chat', id: a }, { kind: 'chat', id: b }, { kind: 'chat', id: c },
  ])
}

function whichPaneHas(ws, tabKey) {
  for (const [pid, pane] of Object.entries(ws.panes)) {
    if (pane.tabs.some(t => `${t.kind}:${t.id}` === tabKey)) return pid
  }
  return null
}

async function readWs(page) {
  return page.evaluate(k => JSON.parse(sessionStorage.getItem(k)), paneModel.STORAGE_KEY)
}

/** Press on a source element, arm past slop, glide to a target point, release —
 *  the mouse-path drag Chromium delivers as real pointer events. */
async function mouseDrag(page, sourceLocator, toX, toY, { release = true } = {}) {
  const box = await sourceLocator.boundingBox()
  const sx = box.x + box.width / 2
  const sy = box.y + box.height / 2
  await page.mouse.move(sx, sy)
  await page.mouse.down()
  await page.mouse.move(sx + 10, sy, { steps: 3 }) // clear the 5px slop → arm
  await page.mouse.move(toX, toY, { steps: 14 })
  if (release) await page.mouse.up()
}

/** A real Chromium touch stream (not synthetic PointerEvents). */
async function touchDrag(page, sourceLocator, toX, toY, { firstDx = 0, firstDy = 12 } = {}) {
  const box = await sourceLocator.boundingBox()
  const sx = box.x + box.width / 2
  const sy = box.y + box.height / 2
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  const point = (x, y) => [{ x, y, radiusX: 4, radiusY: 4, force: 1, id: 1 }]
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(sx, sy) })
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchMove', touchPoints: point(sx + firstDx, sy + firstDy),
  })
  for (let i = 1; i <= 10; i += 1) {
    const t = i / 10
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: point(
        sx + firstDx + (toX - sx - firstDx) * t,
        sy + firstDy + (toY - sy - firstDy) * t,
      ),
    })
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await cdp.detach()
}

async function bootThreeTab(page, tag, workspaceFixture = twoPanesThreeTabs, expectTiled = true) {
  await boot(page, WIDE)
  // These cases exercise full-width three-pane geometry. The persistent
  // sidebar legitimately reduces the usable content rect, so make the
  // workspace-width precondition explicit instead of relying on the old
  // desktop drawer being overlaid.
  await page.evaluate(key => localStorage.setItem(key, 'false'), DESKTOP_SIDEBAR_STORAGE_KEY)
  const a = await createTaggedChat(page, `${tag}A`)
  const b = await createTaggedChat(page, `${tag}B`)
  const c = await createTaggedChat(page, `${tag}C`)
  await mockApps(page, [])
  await seedWorkspace(page, workspaceFixture(a.id, b.id, c.id))
  await page.goto(`${BASE}/shell/?chat=${c.id}`, { waitUntil: 'domcontentloaded' })
  if (expectTiled) await waitTiled(page)
  else await expect(page.locator('[data-pane-strip="p0"]')).toBeVisible({ timeout: 8000 })
  return { a, b, c }
}

test.describe('Workspace drag (PR3)', () => {
  test('dragging a tab to a pane edge splits (one new pane)', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'dragEdge')
    const p1 = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    // Drop inside p1's right edge band → split p1, C alone in the new pane.
    await mouseDrag(page, src, p1.x + p1.width - 18, p1.y + p1.height / 2)
    await expect.poll(async () => Object.keys((await readWs(page)).panes).length, {
      timeout: 3000, message: 'the edge drop created a third pane',
    }).toBe(3)
    const ws = await readWs(page)
    const home = whichPaneHas(ws, `chat:${c.id}`)
    expect(ws.panes[home].tabs.length, 'C is alone in the new pane').toBe(1)
    const bPane = whichPaneHas(ws, `chat:${b.id}`)
    expect(bPane, 'B kept its own pane').not.toBe(home)
    // The split target is untouched — no transient insert, no active-tab churn
    // (review B1: the create-in-new-pane path never mutates the target pane).
    expect(ws.panes[bPane].tabs.map(t => `${t.kind}:${t.id}`), 'B pane tab set intact')
      .toEqual([`chat:${b.id}`])
    expect(ws.panes[bPane].activeTabKey, 'B stays the active tab of its pane').toBe(`chat:${b.id}`)
  })

  test('dragging a tab onto another strip inserts it there (move, no new pane)', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'dragStrip')
    const strip = await page.locator('[data-pane-strip="p1"]').boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    await mouseDrag(page, src, strip.x + strip.width / 2, strip.y + strip.height / 2)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'C landed in p1 via the caret' },
    ).toBe('p1')
    const ws = await readWs(page)
    expect(Object.keys(ws.panes).length, 'still two panes (a move, not a split)').toBe(2)
  })

  test('dragging a tab to a pane center joins it as a tab', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'dragCenter')
    const p1 = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    await mouseDrag(page, src, p1.x + p1.width / 2, p1.y + p1.height / 2)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'C joined p1 as a tab' },
    ).toBe('p1')
    expect(Object.keys((await readWs(page)).panes).length).toBe(2)
  })

  test('Escape mid-drag cancels with no mutation', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'dragEsc')
    const before = await readWs(page)
    const p1 = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    // Arm and hover a live zone, then Escape before release.
    await mouseDrag(page, src, p1.x + p1.width / 2, p1.y + p1.height / 2, { release: false })
    await page.keyboard.press('Escape')
    await page.mouse.up()
    await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))
    const after = await readWs(page)
    expect(whichPaneHas(after, `chat:${c.id}`), 'C never left p0').toBe('p0')
    expect(Object.keys(after.panes).length).toBe(Object.keys(before.panes).length)
  })

  test('a cancelled drag cannot swallow the next intentional tab press', async ({ page }) => {
    const { a, c } = await bootThreeTab(page, 'dragThenPress')
    const source = page.locator(
      `[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${a.id}"]`,
    )
    const box = await source.boundingBox()
    const x = box.x + box.width / 2
    const y = box.y + box.height / 2

    // Use a synthetic pointer stream so Chromium emits no compatibility click.
    // This leaves the controller's one-shot click guard standing after Escape,
    // exactly like browsers/devices that suppress the drag's compat click.
    await source.dispatchEvent('pointerdown', {
      pointerId: 91, pointerType: 'mouse', isPrimary: true,
      button: 0, buttons: 1, clientX: x, clientY: y,
    })
    await source.dispatchEvent('pointermove', {
      pointerId: 91, pointerType: 'mouse', isPrimary: true,
      button: 0, buttons: 1, clientX: x + 10, clientY: y,
    })
    await expect(page.locator('.workspace__drag-chip')).toBeVisible({ timeout: 3000 })
    await page.keyboard.press('Escape')
    await expect(page.locator('.workspace__drag-chip')).toHaveCount(0)
    expect((await readWs(page)).panes.p0.activeTabKey).toBe(`chat:${c.id}`)

    // A new physical press is user intent, never the old drag's compat click.
    await source.click()
    await expect.poll(async () => (await readWs(page)).panes.p0.activeTabKey, {
      timeout: 3000, message: 'the first fresh press activates the requested tab',
    }).toBe(`chat:${a.id}`)
  })

  test('the undo chord restores a mis-dropped tab', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'dragUndo')
    const p1 = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    await mouseDrag(page, src, p1.x + p1.width / 2, p1.y + p1.height / 2)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000 },
    ).toBe('p1')
    // There is no undo toast anymore (owner removed it as noise). Recovery is the
    // Cmd/Ctrl+Z chord, which fires only while no text input holds focus. The
    // restore assertion is unchanged.
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Control+z')
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'Undo returned C to p0' },
    ).toBe('p0')
  })

  test('phone touch-drag moves a tab between stacked panes without a long press', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'touchDrag')
    await page.setViewportSize(PHONE)
    await expect(page.locator('[data-pane-strip="p1"]')).toBeVisible({ timeout: 4000 })
    const target = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    await touchDrag(page, src, target.x + target.width / 2, target.y + target.height / 2)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'the real touch stream moved C into the lower pane' },
    ).toBe('p1')
  })

  test('phone horizontal touch-drag reorders tabs in the same strip', async ({ page }) => {
    const { a, c } = await bootThreeTab(page, 'touchReorder')
    await page.setViewportSize(PHONE)
    const target = await page.locator(
      `[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${a.id}"]`,
    ).boundingBox()
    const src = page.locator(
      `[data-pane-strip="p0"] [data-touch-drag-handle="chat:${c.id}"]`,
    )
    await expect(src).toHaveClass(/shell__tab-kind/)
    await expect(page.locator('.shell__tab-drag-handle')).toHaveCount(0)
    await touchDrag(page, src, target.x + 2, target.y + target.height / 2, {
      firstDx: -12, firstDy: 0,
    })
    await expect.poll(async () => (await readWs(page)).panes.p0.tabs
      .map(t => `${t.kind}:${t.id}`), {
      timeout: 3000, message: 'the real horizontal touch stream reordered p0',
    }).toEqual([`chat:${c.id}`, `chat:${a.id}`])
  })

  test('phone horizontal swipe over a tab body scrolls overflow without reordering', async ({ page }) => {
    const { a, b, c } = await bootThreeTab(
      page, 'touchScroll', singlePaneThreeTabs, false,
    )
    await page.setViewportSize(PHONE)
    const strip = page.locator('[data-pane-strip="p0"]')
    await expect.poll(() => strip.evaluate(el => el.scrollWidth > el.clientWidth), {
      timeout: 3000, message: 'the phone strip has horizontal overflow',
    }).toBe(true)
    const beforeOrder = (await readWs(page)).panes.p0.tabs.map(t => `${t.kind}:${t.id}`)
    const beforeScroll = await strip.evaluate(el => el.scrollLeft)
    const stripBox = await strip.boundingBox()
    const body = page.locator(
      `[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${b.id}"] .shell__tab-text`,
    )
    await touchDrag(page, body, stripBox.x + 20, stripBox.y + stripBox.height / 2, {
      firstDx: -12, firstDy: 0,
    })
    await expect.poll(() => strip.evaluate(el => el.scrollLeft), {
      timeout: 3000, message: 'native pan-x advances the overflowed strip',
    }).toBeGreaterThan(beforeScroll + 20)
    expect((await readWs(page)).panes.p0.tabs.map(t => `${t.kind}:${t.id}`))
      .toEqual(beforeOrder)
    await expect(page.locator('.workspace__drag-chip')).toHaveCount(0)
    expect(beforeOrder).toEqual([`chat:${a.id}`, `chat:${b.id}`, `chat:${c.id}`])

    // A conventional vertical mouse wheel reaches the same hidden overflow;
    // trackpad deltaX remains native and is deliberately not doubled.
    await strip.evaluate(el => { el.scrollLeft = 0 })
    await strip.dispatchEvent('wheel', { deltaX: 0, deltaY: 96, deltaMode: 0 })
    await expect.poll(() => strip.evaluate(el => el.scrollLeft), {
      timeout: 3000, message: 'vertical wheel advances the horizontal tab strip',
    }).toBeGreaterThan(50)
  })

  test('phone touch-drag resizes the pane divider', async ({ page }) => {
    await bootThreeTab(page, 'touchResize', twoStackedPanesThreeTabs)
    await page.setViewportSize(PHONE)
    const divider = page.locator('.workspace__divider').first()
    await expect(divider).toBeVisible({ timeout: 4000 })
    const before = (await readWs(page)).layout.ratio
    const box = await divider.boundingBox()
    await touchDrag(page, divider, box.x + box.width / 2, box.y + box.height / 2 + 90)
    await expect.poll(async () => (await readWs(page)).layout.ratio, {
      timeout: 3000, message: 'the real touch stream resized the stacked panes',
    }).not.toBe(before)
  })

  test('the focused active title reveals once, returns to its start, and restarts on reopen', async ({ page }) => {
    const { a, c } = await bootThreeTab(page, 'aVeryLongActiveChatTitle')
    await page.setViewportSize(PHONE)
    const title = page.locator(
      `[data-pane-strip="p0"] [data-drag-key="chat:${c.id}"] .shell__tab-text`,
    )
    await expect(title).toHaveAttribute('data-overflow', 'true', { timeout: 3000 })
    const motion = await title.evaluate(el => {
      const inner = el.querySelector('.shell__tab-text-inner')
      const style = getComputedStyle(inner)
      return {
        name: style.animationName,
        iterations: style.animationIterationCount,
        duration: style.animationDuration,
        delay: style.animationDelay,
        shift: parseFloat(el.style.getPropertyValue('--tab-title-shift')),
      }
    })
    expect(motion.name).toBe('shell-tab-title-cycle')
    expect(motion.iterations).toBe('1')
    const durationMs = Number.parseFloat(motion.duration) * 1000
    const expectedDurationMs = Math.min(
      32000,
      Math.max(8000, Math.round(Math.abs(motion.shift) * (1000 / 6))),
    )
    expect(durationMs).toBeCloseTo(expectedDurationMs, -1)
    expect(durationMs).toBeGreaterThan(4800)
    expect(motion.delay).toBe('0.7s')
    expect(motion.shift).toBeLessThan(0)

    // Jump the bounded animation to completion: its filled final frame is the
    // beginning of the title, and it remains finished rather than looping.
    const final = await title.evaluate(async (el) => {
      const inner = el.querySelector('.shell__tab-text-inner')
      const animation = inner.getAnimations()[0]
      animation.finish()
      await new Promise(resolve => requestAnimationFrame(resolve))
      return { transform: getComputedStyle(inner).transform, playState: animation.playState }
    })
    expect(final.playState).toBe('finished')
    expect(['none', 'matrix(1, 0, 0, 1, 0, 0)']).toContain(final.transform)

    // Opening another tab removes the animation; reopening this one creates a
    // fresh bounded pass from the beginning.
    await page.locator(`[data-pane-strip="p0"] [data-drag-key="chat:${a.id}"]`).click()
    await page.locator(`[data-pane-strip="p0"] [data-drag-key="chat:${c.id}"]`).click()
    await expect(page.locator(
      `[data-pane-strip="p0"] [data-drag-key="chat:${c.id}"]`,
    )).toHaveAttribute('aria-selected', 'true')
    const restarted = await title.evaluate((el) => {
      const animation = el.querySelector('.shell__tab-text-inner').getAnimations()[0]
      return { playState: animation?.playState, currentTime: animation?.currentTime }
    })
    expect(restarted.playState).toBe('running')
    expect(restarted.currentTime).toBeLessThan(1500)
  })
})

/**
 * View-mode control (design: builder-mode activation). Hold/swipe the top-left
 * Möbius brand or drag from the drawer; there is deliberately no second header icon.
 * Shift+Enter remains the keyboard path. Builder mode is
 * the accent .shell__brand--builder state. Single-mode collapses the preserved tree to
 * the focused pane full-bleed WITHOUT rewriting the persisted geometry, so a
 * round-trip restores the identical tree. The one blob field a first flip DOES
 * write is the two-worlds `singleScreen` slot — seeded once from the focused
 * item (paneModel.seedSingleScreenIfAbsent) and never reseeded after. In single-mode with a multi-pane tree
 * dragging is disabled (attempted drawer-row drag: no split, the LOGO vibrates —
 * the bar paints above the drawer scrim so it is perceivable). In single-mode with
 * ONE leaf dragging stays on: a SPLITTING (edge) drop opts back into panes, a
 * non-splitting (center-join) drop does not.
 */
test.describe('Workspace view-mode toggle', () => {
  test('the header never renders a standalone panes button', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'modeButtonA')
    const b = await createTaggedChat(page, 'modeButtonB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    await expect(page.locator('.shell__mode-toggle')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /Use (panes|single screen)/ })).toHaveCount(0)
  })

  test('the logo gesture flips to single (geometry preserved, one pane) and back', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmA')
    const b = await createTaggedChat(page, 'vmB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    const baseline = await readWs(page)
    expect(baseline.viewMode).toBe('panes')

    // Exercise the logo's keyboard shortcut (Shift+Enter). It must NOT
    // change the navigation state: no modal drawer opens, and the persistent
    // desktop sidebar (WIDE viewport) keeps its aria-expanded.
    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    const navigationWasOpen = await brand.getAttribute('aria-expanded')
    await expect(brand).toHaveClass(/shell__brand--builder/) // builder is the accent state
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    // On WIDE the persistent desktop sidebar is docked-OPEN by default, so scope
    // the "no drawer opened" check to non-persistent (modal) drawers; the flip
    // must not open a modal drawer, and aria-expanded must be unchanged.
    await expect(page.locator('.drawer.drawer--open:not(.drawer--persistent)')).toHaveCount(0)
    await expect(brand).toHaveAttribute('aria-expanded', navigationWasOpen)

    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    const single = await readWs(page)
    expect(single.layout).toEqual(baseline.layout)
    expect(single.panes).toEqual(baseline.panes)
    expect(single.focusedPaneId).toBe(baseline.focusedPaneId)
    expect(single.nextId).toBe(baseline.nextId)
    // Two-worlds: the FIRST flip seeds the single-screen slot from the focused
    // item (chat a) — the only blob field the flip is allowed to write.
    expect(single.singleScreen).toEqual({ kind: 'chat', id: String(a.id) })
    await expect(brand).not.toHaveClass(/shell__brand--builder/) // the mark drops the accent state

    // Leaving builder from a TILED workspace now holds the tiled render for one
    // reverse card-deal beat (~250ms, BUILDER_EXIT_MS) before collapsing (item 1),
    // so the post-exit state settles AFTER that beat — these locator assertions
    // auto-retry (default timeout) and therefore poll past the new duration.
    // Render collapsed to one full-bleed pane (the focused chat a), no chrome.
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1)
    await expect(page.locator(`[data-tab-key="chat:${a.id}"].shell__view--active`)).toHaveCount(1)

    // Flip back — identical tree restored (never mutated); the only delta vs the
    // pre-flip baseline is the seeded slot, which the return flip must NOT clear.
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await waitTiled(page)
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('panes')
    expect(await readWs(page)).toEqual({
      ...baseline,
      singleScreen: { kind: 'chat', id: String(a.id) },
    })
  })

  // Regression (item 0): a genuine MULTI-PANE exit via the real POINTER-HOLD
  // completion path — the path the single-leaf keyboard verification missed — must
  // collapse the tiled workspace and STAY collapsed, and a rapid re-enter within
  // the exit beat must never strand the beat (the old two-latch shape could leave
  // builderExiting true forever → tiled after the mode flipped). Runs with motion
  // ON so the exit reverse-deal beat actually engages (the bug lived in the beat).
  test('(item 0) multi-pane POINTER-HOLD exit collapses and stays; rapid re-enter never wedges', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'holdExitA')
    const b = await createTaggedChat(page, 'holdExitB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)
    expect((await readWs(page)).viewMode).toBe('panes')

    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    // The pointer-hold completion arms the multi-pane exit beat; the collapse must
    // LAND, not stall tiled.
    await holdLogo(page, brand)
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    // The regression symptom was "panes never collapse" — assert the tiled chrome
    // is gone and STAYS gone across the beat window + settle.
    await expect(page.locator('.workspace__chrome')).toHaveCount(0, { timeout: 3000 })
    await expect(page.locator('.shell__view--paned')).toHaveCount(0)
    await page.waitForTimeout(400) // past BUILDER_EXIT_MS (250) + margin
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1)

    // Wedge sequence: re-enter, then exit -> re-enter -> exit with sub-beat gaps.
    // At no sampled moment may the two deal classes co-exist (mutual exclusion),
    // and it must always settle collapsed.
    await brand.focus(); await page.keyboard.press('Shift+Enter') // enter builder
    await waitTiled(page)
    let sawBothClasses = false
    const sampleBoth = async () => {
      if (await page.locator('.shell--builder-exiting.shell--builder-entering').count() > 0) sawBothClasses = true
    }
    await brand.focus(); await page.keyboard.press('Shift+Enter') // exit1
    await page.waitForTimeout(90); await sampleBoth()
    await brand.focus(); await page.keyboard.press('Shift+Enter') // re-enter within the exit beat
    await page.waitForTimeout(20); await sampleBoth()
    await page.waitForTimeout(60); await sampleBoth()
    await brand.focus(); await page.keyboard.press('Shift+Enter') // exit2
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    await expect(page.locator('.shell__view--paned')).toHaveCount(0)
    expect(sawBothClasses, 'the exit and enter deal classes are mutually exclusive').toBe(false)
  })

  // DRAG IS BUILDING (point 15): a single-mode drag unfolds the parked layout LIVE
  // and a drop commits builder mode; the former drag-deny is gone.
  test('single-mode drag → drop commits builder mode; ONE undo reverts tree + mode', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmDragA')
    const b = await createTaggedChat(page, 'vmDragB')
    const c = await createTaggedChat(page, 'vmDragC') // in the drawer, not open
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, paneModel.setViewMode(twoChatPanes(a.id, b.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    const baseline = await readWs(page)
    expect(baseline.viewMode).toBe('single')
    expect(Object.keys(baseline.panes).length).toBe(2)

    // DRAG IS BUILDING (point 15): a single-mode drop commits builder mode — and
    // this works from BOTH the modal drawer (mobile) and the persistent desktop
    // sidebar (this WIDE viewport). ensureNavigationOpen covers either; a drawer/
    // sidebar row dragged onto a PANE INTERIOR commits.
    await ensureNavigationOpen(page)
    const content = await page.locator('.shell__content').boundingBox()
    const row = page.locator(`.drawer__item[data-drag-key="chat:${c.id}"]`)
    await expect(row).toBeVisible()
    // Drop into a pane INTERIOR, not the workspace center: the single-mode preview
    // unfolds the parked TWO-pane layout, so content.width/2 is the inter-pane
    // divider (a resize handle, not a drop zone) where a drop no-ops. 0.75 lands
    // unambiguously inside the right pane's join zone.
    await mouseDrag(page, row, content.x + content.width * 0.75, content.y + content.height / 2)

    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000, message: 'a single-mode drop commits builder mode',
    }).toBe('panes')
    expect(whichPaneHas(await readWs(page), `chat:${c.id}`), 'the dragged chat landed').toBeTruthy()

    // ONE undo reverts BOTH the drop and the mode back to single (restoreViewMode).
    await page.keyboard.press('Control+z')
    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000, message: 'undo restores single-screen mode',
    }).toBe('single')
    expect(whichPaneHas(await readWs(page), `chat:${c.id}`), 'the drop is undone').toBe(null)
  })

  test('single-mode drag → Escape cancels: back to single, no mutation, no residue', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmCancA')
    const b = await createTaggedChat(page, 'vmCancB')
    const c = await createTaggedChat(page, 'vmCancC')
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, paneModel.setViewMode(twoChatPanes(a.id, b.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    const baseline = await readWs(page)

    await ensureNavigationOpen(page)
    const content = await page.locator('.shell__content').boundingBox()
    const row = page.locator(`.drawer__item[data-drag-key="chat:${c.id}"]`)
    await expect(row).toBeVisible()
    // Arm + move the drag (builder unfolds), then Escape to cancel before dropping.
    await mouseDrag(page, row, content.x + content.width / 2, content.y + content.height / 2, { release: false })
    await page.keyboard.press('Escape')
    await page.mouse.up()

    // The builder world was a preview, not a commitment: mode back to single, tree
    // untouched, the dragged chat never landed.
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    const after = await readWs(page)
    expect(after.viewMode).toBe('single')
    expect(after.layout).toEqual(baseline.layout)
    expect(whichPaneHas(after, `chat:${c.id}`), 'the cancelled drag left no residue').toBe(null)
  })

  // Regression (item 0): the render-only builder preview must reconcile on a
  // foreground return so an interrupted drag can't wedge the workspace tiled forever
  // (the owner's "permanent stuck-tiled after an interrupted touch drag"). The
  // preview leaves the reducer viewMode 'single', so it is asserted via the RENDER
  // (tiled chrome), not readWs.viewMode.
  test('(item 0) an interrupted drag preview reconciles on foreground return, never wedging tiled', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'strandA')
    const b = await createTaggedChat(page, 'strandB')
    const c = await createTaggedChat(page, 'strandC')
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, paneModel.setViewMode(twoChatPanes(a.id, b.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    expect((await readWs(page)).viewMode).toBe('single')

    await ensureNavigationOpen(page)
    const content = await page.locator('.shell__content').boundingBox()
    const row = page.locator(`.drawer__item[data-drag-key="chat:${c.id}"]`)
    await expect(row).toBeVisible()
    // Arm the single-mode drag → the builder preview unfolds (render-only, tiled).
    // Leave the pointer DOWN (no drop): the dragPreviewBuilder override is live.
    await mouseDrag(page, row, content.x + content.width / 2, content.y + content.height / 2, { release: false })
    await expect(page.locator('.workspace__chrome')).toHaveCount(1, { timeout: 3000 })

    // Negative: with NO foreground event, the live-drag preview persists (the
    // reconcile is edge-triggered, not a poll — it must not cancel an in-progress drag).
    await expect(page.locator('.workspace__chrome')).toHaveCount(1)

    // Now the tab returns to the foreground with the session still standing (its
    // going-out teardown was skipped — the strand). The foreground reconcile must
    // force it down so the preview cannot wedge the workspace tiled.
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow')))
    await expect(page.locator('.workspace__chrome')).toHaveCount(0, { timeout: 3000 })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1)
    // The reducer never left single and nothing committed — no residue.
    expect((await readWs(page)).viewMode).toBe('single')
    // Release the (now-orphaned) pointer so the test ends clean; the session was
    // already torn down, so this is a no-op.
    await page.mouse.up()
  })

  // Regression (item 0): the splits KILL SWITCH must collapse a persisted 'panes'
  // blob to single on restore. The tiled render is flag-independent but both exit
  // controls are flag-gated, so a rolled-back client (splits off, panes blob) would
  // otherwise restore TILED with no way to reach single — un-exitable, across reload.
  test('(item 0) splits kill-switch collapses a persisted panes blob to single, never un-exitable tiled', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'killA')
    const b = await createTaggedChat(page, 'killB')
    await mockApps(page, [])
    const blob = paneModel.serializeWorkspace(twoChatPanes(a.id, b.id)) // viewMode 'panes'
    await page.addInitScript((wsBlob) => {
      try {
        localStorage.setItem('mobius:workspace-splits', '0') // KILL SWITCH OFF
        sessionStorage.setItem('mobius-workspace', wsBlob)
      } catch { /* private mode */ }
    }, blob)
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    // Restored SINGLE despite the persisted 'panes' viewMode: no tiled chrome/panes.
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    await expect(page.locator('.shell__view--paned')).toHaveCount(0)
    expect((await readWs(page)).viewMode).toBe('single')
    // The logo carries no builder state either (there is no builder mode with splits off).
    await expect(page.getByRole('button', { name: 'Toggle navigation' })).not.toHaveClass(/shell__brand--builder/)
  })

  // single-mode + ONE leaf: dragging stays enabled; the drop's shape decides
  // split-vs-join, but ANY drop commits builder mode (point 15).
  function singleLeafTwoTabs(a, b) {
    let ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: a }, { kind: 'chat', id: b }])
    return paneModel.setViewMode(paneModel.focusPane(ws, 'p0'), 'single')
  }

  test('single-leaf: an EDGE drop splits AND flips to panes', async ({ page }) => {
    // Two-worlds: single mode renders NO strip (the strip is builder chrome), so
    // the single-mode drag source is the DRAWER row — same as the join test
    // below. An edge-band drop must split into two panes AND commit builder.
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmFlipA')
    const b = await createTaggedChat(page, 'vmFlipB')
    const c = await createTaggedChat(page, 'vmFlipC') // in the drawer, not open
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, singleLeafTwoTabs(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    // No strip in single mode — the world contract this spec used to violate.
    await expect(page.locator('.shell__tabstrip')).toHaveCount(0)
    expect((await readWs(page)).viewMode).toBe('single')

    await ensureNavigationOpen(page)
    const content = await page.locator('.shell__content').boundingBox()
    const row = page.locator(`.drawer__item[data-drag-key="chat:${c.id}"]`)
    await expect(row).toBeVisible()
    // Drag the drawer row into the content's right edge band → split.
    await mouseDrag(page, row, content.x + content.width - 18, content.y + content.height / 2)
    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000, message: 'the single-leaf edge drop flipped to panes',
    }).toBe('panes')
    expect(Object.keys((await readWs(page)).panes).length, 'the edge drop split into two panes').toBe(2)
  })

  test('single-leaf: a CENTER (join) drop is a JOIN (one pane) but still commits builder', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmJoinA')
    const b = await createTaggedChat(page, 'vmJoinB')
    const c = await createTaggedChat(page, 'vmJoinC') // sits in the drawer, not open
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, singleLeafTwoTabs(a.id, b.id))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })

    // Drag a NOT-yet-open drawer row into the pane CENTER → join p0 as a tab (no split).
    await ensureNavigationOpen(page)
    const content = await page.locator('.shell__content').boundingBox()
    const row = page.locator(`.drawer__item[data-drag-key="chat:${c.id}"]`)
    await expect(row).toBeVisible()
    await mouseDrag(page, row, content.x + content.width / 2, content.y + content.height / 2)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'C joined the single pane as a tab' },
    ).toBe('p0')
    const after = await readWs(page)
    expect(Object.keys(after.panes).length, 'still one pane (a join, not a split)').toBe(1)
    // Point 15: a JOIN is not a split, but ANY single-mode drop still commits builder.
    expect(after.viewMode, 'dragging is building — the drop commits panes').toBe('panes')
  })

  // Item 3: the owner's phone bug — entering builder with ONE leaf changed nothing
  // but the logo (the tiled chrome needs 2 panes). The single-pane strip is the
  // builder SURFACE (and the phone drag source), so it must appear on entry even
  // at a single leaf. Reproduced on a PHONE viewport with the real "one chat, strip
  // never engaged" state (empty legacy open-tabs).
  test('phone: builder single-leaf shows the strip; single-screen hides it', async ({ page }) => {
    await boot(page, PHONE)
    const a = await createTaggedChat(page, 'vmPhoneStrip')
    await mockApps(page, [])
    await seedFallbackSingleLeaf(page)
    // The ?chat= deep-link RESET_FLATs the empty-legacy boot into builder ('panes')
    // with a single leaf and tabStripEngaged=false — the exact case the owner hit.
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('panes')

    // The fix: builder single-leaf shows the strip (the builder surface + drag
    // source), where it used to change nothing but the logo. Poll past the entry deal.
    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    await expect(brand).toHaveClass(/shell__brand--builder/)
    await expect(page.locator('.shell__tabstrip')).toBeVisible({ timeout: 4000 })
    await expect(page.locator(`[data-pane-strip="p0"] [data-drag-key="chat:${a.id}"]`)).toHaveCount(1)

    // Toggle to single-screen: the strip retires to today's no-strip look (an
    // unengaged single leaf shows nothing outside builder).
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    await expect(brand).not.toHaveClass(/shell__brand--builder/)
    await expect(page.locator('.shell__tabstrip')).toHaveCount(0, { timeout: 4000 })

    // Back to builder: the strip deals in again.
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('panes')
    await expect(page.locator('.shell__tabstrip')).toBeVisible({ timeout: 4000 })
  })
})

// ── Builder-mode Settings (Settings-as-tab, design steps 3-4-7) ─────────────
test.describe('Builder-mode Settings', () => {
  // Open Settings from the navigation (the Settings row → navTo('settings')).
  // ensureNavigationOpen covers BOTH the mobile modal drawer and the persistent
  // desktop sidebar (WIDE viewport), so the Settings row is reachable either way.
  async function openSettingsFromDrawer(page) {
    await ensureNavigationOpen(page)
    await page.locator('button[aria-label="Settings"]').click()
  }

  test('builder mode: Settings opens as a pane TAB and the panes stay visible', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'stTabA')
    const b = await createTaggedChat(page, 'stTabB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id)) // 'panes' = builder mode, two panes
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    await openSettingsFromDrawer(page)

    // The workspace blob now holds the canonical Settings tab (single-instance).
    await expect.poll(
      async () => whichPaneHas(await readWs(page), 'settings:settings'),
      { timeout: 3000, message: 'the blob contains the settings:settings tab' },
    ).toBeTruthy()

    // The named risk, refuted end-to-end: sibling panes are NOT hidden behind
    // Settings. The tiled chrome is still up and the sibling chat pane renders.
    await expect(page.locator('.workspace__chrome')).toHaveCount(1)
    await expect(page.locator(`[data-tab-key="chat:${b.id}"]`)).toHaveCount(1)
    // Settings renders as a PANED wrapper (its pane rect), not the full-bleed overlay.
    await expect(page.locator('[data-tab-key="settings:settings"].shell__view--paned')).toHaveCount(1)
    await expect(page.locator('.settings')).toBeVisible()
  })

  test('single mode: Settings is the full-screen takeover — no tab, panes hidden', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'stTakeA')
    const b = await createTaggedChat(page, 'stTakeB')
    await mockApps(page, [])
    await seedWorkspace(page, paneModel.setViewMode(twoChatPanes(a.id, b.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })

    await openSettingsFromDrawer(page)

    // Today's takeover overlay: Settings full-bleed, no chrome, and NO settings tab.
    await expect(page.locator('.shell__settings-view.shell__view--active')).toHaveCount(1, { timeout: 3000 })
    await expect(page.locator('.settings')).toBeVisible()
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    const ws = await readWs(page)
    expect(whichPaneHas(ws, 'settings:settings'), 'no Settings tab in single mode').toBe(null)
    // The preserved two-pane tree is untouched behind the overlay.
    expect(Object.keys(ws.panes).length).toBe(2)
  })

  test('the Settings tab SURVIVES the world flip; single shows its own world, never a forced takeover', async ({ page }) => {
    // Two-worlds semantics (owner: "Settings survives world flips"): flipping to
    // single must NOT delete the builder's Settings tab, and single must show the
    // SINGLE world's own screen — the slot, which never holds Settings — rather
    // than dragging the builder's focused surface along as a takeover. The old
    // destructive tab<->takeover conversion this spec used to lock is exactly the
    // behavior the two-worlds rebuild removed (convertSettings is a no-op now).
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'stConvA')
    const b = await createTaggedChat(page, 'stConvB')
    await mockApps(page, [])
    await seedWorkspace(page, twoChatPanes(a.id, b.id)) // builder
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await waitTiled(page)

    await openSettingsFromDrawer(page)
    await expect.poll(
      async () => whichPaneHas(await readWs(page), 'settings:settings'),
      { timeout: 3000 },
    ).toBeTruthy()
    const settingsPane = whichPaneHas(await readWs(page), 'settings:settings')

    // Flip to single via the keyboard path (Shift+Enter on the focused logo).
    await page.getByRole('button', { name: 'Toggle navigation' }).focus()
    await page.keyboard.press('Shift+Enter')

    // Entering single: the Settings tab STAYS in the preserved tree (identity pinned
    // to its original pane). The slot seed skips a Settings-focused pane (Settings never
    // occupies the slot); the flip lands on the first-class New Chat landing (round 4
    // item 3) — the reusable active empty chat is materialized after the beat — so the
    // single world shows its OWN empty New-Chat screen, never a Settings takeover and
    // never the freshest OTHER transcript.
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('single')
    // The New Chat empty surface (What's on your mind?) shows, not a Settings takeover.
    // Scope to the VISIBLE full-bleed surface — the preserved builder chat panes sit
    // mounted-but-hidden and also carry an empty title.
    await expect(page.locator('.shell__view--active .chat__empty-title')).toBeVisible({ timeout: 3000 })
    const single = await readWs(page)
    expect(whichPaneHas(single, 'settings:settings'), 'Settings tab survives entering single').toBe(settingsPane)
    // The empty single world is the New Chat landing — a null slot before the row
    // materializes, or a chat home after — NEVER Settings.
    expect(single.singleScreen == null || single.singleScreen?.kind === 'chat',
      'the empty single world is the New Chat landing, never Settings').toBe(true)
    await expect(page.locator('.shell__settings-view.shell__view--active')).toHaveCount(0)
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)

    // Flip back to builder: the untouched tree returns, Settings tab still in
    // its ORIGINAL pane and still the focused pane's active surface.
    await page.getByRole('button', { name: 'Toggle navigation' }).focus()
    await page.keyboard.press('Shift+Enter')
    await expect.poll(async () => (await readWs(page)).viewMode, { timeout: 3000 }).toBe('panes')
    const back = await readWs(page)
    expect(whichPaneHas(back, 'settings:settings'), 'Settings tab intact after the round-trip').toBe(settingsPane)
    // In builder, pane surfaces wear the paned class, not the single-world
    // --active flavor — same locator the takeover-vs-tab spec above uses.
    await expect(page.locator('[data-tab-key="settings:settings"].shell__view--paned')).toHaveCount(1)
    await expect(page.locator('.settings')).toBeVisible()
  })
})

// ── Logo activation gesture + middle-click close (design items 3, 9) ─────────
test.describe('Logo activation + middle-click', () => {
  function oneChat(id) {
    return paneModel.seedFromFlatTabs([{ kind: 'chat', id }])
  }

  test('a HOLD (~450ms) on the logo flips the mode; navigation is untouched', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'holdA')
    await mockApps(page, [])
    await seedWorkspace(page, paneModel.setViewMode(oneChat(a.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    expect((await readWs(page)).viewMode).toBe('single')

    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    const navBefore = await brand.getAttribute('aria-expanded')
    const box = await page.locator('.shell__brand').boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.waitForTimeout(650) // hold past the ~450ms threshold (rAF completes it)
    await page.mouse.up()

    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000, message: 'a completed hold flips to builder mode',
    }).toBe('panes')
    // The completed hold consumed the click — navigation state is unchanged (no
    // modal drawer opened, no persistent sidebar toggled).
    await expect(brand).toHaveAttribute('aria-expanded', navBefore ?? 'false')
  })

  test('a short TAP on the logo does the nav action (not a mode flip)', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'tapA')
    await mockApps(page, [])
    await seedWorkspace(page, paneModel.setViewMode(oneChat(a.id), 'single'))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })

    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    const navBefore = await brand.getAttribute('aria-expanded')
    await page.locator('.shell__brand').click() // a fast tap
    // The tap does its per-platform nav action (toggle the desktop sidebar / open
    // the mobile drawer) — nav state flips — but NEVER flips the view mode.
    await expect(brand).not.toHaveAttribute('aria-expanded', navBefore ?? 'false')
    expect((await readWs(page)).viewMode, 'a tap does not flip the mode').toBe('single')
  })

  test('middle-click on a strip tab closes it (shared close path)', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'midA')
    const b = await createTaggedChat(page, 'midB')
    await mockApps(page, [])
    // A single-pane workspace with two tabs renders the top strip.
    await seedWorkspace(page, paneModel.seedFromFlatTabs([
      { kind: 'chat', id: a.id }, { kind: 'chat', id: b.id },
    ]))
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__tabstrip')).toBeVisible({ timeout: 8000 })
    expect(whichPaneHas(await readWs(page), `chat:${b.id}`)).toBe('p0')

    // Middle-click tab b's open button → closes it via the SAME path as the ✕.
    await page.locator(`[data-pane-strip="p0"] [data-drag-key="chat:${b.id}"]`)
      .click({ button: 'middle' })
    await expect.poll(async () => whichPaneHas(await readWs(page), `chat:${b.id}`), {
      timeout: 3000, message: 'middle-click closed the tab',
    }).toBe(null)
  })
})
