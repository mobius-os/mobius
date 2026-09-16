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
 *   (d) close-only tab actions remain available while model split caps hold;
 *   (e) a projection flip to phone preserves the persisted tree and pane focus;
 *
 * A 2-pane workspace blob is seeded in localStorage before the shell boots,
 * exactly like tabs.spec seeds the canonical workspace. Agent + apps routes are
 * intercepted so no agent tokens are consumed.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/workspace-panes.spec.mjs --project=tests
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { mockAcceptedMessages } from './_mockAcceptedMessages.mjs'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'
import { PRESS_MENU_HOLD_MS } from '../frontend/src/components/Shell/dragController.js'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const DESKTOP_SIDEBAR_STORAGE_KEY = 'mobius:desktop-sidebar-open:v1'
const STREAM_ROUTE = /\/api\/chats\/[0-9a-f-]+\/stream$/
const WIDE = { width: 1400, height: 900 }
const PHONE = { width: 412, height: 760 }

test.use({ serviceWorkers: 'block' })
attachCleanup()

function builderSeed(tabs) {
  return paneModel.setViewMode(paneModel.seedFromFlatTabs(tabs), 'panes')
}

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
  await mockAcceptedMessages(page)
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await replaceStreamRoute(page, EMPTY_STREAM)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    undefined, { timeout: 10000 })
}

async function ensureNavigationOpen(page) {
  const toggle = page.getByLabel('Toggle navigation')
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  const drawer = page.locator('.drawer.drawer--open')
  await expect(drawer).toBeVisible({ timeout: 3000 })
  // A visible modal drawer may still be translating in from the edge. Gesture
  // geometry belongs to the settled panel the user can deliberately drag from,
  // not an arbitrary animation frame captured between the click and pointerdown.
  await expect.poll(() => drawer.evaluate((element) => getComputedStyle(element).transform), {
    timeout: 3000,
    message: 'navigation panel has reached its open position',
  }).toMatch(/^(none|matrix\(1, 0, 0, 1, 0, 0\))$/)
}

/** Press-and-HOLD the logo past HOLD_MS (450ms) so the rAF completion fires — the
 *  real pointer path (not the deterministic Shift+Enter). The completed hold
 *  suppresses the trailing click, so it never also toggles the drawer. */
async function holdLogo(page, brand) {
  await brand.scrollIntoViewIfNeeded()
  const startedInBuilder = await brand.evaluate(element => (
    element.classList.contains('shell__brand--builder')
  ))
  const box = await brand.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  // Under a loaded CI runner the 450ms hold can complete between pointerdown
  // returning and the first assertion. Accept either owned boundary: the live
  // hold class or the mode flip it commits, then require the completed flip.
  await expect.poll(() => brand.evaluate((element, wasBuilder) => (
    element.classList.contains('is-holding')
      || element.classList.contains('shell__brand--builder') !== wasBuilder
  ), startedInBuilder), { timeout: 3000 }).toBe(true)
  await expect.poll(() => brand.evaluate((element, wasBuilder) => (
    !element.classList.contains('is-holding')
      && element.classList.contains('shell__brand--builder') !== wasBuilder
  ), startedInBuilder), { timeout: 3000 }).toBe(true)
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
      body: '<!doctype html><html><body style="margin:0;min-height:100vh" '
        + 'onclick="window.__clicks += 1">'
        + '<button id="probe">app</button>'
        + '<script>window.__fi = 0; window.__clicks = 0;'
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

/** Seed the canonical workspace before the shell bundle evaluates. */
async function seedWorkspace(page, ws) {
  const blob = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([wsKey, wsBlob]) => {
    try {
      localStorage.setItem(wsKey, wsBlob)
    } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, blob])
}

/** Seed one explicit Builder leaf. Fresh workspaces intentionally start in
 *  Standard, so this fixture owns the Builder state directly. */
async function seedBuilderSingleLeaf(page, chatId) {
  const ws = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([{ kind: 'chat', id: chatId }]),
    'panes',
  )
  const blob = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([workspaceKey, workspaceBlob]) => {
    try {
      localStorage.setItem(workspaceKey, workspaceBlob)
    } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, blob])
}

/** Two chat panes side by side: p0 = chatA (focused), p1 = chatB. */
function twoChatPanes(chatA, chatB) {
  let ws = builderSeed([
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

/** Sample the first three pre-paint frames after toggling the persistent drawer.
 *  A correct atomic geometry commit keeps the projected pane area aligned with
 *  the content box in every sample. The old two-phase path exposed one frame
 *  where the content box had moved but the panes still used its previous width. */
async function sampleDesktopDrawerToggle(page) {
  return page.evaluate(async () => {
    const toggle = document.querySelector('button[aria-label="Toggle navigation"]')
    const read = () => {
      const content = document.querySelector('.shell__content')?.getBoundingClientRect()
      const panes = [...document.querySelectorAll('.shell__view--paned')]
        .map(el => el.getBoundingClientRect())
        .sort((a, b) => a.left - b.left)
      if (!content || panes.length < 2) return null
      return {
        contentLeft: content.left,
        contentRight: content.right,
        firstPaneLeft: panes[0].left,
        lastPaneRight: panes[panes.length - 1].right,
      }
    }
    toggle?.click()
    const frames = []
    for (let i = 0; i < 3; i += 1) {
      await new Promise(requestAnimationFrame)
      frames.push(read())
    }
    return frames
  })
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

/** Move the active tab to the other pane through the existing drag contract. */
async function moveActiveTabToOtherPane(page, paneId) {
  const source = page.locator(
    `[data-pane-strip="${paneId}"] .shell__tab--active .shell__tab-open`,
  )
  const target = await page.locator(
    `[data-pane-strip]:not([data-pane-strip="${paneId}"]) .shell__tab--active .shell__tab-open`,
  ).first().boundingBox()
  expect(target, 'the destination pane has an active tab').not.toBeNull()
  await mouseDrag(page, source, target.x + target.width / 2, target.y + target.height / 2)
}

test.describe('Workspace panes (PR2 gate)', () => {


  test('(a) the divider follows physical drag travel without moving a pinned message', async ({ page }) => {
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
    const dividerCenterBefore = box.x + box.width / 2
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 140, box.y + box.height / 2, { steps: 6 })
    await page.mouse.up()
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))))

    const after = await readTop()
    const movedBox = await page.locator('.workspace__divider').boundingBox()
    const dividerCenterAfter = movedBox.x + movedBox.width / 2
    expect(Math.abs(dividerCenterAfter - dividerCenterBefore - 140)).toBeLessThanOrEqual(1)
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
    await moveActiveTabToOtherPane(page, 'p0')
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



  test('(c) an app iframe survives a cross-pane move with no second frame-init', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'wpAppChatA')
    const b = await createTaggedChat(page, 'wpAppChatB')
    const APP_ID = 990101
    await mockApps(page, [{ id: APP_ID, name: 'Pane App', chatId: a.id }])

    // p0 = [chatA, app] (app active), p1 = [chatB]. Moving the app to p1 keeps
    // both panes (chatA survives in p0), so it is a true cross-pane move.
    let ws = builderSeed([
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
    await appFrame.waitForFunction(() => typeof window.__fi === 'number', undefined, { timeout: 4000 })
    const initsBefore = await appFrame.evaluate(() => window.__fi)

    // Exactly one iframe wrapper for the app before the move.
    await expect(page.locator(`[data-tab-key="app:${APP_ID}"]`)).toHaveCount(1)

    // Move the app tab from p0 to p1 through the existing drag contract.
    await moveActiveTabToOtherPane(page, 'p0')
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
    const beforeBlob = await page.evaluate(k => localStorage.getItem(k), paneModel.STORAGE_KEY)
    expect(beforeBlob, 'workspace blob persisted').toBeTruthy()

    // Flip the projection: wide → phone.
    await page.setViewportSize(PHONE)
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 200)))))

    const afterBlob = await page.evaluate(k => localStorage.getItem(k), paneModel.STORAGE_KEY)
    expect(afterBlob, 'the persisted tree is unchanged across the projection flip').toBe(beforeBlob)
    // The tree still parses to two panes (projection changed, tree did not).
    const leaves = await page.evaluate((k) => {
      const ws = JSON.parse(localStorage.getItem(k))
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
      () => page.evaluate((k) => JSON.parse(localStorage.getItem(k)).focusedPaneId,
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
  let ws = builderSeed([
    { kind: 'chat', id: a }, { kind: 'chat', id: b }, { kind: 'chat', id: c },
  ])
  ws = paneModel.moveTab(ws, `chat:${b}`, { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

function twoStackedPanesThreeTabs(a, b, c) {
  let ws = builderSeed([
    { kind: 'chat', id: a }, { kind: 'chat', id: b }, { kind: 'chat', id: c },
  ])
  ws = paneModel.moveTab(ws, `chat:${b}`, { root: true, edge: 'bottom' })
  return paneModel.focusPane(ws, 'p0')
}

function singlePaneThreeTabs(a, b, c) {
  return builderSeed([
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
  return page.evaluate(k => JSON.parse(localStorage.getItem(k)), paneModel.STORAGE_KEY)
}

async function expectCaretAligned(page, caret, target, label) {
  const zoom = await page.evaluate(() => Number(getComputedStyle(document.documentElement).zoom) || 1)
  // Visibility begins as soon as opacity rises above zero, while the preview's
  // left/top/size transition is still moving. Wait for that owned animation
  // boundary, then sample final geometry exactly once: polling coordinates can
  // accidentally accept a transient crossing before the caret drifts onward.
  await caret.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map(animation => animation.finished))
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  })
  const [caretBox, targetGeometry] = await Promise.all([
    caret.boundingBox(),
    target.evaluate((element) => {
      const tabBox = element.closest('.shell__tab').getBoundingClientRect()
      const stripBox = element.closest('[data-pane-strip]').getBoundingClientRect()
      return { tabX: tabBox.x, stripY: stripBox.y }
    }),
  ])
  expect(caretBox.x, `${label} final x matches the measured strip target`)
    .toBeCloseTo(targetGeometry.tabX - zoom, 0)
  expect(caretBox.y, `${label} final y matches the measured strip target`)
    .toBeCloseTo(targetGeometry.stripY + 5 * zoom, 0)
  expect(caretBox.width, `${label} has a real fixed-space width`).toBeGreaterThan(0)
  expect(caretBox.height, `${label} has a real fixed-space height`).toBeGreaterThan(0)
}

/** Press on a source element, arm past slop, glide to a target point, release —
 *  the mouse-path drag Chromium delivers as real pointer events. */
async function mouseDrag(
  page,
  sourceLocator,
  toX,
  toY,
  { release = true, resolveTarget = null } = {},
) {
  await sourceLocator.scrollIntoViewIfNeeded()
  const box = await sourceLocator.boundingBox()
  const sx = box.x + box.width / 2
  const sy = box.y + box.height / 2
  await page.mouse.move(sx, sy)
  await page.mouse.down()
  await page.mouse.move(sx + 10, sy, { steps: 3 }) // clear the 5px slop → arm
  await expect(page.locator('.workspace__drag-chip')).toBeVisible({ timeout: 3000 })
  if (resolveTarget) ({ x: toX, y: toY } = await resolveTarget())
  await page.mouse.move(toX, toY, { steps: 14 })
  await expect(page.locator('.workspace__drop-preview.is-visible'))
    .toBeVisible({ timeout: 3000 })
  if (release) await page.mouse.up()
}

/** A real Chromium touch stream (not synthetic PointerEvents). */
async function touchDrag(
  page,
  sourceLocator,
  toX,
  toY,
  { firstDx = 0, firstDy = 12, holdMs = 0, release = true } = {},
) {
  const box = await sourceLocator.boundingBox()
  const sx = box.x + box.width / 2
  const sy = box.y + box.height / 2
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  const point = (x, y) => [{ x, y, radiusX: 4, radiusY: 4, force: 1, id: 1 }]
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(sx, sy) })
  if (holdMs > 0) await page.waitForTimeout(holdMs)
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
  if (release) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    await cdp.detach()
    return null
  }
  let current = { x: toX, y: toY }
  return {
    async moveTo(x, y) {
      const from = current
      for (let i = 1; i <= 10; i += 1) {
        const t = i / 10
        await cdp.send('Input.dispatchTouchEvent', {
          type: 'touchMove',
          touchPoints: point(from.x + (x - from.x) * t, from.y + (y - from.y) * t),
        })
      }
      current = { x, y }
    },
    async release() {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
      await cdp.detach()
    },
  }
}

async function resolveBuilderContentCenter(page) {
  // Arming a drag from Standard unfolds Builder as a render-only preview.
  // Target geometry therefore belongs to that newly mounted world, not the
  // Standard content box measured before pointerdown. The shell content's
  // geometric center can be the divider between two panes, so use the focused
  // pane strip for the x-axis and the content box for the y-axis.
  await expect(page.locator('.workspace__chrome')).toHaveCount(1, { timeout: 3000 })
  const focusedPaneId = await page.evaluate(
    key => JSON.parse(localStorage.getItem(key)).focusedPaneId,
    paneModel.STORAGE_KEY,
  )
  const [strip, content] = await Promise.all([
    page.locator(`[data-pane-strip="${focusedPaneId}"]`).boundingBox(),
    page.locator('.shell__content').boundingBox(),
  ])
  return { x: strip.x + strip.width / 2, y: content.y + content.height / 2 }
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
  async function bootSingleModeDrawerDrag(page, tag, viewport) {
    await boot(page, viewport)
    const a = await createTaggedChat(page, `${tag}A`)
    const b = await createTaggedChat(page, `${tag}B`)
    const c = await createTaggedChat(page, `${tag}C`)
    await mockApps(page, [])
    await exposeChatsInDrawer(page, [a.id, b.id, c.id])
    await seedWorkspace(page, paneModel.setViewMode(
      paneModel.seedFromFlatTabs([{ kind: 'chat', id: c.id }]),
      'single',
    ))
    await page.goto(`${BASE}/shell/?chat=${c.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    await expect(page.locator('[data-pane-strip="p0"]')).toHaveCount(0)
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
    await ensureNavigationOpen(page)
    return { a, b, c }
  }





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

  test('phone touch-drag moves a tab between stacked panes after a short hold', async ({ page }) => {
    const { c, b } = await bootThreeTab(page, 'touchDrag')
    await page.setViewportSize(PHONE)
    await expect(page.locator('[data-pane-strip="p1"]')).toBeVisible({ timeout: 4000 })
    const target = await page.locator(`[data-tab-key="chat:${b.id}"]`).boundingBox()
    const src = page.locator(`[data-pane-strip="p0"] .shell__tab-open[data-drag-key="chat:${c.id}"]`)
    await touchDrag(page, src, target.x + target.width / 2, target.y + target.height / 2, {
      // Move after the shared 180ms drag stage but before the 400ms stationary
      // menu stage. Waiting through the menu threshold correctly opens actions
      // and therefore must not be used to model this short-hold drag.
      holdMs: 250,
    })
    await expect.poll(
      async () => whichPaneHas(await readWs(page), `chat:${c.id}`),
      { timeout: 3000, message: 'the real touch stream moved C into the lower pane' },
    ).toBe('p1')
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




  test('closing the final legacy Builder tab returns to a visible Standard chat', async ({ page }) => {
    await boot(page, WIDE)
    const current = await createTaggedChat(page, 'vmFinalClose')
    await mockApps(page, [])
    await seedWorkspace(page, builderSeed([{ kind: 'chat', id: current.id }]))
    await page.goto(`${BASE}/shell/?chat=${current.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.shell__chat-view.shell__view--active')).toHaveCount(1, { timeout: 8000 })
    expect('singleScreen' in await readWs(page), 'the fixture reproduces the legacy missing slot').toBe(false)

    await page.getByRole('button', { name: /^Close .* tab$/ }).click()

    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000,
      message: 'the final close returns to Standard',
    }).toBe('single')
    const after = await readWs(page)
    expect(after.singleScreen).toEqual({ kind: 'chat', id: String(current.id) })
    await expect(page.locator(
      `[data-chat-surface="painted"][data-chat-id="${current.id}"].shell__view--active`,
    )).toHaveCount(1)
  })



  test('a Standard round trip preserves Builder reading ownership and geometry', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'vmReadingA')
    const b = await createTaggedChat(page, 'vmReadingB')
    await mockApps(page, [])

    const token = await page.evaluate(() => localStorage.getItem('token'))
    const now = Date.now()
    const messages = Array.from({ length: 36 }, (_, index) => ({
      role: index % 2 === 0 ? 'user' : 'assistant',
      content: `Workspace reading row ${index}. ${'Stable width-sensitive text. '.repeat(20)}`,
      ts: now + index,
      cid: index % 2 === 0 ? `workspace-reading-${index}` : undefined,
      blocks: index % 2 === 0
        ? []
        : [{
            type: 'text',
            content: `Workspace reading row ${index}. ${'Stable width-sensitive text. '.repeat(20)}`,
          }],
    }))
    const seeded = await page.request.put(`${BASE}/api/chats/${a.id}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { messages },
      failOnStatusCode: false,
    })
    expect(seeded.ok()).toBe(true)

    const builder = twoChatPanes(a.id, b.id)
    await seedWorkspace(page, {
      ...builder,
      viewMode: 'single',
      singleScreen: { kind: 'chat', id: String(a.id) },
    })
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    const standardSurface = page.locator(
      `[data-chat-world="standard"][data-chat-id="${a.id}"]`,
    )
    const builderSurface = page.locator(
      `[data-chat-world="builder"][data-chat-id="${a.id}"]`,
    )
    await expect(standardSurface.locator('.chat__scroll')).toBeVisible({ timeout: 15000 })

    // The parked Builder owner must keep the rect it will paint. Expanding it
    // to Standard's box before its ownership cleanup changes wrapping and makes
    // a lifecycle capture describe content the reader was not looking at.
    const [parkedBuilderWidth, standardWidth] = await Promise.all([
      builderSurface.evaluate(element => element.getBoundingClientRect().width),
      standardSurface.evaluate(element => element.getBoundingClientRect().width),
    ])
    expect(parkedBuilderWidth).toBeLessThan(standardWidth - 100)

    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await waitTiled(page)
    const builderScroll = builderSurface.locator('.chat__scroll')
    await expect(builderScroll).toBeVisible({ timeout: 15000 })

    const previousWriteAt = await page.evaluate(
      id => JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]?.at || 0,
      String(a.id),
    )
    await builderScroll.evaluate((scroll) => {
      scroll.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        pointerType: 'mouse',
      }))
      scroll.scrollTop = Math.floor(scroll.scrollHeight / 3)
      scroll.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true,
        pointerType: 'mouse',
      }))
    })
    await page.waitForFunction(({ id, after }) => (
      (JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]?.at || 0) > after
    ), { id: String(a.id), after: previousWriteAt })

    const readBuilderState = () => page.evaluate((id) => {
      const scroll = document.querySelector(
        `[data-chat-world="builder"][data-chat-id="${id}"] .chat__scroll`,
      )
      const { at: _at, ...saved } =
        JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]
      const wrapper = scroll.closest('[data-chat-world="builder"]')
      return {
        top: scroll.scrollTop,
        saved,
        width: wrapper.getBoundingClientRect().width,
      }
    }, String(a.id))
    const before = await readBuilderState()

    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await expect(standardSurface.locator('.chat__scroll'))
      .toHaveAttribute('data-scroll-mode', 'ANCHOR_AT', { timeout: 15000 })
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)

    const afterExit = await readBuilderState()
    expect(afterExit.saved).toEqual(before.saved)
    expect(Math.abs(afterExit.width - before.width)).toBeLessThanOrEqual(1)

    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await waitTiled(page)
    await expect(builderScroll).toHaveAttribute('data-scroll-mode', 'ANCHOR_AT', {
      timeout: 15000,
    })
    expect(Math.abs((await builderScroll.evaluate(scroll => scroll.scrollTop)) - before.top))
      .toBeLessThanOrEqual(8)
  })

  // Regression (item 0): a genuine MULTI-PANE exit via the real POINTER-HOLD
  // completion path — the path the single-leaf keyboard verification missed — must
  // collapse the tiled workspace and STAY collapsed, and a rapid re-enter within
  // the exit beat must never strand the beat (the old two-latch shape could leave
  // builderExiting true forever → tiled after the mode flipped). Runs with motion
  // ON so the exit reverse-deal beat actually engages (the bug lived in the beat).


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





  // Regression (item 0): the render-only builder preview must reconcile on a
  // foreground return so an interrupted drag can't wedge the workspace tiled forever
  // (the owner's "permanent stuck-tiled after an interrupted touch drag"). The
  // preview leaves the reducer viewMode 'single', so it is asserted via the RENDER
  // (tiled chrome), not readWs.viewMode.


  test('a persisted Builder workspace restores canonically and remains exit-able', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'killA')
    const b = await createTaggedChat(page, 'killB')
    await mockApps(page, [])
    const blob = paneModel.serializeWorkspace(twoChatPanes(a.id, b.id)) // viewMode 'panes'
    await page.addInitScript((wsBlob) => {
      try {
        localStorage.setItem('mobius-workspace', wsBlob)
      } catch { /* private mode */ }
    }, blob)
    await page.goto(`${BASE}/shell/?chat=${a.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.workspace__chrome')).toHaveCount(1, { timeout: 8000 })
    expect((await readWs(page)).viewMode).toBe('panes')
    const brand = page.getByRole('button', { name: 'Toggle navigation' })
    await expect(brand).toHaveClass(/shell__brand--builder/)

    await brand.focus()
    await page.keyboard.press('Shift+Enter')
    await expect.poll(() => readWs(page).then(ws => ws.viewMode)).toBe('single')
    await expect(page.locator('.workspace__chrome')).toHaveCount(0)
  })

  // single-mode + ONE leaf: dragging stays enabled; the drop's shape decides
  // split-vs-join, but ANY drop commits builder mode (point 15).
  function singleLeafTwoTabs(a, b) {
    let ws = builderSeed([{ kind: 'chat', id: a }, { kind: 'chat', id: b }])
    return paneModel.setViewMode(paneModel.focusPane(ws, 'p0'), 'single')
  }





  // Item 3: the owner's phone bug — entering builder with ONE leaf changed nothing
  // but the logo (the tiled chrome needs 2 panes). The single-pane strip is the
  // builder SURFACE (and the phone drag source), so it must appear on entry even
  // at a single leaf. Reproduced on a PHONE viewport with the real "one chat, strip
  // never engaged" state (empty legacy open-tabs).

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
    await holdLogo(page, brand)

    await expect.poll(async () => (await readWs(page)).viewMode, {
      timeout: 3000, message: 'a completed hold flips to builder mode',
    }).toBe('panes')
    // The completed hold consumed the click — navigation state is unchanged (no
    // modal drawer opened, no persistent sidebar toggled).
    await expect(brand).toHaveAttribute('aria-expanded', navBefore ?? 'false')
  })



  test('middle-click on a strip tab closes it (shared close path)', async ({ page }) => {
    await boot(page, WIDE)
    const a = await createTaggedChat(page, 'midA')
    const b = await createTaggedChat(page, 'midB')
    await mockApps(page, [])
    // A single-pane workspace with two tabs renders the top strip.
    await seedWorkspace(page, builderSeed([
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
