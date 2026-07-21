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

async function bootShell(page, viewport) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.shell', { timeout: 10000 })
  // Dismiss the install prompt if it landed (keeps focus off the brand clean).
  const notNow = page.getByRole('button', { name: /not now/i })
  if (await notNow.count().catch(() => 0)) await notNow.first().click().catch(() => {})
}

// Mock a chat GET so a seeded chat pane mounts a ChatView without a network error,
// then seed a persisted workspace blob into sessionStorage before boot.
async function bootSeededWorkspace(page, viewport, ws) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, r => r.fulfill({ status: 202, body: '{}' }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, r => r.fulfill({ status: 204, body: '' }))
  await page.route('**/api/chat/stop', r => r.fulfill({ status: 200, body: '{}' }))
  await page.route(/\/api\/chats\/[^/?]+(\?.*)?$/, (r) => {
    if (r.request().method() !== 'GET') return r.fallback()
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'x', title: 'Seeded', messages: [] }) })
  })
  const blob = paneModel.serializeWorkspace(ws)
  await page.addInitScript(([key, raw]) => {
    try { sessionStorage.setItem(key, raw); sessionStorage.setItem('mobius-open-tabs', '[]') } catch { /* private mode */ }
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
  let ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }])
  ws = paneModel.splitPaneWithTab(ws, tabModel.makeTab('chat', 'bbb'), { paneId: ws.focusedPaneId, edge: 'right' })
  const leftId = paneModel.paneOf(ws, 'chat:aaa').id
  ws = paneModel.focusPane(ws, leftId)
  ws = paneModel.setSingleScreen(ws, slot)
  return ws // viewMode stays 'panes' (builder)
}

// Frame-sample the exit beat: on every animation frame while .shell--builder-exiting
// is present, record each motion wrapper's LAYOUT box (offset*, transform-independent)
// + its computed transform + a stable node marker. Start this BEFORE triggering the
// toggle so the first exit frame is captured.
async function sampleExitBeat(page) {
  return page.evaluate(async () => {
    const root = document.querySelector('.shell')
    const byKey = new Map() // data-tab-key → { boxes:Set, transforms:Set, node }
    let started = false
    let dualClass = false
    let underlaySeen = false
    await new Promise((resolve) => {
      let frames = 0
      const tick = () => {
        const cls = root.className
        if (cls.includes('shell--builder-entering') && cls.includes('shell--builder-exiting')) dualClass = true
        const exiting = cls.includes('shell--builder-exiting')
        if (exiting) {
          started = true
          if (document.querySelector('.shell__view--exit-underlay')) underlaySeen = true
          for (const el of document.querySelectorAll('.shell__view[data-mode-motion]')) {
            const key = el.getAttribute('data-tab-key') || el.dataset.modeMotion
            let rec = byKey.get(key)
            if (!rec) { rec = { boxes: new Set(), transforms: new Set(), node: el }; byKey.set(key, rec) }
            rec.boxes.add(`${el.offsetWidth}x${el.offsetHeight}@${el.offsetLeft},${el.offsetTop}`)
            rec.transforms.add(getComputedStyle(el).transform)
          }
        }
        frames += 1
        if ((started && !exiting) || frames > 90) { resolve(); return }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    const wrappers = [...byKey.values()].map(r => ({
      distinctBoxes: r.boxes.size,
      distinctTransforms: r.transforms.size,
      transformsMatrix: [...r.transforms].every(t => t === 'none' || t.startsWith('matrix')),
      survived: r.node.isConnected,
    }))
    return { started, dualClass, underlaySeen, wrappers }
  })
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
    const root = document.querySelector('.shell')
    window.__modeViolations = []
    window.__modeObs = new MutationObserver(() => {
      const c = root.className
      if (c.includes('shell--builder-entering') && c.includes('shell--builder-exiting')) {
        window.__modeViolations.push(c)
      }
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
    const c = document.querySelector('.shell').className
    return ['shell--builder-entering', 'shell--builder-exiting'].filter(k => c.includes(k)).length
  })
}

for (const [name, viewport] of [
  ['phone', { width: 412, height: 915 }],
  ['wide', { width: 1280, height: 900 }],
]) {
  test(`[${name}] a single builder toggle flips the mode and settles clean`, async ({ page }) => {
    await bootShell(page, viewport)
    // A fresh workspace seeds viewMode:'panes' (builder), so do NOT hardcode the
    // initial direction (finding 15) — read it and assert the toggle FLIPS it.
    const before = await builderActive(page)
    await toggleMode(page)
    await expect.poll(() => builderActive(page)).toBe(!before)
    // The beat settles: no transient class lingers.
    await expect.poll(() => transientClassCount(page), { timeout: 2000 }).toBe(0)
    await expect.poll(() => modePhase(page)).toBe('idle')
  })

  test(`[${name}] a cancelled single-mode drag UNTILES (BLOCKER 1: no permanent tile)`, async ({ page }) => {
    await bootShell(page, viewport)
    // Ensure SINGLE mode (a fresh workspace is builder).
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
    await bootShell(page, viewport)
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
    await bootShell(page, viewport)
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

// ── Exit-presentation v2 browser coverage (R4) ───────────────────────────────
// Frame-sampled proof of the compositor-only contract in a real browser. Wide only
// (two visible panes need a wide viewport). A seeded 2-pane builder is exited and
// every frame of the beat is sampled: the participant wrappers' LAYOUT boxes must
// stay constant while their transforms animate, and the same nodes must survive.
const WIDE = { width: 1280, height: 900 }

test('v2 exit is compositor-only: layout boxes constant while transforms animate, nodes survive', async ({ page }) => {
  // slot === the focused LEFT pane's chat → PROMOTE it (a real half→full FLIP scale)
  // and deal the right sibling out. Both are compositor-only participants.
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'aaa' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  await expect(page.locator('.workspace__strip')).toHaveCount(2)
  const sampler = sampleExitBeat(page)
  await page.waitForTimeout(30) // let the rAF sampler install before the toggle
  await toggleMode(page)
  const r = await sampler
  expect(r.started, 'an exit beat ran').toBe(true)
  expect(r.dualClass, 'INV 1: never both beat classes at once').toBe(false)
  expect(r.wrappers.length, 'the participant wrappers were sampled').toBeGreaterThanOrEqual(2)
  // INV 5 (compositor-only): every participant's computed LAYOUT box (offset*,
  // transform-independent) stayed constant across the whole beat.
  for (const w of r.wrappers) expect(w.distinctBoxes, 'layout box constant during the beat').toBe(1)
  // Only transform/opacity animated — every observed transform is a matrix (or none),
  // and at least one participant's transform actually CHANGED across frames.
  for (const w of r.wrappers) expect(w.transformsMatrix, 'only matrix transforms').toBe(true)
  expect(r.wrappers.some(w => w.distinctTransforms > 1), 'a transform animated').toBe(true)
  // INV 4 (stable identity): the same DOM nodes survived completion.
  for (const w of r.wrappers) expect(w.survived, 'same node survives completion').toBe(true)
  // The beat settled clean.
  await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('idle')
  await expect.poll(() => builderActive(page)).toBe(false)
})

test('v2 world-reveal exit paints the mounted destination underlay beneath the deal', async ({ page }) => {
  // slot === a tree-absent chat → WORLD REVEAL: every painted pane deals out over the
  // mounted underlay (INV 3 honest destination), no false promotion.
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'ghost' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleExitBeat(page)
  await page.waitForTimeout(30)
  await toggleMode(page)
  const r = await sampler
  expect(r.started).toBe(true)
  expect(r.underlaySeen, 'the reveal underlay was painted beneath the deal').toBe(true)
  // Every participant deals out (a transform animates) with a constant layout box.
  for (const w of r.wrappers) {
    expect(w.distinctBoxes).toBe(1)
    expect(w.transformsMatrix).toBe(true)
  }
  expect(r.wrappers.some(w => w.distinctTransforms > 1)).toBe(true)
  await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('idle')
})

// ── Round 4 item 2: the two-phase world reveal (destination arrival) ──────────
// Frame-sample the underlay opacity across a world reveal: it must stay veiled (~.60)
// while the cards are still present, then rise to full only AFTER the last card clears
// — the destination ARRIVES as phase 2, it does not read as already-present.
async function sampleUnderlayArrival(page) {
  return page.evaluate(async () => {
    const root = document.querySelector('.shell')
    let started = false
    let maxOpacityWithCards = 0
    let roseAfterCards = false
    await new Promise((resolve) => {
      let frames = 0
      const tick = () => {
        const exiting = root.className.includes('shell--builder-exiting')
        const underlay = document.querySelector('.shell__view--exit-underlay')
        if (exiting && underlay) {
          started = true
          const op = parseFloat(getComputedStyle(underlay).opacity)
          const cards = [...document.querySelectorAll('.shell__view[data-mode-motion="deal-out"]')]
          const cardsPresent = cards.some(c => parseFloat(getComputedStyle(c).opacity) > 0.05)
          if (cardsPresent) maxOpacityWithCards = Math.max(maxOpacityWithCards, op)
          else if (op > 0.9) roseAfterCards = true
        }
        frames += 1
        if ((started && !exiting && frames > 4) || frames > 240) { resolve(); return }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    return { started, maxOpacityWithCards, roseAfterCards }
  })
}

test('round4-2: the world-reveal destination stays veiled while cards are present, then arrives', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, twoPaneBuilder({ kind: 'chat', id: 'ghost' }))
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleUnderlayArrival(page)
  await page.waitForTimeout(30)
  await toggleMode(page)
  const r = await sampler
  expect(r.started, 'a world-reveal exit ran').toBe(true)
  // Phase 1: the destination sits veiled beneath the departing cards (the `both` fill
  // holds the .60 from-frame through the arrival delay).
  expect(r.maxOpacityWithCards, 'the destination stays veiled while cards are present')
    .toBeLessThanOrEqual(0.8)
  // Phase 2: opacity rises to full only after the last card clears.
  expect(r.roseAfterCards, 'the destination arrives (rises to full) after the cards clear').toBe(true)
  await expect.poll(() => modePhase(page), { timeout: 2000 }).toBe('idle')
  await expect.poll(() => builderActive(page)).toBe(false)
})

test('round4-2: reduced motion has no intermediate exit phase (instant world flip)', async ({ page }) => {
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
        if (root.className.includes('shell--builder-exiting')
          || document.querySelector('.shell__view--exit-underlay')) sawExitPhase = true
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

// R4: same-batch descriptor atomicity for the last-tab-close auto-return. A one-tab
// builder is exited by closing its sole tab; a frame-sampler proves the descriptor
// (logo/builder class) and the emptied tree flip in the SAME commit — never an
// intermediate frame where builder is still true over an emptied single tree.
test('v2 auto-return flips the descriptor and the tree atomically (no lagging frame)', async ({ page }) => {
  await bootSeededWorkspace(page, WIDE, paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'aaa' }]))
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
    key => JSON.parse(sessionStorage.getItem(key))?.viewMode, paneModel.STORAGE_KEY,
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
    const brand = document.querySelector('.shell__brand')
    const logo = document.querySelector('.shell__logo')
    let beatHeldSeen = false
    let minScale = 1
    let epochMismatch = false
    let sawBeatClass = false
    await new Promise((resolve) => {
      let frames = 0
      const tick = () => {
        const cls = root.className
        const beatClass = cls.includes('shell--builder-entering') || cls.includes('shell--builder-exiting')
        if (beatClass) sawBeatClass = true
        if (brand.classList.contains('is-beat-held')) {
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
    const settledScale = parseFloat(getComputedStyle(logo).scale)
    return { beatHeldSeen, minScale, epochMismatch, sawBeatClass, settledScale }
  })
}

// Whether is-beat-held is on the brand RIGHT NOW (for the instant/no-compression checks).
async function beatHeldNow(page) {
  return page.evaluate(() => !!document.querySelector('.shell__brand.is-beat-held'))
}

test('round4-1: a completed HOLD keeps the logo compressed then springs back at completion', async ({ page }) => {
  await bootShell(page, WIDE)
  // Fresh boot = builder; a hold EXITS to single with an animated beat.
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
  await bootShell(page, WIDE)
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
  await bootShell(page, WIDE)
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

test('round4-1: reduced motion flips instantly with no beat class and no compression', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await bootShell(page, WIDE)
  await expect.poll(() => builderActive(page)).toBe(true)
  const sampler = sampleLogoBeat(page)
  await page.waitForTimeout(30)
  await toggleMode(page)
  const r = await sampler
  // Reduced motion commits directly: no descriptor arms, so neither the beat class nor
  // the is-beat-held compression is ever emitted (the haptic still fires in JS).
  expect(r.sawBeatClass, 'reduced motion is an instant flip — no beat class').toBe(false)
  expect(r.beatHeldSeen, 'reduced motion never emits is-beat-held').toBe(false)
  await expect.poll(() => builderActive(page)).toBe(false)
})
