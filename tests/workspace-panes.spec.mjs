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
 * authority the PR2 cases read). Touch long-press cannot be expressed with
 * Playwright's mouse-only pointer input, so that case is skipped with a reason;
 * its geometry is covered exhaustively by the dragController unit suite.
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

async function bootThreeTab(page, tag) {
  await boot(page, WIDE)
  const a = await createTaggedChat(page, `${tag}A`)
  const b = await createTaggedChat(page, `${tag}B`)
  const c = await createTaggedChat(page, `${tag}C`)
  await mockApps(page, [])
  await seedWorkspace(page, twoPanesThreeTabs(a.id, b.id, c.id))
  await page.goto(`${BASE}/shell/?chat=${c.id}`, { waitUntil: 'domcontentloaded' })
  await waitTiled(page)
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

  // Touch long-press lift → drag cannot be expressed with Playwright's mouse
  // pointer input (no synthetic touch hold in this harness), and the existing
  // specs simulate only mouse/keyboard. The hold/slop thresholds and the touch
  // escalation (hold→release-in-place = menu) are covered as pure predicates in
  // dragController.test.js; a device path would need real touch events.
  test.skip('touch long-press lifts a tab into a drag', async () => {
    // Intentionally skipped — no touch-hold primitive in the mocked harness.
  })
})
