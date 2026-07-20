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
