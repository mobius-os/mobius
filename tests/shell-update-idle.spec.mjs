/**
 * Apply-on-idle shell update + SW-leash lock-in (design §1.3).
 *
 * The invariant: the streaming view is sacred — a shell rebuild NEVER reloads
 * or blanks a live turn. These cases exercise the SYNTHESIZED client mechanism
 * end to end (route-mocked SSE, no agent tokens):
 *
 *   1. shell_rebuilt DURING a streaming turn does NOT reload; the reload is held
 *      QUIETLY (shellReloadPolicy.shouldDeferShellReload) until idle — no toast,
 *      no popup — and the live turn keeps rendering.
 *   2. a shell_rebuilt that lands mid-turn reloads exactly ONCE at the idle
 *      boundary (the hold-until-idle recheck fires after the turn reaches
 *      `done`), and does not loop.
 *   3. shell_rebuilt delivered on the GLOBAL system stream while idle applies
 *      immediately (reloads once; the dedup window prevents a loop).
 *
 * SYNTHESIS NOTES — why these differ from a naive mock:
 *   - The per-chat stream forwards shell_rebuilt ONLY when live, not during the
 *     catch-up replay (chatSystemEvents.shouldForwardChatStreamSystemEvent). A
 *     fresh send's stream starts in catch-up, so the mock emits `catch_up_done`
 *     BEFORE shell_rebuilt to make it a LIVE signal that reaches Shell.
 *   - The reload is HELD via a recheck timer, not an event-driven boundary, so
 *     the idle apply in case 2 lands a few seconds after `done` — the waits
 *     account for that.
 *   - The mechanism is QUIET (the sibling's "Quiet shell maintenance popups"):
 *     there is no toast to assert. The observable is the reload counter.
 *
 * Reloads are observed via a sessionStorage load counter bumped in an init
 * script that runs on every navigation (including reload).
 *
 * Run: npx playwright test tests/shell-update-idle.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: see tests/_chatTracker.mjs.
attachCleanup()

function sse(events) {
  return events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
}

function fulfillStartedPost(route) {
  if (route.request().method() !== 'POST') return route.continue()
  return route.fulfill({ status: 202, body: '{"status":"started"}' })
}

function fulfillStream(body) {
  return {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    body,
  }
}

// Count page loads (initial + every reload) so a shell-update reload is
// observable. addInitScript runs before page scripts on each load.
async function trackLoads(page) {
  await page.addInitScript(() => {
    try {
      const n = Number(sessionStorage.getItem('__load_count') || '0') + 1
      sessionStorage.setItem('__load_count', String(n))
    } catch { /* ignore */ }
  })
}
const loadCount = (page) =>
  page.evaluate(() => Number(sessionStorage.getItem('__load_count') || '0'))
const resetLoadCount = (page) =>
  page.evaluate(() => sessionStorage.setItem('__load_count', '0'))

async function setup(page, { streamRoute, systemBody } = {}) {
  await page.setViewportSize({ width: 412, height: 915 })
  await trackLoads(page)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, fulfillStartedPost)
  await page.route('**/api/chat/stop', route => route.fulfill({ status: 200, body: '{}' }))
  if (streamRoute) {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, streamRoute)
  }
  if (systemBody) {
    await page.route('**/api/events/system', route => route.fulfill(fulfillStream(systemBody)))
  }
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )
}

async function gotoEmptyChat(page) {
  await createTaggedChat(page)
  await page.evaluate(() => document.querySelector('.drawer__item--new')?.click())
  const hasEmpty = await page.evaluate(() => !!document.querySelector('.chat__empty-wrap'))
  if (!hasEmpty) await page.goto(BASE)
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

test.describe('shell update — apply on idle, SW on a leash', () => {
  test('shell_rebuilt during a streaming turn does not reload; holds quietly', async ({ page }) => {
    // catch_up_done makes the shell_rebuilt a LIVE signal (past the catch-up
    // filter); the stream never sends `done`, so the turn stays live and the
    // chat stays in the streaming set. The gate must DEFER (quiet hold).
    const streamingBody = sse([
      { type: 'catch_up_done' },
      { type: 'text', content: 'building the shell...' },
      { type: 'shell_rebuilt' },
    ])
    await setup(page, { streamRoute: route => route.fulfill(fulfillStream(streamingBody)) })
    await gotoEmptyChat(page)
    await resetLoadCount(page)

    await sendMessage(page, 'rebuild the shell')

    // The live turn keeps rendering — the reload did not blank it.
    await expect(page.getByText('building the shell...')).toBeVisible({ timeout: 8000 })

    // Wait PAST the hold-until-idle recheck interval (6s): the recheck fires,
    // sees a still-streaming turn, and reschedules — it must NOT reload. This is
    // the sacred-streaming-view invariant against the timer mechanism.
    await page.waitForTimeout(7000)
    expect(await loadCount(page)).toBe(0)
  })

  test('a mid-turn shell_rebuilt applies exactly once at the turn-end idle boundary', async ({ page }) => {
    // shell_rebuilt arrives live (after catch_up_done) while streaming → defer,
    // then `done` empties the streaming set → the hold-until-idle recheck
    // applies → exactly one reload, no loop.
    //
    // Stateful route: on RECONNECT after the reload, deliver shell_rebuilt as
    // CATCH-UP (before catch_up_done) so the live-only forwarding filters it —
    // mirroring how a real broadcast replays a finished turn. This prevents a
    // re-apply loop and exercises the catch-up filter.
    let streamConnects = 0
    const firstConnect = sse([
      { type: 'catch_up_done' },
      { type: 'text', content: 'shell rebuilt' },
      { type: 'shell_rebuilt' },
      { type: 'done' },
    ])
    const reconnectReplay = sse([
      // shell_rebuilt in the catch-up portion (before catch_up_done) → filtered.
      { type: 'text', content: 'shell rebuilt' },
      { type: 'shell_rebuilt' },
      { type: 'catch_up_done' },
      { type: 'done' },
    ])
    await setup(page, {
      streamRoute: route => {
        streamConnects += 1
        return route.fulfill(fulfillStream(streamConnects === 1 ? firstConnect : reconnectReplay))
      },
    })
    await gotoEmptyChat(page)
    await resetLoadCount(page)

    await sendMessage(page, 'rebuild the shell')

    // Reloads once when the turn goes idle. The recheck interval (6s) + reload
    // delay put this a few seconds after `done`; allow generous headroom.
    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') === 1,
      { timeout: 15000 },
    )
    // And does NOT loop: the reconnect's shell_rebuilt is a catch-up replay and
    // is filtered, so the reloaded page does not re-apply.
    await page.waitForTimeout(2000)
    expect(await loadCount(page)).toBe(1)
  })

  test('shell_rebuilt on the global system stream while idle applies immediately', async ({ page }) => {
    // No turn is streaming; the global system stream delivers shell_rebuilt at
    // load. Idle → immediate apply → one reload (the 5s dedup, which survives
    // the reload in sessionStorage, prevents a loop).
    const systemBody = sse([
      { type: 'system_stream_open' },
      { type: 'shell_rebuilt' },
    ])
    // The initial goto in `setup` is load #1; the idle-immediate apply reload
    // makes it #2. No pre-reset — the base is the initial load.
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(sse([{ type: 'done' }]))),
      systemBody,
    })

    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') >= 2,
      { timeout: 8000 },
    )
    const after = await loadCount(page)
    // One apply-reload on top of the initial load; no immediate loop.
    await page.waitForTimeout(1500)
    expect(await loadCount(page)).toBe(after)
  })
})
