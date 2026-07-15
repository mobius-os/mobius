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
 *   2. passive shell_rebuilt generations stay coalesced while an idle chat is
 *      visible, so source-save bursts cannot interrupt a reader.
 *   3. a deliberate shell_apply_now that lands mid-turn reloads exactly ONCE
 *      at the idle boundary, captures the current anchor, carries the terminal
 *      transcript across that reload, and does not loop.
 *   4. after a REAL SW update (a genuinely new, WAITING worker), an idle apply
 *      lands the page on the NEW generation — controlled by the registration's
 *      ACTIVE worker with nothing left waiting. Deliberate-apply cases assert
 *      apply-ONCE but never WHICH generation the page ends on; feature 207 is
 *      precisely an apply that reloads onto the OUTGOING generation and sticks,
 *      so this case pins generation identity (the gap that let 207 ship).
 *
 * SYNTHESIS NOTES — why these differ from a naive mock:
 *   - shell_rebuilt is SYSTEM-BUS-ONLY: the backend never fans it out to
 *     per-chat broadcasts (a chat reconnect replaying a stale rebuilt would
 *     fire a spurious apply), so the mock delivers it over /api/events/system.
 *     The mocked system route mirrors the REAL SystemBroadcast contract — live
 *     delivery, NO replay on reconnect (oneShotSystemEventRoute) — which is
 *     exactly why the client needs no dedup stamps, and why a mock that
 *     redelivered on every reconnect would (rightly) reload-loop.
 *   - A deliberate reload is HELD via a recheck timer, not an event-driven
 *     boundary, so the idle apply lands a few seconds after `done` — the waits
 *     account for that.
 *   - The mechanism is QUIET (the sibling's "Quiet shell maintenance popups"):
 *     there is no toast to assert. The observable is the reload counter.
 *
 * Reloads are observed via a sessionStorage load counter bumped in an init
 * script that runs on every navigation (including reload).
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/shell-update-idle.spec.mjs
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

// One-shot system-stream mock, faithful to the real SystemBroadcast contract:
// hold the connection until `armed` resolves, then deliver shell_rebuilt on
// exactly ONE successful connection — every other connection (before the arm,
// after delivery, or the post-reload reconnect) gets only the hello. No
// replay on reconnect is the load-bearing property: with the sessionStorage
// dedup stamps gone, single delivery is what prevents a reload loop, so a
// mock that redelivered would fail these cases for the right reason.
function oneShotSystemEventRoute(eventType, armed) {
  let delivered = false
  return async (route) => {
    try {
      if (!delivered) {
        await armed
        // Re-check after waking: a connection that a test-page navigation
        // aborted may have woken first and thrown on fulfill, leaving
        // delivery to this (live) one.
        if (!delivered) {
          await route.fulfill(fulfillStream(sse([
            { type: 'system_stream_open' },
            { type: eventType },
          ])))
          delivered = true
          return
        }
      }
      await route.fulfill(fulfillStream(sse([{ type: 'system_stream_open' }])))
    } catch {
      // The connection died while held (navigation aborts in-flight
      // requests). `delivered` is still false, so the next connection
      // gets the event.
    }
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

async function setup(page, { streamRoute, systemBody, systemRoute } = {}) {
  await page.setViewportSize({ width: 412, height: 915 })
  await trackLoads(page)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, fulfillStartedPost)
  await page.route('**/api/chat/stop', route => route.fulfill({ status: 200, body: '{}' }))
  if (streamRoute) {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, streamRoute)
  }
  if (systemRoute) {
    await page.route('**/api/events/system', systemRoute)
  } else if (systemBody) {
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
  const chat = await createTaggedChat(page)
  if (!chat?.id) throw new Error('failed to create isolated shell-update chat')
  await page.goto(`${BASE}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })
  return chat
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

test.describe('shell update — apply on idle, SW on a leash', () => {
  test('shell_rebuilt during a streaming turn does not reload; holds quietly', async ({ page }) => {
    // The chat stream never sends `done`, so the turn stays live and the chat
    // stays in the streaming set. shell_rebuilt arrives on the GLOBAL system
    // stream — its only channel — while the turn is streaming; the gate must
    // DEFER (quiet hold). The arm resolves only after the turn is visibly
    // streaming, so the delivery is deterministically mid-turn.
    let armRebuilt
    const armed = new Promise(resolve => { armRebuilt = resolve })
    const streamingBody = sse([
      { type: 'catch_up_done' },
      { type: 'text', content: 'building the shell...' },
    ])
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(streamingBody)),
      systemRoute: oneShotSystemEventRoute('shell_rebuilt', armed),
    })
    await gotoEmptyChat(page)
    await resetLoadCount(page)

    await sendMessage(page, 'rebuild the shell')

    // The live turn is rendering — NOW deliver shell_rebuilt mid-turn.
    await expect(page.getByText('building the shell...')).toBeVisible({ timeout: 8000 })
    armRebuilt()

    // Wait PAST the hold-until-idle recheck interval (6s): the recheck fires,
    // sees a still-streaming turn, and reschedules — it must NOT reload. This is
    // the sacred-streaming-view invariant against the timer mechanism.
    await page.waitForTimeout(7000)
    expect(await loadCount(page)).toBe(0)
  })

  test('a deliberate mid-turn shell_apply_now applies exactly once at the turn-end idle boundary', async ({ page }) => {
    // shell_apply_now arrives on the system stream while the turn streams →
    // defer; the chat stream's `done` (held back so the rebuilt lands
    // genuinely mid-turn) then empties the streaming set → the hold-until-idle
    // recheck applies → exactly one reload. No loop: the post-reload system
    // reconnect carries NO replay (SystemBroadcast has none) — the property
    // that lets single-bus delivery need no client dedup.
    let armRebuilt
    const armed = new Promise(resolve => { armRebuilt = resolve })
    let releasePostReloadChatRead
    const postReloadChatReadReleased = new Promise(resolve => {
      releasePostReloadChatRead = resolve
    })
    let holdChatReads = false
    // The route-mocked SSE is intentionally not persisted by the backend. Hold
    // the post-reload authoritative GET so this test observes the reload
    // handoff itself: the terminal assistant row must hydrate from the cache
    // Shell explicitly flushed before navigation. Without that flush, the last
    // streamed line disappears until a later remount/refetch — the production
    // regression this assertion locks in.
    await page.route(/\/api\/chats\/[0-9a-f-]+(?:\?.*)?$/, async route => {
      if (route.request().method() === 'GET' && holdChatReads) {
        await postReloadChatReadReleased
      }
      return route.continue()
    })
    let streamConnects = 0
    await setup(page, {
      streamRoute: async route => {
        streamConnects += 1
        if (streamConnects === 1) {
          // Hold `done` back a beat: the send marks the chat streaming
          // synchronously, the armed system stream delivers shell_rebuilt
          // within that window (defer), and `done` lands after.
          await new Promise(resolve => setTimeout(resolve, 2500))
          try {
            await route.fulfill(fulfillStream(sse([
              { type: 'catch_up_done' },
              { type: 'text', content: 'shell rebuilt' },
              { type: 'done' },
            ])))
          } catch { /* aborted by the apply-reload — nothing to deliver */ }
          return
        }
        // Reconnect replay of the finished turn: content, boundary, done.
        return route.fulfill(fulfillStream(sse([
          { type: 'text', content: 'shell rebuilt' },
          { type: 'catch_up_done' },
          { type: 'done' },
        ])))
      },
      systemRoute: oneShotSystemEventRoute('shell_apply_now', armed),
    })
    await gotoEmptyChat(page)
    await resetLoadCount(page)
    await page.evaluate(() => {
      window.addEventListener('mobius:before-shell-reload', () => {
        sessionStorage.setItem('__before_shell_reload_seen', '1')
      }, { once: true })
    })

    await sendMessage(page, 'rebuild the shell')
    // The send marks the chat streaming synchronously (onMessageStart), so
    // arming here delivers the rebuilt while the turn is live.
    armRebuilt()
    holdChatReads = true

    // Reloads once when the turn goes idle. The recheck interval (6s) + reload
    // delay put this a few seconds after `done`; allow generous headroom.
    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') === 1,
      { timeout: 20000 },
    )
    await expect(page.getByText('shell rebuilt')).toBeVisible({ timeout: 1500 })
    releasePostReloadChatRead()
    expect(await page.evaluate(() => (
      sessionStorage.getItem('__before_shell_reload_seen')
    ))).toBe('1')
    // And does NOT loop: the reloaded page's system reconnect gets only the
    // hello (no replay), so nothing re-applies.
    await page.waitForTimeout(2000)
    expect(await loadCount(page)).toBe(1)
  })

  test('passive shell_rebuilt stays queued while an idle chat is visible', async ({ page }) => {
    let armRebuilt
    const armed = new Promise(resolve => { armRebuilt = resolve })
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(sse([{ type: 'done' }]))),
      systemRoute: oneShotSystemEventRoute('shell_rebuilt', armed),
    })
    await gotoEmptyChat(page)
    await resetLoadCount(page)

    armRebuilt()
    // Wait past the six-second recheck: a passive generation must remain
    // coalesced for as long as this idle chat is still the visible surface.
    await page.waitForTimeout(7000)
    expect(await loadCount(page)).toBe(0)
  })

  test('a queued passive rebuild releases when the visible chat is backgrounded', async ({ page }) => {
    let armRebuilt
    const armed = new Promise(resolve => { armRebuilt = resolve })
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(sse([{ type: 'done' }]))),
      systemRoute: oneShotSystemEventRoute('shell_rebuilt', armed),
    })
    await gotoEmptyChat(page)
    await resetLoadCount(page)

    armRebuilt()
    await page.waitForTimeout(500)
    expect(await loadCount(page)).toBe(0)

    // Headless Chromium keeps the only page visible, so shadow the readonly
    // getter for this document and dispatch the real lifecycle event. The
    // override disappears with the reload.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        value: 'hidden',
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') === 1,
      { timeout: 8000 },
    )
  })

  test('shell_apply_now on the global system stream while idle applies immediately', async ({ page }) => {
    // No turn is streaming; the global system stream delivers shell_apply_now at
    // load. Idle → immediate apply → one reload. Loop prevention is single
    // delivery itself: the post-reload reconnect gets no replay, exactly like
    // the real SystemBroadcast (the old sessionStorage dedup is gone).
    //
    // The initial goto in `setup` is load #1; the idle-immediate apply reload
    // makes it #2. No pre-reset — the base is the initial load.
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(sse([{ type: 'done' }]))),
      systemRoute: oneShotSystemEventRoute('shell_apply_now', Promise.resolve()),
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

  test('an idle apply lands the page on the new SW generation, not the outgoing one', async ({ page }) => {
    // Publish a genuinely new, WAITING service worker (a real update cycle — the
    // window feature 207 bit, biased to a client's FIRST cycle after install),
    // drive the idle apply through the shell's own recovery path, and assert the
    // page settles CONTROLLED BY THE REGISTRATION'S ACTIVE WORKER with nothing
    // left waiting. That is generation identity: it landed on the new generation,
    // not back on the outgoing one.
    let swMarker = ''
    await page.route('**/sw.js', async (route) => {
      const res = await route.fetch()
      let body = await res.text()
      // A STABLE byte-append once armed → the browser installs ONE genuinely new,
      // leashed worker; later re-fetches stay byte-identical so it does not
      // reinstall. The bundle is unchanged — the new WORKER's identity is the
      // generation the page must land on.
      if (swMarker) body += `\n//${swMarker}\n`
      await route.fulfill({ status: res.status(), headers: res.headers(), body })
    })
    await setup(page, {
      streamRoute: route => route.fulfill(fulfillStream(sse([{ type: 'done' }]))),
      systemBody: sse([{ type: 'system_stream_open' }]),
    })
    const idleChat = await gotoEmptyChat(page)
    // gen A controls the page (first install claims).
    await page.waitForFunction(() => !!navigator.serviceWorker?.controller, { timeout: 15000 })

    // Keep a second gen-A client alive. Without it, navigation commonly leaves
    // no outgoing client and Chromium activates gen B before the shell mounts,
    // so the test never exercises the mount-time pickup path it claims to pin.
    const keeper = await page.context().newPage()
    await keeper.goto(BASE, { waitUntil: 'domcontentloaded' })
    await keeper.waitForFunction(() => !!navigator.serviceWorker?.controller, { timeout: 15000 })

    // Publish gen B; wait until it is installed and WAITING (leashed — the SW
    // never skipWaiting()s on its own).
    swMarker = 'e2e gen B'
    await page.evaluate(async () => { await (await navigator.serviceWorker.getRegistration()).update() })
    await page.waitForFunction(
      async () => !!(await navigator.serviceWorker.getRegistration())?.waiting,
      { timeout: 15000 },
    )

    // The hosted suite shares one backend across parallel Playwright workers.
    // This case is specifically the IDLE recovery path, so do not let an
    // unrelated worker's running fixture make the shell correctly defer its
    // generation handoff. Keep the live-confirmation fetch real for this page,
    // but scope its list to the chat this test owns.
    await page.route(/\/api\/chats\/?(?:\?.*)?$/, async route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          ...idleChat,
          running: false,
          run_status: 'idle',
        }]),
      })
    })

    // The synthetic gen B changes only a trailing sw.js comment, so its
    // advertised page bundle is intentionally identical to gen A. A real shell
    // publish changes that bundle hash and the boot check sets this recovery
    // flag; seed the same public signal so this case exercises the resulting
    // new-document handoff rather than an indistinguishable no-op generation.
    await page.evaluate(() => sessionStorage.setItem('sw-stale-precache-pending', '1'))
    await resetLoadCount(page)
    // Re-mount the shell so its once-per-mount pickup finds the waiting worker and
    // re-arms the idle apply — the recovery path a client hits when a newer
    // generation is installed but the page has not adopted it.
    await page.reload({ waitUntil: 'domcontentloaded' })

    // Controller identity can flip to gen B while this document is still
    // executing gen A's precached bundle. The explicit reload above is load 1;
    // mount-time pickup must remember the pre-fetch stale-generation signal,
    // hand off the worker, and perform load 2. Requiring that navigation proves
    // the document generation changed instead of accepting controller takeover
    // alone as a false positive.
    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') >= 2,
      { timeout: 20000 },
    )

    // Generation identity: the apply settles with the page controlled by the
    // registration's ACTIVE worker and NOTHING left waiting.
    await page.waitForFunction(async () => {
      const reg = await navigator.serviceWorker.getRegistration()
      return !!reg && !reg.waiting && !!reg.active
        && navigator.serviceWorker.controller === reg.active
    }, { timeout: 20000 })

    // And it does not loop or drift back: the settled state holds.
    await page.waitForTimeout(1500)
    const stable = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration()
      return !reg.waiting && navigator.serviceWorker.controller === reg.active
    })
    expect(stable).toBe(true)
    await keeper.close()
  })
})
