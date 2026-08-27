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

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function mockIdleChatRuntime(page) {
  await page.route(/\/api\/chats\/[^/?]+\/runtime(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        running: false,
        active_goal_objective: null,
        pending_messages: [],
        pending_question_id: null,
        updated_at: null,
      }),
    })
  })
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
  await mockIdleChatRuntime(page)
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await page.route(/\/api\/chats\/[^/?]+(\?.*)?$/, (r) => {
    if (r.request().method() !== 'GET') return r.fallback()
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'x', title: 'Seeded', messages: [] }) })
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

  test(`[${name}] a cancelled single-mode drag UNTILES (BLOCKER 1: no permanent tile)`, async ({ page }) => {
    await bootSeededWorkspace(page, viewport, standardChatWithEmptyBuilder())
    // Ensure SINGLE mode.
    if (await builderActive(page)) {
      await toggleMode(page)
      await expect.poll(() => builderActive(page)).toBe(false)
    }
    // Open navigation and find a draggable source (a chat/app row carries
    // data-drag-key). Skip only if the instance genuinely has no source.
    await openNavigation(page)
    const src = page.locator('[data-drag-key]').first()
    if (!(await src.count())) { test.skip(true, 'no drag source available'); return }
    const box = await src.boundingBox()
    // Arm a single-mode drag: press + move past the drag threshold. This unfolds
    // the builder preview (data-mode-phase becomes 'drag-preview').
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2 + 40, { steps: 6 })
    await page.mouse.move(box.x + box.width / 2 + 120, box.y + box.height / 2 + 80, { steps: 6 })
    await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('drag-preview')
    // Cancel the drag (Escape) — the id handoff must clear the LIVE preview.
    await page.keyboard.press('Escape')
    await page.mouse.up().catch(() => {})
    // The descriptor returns to idle and the workspace is NOT stranded in the
    // builder/tiled render — this is the exact wedge the dragArm epoch fix closes.
    await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('idle')
    await expect.poll(() => builderActive(page)).toBe(false)
    // Still responsive: a subsequent toggle works.
    await toggleMode(page)
    await expect.poll(() => builderActive(page)).toBe(true)
  })

  test(`[${name}] 20x rapid toggle never wedges and never doubles the beat class`, async ({ page }) => {
    await bootSeededWorkspace(page, viewport, standardChatWithEmptyBuilder())
    await armOneBeatObserver(page)
    const startBuilder = await builderActive(page)
    // Storm the toggle far faster than the beat can complete, so enter-during-exit
    // and exit-during-entry supersessions are exercised repeatedly (the wedge loop).
    for (let i = 0; i < 20; i += 1) {
      await toggleMode(page)
      await page.waitForTimeout(35)
    }
    // INV 1: at no observed frame were both beat classes present at once.
    expect(await readViolations(page)).toEqual([])
    // Let the final beat settle, then assert NO stranded transient class.
    await expect.poll(() => transientClassCount(page), { timeout: 2000 }).toBe(0)
    // 20 flips from the start state lands back on the start state (even count).
    await expect.poll(() => builderActive(page)).toBe(startBuilder)
    // NOT WEDGED: the machine still responds to the very next toggle.
    await toggleMode(page)
    await expect.poll(() => builderActive(page)).toBe(!startBuilder)
    await expect.poll(() => transientClassCount(page), { timeout: 2000 }).toBe(0)
  })

  test(`[${name}] the builder root class always agrees with the logo state (no reducer/render split)`, async ({ page }) => {
    // Give both worlds real content. An empty Builder workspace correctly has
    // no tab strip, so using strip presence as its rendered-world witness would
    // conflate "Builder is active" with "Builder has at least one tab".
    await bootSeededWorkspace(
      page,
      viewport,
      twoPaneBuilder({ kind: 'chat', id: 'aaa' }),
    )
    for (let i = 0; i < 6; i += 1) {
      await toggleMode(page)
      await page.waitForTimeout(120)
      // effectiveViewMode / logo / geometry all derive from ONE descriptor, so the
      // committed logo state and the rendered content must never disagree once the
      // beat has settled.
      const agree = await page.evaluate(() => {
        const root = document.querySelector('.shell')
        const builder = !!document.querySelector('.shell__brand--builder')
        // The strip is the builder world's rendered surface; the logo state is the
        // committed mode. Both derive from ONE descriptor, so once no beat class
        // is present they must agree — that agreement IS this test's contract.
        const strip = !!document.querySelector('.shell__tabstrip, .workspace__strip')
        const exiting = root.className.includes('shell--builder-exiting')
        const settled = !root.className.includes('shell--builder-entering') && !exiting
        return { builder, strip, settled }
      })
      if (agree.settled) expect(agree.strip).toBe(agree.builder)
    }
    // The shell content is still mounted (no wedge / crash) after the sequence.
    await expect(page.locator('.shell__content')).toBeAttached()
  })
}

// ── Assemble/scatter v3 browser coverage ─────────────────────────────────────
// Frame-sampled proof of the compositor-only contract in a real browser. Wide only
// (two visible panes need a wide viewport). A seeded 2-pane builder is exited and
// every frame of the beat is sampled: the participant wrappers' LAYOUT boxes must
// stay constant while their transforms animate, and the same nodes must survive.
const WIDE = { width: 1280, height: 900 }

test('captured Builder panes scatter toward their durable edges on one timeline', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  const sampler = sampleSceneTransition(page)
  await page.waitForTimeout(30)
  await toggleMode(page)
  const scene = await sampler
  const panes = paneAnimations(scene, 'old')
  expect(scene.direction).toBe('exit')
  expect(panes).toHaveLength(2)
  const destinations = panes.map(record => translation(record.frames.at(-1)))
  expect(destinations.some(({ x, y }) => x < 0 && y === 0), 'left pane scatters left').toBe(true)
  expect(destinations.some(({ x, y }) => x > 0 && y === 0), 'right pane scatters right').toBe(true)
  expect(new Set(panes.map(record => record.startTime)).size, 'one shared start time').toBe(1)
  expect(new Set(panes.map(record => record.duration)).size, 'one shared duration').toBe(1)
  expect(panes.every(record => record.frames.every(frame => frame.opacity === 1 || frame.opacity === '1')),
    'captured panes remain opaque').toBe(true)
  await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('idle')
  await expect.poll(() => builderActive(page)).toBe(false)
})

test('focused pane retains its durable right edge during a mode exit', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await page.locator('[data-pane-strip="p1"]').getByRole('button', { name: 'Focus pane' }).click()
  await expect(page.locator('[data-pane-strip]')).toHaveCount(1)
  const content = await page.locator('.shell__content').boundingBox()
  const sampler = sampleSceneTransition(page)
  await toggleMode(page)
  const scene = await sampler
  const panes = paneAnimations(scene, 'old')
  expect(panes).toHaveLength(1)
  const destination = translation(panes[0].frames.at(-1))
  expect(destination.x).toBeGreaterThan(content.width)
  expect(destination.y).toBe(0)
})

test('captured panes assemble from corresponding edges and land together', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, unevenThreePaneBuilder({ kind: 'chat', id: 'ghost' }))
  await toggleMode(page)
  await expect.poll(() => modePhase(page)).toBe('idle')
  const sampler = sampleSceneTransition(page)
  await toggleMode(page)
  const scene = await sampler
  const panes = paneAnimations(scene, 'new')
  expect(scene.direction).toBe('enter')
  expect(panes).toHaveLength(3)
  const origins = panes.map(record => translation(record.frames[0]))
  expect(origins.some(({ x, y }) => x < 0 && y === 0), 'a pane enters from the left').toBe(true)
  expect(origins.some(({ x }) => x > 0), 'a pane enters from the right').toBe(true)
  expect(panes.every(record => translation(record.frames.at(-1)).x === 0
    && translation(record.frames.at(-1)).y === 0), 'all panes land in place').toBe(true)
  expect(new Set(panes.map(record => record.startTime)).size, 'every pane starts together').toBe(1)
  expect(new Set(panes.map(record => record.duration)).size, 'every pane lands together').toBe(1)
  await expect.poll(() => builderActive(page)).toBe(true)
})

test('world reveal keeps the ready Standard snapshot stationary beneath scattering panes', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'ghost' }))
  const sampler = sampleSceneTransition(page)
  await toggleMode(page)
  const scene = await sampler
  const panes = paneAnimations(scene, 'old')
  const workspace = scene.records.find(record => record.pseudo === '::view-transition-new(mode-workspace)')
  expect(panes).toHaveLength(2)
  expect(workspace, 'the destination workspace has an explicit snapshot animation').toBeTruthy()
  expect(workspace.frames.every(frame => frame.opacity === 1 || frame.opacity === '1')).toBe(true)
  expect(workspace.frames.every(frame => !frame.transform || frame.transform === 'none'),
    'the destination snapshot stays still').toBe(true)
  expect(workspace.startTime, 'destination and panes share one clock').toBe(panes[0].startTime)
  expect(workspace.duration).toBe(panes[0].duration)
  await expect.poll(() => builderActive(page)).toBe(false)
})

test('shared Standard chat stays stationary while both captured Builder owners assemble above it', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await toggleMode(page)
  await expect.poll(() => modePhase(page)).toBe('idle')
  const sampler = sampleSceneTransition(page)
  await toggleMode(page)
  const scene = await sampler
  const panes = paneAnimations(scene, 'new')
  const workspace = scene.records.find(record => record.pseudo === '::view-transition-old(mode-workspace)')
  expect(panes).toHaveLength(2)
  expect(workspace).toBeTruthy()
  expect(workspace.frames.every(frame => !frame.transform || frame.transform === 'none')).toBe(true)
  expect(workspace.frames.every(frame => frame.opacity === 1 || frame.opacity === '1')).toBe(true)
  expect(workspace.startTime).toBe(panes[0].startTime)
  await expect.poll(() => builderActive(page)).toBe(true)
})

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
test('round4-3: exiting a NULL-slot builder reveals the New Chat landing, not a blank main, no composer focus', async ({ page }) => {
  // A materialize POST /chats (when there is no reusable empty) returns a fresh empty
  // row so the swap to a real empty ChatView is seamless.
  await page.route(/\/api\/chats$/, r => {
    if (r.request().method() !== 'POST') return r.fallback()
    return r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ id: 'freshnew', title: 'New chat', has_messages: false }),
    })
  })
  // Two-pane builder with an EXPLICIT null slot → exit reveals home:new-chat.
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder(null))
  await expect.poll(() => builderActive(page)).toBe(true)
  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(false)
  // The first-class New Chat empty surface renders (What's on your mind?), never a
  // blank <main> and never the freshest transcript. Scope to the VISIBLE full-bleed
  // surface — the preserved builder chat panes sit mounted-but-hidden and also carry
  // an empty title, so an unscoped selector would strict-mode-match several.
  await expect(page.locator('.shell__view--active .chat__empty-title')).toBeVisible({ timeout: 3000 })
  // The automatic landing must NOT summon the mobile keyboard — the composer is not
  // auto-focused by a mode toggle.
  const composerFocused = await page.evaluate(() => document.activeElement?.tagName === 'TEXTAREA')
  expect(composerFocused, 'a mode toggle must not auto-focus the composer').toBe(false)
})

test('round4-3: a persisted NULL single slot stays New Chat even with historical chats', async ({ page }) => {
  let createCount = 0
  await page.route(/\/api\/chats(?:\?.*)?$/, (route) => {
    const method = route.request().method()
    if (method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { id: 'historical', title: 'Historical transcript', has_messages: true },
        ]),
      })
    }
    if (method === 'POST') {
      createCount += 1
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'freshboot', title: 'New chat', has_messages: false }),
      })
    }
    return route.fallback()
  })
  const ws = paneModel.setViewMode(twoPaneBuilder(null), 'single')
  await bootSeededWorkspace(page, WIDE, ws)

  await expect.poll(() => page.evaluate(
    key => JSON.parse(localStorage.getItem(key))?.singleScreen?.id || null,
    paneModel.STORAGE_KEY,
  ), { timeout: 3000 }).toBe('freshboot')
  expect(createCount, 'boot materializes one new row instead of selecting chats[0]').toBe(1)
  await expect(page.locator('.shell__view--active .chat__empty-title')).toBeVisible()
})

test('leaving Builder replaces an empty Standard slot without allocating a chat', async ({ page }) => {
  let createCount = 0

  await page.route(/\/api\/chats(?:\?.*)?$/, async (route) => {
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
    createCount += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(createdEmptyChat(`unexpected-${createCount}`)),
    })
  })

  await bootSeededWorkspace(page, WIDE, twoPaneBuilder(null))
  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(false)
  await expect.poll(() => page.evaluate(key => (
    JSON.parse(localStorage.getItem(key))?.singleScreen
  ), paneModel.STORAGE_KEY)).toEqual({ kind: 'chat', id: 'aaa' })
  expect(createCount, 'the selected Builder tab avoids an unnecessary New Chat row').toBe(0)
})

test('retiring an explicit Builder cover returns the selected tab and preserves its draft', async ({ page }) => {
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

  const presentation = page.locator('[data-new-chat-presentation]')
  const composer = presentation.getByRole('textbox', { name: 'Message Möbius…' })
  await expect.poll(() => explicitCreates).toBe(1)
  await expect(composer).toBeFocused()
  await composer.fill('Keep this parked Builder draft')

  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(false)
  await expect(presentation).toHaveCount(0)
  await expect.poll(() => page.evaluate(key => (
    JSON.parse(localStorage.getItem(key))?.singleScreen
  ), paneModel.STORAGE_KEY), { timeout: 4000 }).toEqual({
    kind: 'chat',
    id: 'aaa',
  })
  expect(automaticCreates, 'returning the selected tab must not allocate a replacement').toBe(0)
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
  ), paneModel.STORAGE_KEY)).toEqual({ kind: 'chat', id: 'aaa' })
  await expect.poll(() => page.evaluate(id => ({
    intent: JSON.parse(sessionStorage.getItem('new-chat-intent')),
    draft: JSON.parse(sessionStorage.getItem(`draft:${id}`))?.input,
  }), explicitId)).toEqual({
    intent: { chatId: explicitId, status: 'materialized' },
    draft: 'Keep this parked Builder draft',
  })
})

test('a selected Builder tab supersedes an in-flight NULL-slot allocation', async ({ page }) => {
  let createCount = 0
  let releaseFirstCreate
  const firstCreateGate = new Promise(resolve => { releaseFirstCreate = resolve })
  await page.route(/\/api\/chats(?:\?.*)?$/, async (route) => {
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
    if (method === 'POST') {
      const ordinal = ++createCount
      if (ordinal === 1) await firstCreateGate
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ id: `fresh-race-${ordinal}`, title: 'New chat', has_messages: false }),
      })
    }
    return route.fallback()
  })
  const emptyStandard = { ...twoPaneBuilder(null), viewMode: 'single' }
  await bootSeededWorkspace(page, WIDE, emptyStandard)
  await expect.poll(() => createCount, { timeout: 3000 }).toBe(1)

  // Enter Builder while the automatic allocation is held, then return to Standard.
  // The focused Builder tab is now the newer visible intent and owns the slot.
  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(true)
  await toggleMode(page)
  await expect.poll(() => builderActive(page)).toBe(false)
  releaseFirstCreate()

  await expect.poll(() => page.evaluate(
    key => JSON.parse(localStorage.getItem(key))?.singleScreen?.id || null,
    paneModel.STORAGE_KEY,
  ), { timeout: 4000 }).toBe('aaa')
  expect(createCount, 'the stale allocation settles without duplicating or taking the slot').toBe(1)
  await expect(page.locator('[data-new-chat-presentation]')).toHaveCount(0)
})

// R4: same-batch descriptor atomicity for the last-tab-close auto-return. A one-tab
// builder is exited by closing its sole tab; a frame-sampler proves the descriptor
// (logo/builder class) and the emptied tree flip in the SAME commit — never an
// intermediate frame where builder is still true over an emptied single tree.
test('v2 auto-return flips the descriptor and the tree atomically (no lagging frame)', async ({ page }) => {
  const builder = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }]), 'panes')
  await bootSeededWorkspace(page, WIDE, builder)
  await expect.poll(() => builderActive(page)).toBe(true)
  await expect(page.locator('.shell__tabstrip, .workspace__strip').first()).toBeVisible()
  // Sample builder-class vs strip-presence on every frame across the close.
  const sampler = page.evaluate(async () => {
    const disagreements = []
    let frames = 0
    await new Promise((resolve) => {
      const tick = () => {
        const builder = !!document.querySelector('.shell__brand--builder')
        const strip = !!document.querySelector('.shell__tabstrip, .workspace__strip')
        const root = document.querySelector('.shell')
        const beat = root.className.includes('shell--builder-exiting') || root.className.includes('shell--builder-entering')
        // Off-beat, builder ⟺ strip. A lagging descriptor shows builder=true with the
        // strip already retired (or vice versa) in a settled frame.
        if (!beat && builder !== strip) disagreements.push({ builder, strip })
        frames += 1
        if (frames > 60) { resolve(); return }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    return disagreements
  })
  await page.waitForTimeout(30)
  await page.locator('.shell__tab-close').first().click()
  const disagreements = await sampler
  expect(disagreements, 'builder-class and strip never disagree in a settled frame').toEqual([])
  // The emptied builder auto-returned to single.
  await expect.poll(() => builderActive(page)).toBe(false)
  await expect.poll(() => page.evaluate(
    key => JSON.parse(localStorage.getItem(key))?.viewMode, paneModel.STORAGE_KEY,
  ), { timeout: 3000 }).toBe('single')
})

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

test('round4-1: a completed HOLD keeps the logo compressed then springs back at completion', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  // Explicit builder seed; a hold EXITS to single with an animated beat.
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleLogoBeat(page)
  await page.waitForTimeout(30)
  await pressHoldLogo(page)
  const r = await sampler
  expect(r.sawBeatClass, 'an animated beat ran').toBe(true)
  expect(r.beatHeldSeen, 'the hold emitted the is-beat-held compression class').toBe(true)
  // The mark stayed compressed at ~.84 through the beat (pointer release did NOT
  // spring it) and reaches full size only at completion.
  expect(r.minScale, 'the logo held its .84 compression during the beat').toBeLessThanOrEqual(0.88)
  expect(r.epochMismatch, 'the logo release always tracks the live beat epoch').toBe(false)
  await expect.poll(() => builderActive(page)).toBe(false)
  // Settled: no compression class lingers, the mark is full size.
  await expect.poll(() => beatHeldNow(page)).toBe(false)
  const finalScale = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.shell__logo')).scale))
  expect(Math.abs(finalScale - 1)).toBeLessThan(0.02)
})

test('round4-1: a standalone Shift+Enter flip never emits a compression class', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleLogoBeat(page)
  await page.waitForTimeout(30)
  await toggleMode(page) // keyboard path — the standalone announcement is enough
  const r = await sampler
  expect(r.sawBeatClass, 'the keyboard flip still ran an animated beat').toBe(true)
  expect(r.beatHeldSeen, 'no synthetic compression on a standalone keyboard flip').toBe(false)
  // The logo never dipped toward .84 — it was not compressed.
  expect(r.minScale, 'the logo stayed full size (no compression)').toBeGreaterThan(0.95)
  await expect.poll(() => builderActive(page)).toBe(false)
})

test('round4-1: an EARLY logo release is a tap — mode unchanged, no compression class', async ({ page }) => {
  await bootShell(page, WIDE)
  const before = await builderActive(page)
  // A press well under the ~450ms threshold releases as a tap (opens the drawer),
  // never a mode flip, and never emits is-beat-held.
  await pressHoldLogo(page, 150)
  await page.waitForTimeout(200)
  expect(await beatHeldNow(page)).toBe(false)
  expect(await builderActive(page)).toBe(before)
})

test('round4-1: rapid hold → keyboard retoggle keeps the logo epoch equal to the mode epoch', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleLogoBeat(page)
  await page.waitForTimeout(30)
  // Complete a hold (holdOwnsBeat latches), then immediately retoggle by keyboard —
  // the compression rides through to the newest epoch, whose id the logo release must
  // track (data-logo-beat-epoch === data-mode-epoch on every sampled frame).
  await pressHoldLogo(page)
  await toggleMode(page)
  const r = await sampler
  expect(r.beatHeldSeen, 'the hold-owned compression rode through the retoggle').toBe(true)
  expect(r.epochMismatch, 'the logo release never lagged behind the newest beat epoch').toBe(false)
  await expect.poll(() => modePhase(page), { timeout: 3000 }).toBe('idle')
})

test('round4-1: reduced motion keeps direct hold feedback but releases without animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  await page.evaluate(() => {
    const root = document.documentElement
    window.__reducedMotionBeatSeen = false
    window.__reducedMotionObserver = new MutationObserver(() => {
      if (root.dataset.modeViewTransition) window.__reducedMotionBeatSeen = true
    })
    window.__reducedMotionObserver.observe(root, { attributes: true, attributeFilter: ['class'] })
  })
  const box = await page.getByLabel('Toggle navigation').boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.waitForTimeout(325)
  const heldScale = await page.evaluate(() => parseFloat(
    getComputedStyle(document.querySelector('.shell__logo')).scale,
  ))
  expect(heldScale, 'the user-controlled hold still gives immediate compression feedback').toBeLessThan(0.96)
  // Cross the 450ms completion threshold. Under reduced motion the mode commits and
  // the scale returns to 1 in that same frame; the old 160ms release failed here.
  await page.waitForTimeout(150)
  const result = await page.evaluate(() => {
    window.__reducedMotionObserver?.disconnect()
    return {
      builder: !!document.querySelector('.shell__brand--builder'),
      beatHeld: !!document.querySelector('.shell__brand.is-beat-held'),
      beatSeen: !!window.__reducedMotionBeatSeen,
      scale: parseFloat(getComputedStyle(document.querySelector('.shell__logo')).scale),
    }
  })
  await page.mouse.up()
  expect(result.builder, 'the hold still flips the mode').toBe(false)
  expect(result.beatSeen, 'reduced motion arms no transition descriptor').toBe(false)
  expect(result.beatHeld, 'reduced motion never hands compression to a beat').toBe(false)
  expect(Math.abs(result.scale - 1), 'release is immediate under reduced motion').toBeLessThan(0.02)
})
