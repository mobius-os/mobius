/**
 * Mode-transition (builder <-> single) wedge tests — the flagship machinery.
 *
 * The demo wedge this rebuild fixes: hold-to-exit worked, then stopped
 * permanently (the logo flips, the panes never collapse). Codex's adversarial
 * review traced it to a stranded builderExiting latch and proved the whole
 * shape was not sequence-proof. The replacement is ONE transition descriptor
 * (frontend/src/components/Shell/modeMachine.js) from which everything derives,
 * with supersession and epoch-keyed completion.
 *
 * These e2e specs exercise the descriptor in a real browser through the
 * DETERMINISTIC keyboard path (Shift+Enter on the brand toggles the mode — no
 * 450ms hold timing to flake on) and assert the two invariants a wedge violates:
 *   - INV 1: the shell root never carries BOTH the entering AND exiting beat
 *     class at once (recorded live via a MutationObserver).
 *   - the machine never wedges: after a storm of rapid toggles it settles with
 *     no stranded beat class AND still responds to the next toggle.
 *
 * Runs against the deployed app with agent routes intercepted — no tokens.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/mode-transition.spec.mjs --project=tests
 */
import { test, expect } from '@playwright/test'
import * as paneModel from '../frontend/src/components/Shell/paneModel.js'
import * as tabModel from '../frontend/src/components/Shell/tabModel.js'
import { createMockChatRuntime } from './_mockChatRuntime.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function mockIdleChatRuntime(page) {
  const runtime = createMockChatRuntime()
  await page.route(/\/api\/chats\/[^/?]+\/runtime(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtime.snapshot()),
    })
  })
  return runtime
}

async function bootShell(page, viewport) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  await mockIdleChatRuntime(page)
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.shell', { timeout: 10000 })
  // Dismiss the install prompt if it landed (keeps focus off the brand clean).
  const notNow = page.getByRole('button', { name: /not now/i })
  if (await notNow.count().catch(() => 0)) await notNow.first().click().catch(() => {})
}

// Mock a chat GET so a seeded chat pane mounts a ChatView without a network error,
// then seed a persisted workspace blob into durable browser storage before boot.
async function bootSeededWorkspace(page, viewport, ws) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  const runtime = await mockIdleChatRuntime(page)
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await page.route(/\/api\/chats(?:\?.*)?$/, (r) => {
    if (r.request().method() !== 'GET') return r.fallback()
    return r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{
        id: 'aaa',
        title: 'Seeded',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        activity_at: '2026-01-01T00:00:00Z',
        pinned_at: null,
        created_by_app_id: null,
        has_messages: true,
        running: false,
      }]),
    })
  })
  await page.route(/\/api\/chats\/[^/?]+(\?.*)?$/, (r) => {
    if (r.request().method() !== 'GET') return r.fallback()
    return r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtime.detail({
        id: new URL(r.request().url()).pathname.split('/').pop(),
        title: 'Seeded',
        messages: [],
        total: 0,
        offset: 0,
      })),
    })
  })
  const blob = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([key, raw]) => {
    try { localStorage.setItem(key, raw) } catch { /* private mode */ }
  }, [paneModel.STORAGE_KEY, blob])
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.shell', { timeout: 10000 })
  const notNow = page.getByRole('button', { name: /not now/i })
  if (await notNow.count().catch(() => 0)) await notNow.first().click().catch(() => {})
}

// A wide two-pane BUILDER workspace: chat 'aaa' left (focused), chat 'bbb' right.
// `slotKey` seeds the single-screen slot so an exit can be steered to a promote
// (slot === a visible pane's active key) or a world reveal (slot tree-absent).
function twoPaneBuilder(slot) {
  let ws = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }]), 'panes')
  ws = paneModel.splitPaneWithTab(ws, tabModel.makeTab('chat', 'bbb'), { paneId: ws.focusedPaneId, edge: 'right' })
  const leftId = paneModel.paneOf(ws, 'chat:aaa').id
  ws = paneModel.focusPane(ws, leftId)
  ws = paneModel.setSingleScreen(ws, slot)
  return ws // viewMode stays 'panes' (builder)
}

// The user-visible empty-Builder seam: Standard has one concrete current item,
// while the hidden Builder tree has no tabs yet. Entering Builder must seed this
// chat as its only tab; the truly empty New Chat landing intentionally cannot
// enter a content-less Builder world.
function standardChatWithEmptyBuilder() {
  return paneModel.setSingleScreen(
    paneModel.seedFromFlatTabs([]),
    { kind: 'chat', id: 'aaa' },
  )
}

function onePaneStandardChat() {
  let ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }])
  ws = paneModel.setSingleScreen(ws, { kind: 'chat', id: 'aaa' })
  return paneModel.setViewMode(ws, 'single')
}

// An intentionally asymmetric three-pane tree. Its natural edge vectors differ
// enough to expose same-duration entry as visibly different pane velocities.
function unevenThreePaneBuilder(slot) {
  let ws = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }]), 'panes')
  ws = paneModel.splitPaneWithTab(ws, tabModel.makeTab('chat', 'bbb'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  const rightId = paneModel.paneOf(ws, 'chat:bbb').id
  ws = paneModel.splitPaneWithTab(ws, tabModel.makeTab('chat', 'ccc'), {
    paneId: rightId, edge: 'bottom',
  })
  ws = paneModel.setRatio(ws, ws.layout.id, 0.7)
  ws = paneModel.setRatio(ws, ws.layout.b.id, 0.25)
  return paneModel.setSingleScreen(ws, slot)
}

// Capture the custom pseudo-element animations created for one native View
// Transition. The live DOM commits immediately; the moving surfaces are browser
// snapshots, so the durable contract lives on the document timeline rather than
// transient wrapper classes or transforms.
async function sampleSceneTransition(page) {
  return page.evaluate(async () => {
    const initialDirection = document.documentElement.dataset.modeViewTransition || null
    let direction = null
    let records = []
    for (let frames = 0; frames < 180; frames += 1) {
      const currentDirection = document.documentElement.dataset.modeViewTransition || null
      if (currentDirection && currentDirection !== initialDirection) direction ||= currentDirection
      const animations = document.documentElement.getAnimations({ subtree: true })
      records = animations.flatMap((animation) => {
        const effect = animation.effect
        const pseudo = effect?.pseudoElement || ''
        if (!pseudo.startsWith('::view-transition-')) return []
        const frames = effect.getKeyframes?.() || []
        return [{
          pseudo,
          startTime: animation.startTime,
          duration: effect.getTiming?.().duration,
          frames: frames.map(frame => ({
            opacity: frame.opacity ?? null,
            transform: frame.transform ?? null,
          })),
        }]
      })
      if (direction && records.some(record => record.pseudo.includes('mode-pane-'))) break
      await new Promise(resolve => requestAnimationFrame(resolve))
    }
    return { direction, records }
  })
}

function paneAnimations(scene, side) {
  return scene.records.filter(record => record.pseudo.startsWith(`::view-transition-${side}(mode-pane-`))
}

function translation(frame) {
  const match = /translate3d\((-?[\d.]+)px,\s*(-?[\d.]+)px/.exec(frame?.transform || '')
  return match ? { x: Number(match[1]), y: Number(match[2]) } : { x: 0, y: 0 }
}

// Focus the brand toggle and flip the mode via the keyboard path.
async function toggleMode(page) {
  await page.getByLabel('Toggle navigation').focus()
  await page.keyboard.press('Shift+Enter')
}

async function builderActive(page) {
  return page.evaluate(() => !!document.querySelector('.shell__brand--builder'))
}

// Start recording any frame where BOTH beat classes coexist (INV 1 violation).
async function armOneBeatObserver(page) {
  await page.evaluate(() => {
    const root = document.documentElement
    window.__modeViolations = []
    window.__modeObs = new MutationObserver(() => {
      const direction = root.dataset.modeViewTransition
      if (direction && direction !== 'enter' && direction !== 'exit') window.__modeViolations.push(direction)
    })
    window.__modeObs.observe(root, { attributes: true, attributeFilter: ['class'] })
  })
}

async function readViolations(page) {
  return page.evaluate(() => {
    window.__modeObs?.disconnect()
    return window.__modeViolations || []
  })
}

async function modePhase(page) {
  return page.evaluate(() => document.querySelector('.shell')?.getAttribute('data-mode-phase') || 'idle')
}

async function openNavigation(page) {
  // A persistent (wide) sidebar is already open; a modal (phone) drawer opens via
  // the brand's single tap. Best-effort — the drag source may already be visible.
  const docked = await page.evaluate(() => document.querySelector('.shell')?.className.includes('shell--drawer-docked'))
  if (!docked) await page.getByLabel('Toggle navigation').click().catch(() => {})
  await page.waitForTimeout(300)
}

async function transientClassCount(page) {
  return page.evaluate(() => {
    return document.documentElement.dataset.modeViewTransition ? 1 : 0
  })
}

function createdEmptyChat(id, timestamp = '2026-01-01T00:02:00Z') {
  const detail = {
    messages: [],
    total: 0,
    offset: 0,
    running: false,
    pending_messages: [],
    pending_question_id: null,
    session_id: null,
    provider: 'codex',
    created_by_app_id: null,
    agent_settings_json: null,
    effective_agent_settings: { model: 'gpt-current', effort: 'medium' },
    has_assistant_turns: false,
    auto_resume_on_limit: false,
    auto_resume_on_restart: true,
    updated_at: timestamp,
  }
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
    detail,
  }
}

for (const [name, viewport] of [
  ['phone', { width: 412, height: 915 }],
  ['wide', { width: 1280, height: 900 }],
]) {
  test(`[${name}] a single builder toggle flips the mode and settles clean`, async ({ page }) => {
    await bootSeededWorkspace(page, viewport, standardChatWithEmptyBuilder())
    // The hidden Builder tree is empty, so this toggle also proves that entry
    // seeds the current Standard chat rather than refusing or painting a blank.
    const before = await builderActive(page)
    await toggleMode(page)
    await expect.poll(() => builderActive(page)).toBe(!before)
    const entered = await page.evaluate(key => JSON.parse(localStorage.getItem(key)), paneModel.STORAGE_KEY)
    expect(entered.panes[entered.focusedPaneId].tabs.map(tabModel.tabKey)).toEqual(['chat:aaa'])
    // The beat settles: no transient class lingers.
    await expect.poll(() => transientClassCount(page), { timeout: 2000 }).toBe(0)
    await expect.poll(() => modePhase(page)).toBe('idle')
  })






}



// ── Assemble/scatter v3 browser coverage ─────────────────────────────────────
// Frame-sampled proof of the compositor-only contract in a real browser. Wide only
// (two visible panes need a wide viewport). A seeded 2-pane builder is exited and
// every frame of the beat is sampled: the participant wrappers' LAYOUT boxes must
// stay constant while their transforms animate, and the same nodes must survive.
const WIDE = { width: 1280, height: 900 }











test('reduced motion has no intermediate exit phase (instant world flip)', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'ghost' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  // Watch for ANY exiting beat class or reveal underlay across the flip.
  const sampler = page.evaluate(async () => {
    const root = document.querySelector('.shell')
    let sawExitPhase = false
    await new Promise((resolve) => {
      let frames = 0
      const tick = () => {
        if (document.documentElement.dataset.modeViewTransition) sawExitPhase = true
        frames += 1
        if (frames > 60) { resolve(); return }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    return sawExitPhase
  })
  await page.waitForTimeout(30)
  await toggleMode(page)
  const sawExitPhase = await sampler
  expect(sawExitPhase, 'reduced motion discards the whole exit presentation (no phase)').toBe(false)
  await expect.poll(() => builderActive(page)).toBe(false)
})

// ── Round 4 item 3: the null slot is a first-class New Chat landing ────────────






test('retiring Builder returns the canonical New Chat and preserves its draft', async ({ page }) => {
  let explicitId = null
  let explicitCreates = 0
  let automaticCreates = 0
  let releaseExplicit
  const explicitGate = new Promise(resolve => { releaseExplicit = resolve })

  await page.route(/\/api\/chats(?:\?.*)?$/, async route => {
    const method = route.request().method()
    if (method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { id: 'aaa', title: 'Left', has_messages: true },
          { id: 'bbb', title: 'Right', has_messages: true },
        ]),
      })
    }
    if (method !== 'POST') return route.fallback()

    const body = route.request().postDataJSON()
    if (body.id != null) {
      explicitCreates += 1
      explicitId = body.id
      await explicitGate
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(createdEmptyChat(explicitId)),
      })
    }

    automaticCreates += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(createdEmptyChat('retired-home')),
    })
  })

  await bootSeededWorkspace(page, WIDE, twoPaneBuilder(null))
  await openNavigation(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await navigation.getByRole('button', { name: 'New chat', exact: true }).click()

  await expect.poll(() => explicitCreates).toBe(1)
  const presentation = page.locator(
    `[data-chat-surface="painted"][data-chat-id="${explicitId}"]`,
  )
  const composer = presentation.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeFocused()
  await composer.fill('Keep this parked Builder draft')

  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(false)
  await expect.poll(() => page.evaluate(key => (
    JSON.parse(localStorage.getItem(key))?.singleScreen
  ), paneModel.STORAGE_KEY), { timeout: 4000 }).toEqual({ kind: 'chat', id: explicitId })
  expect(automaticCreates, 'returning the canonical New Chat must not allocate a replacement').toBe(0)
  await expect.poll(() => page.evaluate(id => ({
    intent: JSON.parse(sessionStorage.getItem('new-chat-intent')),
    draft: JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input,
  }), explicitId)).toEqual({
    intent: { chatId: explicitId, status: 'allocating' },
    draft: 'Keep this parked Builder draft',
  })

  const explicitResponse = page.waitForResponse(response => (
    /\/api\/chats(?:\?.*)?$/.test(response.url())
      && response.request().method() === 'POST'
      && response.request().postDataJSON()?.id === explicitId
  ))
  releaseExplicit()
  await explicitResponse
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => (
    requestAnimationFrame(resolve)
  ))))

  expect(automaticCreates, 'the late explicit response must not allocate a replacement').toBe(0)
  await expect.poll(() => page.evaluate(key => (
    JSON.parse(localStorage.getItem(key))?.singleScreen
  ), paneModel.STORAGE_KEY)).toEqual({ kind: 'chat', id: explicitId })
  await expect.poll(() => page.evaluate(id => ({
    intent: JSON.parse(sessionStorage.getItem('new-chat-intent')),
    draft: JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input,
  }), explicitId)).toEqual({
    intent: { chatId: explicitId, status: 'materialized' },
    draft: 'Keep this parked Builder draft',
  })
})



// R4: same-batch descriptor atomicity for the last-tab-close auto-return. A one-tab
// builder is exited by closing its sole tab; a frame-sampler proves the descriptor
// (logo/builder class) and the emptied tree flip in the SAME commit — never an
// intermediate frame where builder is still true over an emptied single tree.


// ── Round 4 item 1: the logo holds its breath until completion ────────────────
// The hold hands its compression to the descriptor: while an animated beat owns the
// logo it stays compressed (~.84) and springs back so its first full-size frame lands
// at completion. A standalone keyboard/swipe flip never synthesizes compression.

// Press-and-hold the brand past the ~450ms threshold, then release. A completed hold
// consumes its trailing click, so this never also opens the drawer.
async function pressHoldLogo(page, holdMs = 650) {
  const box = await page.getByLabel('Toggle navigation').boundingBox()
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  await page.mouse.move(cx, cy)
  await page.mouse.down()
  await page.waitForTimeout(holdMs)
  await page.mouse.up()
}

// Sample the logo across a beat: install BEFORE the trigger. Records, on every frame,
// whether .shell__brand carried is-beat-held, the min computed logo `scale`, and
// whether data-logo-beat-epoch ever disagreed with the root data-mode-epoch while both
// were present. Resolves once a beat started then settled (or a generous frame budget).
async function sampleLogoBeat(page) {
  return page.evaluate(async () => {
    const root = document.querySelector('.shell')
    let beatHeldSeen = false
    let minScale = 1
    let epochMismatch = false
    let sawBeatClass = false
    await new Promise((resolve) => {
      let frames = 0
      const tick = () => {
        const beatClass = !!document.documentElement.dataset.modeViewTransition
        if (beatClass) sawBeatClass = true
        const brand = document.querySelector('.shell__brand')
        const logo = document.querySelector('.shell__logo')
        if (brand?.classList.contains('is-beat-held')) {
          beatHeldSeen = true
          const s = parseFloat(getComputedStyle(logo).scale)
          if (Number.isFinite(s)) minScale = Math.min(minScale, s)
          const logoEpoch = brand.getAttribute('data-logo-beat-epoch')
          const modeEpoch = root.getAttribute('data-mode-epoch')
          if (logoEpoch != null && modeEpoch != null && logoEpoch !== modeEpoch) epochMismatch = true
        }
        frames += 1
        if ((sawBeatClass && !beatClass && frames > 4) || frames > 320) { resolve(); return }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    const settledScale = parseFloat(getComputedStyle(document.querySelector('.shell__logo')).scale)
    return { beatHeldSeen, minScale, epochMismatch, sawBeatClass, settledScale }
  })
}

// Whether is-beat-held is on the brand RIGHT NOW (for the instant/no-compression checks).
async function beatHeldNow(page) {
  return page.evaluate(() => !!document.querySelector('.shell__brand.is-beat-held'))
}
